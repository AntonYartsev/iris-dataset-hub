"""shared table loader: one small flat CSV / Parquet / Arrow unit -> one replaced table in dc_hub_data

runs inside the IRIS process (Embedded Python). Reading and type checks never touch IRIS;
only replace_table() drops, creates and writes, and only in schema dc_hub_data

bind values follow the iris.sql rules observed in P2-01: "" binds as SQL NULL, "\\x00" as the
empty string, bool as 1/0, and doubles are passed as text
"""
import csv
import math
import os
import re
import sys
import unicodedata

import numpy
import pyarrow
import pyarrow.compute
import pyarrow.parquet

SCHEMA = "dc_hub_data"
MAX_ROWS = 1_000_000
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_NAME = 64
# longest string IRIS can store; a whole row must fit into it as well
IRIS_MAX_STRING = 3_641_144
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1
# decimal significant digits that survive text -> double -> text
DOUBLE_DIGITS = 15

NULL = ""
EMPTY = "\x00"

_INT = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUM = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_TARGET = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class LoadError(Exception):
    """expected failure; the message is shown to the user as is"""


class TableWriteError(LoadError):
    """failure after the target table was dropped. state: "removed" or "unknown"."""

    def __init__(self, message, state):
        super().__init__(message)
        self.state = state


class Column:
    def __init__(self, source, name, sql_type, values):
        self.source = source        # column name in the file / dataset
        self.name = name            # normalized SQL column name
        self.sql_type = sql_type    # BIGINT, DOUBLE, BIT or VARCHAR(n)
        self.values = values        # bind-ready values, one per row


class TableData:
    """a unit that was read and checked completely; nothing in IRIS was touched yet"""

    def __init__(self, columns, rows):
        if not columns:
            raise LoadError("no columns")
        self.columns = columns
        self.rows = rows


# ---------------------------------------------------------------- names

def sql_name(text, fallback, prefix):
    """lower-case ASCII identifier [a-z0-9_] that also works unquoted in SELECT; fallback when
    nothing is left; prefix before a leading digit or an IRIS SQL reserved word (date -> c_date)"""
    import iris
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or fallback
    if name[0].isdigit() or iris.system.SQL.IsReservedWord(name):
        name = prefix + name
    return name[:MAX_NAME].rstrip("_")


def table_name(text):
    return sql_name(text, "data", "t_")


def unique_names(names):
    """resolves collisions in order with suffixes _2, _3, ... (names are already lower-case)"""
    taken, out = set(), []
    for name in names:
        candidate, k = name, 2
        while candidate in taken:
            suffix = "_%d" % k
            candidate = name[:MAX_NAME - len(suffix)] + suffix
            k += 1
        taken.add(candidate)
        out.append(candidate)
    return out


def check_target(target):
    """a user-supplied table name or prefix: a plain identifier, never SQL or schema.table"""
    if not _TARGET.fullmatch(target) or len(target) > MAX_NAME:
        raise LoadError(
            "target %r is not allowed: use letters, digits and _ (starting with a letter, at most %d "
            "characters); tables are always created in schema %s" % (target, MAX_NAME, SCHEMA))
    return target.lower()


def qualified(name):
    """delimited name for SQL statements"""
    return '%s."%s"' % (SCHEMA, name.replace('"', '""'))


def full_name(name):
    """name shown in results; normalized names also work unquoted"""
    return "%s.%s" % (SCHEMA, name)


# ---------------------------------------------------------------- values

def _utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def _varchar(source, values):
    """VARCHAR(n) column from str/None values; n is the longest value in UTF-16 units"""
    width = 1
    out = []
    for row, value in enumerate(values, 1):
        if value is None:
            out.append(NULL)
            continue
        if value == EMPTY:
            raise LoadError("column %r, row %d: a value made of a single NUL character is not supported"
                            % (source, row))
        size = _utf16_len(value)
        if size > IRIS_MAX_STRING:
            raise LoadError("column %r, row %d: value of %d characters is longer than IRIS can store (%d)"
                            % (source, row, size, IRIS_MAX_STRING))
        width = max(width, size)
        out.append(value if value else EMPTY)
    return "VARCHAR(%d)" % width, out


def _fits_double(text):
    """True if an ordinary decimal number keeps its value in a double (<= 15 significant digits,
    finite, not underflowing)"""
    mantissa = text.lstrip("-").lower().partition("e")[0]
    digits = mantissa.replace(".", "").strip("0")
    if not digits:
        return True
    value = abs(float(text))
    return len(digits) <= DOUBLE_DIGITS and math.isfinite(value) and value >= sys.float_info.min


def _csv_kind(text):
    if _INT.fullmatch(text):
        if text != "-0" and len(text) <= 20 and INT64_MIN <= int(text) <= INT64_MAX:
            return "int"
        return "text"
    if _NUM.fullmatch(text) and _fits_double(text):
        return "num"
    return "text"


def _csv_column(source, name, values):
    """type for a whole column of raw CSV text (None = empty field)"""
    kinds = set()
    for value in values:
        if value is not None:
            kinds.add(_csv_kind(value))
            if "text" in kinds:
                break
    if kinds == {"int"}:
        return Column(source, name, "BIGINT", [NULL if v is None else int(v) for v in values])
    if kinds == {"num"} or (kinds == {"int", "num"} and all(v is None or _fits_double(v) for v in values)):
        return Column(source, name, "DOUBLE", [NULL if v is None else repr(float(v)) for v in values])
    return Column(source, name, *_varchar(source, values))


def _float_text(arrow_type):
    if pyarrow.types.is_float32(arrow_type):
        return lambda v: str(numpy.float32(v))  # shortest float32 text: 1.4, not 1.399999976158142
    if pyarrow.types.is_float16(arrow_type):
        return lambda v: str(numpy.float16(v))
    return repr


_FLAT = (pyarrow.types.is_null, pyarrow.types.is_boolean, pyarrow.types.is_integer, pyarrow.types.is_floating,
         pyarrow.types.is_string, pyarrow.types.is_large_string, pyarrow.types.is_string_view,
         pyarrow.types.is_decimal, pyarrow.types.is_date, pyarrow.types.is_time, pyarrow.types.is_timestamp)


def _check_flat(source, arrow_type):
    value_type = arrow_type.value_type if pyarrow.types.is_dictionary(arrow_type) else arrow_type
    if not any(test(value_type) for test in _FLAT):
        raise LoadError("column %r has type %s; only flat scalar columns (integer, floating point, boolean, "
                        "text, decimal, date/time) are supported" % (source, arrow_type))


def check_schema(schema):
    """stops on the first column read_arrow would reject (used before a download)"""
    for field in schema:
        _check_flat(field.name, field.type)


def _arrow_column(source, name, column):
    """type for a materialized Arrow column; anything but flat scalars stops the import"""
    _check_flat(source, column.type)
    if pyarrow.types.is_dictionary(column.type):
        column = pyarrow.compute.cast(column, column.type.value_type)
    arrow_type = column.type
    if pyarrow.types.is_date(arrow_type) or pyarrow.types.is_time(arrow_type) or pyarrow.types.is_timestamp(arrow_type):
        # Arrow's own text; to_pylist() cannot convert every unit (nanoseconds)
        return Column(source, name, *_varchar(source, pyarrow.compute.cast(column, pyarrow.string()).to_pylist()))
    values = column.to_pylist()
    if pyarrow.types.is_boolean(arrow_type):
        return Column(source, name, "BIT", [NULL if v is None else int(v) for v in values])
    if pyarrow.types.is_integer(arrow_type) and all(v is None or INT64_MIN <= v <= INT64_MAX for v in values):
        return Column(source, name, "BIGINT", [NULL if v is None else v for v in values])
    if pyarrow.types.is_floating(arrow_type):
        text = _float_text(arrow_type)
        values = [None if v is None else text(v) for v in values]
        if all(v is None or math.isfinite(float(v)) for v in values):
            return Column(source, name, "DOUBLE", [NULL if v is None else v for v in values])
    # null, text, decimal, integers beyond BIGINT, and nan / inf (never stored as DOUBLE)
    return Column(source, name, *_varchar(source, [None if v is None else str(v) for v in values]))


# ---------------------------------------------------------------- readers

def _check_rows(rows):
    if rows > MAX_ROWS:
        raise LoadError("more than %d rows; this loader is meant for small datasets" % MAX_ROWS)


def _check_file_size(path):
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise LoadError("file is %d bytes, more than the %d MB limit" % (size, MAX_FILE_BYTES // 2 ** 20))


def _column_names(sources):
    names = [sql_name(s, "column_%d" % i, "c_") for i, s in enumerate(sources, 1)]
    return unique_names(names)


def read_arrow(table):
    """pyarrow.Table (Parquet file or materialized Hugging Face split) -> TableData"""
    _check_rows(table.num_rows)
    sources = table.column_names
    columns = [_arrow_column(s, n, c) for s, n, c in zip(sources, _column_names(sources), table.columns)]
    return TableData(columns, table.num_rows)


def read_parquet(path):
    _check_file_size(path)
    try:
        table = pyarrow.parquet.read_table(path)
    except (pyarrow.ArrowException, OSError) as e:
        raise LoadError("cannot read Parquet: %s" % e)
    return read_arrow(table)


def read_csv(path):
    """comma-separated UTF-8 text with a header row. Values stay raw text until the whole column is
    known; an empty field is NULL, every other value (NA, ., 00123, ...) is kept as written"""
    _check_file_size(path)
    csv.field_size_limit(IRIS_MAX_STRING)
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise LoadError("empty file: no header row")
            if not header:
                raise LoadError("line 1: empty header row")
            width = len(header)
            values = [[] for _ in header]  # one list per column, an empty field is None
            rows, blank_lines = 0, []
            for record in reader:
                if not record:
                    blank_lines.append(reader.line_num)
                    continue
                if blank_lines:
                    if width != 1:
                        raise LoadError("line %d: blank line inside the data" % blank_lines[0])
                    values[0].extend([None] * len(blank_lines))  # a NULL value in a one-column file
                    rows += len(blank_lines)
                    blank_lines = []
                if len(record) != width:
                    raise LoadError("line %d: %d fields, the header has %d" % (reader.line_num, len(record), width))
                for column, value in zip(values, record):
                    column.append(value or None)
                rows += 1
                _check_rows(rows)
            # blank lines at the very end of the file are ignored
    except UnicodeDecodeError as e:
        raise LoadError("not UTF-8 text: %s" % e)
    except csv.Error as e:
        raise LoadError("malformed CSV near line %d: %s" % (reader.line_num, e))
    columns = [_csv_column(s, n, v) for s, n, v in zip(header, _column_names(header), values)]
    return TableData(columns, rows)


READERS = {".csv": read_csv, ".parquet": read_parquet}


def read_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in READERS:
        raise LoadError("unsupported file type %r (supported: %s)" % (ext, ", ".join(READERS)))
    return READERS[ext](path)


# ---------------------------------------------------------------- IRIS

def _column_ddl(column):
    """strings get exact collation: IRIS defaults to SQLUPPER, under which = and GROUP BY ignore case and
    trailing spaces ('A1', 'a1' and 'a1 ' become one group)"""
    collate = " COLLATE %EXACT" if column.sql_type.startswith("VARCHAR") else ""
    return '"%s" %s%s' % (column.name, column.sql_type, collate)


def replace_table(name, data):
    """drops and recreates dc_hub_data.<name>, writes every row with one prepared INSERT and returns
    the row count read back. A failure after the drop removes the table again (TableWriteError)"""
    import iris
    table = qualified(name)
    try:
        iris.sql.exec("DROP TABLE IF EXISTS " + table)
    except Exception as e:
        raise LoadError("cannot replace %s: %s" % (table, e))
    try:
        cols = ", ".join('"%s"' % c.name for c in data.columns)
        iris.sql.exec("CREATE TABLE %s (%s)" % (table, ", ".join(_column_ddl(c) for c in data.columns)))
        insert = iris.sql.prepare("INSERT INTO %s (%s) VALUES (%s)" % (table, cols, ", ".join("?" * len(data.columns))))
        for row, values in enumerate(zip(*(c.values for c in data.columns)), 1):
            try:
                insert.execute(*values)
            except Exception as e:
                raise LoadError("row %d was rejected by IRIS: %s" % (row, e))
        count = list(iris.sql.exec("SELECT COUNT(*) FROM " + table))[0][0]
        if count != data.rows:
            raise LoadError("%d rows read but %d found in the table" % (data.rows, count))
        return count
    except Exception as e:
        try:
            iris.sql.exec("DROP TABLE IF EXISTS " + table)
            state = "removed"
        except Exception:
            state = "unknown"
        raise TableWriteError(str(e), state)


def message(error):
    """one-line error text; LoadError messages as is, others prefixed with the exception type"""
    text = str(error) if isinstance(error, LoadError) else "%s: %s" % (type(error).__name__, error)
    return " ".join(text.split())[:1000]


def shortened(items, limit, noun):
    """the first limit items, then one "... N <noun> in total" entry when there are more"""
    if len(items) > limit:
        return items[:limit] + ["... %d %s in total" % (len(items), noun)]
    return items


def fail(summary, error):
    """summary for a load that stopped before any table was touched"""
    summary.update(status="failed", tables=[], rowsWritten=0, error=message(error))
    return summary


def run(summary, units):
    """loads units in order into summary (a dict that already holds the source fields).
    units: list of (label, table name, read) where read() returns TableData.
    The first failure stops the import; tables already replaced stay listed"""
    summary.update(status="failed", tables=[], rowsWritten=0)
    for i, (label, name, read) in enumerate(units):
        try:
            data = read()
            rows = replace_table(name, data)
        except Exception as e:
            summary["error"] = "%s: %s" % (label, message(e))
            summary["failed"] = {"from": label, "table": full_name(name),
                                 "tableState": getattr(e, "state", "unchanged")}
            rest = [u[0] for u in units[i + 1:]]
            if rest:
                summary["notAttempted"] = rest
            return summary
        summary["tables"].append({"from": label, "table": full_name(name), "rows": rows, "columns": len(data.columns)})
        summary["rowsWritten"] += rows
    summary["status"] = "success"
    return summary

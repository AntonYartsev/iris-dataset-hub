"""local tests of the shared table loader (no network). Run inside IRIS by tests/run.sh"""
import datetime
import decimal
import os
import tempfile
import unittest
from unittest import mock

import iris
import pyarrow
import pyarrow.parquet

from dc_hub import loader
from dc_hub.loader import LoadError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    return os.path.join(FIXTURES, name)


def query(sql, *args):
    """rows as lists, with SQL NULL as None and the empty string as ''"""
    def value(v):
        if isinstance(v, str):
            return None if v == "" else "" if v == "\x00" else v
        return v
    return [[value(v) for v in row] for row in iris.sql.exec(sql, *args)]


def table_exists(schema, table):
    return query("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
                 schema, table)[0][0] == 1


def sql_columns(table):
    """{name: type} with VARCHAR length, in column order"""
    out = {}
    for name, data_type, length in query(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION", loader.SCHEMA, table):
        out[name] = "%s(%s)" % (data_type, length) if data_type == "varchar" else data_type
    return out


def types(data):
    return {c.name: c.sql_type for c in data.columns}


class NamesTest(unittest.TestCase):

    def test_sql_names(self):
        self.assertEqual(loader.sql_name("Culmen Length (mm)", "x", "c_"), "culmen_length_mm")
        self.assertEqual(loader.sql_name("studyName", "x", "c_"), "studyname")
        self.assertEqual(loader.sql_name("2nd value", "x", "c_"), "c_2nd_value")
        self.assertEqual(loader.table_name("2019 data.csv"), "t_2019_data_csv")
        self.assertEqual(loader.sql_name("Zürich", "x", "c_"), "zurich")
        self.assertEqual(loader.sql_name("東京", "column_3", "c_"), "column_3")
        self.assertEqual(loader.sql_name("%%", "column_1", "c_"), "column_1")
        self.assertEqual(loader.sql_name("Date", "x", "c_"), "c_date")
        self.assertEqual(loader.sql_name("count", "x", "c_"), "c_count")
        self.assertEqual(loader.table_name("user"), "t_user")
        self.assertEqual(loader.sql_name("order", "x", "c_"), "order")  # not reserved in IRIS SQL
        self.assertEqual(len(loader.sql_name("a" * 100, "x", "c_")), loader.MAX_NAME)

    def test_unique_names(self):
        self.assertEqual(loader.unique_names(["a", "a", "a_2", "b", "a"]), ["a", "a_2", "a_2_2", "b", "a_3"])
        long = "x" * loader.MAX_NAME
        out = loader.unique_names([long, long])
        self.assertEqual(out[1], "x" * (loader.MAX_NAME - 2) + "_2")

    def test_target(self):
        self.assertEqual(loader.check_target("My_Table1"), "my_table1")
        for bad in ["", "dc_hub_data.t", "SQLUser.t", "t; DROP TABLE x", '"t"', "1abc", "_t", "a b", "a" * 65]:
            with self.assertRaises(LoadError, msg=bad):
                loader.check_target(bad)


class CsvTypesTest(unittest.TestCase):
    """type decisions over whole raw-text columns (no IRIS access)"""

    def test_kinds(self):
        data = loader.read_csv(fixture("kinds.csv"))
        self.assertEqual(data.rows, 4)
        self.assertEqual(types(data), {
            "big": "BIGINT", "dbl": "DOUBLE", "over": "VARCHAR(19)", "plus": "VARCHAR(2)", "dot": "VARCHAR(2)",
            "nan": "VARCHAR(3)", "huge": "VARCHAR(5)", "prec": "VARCHAR(19)", "mixed": "VARCHAR(3)",
            "c_date": "VARCHAR(19)", "empty": "VARCHAR(1)", "na": "VARCHAR(3)", "neg_zero": "VARCHAR(2)",
            "long_int_in_dbl": "VARCHAR(16)"})
        values = {c.name: c.values for c in data.columns}
        self.assertEqual(values["big"], [2 ** 63 - 1, -(2 ** 63), 0, ""])
        self.assertEqual(values["dbl"], ["1.0", "2.5", "-1e-05", "300.0"])
        self.assertEqual(values["na"], ["NA", ".", "3.5", ""])
        self.assertEqual(values["empty"], ["", "", "", ""])

    def test_leading_zero_in_last_row(self):
        data = loader.read_csv(fixture("late_zero.csv"))
        self.assertEqual(data.rows, 60)
        self.assertEqual(types(data), {"id": "BIGINT", "code": "VARCHAR(5)", "score": "DOUBLE"})
        self.assertEqual(data.columns[1].values[0], "1001")
        self.assertEqual(data.columns[1].values[-1], "00123")

    def test_column_name_collisions(self):
        data = loader.read_csv(fixture("names.csv"))
        self.assertEqual([c.name for c in data.columns], ["name", "name_2", "name_3", "name_2_2", "column_5", "column_6"])

    def test_broken_files(self):
        cases = {
            "broken_fields.csv": "line 4: 2 fields, the header has 3",
            "broken_quote.csv": "malformed CSV",
            "latin1.csv": "not UTF-8 text",
            "empty.csv": "empty file",
            "blank_line.csv": "line 3: blank line",
        }
        for name, message in cases.items():
            with self.assertRaises(LoadError, msg=name) as ctx:
                loader.read_csv(fixture(name))
            self.assertIn(message, str(ctx.exception), name)

    def test_unsupported_file_type(self):
        with self.assertRaises(LoadError):
            loader.read_file(fixture("kinds.json"))

    def test_limits(self):
        with mock.patch.object(loader, "MAX_ROWS", 59), self.assertRaisesRegex(LoadError, "more than 59 rows"):
            loader.read_csv(fixture("late_zero.csv"))
        with mock.patch.object(loader, "MAX_FILE_BYTES", 100), self.assertRaisesRegex(LoadError, "limit"):
            loader.read_csv(fixture("late_zero.csv"))


class LoadTest(unittest.TestCase):
    """reads, replaces and SELECTs real tables in dc_hub_data (names start with test_)"""

    def setUp(self):
        self.tables = []
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())

    def tearDown(self):
        for name in self.tables:
            iris.sql.exec("DROP TABLE IF EXISTS " + loader.qualified(name))

    def run_units(self, *units, summary=None):
        """units: (label, table name, read); returns the summary dict"""
        self.tables.extend(u[1] for u in units)
        return loader.run(summary if summary is not None else {"source": "test"}, list(units))

    def load_csv(self, name, table):
        result = self.run_units((name, table, lambda: loader.read_file(fixture(name))))
        self.assertEqual(result["status"], "success", result)
        return result

    def select(self, table, columns="*", order="%ID"):
        return query("SELECT %s FROM %s ORDER BY %s" % (columns, loader.qualified(table), order))

    def write_parquet(self, table, name="data.parquet"):
        path = os.path.join(self.tmp, name)
        pyarrow.parquet.write_table(table, path)
        return path

    def test_leading_zero_kept(self):
        result = self.load_csv("late_zero.csv", "test_late_zero")
        self.assertEqual(result["tables"], [{"from": "late_zero.csv", "table": "dc_hub_data.test_late_zero",
                                             "rows": 60, "columns": 3}])
        self.assertEqual(result["rowsWritten"], 60)
        self.assertEqual(sql_columns("test_late_zero"), {"id": "bigint", "code": "varchar(5)", "score": "double"})
        self.assertEqual(self.select("test_late_zero", "id, code, score", "id DESC")[0], [60, "00123", 60.5])
        self.assertEqual(query("SELECT COUNT(*) FROM dc_hub_data.test_late_zero WHERE code = '00123'"), [[1]])

    def test_kinds_and_bounds(self):
        self.load_csv("kinds.csv", "test_kinds")
        rows = self.select("test_kinds", "big, dbl, over, plus, nan, huge, prec, c_date, empty, na")
        self.assertEqual(rows[0], [2 ** 63 - 1, 1.0, "9223372036854775808", "+5", "nan", "1e400",
                                   "0.12345678901234567", "11/11/07", None, "NA"])
        self.assertEqual(rows[1][:2], [-(2 ** 63), 2.5])
        self.assertEqual([r[1] for r in rows], [1.0, 2.5, -1e-05, 300.0])
        self.assertEqual(rows[3], [None, 300.0, None, None, None, None, None, None, None, None])

    def test_penguins_like_csv(self):
        self.load_csv("penguins_like.csv", "test_penguins")
        self.assertEqual(sql_columns("test_penguins"), {
            "studyname": "varchar(7)", "sample_number": "bigint", "individual_id": "varchar(4)",
            "date_egg": "varchar(8)", "culmen_length_mm": "double", "sex": "varchar(6)", "sex_2": "varchar(2)",
            "comments": "varchar(30)", "c_2nd_value": "varchar(1)"})
        rows = self.select("test_penguins", "sample_number, date_egg, culmen_length_mm, sex, sex_2, comments, c_2nd_value")
        self.assertEqual(rows, [
            [1, "11/11/07", 39.1, "MALE", "m", "Not enough blood, for isotopes", "x"],
            [2, "11/11/07", None, "FEMALE", "NA", None, "y"],
            [3, "11/16/07", 40.3, ".", ".", "line one\nline two", "z"],
            [4, "11/16/07", 36.7, "NA", None, 'Zürich 東京 🙂 "quoted"', None],
        ])

    def test_strings_compare_exactly(self):
        path = os.path.join(self.tmp, "ids.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("id,n\nA1,1\na1,2\na1 ,3\nA1,4\n")
        result = self.run_units(("ids.csv", "test_exact", lambda: loader.read_file(path)))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(query("SELECT id, COUNT(*) FROM dc_hub_data.test_exact GROUP BY id ORDER BY id"),
                         [["A1", 2], ["a1", 1], ["a1 ", 1]])
        self.assertEqual(query("SELECT n FROM dc_hub_data.test_exact WHERE id = ?", "a1"), [[2]])
        self.assertEqual(query("SELECT COUNT(DISTINCT id) FROM dc_hub_data.test_exact"), [[3]])

    def test_long_string(self):
        path = os.path.join(self.tmp, "long.csv")
        long_text = "abc,def " * 25000 + "🙂"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write('id,text\n1,short\n2,"%s"\n' % long_text)
        self.run_units(("long.csv", "test_long", lambda: loader.read_file(path)))
        self.assertEqual(sql_columns("test_long")["text"], "varchar(200002)")
        self.assertEqual(self.select("test_long", "text, LENGTH(text)")[1], [long_text, 200002])

    def test_parquet_simple_types(self):
        table = pyarrow.table({
            "Id": pyarrow.array(["00123", "A-7", None], pyarrow.string()),
            "n32": pyarrow.array([1, -2, None], pyarrow.int32()),
            "n64": pyarrow.array([2 ** 63 - 1, -(2 ** 63), 0], pyarrow.int64()),
            "u64": pyarrow.array([2 ** 64 - 1, 1, None], pyarrow.uint64()),
            "f64": pyarrow.array([0.1, -2.5e300, None], pyarrow.float64()),
            "f32": pyarrow.array([1.4, 5.1, 3.0], pyarrow.float32()),
            "fnan": pyarrow.array([1.5, float("nan"), float("inf")], pyarrow.float64()),
            "flag": pyarrow.array([True, False, None], pyarrow.bool_()),
            "txt": pyarrow.array(["", None, "Zürich 🙂"], pyarrow.large_string()),
            "price": pyarrow.array([decimal.Decimal("1.50"), None, decimal.Decimal("-0.01")], pyarrow.decimal128(10, 2)),
            "day": pyarrow.array([datetime.date(2024, 2, 29), None, datetime.date(1999, 1, 1)], pyarrow.date32()),
            "seen_at": pyarrow.array([datetime.datetime(2024, 1, 2, 3, 4, 5), None, None], pyarrow.timestamp("s")),
            "cat": pyarrow.array(["b", "a", "b"]).dictionary_encode(),
            "nothing": pyarrow.nulls(3),
        })
        path = self.write_parquet(table)
        result = self.run_units(("data.parquet", "test_parquet", lambda: loader.read_file(path)))
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(sql_columns("test_parquet"), {
            "id": "varchar(5)", "n32": "bigint", "n64": "bigint", "u64": "varchar(20)", "f64": "double",
            "f32": "double", "fnan": "varchar(3)", "flag": "bit", "txt": "varchar(9)", "price": "varchar(5)",
            "day": "varchar(10)", "seen_at": "varchar(23)", "cat": "varchar(1)", "nothing": "varchar(1)"})
        rows = self.select("test_parquet")
        self.assertEqual(rows, [
            ["00123", 1, 2 ** 63 - 1, "18446744073709551615", 0.1, 1.4, "1.5", 1, "", "1.50", "2024-02-29",
             "2024-01-02 03:04:05.000", "b", None],  # Parquet stores seconds as milliseconds
            ["A-7", -2, -(2 ** 63), "1", -2.5e300, 5.1, "nan", 0, None, None, None, None, "a", None],
            [None, None, 0, None, None, 3.0, "inf", None, "Zürich 🙂", "-0.01", "1999-01-01", None, "b", None],
        ])

    def test_nested_column_stops_before_write(self):
        self.load_csv("late_zero.csv", "test_nested")
        table = pyarrow.table({"id": [1, 2], "tags": pyarrow.array([["a"], ["b", "c"]])})
        path = self.write_parquet(table)
        result = self.run_units(("nested.parquet", "test_nested", lambda: loader.read_file(path)))
        self.assertEqual(result["status"], "failed")
        self.assertIn("column 'tags' has type list<", result["error"])
        self.assertEqual(result["failed"]["tableState"], "unchanged")
        self.assertEqual(result["tables"], [])
        self.assertEqual(query("SELECT COUNT(*) FROM dc_hub_data.test_nested"), [[60]])

    def test_struct_column_rejected(self):
        table = pyarrow.table({"image": pyarrow.array([{"bytes": b"x", "path": "a.png"}])})
        with self.assertRaisesRegex(LoadError, "column 'image' has type struct"):
            loader.read_arrow(table)

    def test_repeat_replace(self):
        for _ in range(2):
            self.load_csv("late_zero.csv", "test_repeat")
        self.assertEqual(query("SELECT COUNT(*) FROM dc_hub_data.test_repeat"), [[60]])

    def test_replace_changes_schema(self):
        self.load_csv("late_zero.csv", "test_schema")
        self.load_csv("kinds.csv", "test_schema")
        self.assertEqual(list(sql_columns("test_schema"))[:2], ["big", "dbl"])
        self.assertEqual(query("SELECT COUNT(*) FROM dc_hub_data.test_schema"), [[4]])

    def test_header_only_and_blank_lines(self):
        result = self.load_csv("header_only.csv", "test_header_only")
        self.assertEqual(result["rowsWritten"], 0)
        self.assertEqual(sql_columns("test_header_only"), {"a": "varchar(1)", "b": "varchar(1)", "c": "varchar(1)"})
        self.load_csv("one_column_blank.csv", "test_one_column")
        self.assertEqual(self.select("test_one_column", "value"), [["first"], [None], ["third"]])

    def test_broken_csv_is_failed_and_creates_nothing(self):
        for name in ["broken_fields.csv", "broken_quote.csv", "latin1.csv", "empty.csv", "blank_line.csv"]:
            result = self.run_units((name, "test_broken", lambda: loader.read_file(fixture(name))))
            self.assertEqual(result["status"], "failed", name)
            self.assertTrue(result["error"].startswith(name + ": "), result)
            self.assertEqual(result["failed"], {"from": name, "table": "dc_hub_data.test_broken", "tableState": "unchanged"})
            self.assertFalse(table_exists(loader.SCHEMA, "test_broken"), name)

    def test_write_failure_removes_table(self):
        self.load_csv("late_zero.csv", "test_too_big")
        big = "x" * 2_000_000
        data = loader.TableData([loader.Column("a", "a", "VARCHAR(2000000)", ["ok", big]),
                                 loader.Column("b", "b", "VARCHAR(2000000)", ["ok", big])], 2)
        result = self.run_units(("rows", "test_too_big", lambda: data))
        self.assertEqual(result["status"], "failed")
        self.assertIn("row 2 was rejected by IRIS", result["error"])
        self.assertIn("MAXSTRING", result["error"])
        self.assertEqual(result["failed"]["tableState"], "removed")
        self.assertFalse(table_exists(loader.SCHEMA, "test_too_big"))

    def test_multi_unit_stops_at_first_failure(self):
        result = self.run_units(
            ("late_zero.csv", "test_multi_a", lambda: loader.read_file(fixture("late_zero.csv"))),
            ("broken_fields.csv", "test_multi_b", lambda: loader.read_file(fixture("broken_fields.csv"))),
            ("kinds.csv", "test_multi_c", lambda: loader.read_file(fixture("kinds.csv"))),
            summary={"source": "test", "ref": "local"})
        self.assertEqual(list(result)[:3], ["source", "ref", "status"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual([t["table"] for t in result["tables"]], ["dc_hub_data.test_multi_a"])
        self.assertEqual(result["rowsWritten"], 60)
        self.assertEqual(result["error"], "broken_fields.csv: line 4: 2 fields, the header has 3")
        self.assertEqual(result["notAttempted"], ["kinds.csv"])
        self.assertFalse(table_exists(loader.SCHEMA, "test_multi_c"))

    def test_same_names_in_other_schema_untouched(self):
        iris.sql.exec('CREATE TABLE dc_hub_test_other."test_guard" ("v" VARCHAR(10))')
        try:
            iris.sql.exec('INSERT INTO dc_hub_test_other."test_guard" ("v") VALUES (?)', "keep me")
            self.load_csv("late_zero.csv", "test_guard")
            self.load_csv("late_zero.csv", "test_guard")
            self.assertEqual(query('SELECT "v" FROM dc_hub_test_other."test_guard"'), [["keep me"]])
            self.assertEqual(query("SELECT COUNT(*) FROM dc_hub_data.test_guard"), [[60]])
        finally:
            iris.sql.exec('DROP TABLE IF EXISTS dc_hub_test_other."test_guard"')

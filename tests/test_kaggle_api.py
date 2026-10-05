"""local tests of the Kaggle facade and of the file -> table layout (no network).
A downloaded dataset version is imitated by a local directory; live downloads are covered by tests/smoke.sh"""
import json
import os
import shutil
import tempfile
import unittest

import iris
import pyarrow
import pyarrow.parquet

from dc_hub import kaggle, loader
from test_loader import fixture, query, sql_columns, table_exists


def load_kaggle_sql(*args):
    """calls the real SQL function and parses its JSON summary"""
    sql = "SELECT dc_hub.load_kaggle(%s)" % ", ".join(args)
    return json.loads(query(sql)[0][0])


class FacadeTest(unittest.TestCase):
    """argument checks run before any network access, through SELECT dc_hub.load_kaggle(...)"""

    def assert_failed(self, result, error):
        self.assertEqual(result["status"], "failed", result)
        self.assertIn(error, result["error"])
        self.assertEqual((result["tables"], result["rowsWritten"]), ([], 0))

    def test_missing_handle(self):
        self.assert_failed(load_kaggle_sql(), "handle is required")
        self.assert_failed(load_kaggle_sql("NULL", "'palmer'"), "handle is required")
        self.assert_failed(load_kaggle_sql("''"), "handle is required")

    def test_handle_form(self):
        for handle in ["'https://www.kaggle.com/datasets/owner/name'", "'titanic'", "'../x/y'", "'/etc/passwd'",
                       "'owner/name/extra'", "'owner/na me'", "'owner/name/versions/0'", "'owner/name/versions/abc'",
                       "'owner/name/versions/'", "'owner/name/version/1'"]:
            self.assert_failed(load_kaggle_sql(handle), "is not a Kaggle dataset handle")

    def test_prefix_must_be_plain_name(self):
        for prefix in ["'dc_hub_data.x'", "'SQLUser.x'", "'x; DROP TABLE y'", "'1x'", "'a/b'"]:
            result = load_kaggle_sql("'owner/name/versions/1'", prefix)
            self.assert_failed(result, "is not allowed")

    def test_summary_fields(self):
        result = load_kaggle_sql("'owner/name/versions/3'", "'bad.prefix'")
        self.assertEqual(list(result)[:5], ["source", "handle", "version", "pinned", "status"])
        self.assertEqual((result["source"], result["handle"], result["pinned"]), ("kaggle", "owner/name/versions/3", True))
        self.assertFalse(load_kaggle_sql("'owner/name'", "'bad.prefix'")["pinned"])

    def test_check_handle(self):
        self.assertEqual(kaggle.check_handle("parulpandey/palmer-archipelago-antarctica-penguin-data/versions/1"),
                         ("parulpandey", "palmer-archipelago-antarctica-penguin-data", 1))
        self.assertEqual(kaggle.check_handle("uciml/iris"), ("uciml", "iris", None))


class LayoutTest(unittest.TestCase):
    """which files become tables, in which order and under which names"""

    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())

    def touch(self, *paths):
        for path in paths:
            full = os.path.join(self.root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").close()

    def test_dataset_files(self):
        self.touch("b.csv", "A.CSV", "sub/c.parquet", "readme.txt", "sub/img.png", "data.csv.gz", "db.sqlite")
        self.assertEqual(kaggle.dataset_files(self.root),
                         (["A.CSV", "b.csv", "sub/c.parquet"], ["data.csv.gz", "db.sqlite", "readme.txt", "sub/img.png"]))

    def test_table_names(self):
        paths = ["2019.csv", "Sub/Data.csv", "data.csv", "data.parquet", "sub_data.csv"]
        self.assertEqual(kaggle.table_names("kg", paths), ["kg_2019", "kg_sub_data", "kg_data", "kg_data_2", "kg_sub_data_2"])
        self.assertEqual(kaggle.table_names(loader.table_name("palmer-archipelago-antarctica-penguin-data"),
                                            ["penguins_lter.csv"]), ["palmer_archipelago_antarctica_penguin_data_penguins_lter"])
        long_names = kaggle.table_names("p" * 60, ["first.csv", "second.csv"])
        self.assertEqual(long_names, ["p" * 60 + "_fir", "p" * 60 + "_sec"])

    def test_long_lists_are_shortened(self):
        listed = loader.shortened(["f%02d.json" % i for i in range(30)], kaggle.MAX_LISTED, "files")
        self.assertEqual(len(listed), kaggle.MAX_LISTED + 1)
        self.assertEqual(listed[-1], "... 30 files in total")


class LoadFilesTest(unittest.TestCase):
    """one call -> one table per CSV / Parquet file, on a local directory shaped like a Kaggle download"""

    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())

    def tearDown(self):
        for name in ["test_kg_late_zero", "test_kg_penguins", "test_kg_sub_measures", "test_kg_a", "test_kg_b",
                     "test_kg_c", "test_kg_notes"]:
            iris.sql.exec("DROP TABLE IF EXISTS " + loader.qualified(name))

    def add(self, fixture_name, path):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        shutil.copy(fixture(fixture_name), full)

    def load(self):
        return kaggle.load_files({"source": "kaggle", "handle": "local/test"}, self.root, "test_kg")

    def test_multi_file_dataset(self):
        self.add("penguins_like.csv", "penguins.csv")
        self.add("late_zero.csv", "late_zero.csv")
        os.makedirs(os.path.join(self.root, "sub"))
        pyarrow.parquet.write_table(pyarrow.table({"id": ["007", "A1"], "value": [1.5, None]}),
                                    os.path.join(self.root, "sub", "measures.parquet"))
        with open(os.path.join(self.root, "notes.txt"), "w") as f:
            f.write("not a table")
        result = self.load()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["tables"], [
            {"from": "late_zero.csv", "table": "dc_hub_data.test_kg_late_zero", "rows": 60, "columns": 3},
            {"from": "penguins.csv", "table": "dc_hub_data.test_kg_penguins", "rows": 4, "columns": 9},
            {"from": "sub/measures.parquet", "table": "dc_hub_data.test_kg_sub_measures", "rows": 2, "columns": 2},
        ])
        self.assertEqual(result["rowsWritten"], 66)
        self.assertEqual(result["notImported"], ["notes.txt"])
        self.assertEqual(list(result), ["source", "handle", "status", "tables", "rowsWritten", "notImported"])
        self.assertFalse(table_exists(loader.SCHEMA, "test_kg_notes"))
        self.assertEqual(query("SELECT id, value FROM dc_hub_data.test_kg_sub_measures ORDER BY %ID"),
                         [["007", 1.5], ["A1", None]])
        self.assertEqual(sql_columns("test_kg_late_zero")["code"], "varchar(5)")
        # a repeated call replaces every table: same row counts, not doubled
        again = self.load()
        self.assertEqual([t["rows"] for t in again["tables"]], [60, 4, 2])
        for table, rows in [("test_kg_late_zero", 60), ("test_kg_penguins", 4), ("test_kg_sub_measures", 2)]:
            self.assertEqual(query("SELECT COUNT(*) FROM " + loader.qualified(table)), [[rows]])

    def test_failed_file_stops_the_import(self):
        self.add("late_zero.csv", "a.csv")
        self.add("broken_quote.csv", "b.csv")
        self.add("kinds.csv", "c.csv")
        result = self.load()
        self.assertEqual(result["status"], "failed")
        self.assertEqual([t["table"] for t in result["tables"]], ["dc_hub_data.test_kg_a"])
        self.assertTrue(result["error"].startswith("b.csv: "), result["error"])
        self.assertEqual(result["failed"], {"from": "b.csv", "table": "dc_hub_data.test_kg_b", "tableState": "unchanged"})
        self.assertEqual(result["notAttempted"], ["c.csv"])
        self.assertFalse(table_exists(loader.SCHEMA, "test_kg_b"))
        self.assertFalse(table_exists(loader.SCHEMA, "test_kg_c"))

    def test_no_supported_files(self):
        for name in ["data.json", "db.sqlite"]:
            open(os.path.join(self.root, name), "w").close()
        result = self.load()
        self.assertEqual(result["status"], "failed")
        self.assertEqual((result["tables"], result["rowsWritten"]), ([], 0))
        self.assertIn("no CSV or Parquet files", result["error"])
        self.assertEqual(result["notImported"], ["data.json", "db.sqlite"])

    def test_empty_download(self):
        result = self.load()
        self.assertEqual(result["status"], "failed")
        self.assertIn("files found: 0", result["error"])
        self.assertNotIn("notImported", result)

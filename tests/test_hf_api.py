"""local tests of the SQL facade and of the Hugging Face conversion (no network).
Live downloads are covered by the manual tests/smoke.sh"""
import json
import os
import unittest
from unittest import mock

import datasets
import iris

from dc_hub import api, hf, loader
from test_loader import query, sql_columns, table_exists


def load_hf_sql(*args):
    """calls the real SQL function and parses its JSON summary"""
    sql = "SELECT dc_hub.load_hf(%s)" % ", ".join(args)
    return json.loads(query(sql)[0][0])


class FacadeTest(unittest.TestCase):
    """argument checks run before any network access, through SELECT dc_hub.load_hf(...)"""

    def assert_failed(self, result, error):
        self.assertEqual(result["status"], "failed", result)
        self.assertIn(error, result["error"])
        self.assertEqual((result["tables"], result["rowsWritten"]), ([], 0))

    def test_missing_arguments(self):
        self.assert_failed(load_hf_sql(), "ref is required")
        self.assert_failed(load_hf_sql("'hitorilabs/iris'"), "split is required")
        self.assert_failed(load_hf_sql("'hitorilabs/iris'", "NULL"), "split is required")
        self.assert_failed(load_hf_sql("'hitorilabs/iris'", "''"), "split is required")

    def test_ref_must_be_hub_id(self):
        for ref in ["'/etc/passwd'", "'../x/y'", "'csv'", "'owner/name/extra'", "'owner/na me'"]:
            self.assert_failed(load_hf_sql(ref, "'train'"), "is not a Hugging Face dataset id")

    def test_target_must_be_plain_name(self):
        for target in ["'dc_hub_data.iris'", "'SQLUser.iris'", "'iris; DROP TABLE x'", "'1iris'"]:
            result = load_hf_sql("'hitorilabs/iris'", "'train'", "NULL", target)
            self.assert_failed(result, "is not allowed")

    def test_summary_fields(self):
        result = load_hf_sql("'hitorilabs/iris'", "'train'", "NULL", "'bad.name'", "'fa62476c42edcf9259f895f43da1a7bf9e2697ae'")
        self.assertEqual(list(result)[:6], ["source", "ref", "config", "split", "revision", "pinned"])
        self.assertEqual((result["source"], result["config"], result["pinned"]), ("huggingface", None, True))
        self.assertFalse(load_hf_sql("'hitorilabs/iris'", "'train'", "NULL", "'bad.name'", "'main'")["pinned"])

    def test_arguments(self):
        self.assertEqual([api.arg(v) for v in ["", "\x00", " train ", None]], ["", "", "train", ""])

    def test_secrets_are_masked(self):
        with mock.patch.dict(os.environ, HF_TOKEN="hf_TestSecretValue123"):
            text = api.to_json({"error": "bad token hf_TestSecretValue123 in url"})
        self.assertNotIn("hf_TestSecretValue123", text)
        self.assertIn("***", text)


class HfConversionTest(unittest.TestCase):
    """the materialized-split path on a local in-memory datasets.Dataset"""

    def setUp(self):
        features = datasets.Features({
            "sepal_length": datasets.Value("float32"),
            "Sample ID": datasets.Value("string"),
            "species": datasets.ClassLabel(names=["Iris-setosa", "Iris-versicolor", "Iris-virginica"]),
        })
        self.dataset = datasets.Dataset.from_dict(
            {"sepal_length": [5.1, 4.9, 6.3], "Sample ID": ["00123", "007", "A1"], "species": [0, 1, 2]}, features)

    def tearDown(self):
        iris.sql.exec("DROP TABLE IF EXISTS dc_hub_data.test_hf")

    def test_split_to_table(self):
        result = loader.run({"source": "test"}, [("default/train", "test_hf", lambda: hf.read_dataset(self.dataset))])
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(sql_columns("test_hf"), {"sepal_length": "double", "sample_id": "varchar(5)", "species": "bigint"})
        self.assertEqual(query("SELECT sepal_length, sample_id, species FROM dc_hub_data.test_hf ORDER BY %ID"),
                         [[5.1, "00123", 0], [4.9, "007", 1], [6.3, "A1", 2]])

    def test_selected_rows_and_labels(self):
        subset = self.dataset.select([2, 0])
        data = hf.read_dataset(subset)
        self.assertEqual(data.rows, 2)
        self.assertEqual(data.columns[0].values, ["6.3", "5.1"])
        self.assertEqual(hf.class_labels(subset.features, data),
                         {"species": ["Iris-setosa", "Iris-versicolor", "Iris-virginica"]})

    def test_many_labels_are_shortened(self):
        features = datasets.Features({"label": datasets.ClassLabel(names=["c%d" % i for i in range(30)])})
        dataset = datasets.Dataset.from_dict({"label": [0]}, features)
        labels = hf.class_labels(features, hf.read_dataset(dataset))["label"]
        self.assertEqual(len(labels), hf.MAX_LABELS + 1)
        self.assertEqual(labels[-1], "... 30 labels in total")

    def test_image_feature_rejected_before_download(self):
        features = datasets.Features({"image": datasets.Image(), "label": datasets.Value("int64")})
        with self.assertRaisesRegex(loader.LoadError, "column 'image' has type struct"):
            loader.check_schema(features.arrow_schema)
        self.assertFalse(table_exists(loader.SCHEMA, "test_hf"))

    def test_default_table(self):
        self.assertEqual(hf.default_table("hitorilabs/iris", "", "train"), "iris_train")
        self.assertEqual(hf.default_table("hitorilabs/iris", "default", "train"), "iris_train")
        self.assertEqual(hf.default_table("nyu-mll/glue", "cola", "validation"), "glue_cola_validation")

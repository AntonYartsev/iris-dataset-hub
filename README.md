# iris-dataset-hub

one SQL call loads a small, flat Hugging Face or Kaggle dataset into ordinary InterSystems IRIS
tables. Built for the InterSystems Community Bounty, ideas
[DPI-I-543](https://ideas.intersystems.com/ideas/DPI-I-543) "Load Datasets from Hugging Face into
IRIS" and [DPI-I-1012](https://ideas.intersystems.com/ideas/DPI-I-1012) "Load Datasets from Kaggle
into IRIS"

```sql
SELECT dc_hub.load_hf('hitorilabs/iris', 'train', NULL, 'iris', 'fa62476c42edcf9259f895f43da1a7bf9e2697ae')
SELECT dc_hub.load_kaggle('parulpandey/palmer-archipelago-antarctica-penguin-data/versions/1', 'palmer')
```

the first call creates `dc_hub_data.iris` (150 rows). The second creates one table per CSV file
of the Kaggle dataset, `dc_hub_data.palmer_penguins_lter` and `dc_hub_data.palmer_penguins_size`
(344 rows each). Both return a short JSON summary

it runs on plain IRIS Community: IRIS Embedded Python runs the Hugging Face `datasets` library,
`kagglehub` and `pyarrow` inside the IRIS process

## Installation

you need Docker with Compose v2, about 6 GB of free disk space and internet access

```bash
git clone https://github.com/AntonYartsev/iris-dataset-hub.git iris-dataset-hub
cd iris-dataset-hub
docker compose up -d --build --wait
```

when the command returns, IRIS is healthy and the SQL functions are compiled in namespace `USER`.
The container uses host ports 1972 (SQL) and 52773 (web); set `IRIS_SQL_PORT` and `IRIS_WEB_PORT`
to use others

where to run the SQL:

- IRIS SQL shell: `docker compose exec iris iris sql IRIS -U USER`, `quit` leaves it
- Management Portal: <http://localhost:52773/csp/sys/UtilHome.csp>, user `_SYSTEM`, password
  `SYS`, then System Explorer → SQL → Execute Query in namespace `USER`
- JDBC, ODBC or DB-API on `localhost:1972`, namespace `USER`, same credentials

public datasets need no credentials. For a gated Hugging Face dataset or a private Kaggle dataset,
export `HF_TOKEN`, or `KAGGLE_API_TOKEN` (or `KAGGLE_USERNAME` and `KAGGLE_KEY`), before
`docker compose up`. They are never SQL arguments, and the summaries mask their values (`***`)

`docker compose down --rmi local` removes the container, the tables in it and the built image

## Hugging Face

`dc_hub.load_hf(ref, split, config, target, revision)`; the trailing arguments can be left out

| Argument | Meaning |
| --- | --- |
| `ref` | Hugging Face dataset id, `owner/name`. |
| `split` | Required, for example `train`. One split per call. |
| `config` | Needed when the dataset has several configs (`NULL` otherwise). Without it the error lists the available configs. |
| `target` | Table name in `dc_hub_data`; default `<name>_<split>`, or `<name>_<config>_<split>` for a non-default config. |
| `revision` | A 40-character commit hash pins the data (`"pinned": true`). A branch or tag is resolved to a commit; without a revision the latest commit is used. |

```json
{"source": "huggingface", "ref": "hitorilabs/iris", "config": "default", "split": "train",
 "revision": "fa62476c42edcf9259f895f43da1a7bf9e2697ae", "pinned": true,
 "commit": "fa62476c42edcf9259f895f43da1a7bf9e2697ae", "status": "success",
 "tables": [{"from": "default/train", "table": "dc_hub_data.iris", "rows": 150, "columns": 5}],
 "rowsWritten": 150, "classLabels": {"species": ["Iris-setosa", "Iris-versicolor", "Iris-virginica"]}}
```

## Kaggle

`dc_hub.load_kaggle(handle, targetPrefix)`:

| Argument | Meaning |
| --- | --- |
| `handle` | Kaggle dataset, `owner/dataset/versions/N` (pinned) or `owner/dataset` (latest version, `"pinned": false`). |
| `targetPrefix` | Table name prefix in `dc_hub_data`; default: the dataset name. |

every `.csv` and `.parquet` file of the version replaces its own table
`dc_hub_data.<prefix>_<file path without extension>`. Files of other types are not imported and
are listed as `notImported`

## Loading rules

- a repeated call replaces the tables. Replace is the only mode, there is no append
- a failed call returns `"status": "failed"` with the reason and the tables it already touched.
  Rows and columns are never skipped silently
- CSV values are read as text and typed by the whole column, so `00123` stays `00123`, and a date
  or a mixed column stays text. An empty field is `NULL`. Parquet files and Hugging Face splits
  keep the types the data declares
- table and column names are lower-case `[a-z0-9_]` and work without quotes:
  `Culmen Length (mm)` → `culmen_length_mm`
- tables are created only in schema `dc_hub_data`. Tables in other schemas are never dropped or
  changed

## Limitations

- small, flat, tabular data only: CSV and Parquet files with scalar columns, at most 1,000,000
  rows per table and 100 MB per Hugging Face config or Kaggle version. Over a limit the call fails;
  nothing is truncated
- Hugging Face: dataset scripts, images, audio and other nested columns are refused before the
  download
- CSV must be UTF-8 and comma-separated, with a header row. Other encodings and delimiters fail
- a whole file or split is held in memory and written row by row; speed was not a goal
- the container keeps the default `_SYSTEM` / `SYS` credentials usable. Do not expose it to a
  network
- verified on IRIS Community 2026.1.0.234.1com on linux/amd64 and linux/arm64

## Tests

```bash
tests/run.sh
tests/smoke.sh
```

`tests/run.sh` runs 47 local tests inside the IRIS process, with no dataset downloads.
`tests/smoke.sh` loads real datasets from both sources and checks ten loads that must fail
cleanly, in about 30 seconds; it replaces `dc_hub_data.iris`, `palmer_penguins_lter` and
`palmer_penguins_size`. Both need the running `iris` service and exit with 1 on failure

## Prior art

[iris-kaggle-socrata-generator](https://openexchange.intersystems.com/package/iris-kaggle-socrata-generator)
(Open Exchange, 2022) installs a Kaggle dataset with the ObjectScript call `InstallDataset`.
iris-dataset-hub differs in six points. The loads are SQL functions, `dc_hub.load_hf` and
`dc_hub.load_kaggle`. Hugging Face is a second source. Parquet files and Hugging Face splits get
their SQL types from the types the source declares. A CSV column is typed by all of its values, so
one `00123` in the last row keeps the column as text. One `dc_hub.load_kaggle` call loads every
CSV and Parquet file of a multi-file Kaggle version, one table per file. A bad argument, a
Hugging Face image or nested column, or a dataset over the 100 MB limit stops the call with a
`"status": "failed"` JSON summary before the download starts

## Dataset licensing

the MIT license of this repository covers its code only. Every dataset keeps its own license and
terms: check them before you load or share data. The datasets used here and in the tests are
CC0 (public domain). No dataset is bundled with the repository

## License

[MIT](LICENSE)

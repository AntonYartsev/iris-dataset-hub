#!/usr/bin/env bash
# manual network smoke test against the real sources, through the IRIS SQL shell:
# the README one-line imports, SELECTs, repeats (replace), and loads that must fail cleanly.
# Needs the running compose service and internet access. Replaces dc_hub_data.iris,
# dc_hub_data.palmer_penguins_lter and dc_hub_data.palmer_penguins_size; creates and drops
# the dc_hub_data.smoke_* tables of the other datasets.
# Usage: tests/smoke.sh [extra docker compose args]
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(docker compose "$@" exec -T iris iris session IRIS -U USER <<'OS'
 do $SYSTEM.SQL.Shell()
SELECT dc_hub.load_hf('hitorilabs/iris', 'train', NULL, 'iris', 'fa62476c42edcf9259f895f43da1a7bf9e2697ae')
SELECT COUNT(*) AS "rows" FROM dc_hub_data.iris
SELECT TOP 3 * FROM dc_hub_data.iris
SELECT species, COUNT(*) AS n, ROUND(AVG(petal_length), 3) AS avg_petal_length FROM dc_hub_data.iris GROUP BY species ORDER BY species
SELECT dc_hub.load_hf('hitorilabs/iris', 'train', NULL, 'iris', 'fa62476c42edcf9259f895f43da1a7bf9e2697ae')
SELECT COUNT(*) AS "rows after repeat" FROM dc_hub_data.iris
SELECT dc_hub.load_hf('nyu-mll/glue', 'train')
SELECT dc_hub.load_hf('bigcode/the-stack-v2', 'train')
SELECT dc_hub.load_hf('bigbio/biosses', 'train')
SELECT dc_hub.load_hf('ylecun/mnist', 'train')
SELECT dc_hub.load_hf('hitorilabs/iris', 'test', NULL, 'iris')
SELECT dc_hub.load_hf('hitorilabs/iris', 'train', NULL, 'iris', 'no-such-branch')
SELECT COUNT(*) AS "rows after failed loads" FROM dc_hub_data.iris
SELECT dc_hub.load_kaggle('parulpandey/palmer-archipelago-antarctica-penguin-data/versions/1', 'palmer')
SELECT COUNT(*) AS "lter rows" FROM dc_hub_data.palmer_penguins_lter
SELECT COUNT(*) AS "size rows" FROM dc_hub_data.palmer_penguins_size
SELECT TOP 3 * FROM dc_hub_data.palmer_penguins_size
SELECT TOP 3 studyname, sample_number, individual_id, date_egg, ROUND(culmen_length_mm, 1) AS culmen_length_mm, body_mass_g, sex FROM dc_hub_data.palmer_penguins_lter
SELECT species, COUNT(*) AS n, ROUND(AVG(body_mass_g), 1) AS avg_body_mass_g FROM dc_hub_data.palmer_penguins_lter GROUP BY species ORDER BY species
SELECT dc_hub.load_kaggle('parulpandey/palmer-archipelago-antarctica-penguin-data/versions/1', 'palmer')
SELECT COUNT(*) AS "lter rows after repeat" FROM dc_hub_data.palmer_penguins_lter
SELECT COUNT(*) AS "size rows after repeat" FROM dc_hub_data.palmer_penguins_size
SELECT dc_hub.load_kaggle('uciml/iris/versions/1', 'smoke_uciml')
SELECT COUNT(*) AS "uciml rows" FROM dc_hub_data.smoke_uciml_iris
DROP TABLE dc_hub_data.smoke_uciml_iris
SELECT dc_hub.load_kaggle('nandaprasetia/iris-dataset-various-format-types/versions/1', 'smoke_formats')
SELECT TOP 3 * FROM dc_hub_data.smoke_formats_parquet_data_iris
SELECT COUNT(*) AS "csv rows", (SELECT COUNT(*) FROM dc_hub_data.smoke_formats_parquet_data_iris) AS "parquet rows" FROM dc_hub_data.smoke_formats_csv_data_iris
DROP TABLE dc_hub_data.smoke_formats_csv_data_iris
DROP TABLE dc_hub_data.smoke_formats_parquet_data_iris
SELECT dc_hub.load_kaggle('rio2016/olympic-games/versions/2', 'smoke_rio')
SELECT COUNT(*) AS "athletes rows", (SELECT COUNT(*) FROM dc_hub_data.smoke_rio_countries) AS "countries rows" FROM dc_hub_data.smoke_rio_athletes
DROP TABLE dc_hub_data.smoke_rio_athletes
DROP TABLE dc_hub_data.smoke_rio_countries
SELECT dc_hub.load_kaggle('parulpandey/no-such-dataset-xyz')
SELECT dc_hub.load_kaggle('uciml/iris/versions/99')
SELECT dc_hub.load_kaggle('kaggle/meta-kaggle')
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dc_hub_data' ORDER BY TABLE_NAME
quit
 halt
OS
)
echo "$out"
ok=$(grep -c '"status": "success"' <<<"$out" || true)
failed=$(grep -c '"status": "failed"' <<<"$out" || true)
echo "successful loads: $ok of 6, failed loads: $failed of 10 expected"
[ "$ok" -eq 6 ] && [ "$failed" -eq 10 ]

#!/usr/bin/env bash
# local tests, no dataset downloads. Needs the running compose service (docker compose up -d).
# Copies the working tree's Python package (same path as the image) and the facade classes
# into the container, then runs tests/test_*.py inside the IRIS process, where iris.sql is available.
# Test tables are dc_hub_data.test_* and are dropped by the tests.
# Usage: tests/run.sh [extra docker compose args, e.g. -f docker-compose.yml -f override.yml]
set -euo pipefail
cd "$(dirname "$0")/.."
dc=(docker compose "$@")
"${dc[@]}" exec -T -u root iris rm -rf /tmp/hub-tests /tmp/hub-tests-cls /usr/irissys/mgr/python/dc_hub
"${dc[@]}" cp src/python/dc_hub iris:/usr/irissys/mgr/python/dc_hub
"${dc[@]}" cp src/cls iris:/tmp/hub-tests-cls
"${dc[@]}" cp tests iris:/tmp/hub-tests
"${dc[@]}" exec -T iris iris session IRIS -U USER <<'OS'
 set sc = $SYSTEM.OBJ.LoadDir("/tmp/hub-tests-cls", "ck-d", , 1) if 'sc { write !,"CLASS LOAD FAILED",! do $SYSTEM.Process.Terminate(, 1) }
 set sys = ##class(%SYS.Python).Import("sys") do sys.path.insert(0, "/tmp/hub-tests")
 set ok = ##class(%SYS.Python).Import("run_tests").main("/tmp/hub-tests")
 if 'ok { write !,"TESTS FAILED",! do $SYSTEM.Process.Terminate(, 1) }
 write !,"TESTS PASSED",!
 halt
OS

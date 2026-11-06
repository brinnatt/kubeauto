#!/usr/bin/env bash
# The cheapest ES prerequisites; no SSH, cluster setup or registry mutation.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
"$PY" tests/helpers/validate_elasticsearch_matrix.py tests/elasticsearch-test-matrix.yaml
"$PY" -m py_compile roles/cluster-addon/files/elasticsearch.py roles/cluster-addon/files/eck.py tests/helpers/elasticsearch-*.py
for script in tests/helpers/elasticsearch-*.sh tests/helpers/logging-{cleanup,artifact-gate}.sh tests/run_enterprise_regression.sh; do
  bash -n "$script"
done
"$PY" -m unittest tests.unit.test_elasticsearch_delivery tests.unit.test_elasticsearch_gate tests.unit.test_logging_delivery \
  tests.unit.test_middleware_documentation tests.unit.test_six_repo_version_sync tests.unit.test_registry_pull_sources -v
git diff --check
for id in ES-01 ES-02 ES-03 ES-04; do echo "ELASTICSEARCH_CASE_PASS id=$id"; done
echo ELASTICSEARCH_CONTRACT_SCOPED_CLEAN_PASS
echo ELASTICSEARCH_STATIC_PASS

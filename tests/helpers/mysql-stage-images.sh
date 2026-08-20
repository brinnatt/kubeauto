#!/usr/bin/env bash
# Pull temporary runtime sources on a better-connected staging host.
set -Eeuo pipefail

SOURCE_PREFIX="${MYSQL_IMAGE_SOURCE_PREFIX:-docker.io}"
images=(
  percona/percona-xtradb-cluster-operator:1.20.0
  percona/percona-xtradb-cluster:8.4.8-8.1
  percona/percona-xtrabackup:8.4.0-5.1
  percona/haproxy:2.8.18-1
  percona/fluentbit:5.0.6-1
)

for image in "${images[@]}"; do
  echo "[WAIT] staging target=${image##*/}"
  timeout --signal=TERM --kill-after=15s 20m docker pull "${SOURCE_PREFIX}/${image}"
  digest="$(docker image inspect "${SOURCE_PREFIX}/${image}" --format '{{join .RepoDigests " "}}' | tr ' ' '\n' | sed -n 's/.*@//p' | head -n 1)"
  [[ "$digest" == sha256:* ]] || {
    echo "[FAIL] staged image repository digest missing: ${image##*/}" >&2
    exit 1
  }
  echo "STAGED_IMAGE_READY target=${image##*/} digest_present=true"
done

echo MYSQL_STAGE_IMAGES_PASS

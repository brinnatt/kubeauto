#!/usr/bin/env bash
# Stage the fixed StarRocks archive atomically for the standalone StarCli gate.
set -Eeuo pipefail

URL=https://releases.starrocks.io/starrocks/StarRocks-3.5.12-centos-amd64.tar.gz
SHA256=ec385951242bb3943141633bd73395a6668d23d6c64373bd546d9a2950fd76f9
DEST=/root/StarRocks-3.5.12-centos-amd64.tar.gz
PARTIAL="${DEST}.partial"

cleanup_partial() {
  rm -f "$PARTIAL"
}
trap cleanup_partial EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -s "$DEST" ]] && printf '%s  %s\n' "$SHA256" "$DEST" | sha256sum -c - >/dev/null; then
  echo "STARROCKS_FIXTURE_PASS source=reused sha256=$SHA256 path=$DEST"
  exit 0
fi

rm -f "$DEST" "$PARTIAL"
curl --fail --location --silent --show-error --retry 4 --retry-delay 5 --connect-timeout 15 \
  --output "$PARTIAL" "$URL"
printf '%s  %s\n' "$SHA256" "$PARTIAL" | sha256sum -c -
mv -f "$PARTIAL" "$DEST"
chmod 0600 "$DEST"
echo "STARROCKS_FIXTURE_PASS source=official sha256=$SHA256 path=$DEST"

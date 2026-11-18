#!/bin/bash
set -euo pipefail

: "${RUSTFS_BUCKET:?Missing bucket name}"
# Do not create host filesystem directories to emulate S3 buckets.
# AWS CLI uses the S3 API and reports permission/credential errors directly.
aws configure set default.s3.addressing_style path
if aws --endpoint-url http://rustfs:9000 s3api head-bucket --bucket "$RUSTFS_BUCKET"; then
    printf 'RustFS bucket already exists: %s\n' "$RUSTFS_BUCKET"
else
    aws --endpoint-url http://rustfs:9000 s3 mb "s3://$RUSTFS_BUCKET"
    aws --endpoint-url http://rustfs:9000 s3api head-bucket --bucket "$RUSTFS_BUCKET"
fi

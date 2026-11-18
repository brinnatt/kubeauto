#!/bin/bash
set -euo pipefail

: "${RUSTFS_BUCKET:?Missing bucket name}"
: "${RUSTFS_REGION:?Missing region}"
: "${RUSTFS_ACCESS_KEY:?Missing access key}"
: "${RUSTFS_SECRET_KEY:?Missing secret key}"

# Escape SQL string literals without evaluating shell code from credentials.
sql_literal() {
    local value=$1
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        printf 'Storage credentials and names must be single-line values.\n' >&2
        return 1
    fi
    value=${value//\\/\\\\}
    value=${value//\'/\'\'}
    printf "'%s'" "$value"
}

location=$(sql_literal "s3://$RUSTFS_BUCKET")
region=$(sql_literal "$RUSTFS_REGION")
access_key=$(sql_literal "$RUSTFS_ACCESS_KEY")
secret_key=$(sql_literal "$RUSTFS_SECRET_KEY")

# IF NOT EXISTS preserves persisted metadata on restart. Changes to bucket,
# endpoint or credentials require an explicit ALTER STORAGE VOLUME operation.
# Credentials go through stdin; never echo the SQL or put it on the command line.
mysql --protocol=TCP --connect-timeout=5 -h starrocks-fe -P 9030 -u root <<SQL
CREATE STORAGE VOLUME IF NOT EXISTS rustfs_volume
TYPE = S3
LOCATIONS = ($location)
PROPERTIES (
    'enabled' = 'true',
    'aws.s3.endpoint' = 'http://rustfs:9000',
    'aws.s3.region' = $region,
    'aws.s3.access_key' = $access_key,
    'aws.s3.secret_key' = $secret_key,
    'aws.s3.enable_path_style_access' = 'true',
    'aws.s3.enable_ssl' = 'false',
    'aws.s3.use_instance_profile' = 'false',
    'aws.s3.use_aws_sdk_default_behavior' = 'false'
);
SET rustfs_volume AS DEFAULT STORAGE VOLUME;
SQL
printf 'RustFS storage volume is configured as the default.\n'

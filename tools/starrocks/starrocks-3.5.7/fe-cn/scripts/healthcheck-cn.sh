#!/bin/bash
set -euo pipefail

# SHOW COMPUTE NODES may expose persisted Alive=true while StarMgr still has
# no schedulable worker. This read-only system-table scan uses the shared-data
# worker provider and executes on the CN; SELECT 1 would only prove FE access.
curl -fsS --max-time 3 http://127.0.0.1:8040/api/health >/dev/null
node_id=$(mysql --protocol=TCP --connect-timeout=3 -h starrocks-fe -P 9030 \
    -u root --batch --skip-column-names \
    --execute='SELECT BE_ID FROM information_schema.be_configs LIMIT 1')
[[ "$node_id" =~ ^[0-9]+$ ]]

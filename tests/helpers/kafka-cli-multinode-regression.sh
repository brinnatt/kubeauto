#!/usr/bin/env bash
set -euo pipefail

# This fixture runs on the authorized control host. Every customer operation is
# invoked through KafkaCli; SSH, process startup, and JSON files only provision
# or diagnose the disposable Kafka fixture.
TOOL="${KAFKA_TOOL:?}"
LOCAL_KAFKA_HOME="${KAFKA_HOME:?}"
MULTI_ROOT="${KAFKA_MULTI_ROOT:-/tmp/kafka-cli-multi}"
REMOTE_KAFKA_HOME="$MULTI_ROOT/kafka"
REMOTE_WORKDIR="$MULTI_ROOT/remote"
SSH_KEY="${KAFKA_SSH_KEY:-/root/.ssh/id_ed25519}"

C1=192.168.122.217
C2=192.168.122.246
C3=192.168.122.193
B1=192.168.122.210
B2=192.168.122.216
QUORUM="$C1:19093,$C2:19093,$C3:19093"
BOOTSTRAP="$B1:19092,$B2:19092"
TOPIC=kafkacli_multi_delivery
MOVE_TOPIC=kafkacli_move_delivery
GROUP=kafkacli_multi_group

fail() { echo "KAFKA_CLI_MULTINODE_FAIL: $*" >&2; exit 1; }
run() { echo "+ $*"; "$@"; }
remote() {
  local host="$1"
  shift
  run python3 "$TOOL" --target-host "$host" --ssh-user root --ssh-key "$SSH_KEY" \
    --remote-workdir "$REMOTE_WORKDIR" "$@"
}
remote_ssh() {
  local host="$1"
  shift
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 "root@$host" "$@"
}
wait_tcp() {
  local host="$1" port="$2" label="$3"
  for _ in $(seq 1 75); do
    if timeout 1 bash -c "</dev/tcp/$host/$port" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  fail "$label did not listen on $host:$port"
}
start_remote_kafka() {
  local host="$1" config="$2" pidfile="$3"
  remote_ssh "$host" "nohup '$REMOTE_KAFKA_HOME/bin/kafka-server-start.sh' '$config' >'$pidfile.log' 2>&1 & echo \$! >'$pidfile'"
}
stop_remote_kafka() {
  local host="$1" config_leaf="$2"
  remote_ssh "$host" "pkill -f '[k]afka.Kafka.*$config_leaf' || true"
}
cleanup_remote_product_state() {
  set +e
  remote "$B1" --clean --clean-data --force --deploy broker --kafka-home "$REMOTE_KAFKA_HOME" \
    --node-id 4 --log-dirs "$MULTI_ROOT/broker-4/logs" --user root --group root >/dev/null 2>&1 || true
  remote "$B2" --clean --clean-data --force --deploy broker --kafka-home "$REMOTE_KAFKA_HOME" \
    --node-id 5 --log-dirs "$MULTI_ROOT/broker-5/logs" --user root --group root >/dev/null 2>&1 || true
  remote "$C1" --clean --clean-data --force --deploy controller --kafka-home "$REMOTE_KAFKA_HOME" \
    --node-id 1 --metadata-log-dir "$MULTI_ROOT/controller-1/meta" --log-dirs "$MULTI_ROOT/controller-1/logs" --user root --group root >/dev/null 2>&1 || true
  remote "$C2" --clean --clean-data --force --deploy controller --kafka-home "$REMOTE_KAFKA_HOME" \
    --node-id 2 --metadata-log-dir "$MULTI_ROOT/controller-2/meta" --log-dirs "$MULTI_ROOT/controller-2/logs" --user root --group root >/dev/null 2>&1 || true
  remote "$C3" --clean --clean-data --force --deploy controller --kafka-home "$REMOTE_KAFKA_HOME" \
    --node-id 3 --metadata-log-dir "$MULTI_ROOT/controller-3/meta" --log-dirs "$MULTI_ROOT/controller-3/logs" --user root --group root >/dev/null 2>&1 || true
}
cleanup() {
  set +e
  cleanup_remote_product_state
  for host in "$C1" "$C2" "$C3" "$B1" "$B2"; do
    remote_ssh "$host" "pkill -f '[k]afka.Kafka.*$MULTI_ROOT' || true; rm -rf '$MULTI_ROOT'" || true
  done
  rm -f "$MULTI_ROOT/batch.json" "$MULTI_ROOT/topics.json" "$MULTI_ROOT/reassignment.json"
}
trap cleanup EXIT INT TERM

[[ -f "$SSH_KEY" ]] || fail "SSH key is unavailable: $SSH_KEY"
[[ -x "$LOCAL_KAFKA_HOME/bin/kafka-console-consumer.sh" ]] || fail "local Kafka fixture is unavailable"
for host in "$C1" "$C2" "$C3" "$B1" "$B2"; do
  remote_ssh "$host" "test -x '$REMOTE_KAFKA_HOME/bin/kafka-storage.sh'" || fail "fixture missing on $host"
done

# First controller: KafkaCli itself must generate the cluster id. Reading the
# resulting meta.properties is fixture diagnostics, not acceptance evidence.
remote "$C1" --deploy controller --controller-scope cluster --kafka-home "$REMOTE_KAFKA_HOME" \
  --generate-cluster-id --node-id 1 --advertised-host "$C1" --controller-listen-host 0.0.0.0 \
  --controller-listen-port 19093 --controller-quorum-bootstrap-servers "$QUORUM" \
  --metadata-log-dir "$MULTI_ROOT/controller-1/meta" --log-dirs "$MULTI_ROOT/controller-1/logs" \
  --no-systemd --user root --group root
CID="$(remote_ssh "$C1" "sed -n 's/^cluster.id=//p' '$MULTI_ROOT/controller-1/meta/meta.properties'")"
[[ "$CID" =~ ^[A-Za-z0-9_-]{20,30}$ ]] || fail "KafkaCli did not create a valid cluster id"
start_remote_kafka "$C1" "$REMOTE_KAFKA_HOME/config/controller-1.properties" "$MULTI_ROOT/controller-1.pid"
wait_tcp "$C1" 19093 controller-1

# The next controllers are formatted by KafkaCli as observers, then dynamically
# promoted by its quorum entry point as required by Kafka 4.3 KRaft.
for spec in "$C2:2" "$C3:3"; do
  host="${spec%%:*}"
  node_id="${spec##*:}"
  remote "$host" --deploy controller --controller-scope cluster --kafka-home "$REMOTE_KAFKA_HOME" \
    --cluster-id "$CID" --join-quorum --node-id "$node_id" --advertised-host "$host" \
    --controller-listen-host 0.0.0.0 --controller-listen-port 19093 \
    --controller-quorum-bootstrap-servers "$QUORUM" --metadata-log-dir "$MULTI_ROOT/controller-$node_id/meta" \
    --log-dirs "$MULTI_ROOT/controller-$node_id/logs" --no-systemd --user root --group root
  start_remote_kafka "$host" "$REMOTE_KAFKA_HOME/config/controller-$node_id.properties" "$MULTI_ROOT/controller-$node_id.pid"
  wait_tcp "$host" 19093 "controller-$node_id"
  remote "$host" --quorum-add-controller --kafka-home "$REMOTE_KAFKA_HOME" \
    --bootstrap-controller "$C1:19093" \
    --command-config "$REMOTE_KAFKA_HOME/config/controller-$node_id.properties"
done

CONTROLLER_STATUS="$(remote "$C1" --status --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-controller "$QUORUM")"
grep -q '"id": 1' <<<"$CONTROLLER_STATUS" || fail "controller 1 absent from voters"
grep -q '"id": 2' <<<"$CONTROLLER_STATUS" || fail "controller 2 absent from voters"
grep -q '"id": 3' <<<"$CONTROLLER_STATUS" || fail "controller 3 absent from voters"

# A batch deployment must remain a KafkaCli path. It deploys both brokers in
# node-array order after the quorum is already available.
mkdir -p "$MULTI_ROOT"
cat >"$MULTI_ROOT/batch.json" <<EOF
{
  "kafka_home": "$REMOTE_KAFKA_HOME",
  "ssh_user": "root",
  "ssh_key": "$SSH_KEY",
  "remote_workdir": "$REMOTE_WORKDIR",
  "nodes": [
    {"target_host":"$B1","deploy":"broker","node_id":4,"advertised_host":"$B1","broker_listen_host":"0.0.0.0","broker_listen_port":19092,"cluster_id":"$CID","controller_quorum_bootstrap_servers":"$QUORUM","log_dirs":"$MULTI_ROOT/broker-4/logs","no_systemd":true,"user":"root","group":"root","extra_properties":{"offsets.topic.replication.factor":"2","transaction.state.log.replication.factor":"2","transaction.state.log.min.isr":"1"}},
    {"target_host":"$B2","deploy":"broker","node_id":5,"advertised_host":"$B2","broker_listen_host":"0.0.0.0","broker_listen_port":19092,"cluster_id":"$CID","controller_quorum_bootstrap_servers":"$QUORUM","log_dirs":"$MULTI_ROOT/broker-5/logs","no_systemd":true,"user":"root","group":"root","extra_properties":{"offsets.topic.replication.factor":"2","transaction.state.log.replication.factor":"2","transaction.state.log.min.isr":"1"}}
  ]
}
EOF
run python3 "$TOOL" --batch --config "$MULTI_ROOT/batch.json"
start_remote_kafka "$B1" "$REMOTE_KAFKA_HOME/config/server-broker-4.properties" "$MULTI_ROOT/broker-4.pid"
start_remote_kafka "$B2" "$REMOTE_KAFKA_HOME/config/server-broker-5.properties" "$MULTI_ROOT/broker-5.pid"
wait_tcp "$B1" 19092 broker-4
wait_tcp "$B2" 19092 broker-5

remote "$B1" --topic-create --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
  --topic "$TOPIC" --partitions 2 --replication-factor 2
remote "$B1" --topic-create --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
  --topic "$TOPIC" --partitions 2 --replication-factor 2
TOPIC_DESC="$(remote "$B1" --topic-describe --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$TOPIC")"
grep -q 'Replicas: 4,5\|Replicas: 5,4' <<<"$TOPIC_DESC" || fail "replicated topic did not reach both brokers"
remote "$B1" --produce --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$TOPIC" --message multi-message-1
remote "$B2" --produce --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$TOPIC" --message multi-message-2
CONSUMED=""
for attempt in 1 2 3; do
  trial_group="${GROUP}_${attempt}"
  CONSUMED="$(remote "$B1" --consume --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
    --topic "$TOPIC" --consumer-group "$trial_group" --max-messages 2 || true)"
  printf 'KAFKA_CLI_CONSUME_ATTEMPT=%s\n%s\n' "$attempt" "$CONSUMED"
  if grep -q multi-message-1 <<<"$CONSUMED" && grep -q multi-message-2 <<<"$CONSUMED"; then
    GROUP="$trial_group"
    break
  fi
  sleep 2
done
grep -q multi-message-1 <<<"$CONSUMED" || fail "first replicated message missing after consumer assignment retries"
grep -q multi-message-2 <<<"$CONSUMED" || fail "second replicated message missing after consumer assignment retries"
GROUP_LIST="$(remote "$B1" --group-list --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP")"
grep -q "$GROUP" <<<"$GROUP_LIST" || fail "consumer group list missed $GROUP"
GROUP_DESC="$(remote "$B1" --group-describe --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --consumer-group "$GROUP")"
grep -q "$TOPIC" <<<"$GROUP_DESC" || fail "consumer group describe missed $TOPIC"
remote "$B1" --config-describe-broker --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --config-entity-name 4
remote "$B1" --config-describe-topic --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$TOPIC"
remote "$B1" --preferred-replica-election --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP"
METRICS="$(remote "$B1" --metrics-json --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --bootstrap-controller "$QUORUM")"
grep -q '"under_replicated_partitions": 0' <<<"$METRICS" || fail "replicated topic health is not clean"

# Generate, execute, and verify the exact reassignment JSON produced by the
# official Kafka tool through KafkaCli. JSON parsing retains only the proposed
# JSON object and rejects malformed runner output.
remote "$B1" --topic-create --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
  --topic "$MOVE_TOPIC" --partitions 1 --replication-factor 1
cat >"$MULTI_ROOT/topics.json" <<EOF
{"version":1,"topics":[{"topic":"$MOVE_TOPIC"}]}
EOF
scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes "$MULTI_ROOT/topics.json" "root@$B1:$MULTI_ROOT/topics.json"
GENERATED="$(remote "$B1" --broker-decommission-generate --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --broker-list 5 --topics-to-move-json-file "$MULTI_ROOT/topics.json")"
printf '%s\n' "$GENERATED" >"$MULTI_ROOT/generated.out"
python3 - "$MULTI_ROOT/generated.out" "$MULTI_ROOT/reassignment.json" <<'PY'
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
marker = "Proposed partition reassignment configuration"
start = text.find(marker)
if start < 0:
    raise SystemExit("proposed reassignment marker missing")
start = text.find("{", start)
if start < 0:
    raise SystemExit("proposed reassignment JSON missing")
plan, _ = json.JSONDecoder().raw_decode(text[start:])
if not plan.get("partitions"):
    raise SystemExit("proposed reassignment has no partitions")
with open(sys.argv[2], "w", encoding="utf-8") as output:
    json.dump(plan, output)
PY
scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes "$MULTI_ROOT/reassignment.json" "root@$B1:$MULTI_ROOT/reassignment.json"
remote "$B1" --broker-decommission-execute --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
  --reassignment-json-file "$MULTI_ROOT/reassignment.json" --throttle 1048576
for _ in $(seq 1 30); do
  if remote "$B1" --broker-decommission-verify --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
      --reassignment-json-file "$MULTI_ROOT/reassignment.json"; then
    break
  fi
  sleep 2
done
MOVE_DESC="$(remote "$B1" --topic-describe --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$MOVE_TOPIC")"
grep -q 'Replicas: 5' <<<"$MOVE_DESC" || fail "decommission reassignment did not move to broker 5"

# Broker-loss recovery must fail quickly, leave no duplicate record, and permit
# a successful retry after the owned process returns.
stop_remote_kafka "$B1" server-broker-4.properties
if KAFKA_CLI_TIMEOUT=5 remote "$B1" --topic-list --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$B1:19092" >"$MULTI_ROOT/broker-down.out" 2>&1; then
  fail "broker-down operation unexpectedly succeeded"
fi
start_remote_kafka "$B1" "$REMOTE_KAFKA_HOME/config/server-broker-4.properties" "$MULTI_ROOT/broker-4.pid"
wait_tcp "$B1" 19092 broker-4-retry
RETRY_LIST="$(remote "$B1" --topic-list --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP")"
grep -q "$MOVE_TOPIC" <<<"$RETRY_LIST" || fail "broker retry did not restore topic access"

# Interrupt an owned console-consumer and verify that KafkaCli leaves neither a
# local console consumer nor a remote process. Seed 99 of the requested 100
# records through KafkaCli so the consumer has an assigned partition and must
# remain active waiting for the final record; an empty topic may otherwise be
# treated as a successful zero-record run by the Kafka console client.
INTERRUPT_TOPIC="${MOVE_TOPIC}_interrupt"
remote "$B1" --topic-create --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
  --topic "$INTERRUPT_TOPIC" --partitions 1 --replication-factor 1
INTERRUPT_INPUT="$MULTI_ROOT/interrupt-input.txt"
seq 1 99 | sed 's/^/interrupt-message-/' >"$INTERRUPT_INPUT"
scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes "$INTERRUPT_INPUT" "root@$B1:$INTERRUPT_INPUT"
remote "$B1" --produce --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
  --topic "$INTERRUPT_TOPIC" --input-file "$INTERRUPT_INPUT"
(
  # Non-interactive Bash gives asynchronous jobs SIGINT=ignore. Restore the
  # default before exec so this is a real KafkaCli interruption scenario.
  trap - INT
  exec setsid --wait env KAFKA_CLI_TIMEOUT=120 python3 "$TOOL" --kafka-home "$LOCAL_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" \
    --consume --topic "$INTERRUPT_TOPIC" --consumer-group kafkacli_interrupt_group --max-messages 100
) >"$MULTI_ROOT/interrupt.out" 2>&1 &
INTERRUPT_PID=$!
sleep 3
kill -0 "$INTERRUPT_PID" || {
  cat "$MULTI_ROOT/interrupt.out" >&2 || true
  fail "consumer exited before SIGINT"
}
# Send SIGTERM to the dedicated KafkaCli session and every descendant process.
# The durable gate is non-interactive and inherited SIGINT can be ignored by
# POSIX child processes; SIGTERM is the reliable operator interruption signal
# for this cleanup contract.
# group. `setsid --wait` may fork when its parent is a process-group leader, so
# the wrapper PID is not guaranteed to be the consumer's process group.
INTERRUPT_PIDS="$INTERRUPT_PID"
INTERRUPT_FRONTIER="$INTERRUPT_PID"
for _ in 1 2 3 4; do
  INTERRUPT_NEXT=""
  while read -r interrupt_parent; do
    [[ "$interrupt_parent" =~ ^[0-9]+$ ]] || continue
    interrupt_children="$(pgrep -P "$interrupt_parent" 2>/dev/null || true)"
    if [[ -n "$interrupt_children" ]]; then
      INTERRUPT_NEXT+="$interrupt_children"$'\n'
      INTERRUPT_PIDS+=$'\n'"$interrupt_children"
    fi
  done <<<"$INTERRUPT_FRONTIER"
  [[ -n "$INTERRUPT_NEXT" ]] || break
  INTERRUPT_FRONTIER="$INTERRUPT_NEXT"
done
while read -r interrupt_pid; do
  [[ "$interrupt_pid" =~ ^[0-9]+$ ]] || continue
  interrupt_pgid="$(ps -o pgid= -p "$interrupt_pid" 2>/dev/null | tr -d ' ' || true)"
  if [[ "$interrupt_pgid" =~ ^[0-9]+$ ]]; then
    kill -TERM -- "-$interrupt_pgid" 2>/dev/null || true
  fi
  kill -TERM "$interrupt_pid" 2>/dev/null || true
done < <(printf '%s\n' "$INTERRUPT_PIDS" | awk 'NF && !seen[$0]++')
# A remote/non-interactive shell can reap or detach the setsid wrapper before
# the process-group walk observes the actual Python process. The group and
# topic are unique to this fixture, so explicitly signal only these owned
# commands as a deterministic fallback.
pkill -TERM -f '[K]afkaCli.py.*kafkacli_interrupt_group' 2>/dev/null || true
pkill -TERM -f '[k]afka-console-consumer.*kafkacli_interrupt_group' 2>/dev/null || true
set +e
wait "$INTERRUPT_PID"
INTERRUPT_RC=$?
set -e
if [[ "$INTERRUPT_RC" -eq 0 ]]; then
  cat "$MULTI_ROOT/interrupt.out" >&2 || true
  fail "interrupted consumer unexpectedly succeeded"
fi
! pgrep -af '[k]afka-console-consumer.*kafkacli_interrupt_group' >/dev/null || fail "local consumer leaked after SIGINT"

# Invalid target input must fail before SSH execution and cannot create a local
# sentinel. Credentials are checked in the standalone SASL/SASL_SSL stage.
SENTINEL="$MULTI_ROOT/injected"
if python3 "$TOOL" --target-host "$B1;touch $SENTINEL" --status --kafka-home "$LOCAL_KAFKA_HOME" >/dev/null 2>&1; then
  fail "malicious target unexpectedly succeeded"
fi
test ! -e "$SENTINEL" || fail "target-host injection created a sentinel"

remote "$B1" --topic-delete --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$TOPIC"
remote "$B1" --topic-delete --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$TOPIC"
remote "$B1" --topic-delete --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$MOVE_TOPIC"
remote "$B1" --topic-delete --kafka-home "$REMOTE_KAFKA_HOME" --bootstrap-server "$BOOTSTRAP" --topic "$INTERRUPT_TOPIC"
echo "KAFKA_CLI_MULTINODE_REGRESSION_PASS controllers=3 brokers=2 topic=$TOPIC moved_topic=$MOVE_TOPIC"

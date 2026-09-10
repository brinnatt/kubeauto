#!/usr/bin/env bash
set -euo pipefail

TOOL="${KAFKA_TOOL:?}"; HOME_K="${KAFKA_HOME:?}"; ROOT=/tmp/kafka-cli-live
DATA="$ROOT/data"; BOOT=127.0.0.1:9092; TOPIC=kafkacli_delivery_topic; GROUP=kafkacli_delivery_group
PIDFILE="$ROOT/server.pid"
fail(){ echo "KAFKA_CLI_LIVE_FAIL: $*" >&2; exit 1; }
run(){ echo "+ $*"; "$@"; }
run_sasl(){
  echo "+ KafkaCli SASL command (credentials injected through environment)"
  env KAFKA_SASL_USERNAME=kafkacli_user KAFKA_SASL_PASSWORD=kafkacli_secret "$@"
}
stop_broker(){
  [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  pkill -f "$HOME_K/bin/kafka.Kafka" 2>/dev/null || true
  rm -f "$PIDFILE"
}
clean_standalone(){
  stop_broker
  python3 "$TOOL" --kafka-home "$HOME_K" --deploy standalone --clean --clean-data --force --log-dirs "$DATA/logs" --metadata-log-dir "$DATA/meta" --user root --group root
}
start_broker(){
  nohup "$HOME_K/bin/kafka-server-start.sh" "$HOME_K/config/server-standalone.properties" >"$ROOT/server.log" 2>&1 & echo $! >"$PIDFILE"
  for _ in $(seq 1 60); do (echo >/dev/tcp/127.0.0.1/9092) >/dev/null 2>&1 && return; sleep 1; done
  fail "broker did not start"
}
cleanup(){
  set +e
  clean_standalone >/dev/null 2>&1 || true
  rm -rf "$ROOT/config-fixture" "$ROOT/produce.txt"
}
trap cleanup EXIT INT TERM

rm -rf "$DATA" "$ROOT/config-fixture"; mkdir -p "$DATA/logs" "$DATA/meta"
CID="$($HOME_K/bin/kafka-storage.sh random-uuid | tail -n 1)"
[[ "$CID" =~ ^[A-Za-z0-9_-]{20,30}$ ]] || fail "invalid cluster id"
run python3 "$TOOL" --deploy standalone --kafka-home "$HOME_K" --cluster-id "$CID" --node-id 1 --advertised-host 127.0.0.1 --log-dirs "$DATA/logs" --metadata-log-dir "$DATA/meta" --no-systemd --user root --group root
CFG="$HOME_K/config/server-standalone.properties"; test -s "$CFG" || fail "generated config missing"
start_broker

run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-create --topic "$TOPIC" --partitions 1 --replication-factor 1
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-create --topic "$TOPIC" --partitions 1 --replication-factor 1
python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-describe --topic "$TOPIC" >"$ROOT/topic-describe.out"
grep -q "$TOPIC" "$ROOT/topic-describe.out"
python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-list >"$ROOT/topic-list.out"
grep -q "$TOPIC" "$ROOT/topic-list.out"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --produce --topic "$TOPIC" --message kafkacli-message-1
printf 'kafkacli-message-2\n' >"$ROOT/produce.txt"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --produce --topic "$TOPIC" --input-file "$ROOT/produce.txt"
sleep 3
CONSUMED=""
for attempt in 1 2 3; do
  CONSUMED="$(python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --consume --topic "$TOPIC" --consumer-group "${GROUP}_${attempt}" --max-messages 2 || true)"
  grep -q kafkacli-message-1 <<<"$CONSUMED" && grep -q kafkacli-message-2 <<<"$CONSUMED" && GROUP="${GROUP}_${attempt}" && break
  sleep 2
done
grep -q kafkacli-message-1 <<<"$CONSUMED" || fail "first message missing"
grep -q kafkacli-message-2 <<<"$CONSUMED" || fail "second message missing"
python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --group-list >"$ROOT/group-list.out"
grep -q "$GROUP" "$ROOT/group-list.out"
python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --group-describe --consumer-group "$GROUP" >"$ROOT/group-describe.out"
grep -q "$TOPIC" "$ROOT/group-describe.out"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --metrics-json >"$ROOT/metrics.json"
grep -q broker_connect "$ROOT/metrics.json" || fail "metrics json missing broker_connect"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --config-describe-broker
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --config-describe-topic --topic "$TOPIC"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --status

if python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server 127.0.0.1:1 --topic-list >/dev/null 2>"$ROOT/error.log"; then fail "wrong broker unexpectedly succeeded"; fi
if python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --consume --topic "$TOPIC" --consumer-group 'bad group' --max-messages 1; then fail "invalid group unexpectedly succeeded"; fi
if grep -q kafkacli-message "$ROOT/error.log"; then fail "message leaked into error log"; fi

# SASL_PLAINTEXT: generated default client properties must authenticate later
# commands without placing a password on a command line or in test output.
clean_standalone
rm -rf "$DATA"; mkdir -p "$DATA/logs" "$DATA/meta"
CID="$($HOME_K/bin/kafka-storage.sh random-uuid | tail -n 1)"
run_sasl python3 "$TOOL" --deploy standalone --deploy-sasl-plain --kafka-home "$HOME_K" --cluster-id "$CID" --node-id 1 --advertised-host 127.0.0.1 --log-dirs "$DATA/logs" --metadata-log-dir "$DATA/meta" --no-systemd --user root --group root
test "$(stat -c %a "$HOME_K/config/kafkacli.client.properties")" = 600 || fail "SASL client properties mode"
start_broker
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-create --topic sasl_delivery --partitions 1 --replication-factor 1
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --produce --topic sasl_delivery --message sasl-message
SASL_OUT="$(python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --consume --topic sasl_delivery --consumer-group sasl_delivery_group --max-messages 1)"
grep -q sasl-message <<<"$SASL_OUT" || fail "SASL message readback"
if env KAFKA_SASL_USERNAME=wrong KAFKA_SASL_PASSWORD=wrong_secret python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-list >"$ROOT/bad-auth.out" 2>"$ROOT/bad-auth.err"; then fail "wrong SASL credentials unexpectedly succeeded"; fi
if grep -q wrong_secret "$ROOT/bad-auth.out" "$ROOT/bad-auth.err"; then fail "SASL secret leaked"; fi

# SASL_SSL: local CA material is generated only for this fixture and removed by
# the outer runner. KafkaCli owns the broker and client properties generation.
clean_standalone
rm -rf "$DATA" "$ROOT/pki"; mkdir -p "$DATA/logs" "$DATA/meta" "$ROOT/pki"
keytool -genkeypair -noprompt -alias kafka -keyalg RSA -keysize 2048 -validity 2 -storetype PKCS12 -keystore "$ROOT/pki/kafka.p12" -storepass fixturepass -keypass fixturepass -dname 'CN=127.0.0.1' -ext 'SAN=ip:127.0.0.1' >"$ROOT/keytool.log" 2>&1
keytool -exportcert -rfc -alias kafka -keystore "$ROOT/pki/kafka.p12" -storetype PKCS12 -storepass fixturepass -file "$ROOT/pki/kafka.crt" >>"$ROOT/keytool.log" 2>&1
keytool -importcert -noprompt -alias kafka -file "$ROOT/pki/kafka.crt" -storetype PKCS12 -keystore "$ROOT/pki/trust.p12" -storepass fixturepass >>"$ROOT/keytool.log" 2>&1
CID="$($HOME_K/bin/kafka-storage.sh random-uuid | tail -n 1)"
run_sasl python3 "$TOOL" --deploy standalone --deploy-sasl-ssl --kafka-home "$HOME_K" --cluster-id "$CID" --node-id 1 --advertised-host 127.0.0.1 --log-dirs "$DATA/logs" --metadata-log-dir "$DATA/meta" --no-systemd --user root --group root --ssl-keystore-path "$ROOT/pki/kafka.p12" --ssl-keystore-password fixturepass --ssl-truststore-path "$ROOT/pki/trust.p12" --ssl-truststore-password fixturepass
nohup "$HOME_K/bin/kafka-server-start.sh" "$HOME_K/config/server-standalone.properties" >"$ROOT/server.log" 2>&1 & echo $! >"$PIDFILE"
for _ in $(seq 1 60); do (echo >/dev/tcp/127.0.0.1/9092) >/dev/null 2>&1 && break; sleep 1; done
(echo >/dev/tcp/127.0.0.1/9092) >/dev/null 2>&1 || fail "SASL_SSL broker did not start"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --topic-create --topic ssl_delivery --partitions 1 --replication-factor 1
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --produce --topic ssl_delivery --message ssl-message
SSL_OUT="$(python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "$BOOT" --consume --topic ssl_delivery --consumer-group ssl_delivery_group --max-messages 1)"
grep -q ssl-message <<<"$SSL_OUT" || fail "SASL_SSL message readback"

# systemd mode is an independent deployment lifecycle from the no-systemd
# fixture above and must remove its unit during scoped cleanup.
clean_standalone
rm -rf "$DATA"; mkdir -p "$DATA/logs" "$DATA/meta"
CID="$($HOME_K/bin/kafka-storage.sh random-uuid | tail -n 1)"
run python3 "$TOOL" --deploy standalone --kafka-home "$HOME_K" --cluster-id "$CID" --node-id 1 --advertised-host 127.0.0.1 --log-dirs "$DATA/logs" --metadata-log-dir "$DATA/meta" --user root --group root --verify
systemctl is-active --quiet kafka-standalone.service || fail "systemd service is not active"
clean_standalone
test ! -e /etc/systemd/system/kafka-standalone.service || fail "systemd unit leaked"

# Split roles: exercise a controller-only metadata plane and a separately
# formatted broker joining that quorum. Distinct ports/data roots isolate it
# from the combined-node scenarios above.
SPLIT="$ROOT/split"; CTRL_PORT=19093; BROKER_PORT=19092; CTRL_ID=10; BROKER_ID=11
rm -rf "$SPLIT"; mkdir -p "$SPLIT/controller-meta" "$SPLIT/controller-logs" "$SPLIT/broker-logs"
CID="$($HOME_K/bin/kafka-storage.sh random-uuid | tail -n 1)"
run python3 "$TOOL" --deploy controller --controller-scope single --kafka-home "$HOME_K" --cluster-id "$CID" --node-id "$CTRL_ID" --advertised-host 127.0.0.1 --controller-listen-host 0.0.0.0 --controller-listen-port "$CTRL_PORT" --controller-quorum-bootstrap-servers "127.0.0.1:$CTRL_PORT" --metadata-log-dir "$SPLIT/controller-meta" --log-dirs "$SPLIT/controller-logs" --no-systemd --user root --group root
nohup "$HOME_K/bin/kafka-server-start.sh" "$HOME_K/config/controller-${CTRL_ID}.properties" >"$SPLIT/controller.log" 2>&1 &
for _ in $(seq 1 60); do (echo >/dev/tcp/127.0.0.1/$CTRL_PORT) >/dev/null 2>&1 && break; sleep 1; done
(echo >/dev/tcp/127.0.0.1/$CTRL_PORT) >/dev/null 2>&1 || fail "split controller did not start"
run python3 "$TOOL" --status --kafka-home "$HOME_K" --bootstrap-controller "127.0.0.1:$CTRL_PORT"
run python3 "$TOOL" --deploy broker --kafka-home "$HOME_K" --cluster-id "$CID" --node-id "$BROKER_ID" --advertised-host 127.0.0.1 --broker-listen-host 0.0.0.0 --broker-listen-port "$BROKER_PORT" --controller-quorum-bootstrap-servers "127.0.0.1:$CTRL_PORT" --log-dirs "$SPLIT/broker-logs" --no-systemd --user root --group root
nohup "$HOME_K/bin/kafka-server-start.sh" "$HOME_K/config/server-broker-${BROKER_ID}.properties" >"$SPLIT/broker.log" 2>&1 &
for _ in $(seq 1 60); do (echo >/dev/tcp/127.0.0.1/$BROKER_PORT) >/dev/null 2>&1 && break; sleep 1; done
(echo >/dev/tcp/127.0.0.1/$BROKER_PORT) >/dev/null 2>&1 || fail "split broker did not start"
run python3 "$TOOL" --kafka-home "$HOME_K" --bootstrap-server "127.0.0.1:$BROKER_PORT" --topic-create --topic split_delivery --partitions 1 --replication-factor 1
run python3 "$TOOL" --status --kafka-home "$HOME_K" --bootstrap-server "127.0.0.1:$BROKER_PORT" --bootstrap-controller "127.0.0.1:$CTRL_PORT"
stop_broker
run python3 "$TOOL" --deploy broker --clean --clean-data --force --kafka-home "$HOME_K" --node-id "$BROKER_ID" --log-dirs "$SPLIT/broker-logs" --user root --group root
run python3 "$TOOL" --deploy controller --clean --clean-data --force --kafka-home "$HOME_K" --node-id "$CTRL_ID" --metadata-log-dir "$SPLIT/controller-meta" --log-dirs "$SPLIT/controller-logs" --user root --group root
test ! -e "$HOME_K/config/controller-${CTRL_ID}.properties" || fail "controller config leaked"
test ! -e "$HOME_K/config/server-broker-${BROKER_ID}.properties" || fail "broker config leaked"
echo "KAFKA_CLI_LIVE_REGRESSION_PASS topic=$TOPIC group=$GROUP messages=2"

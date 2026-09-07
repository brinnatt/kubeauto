#!/usr/bin/env bash
set -Eeuo pipefail
logging_command_failure() {
  local rc=$?
  printf 'LOGGING_COMMAND_FAILURE line=%s function=%s rc=%s\n' \
    "${BASH_LINENO[0]:-unknown}" "${FUNCNAME[1]:-main}" "$rc" >&2
  return "$rc"
}
trap logging_command_failure ERR
BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
ROUTE_KEY="${LOGGING_SOLUTION:-unknown}-${LOGGING_EFK_DELIVERY:-none}"
EVIDENCE_DIR="/var/tmp/kubeauto-logging-evidence"
EVIDENCE_FILE="$EVIDENCE_DIR/${ROUTE_KEY}.tsv"
RUNTIME_REGISTRY_NAME="kubeauto-logging-registry"
RUNTIME_REGISTRY_DATA="/var/lib/kubeauto-logging-registry"
RUNTIME_REGISTRY_MARKER="kubeauto-logging-runtime"
LOGGING_FOCUS_CASE="${LOGGING_FOCUS_CASE:-}"
mkdir -p "$EVIDENCE_DIR"
rm -f "$EVIDENCE_FILE"
fail() { echo "[FAIL] $*" >&2; exit 1; }
case_pass() {
  local id="$1" detail="${2:-verified}"
  printf '%s\t%s\t%s\t%s\n' "$id" "$ROUTE_KEY" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$detail" >> "$EVIDENCE_FILE"
  echo "LOGGING_CASE_PASS id=$id route=$ROUTE_KEY detail=$detail"
}
should_run_extended_case() {
  [[ -z "$LOGGING_FOCUS_CASE" || "$LOGGING_FOCUS_CASE" == extended || "$LOGGING_FOCUS_CASE" == "$1" ]]
}

run_extended_cases() {
  echo LOGGING_STAGE_BEGIN extended-delivery
  if should_run_extended_case LOGGING-38; then
    # Exercise one reversible product configuration revision. The release is
    # rolled forward and then back while a real log and every PVC identity are
    # retained; this does not pretend that an unsupported data-format downgrade
    # is a valid rollback.
    if [[ "$LOGGING_SOLUTION" == loki ]]; then
      # LOGGING-37 restarts the object store and may close the existing
      # diagnostic port-forward. Re-establish it before the upgrade boundary
      # so a stale local socket cannot mask the product result.
      restart_loki_gateway_forward
      change_id="logging-loki-change-$(date +%s)"
      change_marker="${change_id}-retained"
      kubectl -n logging-smoke run "$change_marker" \
        --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
        --restart=Never --labels=app.kubernetes.io/managed-by=kubeauto \
        -- sh -c 'printf "%s\n" "$1"' sh "$change_marker" >/dev/null
      kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$change_marker" --timeout=120s
      loki_wait_for_exact_marker \
        "{namespace=\"logging-smoke\",pod=\"${change_marker}\"}" \
        "$change_marker" LOGGING_CHANGE_DATA_WAIT 36
      loki_revision_before="$($HELM status loki --namespace logging -o json | jq -r '.version')"
      loki_pvcs_before="$(kubectl -n logging get pvc -l app.kubernetes.io/component=single-binary -o json \
        | jq -Sc '[.items[] | {name:.metadata.name,uid:.metadata.uid}] | sort_by(.name)')"
      "$HELM" get values loki --namespace logging -a -o yaml >/tmp/kubeauto-logging-loki-values-before.yaml
      "$HELM" get manifest loki --namespace logging >/tmp/kubeauto-logging-loki-manifest-before.yaml
      "$HELM" upgrade loki "$BASE/roles/cluster-addon/files/loki-18.9.0.tgz" \
        --namespace logging -f "$BASE/clusters/$CLUSTER/yml/logging/loki-values.yaml" \
        --set-string "singleBinary.podAnnotations.kubeauto\\.io/change-id=$change_id" \
        --wait --timeout 20m --history-max 10
      loki_revision_changed="$($HELM status loki --namespace logging -o json | jq -r '.version')"
      [[ "$loki_revision_changed" -gt "$loki_revision_before" ]]
      kubectl -n logging rollout status statefulset/loki --timeout=20m
      restart_loki_gateway_forward
      change_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${change_marker}\"}")"
      jq -e --arg marker "$change_marker" \
        'any(.data.result[]?.values[]?; .[1] | contains($marker))' <<<"$change_response" >/dev/null
      "$HELM" rollback loki "$loki_revision_before" --namespace logging --wait --timeout 20m
      kubectl -n logging rollout status statefulset/loki --timeout=20m
      restart_loki_gateway_forward
      loki_revision_rolled_back="$($HELM status loki --namespace logging -o json | jq -r '.version')"
      [[ "$loki_revision_rolled_back" -gt "$loki_revision_changed" ]]
      loki_pvcs_after="$(kubectl -n logging get pvc -l app.kubernetes.io/component=single-binary -o json \
        | jq -Sc '[.items[] | {name:.metadata.name,uid:.metadata.uid}] | sort_by(.name)')"
      [[ "$loki_pvcs_before" == "$loki_pvcs_after" ]]
      change_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${change_marker}\"}")"
      jq -e --arg marker "$change_marker" \
        'any(.data.result[]?.values[]?; .[1] | contains($marker))' <<<"$change_response" >/dev/null
      echo "LOGGING_CHANGE_ROLLBACK_PASS before=$loki_revision_before changed=$loki_revision_changed current=$loki_revision_rolled_back pvc_identity=preserved data=retained"
    else
      change_id="logging-efk-change-$(date +%s)"
      efk_pvcs_before="$(kubectl -n logging get pvc -l common.k8s.elastic.co/type=elasticsearch -o json \
        | jq -Sc '[.items[] | {name:.metadata.name,uid:.metadata.uid}] | sort_by(.name)')"
      # The idempotent setup may recreate the Service endpoints. Re-establish
      # the diagnostic forward and wait for the ES API before querying data.
      start_es_forward
      "${es_curl[@]}" -fsS --get --data-urlencode "q=$smoke_marker" \
        "https://${es_tls_name}:19200/k8s-*/_count" | jq -e '.count > 0' >/dev/null
      config_restore_file="$(mktemp)"
      cp "$CFG" "$config_restore_file"
      python3 - "$CFG" <<'PY'
import re
import sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = re.sub(r'(?m)^logging_efk_retention_days:.*$', 'logging_efk_retention_days: 31', text, count=1)
open(path, "w", encoding="utf-8").write(text)
PY
      stop_es_forward
      "$K" setup "$CLUSTER" 07 </dev/null
      start_es_forward
      "${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_ilm/policy/k8s-retention" \
        | jq -e '.["k8s-retention"].policy.phases.delete.min_age == "31d"' >/dev/null
      "${es_curl[@]}" -fsS --get --data-urlencode "q=$smoke_marker" \
        "https://${es_tls_name}:19200/k8s-*/_count" | jq -e '.count > 0' >/dev/null
      stop_es_forward
      cp "$config_restore_file" "$CFG"
      "$K" setup "$CLUSTER" 07 </dev/null
      start_es_forward
      "${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_ilm/policy/k8s-retention" \
        | jq -e '.["k8s-retention"].policy.phases.delete.min_age == "30d"' >/dev/null
      efk_pvcs_after="$(kubectl -n logging get pvc -l common.k8s.elastic.co/type=elasticsearch -o json \
        | jq -Sc '[.items[] | {name:.metadata.name,uid:.metadata.uid}] | sort_by(.name)')"
      [[ "$efk_pvcs_before" == "$efk_pvcs_after" ]]
      "${es_curl[@]}" -fsS --get --data-urlencode "q=$smoke_marker" \
        "https://${es_tls_name}:19200/k8s-*/_count" | jq -e '.count > 0' >/dev/null
      rm -f "$config_restore_file"
      config_restore_file=
      echo "LOGGING_CHANGE_ROLLBACK_PASS change=$change_id retention=31d->30d pvc_identity=preserved data=retained"
    fi
    case_pass LOGGING-38 upgrade-preflight-rollback-boundary-retention
  fi

  if should_run_extended_case LOGGING-39; then
    # Secret-backed authentication and least-privilege checks.  No credential
    # value is copied into a ConfigMap or emitted into the gate log.
    if [[ "$LOGGING_SOLUTION" == loki ]]; then
      kubectl -n logging get secret logging-loki-storage logging-loki-gateway-auth logging-loki-gateway-client >/dev/null
      kubectl -n logging get secret logging-loki-gateway-auth -o json \
        | jq -e '.data | to_entries | any(.key == ".htpasswd" and (.value | length > 0))' >/dev/null
      # Consume the complete kubectl stream; grep -q can close the pipe early
      # and turn a successful match into rc=141 under pipefail.
      kubectl -n logging get serviceaccount -o name | grep -F '/alloy' >/dev/null
      test "$(kubectl auth can-i --as=system:serviceaccount:logging:alloy get secrets -n logging 2>/dev/null || true)" = no
      ! kubectl -n logging get configmap -o json | jq -e '.. | strings | select(. == "test-gateway-password")' >/dev/null
      rotation_tmp="$(mktemp -d)"
      chmod 0700 "$rotation_tmp"
      kubectl -n logging get secret logging-loki-gateway-auth -o json >"$rotation_tmp/gateway-auth-before.json"
      kubectl -n logging get secret logging-loki-gateway-client -o json >"$rotation_tmp/gateway-client-before.json"
      gateway_auth_uid_before="$(jq -r '.metadata.uid' "$rotation_tmp/gateway-auth-before.json")"
      gateway_client_uid_before="$(jq -r '.metadata.uid' "$rotation_tmp/gateway-client-before.json")"
      old_auth_user="$auth_user"
      old_auth_password="$auth_password"
      new_auth_password="$(openssl rand -hex 24)"
      printf '%s:%s\n' "$old_auth_user" "$(printf '%s' "$new_auth_password" | openssl passwd -apr1 -stdin)" \
        >"$rotation_tmp/.htpasswd"
      printf '%s' "$old_auth_user" >"$rotation_tmp/LOKI_GATEWAY_USERNAME"
      printf '%s' "$new_auth_password" >"$rotation_tmp/LOKI_GATEWAY_PASSWORD"
      kubectl -n logging create secret generic logging-loki-gateway-auth \
        --from-file=.htpasswd="$rotation_tmp/.htpasswd" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
      kubectl -n logging create secret generic logging-loki-gateway-client \
        --from-file=LOKI_GATEWAY_USERNAME="$rotation_tmp/LOKI_GATEWAY_USERNAME" \
        --from-file=LOKI_GATEWAY_PASSWORD="$rotation_tmp/LOKI_GATEWAY_PASSWORD" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
      auth_user="$old_auth_user"
      auth_password="$new_auth_password"
      gateway_deployment="$(kubectl -n logging get deployment -l app.kubernetes.io/component=gateway -o jsonpath='{.items[0].metadata.name}')"
      kubectl -n logging rollout restart "deployment/$gateway_deployment" >/dev/null
      kubectl -n logging rollout restart daemonset/alloy >/dev/null
      kubectl -n logging rollout status "deployment/$gateway_deployment" --timeout=10m >/dev/null
      kubectl -n logging rollout status daemonset/alloy --timeout=10m >/dev/null
      restart_loki_gateway_forward
      new_auth_code="$(curl -sS -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:19100/loki/api/v1/status/buildinfo)"
      old_auth_code="$(curl -sS -u "$old_auth_user:$old_auth_password" -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:19100/loki/api/v1/status/buildinfo)"
      [[ "$new_auth_code" == 200 && "$old_auth_code" == 401 ]]
      rotation_marker="kubeauto-loki-rotation-$(date +%s)"
      kubectl -n logging-smoke run "$rotation_marker" \
        --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
        --restart=Never --labels=app.kubernetes.io/managed-by=kubeauto \
        -- sh -c 'printf "%s\n" "$1"' sh "$rotation_marker" >/dev/null
      kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$rotation_marker" --timeout=120s
      loki_wait_for_exact_marker \
        "{namespace=\"logging-smoke\",pod=\"${rotation_marker}\"}" \
        "$rotation_marker" LOGGING_SECRET_ROTATION_WAIT 36
      jq 'del(.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.managedFields)' \
        "$rotation_tmp/gateway-auth-before.json" | kubectl apply -f - >/dev/null
      jq 'del(.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.managedFields)' \
        "$rotation_tmp/gateway-client-before.json" | kubectl apply -f - >/dev/null
      auth_user="$old_auth_user"
      auth_password="$old_auth_password"
      kubectl -n logging rollout restart "deployment/$gateway_deployment" >/dev/null
      kubectl -n logging rollout restart daemonset/alloy >/dev/null
      kubectl -n logging rollout status "deployment/$gateway_deployment" --timeout=10m >/dev/null
      kubectl -n logging rollout status daemonset/alloy --timeout=10m >/dev/null
      restart_loki_gateway_forward
      restored_auth_code="$(curl -sS -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:19100/loki/api/v1/status/buildinfo)"
      [[ "$restored_auth_code" == 200 ]]
      gateway_auth_uid_after="$(kubectl -n logging get secret logging-loki-gateway-auth -o jsonpath='{.metadata.uid}')"
      gateway_client_uid_after="$(kubectl -n logging get secret logging-loki-gateway-client -o jsonpath='{.metadata.uid}')"
      [[ "$gateway_auth_uid_before" == "$gateway_auth_uid_after" && "$gateway_client_uid_before" == "$gateway_client_uid_after" ]]
      rotation_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${rotation_marker}\"}")"
      jq -e --arg marker "$rotation_marker" \
        'any(.data.result[]?.values[]?; .[1] | contains($marker))' <<<"$rotation_response" >/dev/null
      rm -rf "$rotation_tmp"
      echo "LOGGING_SECRET_ROTATION_PASS new=accepted old=rejected restored=accepted uid=preserved data=retained"
    else
      kubectl -n logging get secret logging-snapshot-s3 logging-efk-writer logging-es-http-certs-public >/dev/null
      kubectl -n logging get secret logging-es-http-certs-public -o jsonpath='{.data.ca\.crt}' | base64 -d | openssl x509 -noout -subject >/dev/null
      test "$(kubectl auth can-i --as=system:serviceaccount:logging:fluent-bit get secrets -n logging 2>/dev/null || true)" = no
      ! kubectl -n logging get configmap -o yaml | grep -Fq 'test-writer-password-change-me'
      rotation_tmp="$(mktemp -d)"
      chmod 0700 "$rotation_tmp"
      kubectl -n logging get secret logging-efk-writer -o json >"$rotation_tmp/writer-before.json"
      writer_uid_before="$(jq -r '.metadata.uid' "$rotation_tmp/writer-before.json")"
      derived_uid_before="$(kubectl -n logging get secret logging-fluent-bit-es-auth -o jsonpath='{.metadata.uid}')"
      writer_user="$(jq -r '.data.username' "$rotation_tmp/writer-before.json" | base64 -d)"
      old_writer_password="$(jq -r '.data.password' "$rotation_tmp/writer-before.json" | base64 -d)"
      new_writer_password="$(openssl rand -hex 24)"
      printf '%s' "$writer_user" >"$rotation_tmp/username"
      printf '%s' "$new_writer_password" >"$rotation_tmp/password"
      kubectl -n logging create secret generic logging-efk-writer \
        --from-file=username="$rotation_tmp/username" --from-file=password="$rotation_tmp/password" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
      stop_es_forward
      "$K" setup "$CLUSTER" 07 </dev/null
      start_es_forward
      new_writer_code="$(curl --resolve "${es_tls_name}:19200:127.0.0.1" --cacert "$es_tmp/ca.crt" \
        -u "$writer_user:$new_writer_password" -sS -o /dev/null -w '%{http_code}' \
        "https://${es_tls_name}:19200/_security/_authenticate")"
      old_writer_code="$(curl --resolve "${es_tls_name}:19200:127.0.0.1" --cacert "$es_tmp/ca.crt" \
        -u "$writer_user:$old_writer_password" -sS -o /dev/null -w '%{http_code}' \
        "https://${es_tls_name}:19200/_security/_authenticate")"
      [[ "$new_writer_code" == 200 && "$old_writer_code" == 401 ]]
      derived_password="$(kubectl -n logging get secret logging-fluent-bit-es-auth -o jsonpath='{.data.ES_PASSWORD}' | base64 -d)"
      [[ "$derived_password" == "$new_writer_password" ]]
      if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
        kubectl -n logging rollout restart daemonset/fluent-bit-kafka deployment/logstash >/dev/null
        kubectl -n logging rollout status daemonset/fluent-bit-kafka --timeout=10m >/dev/null
        kubectl -n logging rollout status deployment/logstash --timeout=20m >/dev/null
      else
        kubectl -n logging rollout restart daemonset/fluent-bit >/dev/null
        kubectl -n logging rollout status daemonset/fluent-bit --timeout=10m >/dev/null
      fi
      rotation_marker="kubeauto-efk-rotation-$(date +%s)"
      kubectl -n logging-smoke run "$rotation_marker" \
        --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
        --restart=Never --labels=app.kubernetes.io/managed-by=kubeauto \
        -- sh -c 'printf "%s\n" "$1"' sh "$rotation_marker" >/dev/null
      kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$rotation_marker" --timeout=120s
      rotation_count=0
      for attempt in $(seq 1 36); do
        rotation_count="$("${es_curl[@]}" -fsS -XPOST -H 'content-type: application/json' \
          --data "$(jq -cn --arg marker "$rotation_marker" '{query:{term:{"message.keyword":$marker}}}')" \
          "https://${es_tls_name}:19200/k8s-*/_count" 2>/dev/null | jq -r '.count // 0' || echo 0)"
        echo "LOGGING_SECRET_ROTATION_WAIT attempt=${attempt}/36 count=${rotation_count:-0}"
        [[ "$rotation_count" == 1 ]] && break
        sleep 5
      done
      [[ "$rotation_count" == 1 ]]
      jq 'del(.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.managedFields)' \
        "$rotation_tmp/writer-before.json" | kubectl apply -f - >/dev/null
      stop_es_forward
      "$K" setup "$CLUSTER" 07 </dev/null
      start_es_forward
      if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
        kubectl -n logging rollout restart daemonset/fluent-bit-kafka deployment/logstash >/dev/null
        kubectl -n logging rollout status daemonset/fluent-bit-kafka --timeout=10m >/dev/null
        kubectl -n logging rollout status deployment/logstash --timeout=20m >/dev/null
      else
        kubectl -n logging rollout restart daemonset/fluent-bit >/dev/null
        kubectl -n logging rollout status daemonset/fluent-bit --timeout=10m >/dev/null
      fi
      restored_writer_code="$(curl --resolve "${es_tls_name}:19200:127.0.0.1" --cacert "$es_tmp/ca.crt" \
        -u "$writer_user:$old_writer_password" -sS -o /dev/null -w '%{http_code}' \
        "https://${es_tls_name}:19200/_security/_authenticate")"
      rejected_new_writer_code="$(curl --resolve "${es_tls_name}:19200:127.0.0.1" --cacert "$es_tmp/ca.crt" \
        -u "$writer_user:$new_writer_password" -sS -o /dev/null -w '%{http_code}' \
        "https://${es_tls_name}:19200/_security/_authenticate")"
      [[ "$restored_writer_code" == 200 && "$rejected_new_writer_code" == 401 ]]
      writer_uid_after="$(kubectl -n logging get secret logging-efk-writer -o jsonpath='{.metadata.uid}')"
      derived_uid_after="$(kubectl -n logging get secret logging-fluent-bit-es-auth -o jsonpath='{.metadata.uid}')"
      [[ "$writer_uid_before" == "$writer_uid_after" && "$derived_uid_before" == "$derived_uid_after" ]]
      "${es_curl[@]}" -fsS -XPOST -H 'content-type: application/json' \
        --data "$(jq -cn --arg marker "$rotation_marker" '{query:{term:{"message.keyword":$marker}}}')" \
        "https://${es_tls_name}:19200/k8s-*/_count" | jq -e '.count == 1' >/dev/null
      rm -rf "$rotation_tmp"
      echo "LOGGING_SECRET_ROTATION_PASS new=accepted old=rejected restored=accepted uid=preserved data=retained"
    fi
    case_pass LOGGING-39 secret-rotation-tls-rbac-no-leakage
  fi

  if should_run_extended_case LOGGING-40; then
    # A bounded customer-shaped ingestion/query probe proves both its source
    # fixture and the exact downstream count. Preserve a sanitized response on
    # failure so a live run cannot be wasted on an opaque empty value.
    perf_started="$(date +%s)"
    perf_marker="kubeauto-${LOGGING_SOLUTION}-perf-$(date +%s)"
    if [[ "$LOGGING_SOLUTION" == loki ]]; then
      kubectl -n logging get statefulset loki -o json | jq -e '
        .spec.template.spec.containers[] | select(.name == "loki") |
        .resources.requests.cpu and .resources.requests.memory and
        .resources.limits.cpu and .resources.limits.memory' >/dev/null
      kubectl -n logging get daemonset alloy -o json | jq -e '
        .spec.template.spec.containers[] | select(.name == "alloy") |
        .resources.requests.cpu and .resources.requests.memory and
        .resources.limits.cpu and .resources.limits.memory' >/dev/null
      collector_name=alloy
    elif [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
      kubectl -n logging get daemonset fluent-bit-kafka -o json | jq -e '
        .spec.template.spec.containers[] | select(.name == "fluent-bit") |
        .resources.requests.cpu and .resources.requests.memory and
        .resources.limits.cpu and .resources.limits.memory' >/dev/null
      kubectl -n logging get deployment logstash -o json | jq -e '
        .spec.template.spec.containers[] | select(.name == "logstash") |
        .resources.requests.cpu and .resources.requests.memory and
        .resources.limits.cpu and .resources.limits.memory' >/dev/null
      collector_name=fluent-bit-kafka
    else
      kubectl -n logging get daemonset fluent-bit -o json | jq -e '
        .spec.template.spec.containers[] | select(.name == "fluent-bit") |
        .resources.requests.cpu and .resources.requests.memory and
        .resources.limits.cpu and .resources.limits.memory' >/dev/null
      collector_name=fluent-bit
    fi
    backpressure_policy=logging-perf-backpressure
    kubectl -n logging apply -f - <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${backpressure_policy}
  labels:
    app.kubernetes.io/managed-by: kubeauto
    kubeauto.io/component: logging-test
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: ${collector_name}
  policyTypes: [Egress]
  egress:
    - ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
YAML
    kubectl -n logging get networkpolicy "$backpressure_policy" >/dev/null
    sleep 5
    kubectl -n logging-smoke run "$perf_marker" \
      --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
      --restart=Never \
      --labels=app.kubernetes.io/managed-by=kubeauto,kubeauto.io/component=logging-test \
      -- sh -c 'i=1; while [ "$i" -le 20 ]; do printf "%s-%02d\n" "$1" "$i"; i=$((i + 1)); done; sleep 300' sh "$perf_marker" >/dev/null
    # Keep the source container alive while the blocked collector is
    # recovered; otherwise a fast runtime may reclaim its CRI log file before
    # the durable Fluent Bit backlog can be replayed.
    kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Running "pod/$perf_marker" --timeout=120s
    read -r perf_source_count perf_source_unique < <(
      kubectl -n logging-smoke logs "$perf_marker" \
        | awk -v marker="$perf_marker" 'index($0, marker "-") == 1 {count++; seen[$0]=1} END {print count+0, length(seen)}'
    )
    echo "LOGGING_PERF_SOURCE marker=$perf_marker lines=$perf_source_count unique=$perf_source_unique"
    [[ "$perf_source_count" -eq 20 && "$perf_source_unique" -eq 20 ]] || \
      fail "LOGGING-40 source fixture did not emit 20 unique lines"

    perf_response_file=/tmp/kubeauto-logging-LOGGING-40-response.json
    perf_curl_error=/tmp/kubeauto-logging-LOGGING-40-curl.err
    perf_count=0
    if [[ "$LOGGING_SOLUTION" == loki ]]; then
      perf_http="$(loki_query_capture "{namespace=\"logging-smoke\",pod=\"${perf_marker}\"}" "$perf_response_file" 2>"$perf_curl_error")"
      perf_blocked_count="$(jq -r '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' \
        --arg marker "$perf_marker" "$perf_response_file")"
      [[ "$perf_http" == 200 && "$perf_blocked_count" -eq 0 ]]
      echo "LOGGING_BACKPRESSURE_BLOCKED route=loki count=$perf_blocked_count"
      kubectl -n logging delete networkpolicy "$backpressure_policy" --wait=true >/dev/null
      for attempt in $(seq 1 36); do
        perf_http=000
        perf_curl_rc=0
        perf_http="$(loki_query_capture "{namespace=\"logging-smoke\",pod=\"${perf_marker}\"}" "$perf_response_file" 2>"$perf_curl_error")" || perf_curl_rc=$?
        perf_status=invalid-json
        perf_count=0
        perf_error=none
        if [[ -s "$perf_response_file" ]] && jq -e . "$perf_response_file" >/dev/null 2>&1; then
          perf_status="$(jq -r '.status // "missing"' "$perf_response_file")"
          perf_count="$(jq -r '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' \
            --arg marker "$perf_marker" "$perf_response_file")"
          perf_error="$(jq -r '.errorType // .error // "none"' "$perf_response_file")"
        fi
        echo "LOGGING_PERF_QUERY attempt=${attempt}/36 http=$perf_http curl_rc=$perf_curl_rc json_status=$perf_status count=$perf_count error=$perf_error"
        [[ "$perf_http" == 200 && "$perf_status" == success && "$perf_count" -eq 20 ]] && break
        sleep 5
      done
      if [[ "$perf_http" != 200 || "$perf_status" != success || "$perf_count" -ne 20 ]]; then
        echo "LOGGING_PERF_DIAGNOSTIC response=$perf_response_file curl_error=$perf_curl_error" >&2
        fail "LOGGING-40 Loki query did not return all 20 source lines"
      fi
    else
      # Re-establish the diagnostic channel after the preceding setup cycles;
      # an idle port-forward may close while the product remains healthy.
      start_es_forward
      "${es_curl[@]}" -sS -XPOST -H 'content-type: application/json' \
        --data "$(jq -cn --arg marker "$perf_marker" '{query:{prefix:{"message.keyword":$marker}}}')" \
        -o "$perf_response_file" "https://${es_tls_name}:19200/k8s-*/_count" 2>"$perf_curl_error"
      perf_blocked_count="$(jq -r '.count // 0' "$perf_response_file")"
      [[ "$perf_blocked_count" -eq 0 ]]
      echo "LOGGING_BACKPRESSURE_BLOCKED route=efk count=$perf_blocked_count"
      kubectl -n logging delete networkpolicy "$backpressure_policy" --wait=true >/dev/null
      # Keep the collector process alive while the controlled egress outage is
      # lifted. Small fixtures may remain in the bounded in-memory queue; a
      # process restart would discard those records and turn a valid recovery
      # check into a test-gate false negative. The DaemonSet rollout and health
      # checks are covered independently by LOGGING-17.
      for attempt in $(seq 1 36); do
        perf_curl_rc=0
        "${es_curl[@]}" -sS -XPOST -H 'content-type: application/json' \
          --data "$(jq -cn --arg marker "$perf_marker" '{query:{prefix:{"message.keyword":$marker}}}')" \
          -o "$perf_response_file" "https://${es_tls_name}:19200/k8s-*/_count" 2>"$perf_curl_error" || perf_curl_rc=$?
        perf_count=0
        [[ -s "$perf_response_file" ]] && perf_count="$(jq -r '.count // 0' "$perf_response_file" 2>/dev/null || echo 0)"
        echo "LOGGING_PERF_QUERY attempt=${attempt}/36 curl_rc=$perf_curl_rc count=$perf_count"
        [[ "$perf_curl_rc" -eq 0 && "$perf_count" -eq 20 ]] && break
        sleep 5
      done
      if [[ "$perf_curl_rc" -ne 0 || "$perf_count" -ne 20 ]]; then
        echo "LOGGING_PERF_DIAGNOSTIC response=$perf_response_file curl_error=$perf_curl_error" >&2
        fail "LOGGING-40 Elasticsearch query did not return all 20 source lines"
      fi
    fi
    rm -f "$perf_response_file" "$perf_curl_error"
    perf_elapsed=$(( $(date +%s) - perf_started ))
    [[ "$perf_elapsed" -lt 300 ]]
    case_pass LOGGING-40 bounded-ingestion-query-backpressure-resource-limits
  fi

  if should_run_extended_case LOGGING-43; then
    # Submit the opposite route through the customer product entry point. It
    # must fail at ownership discovery before changing the live namespace.
    route_label="$(kubectl get namespace logging -o jsonpath='{.metadata.labels.kubeauto\.io/logging-solution}')"
    [[ "$route_label" == "$LOGGING_SOLUTION" ]]
    route_namespace_uid_before="$(kubectl get namespace logging -o jsonpath='{.metadata.uid}')"
    if [[ "$LOGGING_SOLUTION" == loki ]]; then
      ! kubectl -n logging get elasticsearches,kibanas -o name 2>/dev/null | grep -q .
      route_workload_uid_before="$(kubectl -n logging get statefulset loki -o jsonpath='{.metadata.uid}')"
      opposite_solution=efk
    else
      ! kubectl -n logging get statefulset -l app.kubernetes.io/component=single-binary -o name 2>/dev/null | grep -q .
      route_workload_uid_before="$(kubectl -n logging get elasticsearch logging -o jsonpath='{.metadata.uid}')"
      opposite_solution=loki
    fi
    conflict_log=/tmp/kubeauto-logging-LOGGING-43-conflict.log
    conflict_cfg_backup="$(mktemp)"
    cp "$CFG" "$conflict_cfg_backup"
    python3 - "$CFG" "$opposite_solution" <<'PY'
import re
import sys

path, solution = sys.argv[1:]
text = open(path, encoding="utf-8").read()
text = re.sub(r'(?m)^logging_solution:.*$', f'logging_solution: "{solution}"', text, count=1)
open(path, "w", encoding="utf-8").write(text)
PY
    conflict_rc=0
    "$K" setup "$CLUSTER" 07 </dev/null >"$conflict_log" 2>&1 || conflict_rc=$?
    cp "$conflict_cfg_backup" "$CFG"
    rm -f "$conflict_cfg_backup"
    [[ "$conflict_rc" -ne 0 ]]
    grep -Fq "existing kubeauto logging solution=$LOGGING_SOLUTION, requested=$opposite_solution" "$conflict_log"
    route_namespace_uid_after="$(kubectl get namespace logging -o jsonpath='{.metadata.uid}')"
    route_label_after="$(kubectl get namespace logging -o jsonpath='{.metadata.labels.kubeauto\.io/logging-solution}')"
    if [[ "$LOGGING_SOLUTION" == loki ]]; then
      route_workload_uid_after="$(kubectl -n logging get statefulset loki -o jsonpath='{.metadata.uid}')"
    else
      route_workload_uid_after="$(kubectl -n logging get elasticsearch logging -o jsonpath='{.metadata.uid}')"
    fi
    [[ "$route_namespace_uid_before" == "$route_namespace_uid_after" ]]
    [[ "$route_workload_uid_before" == "$route_workload_uid_after" ]]
    [[ "$route_label_after" == "$LOGGING_SOLUTION" ]]
    echo "LOGGING_ROUTE_CONFLICT_REJECTED existing=$LOGGING_SOLUTION requested=$opposite_solution rc=$conflict_rc identity=preserved"
    case_pass LOGGING-43 exclusive-route-no-silent-adoption
  fi

  if should_run_extended_case LOGGING-44; then
    "$PY" -m unittest tests.unit.test_logging_documentation -v
    case_pass LOGGING-44 customer-route-main-and-rollback-documentation
  fi

  if should_run_extended_case LOGGING-41 || should_run_extended_case LOGGING-42; then
    # Build isolated namespaces in normal, failed and interrupted states, then
    # exercise the real scoped cleanup twice. Alternate namespaces deliberately
    # cannot touch the live route's cluster-scoped resources.
    bash -n "$BASE/tests/helpers/logging-cleanup.sh"
    cleanup_route="$LOGGING_SOLUTION"
    [[ "$cleanup_route" == loki ]] && cleanup_case=LOGGING-42 || cleanup_case=LOGGING-41
    for cleanup_outcome in normal failed interrupted; do
      cleanup_ns="logging-cleanup-${cleanup_route}-${cleanup_outcome}-$$"
      kubectl create namespace "$cleanup_ns" >/dev/null
      kubectl label namespace "$cleanup_ns" \
        app.kubernetes.io/managed-by=kubeauto \
        kubeauto.io/component=logging \
        kubeauto.io/logging-solution="$cleanup_route" >/dev/null
      kubectl -n "$cleanup_ns" create secret generic logging-cleanup-fixture \
        --from-literal=state="$cleanup_outcome" >/dev/null
      kubectl -n "$cleanup_ns" label secret logging-cleanup-fixture \
        app.kubernetes.io/managed-by=kubeauto kubeauto.io/component=logging >/dev/null
      cleanup_pod="logging-${cleanup_route}-${cleanup_outcome}"
      case "$cleanup_outcome" in
        normal)
          kubectl -n "$cleanup_ns" run "$cleanup_pod" \
            --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 --restart=Never \
            --labels=app.kubernetes.io/managed-by=kubeauto,kubeauto.io/component=logging \
            -- sh -c 'exit 0' >/dev/null
          kubectl -n "$cleanup_ns" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$cleanup_pod" --timeout=120s
          ;;
        failed)
          kubectl -n "$cleanup_ns" run "$cleanup_pod" \
            --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 --restart=Never \
            --labels=app.kubernetes.io/managed-by=kubeauto,kubeauto.io/component=logging \
            -- sh -c 'exit 17' >/dev/null
          kubectl -n "$cleanup_ns" wait --for=jsonpath='{.status.phase}'=Failed "pod/$cleanup_pod" --timeout=120s
          ;;
        interrupted)
          kubectl -n "$cleanup_ns" run "$cleanup_pod" \
            --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 --restart=Never \
            --labels=app.kubernetes.io/managed-by=kubeauto,kubeauto.io/component=logging \
            -- sh -c 'sleep 3600' >/dev/null
          kubectl -n "$cleanup_ns" wait --for=condition=Ready "pod/$cleanup_pod" --timeout=120s
          ;;
      esac
      LOGGING_NAMESPACE="$cleanup_ns" bash "$BASE/tests/helpers/logging-cleanup.sh"
      LOGGING_NAMESPACE="$cleanup_ns" bash "$BASE/tests/helpers/logging-cleanup.sh" --verify
      LOGGING_NAMESPACE="$cleanup_ns" bash "$BASE/tests/helpers/logging-cleanup.sh"
      ! kubectl get namespace "$cleanup_ns" >/dev/null 2>&1
      kubectl get namespace logging >/dev/null
      echo "LOGGING_CLEANUP_SCENARIO_PASS route=$cleanup_route outcome=$cleanup_outcome scope=preserved"
    done
    if [[ "$cleanup_case" == LOGGING-41 ]]; then
      case_pass LOGGING-41 efk-scoped-normal-failed-interrupted-cleanup
    else
      case_pass LOGGING-42 loki-scoped-normal-failed-interrupted-cleanup
    fi
  fi
}
echo LOGGING_STAGE_BEGIN artifact
bash "$BASE/tests/helpers/logging-artifact-gate.sh"
echo LOGGING_STAGE_BEGIN local-contract
PY="$BASE/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3
cd "$BASE"
"$PY" -m unittest \
  tests.unit.test_logging_delivery \
  tests.unit.test_logging_documentation \
  tests.unit.test_six_repo_version_sync -v
for id in LOGGING-01 LOGGING-02 LOGGING-03 LOGGING-04 LOGGING-05; do case_pass "$id" static; done
echo LOGGING_STAGE_BEGIN cluster-preflight
command -v kubectl >/dev/null
CLUSTER="${LOGGING_CLUSTER:-logging-gate}"
KC="$BASE/clusters/$CLUSTER/kubectl.kubeconfig"
K="$BASE/.venv/bin/kubecli"; [[ -x "$K" ]] || K="$(command -v kubecli)"
if [[ ! -s "$KC" ]]; then
  echo LOGGING_STAGE_BEGIN cluster-build
  mkdir -p "$BASE/clusters"
  cat > "$BASE/clusters/$CLUSTER.hosts" <<'HOSTS'
[etcd]
192.168.122.243 k8s_nodename='logging-master-243'
192.168.122.246 k8s_nodename='logging-master-246'
192.168.122.217 k8s_nodename='logging-master-217'
[kube_master]
192.168.122.243 k8s_nodename='logging-master-243'
192.168.122.246 k8s_nodename='logging-master-246'
192.168.122.217 k8s_nodename='logging-master-217'
[kube_node]
192.168.122.210 k8s_nodename='logging-node-210'
192.168.122.216 k8s_nodename='logging-node-216'
192.168.122.193 k8s_nodename='logging-node-193'
[all:vars]
SECURE_PORT="6443"
CONTAINER_RUNTIME="containerd"
CLUSTER_NETWORK="calico"
PROXY_MODE="ipvs"
SERVICE_CIDR="10.83.0.0/16"
CLUSTER_CIDR="172.30.0.0/16"
NODE_PORT_RANGE="30000-32767"
CLUSTER_DNS_DOMAIN="cluster.local"
bin_dir="/usr/local/bin"
base_dir="/usr/local/kubeauto"
cluster_dir="{{ base_dir }}/clusters/logging-gate"
ca_dir="/etc/kubernetes/ssl"
k8s_nodename=''
ansible_user=root
HOSTS
  "$K" new "$CLUSTER" </dev/null
  cp "$BASE/clusters/$CLUSTER.hosts" "$BASE/clusters/$CLUSTER/hosts"
  CFG="$BASE/clusters/$CLUSTER/config.yml"
  sed -i 's/__k8s_ver__/1.33.6/g; s/^KUBE_RESERVED_ENABLED: "yes"/KUBE_RESERVED_ENABLED: "no"/; s/^SYS_RESERVED_ENABLED: "yes"/SYS_RESERVED_ENABLED: "no"/' "$CFG"
  if grep -q '^REGISTRY_HOST_IP:' "$CFG"; then
    sed -i 's|^REGISTRY_HOST_IP:.*|REGISTRY_HOST_IP: "192.168.122.243"|' "$CFG"
  else
    printf '\nREGISTRY_HOST_IP: "192.168.122.243"\n' >> "$CFG"
  fi
  sed -i 's|^local_path_provisioner_install:.*|local_path_provisioner_install: "yes"|' "$CFG"
  "$K" download -D </dev/null
  "$K" download -E local-path-provisioner </dev/null
  "$K" setup "$CLUSTER" 90 </dev/null
fi
export KUBECONFIG="$KC"
kubectl version --output=yaml >/dev/null
kubectl get nodes -o wide
kubectl get storageclass
kubectl get storageclass local-path -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}' \
  > /var/tmp/kubeauto-logging-storage-default.before
kubectl annotate storageclass local-path storageclass.kubernetes.io/is-default-class=true --overwrite >/dev/null
case_pass LOGGING-06 preflight
kubectl get crd prometheuses.monitoring.coreos.com >/dev/null
kubectl -n monitor get statefulset -l app.kubernetes.io/name=prometheus >/dev/null 2>&1 || true
case_pass LOGGING-07 prometheus-prerequisite
kubectl version --short >/dev/null 2>&1 || kubectl version >/dev/null
kubectl get nodes -o json | jq -e '[.items[] | select(.status.conditions[]? | select(.type=="Ready" and .status=="True"))] | length >= 6' >/dev/null
case_pass LOGGING-08 cluster-capability
# The disposable six-node topology reserves all control-plane nodes. Open one
# master only for this logging gate so Loki's three-replica required anti-
# affinity has three schedulable failure domains; production taints remain
# unchanged and are restored by logging-cleanup.sh.
kubectl taint node logging-master-243 node.kubernetes.io/unschedulable:NoSchedule- >/dev/null 2>&1 || true
echo LOGGING_STAGE_BEGIN artifact-upload
# Reuse any already verified official image layers on the disposable control
# host. This only creates local brinnatt tags; kubecli still performs the
# normal registry upload and digest verification. It never changes production
# defaults or CI source order.
declare -a bridge_images=(
  'docker.elastic.co/eck/eck-operator:3.5.0=brinnatt/eck-operator:3.5.0'
  'docker.elastic.co/elasticsearch/elasticsearch:9.5.1=brinnatt/elasticsearch:9.5.1'
  'docker.elastic.co/kibana/kibana:9.5.1=brinnatt/kibana:9.5.1'
  'cr.fluentbit.io/fluent/fluent-bit:5.1.1=brinnatt/fluent-bit:5.1.1'
  'docker.elastic.co/logstash/logstash:9.5.1=brinnatt/logstash:9.5.1'
  'docker.io/grafana/loki:3.7.6=brinnatt/loki:3.7.6'
  'docker.io/grafana/alloy:v1.18.1=brinnatt/alloy:1.18.1'
  'docker.io/nginxinc/nginx-unprivileged:1.31-alpine=brinnatt/loki-gateway:1.31-alpine'
  'ghcr.io/jkroepke/access-log-exporter:0.4.11=brinnatt/access-log-exporter:0.4.11'
  'docker.io/grafana/loki-canary:3.7.6=brinnatt/loki-canary:3.7.6'
  'docker.io/memcached:1.6.45-alpine=brinnatt/memcached:1.6.45-alpine'
  'docker.io/prom/memcached-exporter:v0.17.0=brinnatt/memcached-exporter:v0.17.0'
  'quay.io/kiwigrid/k8s-sidecar:2.10.1=brinnatt/k8s-sidecar:2.10.1'
  'quay.io/prometheus-operator/prometheus-config-reloader:v0.91.0=brinnatt/prometheus-config-reloader:v0.91.0'
  'quay.io/minio/minio:RELEASE.2025-04-08T15-41-24Z=brinnatt/minio:RELEASE.2025-04-08T15-41-24Z'
  'docker.io/minio/mc:RELEASE.2025-04-08T15-39-49Z=brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z'
  'docker.io/library/busybox:1.37=brinnatt/busybox:1.37'
  'registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.18.0=brinnatt/kube-state-metrics:v2.18.0'
  'registry.k8s.io/ingress-nginx/controller:v1.13.0=brinnatt/ingress-nginx-controller:v1.13.0'
  'registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.6.0=brinnatt/kube-webhook-certgen:v1.6.0'
  'ghcr.io/jkroepke/kube-webhook-certgen:1.8.5=brinnatt/prometheus-webhook-certgen:1.8.5'
  'quay.io/prometheus-operator/admission-webhook:v0.93.0=brinnatt/prometheus-admission-webhook:v0.93.0'
  'docker.io/grafana/grafana:13.1.1=brinnatt/grafana:13.1.1'
  'quay.io/kiwigrid/k8s-sidecar:1.30.5=brinnatt/k8s-sidecar:1.30.5'
  'quay.io/prometheus-operator/prometheus-config-reloader:v0.93.0=brinnatt/prometheus-config-reloader:v0.93.0'
  'quay.io/prometheus-operator/prometheus-operator:v0.93.0=brinnatt/prometheus-operator:v0.93.0'
  'quay.io/prometheus/alertmanager:v0.33.1=brinnatt/alertmanager:v0.33.1'
  'quay.io/prometheus/node-exporter:v1.12.1=brinnatt/node-exporter:v1.12.1'
  'quay.io/prometheus/prometheus:v3.13.1-distroless=brinnatt/prometheus:v3.13.1-distroless'
)
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  bridge_images+=(
    'quay.io/strimzi/operator:1.2.0=brinnatt/strimzi-operator:1.2.0'
    'quay.io/strimzi/kafka:1.2.0-kafka-4.3.1=brinnatt/strimzi-kafka:1.2.0-kafka-4.3.1'
    'quay.io/strimzi/drain-cleaner:1.6.1=brinnatt/strimzi-drain-cleaner:1.6.1'
  )
fi
if command -v docker >/dev/null 2>&1; then
  for mapping in "${bridge_images[@]}"; do
    upstream="${mapping%%=*}"; local_image="${mapping##*=}"
    if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer && "$upstream" == quay.io/strimzi/* ]] && ! docker image inspect "$upstream" >/dev/null 2>&1; then
      docker pull "$upstream" >/dev/null
    fi
    if docker image inspect "$upstream" >/dev/null 2>&1; then
      docker image inspect "$local_image" >/dev/null 2>&1 || docker tag "$upstream" "$local_image"
    fi
  done
  "$K" download -E logging-efk </dev/null
  "$K" download -E logging-loki </dev/null
  if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
    "$K" download -E kafka </dev/null
  fi
  "$K" download -E prometheus </dev/null
  "$K" download -E ingress-nginx </dev/null
  for image_tag in eck-operator:3.5.0 elasticsearch:9.5.1 kibana:9.5.1 fluent-bit:5.1.1; do
    image="${image_tag%%:*}"; tag="${image_tag##*:}"
    digest="$(curl -fsSI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
      "http://127.0.0.1:5000/v2/brinnatt/$image/manifests/$tag" \
      | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tr -d '\r' | tail -n1)"
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
  done
  for image_tag in \
    loki:3.7.6 alloy:1.18.1 loki-gateway:1.31-alpine access-log-exporter:0.4.11 \
    loki-canary:3.7.6 memcached:1.6.45-alpine memcached-exporter:v0.17.0 k8s-sidecar:2.10.1 \
    prometheus-config-reloader:v0.91.0 minio-mc:RELEASE.2025-04-08T15-39-49Z; do
    image="${image_tag%%:*}"; tag="${image_tag##*:}"
    digest="$(curl -fsSI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
      "http://127.0.0.1:5000/v2/brinnatt/$image/manifests/$tag" \
      | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tr -d '\r' | tail -n1)"
    [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
  done
else
  # Rebuilt control hosts may have containerd/nerdctl but no Docker. Pull the
  # fixed TalkEdu artifacts once, materialize them into a disposable local
  # registry, and point only this lab's nodes at it. Production image refs and
  # fallback order remain unchanged.
  command -v nerdctl >/dev/null || fail "no Docker or nerdctl available for runtime artifact bridge"
  nerdctl image inspect hub.talkedu.cn/kubeauto/registry:2.8.3 >/dev/null 2>&1 || \
    nerdctl pull hub.talkedu.cn/kubeauto/registry:2.8.3 >/dev/null
  nerdctl rm -f "$RUNTIME_REGISTRY_NAME" >/dev/null 2>&1 || true
  mkdir -p "$RUNTIME_REGISTRY_DATA"
  nerdctl run -d --name "$RUNTIME_REGISTRY_NAME" -p 5000:5000 \
    -v "$RUNTIME_REGISTRY_DATA:/var/lib/registry" \
    hub.talkedu.cn/kubeauto/registry:2.8.3 >/dev/null
  for mapping in "${bridge_images[@]}"; do
    local_image="${mapping##*=}"
    image_name="${local_image%%:*}"
    image_tag="${local_image##*:}"
    source_image="hub.talkedu.cn/kubeauto/${image_name#brinnatt/}:$image_tag"
    nerdctl pull "$source_image" >/dev/null
    nerdctl tag "$source_image" "127.0.0.1:5000/$local_image"
    nerdctl push --insecure-registry "127.0.0.1:5000/$local_image" >/dev/null
  done
  for ip in $(kubectl get nodes -o wide --no-headers | awk '{print $6}'); do
    ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@$ip" bash -s <<'NODE_REGISTRY'
set -Eeuo pipefail
cp -n /etc/hosts /var/tmp/kubeauto-logging-hosts.before
sed -i "/[[:space:]]registry\.talkschool\.cn\([[:space:]]\|$\)/d" /etc/hosts
printf '%s registry.talkschool.cn # kubeauto-logging-runtime\n' 192.168.122.243 >> /etc/hosts
config_dir='/etc/containerd/certs.d/registry.talkschool.cn:5000'
install -d -m 0755 "$config_dir"
if [[ ! -e "$config_dir/hosts.toml.before" && -e "$config_dir/hosts.toml" ]]; then
  cp "$config_dir/hosts.toml" "$config_dir/hosts.toml.before"
fi
touch "$config_dir/.kubeauto-logging-runtime"
cat >"$config_dir/hosts.toml" <<'EOF'
server = "http://registry.talkschool.cn:5000"
[host."http://registry.talkschool.cn:5000"]
  capabilities = ["pull", "resolve"]
EOF
NODE_REGISTRY
  done
  echo LOGGING_ARTIFACT_SOURCE_PASS hub.talkedu.cn/kubeauto+runtime-registry
fi
manifest_accept='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
for image_tag in ingress-nginx-controller:v1.13.0 kube-webhook-certgen:v1.6.0; do
  image="${image_tag%%:*}"
  tag="${image_tag##*:}"
  digest="$(curl -fsSI -H "Accept: $manifest_accept" \
    "http://127.0.0.1:5000/v2/brinnatt/$image/manifests/$tag" \
    | awk -F': ' 'tolower($1)=="docker-content-digest" {print $2}' | tr -d '\r' | tail -n1)"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
done
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  : > /var/tmp/kubeauto-kafka-crds-owned
fi
echo LOGGING_LOCAL_MANIFEST_DIGEST_PASS
echo LOGGING_ARTIFACT_UPLOAD_PASS
echo LOGGING_STAGE_BEGIN route-safety
test "${LOGGING_SOLUTION:?LOGGING_SOLUTION must be efk or loki}" = efk || test "$LOGGING_SOLUTION" = loki
if [[ "$LOGGING_SOLUTION" == efk ]]; then
  test "${LOGGING_EFK_DELIVERY:-direct}" = direct || test "${LOGGING_EFK_DELIVERY:-direct}" = kafka-buffer
fi
echo LOGGING_STATIC_PREFLIGHT_PASS solution="$LOGGING_SOLUTION"
echo LOGGING_STAGE_BEGIN product-entrypoint
# The live gate must exercise the documented kubeauto workflow.  These values
# are test-cluster inputs only and are written to the generated cluster config
# before setup 07; production defaults remain disabled in conf/config.yml.
CFG="$BASE/clusters/$CLUSTER/config.yml"
if grep -q '^REGISTRY_HOST_IP:' "$CFG"; then
  sed -i 's|^REGISTRY_HOST_IP:.*|REGISTRY_HOST_IP: "192.168.122.243"|' "$CFG"
else
  printf '\nREGISTRY_HOST_IP: "192.168.122.243"\n' >> "$CFG"
fi
python3 - "$CFG" "$LOGGING_SOLUTION" <<'PY'
import re
import sys
import os

path = sys.argv[1]
solution = sys.argv[2]
delivery = os.environ.get("LOGGING_EFK_DELIVERY", "direct")
text = open(path, encoding="utf-8").read()
values = {
    "prom_install": '"yes"',
    "prom_namespace": '"monitor"',
    "prom_storage_class": '"local-path"',
    "logging_install": '"yes"',
    "logging_solution": repr(solution),
    "logging_namespace": '"logging"',
    "logging_eck_ver": '"3.5.0"',
    "logging_elasticsearch_ver": '"9.5.1"',
    "logging_kibana_ver": '"9.5.1"',
    "logging_fluent_bit_ver": '"5.1.1"',
    "logging_loki_ver": '"3.7.6"',
    "logging_loki_chart_ver": '"18.9.0"',
    "logging_alloy_ver": '"1.18.1"',
    "logging_alloy_chart_ver": '"1.11.1"',
    "logging_efk_delivery": repr(delivery),
    "kafka_install": '"yes"' if delivery == "kafka-buffer" else '"no"',
    "kafka_storage_class": '"local-path"' if delivery == "kafka-buffer" else '""',
    # Kafka-buffer runs Kafka on the three workers. Keep the production ES
    # request/heap unchanged and use the three disposable control-plane
    # failure domains for this concurrent lab route.
    "logging_efk_es_nodes": '["logging-master-243", "logging-master-246", "logging-master-217"]' if delivery == "kafka-buffer" else '["logging-node-210", "logging-node-216", "logging-node-193"]',
    "logging_efk_storage_class": '"local-path"',
    "logging_efk_kibana_host": '"kibana.logging.test"',
    "logging_ingress_controller": '"ingress-nginx"',
    "logging_ingress_class": '"nginx"',
    "logging_ingress_tls_secret": '"logging-kibana-ingress-tls"',
    "logging_efk_snapshot_endpoint": '"http://minio.logging.svc:9000"',
    "logging_efk_snapshot_region": '"us-east-1"',
    "logging_efk_snapshot_bucket": '"logging-snapshots"',
    "logging_efk_snapshot_path_style": 'true',
    "logging_efk_snapshot_secret": '"logging-snapshot-s3"',
    "logging_efk_writer_secret": '"logging-efk-writer"',
    "logging_efk_writer_user": '"fluent-bit"',
    "logging_loki_storage_secret": '"logging-loki-storage"',
    "logging_loki_storage_ca_configmap": '"logging-loki-storage-ca"',
    "logging_loki_storage_class": '"local-path"',
    "logging_loki_storage_endpoint": '"http://minio.logging.svc:9000"',
    "logging_loki_memory_request": '"1Gi"',
    "logging_loki_memory_limit": '"2Gi"',
    "logging_loki_tolerate_control_plane": 'true',
    "logging_loki_storage_region": '"us-east-1"',
    "logging_loki_bucket_chunks": '"loki-chunks"',
    "logging_loki_bucket_ruler": '"loki-ruler"',
    "logging_loki_bucket_admin": '"loki-admin"',
    "logging_loki_gateway_auth_secret": '"logging-loki-gateway-auth"',
    "logging_loki_gateway_client_secret": '"logging-loki-gateway-client"',
    "logging_loki_ingress_host": '"loki.logging.test"',
    "ingress_nginx_install": '"yes"',
}
for key, value in values.items():
    pattern = rf"(?m)^{re.escape(key)}:.*$"
    replacement = f"{key}: {value}"
    if re.search(pattern, text):
        text = re.sub(pattern, replacement, text, count=1)
    else:
        text += "\n" + replacement + "\n"
open(path, "w", encoding="utf-8").write(text)
PY

# Fixtures are generated in the disposable logging namespace.  No credential
# is committed or printed; ECK and the product task consume Secret references.
kubectl create namespace logging --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if [[ "$LOGGING_SOLUTION" == efk ]]; then
  kubectl -n logging delete deployment/minio service/minio service/minio-console pod/logging-minio-mc \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl create deployment minio --image=registry.talkschool.cn:5000/brinnatt/minio:RELEASE.2025-04-08T15-41-24Z \
    -n logging --replicas=1 --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n logging set env deployment/minio MINIO_ROOT_USER=test-access MINIO_ROOT_PASSWORD=test-secret >/dev/null
  kubectl -n logging patch deployment minio --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args","value":["server","/data","--console-address",":9001"]}]' >/dev/null
  kubectl -n logging expose deployment minio --name=minio --port=9000 --target-port=9000 >/dev/null
  kubectl -n logging rollout status deployment/minio --timeout=180s
  # The object-store restart gate must exercise retained data, so the
  # disposable MinIO fixture uses a real PVC instead of an implicit emptyDir.
  kubectl -n logging apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: logging-minio-data
  labels:
    app.kubernetes.io/managed-by: kubeauto
    kubeauto.io/component: logging
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests: {storage: 10Gi}
YAML
  kubectl -n logging patch deployment minio --type=json -p='[
    {"op":"add","path":"/spec/template/spec/volumes","value":[{"name":"data","persistentVolumeClaim":{"claimName":"logging-minio-data"}}]},
    {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts","value":[{"name":"data","mountPath":"/data"}]}
  ]' >/dev/null
  kubectl -n logging rollout status deployment/minio --timeout=180s
  kubectl -n logging run logging-minio-mc \
    --image=registry.talkschool.cn:5000/brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z \
    --restart=Never --env=MINIO_ROOT_USER=test-access --env=MINIO_ROOT_PASSWORD=test-secret \
    --command -- sleep 3600 >/dev/null
  kubectl -n logging wait --for=jsonpath='{.status.phase}'=Running pod/logging-minio-mc --timeout=120s
  kubectl -n logging exec logging-minio-mc -- sh -eu -c '
    mc alias set gate http://minio.logging.svc:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" --api S3v4 --path on >/dev/null
    mc mb --ignore-existing gate/logging-snapshots >/dev/null
  '
  kubectl -n logging delete pod logging-minio-mc --wait=true >/dev/null
  kubectl -n logging create secret generic logging-snapshot-s3 \
    --from-literal='s3.client.default.access_key=test-access' \
    --from-literal='s3.client.default.secret_key=test-secret' \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n logging create secret generic logging-efk-writer \
    --from-literal=username=fluent-bit --from-literal=password="test-writer-password-change-me" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
else
  # Loki requires a reachable S3-compatible endpoint.  The product contract
  # consumes an existing object store; this disposable fixture supplies that
  # prerequisite without enabling the MinIO operator in the customer config.
  kubectl -n logging delete deployment/minio service/minio service/minio-console pod/logging-minio-mc \
    --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl create deployment minio --image=registry.talkschool.cn:5000/brinnatt/minio:RELEASE.2025-04-08T15-41-24Z \
    -n logging --replicas=1 --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n logging set env deployment/minio MINIO_ROOT_USER=test-access MINIO_ROOT_PASSWORD=test-secret >/dev/null
  kubectl -n logging patch deployment minio --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/args","value":["server","/data","--console-address",":9001"]}]' >/dev/null
  kubectl -n logging expose deployment minio --name=minio --port=9000 --target-port=9000 >/dev/null
  kubectl -n logging expose deployment minio --name=minio-console --port=9001 --target-port=9001 >/dev/null
  # Keep the object-store fixture durable across the restart/recovery gate.
  # Without a PVC, restarting this disposable deployment erases all buckets
  # and makes Loki fail its compactor startup with NoSuchBucket.
  kubectl -n logging apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: logging-minio-data
  labels:
    app.kubernetes.io/managed-by: kubeauto
    kubeauto.io/component: logging
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: local-path
  resources:
    requests: {storage: 10Gi}
YAML
  kubectl -n logging patch deployment minio --type=json -p='[
    {"op":"add","path":"/spec/template/spec/volumes","value":[{"name":"data","persistentVolumeClaim":{"claimName":"logging-minio-data"}}]},
    {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts","value":[{"name":"data","mountPath":"/data"}]}
  ]' >/dev/null
  kubectl -n logging rollout status deployment/minio --timeout=180s
  # S3 bucket creation must use AWS Signature V4.  A plain HTTP PUT is
  # intentionally rejected by MinIO (InvalidRequest), so use the pinned
  # minio-mc test-support artifact through the in-cluster service endpoint.
  kubectl -n logging delete pod logging-minio-mc --ignore-not-found --wait=true >/dev/null 2>&1 || true
  kubectl -n logging run logging-minio-mc \
    --image=registry.talkschool.cn:5000/brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z \
    --restart=Never --env=MINIO_ROOT_USER=test-access --env=MINIO_ROOT_PASSWORD=test-secret \
    --command -- sleep 3600 >/dev/null
  kubectl -n logging wait --for=jsonpath='{.status.phase}'=Running pod/logging-minio-mc --timeout=120s
  kubectl -n logging exec logging-minio-mc -- sh -eu -c '
    mc alias set gate http://minio.logging.svc:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" --api S3v4 --path on >/dev/null
    mc mb --ignore-existing gate/loki-chunks >/dev/null
    mc mb --ignore-existing gate/loki-ruler >/dev/null
    mc mb --ignore-existing gate/loki-admin >/dev/null
  '
  kubectl -n logging delete pod logging-minio-mc --wait=true >/dev/null
  kubectl -n logging create secret generic logging-loki-storage \
    --from-literal=LOKI_S3_ACCESS_KEY=test-access \
    --from-literal=LOKI_S3_SECRET_KEY=test-secret \
    --from-literal=LOKI_S3_ENDPOINT=http://minio.logging.svc:9000 \
    --from-literal=LOKI_S3_REGION=us-east-1 \
    --from-literal=LOKI_BUCKET_CHUNKS=loki-chunks \
    --from-literal=LOKI_BUCKET_RULER=loki-ruler \
    --from-literal=LOKI_BUCKET_ADMIN=loki-admin \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n logging create configmap logging-loki-storage-ca \
    --from-literal=ca.crt="" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  gateway_htpasswd="admin:$(openssl passwd -apr1 'test-gateway-password')"
  kubectl -n logging create secret generic logging-loki-gateway-auth \
    --from-literal=.htpasswd="$gateway_htpasswd" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n logging create secret generic logging-loki-gateway-client \
    --from-literal=LOKI_GATEWAY_USERNAME=admin --from-literal=LOKI_GATEWAY_PASSWORD="test-gateway-password" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi
if [[ "$LOGGING_SOLUTION" == efk ]]; then
  for node in logging-master-243 logging-master-246 logging-master-217; do
    kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -qx True
  done
  kubectl get storageclass local-path >/dev/null
  case_pass LOGGING-09 efk-target-nodes
  kubectl -n logging get secret logging-snapshot-s3 >/dev/null
  case_pass LOGGING-10 tls-and-s3-prerequisites
else
  kubectl -n logging get secret logging-loki-storage logging-loki-gateway-auth logging-loki-gateway-client >/dev/null
  case_pass LOGGING-11 loki-storage-and-auth-prerequisites
fi
tls_tmp="$(mktemp -d)"
trap 'rm -rf "$tls_tmp"' EXIT
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -keyout "$tls_tmp/tls.key" -out "$tls_tmp/tls.crt" \
  -subj '/CN=logging.test' -addext 'subjectAltName=DNS:kibana.logging.test,DNS:loki.logging.test' \
  >/dev/null 2>&1
kubectl -n logging create secret tls logging-kibana-ingress-tls \
  --cert="$tls_tmp/tls.crt" --key="$tls_tmp/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo LOGGING_STAGE_BEGIN kubeauto-addon
# This independent logging cluster may retain a previous Prometheus release
# from an interrupted gate. Remove only the test-owned release and its PVCs so
# Chart 88 is installed from a clean immutable StatefulSet boundary.
HELM="$BASE/extra-bin/helm"
$HELM uninstall prometheus --namespace monitor --wait --timeout 10m >/dev/null 2>&1 || true
kubectl -n monitor delete statefulset,deploy,daemonset,job --all --ignore-not-found --wait=true >/dev/null 2>&1 || true
kubectl -n monitor delete pvc --all --ignore-not-found --wait=true >/dev/null 2>&1 || true
# The Kafka-buffer lab needs three independent 8Gi ES consumers in addition
# to Kafka. Temporarily make the disposable control-plane nodes schedulable;
# logging-cleanup.sh restores their taints after every outcome.
if [[ "$LOGGING_SOLUTION" == efk && "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  for node in logging-master-243 logging-master-246 logging-master-217; do
    kubectl taint node "$node" node.kubernetes.io/unschedulable:NoSchedule- >/dev/null 2>&1 || true
    kubectl uncordon "$node" >/dev/null 2>&1 || true
  done
fi
"$K" setup "$CLUSTER" 07 </dev/null
# setup 07 reapplies control-plane NoSchedule taints; reopen the dedicated
# disposable logging failure domain immediately before the addon tasks run.
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  for node in logging-master-243 logging-master-246 logging-master-217; do
    kubectl taint node "$node" node.kubernetes.io/unschedulable:NoSchedule- >/dev/null 2>&1 || true
    kubectl uncordon "$node" >/dev/null 2>&1 || true
  done
else
  kubectl taint node logging-master-243 node.kubernetes.io/unschedulable:NoSchedule- >/dev/null 2>&1 || true
fi
kubectl get crd prometheuses.monitoring.coreos.com >/dev/null
if [[ "$LOGGING_SOLUTION" == loki ]]; then
  kubectl -n logging get statefulset -l app.kubernetes.io/component=single-binary -o json \
    | jq -e '[.items[].spec.replicas] | add == 3' >/dev/null
  kubectl -n logging get pvc -l app.kubernetes.io/component=single-binary \
    -o json | jq -e '[.items[] | select(.status.phase == "Bound")] | length == 3' >/dev/null
  kubectl -n logging get statefulset -l app.kubernetes.io/component=single-binary >/dev/null
  kubectl -n logging wait --for=condition=Ready pod -l app.kubernetes.io/component=single-binary --timeout=20m
  kubectl -n logging rollout status daemonset/alloy --timeout=20m
  kubectl -n logging get pods -o wide
  case_pass LOGGING-30 loki-replicas-storage
  gateway_pf="$(mktemp)"
  kubectl -n logging port-forward svc/loki-gateway 19100:80 >"$gateway_pf" 2>&1 & gateway_pid=$!
  cleanup_loki_forwarders() {
    if [[ -n "${gateway_pid:-}" ]]; then
      kill "$gateway_pid" >/dev/null 2>&1 || true
    fi
    if [[ -n "${grafana_pid:-}" ]]; then
      kill "$grafana_pid" >/dev/null 2>&1 || true
    fi
    if [[ -n "${ingress_pid:-}" ]]; then
      kill "$ingress_pid" >/dev/null 2>&1 || true
    fi
    rm -f "${gateway_pf:-}" "${grafana_pf:-}" "${ingress_pf:-}"
  }
  # Never use kill 0 here: it targets the entire process group, including
  # run-durable-gate, and would erase the durable exit record on a gate error.
  trap cleanup_loki_forwarders EXIT
  auth_user="$(kubectl -n logging get secret logging-loki-gateway-client -o jsonpath='{.data.LOKI_GATEWAY_USERNAME}' | base64 -d)"
  auth_password="$(kubectl -n logging get secret logging-loki-gateway-client -o jsonpath='{.data.LOKI_GATEWAY_PASSWORD}' | base64 -d)"
  gateway_ready_code=000
  for _ in $(seq 1 30); do
    gateway_ready_code="$(curl -sS -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' http://127.0.0.1:19100/loki/api/v1/status/buildinfo 2>/dev/null || echo 000)"
    [[ "$gateway_ready_code" == 200 ]] && break
    sleep 1
  done
  unauth_code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:19100/loki/api/v1/status/buildinfo)"
  auth_code="$(curl -sS -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' http://127.0.0.1:19100/loki/api/v1/status/buildinfo)"
  echo "LOGGING_LOKI_GATEWAY_CODES ready=$gateway_ready_code unauth=$unauth_code auth=$auth_code"
  [[ "$unauth_code" == 401 && "$auth_code" == 200 ]]
  loki_ingress_name="$(kubectl -n logging get ingress -l app.kubernetes.io/component=gateway -o jsonpath='{.items[0].metadata.name}')"
  [[ -n "$loki_ingress_name" ]]
  kubectl -n logging get ingress "$loki_ingress_name" -o json | jq -e '
    .spec.ingressClassName == "nginx" and
    .spec.rules[0].host == "loki.logging.test" and
    .spec.tls[0].secretName == "logging-kibana-ingress-tls"' >/dev/null
  kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=10m >/dev/null
  ingress_pf="$(mktemp)"
  kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 19443:443 >"$ingress_pf" 2>&1 &
  ingress_pid=$!
  ingress_auth_code=000
  for _ in $(seq 1 30); do
    ingress_auth_code="$(curl --connect-timeout 5 --max-time 20 -sS \
      --noproxy '*' \
      --resolve loki.logging.test:19443:127.0.0.1 --cacert "$tls_tmp/tls.crt" \
      -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' \
      https://loki.logging.test:19443/loki/api/v1/status/buildinfo 2>/dev/null || echo 000)"
    [[ "$ingress_auth_code" == 200 ]] && break
    sleep 1
  done
  ingress_unauth_code="$(curl --connect-timeout 5 --max-time 20 -sS \
    --noproxy '*' \
    --resolve loki.logging.test:19443:127.0.0.1 --cacert "$tls_tmp/tls.crt" \
    -o /dev/null -w '%{http_code}' https://loki.logging.test:19443/loki/api/v1/status/buildinfo)"
  echo "LOGGING_LOKI_INGRESS_TLS_CODES unauth=$ingress_unauth_code auth=$ingress_auth_code hostname=verified"
  [[ "$ingress_unauth_code" == 401 && "$ingress_auth_code" == 200 ]]
  case_pass LOGGING-31 gateway-basic-auth
  alloy_ready="$(kubectl -n logging get daemonset alloy -o jsonpath='{.status.numberReady}')"
  alloy_desired="$(kubectl -n logging get daemonset alloy -o jsonpath='{.status.desiredNumberScheduled}')"
  [[ "$alloy_ready" == "$alloy_desired" && "$alloy_ready" -ge 6 ]]
  kubectl get nodes -o name | while read -r node; do
    kubectl -n logging get pod -l app.kubernetes.io/name=alloy --field-selector="spec.nodeName=${node#node/}" -o name | grep -q .
  done
  case_pass LOGGING-32 alloy-positions
  echo LOGGING_STAGE_BEGIN loki-data-path
  loki_query_start="$(date -u -d '20 minutes ago' +%s)000000000"
  loki_query() {
    local selector="$1"
    local query_end="$(date -u +%s)000000000"
    curl --connect-timeout 5 --max-time 20 -fsS -u "$auth_user:$auth_password" -G \
      --data-urlencode "query=${selector}" \
      --data-urlencode 'limit=200' \
      --data-urlencode "start=${loki_query_start}" \
      --data-urlencode "end=${query_end}" \
      http://127.0.0.1:19100/loki/api/v1/query_range
  }
  loki_query_capture() {
    local selector="$1" output_file="$2"
    local query_end="$(date -u +%s)000000000"
    curl --connect-timeout 5 --max-time 20 -sS -u "$auth_user:$auth_password" -G \
      --data-urlencode "query=${selector}" \
      --data-urlencode 'limit=200' \
      --data-urlencode "start=${loki_query_start}" \
      --data-urlencode "end=${query_end}" \
      -o "$output_file" -w '%{http_code}' \
      http://127.0.0.1:19100/loki/api/v1/query_range
  }
  loki_wait_for_exact_marker() {
    local selector="$1" marker="$2" label="$3" attempts="$4"
    local response_file="/tmp/kubeauto-logging-${label}-response.json"
    local curl_error="/tmp/kubeauto-logging-${label}-curl.err"
    local attempt http curl_rc status count error
    for attempt in $(seq 1 "$attempts"); do
      http=000
      curl_rc=0
      : >"$response_file"
      : >"$curl_error"
      http="$(loki_query_capture "$selector" "$response_file" 2>"$curl_error")" || curl_rc=$?
      status=invalid-json
      count=0
      error=none
      if [[ -s "$response_file" ]] && jq -e . "$response_file" >/dev/null 2>&1; then
        status="$(jq -r '.status // "missing"' "$response_file")"
        count="$(jq -r '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' \
          --arg marker "$marker" "$response_file")"
        error="$(jq -r '.errorType // .error // "none"' "$response_file")"
      fi
      echo "$label attempt=${attempt}/${attempts} http=$http curl_rc=$curl_rc json_status=$status count=$count error=$error"
      if [[ "$http" == 200 && "$status" == success && "$count" == 1 ]]; then
        rm -f "$response_file" "$curl_error"
        return 0
      fi
      if [[ "$curl_rc" -ne 0 ]]; then
        # Loki rollouts can close an existing local forward after the Service
        # endpoint changes. Rebuild the diagnostic channel before retrying so
        # transport churn cannot be misclassified as a data-path failure.
        restart_loki_gateway_forward || true
      fi
      sleep 5
    done
    echo "${label}_DIAGNOSTIC response=$response_file curl_error=$curl_error" >&2
    return 1
  }
  restart_loki_gateway_forward() {
    if [[ -n "${gateway_pid:-}" ]]; then
      kill "$gateway_pid" >/dev/null 2>&1 || true
      wait "$gateway_pid" >/dev/null 2>&1 || true
    fi
    rm -f "${gateway_pf:-}"
    gateway_pf="$(mktemp)"
    kubectl -n logging port-forward svc/loki-gateway 19100:80 >"$gateway_pf" 2>&1 &
    gateway_pid=$!
    gateway_ready_code=000
    for _ in $(seq 1 60); do
      gateway_ready_code="$(curl --connect-timeout 5 --max-time 20 -sS \
        -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:19100/loki/api/v1/status/buildinfo 2>/dev/null || echo 000)"
      [[ "$gateway_ready_code" == 200 ]] && break
      sleep 1
    done
    [[ "$gateway_ready_code" == 200 ]]
  }
  if [[ -n "$LOGGING_FOCUS_CASE" ]]; then
    kubectl create namespace logging-smoke --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    run_extended_cases
    echo "LOGGING_FOCUSED_GATE_PASS case=$LOGGING_FOCUS_CASE"
    exit 0
  fi
  loki_marker="kubeauto-loki-stdout-$(date +%s)"
  kubectl create namespace logging-smoke --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n logging-smoke run "$loki_marker" \
    --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
    --restart=Never --labels=app.kubernetes.io/managed-by=kubeauto \
    -- sh -c "echo $loki_marker" >/dev/null
  kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded pod/$loki_marker --timeout=120s
  kubectl -n logging-smoke logs "$loki_marker" | grep -Fx "$loki_marker"
  loki_log_count=0
  for attempt in $(seq 1 60); do
    loki_query_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${loki_marker}\"}" 2>/dev/null || true)"
    loki_log_count="$(jq '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' --arg marker "$loki_marker" <<<"$loki_query_response" 2>/dev/null || echo 0)"
    echo "LOGGING_LOKI_QUERY_WAIT attempt=${attempt}/60 marker=${loki_marker} matches=${loki_log_count}"
    [[ "$loki_log_count" =~ ^[0-9]+$ && "$loki_log_count" -gt 0 ]] && break
    sleep 5
  done
  [[ "$loki_log_count" =~ ^[0-9]+$ && "$loki_log_count" -gt 0 ]]
  case_pass LOGGING-33 stdout-alloy-gateway-loki-logql

  echo LOGGING_STAGE_BEGIN grafana-loki-datasource
  # The lab kube-proxy exposes Grafana on a NodePort bound to the node's
  # InternalIP (not loopback). Discover that product endpoint instead of
  # depending on a fixed local port or a leftover port-forward child.
  grafana_node_ip="$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"
  grafana_node_port="$(kubectl -n monitor get svc prometheus-grafana -o jsonpath='{.spec.ports[?(@.name=="http-web")].nodePort}')"
  [[ "$grafana_node_ip" =~ ^[0-9.]+$ && "$grafana_node_port" =~ ^[0-9]+$ ]]
  grafana_url="http://${grafana_node_ip}:${grafana_node_port}"
  grafana_code=000
  for _ in $(seq 1 60); do
    grafana_code="$(curl -ksS -o /dev/null -w '%{http_code}' "${grafana_url}/api/health" || true)"
    [[ "$grafana_code" == 200 ]] && break
    sleep 2
  done
  [[ "$grafana_code" == 200 ]]
  grafana_user="$(kubectl -n monitor get secret grafana-admin -o jsonpath='{.data.admin-user}' | base64 -d)"
  grafana_password="$(kubectl -n monitor get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)"
  grafana_auth=(curl -ksS -u "$grafana_user:$grafana_password")
  grafana_ds_name="kubeauto-loki-gate"
  grafana_ds_uid="kubeauto-loki-gate"
  "${grafana_auth[@]}" -X DELETE "${grafana_url}/api/datasources/uid/${grafana_ds_uid}" >/dev/null 2>&1 || true
  grafana_ds_body="$(jq -cn \
    --arg name "$grafana_ds_name" --arg uid "$grafana_ds_uid" \
    --arg url "http://loki-gateway.${LOGGING_NAMESPACE:-logging}.svc.cluster.local" \
    --arg user "$auth_user" --arg password "$auth_password" \
    '{name:$name,uid:$uid,type:"loki",access:"proxy",url:$url,basicAuth:true,basicAuthUser:$user,secureJsonData:{basicAuthPassword:$password},isDefault:false}')"
  grafana_ds_http="$("${grafana_auth[@]}" -H 'content-type: application/json' -o /tmp/kubeauto-grafana-loki-ds.json -w '%{http_code}' \
    -X POST "${grafana_url}/api/datasources" -d "$grafana_ds_body")"
  [[ "$grafana_ds_http" == 200 ]]
  grafana_health_http="$("${grafana_auth[@]}" -o /tmp/kubeauto-grafana-loki-health.json -w '%{http_code}' \
    "${grafana_url}/api/datasources/uid/${grafana_ds_uid}/health")"
  echo "LOGGING_GRAFANA_HEALTH_HTTP=${grafana_health_http}"
  [[ "$grafana_health_http" == 200 ]] || { cat /tmp/kubeauto-grafana-loki-health.json >&2; false; }
  jq -e '.status == "OK" or .status == "success"' /tmp/kubeauto-grafana-loki-health.json >/dev/null || {
    cat /tmp/kubeauto-grafana-loki-health.json >&2
    false
  }
  grafana_dashboard_uid="kubeauto-loki-gate-dashboard"
  grafana_logql="{namespace=\"logging-smoke\",pod=\"${loki_marker}\"}"
  grafana_dashboard_body="$(jq -cn --arg uid "$grafana_dashboard_uid" --arg ds "$grafana_ds_uid" --arg expr "$grafana_logql" \
    '{dashboard:{uid:$uid,title:"Kubeauto Loki Gate",panels:[{id:1,type:"logs",title:"Loki gate",datasource:{type:"loki",uid:$ds},targets:[{expr:$expr}]}]},overwrite:true}')"
  grafana_dash_http="$("${grafana_auth[@]}" -H 'content-type: application/json' -o /tmp/kubeauto-grafana-loki-dashboard.json -w '%{http_code}' \
    -X POST "${grafana_url}/api/dashboards/db" -d "$grafana_dashboard_body")"
  [[ "$grafana_dash_http" == 200 ]]
  "${grafana_auth[@]}" -fsS "${grafana_url}/api/dashboards/uid/${grafana_dashboard_uid}" \
    | jq -e '.dashboard.uid == "kubeauto-loki-gate-dashboard"' >/dev/null
  "${grafana_auth[@]}" -X DELETE "${grafana_url}/api/dashboards/uid/${grafana_dashboard_uid}" >/dev/null
  "${grafana_auth[@]}" -X DELETE "${grafana_url}/api/datasources/uid/${grafana_ds_uid}" >/dev/null
  case_pass LOGGING-34 grafana-loki-save-test-dashboard-query

  echo LOGGING_STAGE_BEGIN loki-observability
  prom_name="$(kubectl -n monitor get prometheus -o json \
    | jq -r '.items | sort_by(.metadata.name) | .[0].metadata.name // empty')"
  [[ -n "$prom_name" ]] || fail "Prometheus CR not found in namespace monitor"
  expected_prom_replicas="$(kubectl -n monitor get prometheus "$prom_name" -o jsonpath='{.spec.replicas}')"
  mapfile -t prom_pods < <(
    kubectl -n monitor get pod -l app.kubernetes.io/name=prometheus -o json \
      | jq -r '.items[] | select(.metadata.deletionTimestamp == null) | .metadata.name' | sort
  )
  [[ "$expected_prom_replicas" -gt 0 && "${#prom_pods[@]}" -eq "$expected_prom_replicas" ]]
  for prom_pod in "${prom_pods[@]}"; do
    kubectl -n monitor wait --for=condition=Ready "pod/$prom_pod" --timeout=5m >/dev/null
  done
  loki_prom_targets_ready=0
  for attempt in $(seq 1 24); do
    loki_prom_targets_ready=1
    for prom_pod in "${prom_pods[@]}"; do
      loki_targets_file="/tmp/kubeauto-logging-${prom_pod}-loki-targets.json"
      if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/targets" >"$loki_targets_file" 2>/dev/null \
        || ! jq -e '[.data.activeTargets[] | select(.labels.namespace == "logging" and .health == "up" and (((.labels.job // "") + " " + (.labels.service // "")) | test("loki|alloy"; "i")))] | length >= 2' "$loki_targets_file" >/dev/null; then
        loki_prom_targets_ready=0
      fi
    done
    echo "LOGGING_LOKI_TARGET_WAIT attempt=${attempt}/24 replicas=${#prom_pods[@]} ready=${loki_prom_targets_ready}"
    [[ "$loki_prom_targets_ready" == 1 ]] && break
    sleep 15
  done
  [[ "$loki_prom_targets_ready" == 1 ]]
  loki_rules_ready=0
  for attempt in $(seq 1 24); do
    loki_rules_ready=1
    for prom_pod in "${prom_pods[@]}"; do
      loki_rules_file="/tmp/kubeauto-logging-${prom_pod}-loki-rules.json"
      if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/rules" >"$loki_rules_file" 2>/dev/null \
        || ! jq -e '[.data.groups[] | select((.name // "") | test("loki|alloy"; "i")) | .rules[]] | length >= 1 and all(.[]; .health == "ok" and (.lastError // "") == "")' "$loki_rules_file" >/dev/null; then
        loki_rules_ready=0
      fi
    done
    echo "LOGGING_LOKI_RULE_WAIT attempt=${attempt}/24 replicas=${#prom_pods[@]} ready=${loki_rules_ready}"
    [[ "$loki_rules_ready" == 1 ]] && break
    sleep 15
  done
  [[ "$loki_rules_ready" == 1 ]]
  kubectl -n logging apply -f - <<'YAML'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: kubeauto-loki-gate
  namespace: logging
  labels:
    release: prometheus
    app.kubernetes.io/managed-by: kubeauto
    kubeauto.io/component: logging
spec:
  groups:
    - name: kubeauto-loki-gate
      rules:
        - alert: KubeautoLokiGate
          expr: vector(1)
          for: 0s
          labels: {severity: warning}
          annotations: {summary: kubeauto-loki-gate}
YAML
  loki_alert_firing=0
  for attempt in $(seq 1 24); do
    loki_alert_firing=1
    for prom_pod in "${prom_pods[@]}"; do
      alerts_file="/tmp/kubeauto-logging-${prom_pod}-loki-alerts.json"
      if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/alerts" >"$alerts_file" 2>/dev/null \
        || ! jq -e '[.data.alerts[] | select(.labels.alertname == "KubeautoLokiGate" and .state == "firing")] | length == 1' "$alerts_file" >/dev/null; then
        loki_alert_firing=0
      fi
    done
    [[ "$loki_alert_firing" == 1 ]] && break
    sleep 5
  done
  [[ "$loki_alert_firing" == 1 ]]
  kubectl -n logging patch prometheusrule kubeauto-loki-gate --type=json \
    -p='[{"op":"replace","path":"/spec/groups/0/rules/0/expr","value":"vector(0) > 1"}]' >/dev/null
  loki_alert_resolved=0
  for attempt in $(seq 1 24); do
    loki_alert_resolved=1
    for prom_pod in "${prom_pods[@]}"; do
      if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/alerts" 2>/dev/null \
        | jq -e '[.data.alerts[] | select(.labels.alertname == "KubeautoLokiGate")] | length == 0' >/dev/null; then
        loki_alert_resolved=0
      fi
    done
    [[ "$loki_alert_resolved" == 1 ]] && break
    sleep 5
  done
  [[ "$loki_alert_resolved" == 1 ]]
  kubectl -n logging delete prometheusrule kubeauto-loki-gate --ignore-not-found >/dev/null
  case_pass LOGGING-35 loki-alloy-targets-rules-alert-recovery

  echo LOGGING_STAGE_BEGIN loki-failure-recovery
  loki_member_pod="$(kubectl -n logging get pod -l app.kubernetes.io/component=single-binary -o jsonpath='{.items[0].metadata.name}')"
  kubectl -n logging delete pod "$loki_member_pod" --wait=false >/dev/null
  kubectl -n logging rollout status statefulset/loki --timeout=15m >/dev/null
  loki_ready_replicas="$(kubectl -n logging get statefulset loki -o jsonpath='{.status.readyReplicas}')"
  [[ "${loki_ready_replicas:-0}" -ge 3 ]]
  gateway_deployment="$(kubectl -n logging get deployment -o name 2>/dev/null \
    | sed 's#deployment.apps/##' | grep '^loki-gateway' | head -n1 || true)"
  [[ -n "$gateway_deployment" ]]
  echo "LOGGING_RECOVERY_GATEWAY_DEPLOYMENT name=$gateway_deployment"
  kubectl -n logging rollout restart "deployment/$gateway_deployment" >/dev/null
  # Wait on the owner Deployment so a deleted Pod cannot make kubectl wait
  # select a stale name and fail with NotFound during replacement.
  gateway_ready_code=000
  for attempt in $(seq 1 60); do
    gateway_ready_code="$(curl -sS -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' http://127.0.0.1:19100/loki/api/v1/status/buildinfo 2>/dev/null || echo 000)"
    [[ "$gateway_ready_code" == 200 ]] && break
    sleep 5
  done
  [[ "$gateway_ready_code" == 200 ]]
  # Deleting a Gateway Pod can tear down the existing port-forward even when
  # the Service and its replacement Pod are healthy. Recreate the diagnostic
  # channel before asserting post-recovery LogQL continuity.
  old_gateway_pid="${gateway_pid:-}"
  if [[ -n "$old_gateway_pid" ]]; then
    kill "$old_gateway_pid" >/dev/null 2>&1 || true
    wait "$old_gateway_pid" >/dev/null 2>&1 || true
  fi
  gateway_pf="$(mktemp)"
  kubectl -n logging port-forward svc/loki-gateway 19100:80 >"$gateway_pf" 2>&1 & gateway_pid=$!
  gateway_ready_code=000
  for _ in $(seq 1 60); do
    gateway_ready_code="$(curl --connect-timeout 5 --max-time 20 -sS -u "$auth_user:$auth_password" -o /dev/null -w '%{http_code}' http://127.0.0.1:19100/loki/api/v1/status/buildinfo 2>/dev/null || echo 000)"
    [[ "$gateway_ready_code" == 200 ]] && break
    sleep 1
  done
  [[ "$gateway_ready_code" == 200 ]]
  echo "LOGGING_RECOVERY_GATEWAY_READY code=$gateway_ready_code deployment=$gateway_deployment"
  alloy_recovery_pod="$(kubectl -n logging get pod -l app.kubernetes.io/name=alloy -o jsonpath='{.items[0].metadata.name}')"
  kubectl -n logging delete pod "$alloy_recovery_pod" --wait=false >/dev/null
  kubectl -n logging rollout status daemonset/alloy --timeout=10m >/dev/null
  echo LOGGING_RECOVERY_ALLOY_ROLLOUT_PASS
  loki_query_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${loki_marker}\"}" 2>/dev/null || true)"
  loki_log_count_after_failure="$(jq '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' --arg marker "$loki_marker" <<<"$loki_query_response")"
  echo "LOGGING_RECOVERY_FIRST_QUERY count=$loki_log_count_after_failure"
  [[ "$loki_log_count_after_failure" =~ ^[0-9]+$ && "$loki_log_count_after_failure" -gt 0 ]]
  second_marker="${loki_marker}-after-recovery"
  kubectl -n logging-smoke run "$second_marker" --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
    --restart=Never -- sh -c "echo $second_marker" >/dev/null
  kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded pod/$second_marker --timeout=120s
  for attempt in $(seq 1 36); do
    loki_query_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${second_marker}\"}" 2>/dev/null || true)"
    second_count="$(jq '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' --arg marker "$second_marker" <<<"$loki_query_response" 2>/dev/null || echo 0)"
    echo "LOGGING_RECOVERY_SECOND_QUERY attempt=${attempt}/36 count=$second_count"
    [[ "$second_count" =~ ^[0-9]+$ && "$second_count" -gt 0 ]] && break
    sleep 5
  done
  [[ "$second_count" =~ ^[0-9]+$ && "$second_count" -gt 0 ]]
  case_pass LOGGING-36 loki-member-gateway-alloy-recovery-positions

  echo LOGGING_STAGE_BEGIN loki-object-storage-recovery
  kubectl -n logging rollout restart deployment/minio >/dev/null
  kubectl -n logging rollout status deployment/minio --timeout=10m >/dev/null
  retained_count=0
  for attempt in $(seq 1 36); do
    loki_query_response="$(loki_query "{namespace=\"logging-smoke\",pod=\"${loki_marker}\"}" 2>/dev/null || true)"
    retained_count="$(jq '[.data.result[]?.values[]? | select(.[1] | contains($marker))] | length' --arg marker "$loki_marker" <<<"$loki_query_response" 2>/dev/null || echo 0)"
    [[ "$retained_count" =~ ^[0-9]+$ && "$retained_count" -gt 0 ]] && break
    sleep 5
  done
  [[ "$retained_count" =~ ^[0-9]+$ && "$retained_count" -gt 0 ]]
  case_pass LOGGING-37 object-storage-restart-retained-readability
  run_extended_cases
  if [[ -n "$LOGGING_FOCUS_CASE" ]]; then
    echo "LOGGING_FOCUSED_GATE_PASS case=$LOGGING_FOCUS_CASE"
  else
    echo LOGGING_FULL_GATE_PASS
  fi
  exit 0
fi
kubectl -n logging get elasticsearches logging >/dev/null
kubectl -n logging get kibanas logging >/dev/null
kubectl -n logging wait --for=jsonpath='{.status.health}'=green elasticsearches/logging --timeout=20m
kubectl -n logging wait --for=jsonpath='{.status.health}'=green kibanas/logging --timeout=20m
kubectl -n elastic-system rollout status statefulset/elastic-operator --timeout=300s
kubectl -n logging get pod -l common.k8s.elastic.co/type=elasticsearch -o json \
  | jq -e '[.items[] | select(.status.phase == "Running")] | length == 3' >/dev/null
kubectl -n logging get pvc -l common.k8s.elastic.co/type=elasticsearch -o json \
  | jq -e '[.items[] | select(.status.phase == "Bound")] | length == 3' >/dev/null
case_pass LOGGING-12 eck-operator-and-crds
case_pass LOGGING-13 elasticsearch-three-node-tls-pvc
kubectl -n logging get ingress logging-kibana -o json \
  | jq -e '.spec.tls | length == 1 and .[0].secretName == "logging-kibana-ingress-tls"' >/dev/null
kubectl -n logging get service logging-kb-http >/dev/null
case_pass LOGGING-14 kibana-https-ingress
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  kubectl -n logging rollout status daemonset/fluent-bit-kafka --timeout=20m
else
  kubectl -n logging rollout status daemonset/fluent-bit --timeout=20m
fi
kubectl -n logging get pods -o wide
echo LOGGING_STAGE_BEGIN efk-data-smoke
smoke_marker="logging-smoke-$(date +%s)"
kubectl create namespace logging-smoke --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n logging-smoke run "$smoke_marker" --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
  --restart=Never -- sh -c "echo $smoke_marker" >/dev/null
kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded pod/$smoke_marker --timeout=120s
kubectl -n logging-smoke logs "$smoke_marker" | grep -Fx "$smoke_marker"
es_tmp="$(mktemp -d)"
pf_pid=
config_restore_file=
stop_es_forward() {
  if [[ -n "${pf_pid:-}" ]]; then
    kill "$pf_pid" >/dev/null 2>&1 || true
    wait "$pf_pid" >/dev/null 2>&1 || true
    pf_pid=
  fi
}
cleanup_efk_gate() {
  stop_es_forward
  if [[ -n "${config_restore_file:-}" && -f "$config_restore_file" ]]; then
    cp "$config_restore_file" "$CFG"
  fi
  rm -f "${config_restore_file:-}"
  rm -rf "$tls_tmp" "$es_tmp"
  kubectl delete namespace logging-smoke --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup_efk_gate EXIT
kubectl -n logging get secret logging-es-http-certs-public -o jsonpath='{.data.ca\.crt}' | base64 -d >"$es_tmp/ca.crt"
es_password="$(kubectl -n logging get secret logging-es-elastic-user -o jsonpath='{.data.elastic}' | base64 -d)"
es_tls_name="logging-es-http.logging.svc"
es_curl=(curl --resolve "${es_tls_name}:19200:127.0.0.1" --cacert "$es_tmp/ca.crt" -u "elastic:$es_password")
start_es_forward() {
  stop_es_forward
  kubectl -n logging port-forward svc/logging-es-http 19200:9200 >"$es_tmp/pf.log" 2>&1 &
  pf_pid=$!
  for _ in $(seq 1 30); do
    "${es_curl[@]}" -fsS "https://${es_tls_name}:19200" >/dev/null 2>&1 && return 0
    kill -0 "$pf_pid" 2>/dev/null
    sleep 1
  done
  return 1
}
start_es_forward
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200" >/dev/null
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_cluster/health" \
  | jq -e '.number_of_nodes == 3 and .status == "green"' >/dev/null
case_pass LOGGING-15 controlled-api-channel
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_ilm/policy/k8s-retention" | grep -q 'k8s-retention'
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_index_template/k8s-logs" | grep -q 'k8s-logs'
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_snapshot/logging-s3" | grep -q 'logging-s3'
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_slm/policy/logging-daily" | grep -q 'logging-daily'
kubectl -n logging get servicemonitor logging-fluent-bit >/dev/null
echo LOGGING_EFK_POLICY_MONITORING_PASS
case_pass LOGGING-16 ilm-template-shards-writer
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  kubectl -n logging get daemonset fluent-bit-kafka >/dev/null
  kubectl -n logging rollout status deployment/logstash --timeout=20m
  kubectl -n logging get deployment logstash -o json | jq -e '.status.readyReplicas == 2' >/dev/null
  kubectl -n kafka get kafkauser,kafkatopic >/dev/null
  kubectl -n logging get configmap fluent-bit-kafka-config fluent-bit-kafka-logstash >/dev/null
  case_pass LOGGING-26 kafka-tls-scram-topic-acl
  kubectl -n logging get daemonset fluent-bit-kafka -o json \
    | jq -e '.spec.template.spec.containers[0].args | join(" ") | contains("fluent-bit.conf")' >/dev/null
  ! kubectl -n logging get configmap fluent-bit-kafka-config -o json | grep -q 'Name es'
  case_pass LOGGING-27 kafka-no-direct-dual-write
  kubectl -n logging get deployment logstash -o json \
    | jq -e '.spec.replicas == 2 and .status.readyReplicas == 2' >/dev/null
  case_pass LOGGING-28 logstash-consumer-group
fi
for _ in $(seq 1 60); do
  if "${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_search?q=$smoke_marker" 2>/dev/null | grep -q "$smoke_marker"; then
    echo LOGGING_EFK_DATA_PATH_PASS
    break
  fi
  sleep 5
done
"${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_search?q=$smoke_marker" 2>/dev/null | grep -q "$smoke_marker"
case_pass LOGGING-17 fluent-bit-daemonset-cri-buffer
case_pass LOGGING-18 stdout-cri-elasticsearch-kibana-query
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  kubectl -n logging scale deployment/logstash --replicas=0 >/dev/null
  kubectl -n logging-smoke run "${smoke_marker}-lag" --image=registry.talkschool.cn:5000/brinnatt/busybox:1.37 \
    --restart=Never -- sh -c "for i in \$(seq 1 20); do echo ${smoke_marker}-lag; done" >/dev/null
  kubectl -n logging-smoke wait --for=jsonpath='{.status.phase}'=Succeeded pod/${smoke_marker}-lag --timeout=120s
  kubectl -n logging scale deployment/logstash --replicas=2 >/dev/null
  kubectl -n logging rollout status deployment/logstash --timeout=10m
  for _ in $(seq 1 60); do
    if "${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_search?q=${smoke_marker}-lag" 2>/dev/null | grep -q "${smoke_marker}-lag"; then
      break
    fi
    sleep 5
  done
  "${es_curl[@]}" -fsS "https://${es_tls_name}:19200/_search?q=${smoke_marker}-lag" 2>/dev/null | grep -q "${smoke_marker}-lag"
  case_pass LOGGING-29 kafka-lag-recovery
fi
echo LOGGING_STAGE_BEGIN kibana-and-observability
kibana_pf="$(mktemp)"
kubectl -n logging port-forward svc/logging-kb-http 19201:5601 >"$kibana_pf" 2>&1 & kibana_pid=$!
for _ in $(seq 1 60); do
  kibana_code="$(curl -ksS -o /dev/null -w '%{http_code}' https://127.0.0.1:19201/api/status || true)"
  [[ "$kibana_code" == 200 ]] && break
  sleep 2
done
kibana_admin="$(kubectl -n logging get secret logging-es-elastic-user -o jsonpath='{.data.elastic}' | base64 -d)"
[[ "$kibana_code" == 200 && -n "$kibana_admin" ]]
kibana_saved_code="$(curl -ksS -u "elastic:$kibana_admin" -H 'kbn-xsrf: true' \
  -o /tmp/kubeauto-kibana-saved.json -w '%{http_code}' \
  https://127.0.0.1:19201/api/saved_objects/_find?type=dashboard || true)"
[[ "$kibana_saved_code" == 200 ]]
case_pass LOGGING-19 kibana-api-saved-objects
prom_name="$(kubectl -n monitor get prometheus -o json \
  | jq -r '.items | sort_by(.metadata.name) | .[0].metadata.name // empty')"
[[ -n "$prom_name" ]] || fail "Prometheus CR not found in namespace monitor"
expected_prom_replicas="$(kubectl -n monitor get prometheus "$prom_name" -o jsonpath='{.spec.replicas}')"
mapfile -t prom_pods < <(
  kubectl -n monitor get pod -l app.kubernetes.io/name=prometheus -o json \
    | jq -r '.items[] | select(.metadata.deletionTimestamp == null) | .metadata.name' | sort
)
[[ "$expected_prom_replicas" -gt 0 && "${#prom_pods[@]}" -eq "$expected_prom_replicas" ]]
for prom_pod in "${prom_pods[@]}"; do
  kubectl -n monitor wait --for=condition=Ready "pod/$prom_pod" --timeout=5m
done
collector_service="fluent-bit"
[[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]] && collector_service=fluent-bit-kafka
expected_targets="$(kubectl -n logging get daemonset "$collector_service" -o jsonpath='{.status.desiredNumberScheduled}')"
[[ "$expected_targets" -gt 0 ]]
targets_ready=0
for attempt in $(seq 1 24); do
  targets_ready=1
  for prom_pod in "${prom_pods[@]}"; do
    targets_file="/tmp/kubeauto-logging-${prom_pod}-targets.json"
    if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/targets" >"$targets_file" 2>/dev/null \
      || ! jq -e --arg service "$collector_service" --argjson expected "$expected_targets" \
        '[.data.activeTargets[] | select(.labels.namespace == "logging" and .labels.service == $service and .health == "up")] | length == $expected' \
        "$targets_file" >/dev/null; then
      targets_ready=0
    fi
    for metric in fluentbit_output_errors_total fluentbit_output_retries_failed_total; do
      query="count(count by (pod) (${metric}{namespace=\"logging\",service=\"${collector_service}\"}))"
      encoded_query="$(jq -nr --arg query "$query" '$query | @uri')"
      query_file="/tmp/kubeauto-logging-${prom_pod}-${metric}.json"
      if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/query?query=${encoded_query}" >"$query_file" 2>/dev/null \
        || ! jq -e --argjson expected "$expected_targets" \
          '.status == "success" and .data.resultType == "vector" and (.data.result | length) == 1 and (.data.result[0].value[1] | tonumber) == $expected' \
          "$query_file" >/dev/null; then
        targets_ready=0
      fi
    done
  done
  echo "LOGGING_PROMETHEUS_TARGET_WAIT attempt=${attempt}/24 replicas=${#prom_pods[@]} expected_targets=${expected_targets} ready=${targets_ready}"
  [[ "$targets_ready" == 1 ]] && break
  sleep 15
done
[[ "$targets_ready" == 1 ]]
case_pass LOGGING-20 prometheus-targets-and-metrics-up
rules_ready=0
for attempt in $(seq 1 24); do
  rules_ready=1
  for prom_pod in "${prom_pods[@]}"; do
    rules_file="/tmp/kubeauto-logging-${prom_pod}-rules.json"
    if ! kubectl get --raw "/api/v1/namespaces/monitor/pods/${prom_pod}:9090/proxy/api/v1/rules" >"$rules_file" 2>/dev/null \
      || ! jq -e '
        ([.data.groups[] | select(.name == "fluent-bit.rules") | .rules[].name] | sort) ==
          (["FluentBitOutputErrors", "FluentBitRetriesFailed", "FluentBitTargetDown"] | sort) and
        all(.data.groups[] | select(.name == "fluent-bit.rules") | .rules[];
          .health == "ok" and (.lastError // "") == "")
      ' "$rules_file" >/dev/null; then
      rules_ready=0
    fi
  done
  echo "LOGGING_PROMETHEUS_RULE_WAIT attempt=${attempt}/24 replicas=${#prom_pods[@]} ready=${rules_ready}"
  [[ "$rules_ready" == 1 ]] && break
  sleep 15
done
[[ "$rules_ready" == 1 ]]
case_pass LOGGING-21 prometheus-rules-all-replicas-healthy
echo LOGGING_STAGE_BEGIN snapshot-restore
snapshot_name="logging-${smoke_marker}"
snapshot_response_file="/tmp/kubeauto-logging-${snapshot_name}-snapshot.json"
"${es_curl[@]}" -fsS -H 'content-type: application/json' \
  -X POST "https://${es_tls_name}:19200/_snapshot/logging-s3/${snapshot_name}?wait_for_completion=true" \
  -d '{"indices":"k8s-*","include_global_state":false}' >"$snapshot_response_file"
jq -e '.snapshot.state == "SUCCESS" and .snapshot.shards.failed == 0' "$snapshot_response_file" >/dev/null || {
  echo "LOGGING_SNAPSHOT_RESPONSE=$(cat "$snapshot_response_file")" >&2
  false
}
case_pass LOGGING-22 slm-snapshot-success
restore_name="logging-restore-${smoke_marker}"
restore_response_file="/tmp/kubeauto-logging-${restore_name}-restore.json"
restore_replacement="${restore_name}-\$1"
restore_body="$(jq -cn --arg replacement "$restore_replacement" \
  '{indices:"k8s-*",include_global_state:false,rename_pattern:"(.+)",rename_replacement:$replacement,include_aliases:false}')"
restore_http_code="$("${es_curl[@]}" -sS -o "$restore_response_file" -w '%{http_code}' \
  -H 'content-type: application/json' \
  -X POST "https://${es_tls_name}:19200/_snapshot/logging-s3/${snapshot_name}/_restore?wait_for_completion=true" \
  -d "$restore_body" || true)"
if [[ ! "$restore_http_code" =~ ^2[0-9][0-9]$ ]]; then
  echo "LOGGING_RESTORE_HTTP_CODE=$restore_http_code" >&2
  echo "LOGGING_RESTORE_RESPONSE=$(cat "$restore_response_file")" >&2
  false
fi
restore_count=0
restore_count_response_file="/tmp/kubeauto-logging-${restore_name}-count.json"
for _ in $(seq 1 60); do
  if "${es_curl[@]}" -fsS "https://${es_tls_name}:19200/${restore_name}-*/_count" >"$restore_count_response_file" 2>/dev/null; then
    restore_count="$(jq -r '.count // 0' "$restore_count_response_file" 2>/dev/null || echo 0)"
  else
    restore_count=0
  fi
  [[ "$restore_count" =~ ^[0-9]+$ && "$restore_count" -gt 0 ]] && break
  sleep 5
done
if ! [[ "$restore_count" =~ ^[0-9]+$ && "$restore_count" -gt 0 ]]; then
  echo "LOGGING_RESTORE_COUNT_RESPONSE=$(cat "$restore_count_response_file" 2>/dev/null || true)" >&2
  echo "LOGGING_RESTORE_INDICES=$("${es_curl[@]}" -sS "https://${es_tls_name}:19200/_cat/indices/${restore_name}-*?format=json" 2>/dev/null || true)" >&2
  echo "LOGGING_RESTORE_RECOVERY=$("${es_curl[@]}" -sS "https://${es_tls_name}:19200/_recovery/${restore_name}-*?active_only=false" 2>/dev/null || true)" >&2
  false
fi
mapfile -t restore_indices < <(jq -r '.snapshot.indices[]?' "$restore_response_file")
[[ "${#restore_indices[@]}" -gt 0 ]]
for restore_index in "${restore_indices[@]}"; do
  # action.destructive_requires_name rejects wildcard deletes; encode literal '%' in names.
  encoded_restore_index="${restore_index//%/%25}"
  delete_restore_response_file="/tmp/kubeauto-logging-${restore_name}-delete.json"
  delete_restore_http_code="$("${es_curl[@]}" -sS -o "$delete_restore_response_file" -w '%{http_code}' \
    -X DELETE "https://${es_tls_name}:19200/${encoded_restore_index}" || true)"
  if [[ ! "$delete_restore_http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "LOGGING_RESTORE_DELETE_HTTP_CODE=$delete_restore_http_code" >&2
    echo "LOGGING_RESTORE_DELETE_INDEX=$restore_index" >&2
    echo "LOGGING_RESTORE_DELETE_RESPONSE=$(cat "$delete_restore_response_file" 2>/dev/null || true)" >&2
    false
  fi
done
case_pass LOGGING-23 snapshot-restore-isolated-index
echo LOGGING_STAGE_BEGIN member-recovery
es_recovery_pod="$(kubectl -n logging get pod -l common.k8s.elastic.co/type=elasticsearch -o jsonpath='{.items[0].metadata.name}')"
kibana_recovery_pod="$(kubectl -n logging get pod -l kibana.k8s.elastic.co/name=logging -o jsonpath='{.items[0].metadata.name}')"
kubectl -n logging delete pod "$es_recovery_pod" "$kibana_recovery_pod" --wait=false >/dev/null
if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then
  collector_label=fluent-bit-kafka
else
  collector_label=fluent-bit
fi
collector_recovery_pod="$(kubectl -n logging get pod -l app.kubernetes.io/name=$collector_label -o jsonpath='{.items[0].metadata.name}')"
kubectl -n logging delete pod "$collector_recovery_pod" --wait=false >/dev/null
kubectl -n logging wait --for=jsonpath='{.status.health}'=green elasticsearches/logging --timeout=15m
kubectl -n logging wait --for=jsonpath='{.status.health}'=green kibanas/logging --timeout=15m
kubectl -n logging rollout status daemonset/$collector_label --timeout=10m
case_pass LOGGING-24 member-failure-recovery
echo LOGGING_STAGE_BEGIN idempotence
pvc_uid_before="$(kubectl -n logging get pvc -l common.k8s.elastic.co/type=elasticsearch -o jsonpath='{.items[0].metadata.uid}')"
secret_uid_before="$(kubectl -n logging get secret logging-efk-writer -o jsonpath='{.metadata.uid}')"
"$K" setup "$CLUSTER" 07 </dev/null
pvc_uid_after="$(kubectl -n logging get pvc -l common.k8s.elastic.co/type=elasticsearch -o jsonpath='{.items[0].metadata.uid}')"
secret_uid_after="$(kubectl -n logging get secret logging-efk-writer -o jsonpath='{.metadata.uid}')"
[[ "$pvc_uid_before" == "$pvc_uid_after" && "$secret_uid_before" == "$secret_uid_after" ]]
case_pass LOGGING-25 idempotent-product-workflow
run_extended_cases
if [[ -n "$LOGGING_FOCUS_CASE" ]]; then
  echo "LOGGING_FOCUSED_GATE_PASS case=$LOGGING_FOCUS_CASE"
else
  echo LOGGING_FULL_GATE_PASS
fi

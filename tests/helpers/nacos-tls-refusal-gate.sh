#!/usr/bin/env bash
# Product-entry TLS refusals in a disposable namespace, preserving healthy Nacos.
# Connector/J 8.2.0 ExportControlled.HostnameChecker defines the identity error.
wait_nacos_refusal_volumes() {
  local namespace="$1" attempt
  for attempt in $(seq 1 12); do
    if kubectl get pv -o json | python3 -c \
      'import json,sys; assert not any(p.get("spec",{}).get("claimRef",{}).get("namespace")==sys.argv[1] for p in json.load(sys.stdin)["items"])' \
      "$namespace" 2>/dev/null; then return 0; fi
    echo "[WAIT] Nacos TLS fixture volumes namespace=$namespace attempt=$attempt/12"
    sleep 5
  done
  return 1
}

verify_nacos_tls_refusals() (
  set -uo pipefail
  umask 077
  local cluster="$1" cfg="$2" directory kind rc namespace=nacos-tls-refusal
  local owns_namespace=0 owns_alias=0 pod pattern cleanup_rc
  directory=$(umask 077; mktemp -d /var/tmp/kubeauto-nacos-tls-refusal.XXXXXX) || exit 1
  cp "$cfg" "$directory/config.yml" || exit 1
  chmod 0600 "$directory/config.yml"
  finish_tls_refusals() {
    local initial_rc=$? cleanup_rc=0
    trap - EXIT
    cp "$directory/config.yml" "$cfg" || cleanup_rc=1
    if (( owns_namespace )); then
      if (( initial_rc != 0 )); then capture_nacos_failure "$namespace" || true; fi
      wait_namespace_deleted "$namespace" 120 || cleanup_rc=1
      wait_nacos_refusal_volumes "$namespace" || cleanup_rc=1
    fi
    if (( owns_alias )); then
      kubectl -n nacos delete service nacos-mysql-wrong --wait=true --timeout=60s || cleanup_rc=1
    fi
    if (( initial_rc == 0 && cleanup_rc == 0 )); then
      rm -f "$directory/config.yml" "$directory/untrusted-ca.key"
      sha256sum "$directory"/* >"$directory/SHA256SUMS"
      echo "NACOS_TLS_REFUSALS_SCOPED_CLEAN_PASS evidence=$directory"
    else
      echo "NACOS_TLS_REFUSALS_DIAGNOSTICS directory=$directory"
      exit 1
    fi
  }
  trap finish_tls_refusals EXIT
  grep -qx 'nacos_namespace: "nacos"' "$cfg" || exit 1
  if kubectl get namespace "$namespace" >/dev/null 2>&1; then exit 1; fi
  if kubectl -n nacos get service nacos-mysql-wrong >/dev/null 2>&1; then exit 1; fi
  kubectl -n nacos get secret nacos-mysql-tls -o 'jsonpath={.data.ca\.crt}' \
    | base64 -d >"$directory/trusted-ca.crt" || exit 1
  openssl x509 -in "$directory/trusted-ca.crt" -noout >/dev/null 2>&1 || exit 1
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 -subj /CN=kubeauto-untrusted-ca \
    -keyout "$directory/untrusted-ca.key" -out "$directory/untrusted-ca.crt" >/dev/null 2>&1 || exit 1
  cat <<'YAML' | kubectl create -f - || exit 1
apiVersion: v1
kind: Service
metadata: {name: nacos-mysql-wrong, namespace: nacos}
spec:
  selector: {app: nacos-mysql}
  ports: [{port: 3306, targetPort: 3306}]
YAML
  owns_alias=1
  for kind in untrusted-ca hostname; do
    kubectl create namespace "$namespace" || exit 1
    owns_namespace=1
    stage_nacos_auth_secret "$namespace" || exit 1
    if [[ "$kind" == untrusted-ca ]]; then
      kubectl -n "$namespace" create secret generic nacos-mysql-ca \
        --from-file=ca.crt="$directory/untrusted-ca.crt" || exit 1
      set_cfg "$cfg" nacos_mysql_host '"nacos-mysql.nacos.svc.cluster.local"' || exit 1
      pattern='Path does not chain with any of the trust anchors|unable to find valid certification path|PKIX path building failed'
    else
      kubectl -n "$namespace" create secret generic nacos-mysql-ca \
        --from-file=ca.crt="$directory/trusted-ca.crt" || exit 1
      set_cfg "$cfg" nacos_mysql_host '"nacos-mysql-wrong.nacos.svc.cluster.local"' || exit 1
      pattern='Server identity verification failed'
    fi
    set_cfg "$cfg" nacos_namespace '"nacos-tls-refusal"' || exit 1
    set_cfg "$cfg" nacos_mysql_ca_secret '"nacos-mysql-ca"' || exit 1
    set_cfg "$cfg" nacos_ready_timeout 90 || exit 1
    # The expected failed setup is private. Its rc alone never proves TLS refusal.
    if "$K" setup "$cluster" 07 </dev/null >"$directory/$kind.setup.log" 2>&1; then rc=0; else rc=$?; fi
    sha256sum "$cfg" >"$directory/$kind.config.sha256"
    cp "$BASE/clusters/$cluster/yml/nacos-sts.yaml" "$directory/$kind.rendered.yaml" || exit 1
    for pod in nacos-0 nacos-1 nacos-2; do
      timeout 20s kubectl -n "$namespace" logs "$pod" -c nacos --tail=200 \
        >"$directory/$kind.$pod.stdout.log" 2>&1 || true
      timeout 20s kubectl -n "$namespace" exec "$pod" -c nacos -- sh -c \
        'test ! -f /home/nacos/logs/nacos.log || tail -n 300 /home/nacos/logs/nacos.log' \
        >"$directory/$kind.$pod.application.log" 2>&1 || true
    done
    chmod 0600 "$directory"/*
    if (( rc == 0 )) || ! grep -Eq "$pattern" "$directory/$kind."*.log; then
      echo "NACOS_TLS_REFUSAL_FAIL kind=$kind product_rc=$rc"
      exit 1
    fi
    echo "NACOS_TLS_REFUSAL_PASS kind=$kind product_rc=$rc verified_application_cause=yes"
    wait_namespace_deleted "$namespace" 120 || exit 1
    wait_nacos_refusal_volumes "$namespace" || exit 1
    owns_namespace=0
    cp "$directory/config.yml" "$cfg" || exit 1
  done
  wait_nacos_external_mysql 60 && verify_nacos_business existing || exit 1
  echo NACOS_TLS_REFUSALS_PASS
)

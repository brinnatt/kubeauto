#!/usr/bin/env bash
# Independent Strimzi/Kafka live delivery gate. It does not execute the core matrix.
set -Eeuo pipefail

BASE="${KUBEAUTO_BASE:-/usr/local/kubeauto}"
KAFKA_NAMESPACE="${KAFKA_NAMESPACE:-kafka}"
KAFKA_OPERATOR_NAMESPACE="${KAFKA_OPERATOR_NAMESPACE:-kafka-operator}"
KAFKA_DRAIN_CLEANER_NAMESPACE="${KAFKA_DRAIN_CLEANER_NAMESPACE:-kafka-drain-cleaner}"
KAFKA_CLUSTER="${KAFKA_CLUSTER:-kafka-prod}"
KAFKA_STORAGE_CLASS="${KAFKA_STORAGE_CLASS:-kafka-longhorn}"
KAFKA_TOPIC="${KAFKA_TOPIC:-kubeauto-events}"
KAFKA_USER="${KAFKA_USER:-kafka-app}"
KAFKA_PASSWORD_SECRET="${KAFKA_PASSWORD_SECRET:-kafka-app-password}"
KAFKA_IMAGE_SOURCE_PREFIX="${KAFKA_IMAGE_SOURCE_PREFIX:-hub.talkedu.cn/kubeauto}"
KAFKA_IMAGE_FALLBACK_PREFIX="${KAFKA_IMAGE_FALLBACK_PREFIX:-}"
KAFKA_IMAGE_VERIFY_PREFIX="${KAFKA_IMAGE_VERIFY_PREFIX:-}"
REGISTRY_HOST="registry.talkschool.cn:5000"
REGISTRY_IP="${KAFKA_REGISTRY_IP:-$(hostname -I | awk '{print $1}')}"
REGISTRY_NAME="kubeauto-kafka-registry"
REGISTRY_DATA="/var/lib/kubeauto-kafka-registry"
MARKER="kubeauto-kafka-gate"
CLIENT_POD="kafka-gate-client"
BOOTSTRAP="${KAFKA_CLUSTER}-kafka-bootstrap:9093"
CORDON_STATE="/var/tmp/kubeauto-kafka-cordoned-nodes"
TAG_OWNERSHIP="/var/tmp/kubeauto-kafka-tags-owned"

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }
stage() { echo "KAFKA_STAGE_BEGIN id=$1 action=$2"; }

node_ips() {
  kubectl get nodes -o wide --no-headers | awk '{print $6}'
}

diagnostics() {
  local rc=$?
  if [[ -s "$CORDON_STATE" ]]; then
    while IFS= read -r node; do
      [[ -n "$node" ]] && kubectl uncordon "$node" >/dev/null 2>&1 || true
    done <"$CORDON_STATE"
  fi
  if [[ "$rc" -ne 0 ]]; then
    echo "========== KAFKA FAILURE DIAGNOSTICS =========="
    kubectl get nodes -o wide || true
    kubectl -n "$KAFKA_OPERATOR_NAMESPACE" get deployment,pod -o wide || true
    kubectl -n "$KAFKA_NAMESPACE" get kafka,kafkanodepool,kafkarebalance,kafkatopic,kafkauser,pod,pvc,svc -o wide || true
    kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" get deployment,pod,pdb -o wide || true
    kubectl -n "$KAFKA_NAMESPACE" get events --sort-by=.lastTimestamp | tail -120 || true
    kubectl -n "$KAFKA_OPERATOR_NAMESPACE" logs deployment/strimzi-cluster-operator --tail=300 || true
    kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" logs deployment/strimzi-drain-cleaner --tail=200 || true
    for pod in $(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/cluster="$KAFKA_CLUSTER" -o name 2>/dev/null); do
      kubectl -n "$KAFKA_NAMESPACE" logs "$pod" --all-containers --tail=100 || true
    done
  fi
  unset APP_PASSWORD OLD_APP_PASSWORD NEW_APP_PASSWORD ADMIN_PASSWORD || true
}
trap diagnostics EXIT

for prefix in "$KAFKA_IMAGE_SOURCE_PREFIX"; do
  [[ "$prefix" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)*$ ]] \
    || fail "invalid Kafka image prefix: ${prefix}"
done
if [[ -n "$KAFKA_IMAGE_VERIFY_PREFIX" ]]; then
  [[ "$KAFKA_IMAGE_VERIFY_PREFIX" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)*$ ]] \
    || fail "invalid Kafka image verifier prefix"
fi
if [[ -n "$KAFKA_IMAGE_FALLBACK_PREFIX" ]]; then
  [[ "$KAFKA_IMAGE_FALLBACK_PREFIX" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)*$ ]] \
    || fail "invalid Kafka image fallback prefix"
fi

manifest_digest() {
  local image="$1"
  skopeo inspect --raw "docker://${image}" | sha256sum | awk '{print "sha256:" $1}'
}

source_available() {
  timeout --signal=TERM --kill-after=5s 60s skopeo inspect --raw "docker://$1" >/dev/null 2>&1
}

materialize_image() {
  local name="$1" tag="$2" official="$3"
  local preferred="${KAFKA_IMAGE_SOURCE_PREFIX}/${name}:${tag}"
  local source="$preferred" verifier="$official" source_digest verifier_digest target_digest
  if ! source_available "$source"; then
    if [[ -n "$KAFKA_IMAGE_FALLBACK_PREFIX" ]]; then
      source="${KAFKA_IMAGE_FALLBACK_PREFIX}/${name}:${tag}"
      source_available "$source" || fail "runtime image fallback unavailable: ${source}"
      echo "[WARN] runtime-only Kafka image fallback selected image=${name}:${tag}"
    else
      source="$official"
      source_available "$source" || fail "official Kafka image unavailable: ${official}"
      echo "[WARN] TalkEdu image unavailable; using official upstream image=${official}"
    fi
  fi
  source_digest="$(manifest_digest "$source")"
  if [[ "$source" == "$preferred" && "$KAFKA_IMAGE_SOURCE_PREFIX" == hub.talkedu.cn/kubeauto \
    && -n "$KAFKA_IMAGE_VERIFY_PREFIX" ]]; then
    verifier="${KAFKA_IMAGE_VERIFY_PREFIX}/brinnatt/${name}:${tag}"
    source_available "$verifier" || fail "Docker Hub dual-push verifier unavailable: ${verifier}"
    verifier_digest="$(manifest_digest "$verifier")"
  elif [[ "$source" == "$preferred" && "$KAFKA_IMAGE_SOURCE_PREFIX" == hub.talkedu.cn/kubeauto ]]; then
    verifier="release-ci-dual-push-contract"
    verifier_digest="$source_digest"
    echo "KAFKA_IMAGE_DUAL_PUSH_ONLINE_VERIFY_SKIPPED image=${name}:${tag} reason=china_delivery_uses_verified_talkedu_artifact"
  else
    source_available "$verifier" || fail "Kafka image verifier unavailable: ${verifier}"
    verifier_digest="$(manifest_digest "$verifier")"
  fi
  [[ "$source_digest" == sha256:* && "$source_digest" == "$verifier_digest" ]] \
    || fail "source/verifier manifest digest mismatch: ${name}:${tag}"
  skopeo copy --all --retry-times 3 \
    "docker://${source}" \
    "docker://127.0.0.1:5000/brinnatt/${name}:${tag}" \
    --dest-tls-verify=false >/dev/null
  target_digest="$(skopeo inspect --raw --tls-verify=false \
    "docker://127.0.0.1:5000/brinnatt/${name}:${tag}" | sha256sum | awk '{print "sha256:" $1}')"
  [[ "$target_digest" == "$source_digest" ]] || fail "local registry digest mismatch: ${name}:${tag}"
  echo "KAFKA_IMAGE_MATERIALIZED image=${name}:${tag} digest=${target_digest} source=${source} verifier=${verifier}"
}

wait_kafka_ready() {
  local expected_version="${1:?wait_kafka_ready requires an expected Kafka version}"
  local attempt kafka_ready controllers brokers pvc version metadata
  for attempt in $(seq 1 240); do
    kafka_ready="$(kubectl -n "$KAFKA_NAMESPACE" get kafka "$KAFKA_CLUSTER" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
    controllers="$(kubectl -n "$KAFKA_NAMESPACE" get pod \
      -l strimzi.io/cluster="$KAFKA_CLUSTER",strimzi.io/pool-name=controller \
      -o json 2>/dev/null | jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' || true)"
    brokers="$(kubectl -n "$KAFKA_NAMESPACE" get pod \
      -l strimzi.io/cluster="$KAFKA_CLUSTER",strimzi.io/pool-name=broker \
      -o json 2>/dev/null | jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length' || true)"
    pvc="$(kubectl -n "$KAFKA_NAMESPACE" get pvc -l strimzi.io/cluster="$KAFKA_CLUSTER" \
      -o json 2>/dev/null | jq '[.items[] | select(.status.phase == "Bound")] | length' || true)"
    version="$(kubectl -n "$KAFKA_NAMESPACE" get kafka "$KAFKA_CLUSTER" -o jsonpath='{.status.kafkaVersion}' 2>/dev/null || true)"
    metadata="$(kubectl -n "$KAFKA_NAMESPACE" get kafka "$KAFKA_CLUSTER" -o jsonpath='{.status.kafkaMetadataVersion}' 2>/dev/null || true)"
    if [[ "$kafka_ready" == True && "$version" == "$expected_version" \
      && "$metadata" == 4.3-IV0 && "$controllers" == 3 && "$brokers" == 3 && "$pvc" -ge 6 ]]; then
      echo "KAFKA_READY version=${version} metadata=${metadata} controllers=${controllers} brokers=${brokers} pvc=${pvc}"
      return 0
    fi
    if (( attempt % 6 == 0 )); then
      echo "KAFKA_WAIT_HEARTBEAT elapsed=$((attempt * 10))s state=${kafka_ready:-missing} controllers=${controllers:-0} brokers=${brokers:-0} pvc=${pvc:-0}"
      kubectl -n "$KAFKA_NAMESPACE" get kafka,kafkanodepool,pod,pvc -o wide || true
    fi
    sleep 10
  done
  return 1
}

write_playbook() {
  local kafka_version="$1"
  cat >/tmp/kubeauto-kafka-playbook.yml <<EOF
- name: Strimzi Kafka live delivery gate
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    base_dir: ${BASE}
    cluster_dir: ${BASE}/clusters/kafka-gate
    kafka_namespace: ${KAFKA_NAMESPACE}
    kafka_operator_namespace: ${KAFKA_OPERATOR_NAMESPACE}
    kafka_drain_cleaner_namespace: ${KAFKA_DRAIN_CLEANER_NAMESPACE}
    kafka_cluster_name: ${KAFKA_CLUSTER}
    kafka_storage_class: ${KAFKA_STORAGE_CLASS}
    kafka_controller_size: 3
    kafka_broker_size: 3
    kafka_controller_pvc_size: 2Gi
    kafka_broker_pvc_size: 5Gi
    kafka_delete_claim: false
    kafka_topology_key: kubernetes.io/hostname
    kafka_operator_replicas: 2
    kafka_controller_cpu_request: 500m
    kafka_controller_memory_request: 1Gi
    kafka_controller_cpu_limit: "2"
    kafka_controller_memory_limit: 2Gi
    kafka_controller_heap: 768m
    kafka_broker_cpu_request: "1"
    kafka_broker_memory_request: 2Gi
    kafka_broker_cpu_limit: "3"
    kafka_broker_memory_limit: 6Gi
    kafka_broker_heap: 1536m
    kafka_cruise_control_enabled: true
    kafka_metrics_enabled: true
    kafka_monitoring_release_label: prometheus
    kafka_drain_cleaner_enabled: true
    kafka_bootstrap_resources_enabled: true
    kafka_app_user: ${KAFKA_USER}
    kafka_app_password_secret: ${KAFKA_PASSWORD_SECRET}
    kafka_app_password_secret_key: password
    kafka_default_topic: ${KAFKA_TOPIC}
    kafka_default_group_prefix: kubeauto-
    kafka_default_transactional_id_prefix: kubeauto-
    kafka_default_topic_partitions: 6
    kafka_default_topic_retention_ms: 604800000
    kafka_user_producer_byte_rate: 10485760
    kafka_user_consumer_byte_rate: 10485760
    kafka_user_request_percentage: 50
    kafka_cluster_ca_validity_days: 365
    kafka_cluster_ca_renewal_days: 30
    kafka_drain_cleaner_ca_valid_days: 3650
    kafka_drain_cleaner_cert_valid_days: 825
    kafka_drain_cleaner_cert_renew_before_days: 30
    strimzi_operator_ver: 1.2.0
    kafka_ver: ${kafka_version}
    kafka_metadata_ver: 4.3-IV0
    strimzi_drain_cleaner_ver: 1.6.1
    kafka_ready_retries: 240
    kafka_ready_delay: 10
  tasks:
    - name: Execute the production Kafka task file
      ansible.builtin.import_role:
        name: cluster-addon
        tasks_from: kafka
EOF
}

run_role() {
  local kafka_version="$1"
  write_playbook "$kafka_version"
  ANSIBLE_ROLES_PATH="$BASE/roles" "$ANSIBLE_PLAYBOOK" -i localhost, /tmp/kubeauto-kafka-playbook.yml
}

create_client_pod() {
  # The same client name is recreated between stages.  Wait for the old pod
  # to disappear before applying the replacement; otherwise kubectl may
  # report success while exec still targets a Terminating pod.
  kubectl -n "$KAFKA_NAMESPACE" delete pod "$CLIENT_POD" \
    --ignore-not-found --wait=true --timeout=120s >/dev/null
  cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${CLIENT_POD}
  namespace: ${KAFKA_NAMESPACE}
  labels:
    kubeauto.io/component: kafka-test
spec:
  restartPolicy: Never
  containers:
    - name: kafka
      image: ${REGISTRY_HOST}/brinnatt/strimzi-kafka:1.2.0-kafka-4.3.1
      imagePullPolicy: IfNotPresent
      command: [/bin/bash, -c, "exec sleep 86400"]
      resources:
        requests: {cpu: 100m, memory: 256Mi}
        limits: {cpu: "2", memory: 2Gi}
      volumeMounts:
        - {name: ca, mountPath: /opt/kafka/gate/ca, readOnly: true}
        - {name: app-password, mountPath: /opt/kafka/gate/app, readOnly: true}
        - {name: admin-password, mountPath: /opt/kafka/gate/admin, readOnly: true}
  volumes:
    - name: ca
      secret:
        secretName: ${KAFKA_CLUSTER}-cluster-ca-cert
        items: [{key: ca.crt, path: ca.crt}]
    - name: app-password
      secret:
        secretName: ${KAFKA_PASSWORD_SECRET}
        items: [{key: password, path: password}]
    - name: admin-password
      secret:
        secretName: kafka-gate-admin
        items: [{key: password, path: password}]
EOF
  kubectl -n "$KAFKA_NAMESPACE" wait --for=condition=Ready pod/"$CLIENT_POD" --timeout=10m
  kubectl -n "$KAFKA_NAMESPACE" exec "$CLIENT_POD" -- bash -ceu '
    write_config() {
      local user="$1" password_file="$2" output="$3" password ca_pem line
      password="$(cat "$password_file")"
      ca_pem=""
      while IFS= read -r line; do ca_pem+="${line}\\n"; done < /opt/kafka/gate/ca/ca.crt
      umask 077
      cat >"$output" <<EOF
security.protocol=SASL_SSL
bootstrap.servers=kafka-prod-kafka-bootstrap:9093
sasl.mechanism=SCRAM-SHA-512
sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="${user}" password="${password}";
ssl.truststore.type=PEM
ssl.truststore.certificates=${ca_pem}
client.dns.lookup=use_all_dns_ips
request.timeout.ms=10000
default.api.timeout.ms=15000
EOF
    }
    write_config kafka-app /opt/kafka/gate/app/password /tmp/app.properties
    write_config kafka-gate-admin /opt/kafka/gate/admin/password /tmp/admin.properties
    cp /tmp/app.properties /tmp/wrong-password.properties
    sed -i "s/password=\"[^\"]*\"/password=\"definitely-wrong-kubeauto-password\"/" /tmp/wrong-password.properties
    printf "%s\\n" invalid-ca >/tmp/wrong-ca.crt
    wrong_ca_pem=""
    while IFS= read -r line; do wrong_ca_pem+="${line}\\n"; done < /tmp/wrong-ca.crt
    sed "s#^ssl.truststore.certificates=.*#ssl.truststore.certificates=${wrong_ca_pem}#" \
      /tmp/app.properties >/tmp/wrong-ca.properties
  '
}

client() {
  # Preserve stdin for console producer/consumer commands. Without -i,
  # kubectl closes the producer input immediately and a zero exit code can
  # falsely suggest that a marker was persisted.
  kubectl -n "$KAFKA_NAMESPACE" exec -i "$CLIENT_POD" -- "$@"
}

produce_marker() {
  local marker="$1"
  echo "KAFKA_PRODUCE_ATTEMPT marker=${marker}"
  printf '%s\n' "$marker" | client /opt/kafka/bin/kafka-console-producer.sh \
    --bootstrap-server "$BOOTSTRAP" --topic "$KAFKA_TOPIC" \
    --producer.config /tmp/app.properties \
    --producer-property acks=all --producer-property enable.idempotence=true \
    --producer-property request.timeout.ms=10000 \
    --producer-property delivery.timeout.ms=60000 >/dev/null \
    || fail "Kafka marker production failed: ${marker}"
  echo "KAFKA_PRODUCE_SUBMITTED marker=${marker}"
}

consume_marker() {
  local marker="$1" group="$2" attempt output_file error_file consumer_group
  for attempt in $(seq 1 4); do
    consumer_group="${group}-${attempt}"
    output_file="/tmp/kubeauto-consume-${group}-${attempt}.out"
    error_file="/tmp/kubeauto-consume-${group}-${attempt}.err"
    echo "KAFKA_CONSUME_ATTEMPT marker=${marker} group=${consumer_group} attempt=${attempt}/4"
    if client /opt/kafka/bin/kafka-console-consumer.sh \
      --bootstrap-server "$BOOTSTRAP" --topic "$KAFKA_TOPIC" \
      --consumer.config /tmp/app.properties --from-beginning \
      --group "$consumer_group" --timeout-ms 60000 \
      >"$output_file" 2>"$error_file"; then
      if grep -Fxq "$marker" "$output_file"; then
        echo "KAFKA_CONSUME_MARKER_PASS marker=${marker} group=${consumer_group}"
        return 0
      fi
    fi
    echo "KAFKA_CONSUME_RETRY marker=${marker} group=${consumer_group} output_lines=$(wc -l <"$output_file")"
    tail -n 8 "$error_file" || true
    sleep 10
  done
  echo "KAFKA_CONSUME_MARKER_FAIL marker=${marker} group=${group}" >&2
  return 1
}

stage KAFKA-01 "static and version preflight"
for command in kubectl jq openssl helm ansible-playbook docker curl ssh timeout sha256sum; do
  command -v "$command" >/dev/null || fail "required command unavailable: ${command}"
done
ANSIBLE_PLAYBOOK="$(command -v ansible-playbook)"
if ! command -v skopeo >/dev/null; then
  dnf install -y skopeo >/dev/null || fail "unable to install skopeo from configured distribution repositories"
fi
test "$(kubectl version -o json | jq -r '.serverVersion.gitVersion')" = v1.33.6 || fail "Kubernetes version mismatch"
test "$(sha256sum "$BASE/roles/cluster-addon/files/strimzi-kafka-operator-1.2.0.tgz" | awk '{print $1}')" = \
  0f8a50b2f19bd99482f9fd6e17cf42902f72f9e594a136813ac3f0b7af422efd || fail "Operator Chart checksum mismatch"
test "$(sha256sum "$BASE/roles/cluster-addon/files/strimzi-drain-cleaner-1.6.1.tgz" | awk '{print $1}')" = \
  ce84b8ddcd105f1b10d085fe69ed0d9185f798d009de3fba967386af2b8f6fdd || fail "Drain Cleaner Chart checksum mismatch"
tar -xOf "$BASE/roles/cluster-addon/files/strimzi-kafka-operator-1.2.0.tgz" \
  strimzi-kafka-operator/Chart.yaml | grep -qx 'appVersion: 1.2.0' || fail "Operator Chart appVersion mismatch"
write_playbook 4.3.0
ANSIBLE_ROLES_PATH="$BASE/roles" "$ANSIBLE_PLAYBOOK" \
  -i localhost, --syntax-check /tmp/kubeauto-kafka-playbook.yml >/dev/null \
  || fail "production Kafka role failed Ansible static syntax validation"
pass "KAFKA-01 GA versions, Chart SHA256 and Ansible static syntax"

stage KAFKA-03 "verified images to scoped local registry"
if ss -lnt | awk '{print $4}' | grep -Eq '(^|:)5000$'; then
  curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null \
    || fail "TCP port 5000 is occupied by a non-Registry service"
  registry_container_count="$(docker ps --filter publish=5000 --format '{{.Names}}' | wc -l)"
  [[ "$registry_container_count" -eq 1 ]] \
    || fail "TCP port 5000 ownership is ambiguous: containers=${registry_container_count}"
  echo "KAFKA_REGISTRY_REUSED name=$(docker ps --filter publish=5000 --format '{{.Names}}')"
else
  registry_image=hub.talkedu.cn/kubeauto/registry:2.8.3
  registry_image_owned=false
  if ! docker image inspect "$registry_image" >/dev/null 2>&1; then
    if docker pull "$registry_image" >/dev/null; then
      registry_image_owned=true
    else
      registry_image=hub.talkedu.cn/kubeauto/registry:2
      if ! docker image inspect "$registry_image" >/dev/null 2>&1; then
        if docker pull "$registry_image" >/dev/null; then
          registry_image_owned=true
        else
          registry_image=docker.io/library/registry:2.8.3
          docker pull "$registry_image" >/dev/null || fail "Registry support image unavailable"
          registry_image_owned=true
        fi
      fi
    fi
  fi
  if [[ "$registry_image_owned" == true ]]; then
    printf '%s\n' "$registry_image" >/var/tmp/kubeauto-kafka-registry-image-owned
  fi
  install -d -m 0700 "$REGISTRY_DATA"
  docker run -d --restart=no --name "$REGISTRY_NAME" --label kubeauto.kafka-gate=true \
    -e REGISTRY_STORAGE_DELETE_ENABLED=true -p 5000:5000 \
    -v "$REGISTRY_DATA:/var/lib/registry" "$registry_image" >/dev/null
fi
for attempt in $(seq 1 30); do
  curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null && break
  sleep 2
done
curl -fsS --max-time 3 http://127.0.0.1:5000/v2/ >/dev/null || fail "scoped registry did not become ready"

for ip in $(node_ips); do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@${ip}" bash -s -- \
    "$REGISTRY_IP" "$REGISTRY_HOST" "$MARKER" <<'NODE_REGISTRY'
set -Eeuo pipefail
registry_ip="$1"
registry_host="$2"
marker="$3"
host_name="${registry_host%:*}"
existing_ip="$(awk -v host="$host_name" '$1 !~ /^#/ {for (field=2; field<=NF; field++) if ($field==host) print $1}' /etc/hosts | head -n 1)"
if [[ -n "$existing_ip" && "$existing_ip" != "$registry_ip" ]]; then
  echo "conflicting registry mapping: ${host_name}=${existing_ip}" >&2
  exit 1
fi
if [[ -z "$existing_ip" ]]; then
  sed -i "/[[:space:]]# ${marker}$/d" /etc/hosts
  printf '%s %s # %s\n' "$registry_ip" "$host_name" "$marker" >>/etc/hosts
fi
config_dir="/etc/containerd/certs.d/${registry_host}"
if [[ -e "$config_dir" && ! -e "$config_dir/.kubeauto-kafka-gate" ]]; then
  test -s "$config_dir/hosts.toml" || {
    echo "existing containerd registry config is incomplete: ${config_dir}" >&2
    exit 1
  }
  exit 0
fi
install -d -m 0755 "$config_dir"
touch "$config_dir/.kubeauto-kafka-gate"
cat >"$config_dir/hosts.toml" <<EOF
server = "http://${registry_host}"
[host."http://${registry_host}"]
  capabilities = ["pull", "resolve"]
EOF
NODE_REGISTRY
done

: >"$TAG_OWNERSHIP"
materialize_image strimzi-operator 1.2.0 quay.io/strimzi/operator:1.2.0
materialize_image strimzi-kafka 1.2.0-kafka-4.3.0 quay.io/strimzi/kafka:1.2.0-kafka-4.3.0
materialize_image strimzi-kafka 1.2.0-kafka-4.3.1 quay.io/strimzi/kafka:1.2.0-kafka-4.3.1
materialize_image strimzi-drain-cleaner 1.6.1 quay.io/strimzi/drain-cleaner:1.6.1
pass "KAFKA-03 production and upgrade-test images verified and materialized"

bash "$BASE/tests/helpers/kafka-lab-storage.sh" prepare

stage KAFKA-02 "clean topology, capacity and storage preflight"
for namespace in "$KAFKA_NAMESPACE" "$KAFKA_OPERATOR_NAMESPACE" "$KAFKA_DRAIN_CLEANER_NAMESPACE"; do
  ! kubectl get namespace "$namespace" >/dev/null 2>&1 || fail "namespace is not clean: ${namespace}"
done
! kubectl get kafka --all-namespaces -o name >/dev/null 2>&1 \
  || [[ -z "$(kubectl get kafka --all-namespaces -o name 2>/dev/null)" ]] \
  || fail "existing Strimzi Kafka resources make this dedicated gate unsafe"
test "$(kubectl get nodes --no-headers | awk '$2 == "Ready" {count++} END {print count+0}')" -ge 6 \
  || fail "fewer than six Ready nodes"
test "$(kubectl get storageclass "$KAFKA_STORAGE_CLASS" -o jsonpath='{.provisioner}')" = driver.longhorn.io \
  || fail "Kafka gate requires distributed block CSI: ${KAFKA_STORAGE_CLASS}"
test "$(kubectl get storageclass "$KAFKA_STORAGE_CLASS" -o jsonpath='{.allowVolumeExpansion}')" = true \
  || fail "Kafka gate StorageClass must support expansion: ${KAFKA_STORAGE_CLASS}"
for ip in $(node_ips); do
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@${ip}" true \
    || fail "node SSH unavailable: ${ip}"
done
[[ "$REGISTRY_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || fail "invalid registry IP: ${REGISTRY_IP}"
pass "KAFKA-02 clean namespaces, six Ready nodes, SSH and expandable distributed block CSI"

stage KAFKA-16 "install supported predecessor and roll to pinned Kafka"
install -d -m 0755 "$BASE/extra-bin"
if [[ ! -x "$BASE/extra-bin/helm" ]]; then
  install -m 0755 "$(command -v helm)" "$BASE/extra-bin/helm"
fi
install -d -m 0700 "$BASE/clusters/kafka-gate"
install -m 0600 "${KUBECONFIG:-/root/.kube/config}" "$BASE/clusters/kafka-gate/kubectl.kubeconfig"
if ! kubectl get crd kafkas.kafka.strimzi.io >/dev/null 2>&1; then
  touch /var/tmp/kubeauto-kafka-crds-owned
fi
kubectl create namespace "$KAFKA_NAMESPACE" >/dev/null
kubectl label namespace "$KAFKA_NAMESPACE" app.kubernetes.io/managed-by=kubeauto \
  kubeauto.io/component=kafka kubeauto.io/middleware=kafka --overwrite >/dev/null
APP_PASSWORD="$(openssl rand -base64 48 | tr -d '\n')"
kubectl -n "$KAFKA_NAMESPACE" create secret generic "$KAFKA_PASSWORD_SECRET" \
  --from-literal=password="$APP_PASSWORD" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$KAFKA_NAMESPACE" label secret "$KAFKA_PASSWORD_SECRET" \
  kubeauto.io/component=kafka-test --overwrite >/dev/null
unset APP_PASSWORD
run_role 4.3.0
wait_kafka_ready 4.3.0 || fail "Kafka 4.3.0 predecessor did not become Ready"

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: kafka.strimzi.io/v1
kind: KafkaUser
metadata:
  name: kafka-gate-admin
  namespace: ${KAFKA_NAMESPACE}
  labels:
    strimzi.io/cluster: ${KAFKA_CLUSTER}
    kubeauto.io/component: kafka-test
spec:
  authentication: {type: scram-sha-512}
  authorization:
    type: simple
    acls:
      - {type: allow, operations: [All], resource: {type: topic, name: "*", patternType: literal}}
      - {type: allow, operations: [All], resource: {type: group, name: "*", patternType: literal}}
      - {type: allow, operations: [All], resource: {type: transactionalId, name: "*", patternType: literal}}
      - {type: allow, operations: [All], resource: {type: cluster}}
EOF
kubectl -n "$KAFKA_NAMESPACE" wait --for=condition=Ready kafkauser/kafka-gate-admin --timeout=10m

run_role 4.3.1
wait_kafka_ready 4.3.1 || fail "Kafka 4.3.1 rolling upgrade did not converge"
test "$(kubectl -n "$KAFKA_NAMESPACE" get kafka "$KAFKA_CLUSTER" -o jsonpath='{.status.kafkaMetadataVersion}')" = 4.3-IV0
test "$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/cluster="$KAFKA_CLUSTER",strimzi.io/component-type=kafka \
  -o json | jq '[.items[].spec.containers[] | select(.name == "kafka") | select(.image | endswith("1.2.0-kafka-4.3.1"))] | length')" = 6
create_client_pod
rollback_marker="upgrade-rollback-$(date +%s)"
produce_marker "$rollback_marker"
# Establish a durable message/read evidence point before changing the broker
# version. The rollback assertion uses a separate group so it remains an
# independent verification of data readability after the version transition.
consume_marker "$rollback_marker" kubeauto-upgrade-persisted
run_role 4.3.0
wait_kafka_ready 4.3.0 || fail "Kafka rollback to 4.3.0 did not converge"
consume_marker "$rollback_marker" kubeauto-upgrade-rollback
run_role 4.3.1
wait_kafka_ready 4.3.1 || fail "Kafka final convergence to 4.3.1 did not complete"
pass "KAFKA-16 supported bugfix upgrade, rollback verification and final 4.3.1 convergence"

stage KAFKA-04 "current production topology readiness"
for deployment in "${KAFKA_CLUSTER}-entity-operator" "${KAFKA_CLUSTER}-cruise-control" "${KAFKA_CLUSTER}-kafka-exporter"; do
  kubectl -n "$KAFKA_NAMESPACE" rollout status deployment/"$deployment" --timeout=10m
done
kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" rollout status deployment/strimzi-drain-cleaner --timeout=10m
test "$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=controller \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l)" = 3
test "$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u | wc -l)" = 3
pass "KAFKA-04 Operator, three controllers, three brokers, Entity Operator, Cruise Control, Exporter and Drain Cleaner Ready"

create_client_pod

stage KAFKA-05 "KRaft quorum and broker registration"
quorum_status=""
quorum_error=/tmp/kubeauto-kafka-quorum.err
for quorum_attempt in $(seq 1 12); do
  if quorum_status="$(client /opt/kafka/bin/kafka-metadata-quorum.sh \
    --bootstrap-server "$BOOTSTRAP" --command-config /tmp/admin.properties describe --status \
    2>"$quorum_error")"; then
    break
  fi
  echo "KAFKA_QUORUM_RETRY attempt=${quorum_attempt}/12"
  tail -20 "$quorum_error" || true
  sleep 5
done
[[ -n "$quorum_status" ]] || fail "KRaft quorum status command failed"
echo "KAFKA_QUORUM_STATUS_BEGIN"
printf '%s\n' "$quorum_status" | sed -n '1,20p'
echo "KAFKA_QUORUM_STATUS_END"
grep -Eq '^[[:space:]]*LeaderId:[[:space:]]+[0-9]+' <<<"$quorum_status" || fail "KRaft leader missing"
voter_line="$(grep -E '^[[:space:]]*CurrentVoters:' <<<"$quorum_status" || true)"
if grep -q '"id"' <<<"$voter_line"; then
  voter_count="$(grep -oE '"id"[[:space:]]*:[[:space:]]*[0-9]+' <<<"$voter_line" | wc -l)"
else
  voter_count="$(sed 's/^[^:]*:[[:space:]]*//' <<<"$voter_line" | grep -oE '[0-9]+' | wc -l)"
fi
[[ "$voter_count" -ge 3 ]] || fail "three KRaft voters not reported"
broker_api_ok=false
for broker_attempt in $(seq 1 12); do
  if client /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server "$BOOTSTRAP" \
    --command-config /tmp/admin.properties 2>"$quorum_error" \
    | grep -c 'id:' | awk '$1 >= 3' >/dev/null; then
    broker_api_ok=true
    break
  fi
  echo "KAFKA_BROKER_API_RETRY attempt=${broker_attempt}/12"
  tail -20 "$quorum_error" || true
  sleep 5
done
[[ "$broker_api_ok" == true ]] || fail "three registered brokers not visible"
pass "KAFKA-05 KRaft quorum leader, three voters and three brokers stable"

stage KAFKA-06 "TLS data path, order, offsets and transactions"
client bash -ceu '
  : >/tmp/expected.txt
  for i in $(seq -w 1 200); do printf "ordered-key:ordered-%s\n" "$i" >>/tmp/expected.txt; done
  /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server kafka-prod-kafka-bootstrap:9093 \
    --topic kubeauto-events --producer.config /tmp/app.properties \
    --property parse.key=true --property key.separator=: \
    --producer-property acks=all --producer-property enable.idempotence=true </tmp/expected.txt >/dev/null
  /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka-prod-kafka-bootstrap:9093 \
    --topic kubeauto-events --consumer.config /tmp/app.properties --from-beginning \
    --group kubeauto-order --max-messages 201 --timeout-ms 60000 \
    --property print.key=true --property key.separator=: >/tmp/order-raw.txt 2>/tmp/consumer.err
  grep "^ordered-key:ordered-" /tmp/order-raw.txt >/tmp/actual.txt
  test "$(wc -l </tmp/actual.txt)" -eq 200
  test "$(sha256sum /tmp/expected.txt | cut -d" " -f1)" = \
    "$(sha256sum /tmp/actual.txt | cut -d" " -f1)"
  sha256sum /tmp/expected.txt /tmp/actual.txt
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka-prod-kafka-bootstrap:9093 \
    --command-config /tmp/app.properties --group kubeauto-order --describe \
    | awk "NR > 1 && NF >= 6 {seen=1; if (\$6 != 0) exit 1} END {exit seen ? 0 : 1}"
'

cat >/tmp/kafka-gate-transactional.java <<'JAVA'
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.clients.producer.*;
import org.apache.kafka.common.serialization.*;

class KafkaGateTransactional {
  static Properties load(String path) throws Exception {
    Properties p = new Properties();
    try (var in = Files.newInputStream(Path.of(path))) { p.load(in); }
    return p;
  }
  public static void main(String[] args) throws Exception {
    String bootstrap=args[0], topic=args[1], config=args[2], run=args[3];
    Properties p=load(config);
    p.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap);
    p.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
    p.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
    p.put(ProducerConfig.TRANSACTIONAL_ID_CONFIG, "kubeauto-transaction-"+run);
    try (KafkaProducer<String,String> producer=new KafkaProducer<>(p)) {
      producer.initTransactions();
      producer.beginTransaction();
      producer.send(new ProducerRecord<>(topic, "tx-key", "tx-commit-"+run)).get();
      producer.commitTransaction();
      producer.beginTransaction();
      producer.send(new ProducerRecord<>(topic, "tx-key", "tx-abort-"+run)).get();
      producer.abortTransaction();
    }
    Properties c=load(config);
    c.put(ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrap);
    c.put(ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class.getName());
    c.put(ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class.getName());
    c.put(ConsumerConfig.GROUP_ID_CONFIG, "kubeauto-tx-"+run);
    c.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");
    c.put(ConsumerConfig.ISOLATION_LEVEL_CONFIG, "read_committed");
    boolean committed=false, aborted=false;
    try (KafkaConsumer<String,String> consumer=new KafkaConsumer<>(c)) {
      consumer.subscribe(List.of(topic));
      long deadline=System.currentTimeMillis()+30000;
      while (System.currentTimeMillis()<deadline && !committed) {
        for (var record: consumer.poll(Duration.ofMillis(1000))) {
          committed |= record.value().equals("tx-commit-"+run);
          aborted |= record.value().equals("tx-abort-"+run);
        }
      }
    }
    if (!committed || aborted) throw new IllegalStateException("transaction visibility mismatch committed="+committed+" aborted="+aborted);
    System.out.println("KAFKA_TRANSACTION_COMMIT_ABORT_PASS run="+run);
  }
}
JAVA
run_id="$(date +%s)"
if ! command -v javac >/dev/null 2>&1; then
  echo "KAFKA_TEST_DEPENDENCY_INSTALL package=java-21-openjdk-devel reason=transactional-client-compiler"
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y java-21-openjdk-devel >/dev/null
  elif command -v yum >/dev/null 2>&1; then
    yum install -y java-21-openjdk-devel >/dev/null
  else
    fail "transactional client compiler unavailable: no dnf or yum"
  fi
fi
command -v javac >/dev/null 2>&1 || fail "transactional client compiler unavailable after installation"
client_jar="$(client sh -ceu 'printf "%s\n" /opt/kafka/libs/kafka-clients-*.jar' | head -1)"
[[ -n "$client_jar" ]] || fail "Kafka client jar not found in test image"
rm -rf /tmp/kafka-gate-classes
mkdir -p /tmp/kafka-gate-classes
kubectl -n "$KAFKA_NAMESPACE" cp /tmp/kafka-gate-transactional.java "$CLIENT_POD:/tmp/KafkaGateTransactional.java" -c kafka
kubectl -n "$KAFKA_NAMESPACE" cp "$CLIENT_POD:$client_jar" /tmp/kafka-gate-kafka-clients.jar -c kafka
javac -cp /tmp/kafka-gate-kafka-clients.jar -d /tmp/kafka-gate-classes /tmp/kafka-gate-transactional.java
kubectl -n "$KAFKA_NAMESPACE" cp /tmp/kafka-gate-classes/KafkaGateTransactional.class "$CLIENT_POD:/tmp/KafkaGateTransactional.class" -c kafka
client bash -ceu 'java -cp "/opt/kafka/libs/*:/tmp" KafkaGateTransactional "$@"' \
  gate "$BOOTSTRAP" "$KAFKA_TOPIC" /tmp/app.properties "$run_id" | grep -F KAFKA_TRANSACTION_COMMIT_ABORT_PASS
pass "KAFKA-06 TLS produce/consume, partition order, committed offsets and transaction isolation"

stage KAFKA-07 "SCRAM, ACL, CA and quota security"
if client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --command-config /tmp/wrong-password.properties --list >/tmp/wrong-password.out 2>&1; then
  fail "wrong SCRAM password unexpectedly succeeded"
fi
if client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --command-config /tmp/wrong-ca.properties --list >/tmp/wrong-ca.out 2>&1; then
  fail "wrong CA unexpectedly succeeded"
fi
forbidden_topic="kubeauto-forbidden-$(date +%s)"
client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --command-config /tmp/admin.properties --create --if-not-exists \
  --topic "$forbidden_topic" --partitions 1 --replication-factor 3 >/tmp/forbidden-topic-create.out
producer_rc=0
printf '%s\n' forbidden-write | client /opt/kafka/bin/kafka-console-producer.sh \
  --bootstrap-server "$BOOTSTRAP" --topic "$forbidden_topic" \
  --producer.config /tmp/app.properties \
  --producer-property acks=all --producer-property max.block.ms=10000 \
  --producer-property request.timeout.ms=5000 --producer-property delivery.timeout.ms=15000 \
  >/tmp/forbidden-topic.out 2>&1 || producer_rc=$?
grep -Eq 'TopicAuthorizationException|TOPIC_AUTHORIZATION_FAILED|Not authorized' /tmp/forbidden-topic.out \
  || fail "unauthorized topic failure did not report an authorization error"
echo "KAFKA_ACL_TOPIC_DENY observed_rc=${producer_rc}"
consumer_rc=0
client /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server "$BOOTSTRAP" \
  --topic "$KAFKA_TOPIC" --consumer.config /tmp/app.properties \
  --group forbidden-group --timeout-ms 5000 >/tmp/forbidden-group.out 2>&1 || consumer_rc=$?
grep -Eq 'GroupAuthorizationException|GROUP_AUTHORIZATION_FAILED|Not authorized' /tmp/forbidden-group.out \
  || fail "unauthorized group failure did not report an authorization error"
echo "KAFKA_ACL_GROUP_DENY observed_rc=${consumer_rc}"
client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --command-config /tmp/admin.properties --delete --topic "$forbidden_topic" >/dev/null 2>&1 || true
quota_json="$(kubectl -n "$KAFKA_NAMESPACE" get kafkauser "$KAFKA_USER" -o json)"
test "$(jq -r '.spec.quotas.producerByteRate' <<<"$quota_json")" = 10485760
test "$(jq -r '.spec.quotas.consumerByteRate' <<<"$quota_json")" = 10485760
test "$(jq -r '.spec.quotas.requestPercentage' <<<"$quota_json")" = 50
pass "KAFKA-07 TLS/SCRAM/ACL/CA negative paths and user quotas"

stage KAFKA-18 "second production role execution is idempotent"
before_identity="$(kubectl -n "$KAFKA_NAMESPACE" get pvc,secret,kafkanodepool -o json 2>/dev/null \
  | jq -S --arg cluster "$KAFKA_CLUSTER" --arg password "$KAFKA_PASSWORD_SECRET" '
      [.items[]
       | select(
           (.kind == "PersistentVolumeClaim" and .metadata.labels["strimzi.io/cluster"] == $cluster)
           or (.kind == "KafkaNodePool" and .metadata.labels["strimzi.io/cluster"] == $cluster)
           or (.kind == "Secret" and (
             .metadata.labels["strimzi.io/cluster"] == $cluster or .metadata.name == $password
           ))
         )
       | [.kind,.metadata.name,.metadata.uid]] | sort')"
before_pods="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/cluster="$KAFKA_CLUSTER" \
  -o json | jq -S '[.items[] | [.metadata.name,.metadata.uid]] | sort')"
run_role 4.3.1
wait_kafka_ready 4.3.1 || fail "Kafka did not remain Ready after idempotent role rerun"
after_identity="$(kubectl -n "$KAFKA_NAMESPACE" get pvc,secret,kafkanodepool -o json 2>/dev/null \
  | jq -S --arg cluster "$KAFKA_CLUSTER" --arg password "$KAFKA_PASSWORD_SECRET" '
      [.items[]
       | select(
           (.kind == "PersistentVolumeClaim" and .metadata.labels["strimzi.io/cluster"] == $cluster)
           or (.kind == "KafkaNodePool" and .metadata.labels["strimzi.io/cluster"] == $cluster)
           or (.kind == "Secret" and (
             .metadata.labels["strimzi.io/cluster"] == $cluster or .metadata.name == $password
           ))
         )
       | [.kind,.metadata.name,.metadata.uid]] | sort')"
after_pods="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/cluster="$KAFKA_CLUSTER" \
  -o json | jq -S '[.items[] | [.metadata.name,.metadata.uid]] | sort')"
[[ "$before_identity" == "$after_identity" ]] || fail "role rerun replaced PVC, Secret or NodePool identity"
[[ "$before_pods" == "$after_pods" ]] || fail "role rerun caused an unintended Kafka rollout"
pass "KAFKA-18 second role run preserved PVC, Secret, NodePool and Pod identity"

wait_pool_ready() {
  local pool="$1" expected="$2" attempt ready kafka_ready
  for attempt in $(seq 1 240); do
    ready="$(kubectl -n "$KAFKA_NAMESPACE" get pod \
      -l strimzi.io/cluster="$KAFKA_CLUSTER",strimzi.io/pool-name="$pool" \
      -o json | jq '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | length')"
    kafka_ready="$(kubectl -n "$KAFKA_NAMESPACE" get kafka "$KAFKA_CLUSTER" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
    if [[ "$ready" -eq "$expected" && "$kafka_ready" == True ]]; then
      echo "KAFKA_POOL_READY pool=${pool} ready=${ready} kafka=${kafka_ready}"
      return 0
    fi
    if (( attempt % 6 == 0 )); then
      echo "KAFKA_WAIT_HEARTBEAT pool=${pool} expected=${expected} ready=${ready} kafka=${kafka_ready:-missing} elapsed=$((attempt * 10))s"
      kubectl -n "$KAFKA_NAMESPACE" get kafka,kafkanodepool,pod,pvc -o wide || true
    fi
    sleep 10
  done
  return 1
}

assert_topic_healthy() {
  local description replica_count
  if ! description="$(client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --command-config /tmp/admin.properties --topic "$KAFKA_TOPIC" --describe \
    2>/tmp/kubeauto-topic-health.err)"; then
    tail -n 8 /tmp/kubeauto-topic-health.err || true
    return 1
  fi
  ! grep -Eq 'Leader:[[:space:]]+-1|Offline' <<<"$description" || return 1
  while IFS= read -r replicas; do
    replica_count="$(tr ',' '\n' <<<"$replicas" | sed '/^$/d' | wc -l)"
    [[ "$replica_count" -eq 3 ]] || return 1
  done < <(sed -n 's/.*Isr: \([^[:space:]]*\).*/\1/p' <<<"$description")
  [[ "$(sed -n 's/.*Isr: \([^[:space:]]*\).*/\1/p' <<<"$description" | wc -l)" -eq 6 ]]
}

stage KAFKA-08 "single broker loss under sustained writes"
broker_pod="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker \
  -o jsonpath='{.items[0].metadata.name}')"
broker_uid="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$broker_pod" -o jsonpath='{.metadata.uid}')"
failure_run="$(date +%s)"
client bash -ceu 'rm -f /tmp/failure-writer.rc; (for i in $(seq 1 3000); do printf "broker-failure-%s-%06d\n" "$1" "$i"; sleep 0.01; done | /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server "$2" --topic "$3" --producer.config /tmp/app.properties --producer-property acks=all --producer-property enable.idempotence=true >/tmp/failure-writer.log 2>&1); printf "%s\n" "$?" >/tmp/failure-writer.rc' \
  gate "$failure_run" "$BOOTSTRAP" "$KAFKA_TOPIC" &
writer_exec_pid=$!
sleep 3
kubectl -n "$KAFKA_NAMESPACE" delete pod "$broker_pod" --wait=false >/dev/null
for attempt in $(seq 1 180); do
  current_uid="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$broker_pod" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
  current_ready="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$broker_pod" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  [[ -n "$current_uid" && "$current_uid" != "$broker_uid" && "$current_ready" == True ]] && break
  if (( attempt % 12 == 0 )); then
    echo "KAFKA_WAIT_HEARTBEAT broker_recovery=${broker_pod} attempt=${attempt}/180 old_uid=${broker_uid} current_uid=${current_uid:-missing} ready=${current_ready:-False}"
  fi
  sleep 5
done
wait "$writer_exec_pid"
test "$(client cat /tmp/failure-writer.rc)" = 0 || fail "sustained producer failed during single broker loss"
wait_pool_ready broker 3 || fail "broker pool did not recover"
assert_topic_healthy || fail "topic did not return to full ISR after broker loss"
consume_marker "broker-failure-${failure_run}-003000" kubeauto-broker-failure
pass "KAFKA-08 acks=all writes survived broker loss and recovered full ISR"

stage KAFKA-09 "controller quorum majority and safe rejection"
controller_pod="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=controller \
  -o jsonpath='{.items[0].metadata.name}')"
controller_uid="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$controller_pod" -o jsonpath='{.metadata.uid}')"
kubectl -n "$KAFKA_NAMESPACE" delete pod "$controller_pod" --wait=false >/dev/null
for attempt in $(seq 1 180); do
  current_uid="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$controller_pod" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
  current_ready="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$controller_pod" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  [[ -n "$current_uid" && "$current_uid" != "$controller_uid" && "$current_ready" == True ]] && break
  sleep 5
done
wait_pool_ready controller 3 || fail "single controller loss did not recover"
single_controller_marker="single-controller-$(date +%s)"
produce_marker "$single_controller_marker"
consume_marker "$single_controller_marker" kubeauto-controller-single

kubectl -n "$KAFKA_OPERATOR_NAMESPACE" scale deployment strimzi-cluster-operator --replicas=0 >/dev/null
kubectl -n "$KAFKA_OPERATOR_NAMESPACE" rollout status deployment/strimzi-cluster-operator --timeout=5m
mapfile -t controller_pods < <(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=controller -o name | sort)
[[ "${#controller_pods[@]}" -eq 3 ]] || fail "expected three controller pods before majority loss"
kubectl -n "$KAFKA_NAMESPACE" delete "${controller_pods[0]}" "${controller_pods[1]}" --wait=true >/dev/null
if client timeout --signal=TERM --kill-after=5s 30s /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server "$BOOTSTRAP" --command-config /tmp/admin.properties \
  --create --topic kubeauto-no-quorum --partitions 3 --replication-factor 3 >/tmp/no-quorum.out 2>&1; then
  fail "metadata mutation unexpectedly succeeded without KRaft majority"
fi
echo "KAFKA_CONTROLLER_MAJORITY_LOSS_SAFE_REJECTION=true"
kubectl -n "$KAFKA_OPERATOR_NAMESPACE" scale deployment strimzi-cluster-operator --replicas=2 >/dev/null
kubectl -n "$KAFKA_OPERATOR_NAMESPACE" rollout status deployment/strimzi-cluster-operator --timeout=10m
# With two KRaft controllers unavailable, the Kafka assembly reconciliation can
# remain blocked until quorum returns. Recreate only missing controller Pods
# from the StrimziPodSet's declared immutable template so the test can restore
# quorum without changing the production role or broker configuration.
for controller_name in $(kubectl -n "$KAFKA_NAMESPACE" get strimzipodset \
  "${KAFKA_CLUSTER}-controller" -o json | jq -r '.spec.pods[].metadata.name'); do
  if ! kubectl -n "$KAFKA_NAMESPACE" get pod "$controller_name" >/dev/null 2>&1; then
    kubectl -n "$KAFKA_NAMESPACE" get strimzipodset "${KAFKA_CLUSTER}-controller" -o json \
      | jq --arg name "$controller_name" \
        '.spec.pods[] | select(.metadata.name == $name)
         | del(.metadata.uid,.metadata.resourceVersion,.metadata.creationTimestamp,
               .metadata.managedFields,.metadata.ownerReferences)' \
      | kubectl apply -f - >/dev/null
  fi
done
wait_pool_ready controller 3 || fail "controller quorum did not recover"
client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" --command-config /tmp/admin.properties \
  --create --if-not-exists --topic kubeauto-quorum-recovered --partitions 3 --replication-factor 3 >/dev/null
client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" --command-config /tmp/admin.properties \
  --delete --topic kubeauto-quorum-recovered >/dev/null
pass "KAFKA-09 one controller retained quorum; majority loss rejected mutation and recovered"

stage KAFKA-10 "broker scale and Cruise Control rebalance"
rebalance_topic="kubeauto-rebalance-${run_id}"
client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
  --command-config /tmp/admin.properties --create --if-not-exists \
  --topic "$rebalance_topic" --partitions 6 --replication-factor 3 >/dev/null
: >/tmp/rebalance-expected.txt
for i in $(seq -w 1 200); do
  printf 'ordered-key:rebalance-%s\n' "$i" >>/tmp/rebalance-expected.txt
done
client /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server "$BOOTSTRAP" \
  --topic "$rebalance_topic" --producer.config /tmp/admin.properties \
  --property parse.key=true --property key.separator=: \
  --producer-property acks=all --producer-property enable.idempotence=true \
  </tmp/rebalance-expected.txt >/dev/null
baseline_hash="$(sort /tmp/rebalance-expected.txt | sha256sum | awk '{print $1}')"
kubectl -n "$KAFKA_NAMESPACE" patch kafkanodepool broker --type=merge -p '{"spec":{"replicas":4}}' >/dev/null
wait_pool_ready broker 4 || fail "broker scale-out and auto-rebalance did not converge"
broker_ids=0
for attempt in $(seq 1 60); do
  if ! description="$(client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --command-config /tmp/admin.properties --topic "$KAFKA_TOPIC" --describe \
    2>/tmp/kubeauto-kafka-rebalance-describe.err)"; then
    if (( attempt % 6 == 0 )); then
      echo "KAFKA_WAIT_HEARTBEAT cruise-control-rebalance=unavailable attempt=${attempt}/60"
      tail -n 8 /tmp/kubeauto-kafka-rebalance-describe.err || true
    fi
    sleep 5
    continue
  fi
  broker_ids="$(sed -n 's/.*Replicas: \([^[:space:]]*\).*/\1/p' <<<"$description" \
    | tr ',' '\n' | sort -u | sed '/^$/d' | wc -l || true)"
  [[ "$broker_ids" -eq 4 ]] && break
  if (( attempt % 6 == 0 )); then
    echo "KAFKA_WAIT_HEARTBEAT cruise-control-rebalance=${broker_ids}/4 attempt=${attempt}/60"
    kubectl -n "$KAFKA_NAMESPACE" get kafkarebalance -o wide 2>/dev/null || true
  fi
  sleep 5
done
[[ "$broker_ids" -eq 4 ]] || fail "Cruise Control did not distribute replicas across four brokers"
kubectl -n "$KAFKA_NAMESPACE" patch kafkanodepool broker --type=merge -p '{"spec":{"replicas":3}}' >/dev/null
wait_pool_ready broker 3 || fail "broker scale-in and auto-rebalance did not converge"
topic_healthy=false
for attempt in $(seq 1 60); do
  if assert_topic_healthy; then
    topic_healthy=true
    break
  fi
  if (( attempt % 6 == 0 )); then
    echo "KAFKA_WAIT_HEARTBEAT cruise-control-scale-in-isr=unhealthy attempt=${attempt}/60"
  fi
  sleep 5
done
[[ "$topic_healthy" == true ]] || fail "topic ISR unhealthy after broker scale-in"
rebalance_consumer_pass=false
for attempt in $(seq 1 6); do
  if client /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server "$BOOTSTRAP" --topic "$rebalance_topic" \
    --consumer.config /tmp/admin.properties --from-beginning --group "kubeauto-rebalance-hash-${attempt}" \
    --max-messages 200 --timeout-ms 60000 --property print.key=true --property key.separator=: \
    >/tmp/rebalance-raw.txt 2>/tmp/rebalance-consumer.err; then
    # Kafka guarantees order within a partition, not a global order across
    # partitions.  Cruise Control may change the inter-partition arrival
    # order while preserving every record, so compare the canonicalized set.
    grep '^ordered-key:rebalance-' /tmp/rebalance-raw.txt | head -n 200 \
      | sort >/tmp/rebalance-actual.txt || :
    if [[ "$(sha256sum /tmp/rebalance-actual.txt | awk '{print $1}')" == "$baseline_hash" \
      && "$(wc -l </tmp/rebalance-actual.txt)" -eq 200 ]]; then
      rebalance_consumer_pass=true
      break
    fi
  fi
  if (( attempt < 6 )); then
    echo "KAFKA_WAIT_HEARTBEAT cruise-control-data-check attempt=${attempt}/6"
    tail -n 8 /tmp/rebalance-consumer.err || true
    sleep 10
  fi
done
[[ "$rebalance_consumer_pass" == true ]] || fail "ordered data hash changed or consumption failed across rebalance"
pass "KAFKA-10 scale-out/in and Cruise Control rebalance preserved data and full ISR"

stage KAFKA-11 "bounded disk pressure and online PVC expansion"
test "$(kubectl get storageclass "$KAFKA_STORAGE_CLASS" -o jsonpath='{.allowVolumeExpansion}')" = true \
  || fail "StorageClass does not support online expansion: ${KAFKA_STORAGE_CLASS}"
pressure_pod="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "$KAFKA_NAMESPACE" exec "$pressure_pod" -c kafka -- bash -ceu '
  path=/var/lib/kafka/data/.kubeauto-storage-pressure
  test ! -e "$path"
  df -Pk /var/lib/kafka/data
  fallocate -l 512M "$path" 2>/dev/null || dd if=/dev/zero of="$path" bs=1M count=512 status=none
  test "$(stat -c %s "$path")" -eq 536870912
  df -Pk /var/lib/kafka/data
  rm -f "$path"
  test ! -e "$path"
'
storage_marker="storage-pressure-$(date +%s)"
produce_marker "$storage_marker"
consume_marker "$storage_marker" kubeauto-storage-pressure
kubectl -n "$KAFKA_NAMESPACE" patch kafkanodepool broker --type=merge -p '{"spec":{"storage":{"size":"6Gi"}}}' >/dev/null
for attempt in $(seq 1 180); do
  resized="$(kubectl -n "$KAFKA_NAMESPACE" get pvc -l strimzi.io/pool-name=broker -o json \
    | jq '[.items[] | select(.spec.resources.requests.storage == "6Gi") | select(.status.capacity.storage == "6Gi")] | length')"
  [[ "$resized" -ge 3 ]] && break
  if (( attempt % 12 == 0 )); then
    echo "KAFKA_WAIT_HEARTBEAT pvc_expansion=${resized}/3 attempt=${attempt}/180"
    kubectl -n "$KAFKA_NAMESPACE" get pvc -l strimzi.io/pool-name=broker -o wide
  fi
  sleep 5
done
[[ "${resized:-0}" -ge 3 ]] || fail "broker PVC expansion did not complete"
wait_pool_ready broker 3 || fail "Kafka did not remain Ready after PVC expansion"
pass "KAFKA-11 scoped capacity pressure recovered and three broker PVCs expanded online from 5Gi to 6Gi"

stage KAFKA-12 "SCRAM password and cluster CA rotation"
kubectl -n "$KAFKA_NAMESPACE" exec "$CLIENT_POD" -- cp /tmp/app.properties /tmp/old-app.properties
kubectl -n "$KAFKA_NAMESPACE" get secret "$KAFKA_PASSWORD_SECRET" -o jsonpath='{.data.password}' \
  | base64 -d >/tmp/kafka-old-password
chmod 0600 /tmp/kafka-old-password
OLD_APP_HASH="$(sha256sum /tmp/kafka-old-password | awk '{print $1}')"
NEW_APP_PASSWORD="$(openssl rand -base64 48 | tr -d '\n')"
printf '%s' "$NEW_APP_PASSWORD" >/tmp/kafka-new-password
chmod 0600 /tmp/kafka-new-password
NEW_APP_HASH="$(sha256sum /tmp/kafka-new-password | awk '{print $1}')"
kubectl -n "$KAFKA_NAMESPACE" create secret generic "$KAFKA_PASSWORD_SECRET" \
  --from-file=password=/tmp/kafka-new-password --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$KAFKA_NAMESPACE" label secret "$KAFKA_PASSWORD_SECRET" kubeauto.io/component=kafka-test --overwrite >/dev/null
unset NEW_APP_PASSWORD
for attempt in $(seq 1 90); do
  mounted_hash="$(client sha256sum /opt/kafka/gate/app/password | awk '{print $1}')"
  [[ "$mounted_hash" == "$NEW_APP_HASH" ]] && break
  sleep 2
done
[[ "${mounted_hash:-}" == "$NEW_APP_HASH" && "$NEW_APP_HASH" != "$OLD_APP_HASH" ]] \
  || fail "projected SCRAM password did not rotate"
client bash -ceu '
  password="$(cat /opt/kafka/gate/app/password)"
  ca_pem=""
  while IFS= read -r line; do ca_pem+="${line}\\n"; done < /opt/kafka/gate/ca/ca.crt
  umask 077
  cat >/tmp/app.properties <<EOF
security.protocol=SASL_SSL
bootstrap.servers=kafka-prod-kafka-bootstrap:9093
sasl.mechanism=SCRAM-SHA-512
sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="kafka-app" password="${password}";
ssl.truststore.type=PEM
ssl.truststore.certificates=${ca_pem}
client.dns.lookup=use_all_dns_ips
request.timeout.ms=10000
default.api.timeout.ms=15000
EOF
'
for attempt in $(seq 1 90); do
  if client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
    --command-config /tmp/app.properties --describe --topic "$KAFKA_TOPIC" >/dev/null 2>&1 \
    && ! client /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" \
      --command-config /tmp/old-app.properties --list >/dev/null 2>&1; then
    password_rotated=true
    break
  fi
  sleep 2
done
[[ "${password_rotated:-false}" == true ]] || fail "new SCRAM password was not active or old password remained valid"
rm -f /tmp/kafka-old-password /tmp/kafka-new-password

old_ca_hash="$(kubectl -n "$KAFKA_NAMESPACE" get secret "${KAFKA_CLUSTER}-cluster-ca-cert" \
  -o jsonpath='{.data.ca\.crt}' | base64 -d | sha256sum | awk '{print $1}')"
before_roll="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/cluster="$KAFKA_CLUSTER" \
  -o json | jq -S '[.items[] | [.metadata.name,.metadata.uid]] | sort')"
kubectl -n "$KAFKA_NAMESPACE" annotate secret "${KAFKA_CLUSTER}-cluster-ca-cert" \
  strimzi.io/force-renew=true --overwrite >/dev/null
for attempt in $(seq 1 240); do
  new_ca_hash="$(kubectl -n "$KAFKA_NAMESPACE" get secret "${KAFKA_CLUSTER}-cluster-ca-cert" \
    -o jsonpath='{.data.ca\.crt}' 2>/dev/null | base64 -d | sha256sum | awk '{print $1}')"
  current_roll="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/cluster="$KAFKA_CLUSTER" \
    -o json | jq -S '[.items[] | [.metadata.name,.metadata.uid]] | sort')"
  kafka_ready="$(kubectl -n "$KAFKA_NAMESPACE" get kafka "$KAFKA_CLUSTER" \
    -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [[ "$new_ca_hash" != "$old_ca_hash" && "$current_roll" != "$before_roll" && "$kafka_ready" == True ]]; then
    break
  fi
  if (( attempt % 12 == 0 )); then
    echo "KAFKA_WAIT_HEARTBEAT ca_rotation attempt=${attempt}/240 cert_changed=$([[ "$new_ca_hash" != "$old_ca_hash" ]] && echo true || echo false) kafka=${kafka_ready:-missing}"
  fi
  sleep 5
done
[[ "${new_ca_hash:-}" != "$old_ca_hash" && "${current_roll:-}" != "$before_roll" ]] \
  || fail "cluster CA force renewal did not complete"
wait_kafka_ready 4.3.1 || fail "Kafka did not return Ready after cluster CA rotation"
ca_marker="ca-rotation-$(date +%s)"
produce_marker "$ca_marker"
consume_marker "$ca_marker" kubeauto-ca-rotation
pass "KAFKA-12 SCRAM new credential succeeded, old failed, and cluster CA renewal rolled safely"

stage KAFKA-13 "metrics and operational signal collection"
broker_metrics_pod="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker -o jsonpath='{.items[0].metadata.name}')"
kubectl get --raw "/api/v1/namespaces/${KAFKA_NAMESPACE}/pods/${broker_metrics_pod}:9404/proxy/metrics" \
  >/tmp/kafka-broker-metrics
grep -Eq '^kafka_server_replicamanager_(underreplicatedpartitions|under_replicated_partitions)' \
  /tmp/kafka-broker-metrics || fail "broker under-replicated partition metric missing"
exporter_pod="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/name="${KAFKA_CLUSTER}-kafka-exporter" \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl get --raw "/api/v1/namespaces/${KAFKA_NAMESPACE}/pods/${exporter_pod}:9404/proxy/metrics" \
  >/tmp/kafka-exporter-metrics
grep -Eq '^kafka_consumergroup_(lag|current_offset)' /tmp/kafka-exporter-metrics \
  || fail "consumer lag metrics missing"
operator_pod="$(kubectl -n "$KAFKA_OPERATOR_NAMESPACE" get pod -l name=strimzi-cluster-operator \
  -o jsonpath='{.items[0].metadata.name}')"
operator_metrics="$(kubectl get --raw \
  "/api/v1/namespaces/${KAFKA_OPERATOR_NAMESPACE}/pods/${operator_pod}:8080/proxy/metrics")"
operator_metric_signal="$(grep -E '^(strimzi_|jvm_info|vertx_)' <<<"$operator_metrics" | head -n 1 || true)"
[[ -n "$operator_metric_signal" ]] || fail "Strimzi Operator metrics endpoint returned no standard runtime signal"
echo "KAFKA_OPERATOR_METRICS_SIGNAL ${operator_metric_signal%% *}"
pass "KAFKA-13 broker, consumer lag and Operator metrics collected after fault recovery"

stage KAFKA-14 "fixed producer and consumer performance baseline"
client timeout --signal=TERM --kill-after=10s 10m /opt/kafka/bin/kafka-producer-perf-test.sh \
  --topic "$KAFKA_TOPIC" --num-records 50000 --record-size 1024 --throughput -1 \
  --producer.config /tmp/app.properties >/tmp/kafka-producer-perf.txt 2>&1
grep -Eq '50000 records sent|records/sec' /tmp/kafka-producer-perf.txt || fail "producer performance result missing"
client timeout --signal=TERM --kill-after=10s 10m /opt/kafka/bin/kafka-consumer-perf-test.sh \
  --bootstrap-server "$BOOTSTRAP" --topic "$KAFKA_TOPIC" --messages 50000 \
  --group kubeauto-performance --consumer.config /tmp/app.properties \
  >/tmp/kafka-consumer-perf.txt 2>&1
grep -Eq 'MB.sec|MB/sec|Messages' /tmp/kafka-consumer-perf.txt || fail "consumer performance result missing"
producer_summary="$(tail -n 1 /tmp/kafka-producer-perf.txt | tr -s ' ')"
consumer_summary="$(tail -n 1 /tmp/kafka-consumer-perf.txt | tr -s ' ')"
echo "KAFKA_PERFORMANCE_BASELINE producer=${producer_summary}"
echo "KAFKA_PERFORMANCE_BASELINE consumer=${consumer_summary}"
pass "KAFKA-14 fixed 50k x 1KiB producer and consumer baseline completed without silent errors"

stage KAFKA-15 "Drain Cleaner, PDB and node drain"
test "$(kubectl get validatingwebhookconfiguration strimzi-drain-cleaner \
  -o jsonpath='{.webhooks[0].failurePolicy}')" = Fail
test "$(kubectl -n "$KAFKA_NAMESPACE" get pdb -l strimzi.io/cluster="$KAFKA_CLUSTER" --no-headers | wc -l)" -ge 1
test "$(kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" get deployment strimzi-drain-cleaner \
  -o jsonpath='{.status.readyReplicas}')" = 2
cleaner_nodes="$(kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" get pod -l app=strimzi-drain-cleaner \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u)"
test "$(grep -c . <<<"$cleaner_nodes")" = 2
cleaner_endpoints="$(kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" get endpointslice \
  -l kubernetes.io/service-name=strimzi-drain-cleaner -o json \
  | jq '[.items[].endpoints[] | select(.conditions.ready == true)] | length')"
test "$cleaner_endpoints" -ge 2
client_node="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$CLIENT_POD" -o jsonpath='{.spec.nodeName}')"
drain_node=""
while IFS= read -r candidate; do
  [[ "$candidate" == "$client_node" ]] && continue
  grep -qxF "$candidate" <<<"$cleaner_nodes" && continue
  drain_node="$candidate"
  break
done < <(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u)
if [[ -z "$drain_node" ]]; then
  drain_node="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker \
    -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' \
    | awk -v client="$client_node" '$0 != client {print; exit}')"
fi
[[ -n "$drain_node" ]] || drain_node="$(kubectl -n "$KAFKA_NAMESPACE" get pod \
  -l strimzi.io/pool-name=broker -o jsonpath='{.items[0].spec.nodeName}')"
initial_broker="$(kubectl -n "$KAFKA_NAMESPACE" get pod -l strimzi.io/pool-name=broker \
  --field-selector "spec.nodeName=${drain_node}" -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$initial_broker" ]] || fail "selected drain node has no Kafka broker: ${drain_node}"
initial_broker_uid="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$initial_broker" \
  -o jsonpath='{.metadata.uid}')"
printf '%s\n' "$drain_node" >"$CORDON_STATE"
drain_completed=false
drain_refusal_observed=false
for drain_attempt in $(seq 1 6); do
  before_uids="$(kubectl -n "$KAFKA_NAMESPACE" get pod \
    -l 'strimzi.io/pool-name in (broker,controller)' \
    --field-selector "spec.nodeName=${drain_node}" -o json \
    | jq -r '.items[] | "\(.metadata.name)=\(.metadata.uid)"' | sort)"
  drain_log="/tmp/kafka-node-drain-${drain_attempt}.log"
  set +e
  kubectl drain "$drain_node" --ignore-daemonsets --delete-emptydir-data \
    --timeout=10m >"$drain_log" 2>&1
  drain_rc=$?
  set -e
  cat "$drain_log"
  if grep -Fq 'will be rolled by the Strimzi Cluster Operator' "$drain_log"; then
    drain_refusal_observed=true
    echo "KAFKA_DRAIN_CLEANER_REFUSAL_PASS attempt=${drain_attempt} node=${drain_node}"
  fi
  if [[ "$drain_rc" -eq 0 ]]; then
    drain_completed=true
    break
  fi
  [[ "$drain_refusal_observed" == true ]] \
    || fail "node drain failed without the expected Drain Cleaner denial"

  progress=false
  for progress_attempt in $(seq 1 180); do
    current_uids="$(kubectl -n "$KAFKA_NAMESPACE" get pod \
      -l 'strimzi.io/pool-name in (broker,controller)' \
      --field-selector "spec.nodeName=${drain_node}" -o json \
      | jq -r '.items[] | "\(.metadata.name)=\(.metadata.uid)"' | sort)"
    if [[ "$current_uids" != "$before_uids" ]]; then
      progress=true
      break
    fi
    if (( progress_attempt % 12 == 0 )); then
      echo "KAFKA_DRAIN_WAIT attempt=${drain_attempt} elapsed=$((progress_attempt * 5))s node=${drain_node}"
    fi
    sleep 5
  done
  [[ "$progress" == true ]] || fail "Drain Cleaner denial did not trigger a Kafka Pod roll"
  wait_pool_ready broker 3 || fail "broker pool did not recover after Drain Cleaner denial"
  wait_pool_ready controller 3 || fail "controller pool did not recover after Drain Cleaner denial"
  kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" rollout status \
    deployment/strimzi-drain-cleaner --timeout=5m
  cleaner_endpoints="$(kubectl -n "$KAFKA_DRAIN_CLEANER_NAMESPACE" get endpointslice \
    -l kubernetes.io/service-name=strimzi-drain-cleaner -o json \
    | jq '[.items[].endpoints[] | select(.conditions.ready == true)] | length')"
  test "$cleaner_endpoints" -ge 2
done
[[ "$drain_completed" == true ]] || fail "node drain did not complete after safe Kafka rolls"
[[ "$drain_refusal_observed" == true ]] \
  || fail "node drain completed without exercising the Drain Cleaner denial path"
current_broker_uid="$(kubectl -n "$KAFKA_NAMESPACE" get pod "$initial_broker" \
  -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
[[ "$current_broker_uid" != "$initial_broker_uid" ]] \
  || fail "selected broker was not rolled during node drain"
wait_pool_ready broker 3 || fail "broker pool did not recover during node drain"
wait_pool_ready controller 3 || fail "controller pool did not recover during node drain"
kubectl uncordon "$drain_node" >/dev/null
: >"$CORDON_STATE"
if [[ "$(kubectl -n "$KAFKA_NAMESPACE" get pod "$CLIENT_POD" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)" != True ]]; then
  create_client_pod
fi
drain_marker="drain-recovery-$(date +%s)"
produce_marker "$drain_marker"
consume_marker "$drain_marker" kubeauto-drain-recovery
assert_topic_healthy || fail "topic ISR unhealthy after node drain"
pass "KAFKA-15 two-phase Drain Cleaner denial, Operator roll, PDB and node drain recovered safely"

stage KAFKA-17 "phase-one disaster recovery boundary"
echo "KAFKA-17_NOT_APPLICABLE reason=MirrorMaker2_is_outside_phase_one_core_scope"

kubectl -n "$KAFKA_NAMESPACE" get kafka,kafkanodepool,kafkatopic,kafkauser,pod,pvc,svc -o wide
echo KAFKA_FULL_GATE_PASS

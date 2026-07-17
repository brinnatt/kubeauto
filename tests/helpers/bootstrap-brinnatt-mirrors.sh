#!/bin/bash
# Bootstrap essential brinnatt/* images before CI dual-push.
# Prefer talkedu → upstream → dockerhub. Non-fatal per image unless BOOTSTRAP_STRICT=1.
#
# BOOTSTRAP_MODE:
#   defaults   — only get_default_images + registry (for jumper / calico clusters)
#   essential  — defaults + Tier2 CNI (default)
#   full       — essential + common addons
set -uo pipefail

STRICT="${BOOTSTRAP_STRICT:-0}"
MODE="${BOOTSTRAP_MODE:-essential}"
FAILS=0

pull_tag() {
  local upstream="$1"
  local target="$2"
  local name_tag="${target#brinnatt/}"
  local talkedu="hub.talkedu.cn/kubeauto/${name_tag}"
  local t_talk="${BOOTSTRAP_TALEDU_TIMEOUT:-60}"
  local t_up="${BOOTSTRAP_UPSTREAM_TIMEOUT:-240}"
  local t_hub="${BOOTSTRAP_HUB_TIMEOUT:-120}"

  if docker image inspect "$target" >/dev/null 2>&1; then
    echo "[skip] $target"
    return 0
  fi

  echo "[pull] $target (talkedu → upstream → dockerhub)"
  if timeout "$t_talk" docker pull "$talkedu"; then
    docker tag "$talkedu" "$target"
    return 0
  fi
  if timeout "$t_up" docker pull "$upstream"; then
    docker tag "$upstream" "$target"
    return 0
  fi
  if [[ "$upstream" != "$target" ]] && timeout "$t_hub" docker pull "$target"; then
    return 0
  fi
  echo "[WARN] failed to materialize $target"
  FAILS=$((FAILS + 1))
  [[ "$STRICT" == "1" ]] && return 1
  return 0
}

pull_tag "calico/cni:v3.28.4" "brinnatt/calico-cni:v3.28.4"
pull_tag "calico/node:v3.28.4" "brinnatt/calico-node:v3.28.4"
pull_tag "calico/kube-controllers:v3.28.4" "brinnatt/calico-kube-controllers:v3.28.4"
pull_tag "coredns/coredns:1.12.4" "brinnatt/coredns:1.12.4"
pull_tag "registry:2" "brinnatt/registry:2"
pull_tag "brinnatt/pause:3.10" "brinnatt/pause:3.10"
pull_tag "brinnatt/metrics-server:v0.8.0" "brinnatt/metrics-server:v0.8.0"
pull_tag "brinnatt/k8s-dns-node-cache:1.26.4" "brinnatt/k8s-dns-node-cache:1.26.4"

if [[ "$MODE" == "essential" || "$MODE" == "full" ]]; then
  pull_tag "quay.io/cilium/cilium:v1.19.5" "brinnatt/cilium:v1.19.5"
  pull_tag "quay.io/cilium/operator-generic:v1.19.5" "brinnatt/cilium-operator-generic:v1.19.5"
  pull_tag "flannel/flannel:v0.28.4" "brinnatt/flannel:v0.28.4"
  pull_tag "flannel/flannel-cni-plugin:v1.8.0-flannel1" "brinnatt/flannel-cni-plugin:v1.8.0-flannel1"
  pull_tag "cloudnativelabs/kube-router:v1.5.4" "brinnatt/kube-router:v1.5.4"
  pull_tag "kubeovn/kube-ovn:v1.11.5" "brinnatt/kube-ovn:v1.11.5"
fi

if [[ "$MODE" == "full" ]]; then
  # Optional Hubble UI (not required for default cilium / addon smoke)
  if [[ "${BOOTSTRAP_HUBBLE:-0}" == "1" ]]; then
    pull_tag "quay.io/cilium/hubble-relay:v1.19.5" "brinnatt/hubble-relay:v1.19.5"
    pull_tag "quay.io/cilium/hubble-ui:v0.13.5" "brinnatt/hubble-ui:v0.13.5"
    pull_tag "quay.io/cilium/hubble-ui-backend:v0.13.5" "brinnatt/hubble-ui-backend:v0.13.5"
  fi
  # dashboard
  pull_tag "kubernetesui/dashboard-api:1.14.0" "brinnatt/dashboard-api:1.14.0"
  pull_tag "kubernetesui/dashboard-auth:1.4.0" "brinnatt/dashboard-auth:1.4.0"
  pull_tag "kubernetesui/dashboard-web:1.7.0" "brinnatt/dashboard-web:1.7.0"
  pull_tag "kubernetesui/dashboard-metrics-scraper:1.2.2" "brinnatt/dashboard-metrics-scraper:1.2.2"
  pull_tag "kong:3.9" "brinnatt/kong:3.9"
  # prometheus stack
  pull_tag "grafana/grafana:12.0.2" "brinnatt/grafana:12.0.2"
  pull_tag "quay.io/kiwigrid/k8s-sidecar:1.30.5" "brinnatt/k8s-sidecar:1.30.5"
  pull_tag "quay.io/prometheus/prometheus:v3.4.2" "brinnatt/prometheus:v3.4.2"
  pull_tag "quay.io/prometheus/alertmanager:v0.28.1" "brinnatt/alertmanager:v0.28.1"
  pull_tag "quay.io/prometheus/node-exporter:v1.9.1" "brinnatt/node-exporter:v1.9.1"
  pull_tag "quay.io/prometheus-operator/prometheus-operator:v0.83.0" "brinnatt/prometheus-operator:v0.83.0"
  pull_tag "quay.io/prometheus-operator/prometheus-config-reloader:v0.83.0" "brinnatt/prometheus-config-reloader:v0.83.0"
  pull_tag "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.16.0" "brinnatt/kube-state-metrics:v2.16.0"
  pull_tag "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.6.0" "brinnatt/kube-webhook-certgen:v1.6.0"
  # storage / openebs
  pull_tag "rancher/local-path-provisioner:v0.0.31" "brinnatt/local-path-provisioner:v0.0.31"
  pull_tag "openebs/provisioner-localpv:4.3.0" "brinnatt/provisioner-localpv:4.3.0"
  pull_tag "openebs/linux-utils:4.2.0" "brinnatt/linux-utils:4.2.0"
  pull_tag "openebs/lvm-driver:1.7.0" "brinnatt/lvm-driver:1.7.0"
  # Built in ext-images from dl.k8s.io; talkedu/brinnatt first, legacy Bitnami as last resort.
  pull_tag "bitnamilegacy/kubectl:1.33.4" "brinnatt/openebs-kubectl:1.33.6"
  pull_tag "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.13.0" "brinnatt/csi-node-driver-registrar:v2.13.0"
  pull_tag "registry.k8s.io/sig-storage/csi-resizer:v1.11.2" "brinnatt/csi-resizer:v1.11.2"
  pull_tag "registry.k8s.io/sig-storage/csi-snapshotter:v7.0.0" "brinnatt/csi-snapshotter:v7.0.0"
  pull_tag "registry.k8s.io/sig-storage/csi-provisioner:v5.2.0" "brinnatt/csi-provisioner:v5.2.0"
  pull_tag "registry.k8s.io/sig-storage/snapshot-controller:v8.3.0" "brinnatt/snapshot-controller:v8.3.0"
  # minio / nacos / rocketmq
  pull_tag "quay.io/minio/operator:v7.1.1" "brinnatt/minio-operator:v7.1.1"
  pull_tag "quay.io/minio/operator-sidecar:v7.0.1" "brinnatt/minio-operator-sidecar:v7.0.1"
  pull_tag "quay.io/minio/minio:RELEASE.2025-04-08T15-41-24Z" "brinnatt/minio:RELEASE.2025-04-08T15-41-24Z"
  pull_tag "nacos/nacos-server:v2.4.3" "brinnatt/nacos-server:v2.4.3"
  pull_tag "nacos/nacos-peer-finder-plugin:1.1" "brinnatt/nacos-peer-finder-plugin:1.1"
  pull_tag "apache/rocketmq-operator:latest" "brinnatt/rocketmq-operator:latest"
  pull_tag "apacherocketmq/rocketmq-broker:4.5.0-alpine-operator-0.3.0" "brinnatt/rocketmq-broker:4.5.0-alpine-operator-0.3.0"
  pull_tag "apacherocketmq/rocketmq-nameserver:4.5.0-alpine-operator-0.3.0" "brinnatt/rocketmq-nameserver:4.5.0-alpine-operator-0.3.0"
  pull_tag "apacherocketmq/rocketmq-console:2.0.0" "brinnatt/rocketmq-console:2.0.0"
fi

echo "BOOTSTRAP_BRINATT_OK mode=$MODE fails=$FAILS"
[[ "$STRICT" == "1" && "$FAILS" -gt 0 ]] && exit 1
exit 0

from dataclasses import dataclass, field
import os
from common.os import SystemProbe


@dataclass
class KubeConstant:
    # kubernetes ecosystem components version
    v_docker: str = field(default="28.5.2", metadata={
        "refer_bin": "https://docs.docker.com/engine/install/binaries/",
        "refer_docs": "https://docs.docker.com/engine/release-notes/28/",
        "description": "Engine 28.5.2 (28.x latest stable); CLI plugins paired via v_docker_compose / v_docker_buildx",
    })
    v_docker_compose: str = field(default="2.40.3", metadata={
        "refer_github": "https://github.com/docker/compose/releases",
        "description": "Compose v2 CLI plugin paired with Engine 28.5.x (not v5.x, which targets Engine 29)",
    })
    v_docker_buildx: str = field(default="0.29.1", metadata={
        "refer_github": "https://github.com/docker/buildx/releases",
        "description": "Buildx CLI plugin paired with Engine 28.5.x (docker-ce packaging release train)",
    })
    v_docker_registry: str = field(default="2", metadata={
        "refer_hub": "https://hub.docker.com/_/registry",
        "refer_docs": "https://distribution.github.io/distribution/"
    })
    # Required when CONTAINER_RUNTIME=docker on K8s >=1.24 (dockershim removed).
    # Official: https://github.com/Mirantis/cri-dockerd
    v_cri_dockerd: str = field(default="0.3.26", metadata={
        "refer_github": "https://github.com/Mirantis/cri-dockerd/releases",
        "description": "cri-dockerd CRI shim for dockerd; pairs with Engine 28.x / K8s 1.33",
    })
    v_kubeauto: str = field(default="v0.1.1", metadata={
        "refer_github": "https://github.com/brinnatt"
    })
    v_talkedu_registry: str = field(default="hub.talkedu.cn/kubeauto", metadata={
        "description": (
            "CN private registry for brinnatt/* images. "
            "Pull order: hub.talkedu.cn/kubeauto/<name> first, then Docker Hub brinnatt/<name>. "
            "CI dual-push path must stay hub.talkedu.cn/kubeauto/<name>:<tag> across all dockerfile projects."
        ),
    })
    v_k8s_bin: str = field(default="v1.33.6", metadata={
        "refer_all": "https://kubernetes.io/releases/download/",
        "refer_bin": "https://www.downloadkubernetes.com/",
        "refer_old": "https://github.com/kubernetes/kubernetes/tree/master/CHANGELOG",
    })
    v_extra_bin: str = field(default="1.13.1", metadata={
        "refer_github": "https://github.com/brinnatt/dockerfile-kubeauto-ext-bin",
    })
    v_extra_bin_sp1: str = field(default="1.3.1", metadata={
        "refer_github": "https://github.com/brinnatt/dockerfile-kubeauto-ext-bin-sp1",
        "description": "Supplement package pulled into kubeauto-ext-bin build",
    })
    v_harbor: str = field(default="v2.13.0", metadata={
        "refer_image": "https://github.com/wise2c-devops/build-harbor-aarch64",
        "description": "None-official"
    })
    # Pack / component image pins (brinnatt/* dual-publish tags)
    v_kube_state_metrics: str = field(default="v2.16.0")
    v_webhook_certgen: str = field(default="v1.6.0")
    v_ingress_nginx_controller: str = field(default="v1.13.0")
    # v1.3.1: Node 20 + pinned json-server@0.17.4 (v1.3.0 used Node 18.10 + unpinned npm → CrashLoop)
    v_json_mock: str = field(default="v1.3.1")
    v_alpine_curl: str = field(default="v7.85.0")
    v_snapshot_controller: str = field(default="v8.3.0")
    v_csi_node_driver_registrar: str = field(default="v2.13.0")
    v_csi_provisioner: str = field(default="v5.2.0")
    v_csi_resizer: str = field(default="v1.11.2")
    v_csi_snapshotter: str = field(default="v7.0.0")

    @property
    def k8s_bin_pack_tags(self):
        """k8s-bin image tags published by pack CI (includes current default)."""
        return (self.v_k8s_bin,)
    v_calico: str = field(default="v3.28.4", metadata={
        "refer_github": "https://github.com/projectcalico/calico",
        "refer_docs": "https://docs.tigera.io/calico/latest/about/"
    })
    v_coredns: str = field(default="1.12.4", metadata={
        "refer_github": "https://github.com/coredns/coredns",
        "refer_docs": "https://coredns.io/"
    })
    v_dnsnodecache: str = field(default="1.26.4", metadata={
        "refer_github": "https://github.com/kubernetes/kubernetes/blob/master/cluster/addons/dns/nodelocaldns/nodelocaldns.yaml",
        "refer_docs": "https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/"
    })
    v_dashboard: str = field(default="7.14.0", metadata={
        "refer_github": "https://github.com/kubernetes/dashboard",
    })
    v_dashboardmetricsscraper: str = field(default="1.2.2", metadata={
        "refer_github": "https://github.com/kubernetes-sigs/dashboard-metrics-scraper"
    })
    v_metricsserver: str = field(default="v0.8.0", metadata={
        "refer_github": "https://github.com/kubernetes-sigs/metrics-server",
        "refer_docs": "https://kubernetes-sigs.github.io/metrics-server/"
    })
    v_pause: str = field(default="3.10", metadata={
        "refer_github": "https://github.com/kubernetes/kubernetes/tree/master/build/pause",
        "refer_none_official_docs": "https://k8s.iswbm.com/c02/p02_learn-kubernetes-pod-via-pause-container.html"
    })
    v_flannel: str = field(default="v0.28.4", metadata={
        "refer_github": "https://github.com/flannel-io/flannel/releases",
        "description": "Latest stable v0.28.4 (2026-04); pin flannel_ver in cluster config if mirror lags",
    })
    v_flannel_cni: str = field(default="v1.8.0-flannel1", metadata={
        "refer_github": "https://github.com/flannel-io/flannel-cni-plugin/releases",
    })
    v_cilium: str = field(default="v1.19.5", metadata={
        "refer_github": "https://github.com/cilium/cilium/releases",
        "refer_docs": "https://docs.cilium.io/en/stable/network/kubernetes/compatibility/",
        "description": "Latest stable v1.19.5; K8s 1.33 compatible",
    })
    v_cilium_hubble_ui: str = field(default="v0.13.5", metadata={
        "refer_github": "https://github.com/cilium/hubble-ui/releases",
        "refer_quay": "https://quay.io/repository/cilium/hubble-ui?tab=tags",
        "description": (
            "Hubble UI v0.13.5 paired with Cilium 1.19.x per upstream chart. "
            "Official images are on quay.io/cilium/* (not docker.io/cilium for this tag)."
        ),
    })
    v_kuberouter: str = field(default="v1.5.4", metadata={
        "refer_github": "https://github.com/cloudnativelabs/kube-router"
    })
    v_kubeovn: str = field(default="v1.11.5", metadata={
        "refer_github": "https://github.com/kubeovn/kube-ovn"
    })
    v_localpathprovisioner: str = field(default="v0.0.31", metadata={
        "refer_github": "https://github.com/rancher/local-path-provisioner"
    })
    v_nfsprovisioner: str = field(default="v4.0.2", metadata={
        "refer_github": "https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner"
    })
    v_promchart: str = field(default="75.7.0", metadata={
        "refer_github": "https://github.com/prometheus/prometheus",
        "refer_docs": "https://prometheus.io/",
        "refer_helm": "https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack"
    })
    v_miniooperator: str = field(default="7.1.1", metadata={
        "refer_docs": "https://docs.min.io/enterprise/aistor-object-store/installation/kubernetes/"
    })
    v_openebs: str = field(default="4.3.2", metadata={
        "refer_docs": "https://openebs.io/docs/quickstart-guide/installation",
    })
    v_ingressnginx: str = field(default="4.13.0", metadata={
        "refer_docs": "https://kubernetes.github.io/ingress-nginx/deploy/",
    })

    # path for storing some important files
    BASE_PATH: str = field(default="/usr/local/kubeauto", metadata={
        "description": "This basic path stores all kubeauto files"
    })
    IMAGE_DIR: str = field(default="/usr/local/kubeauto/down", metadata={
        "description": "This path stores image files"
    })
    KUBE_BIN_DIR: str = field(default="/usr/local/kubeauto/kube-bin", metadata={
        "description": "This path stores binaries"
    })
    EXTRA_BIN_DIR: str = field(default="/usr/local/kubeauto/extra-bin", metadata={
        "description": "This path stores extra binaries"
    })
    DOCKER_BIN_DIR: str = field(default="/usr/local/kubeauto/docker-bin", metadata={
        "description": "This path stores docker binaries"
    })
    DOCKER_PROXY_DIR: str = field(default="/etc/systemd/system/docker.service.d", metadata={
        "description": "This path is used to configure docker proxies"
    })
    SYS_BIN_DIR: str = field(default="/usr/local/bin", metadata={
        "description": "This path stores system binaries symlink to k8s real binaries"
    })

    # path specifically for storing app data
    BASE_DATA_PATH: str = field(default="/data", metadata={
        "description": "This path stores app data"
    })

    # path specifically for storing temporary files removed after copied to somewhere
    TEMP_PATH: str = field(default="/tmp", metadata={
        "description": "This path stores temporary binaries"
    })

    def __post_init__(self):
        """用于 @dataclass 自动生成的 __init__ 后执行额外逻辑"""
        base_override = os.environ.get("KUBEAUTO_BASE_PATH")
        if base_override:
            self.BASE_PATH = base_override.rstrip("/")
            self.IMAGE_DIR = f"{self.BASE_PATH}/down"
            self.KUBE_BIN_DIR = f"{self.BASE_PATH}/kube-bin"
            self.EXTRA_BIN_DIR = f"{self.BASE_PATH}/extra-bin"
            self.DOCKER_BIN_DIR = f"{self.BASE_PATH}/docker-bin"
        self.arch = SystemProbe().system_info["machine"]

    _DOCKER_PLUGIN_ARCH = {
        "x86_64": {"compose": "x86_64", "buildx": "amd64"},
        "aarch64": {"compose": "aarch64", "buildx": "arm64"},
    }

    def docker_bin_url(self, version):
        url = f"https://repo.huaweicloud.com/docker-ce/linux/static/stable/{self.arch}/docker-{version}.tgz"
        return url

    def _docker_plugin_arch(self) -> dict:
        suffixes = self._DOCKER_PLUGIN_ARCH.get(self.arch)
        if not suffixes:
            raise RuntimeError(f"Unsupported architecture for Docker CLI plugins: {self.arch}")
        return suffixes

    def docker_compose_bin_url(self, version: str) -> str:
        suffix = self._docker_plugin_arch()["compose"]
        return (
            f"https://v6.gh-proxy.org/https://github.com/docker/compose/releases/download/v{version}/"
            f"docker-compose-linux-{suffix}"
        )

    def docker_buildx_bin_url(self, version: str) -> str:
        suffix = self._docker_plugin_arch()["buildx"]
        return (
            f"https://v6.gh-proxy.org/https://github.com/docker/buildx/releases/download/v{version}/"
            f"buildx-v{version}.linux-{suffix}"
        )

    def cri_dockerd_bin_url(self, version: str) -> str:
        """Mirantis cri-dockerd static tarball (amd64/arm64)."""
        arch = "amd64" if self.arch in ("x86_64", "amd64") else "arm64"
        return (
            f"https://v6.gh-proxy.org/https://github.com/Mirantis/cri-dockerd/releases/download/"
            f"v{version}/cri-dockerd-{version}.{arch}.tgz"
        )

    @property
    def component_images(self):
        """Extra component images for `kubecli download -E <component>`.

        All entries must be brinnatt/<name>:<tag> so pull order is:
          1. hub.talkedu.cn/kubeauto/<name>:<tag>
          2. Docker Hub brinnatt/<name>:<tag>
        Roles deploy from registry.talkschool.cn:5000/brinnatt/<name>:<tag>.
        """
        return {
            "cilium": [
                f"brinnatt/cilium:{self.v_cilium}",
                f"brinnatt/cilium-operator-generic:{self.v_cilium}",
            ],
            # Optional: enable with cilium_hubble_enabled / cilium_hubble_ui_enabled
            "cilium-hubble": [
                f"brinnatt/hubble-relay:{self.v_cilium}",
                f"brinnatt/hubble-ui-backend:{self.v_cilium_hubble_ui}",
                f"brinnatt/hubble-ui:{self.v_cilium_hubble_ui}",
            ],
            "flannel": [
                f"brinnatt/flannel:{self.v_flannel}",
                f"brinnatt/flannel-cni-plugin:{self.v_flannel_cni}",
            ],
            "dashboard": [
                "brinnatt/dashboard-api:1.14.0",
                "brinnatt/dashboard-auth:1.4.0",
                f"brinnatt/dashboard-metrics-scraper:{self.v_dashboardmetricsscraper}",
                "brinnatt/dashboard-web:1.7.0",
                "brinnatt/kong:3.9",
            ],
            "minio": [
                f"brinnatt/minio-operator:v{self.v_miniooperator}",
                "brinnatt/minio-operator-sidecar:v7.0.1",
                "brinnatt/minio:RELEASE.2025-04-08T15-41-24Z",
            ],
            "nacos": [
                "brinnatt/nacos-server:v2.4.3",
                "brinnatt/nacos-peer-finder-plugin:1.1",
            ],
            "openebs": [
                f"brinnatt/openebs-kubectl:{self.v_k8s_bin.lstrip('v')}",
                # OpenEBS umbrella chart 4.3.2 ships localpv-provisioner image 4.3.0 (no :4.3.2 tag upstream).
                "brinnatt/provisioner-localpv:4.3.0",
                "brinnatt/linux-utils:4.2.0",
                "brinnatt/lvm-driver:1.7.0",
                f"brinnatt/csi-node-driver-registrar:{self.v_csi_node_driver_registrar}",
                f"brinnatt/csi-resizer:{self.v_csi_resizer}",
                f"brinnatt/csi-snapshotter:{self.v_csi_snapshotter}",
                f"brinnatt/csi-provisioner:{self.v_csi_provisioner}",
                f"brinnatt/snapshot-controller:{self.v_snapshot_controller}",
            ],
            "rocketmq": [
                "brinnatt/rocketmq-operator:latest",
                "brinnatt/rocketmq-broker:4.5.0-alpine-operator-0.3.0",
                "brinnatt/rocketmq-nameserver:4.5.0-alpine-operator-0.3.0",
                "brinnatt/rocketmq-console:2.0.0",
            ],
            "ingress-nginx": [
                f"brinnatt/ingress-nginx-controller:{self.v_ingress_nginx_controller}",
                f"brinnatt/kube-webhook-certgen:{self.v_webhook_certgen}",
            ],
            "kube-ovn": [
                f"brinnatt/kube-ovn:{self.v_kubeovn}",
            ],
            "kube-router": [
                f"brinnatt/kube-router:{self.v_kuberouter}",
            ],
            "local-path-provisioner": [
                f"brinnatt/local-path-provisioner:{self.v_localpathprovisioner}",
            ],
            "network-check": [
                f"brinnatt/json-mock:{self.v_json_mock}",
                f"brinnatt/alpine-curl:{self.v_alpine_curl}",
            ],
            "nfs-provisioner": [
                f"brinnatt/nfs-subdir-external-provisioner:{self.v_nfsprovisioner}",
            ],
            "prometheus": [
                f"brinnatt/kube-state-metrics:{self.v_kube_state_metrics}",
                f"brinnatt/kube-webhook-certgen:{self.v_webhook_certgen}",
                "brinnatt/grafana:12.0.2",
                "brinnatt/k8s-sidecar:1.30.5",
                "brinnatt/prometheus-config-reloader:v0.83.0",
                "brinnatt/prometheus-operator:v0.83.0",
                "brinnatt/alertmanager:v0.28.1",
                "brinnatt/node-exporter:v1.9.1",
                "brinnatt/prometheus:v3.4.2",
            ],
            # Optional example receiver (roles/cluster-addon/templates/prometheus/dingtalk-webhook.yaml)
            # v0.3.0 is Docker Schema 1 (rejected by modern buildx); upstream stable is v2.1.0.
            "prometheus-dingtalk": [
                "brinnatt/prometheus-webhook-dingtalk:v2.1.0",
            ],
        }

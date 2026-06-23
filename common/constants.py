from dataclasses import dataclass, field
from common.os import SystemProbe


@dataclass
class KubeConstant:
    # kubernetes ecosystem components version
    v_docker: str = field(default="29.6.0", metadata={
        "refer_bin": "https://docs.docker.com/engine/install/binaries/",
        "refer_docs": "https://docs.docker.com/manuals/",
        "description": "Engine 29.6.0; CLI plugins paired via v_docker_compose / v_docker_buildx",
    })
    v_docker_compose: str = field(default="5.1.4", metadata={
        "refer_github": "https://github.com/docker/compose/releases",
        "description": "CLI plugin paired with v_docker",
    })
    v_docker_buildx: str = field(default="0.35.0", metadata={
        "refer_github": "https://github.com/docker/buildx/releases",
        "description": "CLI plugin paired with v_docker",
    })
    v_docker_registry: str = field(default="2", metadata={
        "refer_hub": "https://hub.docker.com/_/registry",
        "refer_docs": "https://distribution.github.io/distribution/"
    })
    v_kubeauto: str = field(default="v0.1.1", metadata={
        "refer_github": "https://github.com/brinnatt"
    })
    v_k8s_bin: str = field(default="v1.33.6", metadata={
        "refer_all": "https://kubernetes.io/releases/download/",
        "refer_bin": "https://www.downloadkubernetes.com/",
        "refer_old": "https://github.com/kubernetes/kubernetes/tree/master/CHANGELOG",
    })
    v_extra_bin: str = field(default="1.13.0", metadata={
        "refer_github": "https://github.com/brinnatt/dockerfile-kubeauto-ext-bin",
    })
    v_harbor: str = field(default="v2.13.0", metadata={
        "refer_image": "https://github.com/wise2c-devops/build-harbor-aarch64",
        "description": "None-official"
    })
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
    v_dashboardmetricsscraper: str = field(default="v1.0.8", metadata={
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
    v_flannel: str = field(default="v0.27.3", metadata={
        "refer_github": "https://github.com/flannel-io/flannel"
    })
    v_cilium: str = field(default="v1.17.4", metadata={
        "refer_github": "https://github.com/cilium/cilium",
        "refer_docs": "https://docs.cilium.io/en/stable/installation/k8s-install-helm/"
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
        self.arch = SystemProbe().system_info["machine"]

    _DOCKER_PLUGIN_ARCH = {
        "x86_64": {"compose": "x86_64", "buildx": "amd64"},
        "aarch64": {"compose": "aarch64", "buildx": "arm64"},
    }

    def docker_bin_url(self, version):
        url = f"https://mirrors.aliyun.com/docker-ce/linux/static/stable/{self.arch}/docker-{version}.tgz"
        return url

    def _docker_plugin_arch(self) -> dict:
        suffixes = self._DOCKER_PLUGIN_ARCH.get(self.arch)
        if not suffixes:
            raise RuntimeError(f"Unsupported architecture for Docker CLI plugins: {self.arch}")
        return suffixes

    def docker_compose_bin_url(self, version: str) -> str:
        suffix = self._docker_plugin_arch()["compose"]
        return (
            f"https://github.com/docker/compose/releases/download/v{version}/"
            f"docker-compose-linux-{suffix}"
        )

    def docker_buildx_bin_url(self, version: str) -> str:
        suffix = self._docker_plugin_arch()["buildx"]
        return (
            f"https://github.com/docker/buildx/releases/download/v{version}/"
            f"buildx-v{version}.linux-{suffix}"
        )

    @property
    def component_images(self):
        return {
            "cilium": [
                f"cilium/cilium:{self.v_cilium}",
                f"cilium/operator-generic:{self.v_cilium}",
                f"cilium/hubble-relay:{self.v_cilium}",
                "cilium/hubble-ui-backend:v0.13.2",
                "cilium/hubble-ui:v0.13.2"
            ],
            "flannel": [
                f"ghcr.io/flannel-io/flannel:{self.v_flannel}",
                "ghcr.io/flannel-io/flannel-cni-plugin:v1.7.1-flannel1",
                f"flannel/flannel:{self.v_flannel}",
                "flannel/flannel-cni-plugin:v1.7.1-flannel1"
            ],
            "dashboard": [
                "kubernetesui/dashboard-api:1.14.0",
                "kubernetesui/dashboard-auth:1.4.0",
                "kubernetesui/dashboard-metrics-scraper:1.2.2",
                "kubernetesui/dashboard-web:1.7.0",
                "kong:3.9"
            ],
            "minio": [
                f"quay.io/minio/operator:v{self.v_miniooperator}",
                "quay.io/minio/operator-sidecar:v7.0.1",
                "quay.io/minio/minio:RELEASE.2025-04-08T15-41-24Z",
            ],
            "nacos": [
                "nacos/nacos-server:v2.4.3",
                "nacos/nacos-peer-finder-plugin:1.1"
            ],
            "openebs": [
                "bitnami/kubectl:1.25.15",
                "openebs/provisioner-localpv:4.3.0",
                "openebs/linux-utils:4.2.0",
                "openebs/lvm-driver:1.7.0",
                "brinnatt/csi-node-driver-registrar:v2.13.0",
                "brinnatt/csi-resizer:v1.11.2",
                "brinnatt/csi-snapshotter:v7.0.0",
                "brinnatt/csi-provisioner:v5.2.0",
                "brinnatt/snapshot-controller:v7.0.0",
            ],
            "rocketmq": [
                "apache/rocketmq-operator:latest",
                "apacherocketmq/rocketmq-broker:4.5.0-alpine-operator-0.3.0",
                "apacherocketmq/rocketmq-nameserver:4.5.0-alpine-operator-0.3.0",
                "apacherocketmq/rocketmq-console:2.0.0"
            ],
            "ingress-nginx": [
                "brinnatt/ingress-nginx-controller:v1.13.0",
                "brinnatt/kube-webhook-certgen:v1.6.0"
            ],
            "kube-ovn": [
                f"kubeovn/kube-ovn:{self.v_kubeovn}"
            ],
            "kube-router": [
                f"cloudnativelabs/kube-router:{self.v_kuberouter}"
            ],
            "local-path-provisioner": [
                f"rancher/local-path-provisioner:{self.v_localpathprovisioner}"
            ],
            "network-check": [
                "brinnatt/json-mock:v1.3.0",
                "brinnatt/alpine-curl:v7.85.0"
            ],
            "nfs-provisioner": [
                f"brinnatt/nfs-subdir-external-provisioner:{self.v_nfsprovisioner}"
            ],
            "prometheus": [
                "brinnatt/kube-state-metrics:v2.16.0",
                "brinnatt/kube-webhook-certgen:v1.6.0",
                "grafana/grafana:12.0.2",
                "quay.io/kiwigrid/k8s-sidecar:1.30.5",
                "quay.io/prometheus-operator/prometheus-config-reloader:v0.83.0",
                "quay.io/prometheus-operator/prometheus-operator:v0.83.0",
                "quay.io/prometheus/alertmanager:v0.28.1",
                "quay.io/prometheus/node-exporter:v1.9.1",
                "quay.io/prometheus/prometheus:v3.4.2"
            ]
        }

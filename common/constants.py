from dataclasses import dataclass, field
from common.os import SystemProbe


@dataclass
class KubeConstant:
    # kubernetes ecosystem components version
    v_docker: str = field(default="28.0.4", metadata={
        "refer_bin": "https://docs.docker.com/engine/install/binaries/",
        "refer_docs": "https://docs.docker.com/manuals/"
    })
    v_docker_registry: str = field(default="2", metadata={
        "refer_hub": "https://hub.docker.com/_/registry",
        "refer_docs": "https://distribution.github.io/distribution/"
    })
    v_kubeauto: str = field(default="v1.0.1", metadata={
        "refer_github": "https://github.com/brinnatt"
    })
    v_k8s_bin: str = field(default="v1.33.1", metadata={
        "refer_all": "https://kubernetes.io/releases/download/",
        "refer_bin": "https://www.downloadkubernetes.com/",
        "refer_old": "https://github.com/kubernetes/kubernetes/tree/master/CHANGELOG",
    })
    v_extra_bin: str = field(default="1.12.5", metadata={
        "refer_github": "https://github.com/brinnatt/dockerfile-kubeauto-ext-bin",
    })
    v_harbor: str = field(default="v2.12.4", metadata={
        "refer_image": "https://github.com/wise2c-devops/build-harbor-aarch64",
        "description": "None-official"
    })
    v_calico: str = field(default="v3.28.4", metadata={
        "refer_github": "https://github.com/projectcalico/calico",
        "refer_docs": "https://docs.tigera.io/calico/latest/about/"
    })
    v_coredns: str = field(default="1.12.1", metadata={
        "refer_github": "https://github.com/coredns/coredns",
        "refer_docs": "https://coredns.io/"
    })
    v_dnsnodecache: str = field(default="1.25.0", metadata={
        "refer_github": "https://github.com/kubernetes/kubernetes/blob/master/cluster/addons/dns/nodelocaldns/nodelocaldns.yaml",
        "refer_docs": "https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/"
    })
    v_dashboard: str = field(default="7.12.0", metadata={
        "refer_github": "https://github.com/kubernetes/dashboard",
    })
    v_dashboardmetricsscraper: str = field(default="v1.0.8", metadata={
        "refer_github": "https://github.com/kubernetes-sigs/dashboard-metrics-scraper"
    })
    v_metricsserver: str = field(default="v0.7.2", metadata={
        "refer_github": "https://github.com/kubernetes-sigs/metrics-server",
        "refer_docs": "https://kubernetes-sigs.github.io/metrics-server/"
    })
    v_pause: str = field(default="3.10", metadata={
        "refer_github": "https://github.com/kubernetes/kubernetes/tree/master/build/pause",
        "refer_none_official_docs": "https://k8s.iswbm.com/c02/p02_learn-kubernetes-pod-via-pause-container.html"
    })
    v_flannel: str = field(default="v0.26.7", metadata={
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
    v_promchart: str = field(default="45.23.0", metadata={
        "refer_github": "https://github.com/prometheus/prometheus",
        "refer_docs": "https://prometheus.io/",
        "refer_helm": "https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack"
    })
    v_kubeapps: str = field(default="12.4.3", metadata={
        "refer_github": "https://github.com/vmware-tanzu/kubeapps",
        "refer_helm": "https://github.com/bitnami/charts/tree/main/bitnami/kubeapps",
    })
    v_kubeblocks: str = field(default="0.9.3", metadata={
        "refer_github": "https://github.com/apecloud/kubeblocks",
        "refer_docs": "https://kubeblocks.io/docs/preview/user_docs/overview/introduction"
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
        self.systeminfo = SystemProbe().system_info
        self.arch = self.systeminfo["machine"]

    def docker_bin_url(self, version):
        url = f"https://mirrors.aliyun.com/docker-ce/linux/static/stable/{self.arch}/docker-{version}.tgz"
        return url

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
                f"flannel/flannel:{self.v_flannel}",
                "flannel/flannel-cni-plugin:v1.5.1-flannel2"
            ],
            "dashboard": [
                "kubernetesui/dashboard-api:1.12.0",
                "kubernetesui/dashboard-auth:1.2.4",
                "kubernetesui/dashboard-metrics-scraper:1.2.2",
                "kubernetesui/dashboard-web:1.6.2",
                "kong:3.8"
            ],
            "kubeapps": [
                "bitnami/kubeapps-apis:2.7.0-debian-11-r10",
                "bitnami/kubeapps-apprepository-controller:2.7.0-scratch-r0",
                "bitnami/kubeapps-asset-syncer:2.7.0-scratch-r0",
                "bitnami/kubeapps-dashboard:2.7.0-debian-11-r12",
                "bitnami/nginx:1.23.4-debian-11-r18",
                "bitnami/postgresql:15.3.0-debian-11-r0"
            ],
            "kubeblocks": [
                "apecloud-registry.cn-zhangjiakou.cr.aliyuncs.com/apecloud/snapshot-controller:v6.2.1",
                "apecloud-registry.cn-zhangjiakou.cr.aliyuncs.com/apecloud/kubeblocks-charts:0.9.3",
                "apecloud-registry.cn-zhangjiakou.cr.aliyuncs.com/apecloud/kubeblocks-datascript:0.9.3",
                "apecloud-registry.cn-zhangjiakou.cr.aliyuncs.com/apecloud/kubeblocks:0.9.3",
                "apecloud-registry.cn-zhangjiakou.cr.aliyuncs.com/apecloud/kubeblocks-tools:0.9.3",
                "apecloud-registry.cn-zhangjiakou.cr.aliyuncs.com/apecloud/kubeblocks-dataprotection:0.9.3"
            ],
            "kb-addon-mysql": [
                "apecloud/mysql_audit_log:8.0.33",
                "apecloud/xtrabackup:8.0",
                "apecloud/jemalloc:5.3.0",
                "apecloud/syncer:0.5.0",
                "apecloud/mysql:8.0.35",
                "apecloud/agamotto:0.1.2-beta.1"
            ],
            "kb-addon-pg": [
                "apecloud/spilo:16.4.0",
                "apecloud/pgbouncer:1.19.0",
                "apecloud/postgres-exporter:v0.15.0"
            ],
            "kb-addon-redis": [
                "apecloud/redis-stack-server:7.2.0-v14"
            ],
            "kb-addon-minio": [
                "apecloud/minio:RELEASE.2024-06-29T01-20-47Z",
                "apecloud/kubeblocks-tools:0.8.2"
            ],
            "kb-addon-mongodb": [
                "apecloud/mongo:5.0.30",
                "apecloud/syncer:0.3.7"
            ],
            "kb-addon-es": [
                "apecloud/kibana:8.8.2",
                "apecloud/elasticsearch-plugins:8.8.2",
                "apecloud/elasticsearch:8.8.2",
                "apecloud/elasticsearch-exporter:v1.7.0",
                "apecloud/curl-jq:0.1.0"
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
                "brinnatt/kube-state-metrics:v2.8.2",
                "brinnatt/kube-webhook-certgen:v1.5.1",
                "grafana/grafana:9.4.7",
                "quay.io/kiwigrid/k8s-sidecar:1.22.0",
                "quay.io/prometheus-operator/prometheus-config-reloader:v0.63.0",
                "quay.io/prometheus-operator/prometheus-operator:v0.63.0",
                "quay.io/prometheus/alertmanager:v0.25.0",
                "quay.io/prometheus/node-exporter:v1.5.0",
                "quay.io/prometheus/prometheus:v2.42.0"
            ]
        }

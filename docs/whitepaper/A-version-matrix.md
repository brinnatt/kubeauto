# 附录 A 版本矩阵与官方文档索引

> **单一真相源（SSOT）：** `common/constants.py` 中的 `KubeConstant`。  
> 六仓 CI 与 SSOT 对齐测试：`tests/unit/test_six_repo_version_sync.py`（需 sibling dockerfile 仓库检出）。  
> 编写基线日期：以仓库当前 `KubeConstant` 默认值为准；变更时须同步修订本表与 [第 0 章](./00-preface.md) 基线声明。

## A.1 产品与 Kubernetes 核心

| 组件 | 版本 | 常量 / 声明位置 |
|------|------|-----------------|
| kubeauto 产品 | v0.1.1 | `v_kubeauto` |
| Kubernetes 二进制 | v1.33.6 | `v_k8s_bin` |
| k8s-bin 镜像 tag | v1.33.6 | `k8s_bin_pack_tags` |
| pause（sandbox） | 3.10 | `v_pause` |
| metrics-server | v0.8.0 | `v_metricsserver` |

## A.2 扩展二进制包（ext-bin / ext-bin-sp1）

| 组件 | 版本 | 声明位置 |
|------|------|----------|
| ext-bin 镜像 tag | 1.14.0 | `v_extra_bin` |
| ext-bin-sp1 镜像 tag | 1.3.1 | `v_extra_bin_sp1` |
| etcd | v3.6.4 | ext-bin Dockerfile |
| containerd | 2.1.4 | ext-bin Dockerfile |
| runc | v1.3.1 | ext-bin Dockerfile |
| CNI plugins | v1.8.0 | ext-bin Dockerfile |
| helm | v3.19.0 | ext-bin Dockerfile |
| crictl | v1.34.0 | ext-bin Dockerfile |
| nerdctl（minimal） | 2.3.4 | `v_nerdctl` / ext-bin Dockerfile |
| cfssl / cfssljson | v1.6.5 | ext-bin 静态编译 |
| calicoctl | 随 Calico 版本 | ext-bin |

## A.3 容器运行时（可选 docker 路径）

| 组件 | 版本 | 常量 |
|------|------|------|
| Docker Engine | 28.5.2 | `v_docker` |
| Docker Compose 插件 | 2.40.3 | `v_docker_compose` |
| Docker Buildx 插件 | 0.29.1 | `v_docker_buildx` |
| cri-dockerd | 0.3.26 | `v_cri_dockerd` |
| Distribution Registry | 2 | `v_docker_registry` |

## A.4 集群网络（CNI）

| 组件 | 版本 | 常量 |
|------|------|------|
| Calico（默认） | v3.28.4 | `v_calico` |
| Flannel | v0.28.4 | `v_flannel` |
| flannel-cni-plugin | v1.8.0-flannel1 | `v_flannel_cni` |
| Cilium | v1.19.5 | `v_cilium` |
| Hubble UI | v0.13.5 | `v_cilium_hubble_ui` |
| kube-router | v1.5.4 | `v_kuberouter` |
| kube-ovn | v1.11.5 | `v_kubeovn` |

**Calico 数据存储：** 本项目默认 **etcdv3**（`roles/calico/templates/calicoctl.cfg.j2`），**非** Kubernetes CRD（KDD）模式。

## A.5 DNS 与 Service 发现

| 组件 | 版本 | 常量 |
|------|------|------|
| CoreDNS | 1.12.4 | `v_coredns` |
| NodeLocal DNSCache | 1.26.4 | `v_dnsnodecache` |
| NodeLocal 监听地址 | 169.254.20.10 | `LOCAL_DNS_CACHE`（`conf/config.yml`） |

DNS 在 setup 步骤 **07**（`07.cluster-addon.yml`）安装；CNI 在步骤 **06**。

## A.6 集群插件与 Helm Chart

| 组件 | 版本 | 常量 |
|------|------|------|
| kube-prometheus-stack chart | 75.7.0 | `v_promchart` |
| ingress-nginx chart | 4.13.0 | `v_ingressnginx` |
| ingress-nginx controller 镜像 | v1.13.0 | `v_ingress_nginx_controller` |
| kube-webhook-certgen | v1.6.0 | `v_webhook_certgen` |
| Kubernetes Dashboard chart | 7.14.0 | `v_dashboard` |
| dashboard-metrics-scraper | 1.2.2 | `v_dashboardmetricsscraper` |
| OpenEBS chart | 4.3.2 | `v_openebs` |
| MinIO Operator chart | 7.1.1 | `v_miniooperator` |
| local-path-provisioner | v0.0.31 | `v_localpathprovisioner` |
| nfs-subdir-external-provisioner | v4.0.2 | `v_nfsprovisioner` |
| Harbor 离线包 | v2.13.0 | `v_harbor` |

### A.6.1 Prometheus 栈镜像钉扎（`component_images["prometheus"]`）

| 镜像 | Tag |
|------|-----|
| kube-state-metrics | v2.16.0 |
| prometheus | v3.4.2 |
| alertmanager | v0.28.1 |
| grafana | 12.0.2 |
| prometheus-operator | v0.83.0 |
| prometheus-config-reloader | v0.83.0 |
| node-exporter | v1.9.1 |
| k8s-sidecar | 1.30.5 |

### A.6.2 OpenEBS / CSI 相关

| 镜像 | Tag |
|------|-----|
| OpenEBS umbrella chart | 4.3.2 |
| provisioner-localpv（Hostpath 子 chart/app） | 4.3.0 |
| lvm-driver（LVM LocalPV 子 chart/app） | 1.7.0 |
| linux-utils helper | 4.2.0 |
| csi-node-driver-registrar | v2.13.0 |
| csi-provisioner | v5.2.0 |
| csi-resizer | v1.11.2 |
| csi-snapshotter | v7.0.0 |
| snapshot-controller | v8.3.0 |

### A.6.3 验收 / 网络检查

| 镜像 | Tag | 常量 |
|------|-----|------|
| json-mock | v1.3.1 | `v_json_mock` |
| alpine-curl | v7.85.0 | `v_alpine_curl` |

## A.7 制品 Registry 路径约定

| 阶段 | 路径 |
|------|------|
| CI dual-push（首选） | `hub.talkedu.cn/kubeauto/<name>:<tag>` |
| CI dual-push（回落） | `brinnatt/<name>:<tag>` |
| 控制节点本地 Registry | `registry.talkschool.cn:5000/brinnatt/<name>:<tag>` |
| 节点 containerd pull | 同上（经 `INSECURE_REG` / `certs.d` 信任） |

## A.8 Node Allocatable 合同默认（非版本号，交付基线）

| 项 | 默认值 |
|----|--------|
| kubeReserved | CPU `1000m`，memory `1536Mi` |
| systemReserved | CPU `1000m`，memory `2560Mi` |
| 合计预留 | 约 **2 CPU + 4Gi** |
| `SYS_RESERVED_ENFORCE` | **`no`** |
| 节点规格地板 | ≥ **16 CPU / 32Gi** |

## A.9 官方文档索引

| 主题 | URL |
|------|-----|
| Kubernetes 组件 | https://kubernetes.io/docs/concepts/overview/components/ |
| PKI 证书 | https://kubernetes.io/docs/setup/best-practices/certificates/ |
| 预留系统资源 | https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/ |
| 容器运行时 | https://kubernetes.io/docs/setup/production-environment/container-runtimes/ |
| 集群网络 | https://kubernetes.io/docs/concepts/cluster-administration/networking/ |
| NodeLocal DNS | https://kubernetes.io/docs/tasks/administer-cluster/nodelocaldns/ |
| etcd | https://etcd.io/docs/ |
| Calico | https://docs.tigera.io/calico/latest/about/ |
| Cilium | https://docs.cilium.io/ |
| CoreDNS | https://coredns.io/ |
| Prometheus Operator | https://prometheus-operator.dev/ |
| Helm | https://helm.sh/docs/ |
| OpenEBS 4.3.x | https://openebs.io/docs/4.3.x/ |
| OpenEBS LVM v1.7.0 源码 | https://github.com/openebs/lvm-localpv/tree/v1.7.0 |

## A.10 实现路径速查

| 主题 | 路径 |
|------|------|
| 版本 SSOT | `common/constants.py` |
| 默认配置 | `conf/config.yml` |
| 六仓契约测试 | `tests/unit/test_six_repo_version_sync.py` |
| 一键安装 | `playbooks/90.setup.yml` |
| 网络（06） | `playbooks/06.network.yml` |
| 插件 / DNS（07） | `playbooks/07.cluster-addon.yml` |
| 证书 deploy | `roles/deploy/` |
| etcd | `roles/etcd/` |
| 控制面 | `roles/kube-master/` |
| 节点 | `roles/kube-node/` |
| kube-lb | `roles/kube-lb/` |
| 下载 | `service/cluster/downloader.py` |
| 编排 | `service/cluster/manager.py` |

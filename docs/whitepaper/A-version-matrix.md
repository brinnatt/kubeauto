# 附录 A 版本矩阵与官方文档索引

## A.1 版本矩阵（编写时基线）

| 组件 | 版本 | 声明位置 |
|------|------|----------|
| kubeauto | v0.1.1 | `v_kubeauto` |
| Kubernetes | v1.33.6 | `v_k8s_bin` |
| etcd | v3.6.4 | ext-bin 镜像 |
| containerd | 2.1.4 | ext-bin Dockerfile |
| runc | v1.3.1 | ext-bin |
| CNI plugins | v1.8.0 | ext-bin |
| helm | v3.19.0 | ext-bin |
| crictl | v1.34.0 | ext-bin |
| cfssl | v1.6.5 | ext-bin 编译 |
| Docker Engine | 28.5.2 | `v_docker` |
| cri-dockerd | 0.3.26 | `v_cri_dockerd` |
| Calico | v3.28.4 | `v_calico` |
| Flannel | v0.28.4 | `v_flannel` |
| Cilium | v1.19.5 | `v_cilium` |
| CoreDNS | 1.12.4 | `v_coredns` |
| pause | 3.10 | `v_pause` |
| metrics-server | v0.8.0 | `v_metricsserver` |
| Dashboard chart | 7.14.0 | `v_dashboard` |
| kube-prometheus-stack | 75.7.0 | `v_promchart` |
| ingress-nginx chart | 4.13.0 | `v_ingressnginx` |
| OpenEBS | 4.3.2 | `v_openebs` |
| MinIO Operator | 7.1.1 | `v_miniooperator` |
| ext-bin / sp1 | 1.13.1 / 1.3.1 | `v_extra_bin` / `v_extra_bin_sp1` |

## A.2 官方文档索引

| 主题 | URL |
|------|-----|
| Kubernetes 组件 | https://kubernetes.io/docs/concepts/overview/components/ |
| PKI 证书 | https://kubernetes.io/docs/setup/best-practices/certificates/ |
| 预留系统资源 | https://kubernetes.io/docs/tasks/administer-cluster/reserve-compute-resources/ |
| 容器运行时 | https://kubernetes.io/docs/setup/production-environment/container-runtimes/ |
| 集群网络 | https://kubernetes.io/docs/concepts/cluster-administration/networking/ |
| etcd | https://etcd.io/docs/ |
| Calico | https://docs.tigera.io/calico/latest/about/ |
| Cilium | https://docs.cilium.io/ |
| CoreDNS | https://coredns.io/ |
| Prometheus Operator | https://prometheus-operator.dev/ |
| Helm | https://helm.sh/docs/ |

## A.3 实现路径速查

| 主题 | 路径 |
|------|------|
| 版本 SSOT | `common/constants.py` |
| 默认配置 | `conf/config.yml` |
| 一键安装 | `playbooks/90.setup.yml` |
| 证书 deploy | `roles/deploy/` |
| etcd | `roles/etcd/` |
| 控制面 | `roles/kube-master/` |
| 节点 | `roles/kube-node/` |
| kube-lb | `roles/kube-lb/` |
| 插件 | `roles/cluster-addon/` |
| 下载 | `service/cluster/downloader.py` |
| 编排 | `service/cluster/manager.py` |

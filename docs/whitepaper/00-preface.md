# 前言与文档约定

## 文档定位

本技术白皮书（Technical White Paper / Concepts Guide）是 kubeauto 交付包的**概念与架构说明书**，对应业界常见的「Concepts」文档形态（例如 Oracle Cloud Native Environment Concepts、华为云 CCE 产品介绍与架构说明、Amazon EKS 架构白皮书），并与 [Kubernetes Concepts](https://kubernetes.io/docs/concepts/) 的叙述方式对齐：**形式化定义 → 机制与不变量（表格 / 时序）→ 本产品实现路径**。

各章采用官方 Concepts 结构书写（形式化定义 → 机制与表格 → 本产品实现），**不采用**隐喻式或教材式开篇导读；说明对象为架构评审与现场验收所需的技术事实。

它与另外两份交付文档分工如下：

| 文档 | 对应业界形态 | 回答的问题 |
|------|--------------|------------|
| [技术白皮书](../technical-whitepaper.md)（本文体系） | Concepts / Architecture Whitepaper | 为什么这样设计？组件如何工作？证书/网络/监控如何落地？ |
| [操作手册](../operations-manual.md) | Installation & Administration Guide | 如何安装、扩缩、备份、验收？ |
| [开发手册](../development-manual.md) | Developer Guide | 六仓如何改代码、如何钉版本、如何测试？ |

## 读者对象

- 甲方架构师 / 技术负责人：评审方案与签收依据
- 实施与运维工程师：理解现场行为与排障路径
- 二次开发与开源贡献者：对照实现路径修改

## 阅读路径建议

1. 先读 [总册入口](../technical-whitepaper.md) 第 2 章总体架构，建立全局图。
2. 按职责精读：安全看第 6 章证书；网络看第 9 章；可观测看第 12 章。
3. 对照仓库路径验收：每章均标注 `roles/`、`playbooks/`、`common/constants.py` 等实现落点。
4. 上机操作回到操作手册；改代码回到开发手册。

## 术语与符号

| 术语 | 含义 |
|------|------|
| 控制节点 | 运行 `kubecli`、本地 Registry、持有 `clusters/<name>` 的主机，默认 `/usr/local/kubeauto` |
| 库存 | Ansible inventory，`clusters/<name>/hosts` |
| 集群配置 | `clusters/<name>/config.yml`（由 `conf/config.yml` 生成） |
| `ca_dir` | 节点证书目录，默认 `/etc/kubernetes/ssl` |
| `SECURE_PORT` | apiserver 安全端口，默认 `6443` |
| `brinnatt/*` | 本项目统一镜像命名空间（Docker Hub）；离线部署时经本地 Registry 以 `registry.talkschool.cn:5000/brinnatt/*` 引用 |
| `hub.talkedu.cn/kubeauto` | 国内镜像拉取优先源（`KubeConstant.v_talkedu_registry`）；CI dual-push 与 Docker Hub `brinnatt/*` 对齐 |
| SSOT | Single Source of Truth，版本单一真相源 `common/constants.py` |
| 安装步骤 `01`–`07` | 分步 playbook 编号；`90` / `all` 等价于 `playbooks/90.setup.yml` 一键总装（映射见 `service/cluster/manager.py` 中 `_PLAYBOOK_MAP_SETUP`） |

## 安装步骤约定

与 [操作手册](../operations-manual.md) 一致，集群安装可按下列步骤分步执行，或使用 `kubecli setup <cluster> 90`（别名 `all`）一次性运行 `playbooks/90.setup.yml`：

| 步骤 | CLI 别名 | Playbook | 主要角色 |
|------|----------|----------|----------|
| `01` | `prepare` | `01.prepare.yml` | `chrony`（可选）→ `deploy` → `prepare` |
| `02` | `etcd` | `02.etcd.yml` | `etcd` |
| `03` | `container-runtime` | `03.runtime.yml` | `containerd`（默认）或 `docker` + `cri-dockerd` |
| `04` | `kube-master` | `04.kube-master.yml` | `kube-lb` → `kube-master` → `kube-node`（`serial: 1`） |
| `05` | `kube-node` | `05.kube-node.yml` | `kube-lb` → `kube-node`（非 master 的 worker） |
| `06` | `network` | `06.network.yml` | CNI（默认 `calico`，**etcdv3** 数据存储，非 KDD） |
| `07` | `cluster-addon` | `07.cluster-addon.yml` | DNS、metrics-server 等可选插件 |
| `90` | `all` | `90.setup.yml` | 上述流程总装（含 CNI 后 `wait-node-ready`） |

可选步骤：`10`（`ex-lb`）、`11`（`harbor`），不在 `90` 默认路径内。

## 版本基线（编写时）

以 `KubeConstant` 为准：Kubernetes **v1.33.6**，Calico **v3.28.4**，containerd **2.1.4**（ext-bin），nerdctl **2.3.4**（ext-bin minimal），etcd **v3.6.4**，ext-bin **1.14.0**，kubeauto **v0.1.1**。数字变更时以代码与六仓同步测试为准，并应同步修订本白皮书版本矩阵。

## 官方规范对齐声明

本白皮书对 Kubernetes 行为的描述对齐官方文档语义；实现路径为 **二进制 + systemd + cfssl + Ansible**，**不使用 kubeadm**。凡写「本项目实现」处，均以仓库实装为准，而非发行版默认行为。

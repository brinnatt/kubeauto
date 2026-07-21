# 第 1 章 产品概述与交付范围

## 1.1 产品要解决的问题

企业交付 Kubernetes 底座时，通常同时面临四类约束：

1. **安装路径可控**：生产环境往往禁止「黑盒安装器」；需要可审计的二进制、systemd unit、证书与配置文件。
2. **离线可复现**：节点不能直连 Docker Hub / GitHub；制品必须可缓存、可校验、可回放。
3. **生态组件齐套**：CNI、DNS、Dashboard、Prometheus、Ingress、存储等必须与控制面版本对齐，避免标签漂移。
4. **资源与高可用可验收**：Node Allocatable、多 master、VIP/本地 LB、备份恢复必须有明确合同口径。

kubeauto 将上述能力收敛为：**一个控制面 CLI（`kubecli`）+ 一套 Ansible 角色 + 六仓制品镜像**，在目标主机上以 **二进制 + systemd + cfssl + Ansible** 方式落地 Kubernetes 与生态组件（**不使用 kubeadm**）。

## 1.2 交付物清单（签收视角）

| 交付物 | 说明 | 对应仓库/路径 |
|--------|------|----------------|
| 产品逻辑与编排 | CLI、playbooks、roles、配置模板、测试 | `kubeauto` |
| 产品运行镜像 | 打包后的 kubeauto（可选容器化控制面） | `kubeauto-dockerfile` |
| Kubernetes 二进制包 | apiserver/CM/scheduler/kubelet/proxy/kubectl | `kubeauto-k8s-bin-dockerfile` |
| 扩展二进制包 | etcd、containerd、runc、CNI、helm、crictl、nerdctl、cfssl、calicoctl 等 | `kubeauto-ext-bin-dockerfile` |
| 源码编译补充包 | nginx(stream)、chrony、keepalived | `kubeauto-ext-bin-sp1-dockerfile` |
| 组件镜像集 | 全部 `brinnatt/<name>:<tag>` | `kubeauto-ext-images-dockerfile` |
| 文档三件套 | 操作 / 白皮书 / 开发 | `kubeauto/docs/` |
| 验收辅助 | 单测、企业矩阵、reserved 校验脚本 | `kubeauto/tests/` |

## 1.3 默认技术栈与关键设计

编写时基线以 `common/constants.py`（`KubeConstant`）为准，经 `kubecli new` 渲染至 `clusters/<name>/config.yml`：

| 类别 | 默认值 | 声明 / 落地 |
|------|--------|-------------|
| Kubernetes | **v1.33.6** | `v_k8s_bin` → `K8S_VER` |
| 容器运行时 | **containerd 2.1.4**（ext-bin）+ **nerdctl 2.3.4** | `CONTAINER_RUNTIME=containerd`；可选 `docker` + cri-dockerd |
| CNI | **Calico v3.28.4** | `CLUSTER_NETWORK=calico`；数据存储为 **etcdv3**（`roles/calico/templates/calicoctl.cfg.j2`），**非** Kubernetes Datastore（KDD） |
| etcd | **v3.6.4** | ext-bin；`roles/etcd` |
| apiserver 本机入口 | **kube-lb** `127.0.0.1:6443` | `roles/kube-lb` |
| 证书目录 | `/etc/kubernetes/ssl` | `ca_dir`；cfssl 签发，非 `/etc/kubernetes/pki` |
| 镜像命名 | `brinnatt/<name>:<tag>` | 全组件统一前缀 |

## 1.4 制品与镜像供给链

离线交付采用「控制节点下载 → 本地 Registry → 节点拉取」三段式：

1. **拉取**：`kubecli download` 按 `hub.talkedu.cn/kubeauto/<name>:<tag>` → Docker Hub `brinnatt/<name>:<tag>` 顺序尝试（见 `KubeConstant.v_talkedu_registry` 与 `component_images`）。
2. **灌仓**：推送至控制节点本地 Registry `registry.talkschool.cn:5000/brinnatt/...`（`service/cluster/registry.py`）。
3. **部署**：Ansible 角色引用 `registry.talkschool.cn:5000/brinnatt/*`（例如 `conf/config.yml` 中 `SANDBOX_IMAGE`）。

版本钉扎的唯一真相源为 `common/constants.py`；禁止仅在某一角色模板中手写旧 tag。

## 1.5 安装编排：步骤 01–07 与 90/all

| 步骤 | Playbook | 要点 |
|------|----------|------|
| `01` | `01.prepare.yml` | 可选 `chrony` → `deploy`（CA / kubeconfig）→ `prepare` |
| `02` | `02.etcd.yml` | etcd 集群；`CLUSTER_STATE=new` |
| `03` | `03.runtime.yml` | containerd 或 docker 路径 |
| `04` | `04.kube-master.yml` | `kube-lb` → `kube-master` → `kube-node`；多 master 时 `serial: 1` |
| `05` | `05.kube-node.yml` | worker：`kube-lb` → `kube-node` |
| `06` | `06.network.yml` | 五选一 CNI；默认 Calico（etcd 后端） |
| `07` | `07.cluster-addon.yml` | CoreDNS、metrics-server 等 |
| `90` / `all` | `90.setup.yml` | 上述总装 + CNI 后 `wait-node-ready` |

CLI 映射：`kubecli setup <cluster> <step>`，定义于 `service/cluster/manager.py` 中 `_PLAYBOOK_MAP_SETUP`。

### 1.5.1 签收验收要点（安装阶段）

- [ ] 控制节点 `clusters/<name>/ssl/` 存在 `ca.pem` / `ca-key.pem` 与 `kubectl.kubeconfig`。
- [ ] 各节点 `systemctl is-active` 覆盖 etcd（若有）、containerd、kube-apiserver（master）、kubelet、kube-lb。
- [ ] `kubectl get nodes` 全部 Ready；`kubectl get pods -n kube-system` 无持续 CrashLoop。
- [ ] 节点 kubeconfig 的 `server` 为 `https://127.0.0.1:6443`（经 kube-lb）。
- [ ] 默认 Calico 时，`calicoctl` 配置 `datastoreType: etcdv3`，且 `calico-node` Pod Running。

## 1.6 能力边界

**在范围内：**

- 单节点（all-in-one）与多节点高可用安装
- containerd 或 docker+cri-dockerd
- 五选一 CNI：calico / flannel / cilium / kube-router / kube-ovn
- 集成 kube-lb；可选 ex-lb（keepalived + nginx）
- 证书签发与 `kca-renew` 轮换
- etcd 备份恢复、集群启停升级销毁、节点扩缩
- 可选插件：DNS、metrics-server、Dashboard、Prometheus、Ingress、存储、MinIO 等
- 离线 Registry 分发

**不在范围内（需甲方另行建设）：**

- 多集群联邦 / 服务网格全家桶（可基于本底座扩展）
- 公有云托管控制面（EKS/ACK/CCE 托管模式）
- 业务应用本身的 CI/CD 与微服务治理（本项目提供底座与部分中间件示例）

## 1.7 与 kubeadm 发行版的定位差异

| 维度 | kubeadm 典型路径 | kubeauto |
|------|------------------|----------|
| 控制面安装 | `kubeadm init/join` | Ansible 渲染 systemd unit + 二进制 |
| 配置真相 | ClusterConfiguration CR | `clusters/*/config.yml` + Jinja |
| 证书 | kubeadm 生成 | cfssl，角色内显式 CSR |
| 升级 | `kubeadm upgrade` | `93.upgrade.yml` |
| 离线 | 需自建镜像与 pause | 六仓 dual-push + `download` 灌仓 |
| 可审计性 | 抽象层较多 | 每个 flag/证书/unit 文件可见 |

选择无 kubeadm 的动机：**离线可控、配置可审计、与企业二进制交付规范一致**。代价是升级与证书流程需维护自有 playbook——本项目已覆盖 backup/restore/renew/upgrade。

# 第 17 章 其他存储与中间件插件

> **适用版本：** local-path-provisioner v0.0.31、NFS Subdir External Provisioner v4.0.2、Nacos Server v2.4.3、RocketMQ Operator app 0.3.0  
> **实现入口：** `roles/cluster-addon/tasks/` 与 `roles/cluster-addon/templates/`  
> 本章只描述 kubeauto 实际启用的形态，不借用上游未启用能力。

## 17.1 四种能力不是同一层

```mermaid
flowchart TB
  PVC[PVC / StorageClass]
  PVC --> LP[local-path<br/>节点目录]
  PVC --> NFS[NFS subdir provisioner<br/>已有 NFS export]
  PVC --> OE[OpenEBS<br/>见第 16 章]
  LP & NFS & OE --> DATA[持久化数据层]
  DATA --> NACOS[Nacos<br/>注册/配置服务]
  DATA --> RMQ[RocketMQ<br/>消息服务]
  MYSQL[外部 MySQL] --> NACOS
```

local-path、NFS provisioner 和 OpenEBS 是存储供给方式；Nacos、RocketMQ 是消费存储与网络的中间件。业务组件 Pod Ready 之前，必须先验证其 StorageClass 数据面和外部依赖。

## 17.2 Rancher Local Path Provisioner

### 17.2.1 原理与项目实现

控制器监听 PVC，在被调度节点上启动 helper Pod，执行 ConfigMap 中的 setup/teardown 脚本创建或删除目录，再生成带节点约束的 PV。

| 项 | kubeauto 值 |
|----|-------------|
| 开关 | `local_path_provisioner_install`，默认 `no` |
| 版本 | v0.0.31 |
| Namespace | `kube-system` |
| SC | `local_path_storage_class`，默认 `local-path` |
| 节点目录 | `local_path_provisioner_dir` |
| Provisioner | `rancher.io/local-path` |
| 绑定/回收 | `WaitForFirstConsumer` / `Delete` |

```mermaid
sequenceDiagram
  participant P as Pod/PVC
  participant S as Scheduler
  participant C as local-path controller
  participant H as helper Pod on node
  S->>C: selected node
  C->>H: setup mkdir 0777
  C->>P: PV + node affinity
  Note over H,P: 删除 PVC 时 teardown rm -rf 对应卷目录
```

官方 v0.0.31 明确说明当前不支持 volume capacity limit，请求容量会被忽略。它和 OpenEBS Hostpath 一样是本地故障域，但实现、SC 和 provisioner 不同；同时安装不冲突，PVC 必须显式选一个。

### 17.2.2 运维与验收

- 基目录不得是 `/` 或系统敏感路径；项目默认映射会允许所有未单列节点使用同一配置路径。
- 监控每个节点基目录所在文件系统的容量与 inode，不能只汇总 PVC requests。
- `Delete` 会执行 `rm -rf "$VOL_DIR"`；卸载 provisioner 前必须先处理其 PV。
- 验收必须完成 PVC Bound、写/读、Pod 重建数据仍在、PVC 删除后测试目录被清理。

```bash
kubectl -n kube-system get deploy,pod -l app=local-path-provisioner
kubectl get sc <local-path-storage-class> -o yaml
kubectl describe pvc <pvc>
kubectl -n kube-system logs deploy/local-path-provisioner --tail=200
```

## 17.3 NFS Subdir External Provisioner

### 17.3.1 原理与责任边界

该 provisioner **不提供 NFS 服务**。它把已经存在、已经导出的 `nfs_server:nfs_path` 挂到自身 `/persistentvolumes`，按 PVC 创建子目录并生成 PV。NFS 服务端的高可用、磁盘、配额、快照、备份、权限和网络仍由客户负责。

```mermaid
flowchart LR
  PVC --> NP[nfs-subdir provisioner<br/>单副本控制器]
  NP --> DIR[export/namespace-pvc-pv]
  DIR --> N[NFS Server]
  N -->|NFS mount| A[Pod on node A]
  N -->|NFS mount| B[Pod on node B]
```

| 项 | kubeauto 值 |
|----|-------------|
| 开关 | `nfs_provisioner_install`，默认 `no` |
| 版本 | v4.0.2 |
| SC | `nfs_storage_class`，默认 `managed-nfs-storage` |
| Provisioner | `k8s-sigs.io/nfs-subdir-external-provisioner` |
| 后端 | `nfs_server` + `nfs_path` |
| 删除参数 | `archiveOnDelete: "false"` |

当前 `archiveOnDelete=false` 意味着删除 PVC 时不归档旧目录；配合 SC 默认 Delete 时应按数据删除操作管理。NFS 可提供跨节点访问，但可用性不高于 NFS 服务端本身。

供给控制面还依赖 Kubernetes API 与 etcd。v4.0.2 固定使用 external-provisioner v6.0.0 / client-go v0.18.0，通过 Endpoints 对象选主；默认租期、续约截止及重试周期分别为 15、10、2 秒。失去续约时 `OnStoppedLeading` 主动以致命错误退出，随后由 Kubernetes 重启容器并重新竞争锁，以避免失去领导身份的控制器继续处理供给请求。`leaderelection lost` 表示控制面续约没有按时完成，不能据此认定 NFS 服务器或共享文件已经损坏。已有挂载的文件 I/O 直接走客户端与 NFS 服务端，provisioner 不代理每次读写；新建／删除 PVC 则需要控制器与 API 恢复。

生产只读调查中，曾观察到 etcd 3.6.4 的 WAL 同步出现约 1–2.6 秒长尾，同一 NFS Endpoints 锁的读取持续 10 秒并超时，与控制器退出时刻对应。这证明该次退出的控制面依赖失败；它不能证明每次历史重启都有同一原因，也不能仅凭虚拟磁盘信息指定物理存储故障。排查须把 Pod 最后退出时间、当前／previous 日志、API 超时和各 etcd 成员的 WAL/后端提交延迟统一到同一时区、同一时间窗，再关联虚拟化调度、快照备份及后端写延迟。依据为固定版[选主回调源码](https://github.com/kubernetes-sigs/sig-storage-lib-external-provisioner/blob/v6.0.0/controller/controller.go)及 [etcd 3.6 性能说明](https://etcd.io/docs/v3.6/op-guide/performance/)。

遇到续约错误时，集群管理员先在已核验的目标 kubeconfig 与 `nfs_provisioner_namespace` 中只读收集故障 Pod 的退出状态、事件和前后两次容器日志；由控制面管理员按相同 UTC 时间窗收集 API/etcd 日志及延迟指标。API 不健康或证据不足时停止供给变更，保留现有 PVC、PV、目录和选主配置。禁止通过删除业务 PVC、关闭选主、随意放大续约超时或盲目执行 etcd defrag 消除现象。基础设施修复由其负责人安排维护、备份及逐成员恢复；恢复后既要观察 API/etcd 与选主稳定，也要以客户批准的测试卷验证新 PVC 供给、跨节点写入/fsync/读回和旧数据保持，最后按约定删除策略回收测试卷。单次重启成功或短时集成回归不等于长期稳定性保证。

在 Kubernetes 1.33.6、containerd、Calico/IPVS 和三台 Rocky Linux 9.8 节点的集成验证中，两个 worker 共享同一 RWX PVC：一端写入并执行 fsync，另一端读回一致。替换 provisioner leader 后，新 PVC 完成供给并通过同样的跨节点读写，原 PVC 的原数据再次读回成功，共得到三次一致性读回结果。该样本验证供给恢复与已有文件访问的集成边界；它使用正常 API/etcd，不代表已消除客户控制面长尾，也不构成长期可用性、NFS 服务端高可用或备份恢复保证。

### 17.3.2 前置与验收

每个可能运行业务 Pod 的节点必须安装 NFS client，并能解析/访问服务端。先做客户端数据面验证，再安装 provisioner：

```bash
showmount -e <nfs-server>
mount -t nfs <nfs-server>:<export> <approved-test-mountpoint>
touch <approved-test-mountpoint>/kubeauto-nfs-preflight
umount <approved-test-mountpoint>
```

现场还必须验证：PVC RWO/RWX 与应用声明一致、两个节点读写同一测试数据、删除策略符合合同、服务端备份可恢复。Provisioner Running 只说明控制器存活，不说明 NFS export 可写。

```bash
kubectl -n <nfs-namespace> get deploy,pod -l app=nfs-client-provisioner
kubectl get sc <nfs-storage-class> -o yaml
kubectl describe pvc <pvc>
kubectl -n <nfs-namespace> logs deploy/nfs-client-provisioner --tail=200
```

## 17.4 Nacos 2.4.3

### 17.4.1 架构

```mermaid
flowchart TB
  CLIENT[SDK/应用] --> SVC[nacos Service<br/>8848/9848]
  SVC --> N1[nacos-0]
  SVC --> N2[nacos-1]
  SVC --> N3[nacos-2]
  N1 & N2 & N3 --> HEAD[nacos-headless<br/>成员发现]
  N1 & N2 & N3 --> MYSQL[外部 MySQL<br/>官方 2.4.3 schema]
  N1 & N2 & N3 --> PVC[各自 RWO PVC<br/>插件/数据/日志]
```

项目使用 StatefulSet、peer-finder initContainer、headless Service 和外部 MySQL。三副本采用 `requiredDuringSchedulingIgnoredDuringExecution` 主机反亲和，因此 `nacos_replicas: 3` 至少需要 3 个满足资源与存储约束的可调度节点；单节点不能通过简单等待变成三副本 Ready。

peer-finder 需要当前 Pod IP 才能在成员列表中识别自身；模板通过 `status.podIP` 的 Downward API 注入 `POD_IP`，而不是从宿主机地址或容器接口猜测。稳定 Pod 名与 headless Service 提供成员地址，跨节点网络和成员端口仍须可达。插件成功安装只证明文件进入卷，不证明 Nacos 成员发现已成功。

Nacos 2.4.3 内置 Connector/J 8.2.0。项目默认 `sslMode=VERIFY_IDENTITY`，在建立数据库会话前验证证书链及数据库主机名，避免把动态 RSA 公钥获取当作数据库身份认证。MySQL 使用 `caching_sha2_password` 时，经过验证的 TLS 通道也保护完整认证交换。私有 CA 由命名空间 Secret 的 `ca.crt` 提供，initContainer 将其导入临时 PKCS12 truststore，数据库私钥始终由数据库服务端持有。Secret 留空时采用 JVM 信任来源；自行替换 JDBC 参数时，原有 TLS 保证不再自动成立。机制依据为 [Connector/J 安全属性](https://dev.mysql.com/doc/connector-j/en/connector-j-connp-props-security.html)及 [Nacos 2.4.3 依赖定义](https://github.com/alibaba/nacos/blob/2.4.3/pom.xml)。

| 配置 | 含义/风险 |
|------|-----------|
| `nacos_mysql_*` | 外部 MySQL 连接；必须预导入仓库内官方 2.4.3 schema |
| `nacos_storage_class/size` | 每个 StatefulSet Pod 的 RWO PVC |
| `nacos_jvm_xms/xmx/xmn` | JVM 堆；Xms/Xmx 应结合容器/节点内存 |
| `nacos_mem_request/cpu_request` | 调度 requests，不等于 JVM 最大堆 |
| `nacos_replicas` | 服务副本数，受强反亲和约束 |
| `nacos_auth_secret` | 客户预建的鉴权 Secret；三个副本共享 token 和服务端身份，不提供公共密钥初值 |
| `nacos_mysql_ca_secret` | 私有数据库 CA Secret 名；仅消费 `ca.crt`，更新后需重建 Pod 重新生成 truststore |
| `nacos_ready_timeout` | StatefulSet 应用就绪等待上限，默认 900 秒；超时失败不自动撤销已创建资源 |
| 8848/9848/9849/7848 | HTTP、客户端 RPC、Raft/兼容通信端口 |

项目启用了 Nacos auth，token 与服务端 identity 从同命名空间的客户 Secret 注入。token 是 Base64 编码的至少 32 字节随机密钥，identity 也须由客户生成且各副本一致；公开初值会使第三方能够冒充可信身份，因此模板不再携带固定值。数据库密码通过独立 Secret 注入，ConfigMap 仅存非敏感连接信息。Secret 不自动提供 etcd 静态加密，权限及密钥保护仍由集群安全边界承担。环境变量只在进程启动时读取，轮换需要维护窗口、Pod 重建及客户端认证复验；操作和恢复步骤见[运维手册可选组件部署](../operations-manual.md#13378其他可选组件概要)。

运行状态分为容器存活、应用就绪和业务成功。2.4.3 的 liveness 接口直接返回成功，不能证明数据库或成员服务可用；项目使用检查模块状态的 readiness 接口作为 startup/readiness 探针，并由产品安装步骤等待 StatefulSet rollout。应用暂时失去就绪状态时，Service 应停止将新流量送往该实例；这不能保证既有连接、配置缓存或客户端重试行为。产品没有用数据库短暂故障触发 liveness 重启，故障恢复仍取决于数据库、网络和应用模块重新就绪。源码依据为 [2.4.3 HealthController](https://github.com/alibaba/nacos/blob/2.4.3/console/src/main/java/com/alibaba/nacos/console/controller/HealthController.java)。

在 Kubernetes 1.33.6、Nacos 2.4.3、Connector/J 8.2.0 和 MySQL 8.0.46 的三副本集成验证中，初始化、全部 Pod 重建、真实重复安装以及 TLS 拒绝场景恢复后，逐实例读回原配置与永久服务记录，共完成 12 次业务读回。恢复阶段读取已有值；错误 CA 与错误数据库主机名均使产品部署非零退出，并分别核对到证书链和服务器身份验证异常。该结果支持上述固定版本的启动、重复收敛和短时恢复判断；客户数据库灾备、容量和长期运行仍须按业务要求另行验收。

### 17.4.2 正确验收

1. MySQL 可达且 schema 表存在，不能回退成嵌入式数据库假通过。
2. StatefulSet 期望副本全部 Ready，PVC 全部 Bound，反亲和分布符合预期。
3. 通过 API/客户端创建配置，另一客户端读取；重建 Pod 后仍能读取。
4. 验证注册实例、健康检查和订阅通知，而不只打开控制台。
5. MySQL 备份恢复与 Nacos 配置/命名空间恢复完成演练。

```bash
kubectl -n nacos get sts,pod,pvc,svc -o wide
kubectl -n nacos describe pod <nacos-pod>
kubectl -n nacos logs <nacos-pod> -c nacos --tail=200
```

Pod Pending 优先检查反亲和、节点数、PVC/topology 和 requests；CrashLoop 优先检查 MySQL schema/认证、JVM 与外部数据库连接参数。

## 17.5 RocketMQ Operator

### 17.5.1 协调架构

```mermaid
flowchart TB
  CR[Broker/NameService/Console CR] --> OP[rocketmq-operator]
  OP --> NS[NameService<br/>hostNetwork]
  NS -->|Ready 后异步 reconcile| BR[Broker master/replica]
  OP --> CON[Console]
  BR --> BPVC[20Gi PVC/实例]
  NS --> NPVC[5Gi PVC/实例]
  PROD[Producer] --> NS --> BR
  CONS[Consumer] --> NS --> BR
```

项目 vendored chart `0.1.0` 的 appVersion 是 operator `0.3.0`；Broker/NameServer 为定制 `4.5.0-alpine-operator-0.3.0`，Console 为 `2.0.0`。`latest` operator 镜像是现有发布风险点，升级时必须通过六仓 digest/行为门禁，不能把 tag 当不可变版本。

| 配置 | 当前默认 | 真实含义 |
|------|----------|----------|
| `rocketmq_nameservice_size` | 1 | hostNetwork；多副本需要不同可用节点/端口 |
| `rocketmq_broker_size` | 1 | broker group 数 |
| `rocketmq_replica_per_group` | 0 | 每组只有 master，不是消息副本 HA |
| `rocketmq_broker_mem` | 512Mi heap | 应与 request/limit 和负载联合规划 |
| `rocketmq_storage_class` | OpenEBS LVM 变量 | Broker 与 NameService 都依赖 PVC |
| `flushDiskType` | `ASYNC_FLUSH` | 性能优先，主机故障可能丢失尚未刷盘消息 |
| `brokerRole` | `ASYNC_MASTER` | 当前默认不是同步复制承诺 |

这是异步 CR 协调：Operator 创建 CR 成功后，Broker 可能仍在等待 NameService 或 PVC；`kubectl apply` 返回 0 不是集群 Ready。

### 17.5.2 正确验收与故障分类

1. Operator Ready；NameService 达到期望副本；随后 Broker/Console Ready。
2. 所有 Broker/NameService PVC Bound，实际卷容量与故障域符合选型。
3. 创建 topic，生产带唯一 ID 的消息，消费者读到相同 ID，并核对消费进度。
4. 重建 Broker 后消息仍可消费；按合同验证 master/replica 故障，而不是只验证 Pod 重启。
5. JVM 无 OOMKilled，磁盘水位、积压、发送/消费失败和 broker 注册均有告警。

```bash
kubectl -n rocketmq get broker,nameservice,console
kubectl -n rocketmq get pod,pvc,svc -o wide
kubectl -n rocketmq describe broker broker
kubectl -n rocketmq logs deploy/rocketmq-operator --tail=200
```

| 现象 | 首查 |
|------|------|
| NameService 不足 | hostNetwork 端口、节点数、PVC、资源 |
| Broker 长期未创建 | NameService Ready 条件、Operator 日志、CR status |
| Broker Pending | StorageClass/PVC、节点 topology、内存 request |
| Broker OOMKilled | `BROKER_MEM` 与 container limit、节点压力 |
| Pod 全 Ready 但消息失败 | topic、路由注册、生产/消费客户端、积压与 broker 日志 |

## 17.6 统一安装顺序与验收链

```mermaid
flowchart LR
  PRE[节点/外部服务前置] --> IMG[kubecli download -E]
  IMG --> SC[安装并真实验证 StorageClass]
  SC --> EXT[MySQL/NFS 等外部依赖验证]
  EXT --> ADD[安装 Nacos/RocketMQ]
  ADD --> OBJ[对象/Pod/PVC Ready]
  OBJ --> BIZ[配置读写或消息生产消费]
  BIZ --> FAIL[故障与恢复演练]
```

任何一层失败都应停在本层修复并回收失败资源，不能通过放宽 Ready 检查把失败推给下一层。

## 17.7 官方来源与项目路径

| 组件 | 锁定版本官方来源 | 项目路径 |
|------|------------------|----------|
| local-path v0.0.31 | https://github.com/rancher/local-path-provisioner/tree/v0.0.31 | `templates/local-storage/` |
| NFS provisioner v4.0.2 | https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner/tree/nfs-subdir-external-provisioner-4.0.2 | `templates/nfs-provisioner/` |
| Nacos 2.4.3 | https://github.com/alibaba/nacos/tree/2.4.3 | `templates/nacos/` |
| RocketMQ Operator | https://github.com/apache/rocketmq-operator | `templates/rocketmq/`、`rocketmq-operator-0.1.0.tgz` |

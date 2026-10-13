# Redis 用户与运维手册

本手册按生产操作顺序组织：先规划和部署，再接入业务、巡检和处理故障，最后执行容量、备份、升级与下线变更。原理与一致性边界见[技术白皮书](technical-whitepaper.md)，版本、模板和发布维护见[开发手册](development-manual.md)。

## 1. 产品与操作边界

固定运行集合为 Redis Open Source 8.10.2、OpsTree Redis Operator 0.27.0、Redis Exporter v1.93.0。Operator 运行镜像为 `0.27.0-kubeauto.2`；Sentinel 模式使用原 `8.10.2`，Cluster 模式使用处理 ASM 接管状态刷新的 `8.10.2-kubeauto.1`。Redis 默认关闭。配置在目标部署目录的 `config.yml` 中完成；`conf/config.yml` 是创建部署目录的模板，修改模板不会更新已有部署。

生产入口只有以下两个步骤：

```bash
bash <<'REDIS_OPERATIONS_1'
set -Eeuo pipefail
kubecli download -E redis </dev/null
kubecli setup redis-prod-cluster 07 --tags redis </dev/null
REDIS_OPERATIONS_1
```

`--tags redis` 将本次收敛限定为 Redis。Operator 管理 Redis StatefulSet、Service、复制和分片拓扑；日常部署不得直接修改这些受控对象，否则控制器可能覆盖变更。Kubeauto 不自动创建 Kubernetes 集群、存储系统或业务凭据。

| 模式 | 数据与客户端 | 默认规模 | 适用场景 |
| --- | --- | --- | --- |
| Sentinel | 一份数据；主写、副本读须接受延迟；支持多 DB | 1 主、2 副本、3 Sentinel | 单实例容量足够、需要故障发现和自动切换 |
| Cluster | 16384 槽；仅 DB 0；集群客户端处理 MOVED/ASK | 3 主分片、每主 2 副本，共 9 个 Redis | 需要分片容量或横向吞吐扩展 |

> **边界：** Sentinel 与 Cluster 不能通过修改 `redis_mode` 原地转换。同名另一模式资源存在时，产品拒绝执行；应部署独立目标、迁移数据、验证客户端后切换业务。
>
> **上游特性：** 本固定镜像使用 Operator 的 `GenerateConfigInInitContainer` 路径生成认证、TLS 和持久化配置。该特性在上游 0.27.0 仍为 Alpha，项目验收不会改变其上游成熟度。内存水位设为 0 也不会关闭配置生成；生产版本变更须重新验证该路径。

## 2. 上线规划

### 2.1 故障域、存储与网络

准备至少三个可调度且独立的故障域，默认按 `kubernetes.io/hostname` 分布。使用可扩容的块存储 StorageClass，明确 `ReadWriteOnce`、容量限制、扩容方式、回收策略和故障恢复 SLA。业务 PVC 在删除 CR 后保留；保留不代表备份，也不保证底层 StorageClass 删除 PV 后仍能恢复。

Sentinel 的 Redis 与 Sentinel 分别约束分布，不以六个 Pod 的总分布代替两组各自分散。Cluster 的 leader/follower StatefulSet 标签是控制器配置角色，故障转移后的真实主从角色应从 `ROLE`、`INFO replication` 或 `CLUSTER NODES` 读取。组级拓扑分散不能单独证明每个主分片与其副本已跨故障域，上线须验证实际复制关系。

Cluster 接入新副本前会验证当前节点标签与剩余副本布局，确保每个主至少有一个跨故障域副本。遇到 `scheduled replicas cannot satisfy the failure-domain and replica budgets` 时，保留已接入成员及 PVC，检查待接入 Pod 的节点、拓扑标签和调度约束，调整其布局后重新执行同一产品入口。不要删除已有业务副本、关闭写保护或将健康检查改为只验证 Ready。

网络须支持集群 DNS、Redis 客户端端口 `6379/TCP`、Sentinel `26379/TCP`、Cluster bus `16379/TCP`、Exporter `9121/TCP` 和 Kubernetes API。Cluster bus 仅允许 Redis 成员互通，不对应用开放。应用到 Redis、应用到 Sentinel、成员间复制、Operator 到成员和监控抓取应分别列入网络访问清单。

> **注意：** 产品不自动安装 CNI、修改现有 NetworkPolicy 或把 Redis 暴露为公网服务。启用默认拒绝策略前，应逐条验证上述必要流量；禁止以开放全部命名空间流量作为永久排障措施。

### 2.2 容量模型

| 资源 | 规划依据 | 上线核验 |
| --- | --- | --- |
| Redis 内存 | 业务对象、碎片、复制/客户端缓冲、fork/COW | `used_memory`、RSS、`mem_fragmentation_ratio`、容器内存峰值 |
| PVC | RDB、AOF、rewrite 双份材料、复制重建临时空间 | `df`、AOF 文件大小、存储告警、扩容能力 |
| CPU | 命令复杂度、TLS、序列化、后台保存、事件循环 | 饱和度、限流时间、延迟分位、慢查询 |
| 网络 | 客户端吞吐、全量复制、分片迁移和备份并发 | 重传、带宽、复制积压覆盖时间 |
| 客户端 | 连接池、pipeline、超时、输出缓冲 | `connected_clients`、`blocked_clients`、拒绝连接和缓冲上限 |

默认每个 Redis request 为 `500m/1Gi`、limit 为 `2 CPU/2Gi`；这是声明起点，不能直接作为客户容量承诺。`redis_maxmemory_percent: 70` 为容器限制预留约 30% 非数据内存，但 fork 写时复制和突发缓冲仍可能超过余量。不得把 `maxmemory` 等同于进程 RSS 或容器硬限制。

## 3. 安装主线路

以下操作在已安装 Kubeauto 的 Linux 控制端执行，由有权管理目标集群与本地部署目录的运维账号负责。全部命令使用同一 kubeconfig；执行新 shell 时先重新导出该路径。

| 输入或产物 | 归属与位置 | 读取方与生命周期 |
| --- | --- | --- |
| `config.yml` | 控制端 `/usr/local/kubeauto/clusters/redis-prod-cluster/`；运维维护 | `kubecli setup` 读取；版本、规模和凭据引用的权威声明 |
| `hosts` | 同一部署目录；运维核对路径与 inventory | Ansible `07` 在 localhost 执行；已有集群仅安装 Redis 时不需要重建节点 |
| `kubectl.kubeconfig` | 同一部署目录，权限 `0600` | Helm、kubectl、产品健康检查使用；不得误用另一个集群上下文 |
| `/secure/redis/` | 控制端安全目录；凭据管理方提供，目录 `0700`、文件 `0600` | 首次生成 Secret 与后续轮换；不进入镜像、Git 或诊断包 |
| 三个业务 Secret | `redis` 命名空间；凭据管理方维护 | Operator、Redis、Sentinel、Exporter 按配置引用，应用另用受限 ACL 用户 |
| `yml/redis/` | 产品在部署目录生成，目录 `0700` | 固定 Chart values、启动 ConfigMap 与 CR；应修改权威配置后重新渲染 |
| 独立备份目录 | 控制端安全目录及异地副本；备份管理方维护 | 快照出口及恢复输入，按保留策略管理；不随实例下线删除 |

### 3.1 准备目标部署目录

执行前需要已可访问的 Kubernetes 集群、Kubeauto 控制端、有效 kubeconfig 和选定 StorageClass。本步骤创建独立部署配置，读取集群状态，随后只收敛 Redis；重复创建同名目录会拒绝，应复用已有目录。

```bash
bash <<'REDIS_OPERATIONS_2'
set -Eeuo pipefail
cluster=redis-prod-cluster
base=/usr/local/kubeauto
source_kubeconfig="$HOME/.kube/config"

test -s "$source_kubeconfig"
echo 'Redis 部署准备：检查控制端输入并创建独立部署目录'
kubecli new "$cluster" </dev/null
install -m 0600 "$source_kubeconfig" "$base/clusters/$cluster/kubectl.kubeconfig"
export KUBECONFIG="$base/clusters/$cluster/kubectl.kubeconfig"
kubectl get nodes -o wide
kubectl get storageclass
echo REDIS_DEPLOYMENT_INPUTS_READY
REDIS_OPERATIONS_2
```

检查 `hosts` 中的 `base_dir`、`cluster_dir` 与实际目录一致。`07` 的 Redis 任务在控制端执行；仅部署 Redis 时可将 inventory 保持为明确的本地控制入口，不能复用含错误节点地址的旧库存。

### 3.2 设置配置与凭据引用

编辑 `/usr/local/kubeauto/clusters/redis-prod-cluster/config.yml`，保留 `kubecli new` 已解析的版本字段，设置以下业务参数：

```yaml
redis_install: "yes"
redis_mode: "sentinel"
# kubecli new 自动解析运行版本；Cluster 镜像单独由 redis_cluster_image_ver 选择。
redis_namespace: "redis"
redis_operator_namespace: "redis-operator"
redis_name: "redis-prod"
redis_storage_class: "csi-block"       # 改成已核验的 StorageClass
redis_pvc_size: "20Gi"
redis_password_secret: "redis-auth"
redis_password_secret_key: "password"
redis_tls_secret: "redis-tls"
redis_acl_secret: "redis-acl"
redis_maxmemory_percent: 70
redis_appendfsync: "everysec"
redis_min_replicas_to_write: 1
redis_min_replicas_max_lag: 10
redis_backup_enabled: false
```

密码、证书和 ACL 文件由客户凭据系统提供，不写入配置文件、Git 或命令行参数。TLS Secret 必须包含 `ca.crt`、`tls.crt`、`tls.key`；密码 Secret 使用配置的键名；ACL Secret 使用 `user.acl`。

证书应具备 serverAuth/clientAuth 用途、正确 CA 链和有效期，SAN 覆盖实际 Service/FQDN、成员通告 DNS，以及 Exporter 使用的 `localhost`。Sentinel、复制和 Cluster 成员地址不同，不得只给客户端 Service 签发证书而遗漏成员间路径。

Sentinel 示例的成员名称为 `redis-prod-0.redis-prod-headless.redis.svc.cluster.local`，Sentinel 成员为 `redis-prod-sentinel-sentinel-0.redis-prod-sentinel-sentinel-headless.redis.svc.cluster.local`。Cluster 分别使用 `redis-prod-leader-0.redis-prod-leader-headless.redis.svc.cluster.local` 和 `redis-prod-follower-0.redis-prod-follower-headless.redis.svc.cluster.local`。按实际副本序号与 DNS 后缀签发，或对每组 headless 域使用对应的单级通配 SAN；不得用命名空间级通配符代替全部成员域名。

外部 ACL 同时启用密码时必须包含与密码 Secret 一致的 `default` 用户，Operator、探针、复制和 Sentinel 使用该用户。业务另建命名用户，只允许所需命令和 key 前缀。`default` 是管理身份，不应交给应用。

凭据准备步骤读取已有安全文件，创建带所有权标签的 Redis 命名空间和三个 Secret；不输出文件内容。文件路径是示例输入，执行前须替换为本地受控凭据路径。

```bash
bash <<'REDIS_OPERATIONS_3'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
ns=redis
secret_dir=/secure/redis
for file in password ca.crt tls.crt tls.key user.acl; do test -s "$secret_dir/$file"; done
openssl verify -CAfile "$secret_dir/ca.crt" "$secret_dir/tls.crt"
echo 'Redis 凭据准备：验证文件后创建专属命名空间与 Secret'
# 此命名空间须专用于该 Redis 部署；已有命名空间先核验所有权。
kubectl create namespace "$ns"
kubectl label namespace "$ns" kubeauto.io/managed-by=kubeauto kubeauto.io/component=redis
kubectl -n "$ns" create secret generic redis-auth --from-file=password="$secret_dir/password"
kubectl -n "$ns" create secret generic redis-tls \
  --from-file=ca.crt="$secret_dir/ca.crt" \
  --from-file=tls.crt="$secret_dir/tls.crt" \
  --from-file=tls.key="$secret_dir/tls.key"
kubectl -n "$ns" create secret generic redis-acl --from-file=user.acl="$secret_dir/user.acl"
echo REDIS_CREDENTIALS_CREATED
REDIS_OPERATIONS_3
```

> **重复执行：** 此凭据创建片段是首次准备步骤；同名资源存在时会拒绝，不能添加 `--overwrite` 抢占其它部署的命名空间。轮换应走第 8 章的变更流程。
>
> **未启用 ACL：** 将 `redis_acl_secret` 置空并省略 ACL Secret；仍应启用认证与 TLS。密码仅保护客户端命令，不能代替 Cluster bus TLS 认证。

### 3.3 分发制品并收敛

本步骤读取固定版本清单，将制品同步到部署 Registry，再安装 Operator 和 Redis CR。中国交付优先 TalkEdu，保留固定 Docker Hub 回退。开始前核验 Chart SHA256 和镜像 digest；Tag 名相同不代表身份相同。

```bash
bash <<'REDIS_OPERATIONS_4'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
cluster=redis-prod-cluster
kubecli download -E redis </dev/null
kubecli setup "$cluster" 07 --tags redis </dev/null
kubectl -n redis-operator get deployment,pod
kubectl -n redis get redisreplication,redissentinel,pod,pvc,service,pdb
echo REDIS_DEPLOYMENT_INSPECTED
REDIS_OPERATIONS_4
```

成功收敛必须出现 `REDIS_CONTROL_PLANE_READY` 和 `REDIS_DATA_PLANE_READY`。数据面检查包含预期成员数量、Pod Ready、故障域、PVC Bound、实际主从连接、Sentinel 仲裁与写 Service 端点；Cluster 还检查成员数量和全部 16384 槽。业务验收仍须独立完成，不能用上述标志代替应用读写。

> **异常处理：** 首先保存 CR、Pod/PVC 描述、事件和 Operator 日志。不要先删除 PVC、强行移除 finalizer 或把 Service 指向某个暂时可写的副本。分类和恢复顺序见第 6 章。

### 3.4 核验连接入口

| 用途 | 默认入口 | 使用规则 |
| --- | --- | --- |
| Sentinel 查询 | `redis-prod-sentinel-sentinel.redis.svc:26379` | master group 为 `mymaster`；客户端设置 Sentinel 自身认证 |
| Sentinel 主写 | `redis-prod-master.redis.svc:6379` | 当前主 Service；Sentinel SDK 应通过发现结果刷新连接 |
| Cluster seed | `redis-prod-leader.redis.svc:6379` | 只作为种子；应用须可连接发现的全部成员 |
| 复制指标 | `redis-prod.redis.svc:9121` | Service 发现后逐个 Endpoint 抓取，不能用一次负载均衡请求代表全部实例 |
| Cluster 指标 | `redis-prod-leader.redis.svc:9121`、`redis-prod-follower.redis.svc:9121` | 两组都需逐个 Endpoint 抓取，不能只监控主分片 |
| Sentinel 指标 | `redis-prod-sentinel-sentinel.redis.svc:9121` | 验证 Exporter 是否支持所需 Sentinel 指标 |

名称来源于固定 Operator，不是人为约定。部署名称改变时同步改变前缀；上线前用 `kubectl -n redis get service` 核验。普通 `redis-prod` Service 可能包括副本，不能作为安全的固定写入口。

## 4. 业务接入与上线验收

### 4.1 客户端约束

应用使用维护中的 Redis SDK，配置 CA 信任、服务端主机名验证、客户端证书、认证、连接池、超时和有限重试。Sentinel 配置需要分别给 Sentinel 与 Redis 连接提供凭据；发现 master 后必须重新建立到新主的连接。Cluster 客户端需要缓存槽位并在 MOVED 后更新拓扑，ASK 只针对当前请求临时跳转。

`redis-cli --tls --cacert` 可以验证信任链，但不能据此宣称应用已校验服务端主机名；上线验收还需使用应用 SDK 的主机名验证选项和错误 SAN 证书进行拒绝测试。不要使用 `--insecure`、关闭证书检查或无界重试作为生产设置。

| 行为 | Sentinel | Cluster |
| --- | --- | --- |
| 多数据库 | 可按业务选择 DB | 仅 DB 0，SELECT 非零应失败 |
| 多 key 命令 | 同实例执行，注意大命令阻塞 | 必须同槽；使用一致 hash tag |
| 事务/脚本 | 不等于关系数据库回滚 | 涉及的 key 必须同槽 |
| 副本读取 | 显式接受复制延迟 | SDK 明确开启副本读取并接受滞后 |
| 自动重试 | 超时可能意味着结果未知 | 切换和重定向同样可能造成结果未知 |

> **业务风险：** 非幂等写入不能在超时后盲目重复。计数、扣减和队列消费须采用业务去重、幂等键或明确的补偿规则；不能把连接恢复等同于事务成功。

### 4.2 业务验收清单

在实际应用网络中，以专属验收前缀执行 SET/GET、TTL、删除、pipeline 和应用使用的事务/脚本。Sentinel 还验证主发现、主切换后的连接刷新；Cluster 验证 MOVED/ASK、同槽操作及跨槽拒绝。记录写入值的 SHA256、读取数量、TTL 容差、错误类型和完成时间，验收后只删除本次前缀。

安全拒绝包含错误密码、错误 CA、缺少客户端证书、错误主机名、越权命令和越权 key。网络连接失败不能算认证拒绝；测试必须先证明同一入口使用正确凭据能成功。

二次运行相同产品入口，比较 CR、PVC、Secret、Service 和 Pod UID、数据摘要、Operator release revision 与事件。Helm release revision 可以因 `upgrade` 执行增加；关键是配置内容稳定、PVC/Secret 不被替换、没有非预期数据变化或 Pod 滚动。

## 5. 日常巡检与监控

### 5.1 巡检顺序

先检查业务成功率和延迟，再检查 Redis 内部状态，最后解释 Kubernetes 和 Operator 状态。不要从 Pod Running 反推业务健康。

| 层次 | 检查内容 | 判定重点 |
| --- | --- | --- |
| 业务 | 成功率、P95/P99、超时、重试、读取摘要 | 业务错误不能被 SDK 重试隐藏 |
| Redis | INFO replication/persistence/memory/stats；ROLE | 唯一主、链接 up、复制偏移、持久化最近结果 |
| Sentinel | CKQUORUM、master、replicas、sentinels | 三个视图一致，仲裁可授权 |
| Cluster | CLUSTER INFO/NODES | state=ok；槽位完整；真实主副本关系与故障域 |
| 存储 | PVC 使用率、fsync 延迟、扩容状态 | 留出 rewrite/fork/重建余量 |
| 平台 | Pod Ready/restarts、PDB、事件、Operator 日志 | 分辨探针、调度、挂载与控制器故障 |

平台巡检是只读操作：

```bash
bash <<'REDIS_OPERATIONS_5'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
kubectl -n redis get pod,pvc,service,pdb -o wide
kubectl -n redis get events --sort-by=.lastTimestamp
kubectl -n redis-operator logs deployment/redis-operator --since=15m --all-containers=true
echo REDIS_PLATFORM_INSPECTION_COMPLETE
REDIS_OPERATIONS_5
```

诊断日志按现场保密等级存储；不得导出完整 Secret、密码环境变量或 ACL 原文。慢日志可能包含业务参数，分享前脱敏。

### 5.2 监控与告警

Exporter 与 Operator 指标需要分别抓取。现有 Prometheus 由客户监控平台维护，Redis 部署不自动启用或调整其它监控分支。使用 ServiceMonitor 时确认该集群已安装对应 CRD，标签匹配监控平台选择器，Service 端口与目标端口一致。

| 告警 | 观察窗口与关联信号 | 首要动作 |
| --- | --- | --- |
| 业务超时/错误 | 结合客户端重试、网络和慢命令 | 优先止损、限流，保留业务结果未知请求 |
| 内存接近水位 | used_memory/maxmemory 与 RSS/容器限制分别计算 | 排查大 key、缓冲和 fork；勿只提高硬限制 |
| 复制中断/延迟 | master_link_status、偏移差、积压覆盖时间 | 检查网络/CPU/磁盘；保护写入 |
| 持久化失败 | rdb_last_bgsave_status、aof_last_write_status、rewrite | 检查空间/权限/IO；验证是否已拒写 |
| PVC 高水位 | 75% 初始预警结合增长速度 | 预留 rewrite 空间，走受控扩容 |
| 仲裁不足/槽位异常 | CKQUORUM、cluster_state、slots_fail/pfail | 先恢复成员通信，禁止强制制造新拓扑 |
| 控制器故障 | 副本数、协调错误、API/RBAC/leader-election | 验证数据面，恢复控制面，避免删数据 |

阈值是初始运维建议，须按客户 SLO 校准。告警验收必须包含规则被加载、条件触发、恢复解除和通知路由检查；仅看到 `/metrics` 不等于告警系统可用。

## 6. 故障诊断与恢复

### 6.1 通用处置顺序

1. 固定首个业务错误、时间、对象、命令及原始退出码，保留正常对照。
2. 核验制品 digest、控制器和节点版本；区分 Registry、运行时、Kubernetes、存储、控制器与 Redis 本体。
3. 恢复网络、时间和存储，再确认控制器健康、真实角色/槽位和复制追赶。
4. 验证客户端重新发现拓扑、数据摘要和 TTL，确认错误率恢复。
5. 回收故障注入、临时进程和验收 key；记录未完成的数据修复，不提前宣布恢复。

### 6.2 典型场景

| 场景 | 证据与安全行为 | 恢复方法 |
| --- | --- | --- |
| 副本故障 | 主是否满足最小副本写入保护；副本延迟 | 恢复副本挂载/网络，等待全量或增量同步 |
| Sentinel 主故障 | 唯一新主、Sentinel 三视图、客户端重连 | 等控制器与 Sentinel 收敛；恢复旧主为副本 |
| Sentinel 多数派丢失 | CKQUORUM 拒绝；不能假称自动切换安全 | 恢复 Sentinel 通信与多数派，再恢复切换能力 |
| 网络分区 | 各侧成员视图与实际写入结果；旧主窗口写入 | 恢复网络后按新主拓扑处理业务损失，不手工双主合并 |
| Cluster 主故障 | 可授权副本提升、槽位与客户端刷新 | 恢复成员，校验每主副本数和跨域关系 |
| Cluster 丢槽 | 完整覆盖策略下应拒绝不安全服务 | 优先找回持久化成员；不可用空节点顶替丢失数据 |
| AOF/RDB 失败 | 保存实际错误、文件材料与业务拒写 | 扩容/修复存储后重新持久化并恢复验收 |
| Operator 故障 | 数据面可能仍服务；新变更无法协调 | 修复制品、RBAC/API、配置和 leader election |

> **风险：** 多数派与数据副本是两种不同条件。Sentinel 多数派恢复不代表数据未丢失；Cluster 槽位重新分配也不代表原 key 已恢复。禁止用强制 failover、删除 nodes.conf 或清空 PVC 掩盖数据损失。

## 7. 维护与容量变更

### 7.1 节点维护与滚动重启

维护前确认全部成员健康、数据摘要和备份可恢复，检查真实主副本跨故障域关系以及 PDB。按一次一个故障域操作，观察业务错误与复制追赶；本批未恢复不得开始下一批。

RedisReplication 与 Sentinel 各有 `minAvailable: 2`。Cluster leader 至少保留 `主分片数-1`，follower 最多不可用 1。PDB 控制自愿中断，不阻止主机断电、磁盘故障，也不自动证明每个分片都有可用副本。节点 drain 必须结合实际拓扑判断。

滚动重启之前先通过产品入口收敛所需配置。受控 `rollout restart` 是维护动作，应等待完整业务健康检查；不要以单个 StatefulSet rollout 完成代替 Sentinel/Cluster 拓扑验收。

以下流程在第 3 章的控制端执行，适用于已完成备份恢复验证和应用数据摘要记录的 TLS 部署。变更目录 `/secure/redis-changes/maintenance-01/` 由本次维护操作独占；其中的 CR、PVC 快照和健康命令由运维账号创建，供后续重启或节点维护步骤复用。开始另一批维护时使用新的变更目录。健康命令从本批 CR 快照读取已批准的模式和副本数，不自行推测拓扑。

```bash
bash <<'REDIS_MAINTENANCE_PREPARE'
set -Eeuo pipefail
cluster=redis-prod-cluster
base=/usr/local/kubeauto
export KUBECONFIG="$base/clusters/$cluster/kubectl.kubeconfig"
change_dir=/secure/redis-changes/maintenance-01
test ! -e "$change_dir"
install -d -m 0700 "$change_dir"
echo '[维护] 收敛产品配置并记录 CR、PVC 和中断预算'
kubecli setup "$cluster" 07 --tags redis </dev/null
replication=$(kubectl -n redis get redisreplication redis-prod --ignore-not-found -o name)
if test -n "$replication"; then
    kubectl -n redis get redisreplication redis-prod -o json > "$change_dir/instance.json"
else
    kubectl -n redis get rediscluster redis-prod -o json > "$change_dir/instance.json"
fi
kubectl -n redis get pvc -o json > "$change_dir/pvc-before.json"
kubectl -n redis get pdb -o wide
cat > "$change_dir/health.sh" <<'REDIS_MAINTENANCE_HEALTH'
#!/usr/bin/env bash
set -Eeuo pipefail
change_dir=$(cd "$(dirname "$0")" && pwd)
read -r mode replicas shards followers < <(python3 - "$change_dir/instance.json" <<'PY'
import json,sys
cr=json.load(open(sys.argv[1]))
assert cr['metadata']['name']=='redis-prod' and cr['metadata']['namespace']=='redis'
if cr['kind']=='RedisReplication':
    print('sentinel',int(cr['spec']['clusterSize']),3,2)
else:
    assert cr['kind']=='RedisCluster'
    shards=int(cr['spec']['clusterSize'])
    followers=int(cr['spec']['redisFollower']['replicas'])
    assert shards>=3 and followers>=shards and followers%shards==0
    print('cluster',3,shards,followers//shards)
PY
)
python3 /usr/local/kubeauto/roles/cluster-addon/files/redis-health.py \
    --kubeconfig /usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig \
    --namespace redis --name redis-prod --mode "$mode" --replication-replicas "$replicas" \
    --shards "$shards" --replicas-per-shard "$followers" --tls --wait 600
REDIS_MAINTENANCE_HEALTH
chmod 0700 "$change_dir/health.sh"
bash "$change_dir/health.sh"
echo REDIS_MAINTENANCE_PREPARE_PASS
REDIS_MAINTENANCE_PREPARE
```

选择滚动重启时，下面按 StatefulSet 逐组执行。每组完成控制器滚动和完整 Redis 健康验收后才开始下一组；全部结束后对比 PVC UID，随后通过应用再次核验已记录的数据摘要和业务错误率。

```bash
bash <<'REDIS_MAINTENANCE_ROLLING'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
change_dir=/secure/redis-changes/maintenance-01
test -x "$change_dir/health.sh"
bash "$change_dir/health.sh"
read -r -a groups < <(python3 - "$change_dir/instance.json" <<'PY'
import json,sys
cr=json.load(open(sys.argv[1]))
print('redis-prod redis-prod-sentinel-sentinel' if cr['kind']=='RedisReplication'
      else 'redis-prod-follower redis-prod-leader')
PY
)
for group in "${groups[@]}"; do
    echo "[维护] 滚动重启 $group"
    kubectl -n redis rollout restart "statefulset/$group"
    kubectl -n redis rollout status "statefulset/$group" --timeout=15m
    bash "$change_dir/health.sh"
done
kubectl -n redis get pvc -o json > "$change_dir/pvc-after.json"
python3 - "$change_dir" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
def identities(name):
    return sorted((v['metadata']['name'],v['metadata']['uid']) for v in json.loads((p/name).read_text())['items'])
assert identities('pvc-before.json')==identities('pvc-after.json')
print('REDIS_MAINTENANCE_PVC_RETAINED_PASS')
PY
echo REDIS_MAINTENANCE_ROLLING_PASS
REDIS_MAINTENANCE_ROLLING
```

节点维护需要额外的可调度容量。排空一个节点后，仍须具备至少三个可用故障域、满足反亲和的副本位置、CSI 挂载能力和足够的资源；只有三个可承载数据的节点时，应先增加经验证的替代节点。核对待维护节点上的全部工作负载及其 PDB，完成应用侧变更批准后，再执行标准驱逐。

```bash
bash <<'REDIS_MAINTENANCE_DRAIN'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
change_dir=/secure/redis-changes/maintenance-01
test -x "$change_dir/health.sh"
read -r -p '已审核的待维护节点名: ' maintenance_node </dev/tty
test -n "$maintenance_node"
test "$(kubectl get node "$maintenance_node" -o jsonpath='{.spec.unschedulable}')" != true
bash "$change_dir/health.sh"
kubectl get pods -A --field-selector "spec.nodeName=$maintenance_node" -o wide
kubectl -n redis get pdb -o wide
echo "[维护] 通过 Eviction API 排空 $maintenance_node"
kubectl drain "$maintenance_node" --ignore-daemonsets --timeout=15m
bash "$change_dir/health.sh"
echo "REDIS_MAINTENANCE_DRAIN_PASS node=$maintenance_node state=cordoned"
REDIS_MAINTENANCE_DRAIN
```

> **异常处理：** drain 被 PDB、剩余故障域或 CSI 挂载阻断时，保留节点、Pod、PVC、事件和控制器日志，停止本批。用 `kubectl uncordon <本批节点>` 恢复调度并重新执行本批健康命令；不得通过强制删除 Pod、禁用驱逐或降低 PDB 绕过阻断。涉及 emptyDir 的工作负载必须由其所属应用确认数据可丢弃后单独处理。
>
> **恢复调度：** 节点系统维护完成并恢复 Ready 后，执行 `kubectl uncordon <本批节点>`，重新运行本批 `health.sh`、PVC 身份比较和应用数据摘要验收。原节点恢复可调度即可进入下一批，不要求将已正常运行的成员强行迁回原节点。

### 7.2 内存与 PVC 扩容

默认 `noeviction`：内存达到 maxmemory 时，新增占用内存的写命令可能返回 OOM；读取和部分释放内存操作仍可用。它保护现有 key，不保证所有写都成功。缓存型业务需要淘汰策略时应单独评估并显式设计配置与验收，不以临时 CONFIG SET 作为永久声明。

PVC 扩容前核验 StorageClass 的 `allowVolumeExpansion`、CSI 支持和文件系统处理方式。固定 Operator 0.27.0 会根据 CR 中新的数据卷大小更新既有数据 PVC，并在 StatefulSet 上记录 `storageCapacity`；其原始不可变 volumeClaimTemplates 可以保留旧值。以 PVC 的请求、实际 capacity 和容器文件系统为验收依据，不能因原模板仍是旧值就删除 StatefulSet 或 PVC。

以下为 Sentinel 三数据卷从 20Gi 扩至 40Gi 的操作。执行位置和配置归属沿用第 3 章；变更目录由本次运维操作独占。先完成业务停写或风险允许的在线窗口、独立备份恢复验证与应用侧摘要记录，随后记录平台身份与容量。

```bash
bash <<'REDIS_PVC_BEFORE'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
change_dir=/secure/redis-changes/pvc-expand-01
test ! -e "$change_dir"
install -d -m 0700 "$change_dir"
kubectl get storageclass csi-block -o json > "$change_dir/storageclass.json"
kubectl -n redis get pvc -l app=redis-prod -o json > "$change_dir/pvc-before.json"
python3 - "$change_dir" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
assert json.loads((p/'storageclass.json').read_text())['allowVolumeExpansion'] is True
claims=json.loads((p/'pvc-before.json').read_text())['items']
assert len(claims)==3
for claim in claims:
    assert claim['metadata']['namespace']=='redis' and claim['status']['phase']=='Bound'
    assert claim['spec']['storageClassName']=='csi-block'
    assert claim['spec']['resources']['requests']['storage']=='20Gi'
    assert claim['status']['capacity']['storage']=='20Gi'
print('REDIS_PVC_EXPANSION_INPUTS_VERIFIED members=3 from=20Gi to=40Gi')
PY
chmod 0600 "$change_dir"/*.json
REDIS_PVC_BEFORE
```

在原 `config.yml` 中仅将顶层 `redis_pvc_size` 改为 `"40Gi"`，保留认证、TLS、资源、模式和其它已有配置。Kubeauto 渲染 CR，由 Operator 发起扩容，由 CSI 和节点完成块设备及文件系统处理。

```bash
bash <<'REDIS_PVC_EXPAND'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
change_dir=/secure/redis-changes/pvc-expand-01
test -s "$change_dir/pvc-before.json"
echo 'Redis 扩容：收敛产品声明并等待三个数据 PVC'
kubecli setup redis-prod-cluster 07 --tags redis </dev/null
kubectl -n redis wait '--for=jsonpath={.status.capacity.storage}=40Gi' pvc -l app=redis-prod --timeout=10m
kubectl -n redis get pvc -l app=redis-prod -o json > "$change_dir/pvc-after.json"
python3 - "$change_dir" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
before=json.loads((p/'pvc-before.json').read_text())['items']
after=json.loads((p/'pvc-after.json').read_text())['items']
assert {(c['metadata']['name'],c['metadata']['uid']) for c in before} == {
    (c['metadata']['name'],c['metadata']['uid']) for c in after}
for c in after:
    assert c['spec']['resources']['requests']['storage']=='40Gi'
    assert c['status']['capacity']['storage']=='40Gi' and not c['status'].get('conditions')
print('REDIS_PVC_CAPACITY_VERIFIED members=3 pvc-uids=retained capacity=40Gi')
PY
for ordinal in 0 1 2; do
  pod="redis-prod-$ordinal"
  echo "检查 $pod 的 /data 文件系统"
  kubectl -n redis exec "$pod" -c redis-prod -- sh -ec \
    'total=$(df -Pk /data | awk "NR==2 {print \$2}"); test "$total" -ge 37748736; df -Ph /data'
done
echo REDIS_PVC_FILESYSTEM_VERIFIED
REDIS_PVC_EXPAND
```

文件系统门禁按 40Gi 的 90% 检查可用格式化容量，不以空闲容量代替总容量。完成后通过应用验证既有 key 摘要、绝对过期时间、新写入与复制确认，再次运行同一产品入口并比较 PVC UID、实际容量和业务结果，确认可重复收敛。

> **异常处理：** PVC 请求已增大而 capacity/文件系统未增大时，保存 PVC conditions、事件、CSI controller/node 日志和实际 `df`。CSI 需要重启挂载才能扩展时，按第 7.1 节逐成员维护并重新验收；不要一次重启全部成员。
>
> **不可逆边界：** 已接受的扩容请求与卷容量保持新值。回退其它配置时不得将 `redis_pvc_size` 改回旧值，也不得尝试缩小 PVC/PV；需要减容时创建独立较小目标并迁移验证。
>
> **Cluster 分支：** 使用实际 leader/follower 标签逐数据卷检查；node-conf 卷仍固定 1Gi，不由 `redis_pvc_size` 扩容。扩容结束后额外核验每个分片与副本、全部 16384 槽和无迁移状态。

### 7.3 Cluster 扩缩与槽位迁移

本流程使用 `0.27.0-kubeauto.2` Operator 镜像及受保护 Cluster 生命周期。Chart/CRD 版本仍为 `0.27.0`；维护镜像参数 `redis_operator_image_ver` 时，不能把 Chart 版本直接当作镜像版本。变更期间保留最小副本写保护，先验证复制，再迁移槽位。业务使用的 Functions 必须在新增分片上保留定义和执行结果。

> **安全拒绝：** 新节点已有数据或 Functions 冲突时停止接入；待缩分片包含计划保留的副本、复制未收敛或仍有源槽位时停止退役。保留变更记录、业务数据和卷，重新核对真实复制图与退役预算。不得降低写保护、清空库或手工遗忘保留成员来绕过拒绝条件。

先计算迁移后的主数、每主副本数、存储和网络峰值，记录槽位、key 摘要和 TTL。变更 `redis_cluster_shards` 会改变 leader 数，follower 数按主数与每主副本数计算。必须先在相同版本的独立目标验证控制器扩缩流程；扩容新增节点就绪并不代表槽位已完成迁移。

迁移过程中持续观察 ASK/MOVED、业务错误、slot ownership 和复制状态。完成条件包括无 importing/migrating 槽、完整 16384 槽、预期主副本数、真实复制关系、业务摘要与 TTL。缩容必须先确认待删除节点无槽且无独有数据；禁止直接缩 StatefulSet 或删除 PVC。

产品以每主声明副本数设置 `cluster-migration-barrier`，避免新增分片时自动迁移抽走原分片的正常副本预算。变更前核验每个实际成员的该参数，而非只检查 ConfigMap；对既有实例，启动配置更新不等于运行参数已经生效。若真实副本数不符、部分成员只认识较少节点，或仍有迁移标志，停止后续批次，保留各成员 `CLUSTER NODES`、`CLUSTER INFO`、复制状态及控制器日志。

以三主、每主两个副本扩展为四主为例，保持 `redis_cluster_replicas: 2`，将目标部署目录的 `config.yml` 中 `redis_cluster_shards` 改为 `4`。预留三个新成员的资源：一个主、两个副本，以及各成员的数据卷和 node-conf 卷；保留迁移期间原成员的容量。停止其它拓扑变更，启动应用侧持续读写与 TTL 核验后，执行以下产品入口：

```bash
bash <<'REDIS_OPERATIONS_7'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
cluster=redis-prod-cluster
kubecli setup "$cluster" 07 --tags redis </dev/null
kubectl -n redis get rediscluster redis-prod -o wide
kubectl -n redis get pod,pvc,pdb -o wide
kubectl -n redis-operator logs deployment/redis-operator --all-pods=true --all-containers=true --since=15m
echo REDIS_CLUSTER_CHANGE_INSPECTED
REDIS_OPERATIONS_7
```

产品入口必须先完成控制面与数据面健康检查；四主、八副本应有十二个成员，每个成员的视图均须包含这些成员。随后用应用 Cluster SDK 对变更前的验收数据逐项读取，核对值、类型和绝对到期时间；确认新主确实持有槽位且参与读写，再登记扩容完成。上例最后的检查标志只表示命令已执行，业务摘要和拓扑验收仍是完成条件。

缩回三主时，在同一部署配置将 `redis_cluster_shards` 改为 `3`，保持其它参数，执行同一入口；等待 Operator 迁移槽位并移除多余成员后，核验九成员、三主各两个副本、全部槽位及原业务数据。原有 PVC UID 应保留，扩容新增但缩容后闲置的保留卷须单独登记。确认成员移除、数据迁移及回退窗口结束前，不删除这些卷；PVC 保留策略也不能替代迁移验收。

> **异常处理：** 槽位全部覆盖仍可能伴随副本不足，不能降低健康门槛后继续缩容。先通过原产品配置请求回退并核验完整成员；回退未收敛时保留数据卷和首失败，按实际复制图恢复成员关系。不要通过手工删除成员记录、强制复制重挂或清空卷，把拓扑异常包装成成功。

## 8. 凭据与证书轮换

### 8.1 变更准备与执行边界

轮换前记录 Secret 名称/版本标识、证书指纹与到期日、Pod/PVC UID、当前角色和业务摘要。避免在同一批次同时改变密码、CA 和 Redis 版本，否则首次失败难以定位。

沿用第 3 章的部署目录和 kubeconfig。凭据管理系统应提供本次批准的 Secret 清单与原材料的安全备份；清单须保留既有名称、命名空间、UID 所对应的资源身份及最新 `resourceVersion`。使用 `kubectl -n redis replace -f /secure/redis-change/<阶段清单>.yaml` 更新已有 Secret；遇到版本冲突先重新读取资源并核对并发变更，不使用 `--force` 删除重建。清单和备份应为属主私有，不能通过终端输出 Secret 全文。

本节的滚动执行方法适用于第 3 章的 Sentinel 示例：依次执行 `kubectl -n redis rollout restart statefulset/redis-prod`、`kubectl -n redis rollout status statefulset/redis-prod --timeout=10m`，再对 `statefulset/redis-prod-sentinel-sentinel` 执行相同两步。随后运行 `kubecli setup redis-prod-cluster 07 --tags redis`，取得控制面与数据面就绪标志，并完成第 4.2 节的实际业务验收。每组等待成功后才操作下一组；Pod Ready 或 Secret 更新成功均不能代替复制和业务验证。

> **拓扑边界：** 上述两个 StatefulSet 名称只适用于 Sentinel。Cluster 的证书变更须另行安排逐主副本组的维护批次，维持每主两个健康副本及完整槽位；不能把两角色全量重启当作本节已验证的轮换流程。

### 8.2 密码与 ACL 轮换

密码改变会同时影响 Operator、探针、复制、Sentinel 和应用。Secret 值更新并不保证现有容器环境或已生成配置自动刷新。先设计双凭据窗口：ACL 支持新旧凭据并存时，先发新凭据、更新客户端，再滚动成员，最终撤销旧凭据；默认用户与 Operator Secret 的匹配在每一步都必须成立。

> **固定版本限制：** 初始化配置会核验 ACL 默认用户与密码 Secret 一致。不能先更新 Secret、随后才更新 ACL，让新建 Pod 进入初始化失败。Sentinel 自身密码与其 Redis auth-pass 也要协调。无已验证双凭据窗口时安排停写维护窗口，不承诺零中断密码轮换。

停写窗口的主线路为：暂停应用写入并等待已提交请求完成；凭据系统准备相互匹配的新 `redis-auth` 和 `redis-acl` 清单；更新这两个 Secret 后按第 8.1 节滚动两组成员；更新应用凭据并验证合法读写、旧 Redis/Sentinel 密码拒绝、ACL 命令和 key 边界、原值与 TTL、PVC UID 保留，最后恢复写入。两项 Secret 更新之间不是事务，应保持维护窗口并监控意外重建；任一更新失败立即停止，协调恢复匹配的原密码与原 ACL，再滚动和验收，不能带着不匹配的材料继续。

### 8.3 CA 与叶子证书轮换

CA 轮换先下发新旧 CA 信任包，再更新叶子证书，逐成员验证服务、复制和指标，最后撤旧 CA。叶子证书必须保持 SAN、用途和客户端证书需求。应用主机名验证、错误 CA/旧凭据拒绝以及持续业务错误率都是轮换验收的一部分。

| 阶段 | `redis-tls` 的材料 | 本阶段执行与退出条件 |
| --- | --- | --- |
| 重叠信任 | `ca.crt` 包含旧、新 CA；保留旧叶子证书和私钥 | 先让应用信任两代 CA，再更新 Secret；按第 8.1 节滚动并核验业务和复制 |
| 新叶子 | 保留两代 CA，替换为新叶子证书及匹配私钥 | 再次滚动；实际 Sentinel 发现名称通过主机名校验，所有成员、Exporter 和应用连接正常 |
| 撤销旧 CA | 仅保留新 CA、新叶子证书和私钥 | 再次滚动并验收；旧 CA、旧客户端证书和错误 SAN 均拒绝，新材料完成真实读写后才关闭变更 |

每阶段由凭据系统刷新清单的资源版本后执行更新，不能反复重放首次导出的 YAML。失败时先保存首失败、事件和控制器日志；在已核验的重叠信任窗口内恢复上一阶段材料，必要时恢复原 CA、原叶子证书和原私钥，重新滚动并核验复制、业务和 PVC 身份。应用撤销旧 CA 必须晚于服务端新叶子验收，不能只回退服务端而留下客户端不兼容的信任链。

在第一次修改 Secret 前，用客户实际使用的 OpenSSL/JDK/客户端库验证重叠信任包分别信任旧、新叶子证书。两代 CA 应有可区分的主体标识，叶子证书应有 Authority Key Identifier；缺少签发者标识时，同名 CA 可能导致 `certificate signature failure`。发现此错误先检查证书链，保留原 Secret 版本，恢复原信任材料并完成拓扑、复制和业务验收，不通过关闭证书验证恢复服务。

Sentinel 会返回当前主的稳定 Pod 完整域名。签发证书时核对 Pod 的 `spec.hostname`、`spec.subdomain`、命名空间和集群 DNS 后缀；例如 `redis-prod-0.redis-prod-headless.redis.svc.cluster.local`。`*.redis.svc.cluster.local` 只能匹配一级名称，不能覆盖上述多级成员名称。CA 轮换完成后，既要通过实际发现名称连接，也要确认旧 CA 和错误 SAN 被拒绝。

## 9. 数据保护与恢复

### 9.1 保护层次与备份契约

RDB/AOF 是实例恢复材料；PVC 保留是删除保护；只有独立故障域内的可验证材料和成功恢复演练才构成备份。当前配置的 `redis_backup_enabled` 为保留开关，开启会明确拒绝，不能把它当作已实现的自动备份计划。

外部备份系统应使用受控凭据和 TLS，从已确定角色的成员生成 RDB，并将快照与元数据复制到独立存储。元数据至少包括版本、拓扑、各分片槽范围、起止时间、停写边界、文件大小、SHA256、业务数量/摘要、TTL 验收时间和恢复工具版本。

Cluster 逐分片导出不是全局一致快照。需要业务一致性时，应停止写入、等待已提交业务完成和复制追赶，再固定槽位拓扑导出；跨分片事务或外部业务系统需额外协调。备份期间不得同时迁移槽位。

### 9.2 导出可核验的独立快照

一次性导出复用已分发的固定 Redis 镜像，创建短期客户端 Pod，在其临时卷中通过 `redis-cli --rdb` 导出并执行 `redis-check-rdb`，将文件流式传输到控制端，核对 SHA256 后原子发布。它不写入源 PVC，也不删除源 CR/PVC；临时 Pod 在成功或失败后限定回收。

在目标 `config.yml` 设置新的专用输出目录，启用一次性请求：

```yaml
redis_snapshot_requested: true
redis_snapshot_directory: "/secure/redis-backups/snapshot-20261012-01"
```

执行只选择快照任务的产品入口，随后检查文件、元数据与摘要；下次导出必须使用新的目录，同名目录或文件不会被覆盖。

```bash
bash <<'REDIS_OPERATIONS_6'
set -Eeuo pipefail
export KUBECONFIG=/usr/local/kubeauto/clusters/redis-prod-cluster/kubectl.kubeconfig
kubecli setup redis-prod-cluster 07 --tags redis-snapshot </dev/null
backup_dir=/secure/redis-backups/snapshot-20261012-01
python3 - "$backup_dir" <<'PY'
import hashlib,json,sys
from pathlib import Path
directory=Path(sys.argv[1])
manifest=json.loads((directory/'manifest.json').read_text())
assert manifest['files']
for entry in manifest['files']:
    path=directory/entry['file']
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    assert path.stat().st_size==entry['size'] and digest.hexdigest()==entry['sha256']
print('REDIS_SNAPSHOT_FILES_VERIFIED')
PY
REDIS_OPERATIONS_6
```

产品终端标志为 `REDIS_SNAPSHOT_EXPORTED`，表示文件已完成结构检查、传输校验和临时 Pod 清理。`manifest.json` 明确记录 `restore_verified: false`；不得将导出标志改称完整备份成功。还应将整个目录复制到独立故障域，核验副本摘要并完成下一节的恢复演练，再登记备份可用。

> **异常处理：** 失败目录不能作为成功备份；只有所有文件与完整 manifest 同时存在并通过校验才可进入恢复资格检查。保留首次失败和已导出材料用于诊断，按请求目录处理，不覆盖其它备份。Cluster 各文件对应当时真实主节点与槽位，不自动保证同一时刻一致。

### 9.3 恢复流程

本版提供一次性导出和独立 RDB 恢复验证，不提供向运行中 Operator PVC 自动注入数据的配置项。`kubecli restore` 的 Kubernetes 控制面恢复不能替代 Redis 数据恢复。先证明材料可以独立加载和回读，再实施生产恢复变更。

下面给出小规模 Sentinel 备份的离线验证主路径，适用于已具备 Docker 的备份控制端。它用备份登记的固定镜像、新的容器临时卷与 Unix socket 加载数据，容器没有网络，不挂载源 PVC，也不修改原备份。临时数据在容器结束时回收。示例的 2 GiB 内存上限包含临时卷和 Redis RSS；较大的数据集须按实测解压后大小提高上限，模块数据须使用相同模块与加载配置。

| 执行契约 | 内容 |
| --- | --- |
| 输入 | 第 9.2 节的 Sentinel 备份目录；备份时登记且尚未过期的字符串校验键、原值 SHA256、恢复时预期 key 数量 |
| 影响 | 创建并自动删除唯一离线容器；原 RDB 从标准输入只读复制到临时卷 |
| 产物 | 文件 CRC、key 数量、原值摘要、新写入回读与进程重启保留结果 |
| 幂等 | 每次创建新容器与临时数据，不覆盖原备份或运行中的实例 |
| 验收 | 下列所有断言通过且容器已退出并删除，最后输出唯一完成标志 |

先在当前终端设置 `RESTORE_PROBE_KEY`、`RESTORE_PROBE_SHA256` 和 `RESTORE_EXPECTED_KEYS`。预期摘要来自备份前的业务记录，不能从恢复目标反向生成。key 数量应考虑期间已经到期的数据；这个字符串样例不能替代应用对其他类型、权限及 TTL 的完整验收。

```bash
bash <<'REDIS_OFFLINE_RDB_VERIFY'
set -Eeuo pipefail
backup_dir=/secure/redis-backups/snapshot-20261012-01
: "${RESTORE_PROBE_KEY:?设置备份时登记的字符串校验键}"
: "${RESTORE_PROBE_SHA256:?设置原值的 SHA256}"
: "${RESTORE_EXPECTED_KEYS:?设置恢复时预期 key 数量}"
restore_run=${RESTORE_RUN_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')}
[[ "$restore_run" =~ ^[0-9a-f]{32}$ ]]
container=redis-offline-$restore_run
cleanup() {
  local id
  id=$(docker container ls -aq --filter "name=^/${container}$")
  if [[ -n "$id" ]]; then
    [[ "$(docker inspect --format '{{index .Config.Labels "kubeauto.io/recovery-run"}}' "$id")" == "$restore_run" ]]
    docker rm -f "$id" >/dev/null
  fi
}
trap cleanup EXIT
echo '[1/3] 核验原始备份身份、大小与 SHA256'
image=$(python3 - "$backup_dir" <<'PY'
import hashlib,json,re,sys
from pathlib import Path
root=Path(sys.argv[1]);m=json.loads((root/'manifest.json').read_text())
assert m['mode']=='sentinel' and len(m['files'])==1
r=m['files'][0];assert r['file']=='replication.rdb'
p=root/r['file'];assert p.is_file() and not p.is_symlink()
data=p.read_bytes()
assert len(data)==r['size'] and len(data)>16 and data[:5]==b'REDIS'
assert hashlib.sha256(data).hexdigest()==r['sha256']
assert re.fullmatch(r'registry\.talkschool\.cn:5000/brinnatt/redis:8\.[0-9]+\.[0-9]+',m['image'])
print(m['image'])
PY
)
echo '[2/3] 离线加载，核验业务并重启独立进程'
docker run --rm -i --name "$container" --label "kubeauto.io/recovery-run=$restore_run" \
  --network none --user 999:999 --cap-drop ALL \
  --security-opt no-new-privileges --memory 2g --cpus 1 \
  --tmpfs /work:rw,nosuid,nodev,size=1g,mode=1777 \
  -e RESTORE_PROBE_KEY -e RESTORE_PROBE_SHA256 -e RESTORE_EXPECTED_KEYS \
  "$image" bash -euo pipefail -c '
    cat > /work/dump.rdb
    redis-check-rdb /work/dump.rdb
    start() {
      redis-server --daemonize yes --port 0 --unixsocket /work/redis.sock \
        --unixsocketperm 700 --dir /work --dbfilename dump.rdb --appendonly no \
        --save "" --logfile /work/redis.log
      for attempt in $(seq 1 60); do
        if [ "$(redis-cli -s /work/redis.sock -e --raw PING 2>/dev/null || true)" = PONG ]; then return; fi
        echo "RDB 加载等待：attempt=$attempt/60"
        sleep 1
      done
      cat /work/redis.log >&2
      return 1
    }
    cli() { redis-cli -s /work/redis.sock -e --raw "$@"; }
    verify() {
      [ "$(cli TYPE "$RESTORE_PROBE_KEY")" = string ]
      # --raw GET adds one newline; remove only that byte before hashing.
      digest=$(cli GET "$RESTORE_PROBE_KEY" | head -c -1 | sha256sum)
      [ "${digest%% *}" = "$RESTORE_PROBE_SHA256" ]
    }
    start
    [ "$(cli DBSIZE)" = "$RESTORE_EXPECTED_KEYS" ]
    verify
    run=$(cli INFO server | tr -d "\r" | sed -n "s/^run_id://p")
    [ -n "$run" ]
    [ "$(cli SET "recovery:$run" "$run" EX 600)" = OK ]
    cli SHUTDOWN SAVE
    start
    after=$(cli INFO server | tr -d "\r" | sed -n "s/^run_id://p")
    [ -n "$after" ] && [ "$after" != "$run" ]
    verify
    [ "$(cli GET "recovery:$run")" = "$run" ]
    cli SHUTDOWN NOSAVE
  ' < "$backup_dir/replication.rdb"
cleanup
[[ -z "$(docker container ls -aq --filter "name=^/${container}$")" ]]
trap - EXIT
echo '[3/3] 独立目标已退出，临时卷已回收，原材料保留'
echo REDIS_OFFLINE_RDB_VERIFY_PASS
REDIS_OFFLINE_RDB_VERIFY
```

> **异常处理：** 加载失败或 OOM 时保留首失败及镜像身份，先区分格式、模块、数据完整性与恢复资源不足。示例临时卷不是事故取证存储；需要保存目标日志或文件时，改用预先登记的独立恢复卷，并明确人工回收范围。

生产恢复继续按下列顺序执行，独立验证容器不能直接作为生产实例：

1. 校验备份元数据、签名/摘要、非零文件、可读取性和版本兼容，不覆盖源材料。
2. 创建独立命名空间、独立 CR 和独立 PVC，目标客户端入口与生产隔离。
3. 按备份模式恢复数据；Cluster 还需恢复正确的槽位归属与复制拓扑，不能把一个分片 RDB 当作全量备份。
4. 核验 key 数量、数据摘要、类型、TTL 容差、权限和业务读写；恢复期间的时间流逝应从 TTL 预期中扣除。
5. 在业务批准的维护窗口切换客户端，再验收错误率和拓扑；保留原目标以满足回退窗口。

> **恢复风险：** RDB/AOF 格式不保证可向任意旧版本降级。失败时保存目标日志与 PVC，不自动覆盖生产；只有备份经独立目标成功恢复后，才能用于升级或灾难恢复的回退承诺。

## 10. 性能与稳定性验收

性能测试使用独立前缀或独立实例，固定 key/value 大小、数据类型、TTL、连接数、pipeline、读写比例、TLS、持久化和持续时间。先建立无故障基线，再测主切换、复制重建、持久化 rewrite 与槽位迁移期间的负载。

| 输出 | 判定方式 |
| --- | --- |
| 吞吐 | 明确每秒业务操作、请求与命令计数口径 |
| P50/P95/P99 | 报告完整采样窗口，不只报告平均值 |
| 错误率 | 分开记录拒写、超时、重定向、网络和权限错误；正常基线业务错误必须为零 |
| 资源 | CPU/限流、RSS/容器内存、磁盘/fsync、网络、fork 时长 |
| 正确性 | 写入数量、读取摘要、TTL 和失败后的结果未知请求 |
| 稳定性 | 长时间趋势、重连、重启、主切换、积压清空和恢复时间 |

不要把短时间峰值当作稳定容量。调优一次只改变一个因果参数，保留原始样本和负载配置。热点 key、大 value、阻塞脚本、深 pipeline 和批量过期应分别测试，避免总吞吐掩盖尾延迟或单分片瓶颈。

## 11. 升级、回退与下线

### 11.1 升级

升级包须同时锁定 Operator、Chart/CRD、Redis、Exporter、镜像 digest 与备份格式。先验证目标版本兼容性和独立恢复，再备份当前 CR、拓扑、PVC UID、数据摘要与配置。更新 CRD 的行为不同于更新 Helm 模板：Helm 的 `crds/` 在已有 release 上不自动升级，须按锁定版本官方流程执行受控 CRD 更新与验收。

在修改配置或执行产品收敛前，逐数据成员核验 `CONFIG GET appendfilename`、多段 AOF manifest 与备份登记。此交付包自首次部署起固定使用 `appendonly.aof`；Operator 0.26 的默认值为 `Appendonly.aof`，0.27 的默认值为小写。文件名区分大小写，PVC 保留不能证明原 AOF 被读取。已经使用大写默认值的外部旧部署必须先按第 9.3 节在独立目标恢复并迁移客户端，不得直接套用此包或重命名多段 AOF 文件。

> **版本边界：** 原地迁移与回退只适用于已经核验完整制品、相同持久化文件身份和数据格式的版本集合；保留前向 CRD。模块数据、跨 Redis 主版本降级及任意旧 Operator 均须单独验证。旧 Chart 和旧镜像必须作为受控回退材料预先分发，默认 `kubecli download -E redis` 下载当前生产集合，不会自动提供任意历史版本。

在第 3 章的 Kubeauto 控制端，以同一账号和部署目录执行。下列变更目录由操作人创建，保存变更前配置、CR、PVC 和 Operator 状态；它不属于业务存储，也不能代替第 9 章的独立备份。目录已存在时拒绝覆盖，先核对并归档上一轮变更。

```bash
bash <<'REDIS_UPGRADE_PREPARE'
set -Eeuo pipefail
base=/usr/local/kubeauto
cluster=redis-prod-cluster
change="$base/clusters/$cluster/redis-upgrade-change"
export KUBECONFIG="$base/clusters/$cluster/kubectl.kubeconfig"
echo 'Redis 升级准备：确认当前产品收敛并保存变更前状态'
kubecli setup "$cluster" 07 --tags redis </dev/null
mkdir -m 0700 "$change"
install -m 0600 "$base/clusters/$cluster/config.yml" "$change/config.before.yml"
kubectl -n redis get redis,redisreplication,redissentinel,rediscluster \
  -l kubeauto.io/component=redis -o json > "$change/instances.before.json"
kubectl -n redis get pvc -o json > "$change/pvc.before.json"
kubectl -n redis-operator get deployment redis-operator -o json > "$change/operator.before.json"
chmod 0600 "$change"/*.json
echo "REDIS_UPGRADE_PREPARE_PASS change=$change"
REDIS_UPGRADE_PREPARE
```

先更新控制器：在已有 `config.yml` 中替换以下顶层键，保留业务名称、模式、Secret 引用、存储和 Redis 运行版本。这里的版本对应本交付包；未来版本必须使用已完成兼容性和制品验证的配套包，不能只替换镜像标签。

```yaml
redis_operator_ver: "0.27.0"
redis_operator_image_ver: "0.27.0-kubeauto.2"
redis_crd_upgrade_requested: true
```

`redis_crd_upgrade_requested` 是一次显式产品请求，由 Redis Ansible 任务调用 `redis-crd.py` 消费。它读取交付包内已校验 Chart 的四项 CRD，检查现有 API 身份、存储版本和所有命名空间的消费者，在服务端预检全部通过后，将原 schema 保存到部署目录 `yml/redis/crd-backups/crd-<UUID>/before.json`，再更新 schema 并等待 `Established`。已有 CR 必须全部位于当前 Redis 命名空间且带本产品所有权标签；涉及其它租户时拒绝执行。schema 备份由本次升级维护人保存并归档，后续回退评审继续使用。

```bash
bash <<'REDIS_UPGRADE_OPERATOR'
set -Eeuo pipefail
base=/usr/local/kubeauto
cluster=redis-prod-cluster
change="$base/clusters/$cluster/redis-upgrade-change"
export KUBECONFIG="$base/clusters/$cluster/kubectl.kubeconfig"
test -s "$change/config.before.yml"
echo 'Redis 控制器升级：先预检与备份 CRD，再收敛 Operator 和现有实例'
kubecli setup "$cluster" 07 --tags redis </dev/null
kubectl -n redis-operator rollout status deployment/redis-operator --timeout=10m
kubectl -n redis get pvc -o json > "$change/pvc.after-operator.json"
python3 - "$change/pvc.before.json" "$change/pvc.after-operator.json" <<'REDIS_UPGRADE_PVC_CHECK'
import json, sys
def identities(path):
    return {(r['metadata']['name'], r['metadata']['uid'], r['spec'].get('volumeName'))
            for r in json.load(open(path))['items']}
if identities(sys.argv[1]) != identities(sys.argv[2]):
    raise SystemExit('Redis 升级阻断：PVC 或底层 PV 绑定身份发生变化')
print('REDIS_UPGRADE_PVC_IDENTITY_PASS')
REDIS_UPGRADE_PVC_CHECK
echo REDIS_UPGRADE_OPERATOR_PASS
REDIS_UPGRADE_OPERATOR
```

日志必须同时出现 `REDIS_CRD_UPGRADE_PASS`、`REDIS_CONTROL_PLANE_READY` 和 `REDIS_DATA_PLANE_READY`。重复执行相同 schema 请求会预检并核验，不重复创建无变更备份。完成后把 `redis_crd_upgrade_requested` 恢复为 `false`，保留升级后的控制器版本；该开关不用于自动降级或强制抢占 schema 字段。

控制器验收通过后，再在同一配置中更新已批准的 Redis 运行镜像版本：Sentinel 使用 `redis_ver`，Cluster 使用 `redis_cluster_image_ver`。保留其它参数，通过 `kubecli download -E redis` 和 `kubecli setup redis-prod-cluster 07 --tags redis` 执行滚动变更。每批检查版本、真实角色、复制/槽位、认证/TLS、持久化和第 4.2 节的业务摘要，并按上述方法比较 PVC 身份；全部业务验收完成后才解除维护窗口。存在未解释的警告、错误率或重建行为时停止下一批。

> **异常处理：** checksum、共享消费者、存储 API 版本或 schema 字段所有权冲突失败时，产品在相应前置条件处停止。保存首次错误和渲染输入，核实 CRD 当前状态及日志中的备份路径；禁止添加 `--force-conflicts`、删除 CRD 或绕过消费者检查来继续升级。共享 schema 由平台维护人统一评审；恢复已验证的配置后，重新从当前状态核验，而不是假定失败意味着所有资源均未变化。

> **回退：** 回退前核验 CRD 与数据格式的兼容性。不能以 `helm rollback` 代替 CRD 或 Redis 数据回退；不兼容时采用独立备份恢复与客户端迁移。Redis 自动生成文件及槽位状态不得被旧镜像静默覆盖。

8.10.1 不支持 8.10.2 的 `cluster-bus-port-protected-mode` 参数，本交付包按请求的 Cluster 镜像版本渲染对应启动配置。旧版本回退应限定固定拓扑、TLS 和维护访问边界，不承诺旧版本具备新增总线保护。看到 `Unresolved Configuration(s)` 时，先比对请求版本的官方参数表与生成配置，不能删除全部保护参数后继续上线。

> **StatefulSet 回退阻塞：** 若错误版本已产生未就绪 Pod，恢复正确模板后，OrderedReady 仍可能等待该 Pod。先核验产品 CR 和两组 StatefulSet 的镜像、配置已经恢复，再依据 Pod 的 controller owner、UID 和实际失败状态，限定删除使用错误模板的未就绪 Pod，让 StatefulSet 按已恢复模板重建；保留 PVC，等待产品健康与原业务数据验收。不要删除健康成员、批量重启两组或修改更新分区来掩盖阻塞。生产操作由本次维护负责人按实际身份执行；实验室恢复入口不用于客户资源。

### 11.2 下线

先停止业务写入并确认没有生产客户端，完成独立备份和恢复校验，然后删除 Redis CR，等待 finalizer 在 Operator 存活时完成。确认受控资源清理和 PVC 保留状态后，才卸载 Operator。共享 CRD 仍有其它消费者时必须保留。

实验用 `tests/helpers/redis-cleanup.sh` 会删除其拥有的命名空间和卷，只用于固定实验现场，不是客户生产数据删除入口。客户下线必须单独审核 PVC/PV、凭据、备份保留和回退窗口，不能直接调用实验清理脚本。

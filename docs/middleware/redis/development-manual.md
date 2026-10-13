# Redis 开发手册

## 1. 代码边界

Redis 入口由 `conf/config.yml`、`common/constants.py`、`service/cluster/manager.py`、`roles/cluster-addon/tasks/redis.yml` 和 `roles/cluster-addon/templates/redis/` 组成。制品放在 `roles/cluster-addon/files/` 并由 SHA256 文件校验。专项 runner、矩阵、cleanup 和单元/契约测试必须独立于 Kafka、PXC、日志和 `tools/`。

调用链为：

```text
kubecli download -E redis </dev/null
  -> KubeConstant.component_images["redis"]
  -> TalkEdu 优先、Docker Hub 固定回退、本地 Registry manifest 校验
kubecli setup redis-prod-cluster 07 --tags redis </dev/null
  -> cluster-addon/main.yml
  -> redis.yml
  -> Helm Redis Operator + Redis CR
  -> Operator StatefulSet/Service/PVC/故障收敛
```

## 2. 版本和制品契约

| 组件 | 固定版本 | 官方来源 | 产品镜像 |
| --- | --- | --- | --- |
| Operator Chart/CRD | 0.27.0 | OT-CONTAINER-KIT/redis-operator | 固定 Chart 与 CRD 制品 |
| Operator runtime | 0.27.0-kubeauto.2 | 官方 v0.27.0 固定提交与受保护 Cluster 生命周期补丁 | `brinnatt/redis-operator:0.27.0-kubeauto.2` |
| Sentinel Redis/Sentinel runtime | 8.10.2 | `redis/redis` 官方镜像 | `brinnatt/redis:8.10.2` |
| Cluster Redis runtime | 8.10.2-kubeauto.1 | 官方 8.10.2 源码与 ASM 状态刷新补丁 | `brinnatt/redis:8.10.2-kubeauto.1` |
| Redis Exporter | v1.93.0 | `oliver006/redis_exporter` | `brinnatt/redis-exporter:v1.93.0` |

Chart 文件 `redis-operator-0.27.0.tgz` 与 `.sha256` 必须一起更新；不得运行时从公网拉 Chart、CRD 或动态发现镜像。新增或变更镜像必须在 `kubeauto-ext-images-dockerfile` 的 `middleware/redis/` 下注册并通过 `python3 scripts/validate_catalog.py`。

Chart 使用上游 `helm-charts` 的完整官方发布包，SHA256 为 `4eabe2cde88657f8b7b21c2ec5ddafa3a305d35c9877021f9be89ac3beeb0f7f`；相邻的 provenance 文件记录 release、asset ID 和来源。不得裁剪模板或自行重新打包后沿用官方制品身份。`rbac.aggregate: false` 明确关闭向 Kubernetes 内置 `view/edit/admin` 角色聚合 Redis CR 权限；Operator 自身仍使用专属 ClusterRole。

`redis_operator_ver` 选择 Chart/CRD，`redis_operator_image_ver` 选择 Operator 与初始化 agent 的同版本镜像，两者不能混用。镜像构建基于官方提交 `71954323c9191e6171f3eb04f80bb46a5f128306`，构建上下文位于制品仓 `middleware/redis/protected-operator/`；源码校验、零 fuzz 补丁、单元测试和 AMD64/ARM64 编译均在构建路径中执行，最终保留官方运行镜像基线。原 `0.27.0` 镜像仍作为独立制品保留。

`redis_ver` 选择 Sentinel 模式的 Redis 与 Sentinel 镜像；`redis_cluster_image_ver` 独立选择 Cluster 运行镜像，快照校验使用所属模式的同一版本。Cluster 修订基于官方 8.10.2 完整源码，仅为 `ASM_EVENT_TAKEOVER` 既有的 `CLUSTER_TODO_BROADCAST_PONG` 追加 `CLUSTER_TODO_UPDATE_STATE`，使接收方在下一事件循环前重算主节点数量与仲裁状态。不能以放宽健康门禁、临时降低复制预算或改用传统迁移来掩盖状态缓存缺陷。源码依据为 [ASM takeover](https://github.com/redis/redis/blob/8.10.2/src/cluster_legacy.c) 与 [Redis CLI 迁移选择](https://github.com/redis/redis/blob/8.10.2/src/redis-cli.c)。

修订镜像的构建上下文为制品仓 `middleware/redis/state-refresh-redis/`。固定基础 image manifest、完整源码 archive SHA256、目标源码 SHA256 与零 fuzz 补丁；只替换 `redis-server`，保留官方 CLI、模块库、入口及 Sentinel/check symlink，匹配官方 TLS、protected-mode 与 jemalloc 构建设置。两种架构的原版负控、补丁正控、官方模块/JSON/TLS smoke 及进程回收必须全部通过，再双推与读回两端 digest。构建 fixture 的参数不得进入最终镜像或客户配置；ARM64 生产启动保留官方内核安全检查。

已有 release 的 CRD 更新使用显式 `redis_crd_upgrade_requested`，默认关闭。`redis-crd.py` 消费同一份固定 Chart 与 checksum 清单，读取四项 CRD 的全部消费者，拒绝其它命名空间或无本产品标签的 CR，拒绝删除存储 API 版本及接管外部 conversion webhook。全部 schema 先经过服务端 dry-run，再保存权限 `0600` 的原 schema，使用同一 field manager 更新并核验。UID/resourceVersion 作为并发前置条件；不使用 `--force-conflicts`。原 schema 备份在部署目录内，不能随源代码同步覆盖。kubectl 对多段清单可逐对象输出 JSON，解析器必须完整消费输出流并校验四项定义，不能假定输出一定是单个 `List`。

Cluster CR 显式声明 `kubeauto.io/protected-cluster-lifecycle: v1`，由同一个 Operator 控制扩缩。新增主必须是无槽、无数据、无 Functions 的独立成员；通过 `CLUSTER MEET` 接入后，为缺少副本的主显式指定新增副本，保留既有复制归属。全部主副本数量、在线复制和同步状态满足声明后，才复制 Functions 并迁移槽位。Functions 按名称、引擎、源码和函数定义比较，不以字典迭代顺序造成的二进制 dump 差异判定冲突；有冲突时拒绝覆盖。整个过程不降低 `min-replicas-to-write`。RedisReplication、RedisSentinel 与未声明该注解的 Cluster 保留上游处理路径。

`protected-topology.go` 将实际节点标签与 Redis 复制图关联，为每个尚无跨故障域副本的主预留一个独立名额，再用完整匹配规划剩余接入。逐个接入和控制器重入均须重新验证当前图；不重新分配已有副本，不接受未知 Pod、缺少标签或不可行的调度布局。原先按 node ID 排序补齐数量的逻辑不能证明故障域安全。官方 Chart 的 Role 不含节点读取权限；产品使用[Helm post-renderer](https://helm.sh/docs/v3/topics/advanced/#post-rendering) `redis-operator-rbac.py` 为精确的修订镜像添加唯一的 `list nodes` 规则，保留全部其它规则与 release 所有权。包装入口 `redis-operator-rbac.sh` 从产品任务接收 `ansible_playbook_python`，使用 Ansible 自身已具备 YAML 依赖的解释器，不能依赖系统 Python 或在执行中安装依赖。旧镜像的 RBAC 保持原样。解析、身份拒绝、幂等及真实 Chart 渲染均须过契约，不能直接修改现场 ClusterRole 代替产品入口。

缩容先记录待退役节点身份，再保留副本搬迁数据；源主不再持有槽位后，才删除已证明属于退役 Pod 预算的成员。持久化记录支持控制器中断重入。若计划包含应保留的副本、未知节点、未完成复制或仍有槽位的源主，控制器拒绝继续，而不是扩大删除范围。

## 3. 模板契约

`operator-values.yaml.j2` 只配置 Operator image、namespace watch、RBAC、资源和 feature gates。`config.yaml.j2` 生成持久化启动配置，`instances.yaml.j2` 只生成 Redis CR，不能直接创建 StatefulSet。Sentinel 模式必须生成 RedisReplication 与 RedisSentinel；Cluster 模式只生成 RedisCluster。所有资源带组件和模式标签，Storage 保留策略显式为 `keepAfterDelete: true`。

固定 Operator 的 Cluster 启动配置由 `redisLeader.redisConfig.additionalRedisConfig` 与 `redisFollower.redisConfig.additionalRedisConfig` 分别挂载；根级 `redisConfig` 承载运行时 `dynamicConfig` 和内存水位。仅在根级引用 ConfigMap 会使角色 Pod 缺少启动配置，运行时参数收敛也不能证明重建前已生效。两角色必须引用同一声明配置，并验证 init/data 容器的真实挂载、生成配置的 include 与实际 `CONFIG GET`；不得输出包含凭据的完整生成配置。

密码 Secret、TLS Secret 和 ACL Secret 只通过名称和 key 引用。任何模板测试都要验证错误模式、空 StorageClass、非法副本数、Secret 泄漏和 Cluster/ Sentinel 资源互斥。

Cluster leader/follower 的拓扑分布显式采用 `nodeTaintsPolicy: Honor`，只计算待调度 Pod 可容忍的节点。两组成员使用 `apps.kubernetes.io/pod-index` 的双向硬反亲和，隔离相同序号的 leader 与第一轮 follower。上游默认流程以 leader `i % shards` 为新增 follower 的联系入口，但没有传递 `--cluster-master-id`；受保护流程显式选择缺少副本的真实主并指定其节点 ID。不能把联系入口或 Pod 序号当作固定复制归属，初始化、提升与扩缩之后均须核验真实复制图。leader 使用 `maxSkew: 1`；follower 使用 `maxSkew: 2`，为 OrderedReady 逐成员调度与反亲和保留可调度空间，仍要求至少三个域。将 follower 的差值强制为 1 可能使第三个成员既不能占据空域，也不能进入已有域。此约束依赖 StatefulSet 的序号标签及 Kubernetes 1.33 已 GA 的 `MatchLabelKeysInPodAffinity`。自定义区域拓扑还须验证集群准入允许该硬反亲和 topology key；默认使用主机名。故障提升可能改变真实复制图，健康检查仍须逐分片核验实际关系。

Redis 8 的 `clusterVersion: v8` 必须与 Operator 0.27.0 的版本判断同时验证。Operator v0.27.0 将 `GenerateConfigInInitContainer` 作为 Alpha 特性；由于固定官方 Redis 镜像需要生成配置，渲染的 Helm values 会始终显式开启该 gate。现场必须把 gate 启动日志、maxmemory 实际值和 ACL/TLS 客户端结果纳入 focused gate；内存水位为 0 时也必须保留 gate，否则官方 Redis 镜像不会应用认证、TLS 和持久化配置。不能把 Alpha 特性写成上游 GA。

0.26.0 升级夹具使用独立官方 Chart，保留已安装的前向 CRD，不把控制器回退等同于 schema 降级。该 Chart 默认包含 `AvoidCommandLinePassword`，但固定 0.26.0 二进制只注册 `GenerateConfigInInitContainer`；模板仅在 0.26.0 分支将前者设为 YAML `null`，由 Helm coalescing 移除默认键。两版 Chart 必须分别真实渲染并核验最终 `FEATURE_GATES`。依据为 [0.26 特性注册](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/ef33adb71ce1bffb0ab1fce593625c320dc6164e/internal/features/features.go) 与 [Helm 3.19.0 values 合并](https://github.com/helm/helm/blob/v3.19.0/pkg/chartutil/coalesce.go)。

启动配置显式固定 `appendfilename "appendonly.aof"`，避免初始化 agent 的默认值决定持久化文件身份。0.26 agent 使用 `Appendonly.aof`，0.27 使用 `appendonly.aof`；文件系统区分大小写，切换默认值可能创建空 AOF，PVC UID 保留也不能证明数据保留。两版 agent 都在最后 include 外部配置，因此产品声明覆盖该默认值。迁移资格须在任何版本变更前写入业务键、执行 `WAITAOF`，逐成员核验值及绝对过期时间，再验证回退。依据为 [0.26 初始化](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/ef33adb71ce1bffb0ab1fce593625c320dc6164e/internal/agent/bootstrap/redis/config.go) 与 [0.27 初始化](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/71954323c9191e6171f3eb04f80bb46a5f128306/internal/agent/bootstrap/redis/config.go)。

> **既有数据边界：** 只有自首次部署起使用相同 AOF 文件名的版本集合，才能进入原地迁移资格验证。对已经使用 `Appendonly.aof` 的外部旧部署，不得直接套用小写文件名或重命名多段 AOF；先核实 `CONFIG GET appendfilename`、manifest 与独立备份，再按运维手册在隔离目标恢复并迁移客户端。

`cluster-bus-port-protected-mode` 在固定 8.10.2 的 `src/config.c` 中注册，8.10.1 没有该参数。启动模板按实际 Cluster 镜像的数值版本判断，仅对 8.10.2 及之后的版本输出该选项；不能以字符串字典序比较 patch 号，也不能把修订标签当作 Redis 服务端版本。当前生产组合保持总线保护，8.10.1 仅是受控固定拓扑迁移/回退资格材料，不能继承当前版本新增的安全能力。依据为 [8.10.1 参数注册](https://github.com/redis/redis/blob/8.10.1/src/config.c) 与 [8.10.2 参数注册](https://github.com/redis/redis/blob/8.10.2/src/config.c)。

Cluster 升级失败恢复遵循 Kubernetes [StatefulSet forced rollback](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#forced-rollback)：先通过原 `config.yml + kubecli` 恢复 CR 和两组 Pod template，再以 UID/resourceVersion 前置条件删除仍使用旧错误镜像且未就绪的测试 Pod。固定 `--redis-upgrade-cluster-repair` 只消费已验证的原配置摘要、CR/集群 UID、私有部署目录归属和故障 UUID；不删除健康成员或 PVC，不以跳过产品健康任务代替恢复。原值、TTL、PVC 身份与限定清理通过后才移除 journal。

## 4. 幂等和资源所有权

Ansible 使用 `helm upgrade --install` 和固定 field manager。ConfigMap 使用 Server-Side Apply；Redis CR 采用上游文档的普通 `kubectl apply` 三方合并，保留未声明的控制器默认值与 status。Operator 使用 Kubernetes `resource.Quantity` 序列化资源量，会把 `1000m` 规范化为 `"1"`；不能把这种等值表达变化当成需要强制抢占字段的配置变更。模板将 CPU、内存请求和限制渲染为字符串，不自行实现 Kubernetes 数量解析器。

第二次执行必须比较 CR、PVC、业务 Secret、Service、Operator Deployment 和 Pod UID；无输入变化不得发生 PVC 替换、凭据重建或无意义滚动。Helm upgrade 会生成新的 release revision Secret，这是发布记录；仅排除本 release 的 `helm.sh/release.v1` 记录，仍检查其它全部 Secret。名称、版本或模式改变时应显式拒绝或要求 change ID。

生产 CR 默认保留 PVC。实验清理脚本只处理同时具备 `kubeauto.io/component=redis` 和 `kubeauto.io/managed-by=kubeauto` 的专属命名空间与 CR；删除命名空间会连同实验 PVC 一起删除，不能用于客户生产下线。失败、中断和升级回滚均必须复用同一 cleanup，并在删除前确认资源所有权。

## 5. 测试阶梯

### 5.1 静态前置

1. YAML、Jinja、Chart tar、SHA256、版本映射、镜像目录和文档链接静态校验。
2. `tests/unit/test_redis_delivery.py` 覆盖默认关闭、模式互斥、模板渲染、TLS/ACL 引用、资源数量、镜像 tag、Chart checksum、六仓 catalog 和入口命令。
3. 固定 runner 的 Redis focused gate：清洁命名空间、StorageClass、Registry digest、Operator、真实客户端业务读写和 scoped cleanup。

### 5.2 聚焦现场与归属

聚焦模式复用验证健康的专属基线，并持有同一个节点池租约。`--redis-tls-rotation` 先在实际控制端 OpenSSL 验证重叠证书链，再验证信任包、叶子证书与撤销旧 CA 三阶段；`--redis-password-rotation` 使用明确停写窗口，协调 ACL 和认证 Secret。Secret 写入保留 resourceVersion，检查原 UID、所有权及预期数据；写入超时必须读回实际状态，不能假定未生效。诊断、回滚或清理异常均不得覆盖首次失败。

`--redis-backup` 通过 `config.yml` 与 `kubecli setup --tags redis-snapshot` 生成源快照，在不挂载源 PVC 的隔离临时目标验证 CRC、SHA256、数量、类型、绝对过期时间和新读写；导出标志不能替代恢复标志。`--redis-performance` 固定 TLS 负载及采样窗口，从实际 kubelet Summary API 采集成员 CPU、内存、PVC 和网络统计，并记录测量传输路径与延迟分位。新增断言先通过单元和资源契约，再执行对应聚焦模式；部分维护或恢复证据不能将整个矩阵项标为 pass。

备份门禁还直接执行运维手册的离线 RDB 验证主路径，核验业务摘要、首次加载后的新写入、实际进程更替及保存后重启保留。临时 Docker 容器按 UUID、完整容器 ID 和所有权标签回收；退出码为 0 但没有业务成功标志仍为失败。`--redis-cluster-snapshot` 使用独立一次性部署目录，通过产品入口导出三主快照，逐文件验证 CRC、SHA256、RDB header 与大小，并核验不同成员 ID、无重叠完整槽位及源 PVC 保留。修订运行镜像允许严格的 `8.x.y-kubeauto.N` 标签，拒绝任意 Registry 或无效修订号。各主的 RDB 是各自的快照，不形成跨分片原子时点，也不构成完整 Cluster 灾备恢复。
`--redis-capacity` 验证产品内存水位与 OOM 拒写、真实副本旧值与恢复追赶、限量磁盘文件及产品 PVC 扩容。`--redis-capacity-lag` 只到达复制滞后断言，不能替代完整容量项。NetworkPolicy 对已建立连接的处理依赖 CNI；断链必须命中 Redis `CLIENT KILL TYPE master` 的上游连接，并保留 Pod 实际 resolver 的 TCP/UDP 53。DNS 全断可能使 Redis 按域名立即重连时进入同步解析等待，不能把管理连接超时当作已验证的数据滞后。`--redis-dns-probe` 只读显示实际 resolver、DNS Service 与 Pod，不修改生产路径。

Cluster bus 使用独立 `clusterLink`，普通与复制 `CLIENT KILL` 不会终止该链路。进程故障夹具由固定 runner 根据 Pod 所属白名单节点，通过 CRI 核验 Pod UID、容器 ID，再检查宿主 PID 启动时间、进程名和 cgroup；从祖先 PID namespace 发出 SIGSTOP 后必须读到 `T` 状态。容器 PID namespace 内对 PID 1 发送 SIGSTOP 可能因 init 信号保护无效，命令返回 0 不能证明故障成立。恢复只作用于同一进程；故障态诊断必须早于恢复。依据为 [Redis Cluster 链路源码](https://github.com/redis/redis/blob/8.10.2/src/cluster_legacy.c)、[Linux PID namespace 信号规则](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html) 和 [crictl 1.34.0 inspect](https://github.com/kubernetes-sigs/cri-tools/blob/v1.34.0/cmd/crictl/container.go)。

### 5.3 模块与完整验收

1. Sentinel：主发现、单副本/主故障、Sentinel 多数派拒绝、重连、复制追赶、凭据/TLS 轮换。
2. Cluster：槽位完整性、MOVED/ASK、跨槽拒绝、单节点故障、槽位迁移、扩缩容、PDB、磁盘压力和客户端拓扑刷新。
3. 备份恢复、性能、稳定性、升级/回滚和二次执行通过后，运行一次干净完整 Redis 回归。

每一项记录产品入口、期望标志、最小反证命令、故障分类、run ID、manifest digest、PVC/Secret UID、业务摘要和 cleanup 结果。备份项需要独立制品与隔离目标的恢复证据，不能由 PVC 保留或 RDB/AOF 文件存在替代。只有矩阵全部 PASS、durable `rc=0`、零失败标志和 `REDIS_CLEAN_VERIFY_PASS` 同时成立，才能标记已验证或已交付。

## 6. 开发与发布检查

```bash
bash <<'REDIS_DEVELOPMENT_1'
set -Eeuo pipefail
ext_image_repo="${KUBEAUTO_EXT_IMAGES_REPO:-../kubeauto-ext-images-dockerfile}"
python3 "$ext_image_repo/scripts/validate_catalog.py"
bash tests/run_enterprise_regression.sh --redis-static-only </dev/null
bash tests/run_enterprise_regression.sh --redis-artifacts </dev/null
bash tests/run_enterprise_regression.sh --redis-protected-artifacts </dev/null
bash tests/run_enterprise_regression.sh --redis-cluster-artifacts </dev/null
bash tests/run_unit_tests.sh </dev/null
bash tests/run_enterprise_regression.sh --redis-only </dev/null
REDIS_DEVELOPMENT_1
```

完整现场前，先通过受影响单元测试、模板渲染、矩阵 schema 和镜像 catalog。失败时保存首个失败、durable exit、Pod/CR 事件和 Operator 日志；修复单一因果层并清理错误代码及现场残留后，才允许重新执行 focused gate。

## 7. 维护约束

禁止在 Redis 分支引入 `common/` 之外的跨中间件共享状态，禁止复用 Kafka/PXC 的副本、事务、备份或清理语义。禁止使用 `latest`、动态镜像加速器、直接 Helm 代替产品入口或把 Pod `Running` 作为业务结果。任何新模块、跨地域复制、Active-Active、全局一致快照或 Valkey 替换都必须创建新的版本集合、能力边界和专项矩阵。

## 8. 固定源码对应关系

| 实现选择 | 官方版本依据 | 维护断言 |
| --- | --- | --- |
| 根级 featureGates | [Chart Deployment](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/charts/redis-operator/templates/operator-deployment.yaml) | 真 Helm 渲染的 FEATURE_GATES，不只匹配模板文字 |
| ConfigMap additionalRedisConfig | [Redis 配置生成](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/internal/agent/bootstrap/redis/config.go) | 启动合入与重建后实际 CONFIG GET 一致 |
| Sentinel 两层密码 | [Sentinel 资源参数](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/internal/k8sutils/redis-sentinel.go) | 自身 requirepass 与 Redis auth-pass 分别有效 |
| CR 字段/PDB/拓扑 | [固定 API](https://github.com/OT-CONTAINER-KIT/redis-operator/tree/v0.27.0/api) | 渲染 spec 每个字段被 vendored CRD 接受 |
| Cluster 副本分布 | [固定联系入口](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/internal/k8sutils/redis.go)、[Redis CLI 主选择](https://github.com/redis/redis/blob/8.10.2/src/redis-cli.c)、[污点域计算](https://github.com/kubernetes/kubernetes/blob/v1.33.6/pkg/scheduler/framework/plugins/podtopologyspread/common.go)、[动态反亲和 selector](https://github.com/kubernetes/kubernetes/blob/v1.33.6/pkg/registry/core/pod/strategy.go)、[1.33 特性状态](https://github.com/kubernetes/kubernetes/blob/v1.33.6/pkg/features/kube_features.go) | 可调度域和真实复制组同时满足，不以角色组均匀分布代替分片容错 |
| CR 应用与资源量 | [上游复制部署入口](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/docs/content/en/docs/Getting%20Started/Replication/_index.md)、[Kubernetes Quantity](https://github.com/kubernetes/apimachinery/blob/v0.33.6/pkg/api/resource/quantity.go) | 整数与等值单位经控制器规范化后，产品重复执行仍保留业务资源身份 |
| 真实成员与 Service | [StatefulSet 实现](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/internal/k8sutils/statefulset.go) | 主容器名来自 StatefulSet 名，不能硬编码 redis |
| Cluster bus 保护 | [Redis 8.10.2 配置](https://github.com/redis/redis/blob/8.10.2/redis.conf) | TLS 成员认证；未启用 TLS 的 Cluster 在产品校验拒绝 |
| 镜像身份 | [skopeo inspect](https://github.com/containers/skopeo/blob/v1.20.0/docs/skopeo-inspect.1.md) | index 与两个架构 child digest 均核验 |
| 证书签发者与信任窗口 | [OpenSSL X.509 v3 扩展](https://docs.openssl.org/1.1.1/man5/x509v3_config/) | 两代 CA 主体可区分，AKI/SKI 明确；实际客户端库分别验证两代证书链 |
| 失败滚动回退 | [Kubernetes 1.33 StatefulSet forced rollback](https://v1-33.docs.kubernetes.io/docs/concepts/workloads/controllers/statefulset/#forced-rollback)、[kubectl 0.33.6 raw delete](https://github.com/kubernetes/kubectl/blob/v0.33.6/pkg/cmd/delete/delete.go) | 先恢复正确配置，再仅重建归属和 UID 已核验的未就绪成员，保留全部 PVC |
| RDB 加载与资源采样 | [Redis 8.10.2 loadDataFromDisk](https://github.com/redis/redis/blob/8.10.2/src/server.c)、[kubelet 1.33.6 Summary schema](https://github.com/kubernetes/kubernetes/blob/v1.33.6/staging/src/k8s.io/kubelet/pkg/apis/stats/v1alpha1/types.go) | 独立目标明确关闭 AOF 后加载 RDB；统计绑定真实 Pod UID，不以缺失数据或重置计数器生成结果 |
| 复制断链与 DNS 路径 | [CLIENT 类型与断链](https://github.com/redis/redis/blob/8.10.2/src/networking.c)、[立即重连](https://github.com/redis/redis/blob/8.10.2/src/replication.c)、[同步地址解析](https://github.com/redis/redis/blob/8.10.2/src/anet.c) | 上游连接确实断开；保留实际 DNS；正常对照、真实旧值、恢复最新值均须验证 |
| 在线卷扩容 | [Operator HandlePVCResizing](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/internal/k8sutils/pvc.go)、[StatefulSet 不可变模板处理](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/v0.27.0/internal/k8sutils/statefulset.go)、[Kubernetes 1.33 PVC expansion](https://v1-33.docs.kubernetes.io/docs/concepts/storage/persistent-volumes/#expanding-persistent-volumes-claims) | 通过 CR 扩容，核验 PVC、文件系统与业务；不请求缩容，不要求替换原 StatefulSet 模板 |

生产健康检查由 `roles/cluster-addon/files/redis-health.py` 执行，只读访问 Kubernetes API 与成员 Redis CLI。健康及快照 CLI 优先读取 agent 的 `REDISCLI_AUTH`，缺失或为空时兼容 0.26 的 `REDIS_PASSWORD`，按上游语义去除 CR/LF 后仅通过进程环境认证。独立 TLS 客户端成功但产品 CLI 返回 `NOAUTH` 时，先核对 agent 的环境变量名称与入口兼容性；禁止把密码补入命令参数或输出 Secret 来诊断。依据为 [0.26 StatefulSet 环境](https://github.com/OT-CONTAINER-KIT/redis-operator/blob/ef33adb71ce1bffb0ab1fce593625c320dc6164e/internal/k8sutils/statefulset.go) 与 [Redis CLI 认证环境](https://redis.io/docs/latest/develop/tools/cli/#host-port-password-and-database)。`tests/helpers/redis-config.py` 用 YAML 解析目标部署目录的配置，拒绝未解析版本占位符；不得恢复 sed/awk 解析配置或 shell eval。日志只能记录对象名称、状态、数量和摘要，不能记录凭据值。

## 9. 静态与现场执行边界

`--redis-static-only` 不进行 SSH、源码同步、镜像拉取或集群变更。它执行 shell/Python/YAML、专项契约、真 Helm 渲染、Ansible syntax/list-tasks 与默认关闭 check。开发机可通过临时 `REDIS_HELM`、`REDIS_ANSIBLE_PLAYBOOK` 指定已校验工具，不能把临时路径写入生产默认配置。

`--redis-artifacts` 是只读双端制品核验，要求 TalkEdu 与 Docker Hub 同时具备 AMD64/ARM64，完整 index 与架构 manifest 身份一致。国内执行可使用已核验的 Actions 双推 artifact：必须绑定 run、SHA、构建指纹、artifact SHA256 和两端双架构 digest，再将当前 TalkEdu manifest 与该证据比对。此路径不重复访问不可达的 Docker Hub；没有完整发布证据时仍需读取两端。网络失败不是成功，TalkEdu 单端存在不构成双推验收。

矩阵允许已完成模块先登记 PASS，整体保持 PENDING，直到最终完整回归完成。聚合 PASS 必须全部 case 为 pass；每项必须绑定当前证据。基础安装 smoke 的标志是 `REDIS_INSTALL_SMOKE_PASS`，不能发出全分支交付标志。

`--redis-only` 要求前 23 项聚焦资格全部通过，随后在同一实验资源租约下按依赖顺序清理 Redis、PV、CSI 和专属 Kubernetes 基线，重新通过 `config.yml + kubecli` 安装并执行全部业务、故障、维护、升级和稳定性阶段，最后完整清理。阶段失败、业务标志缺失或清理失败立即阻断后续场景。各阶段独立保存 durable 退出状态、日志摘要和启动时源码清单；长门禁期间的本地编辑不得被写成该次执行的源码身份。`redis-full-evidence.py` 在所有阶段、完整清理和当前源码身份均符合契约后，才更新 24 项矩阵。

Redis 8.10.2 的模块资格检查须同时核验四个官方共享库与内置 `vectorset`。`MODULE LIST` 的 RESP3 返回五条记录，内置模块的 `path` 为空；不能以 `.so` 文件数等于注册模块数作为验收断言。身份、版本、加载路径与参数均依据固定基础镜像核验，依据见[官方内部模块加载](https://github.com/redis/redis/blob/8.10.2/src/module.c)。ARM64 的 QEMU 构建夹具仅对自有测试进程使用上游 COW 检查的仿真豁免；正式镜像保留原生内核检查。双架构构建夹具使用独立 loopback 网络并验证自有进程仍存活，避免端口冲突导致探针误接其它进程。

# Enterprise regression handbook

## 当前 dev 的生产启动故障聚焦回归

`KUBEAUTO_MATRIX_WIP=1 bash tests/run_enterprise_regression.sh --nacos-startup-only`
对应主矩阵 `T2-AD-13`，先运行聚焦合同与模板检查，再在 138 上持有
`/var/lock/kubeauto-core-pool.lock`，拒绝已有活动门禁，并验证 137、128、142–143
无运行时/集群残留。固定入口负责源码同步、控制机密钥、durable 状态和日志监控。
它创建独占的 `dev-nacos-startup` 集群（137 控制面，Debian 128、openEuler 142、openSUSE 143 工作节点），
在任何 setup 前将测试配置的 `KUBE_RESERVED_ENABLED`、`SYS_RESERVED_ENABLED`
及 `SYS_RESERVED_ENFORCE` 设为 `"no"`，Nacos 堆为 512 MiB；不改产品容量默认值。

该分支先通过 config + kubecli setup 01 验证基础依赖，成功后才进入 setup 90、再次 setup 01、setup 07，验证 prepare
重入和 Nacos 2.4.3 的 MySQL TLS、逐实例配置/注册发现业务、Pod 重建及重复安装。
配置和持久实例仅在首次初始化时写入；重建与重复安装直接读回原配置和原实例地址，
不会先重新写入来掩盖丢失。2.4.3 开启鉴权缓存后存在官方说明的 15 秒更新延迟；
仅首次创建管理员后的 HTTP 500 且响应恰为 `caused: User nacos not found;`
允许最多 7 次检查、间隔 5 秒。其它状态、错误内容、无效 token 或现有管理员登录失败立即停止。
失败响应、Pod 事件和应用日志在删除命名空间前以私有权限保存，只向主日志输出路径，
不流式输出凭据或原始应用日志。
MySQL 8.0.46、测试 CA/证书、schema 和管理员仅属于新建集群的 nacos 命名空间；
命名空间已存在或不足三个可调度 Ready 节点时拒绝进入夹具。成功和失败均销毁
本次创建的集群，随后通过 `lab-wipe-nodes.sh --startup-only` 清理并复验这四台节点；不借用其它集群，不重跑完整 G10。
本地 Registry 与已下载制品可作为健康公共前置保留；本次最终验收仅汇总受影响范围的干净证据。

验收要求 `NACOS_RESTART_REPEAT_BUSINESS_PASS`、`NACOS_STARTUP_FOCUSED_PASS`、
`NACOS_TLS_REFUSALS_PASS`、`NACOS_TLS_REFUSALS_SCOPED_CLEAN_PASS`、
`NACOS_STARTUP_SCOPED_CLEAN_PASS`、durable exit/finalized 双 0、零非预期失败标志及
末尾清理复验通过。错误 CA 与主机名场景使用独立命名空间，通过原产品 setup 07
要求拒绝连接，同时从应用日志核对对应的证书链／主机名错误；仅非零退出不能通过。
预期失败原日志私有保存，场景结束恢复原配置、回收限定资源并读回原业务。
当前 Secret、TLS 拒绝和真实重复收敛已通过，精确输入、归档摘要和 durable 状态见
`tests/evidence/dev-nacos-startup-20261103.json`；`--nacos-startup-evidence` 仅收取已完成门禁的私有原始证据，
不重新执行现场操作。该聚焦分支不签署 NFS 故障恢复、
全发行版兼容或整个项目 GA。当前差距和五个阶段由主矩阵
`dev_production_requalification` 跟踪，历史 PASS 不替代改变后的集成证据。

`KUBEAUTO_MATRIX_WIP=1 bash tests/run_enterprise_regression.sh --startup-ceph-pool-inventory`
只读盘点 Ceph 共享池六台 Rocky Linux 9.8 主机的实际系统、内存、运行服务、活动任务、
锁、集群清单及磁盘挂载；不取得租约，不清理集群或操作附加测试盘。
本次核心缺陷回归须先核对占用并取得与原 Ceph runner 相同的共享池锁，
仅重测受影响的 prepare／网络工具和 NFS 业务恢复；Rocky 9.8 不代替 Rocky 9.7 的精确版本证据。

`--rocky9-production-only` 使用 130 控制端和 `.212/.165/.238` 三节点，
持有 130 上的 `kubeauto-ceph-priority.lock`、`kubeauto-ceph-compute.lock`，
从干净边界建立 `dev-rocky9-production`。它先恢复 firewalld 包作为客户故障前置，
仅启用而不启动服务；记录完整 RPM 身份清单，经产品 setup 01/90 后再次启用服务，
以已有 prepare 标记重入 setup 01。要求无原有 RPM 被删除、八项网络工具可执行、
firewalld inactive/masked、三节点 Ready，以及两 worker 间 DNS/ClusterIP HTTP 写入读回。
部署前复用已交付的 `ceph_ssh_fixture.py --profile rocky9`：仅对不能直达 130 Registry 的
这三台节点建立 loopback SSH 中继和精确目标/端口 REDIRECT，保持原产品镜像源。
`--rocky9-registry-qualification` 先证明实际连通、断线恢复和清理，完成后才消费其结果；
本分支在同一租约内设置、维护和回收中继，setup 前逐节点检查 pause 与业务镜像 manifest。
外来 journal 拒绝接管，成功、失败和中断均回收本轮规则、socket 和监听。
`--rocky9-production-diagnostics` 只读采集当前独占集群的节点、Pod、事件和 Calico 日志。
三项资源预留开关均关闭。包身份排序和比较仅为对应命令设置 C locale，
不能覆盖 Ansible 所需的 UTF-8 运行 locale。

`--rocky9-nfs-only` 在同样干净的三节点路径上增加 NFS 聚焦分支，对应 `T2-AD-11`：
经 config+kubecli setup 07 安装固定 v4.0.2，两个 worker 共享同一 RWX PVC，
真实写入并 fsync 后跨节点读回；删除原 leader Pod，核对新 UID，要求新 PVC 可供给、
新卷跨节点读写成功且原数据保持。NFS 服务端位于 `.238` 的专有临时目录，
只向三台租用节点导出，拒绝已有 NFS 服务／导出或外来 journal。
回收先等待业务 namespace/PV，再删除本轮 provisioner Deployment，最后逐项撤销导出、
恢复原服务状态并移除本轮目录；失败未能证明卸载完成时保留导出，外层销毁自有集群后再按原 run ID 恢复。
附加盘不在操作范围。预期 `NFS_LEADER_REPLACEMENT_BUSINESS_PASS`、
`NFS_PRODUCTION_FOCUSED_PASS`、`NFS_PRODUCTION_SCOPED_CLEAN_PASS`、
外层 `ROCKY9_PRODUCTION_ACCEPTED`、durable 双 0 与 `LAB_CLEAN_VERIFY_PASS`。
这些入口仍须当前真实日志，存在入口或合同通过均不是现场 PASS。
`--rocky9-production-evidence` 与 `--prepare-os-evidence` 只读收取对应已完成门禁的
源码、durable exit/finalized、canonical 日志及限定诊断目录，要求双 0、成功和清理标志、
零失败后才归档；私有 tar 为 0600，不提交。可提交 JSON 仅保存输入/归档摘要与验收范围。

NFS 此次生产退出的证据与固定上游源码对应：external-provisioner v6.0.0 默认
lease/renew/retry 为 15/10/2 秒，失去续约会主动退出；现场同一 Endpoints 锁读取超时
与 etcd WAL 长尾吻合。该机制复用官方与已采集只读证据，不通过停 API 重测上游机制，
不扩大本次 bug 回归到其它存储，也不调整选主超时掩盖环境问题。

`--nacos-startup-diagnostics` 只读取已保存的 TLS 首失败诊断，输出文件摘要、
渲染对象身份和限定任务统计，不打印私有 Secret、原始应用日志或配置备份。
任意命名空间中的 `nacos-0` 不能作为跳过声明式收敛的依据；启用 Nacos 时必须执行其
Secret 前置、渲染、apply 和 rollout，重复安装与 TLS 拒绝合同均覆盖该入口。

`--prepare-os-only` 仅补验 Ubuntu 22.04（138）和 Anolis 23.3（141）改变的 prepare，
复用两系统未变的控制端/运行时证据。先通过真实 helper 的调用顺序及拒绝/清理合同，
再持有 138 的 `kubeauto-core-pool.lock` 并拒绝活动任务；逐节点要求无 Kubernetes/runtime
服务、数据目录、prepare 标记或 Pod 进程，允许控制端健康的 Docker/Registry 公共前置。
以独占 `dev-prepare-compat` 的 config+kubecli 连续执行两次 setup 01，不执行 setup 90，
校验软件包保留、防火墙收敛和八项工具；测试资源预留均为 no。
138 使用行内 ubuntu+sudo，141 使用 root，不把混合用户 inventory 变成 root 自连接。
成功、失败、中断仅回收本轮 prepare 标记、空证书目录和与本轮摘要匹配的 kubeconfig，
再删除本轮控制端 cluster 目录并核对两节点的限定干净边界。此处没有 API，
不调用需要 API 保护检查的完整集群 destroy；活动 runtime、数据目录、外来文件或 kubeconfig
摘要不符均拒绝清理。`--prepare-os-clean-only` 仅恢复固定两节点的本轮 prepare 失败残留，
仍需租约、限定 inventory 和上述拒绝条件；`--prepare-os-diagnostics` 只读检查首失败。
验收要求 `PREPARE_OS_HOST_PASS` 两条、`PREPARE_OS_FOCUSED_PASS`、
`PREPARE_OS_SCOPED_CLEAN_PASS`、durable 双 0、零失败以及 `LAB_CLEAN_VERIFY_PASS`。
本入口不签署 Anolis 新的运行时或全拓扑 PASS。

Anolis 141 的发行版验收仍为 pending，不从验收分母移除。2026-11-03 现场发现该主机的
6.6.88-4.an23 内核启用 `CONFIG_RT_GROUP_SCHED`，桌面音频及 multipath 实时进程位于
非根 cgroup，根 `cgroup.subtree_control` 无 CPU 控制器，导致 runc 创建 Pod 时缺少
`cpu.weight`。这属于节点前置环境失败，不能通过关闭 CPU 资源管理绕过；处理依据为
[Linux cgroup v2 CPU controller 限制](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu)。
先独立完成服务器节点前置资格和恢复验证，再重新进入 Anolis 产品验收；Nacos 使用其余
健康节点继续验证独立业务范围。原五节点失败日志与清理证据保留，不签署其完整安装 PASS。

`KUBEAUTO_MATRIX_WIP=1 bash tests/run_enterprise_regression.sh --anolis-cgroup-prereq-only`
仅在 141 的清洁节点上验证上述内核前置条件。固定夹具用私有 journal 绑定本轮、原 boot ID、
实时进程 PID/starttime/cgroup，按官方接口把实时进程移到根 cgroup 后启用 CPU 控制器。
成功、部分失败和中断均要求先确认节点无 Kubernetes/运行时/Pod，再重启恢复服务进程树，
核对新 boot ID 后回收本轮 journal；外来 journal、活动服务或身份变化会拒绝接管。
须同时取得 `ANOLIS_CPU_PREREQUISITE_PASS`、`ANOLIS_CPU_RESTORE_PASS`、
`ANOLIS_CPU_QUALIFICATION_PASS` 和 durable rc=0。它不修改持久服务配置、不安装集群，
不签署 Anolis 产品兼容或 Nacos 验收；恢复失败保留 journal 并停止后续现场工作。
策略识别须去除 `SCHED_RESET_ON_FORK` 标志后再判断 FIFO/RR；Linux 6.6
`sched_getscheduler()` 源码明确将此标志与策略按位合并，漏识别 rtkit/音频实时线程会使
启用 CPU 控制器返回 EINVAL。对应多线程及标志合同必须先通过，不能调整调度策略绕过。

## Longhorn 独立存储分路

Longhorn 位于 `docs/storage/longhorn/` 和 `roles/longhorn/`，固定为
Longhorn 1.12.1 / V1 文件系统盘与 V2 replicated 裸块盘、Kubernetes 1.33.6、Helm 3.19.0，升级资格源为
1.12.0。产品入口是部署目录的 `config.yml`、`kubecli download -E longhorn`
及 `kubecli setup longhorn-gate 09`。它使用 `longhorn-test-matrix.yaml`
独立跟踪 54 项验收，不继承 Ceph 或其它中间件 Longhorn 1.10.1 夹具的证据。
当前矩阵仍有 pending，不能按本节入口存在判定已完成生产交付。

V2 自 Longhorn 1.12.0 起 GA。44–54 项按官方前置、正常/拒绝/恢复路径与限制设计，
分别覆盖资源资格、裸盘生命周期、安装、业务、数据保护、克隆、故障、维护、升级、
可观测性/负载和清理。三节点 AIO 安装及访问模式、扩容、回收、加密和 TRIM 已有独立资格及完整 LONGHORN-47 模块证据；
原生／CSI 快照与外部全量／增量备份恢复也已通过最窄资格。其余 pending 运维场景仍按矩阵逐项验收，未实现入口由 runner 拒绝。PCI NVMe/VirtIO 的绑定、
IOMMU 和队列资格仍待实现。已有 5.14 内核池只可作为 V1 证据，不能冒充 V2 的
NVMe/TCP >=6.7 资格。官方 GA/生产资料用于设计和机制依据，不直接赋予项目 PASS。

所有入口从开发机执行 `bash tests/run_enterprise_regression.sh MODE`：

| 固定入口 | 行为与边界 |
| --- | --- |
| `--longhorn-v2-interrupt-qualification` / `--longhorn-v2-interrupt-cleanup` | LONGHORN-51 的 AIO 轮询→中断→轮询窄资格，尚待现场验收。附着时经 config+kubecli 精确拒绝启动参数变更；原卷完全分离后再经同一路径往返，核对原 Setting／IM CR 身份、三节点实际新 Pod UID、SPDK 参数与原生 interrupt 状态，并在不同节点读回原数据和中断模式下的 fsync ledger。私有 journal 绑定安装与业务两套 namespace UID；失败／中断先恢复原生轮询配置，再回收本轮业务。cleanup-only 在空业务前置之前进入原 journal，不输出资格成功。该项不覆盖匿名内存、CPU 独占、iobuf、PCI NVMe 或性能比较 |
| `--longhorn-v2-broken-restore-qualification` / `--longhorn-v2-broken-restore-cleanup` | LONGHORN-48 缺块恢复负路径；config+kubecli 创建两点备份后，仅隔离本轮独立库中一个已读取并校验的引用块。拒绝必须绑定原 Volume/Engine/Backup 和块 key；停止 Engine 清空瞬时状态时，核验绑定原 Volume/Replica 的原生 FailedRestore 事件，超时不算拒绝。按原 SHA 修复块、原生退役失败目标，再验证两点独立 CSI 恢复、写隔离和源跨节点重挂。未知结果保留账本与库，禁止后续删除；cleanup 先恢复自有故障。当前窄资格通过，durable rc=0、零失败、205 项冻结输入匹配、原块恢复及双侧原基线独立复核见 `evidence/longhorn-v2-broken-restore-qualification-20261021.json`；两个恢复点均通过，源跨节点读回 17,039,360 字节。不代替完整十一项模块或最终交付。 |
| `--longhorn-v2-data-protection` | LONGHORN-48 的完整十一项组合入口，复用健康源与独立目标基线。缺块原生拒绝、原块修复和两点独立恢复、checksum 与原生/CSI 快照恢复、备份拒绝及恢复、四类计划任务、分离卷 periodic full、系统包与附着拒绝、缺失卷系统恢复、两点 standby、计划隔离激活及回切均已通过本次完整模块。209 项远端冻结输入首尾匹配，durable rc=0、零失败、十一份 scoped-clean 及两侧原基线独立复核见 `evidence/longhorn-v2-data-protection-complete-20261021.json`。源为三节点三副本，目标为单节点单副本；软件隔离的计划切换不证明突然站点失联、断电 fencing 或目标 HA，也不替代最终干净完整交付 |
| `--longhorn-v2-integrity-qualification` | LONGHORN-48 的按需快照 checksum 窄资格。先经 config+kubecli 创建原生／CSI 快照并独立恢复，再对本轮三副本 V2 源卷按 UID/resourceVersion/spec 提交原生 CR 请求及 fast-check 策略；等待请求完成、两个快照均有 checksum 和时间戳，恢复原逐卷策略并核对全局策略未变，最后跨节点读回及限定清理。已有 checksum 可复用，不声称重新扫描全部数据或位腐坏检测；当前资格通过两份原生 checksum、策略恢复、独立恢复及 13,369,344 字节跨节点读回，133 项冻结输入匹配、durable rc=0 和限定清理见 `evidence/longhorn-v2-integrity-qualification-20261020.json`，不签收完整 48 项 |
| `--longhorn-v2-linked-clone-cleanup` | 仅按原 run/namespace/PVC/PV/Volume/class 的身份账本清理未完成的 linked 图，先到达自有清理再要求空业务基线；恢复原产品输入，按子卷优先顺序回收，核对空基线、原核心及通道。对象替换或外来消费者时保留账本并停止；成功不代表克隆业务资格通过。 |
| `--longhorn-v2-linked-clone-qualification` | LONGHORN-49 的新 linked/nested clone 窄资格。通过 config+kubecli 创建独立 linked StorageClass，再按 CSI PVC dataSource 建立源→克隆→嵌套图；分别核对初始数据、双向写隔离、三副本同节点同盘祖先关系及跨节点读回。用原生 server dry-run 验证源卷、入口快照、源副本删除与副本数保护；按嵌套→克隆→源回收，并在子卷退役后读回仍存活的父卷。独立混合类 journal 不进入通用快照优先清理，不移除保护 finalizer。linked/nested 整体资格尚无现场 PASS，不覆盖依赖迁移/重建、扩容和备份恢复。原 TLS 首失败保留于 `evidence/longhorn-v2-linked-tls-first-failure-20261021.json`；派生镜像 `v1.12.1-kubeauto.2` 已修复四处 TLS 客户端调用并完成双仓双架构发布和源／独立目标采用，见 `evidence/longhorn-v2-linked-tls-rebuild-publication-20261021.json`。后续 owning 资格仍须验证当前镜像的真实克隆业务，发布成功不代表 LONGHORN-49 PASS。 |
| `--longhorn-v2-replica-im-loss-cleanup` | 只处理 V2 远端副本 IM Pod 丢失资格保留的原业务 journal；在正常资格的空业务前置之前进入 UID 绑定的组件恢复，未知归属、原 IM 被替换或恢复不明时保留现场。只有原生恢复、业务限定回收、空业务／原盘基线与传输清理均通过，才输出独立 `LONGHORN_V2_REPLICA_IM_LOSS_CLEANUP_PASS` 和 scoped-clean；不会输出故障业务资格标志，也不替代原失败重验 |
| `--longhorn-v1-v2-im-adopt-qualification` | 已发布派生 IM 的两池并行采用窄资格：只有精确审核的 consumer pin、双仓双架构发布证据和原健康空业务身份都匹配才执行；分别经 config+kubecli setup09 更换 V1 三个客户端节点／V2 三个节点上的 IM，核对新 UID、原盘／凭据／namespace、fsync 与跨节点读回。失败保留集群、采用 journal 和诊断，不自动卸载或清盘；恢复冻结 consumer 后同一产品流程回退。两支各有独立 durable，父预期 `LONGHORN_V1_V2_IM_ADOPT_QUALIFICATION_PASS` 与 scoped-clean。也可分别以 `--longhorn-v1-im-adopt-qualification`／`--longhorn-v2-im-adopt-qualification` 验证单池。该资格不签收克隆模块或整体 GA。 |
| `--longhorn-v2-resources-reboot-recovery` / `--longhorn-v2-dr-target-resources-reboot-recovery` | 分别恢复原三节点源池或单节点 `.149` 目标的临时 hugepages。要求原机器／集群／盘身份、原 ready 资源账本和空业务，且至少一台 boot ID 已变化、页池为零并有足够实际内存；未重启节点必须保有原身份和 1024 页，其页池与 kubelet 不作写入。先核验全部主机，再归档原账本字节、记录写入意图，仅为已重启节点恢复 1024 个 2 MiB 页并刷新 kubelet。源池在最终全量上下文中只消费本轮已接受的安装收据，不回退到历史收据。按原固定镜像校验安装健康，不更改产品配置或裸盘。中断、部分分配或身份变化保留进度与首失败，禁止盲重试；成功保留页池和原安装，供后续验收及原限定恢复使用，不代表 LONGHORN-44 PASS。 |
| `--longhorn-v2-dr-target-im-adopt-qualification` | 独立目标的派生 IM 采用入口；使用目标自身安装证据、配置、原集群／盘／密钥身份及单节点业务校验。经 config+kubecli 更换镜像后验证 fsync 和同节点分离重挂，不声称跨节点或目标高可用。原页池必须先健康；失败保留安装与采用账本，不卸载或清盘。 |
| `--longhorn-v1-ownership-operations-v2-failure-platform` | 在原 V1 已原生卸载的健康核心及原 V2 健康空业务基线上，持原三把父租约、一次同步后并行两条原 gate 链：V1 ownership/install→maintenance、capacity、integrity、disk-lifecycle、node-failure、network-failure、disk-failure、last-replica、middleware→系统恢复、灾备、升级生命周期、告警可观测性；V2 剩余六故障→runtime-resources、coexist、maintenance-drain→V2 middleware（PXC SQL 恢复及六个监控卷）→原 interrupt 轮询/中断往返。每池完成自己的前阶段后立即续接，不等待另一池；阶段失败停止本池后续，另一池继续验证及清理。V2 使用专有 kubeconfig，保留 V1 对共享 context 的临时所有权；V1 恢复、灾备及升级阶段独占 `.149` 目标，V2 全链仅使用 `.146`–`.148`。每阶段须有原业务标志、scoped-clean 及 durable 两份 0；父预期 `LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM_PASS`。剩余六故障不能单独签完整 50，当前仅合同通过，尚待现场。 |
| `--longhorn-v2-cleanup-recovery` / `--longhorn-v2-cleanup-target-recovery` | LONGHORN-54 的源／目标原 journal 恢复入口，当前仅合同通过。实施人员从开发机仓库根目录执行；运行时变量 `LONGHORN_V2_RETIREMENT_JOURNAL` 必须取原 `LONGHORN_V2_RETIREMENT_ATTEMPT` 的 original 目录下实际存在的 `retirement.json`。仅接受已通过保护检查、身份及原 durable 状态一致的限定 phase；源还须有 failure/SIGTERM checkpoint 清理证据，目标须核对原 source-boundary。恢复在原目录下新建 recovery attempt，保留原失败日志和退出码，依 phase 续接原生卸载、宿主状态恢复或 core 清理并重新核验原盘。身份变化、缺 intent、未知状态即停止并保留材料；禁止用正常 cleanup 入口新建 journal 绕过。经 `--longhorn-status`／`--longhorn-log` 观察，不以命令仍运行判断成功；只有新 attempt durable 两份 0、全部清理标志和主机复验通过才生成独立 release-proofs，供后续节点场景消费。 |
| `--longhorn-ownership` | 在已原生卸载 Longhorn 的健康 V1 核心上，依次执行 CSI 拒绝、namespace/release/CRD 拒绝、原产品完整安装、类拒绝、共享控制器拒绝、请求身份拒绝。各阶段复用原固定入口、独立 durable 与 scoped-clean，首失败停止，预期 `LONGHORN_OWNERSHIP_PASS`。成功保留健康存储；不额外卸载。安装阶段仍保存／恢复默认 kubeconfig，因此本组合不能与检查该默认上下文的另一现场批次并行。 |
| `--longhorn-v1-backups-v2-linked-qualification` | 第二个固定并行批次：V1 按 platform→security→backup→backup-refusal→recurring→encryption→uninstall 执行完整模块；V2 同时执行 linked/nested clone 窄资格。仅在原健康空业务基线、匹配制品及固定独占租约下运行；每项有独立原业务标志、scoped-clean 和 durable 文件，一侧失败不取消另一侧清理。V1 卸载位于最后，成功后需经原产品安装入口恢复存储才能执行后续 V1 用例。V2 linked/nested 资格不等同完整克隆模块；预期父标志 `LONGHORN_V1_BACKUPS_V2_LINKED_QUALIFICATION_PASS` 与 scoped-clean。 |
| `--longhorn-v1-v2-control-qualification` | 固定并行资格：一次静态／制品检查、三把独占父租约和源码同步，V1 六节点按幂等→安全执行，V2 三节点按轮询／中断往返→Manager Pod 故障执行；两池同时运行，日志、durable 状态及产品日志分离，私有 kubeconfig 显式指定，首尾只读核对默认 context。一侧失败只阻断本侧后续，另一侧完成自身清理。各子项仍按原业务断言、退出码和限定清理独立验收；本入口不代表完整 Longhorn 回归或 GA。预期 `LONGHORN_V1_V2_CONTROL_QUALIFICATION_PASS` 及 scoped-clean，最小反证为本入口。 |
| `--longhorn-v2-interrupt-manager-qualification` | 同一 V2 池内依次执行已有中断模式与 Manager Pod 窄资格，复用一次冻结输入及同步，各子项保留独立 durable 文件、原验证器和清理；首失败停止。预期 `LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS` 及 scoped-clean，不签收完整 50／51 项。 |
| `--longhorn-v2-manager-loss-qualification` / `--longhorn-v2-manager-loss-cleanup` | LONGHORN-50 的控制面 Manager Pod 正常删除窄资格，尚待现场验收。通过 config+kubecli 进入原健康三节点 V2 基线；记录原 DaemonSet、Pod、IM、Engine/Frontend/Replica 身份，先落盘故障意图再执行 UID/resourceVersion 保护的删除。要求原 DaemonSet 下新 Ready Manager Pod、三个 IM Pod 与原生实例身份保持、原客户端确认写和恢复后独立 ledger、跨节点读回。失败或中断先恢复组件再回收业务；cleanup-only 不输出资格成功。不覆盖 SPDK、引擎、主机、网络或完整 LONGHORN-50 |
| `--longhorn-v2-replica-im-loss-qualification` | LONGHORN-50 的远端副本 IM 正常删除窄资格已通过：恢复约 84.87 秒，原客户端 319 条确认写、恢复后 20 条新确认写及跨节点读回一致；durable rc=0、零失败、限定清理和独立双侧基线均已核验，证据为 `tests/evidence/longhorn-v2-replica-im-loss-qualification-20261021.json`。经 config+kubecli 复用原健康三盘空业务基线，仅允许本轮单卷和远端副本所在 SPDK IM，不得含其它原生消费者。双读归属后以原 Pod UID/resourceVersion 正常删除，等待原 IM UID/spec 下不同 UID 的 Ready Pod 与三份原盘健康副本；核对原客户端 fsync 确认前缀、恢复后新 ledger 和跨节点读回。删除前持久化故障意图，成功／失败／中断均先核验组件恢复再回收业务；不明状态保留 journal。预期 `LONGHORN_V2_REPLICA_IM_LOSS_QUALIFICATION_PASS` 与 scoped-clean；最小反证为本入口。正常 Pod DELETE 可含优雅终止，不代替突然断电、节点失联、引擎／网络故障、重建 QoS 或完整 LONGHORN-50 |
| `--longhorn-v2-full-copy-lifecycle-qualification` | LONGHORN-49 的全拷贝源卷退役窄资格；经 config+kubecli 重入并建立本轮独立源与 CSI full-copy 卷，核验完成态、三副本及双向写隔离后，按原 UID 删除源客户端、PVC、Retain PV 和已分离的原生源卷，等待源副本回收。克隆卷必须保持原 PVC/PV/Volume 及三副本身份，继续读回、跨节点重挂并确认新写入。通用业务账本负责成功、失败和中断清理，替换对象或外来 PV 引用时停止；不移除 finalizer。该窄资格已通过，230 项冻结输入匹配、durable rc=0、限定清理及双侧基线保持，证据为 `tests/evidence/longhorn-v2-full-copy-lifecycle-qualification-20261021.json`；不代替 linked 生命周期、克隆扩容、备份恢复或完整 49 项验收。 |
| `--longhorn-v2-full-copy-clone-qualification` | LONGHORN-49 的 V2 等量全拷贝窄资格。经 config+kubecli 重入配置，核对错误容量的原生拒绝、full-copy 完成、独立三副本、双向写隔离和源卷跨节点读回；当前资格已通过原 9 MiB 确认数据和额外 1 MiB 文件重挂、133 项输入一致、durable rc=0 和限定清理，见 `evidence/longhorn-v2-full-copy-clone-qualification-20261020.json`。不替代源卷删除、linked/nested 生命周期及完整 49 项验收 |
| `--longhorn-v2-detached-backup-recovery` | 仅恢复已归档的跳板机重启中断：绑定原 run、cluster、boot、失效 PID 和业务 journal 摘要，要求原业务 namespace、PV、原生卷/副本/备份和计划均已消失；按原 UID/配置恢复目标与 Secret，原容器和通道限定回收。失效 Unix socket 必须匹配原 inode 且连接返回 ECONNREFUSED 才能移除。恢复后独立复核三盘、空业务和原设置；替换对象、外来消费者或未知状态停止并保留材料。成功仅为 `LONGHORN_V2_DETACHED_BACKUP_RECOVERY_PASS`，不替代干净备份资格。 |
| `--longhorn-v2-dr-target-registry-qualification` | LONGHORN-48 独立目标的最窄传输资格。仅使用明确授权的 `.47.149`：持有共享租约后，经已有 `ubuntu24` BatchMode/sudo 核对原机器及空盘身份，追加开发/控制公钥，并通过已认证连接核对目标主机公钥；保留原授权内容，不使用或传输密码。独立目录和单节点 inventory 经产品 new 入口生成，验证 Registry 重连、120 秒监督及精确通道回收；不安装集群、不准备页池或写盘。成功需 `LONGHORN_V2_DR_TARGET_REGISTRY_QUALIFICATION_PASS`、scoped-clean、durable rc=0 和源集群前后身份摘要一致；不代表 DR 或目标站点 HA。 |
| `--longhorn-v2-dr-target-core` | 上一目标传输资格通过后，在 `.149` 通过 config+kubecli 部署独立 Kubernetes 1.33.6；使用与源不同的集群目录及 Service/Pod 网段。核对目标 cluster/node UID 与源不重叠，执行标准 DNS/ClusterIP HTTP 写入读回的同节点路径，回收探针/通道并恢复控制机 kubeconfig，保留健康目标核心供后续安装。失败按机器身份限定恢复目标核心；源三节点前后健康和身份摘要必须保持。成功需 `LONGHORN_V2_DR_TARGET_CORE_PASS`、scoped-clean 和 durable rc=0，不证明跨节点网络、Longhorn 安装、standby/切换/回切或站点 HA。 |
| `--longhorn-v2-dr-activation-qualification` / `--longhorn-v2-dr-failback-qualification` | 归属 LONGHORN-48，先通过两点 standby 资格。activation 经 config+kubecli 创建本轮双点备份和待命目标，先证明工作流拒绝缺少隔离证明的激活；删除原源消费者和 Retain PV/PVC、确认 detached、关闭 frontend，并核对无票据及 Engine/EngineFrontend 停止，才提交原生激活。静态 PV/PVC 必须绑定原目标 UID，读回最新源摘要、确认目标新写入且源仍隔离。此窄项通过后才可进入 failback；固定入口先核对激活证据的业务依赖摘要、双集群身份和独立清理读回，缺失或变化即停在传输创建前。随后目标新全量备份逐对象校验，恢复到源站新三副本卷，反向隔离目标后才在新卷接入和写入，旧源保持隔离。两入口各需业务标志、durable rc=0、限定清理及双侧基线不变；前者不代替回切，二者均不签收目标节点 HA 或完整 LONGHORN-48。未知对象保留后端与账本；失败收集双集群原生状态、Pod 描述、事件、Manager 日志及渲染输入。 |
| `--longhorn-v2-dr-standby-qualification` | LONGHORN-48 的独立 V2 standby 两点同步窄资格。复用已验收双集群和外部 TLS 库；经源 config+kubecli 创建全量与写入新数据后的增量备份，逐对象读回，目标先发现原生 Backup 再创建显式 V2／`Standby:true` 的单副本卷。必须同一 Volume UID 连续完成两个 Engine restore，原裸盘副本健康、无 PV 或 EngineFrontend 业务端点；发现 lastBackup 不作为恢复完成。先退役目标卷，再自然清除双侧 cache 并验证远端对象保留，最后限定回收源业务、消费者与后端；未知创建或身份变化停止并保留账本。预期 scoped-clean、durable rc=0 和 `LONGHORN_V2_DR_STANDBY_QUALIFICATION_PASS`；不代表隔离激活、回切、目标 HA 或完整 LONGHORN-48。 |
| `--longhorn-v2-dr-store-qualification` | 在健康三节点源与独立单节点目标上验证同一外部 TLS 备份库。先资格核验原源库，再移除源备份传输 Pod／隧道；目标直连外部端点，实测正确 CA 成功、错误 CA 精确拒绝，并经 config+kubecli 配置目标 BackupTarget 及独立 Secret。目标消费者先恢复原引用，原源 Store 再回收自己的后端；任何剩余卷、备份或身份歧义均保留后端。最后清两侧 Registry、保留其它分支的共享控制上下文并比较双侧基线；只证明共享库前置，不证明 standby、切换、回切或站点 HA。 |
| `--longhorn-v2-dr-target-install-recovery` | 仅接续独立目标的自有安装恢复：绑定原核心及 namespace/凭据 UID，经产品原生空存储卸载，保留共享快照资源；完整命名空间图检查通过后恢复主机、multipath 和页池，保留健康独立核心及裸盘介质。外来对象或歧义写入停止并保留账本；恢复控制上下文、清理目标通道并比较源前后摘要。只证明恢复及限定清理，不代表安装或 DR PASS。 |
| `--longhorn-v2-dr-target-install-qualification` | 已验收的 `.149` 独立核心上，经 config+kubecli 步骤 09 安装单节点 V2 AIO。先核对核心/原空盘身份，记录主机及 multipath 原状态，准备 1024 个 2 MiB 页，按显式单副本和 CSI 单控制器配置安装；通过原 PVC 的 fsync、真实 detached、同节点重挂读回及原裸盘 UUID 验证。成功保留健康目标安装，限定回收业务和通道并恢复控制上下文；失败经原生卸载路径恢复可证明的自有状态，歧义写入保留账本。需 scoped-clean、durable rc=0、零失败及源前后摘要一致；不证明跨节点、standby/切换/回切或站点 HA。 |
| `--longhorn-v2-detached-backup-qualification` | LONGHORN-48 的允许分离卷周期备份及 periodic full 窄资格。经 config+kubecli 创建 `backup-force-create`、`full-backup-interval=2` 计划，以原生 Setting 的 UID/resourceVersion 临时开启分离卷任务；确认无业务 Pod 时临时附着、实际备份及结束后 detached。将成功 Pod→Job→计划归属与原生日志 executionCount、Backup snapshotName/backupMode 绑定，避免把首次增量实际全量误判为参数生效；读回配置与全部引用对象、独立恢复、后写源隔离及跨节点读回。先停止本轮计划，再恢复原 Setting，最后回收业务、对象库和通道。预期 `LONGHORN_V2_DETACHED_BACKUP_QUALIFICATION_PASS` 与 scoped-clean；最小反证为本入口，不代表独立 DR、完整 48 项或最终交付。 |
| `--longhorn-v2-systemrestore-qualification` | LONGHORN-48 缺失卷重建窄资格：独立保存原 Volume/PV/PVC UID、备份时及之后两份数据摘要；实际读回外部完整备份和资源包后，按 UID 删除源消费者、Retain PV/PVC/卷，再次读回材料，经 config+kubecli 请求 SystemRestore。验证新 UID、原 namespace/绑定、包内备份来源、Pod→Job→SystemRestore 归属、业务时间边界和跨节点重挂；成功后按恢复账本限定回收，未验证或未知结果保留恢复材料。当前资格已通过原三对象消失后的材料读回、新身份/绑定/来源、14,680,064-byte 备份时数据与跨节点重挂；146 项输入匹配、durable rc=0、零失败及限定清理见 `evidence/longhorn-v2-systemrestore-qualification-20261020.json`。不证明独立站点 DR、完整 48 项或最终交付 |
| `--longhorn-v2-systemrestore-recovery` | 仅接续已归档的系统包 URL 末尾斜线断言首失败；先精确匹配三份原账本、原 Completed 请求、新 Volume/PV/PVC UID/spec、共享控制对象与外部恢复材料。通过 config+kubecli 重放同一请求且保持原 UID，按官方无尾斜线 URL 核对来源，继续备份时数据和跨节点重挂，再由 runner 限定回收。无法绑定即停止并保留材料；恢复成功不替代一次干净资格或独立站点 DR。 |
| `--longhorn-v2-systembackup-cleanup` | 仅回收已归档的 checksum 门禁失败残留：精确匹配原业务 journal、集群/namespace/目标/对象库身份，确认原系统包与备份 CR 已消失且无恢复消费者，再由 runner 执行原有 UID 保护清理，比较失败前后空业务/备份基线。只证明夹具回收，不作为原请求恢复或 SystemBackup 资格 PASS。 |
| `--longhorn-v2-systembackup-recovery` | 仅恢复已归档的 nullable conditions 首失败：先绑定原 journal、原 SystemBackup UID/创建时间/目标 owner 与原业务身份，再通过 config+kubecli 重放同一请求，校验远端资源包及原源卷确认数据，依次原生删除资源包、业务与对象库，复验原安装/裸盘基线。未知结果和替换仍拒绝；不作为系统恢复或完整数据保护 PASS。 |
| `--longhorn-v2-systembackup-qualification` | LONGHORN-48 的资源包及附着卷恢复拒绝窄资格。先通过 config+kubecli 完成外部全量/增量备份及两次独立恢复，再声明 disabled 策略的 SystemBackup；逐项读回精确 cfg/ZIP，核对整个 ZIP SHA256、CRC、原 namespace/Volume/PV/PVC/类 UID 和已验证的 lastBackup。附着源上经产品请求 SystemRestore 必须精确拒绝且不创建对象；原生删除资源包及两个远端对象后，回收业务与对象库，保留健康安装。创建结果、消费者或对象归属不明时保留恢复材料。当前资格已通过 57,486-byte ZIP／487-byte cfg、20 个 YAML 成员／182 个资源、源身份与 lastBackup、附着拒绝及 17,039,360-byte 源卷跨节点读回；141 项输入匹配、durable rc=0 和限定清理见 `evidence/longhorn-v2-systembackup-qualification-20261020.json`。不证明真正系统恢复或 DR |
| `--longhorn-v1-operations-remaining-v2-monitoring` | Longhorn 1.12.1 的限定续跑：交付操作人在开发机仓库根目录启动前，核对 maintenance、capacity、LONGHORN-30 integrity、LONGHORN-26 disk-lifecycle 和 V2 CPU 当前独立通过证据的版本、依赖、原始日志、durable 状态及对应限定清理后的健康空业务基线；确认两池原失败已归档，核对当前所属夹具、存储状态及原恢复边界；保全 V1 稳定性首失败与业务子范围证据，第 39 项独立保持 pending。固定入口获取原三把互斥租约并同步一次；V1 仅在原 node-failure receipt 存在（含符号链接）时先调用原 node-failure-cleanup，由其严格核验归属、拒绝不合法 receipt 并恢复；必须取得原 scoped-clean、成功标志及 durable 双 0 才进入 node-failure。无 receipt 时直接进入 node-failure，其原空业务和残留 guard 继续生效。禁止直接删除 receipt；清理失败立即停止 V1 后继并保全状态，V2 独立继续。V1 随后顺序执行 network-failure、disk-failure、last-replica、middleware，随后原系统恢复、灾备、升级和可观测性；V2 若原 monitoring journal 存在（含符号链接），先通过既有严格 monitoring cleanup 的独立 durable 阶段和 scoped-clean，恢复 CPU 验证要求的原配置及无监控残留状态；清理失败停止 V2，不执行 CPU 验证或 setup。随后完整只读核对已完成 CPU receipt，再进入原空业务基线、监控/性能入口及全限定清理；无 journal 时仍须通过同一 CPU 验证。保留各 owning scope 的原等待/超时、产品入口、业务校验、限定清理和 durable 双 0，不重复已通过的 maintenance、capacity、integrity、disk-lifecycle 或 CPU；稳定性保持独立 pending。V1 恢复链独占 `.149`，V2 仅用 `.146`–`.148`；一侧失败停止本侧依赖，另一侧继续，保留首失败并使用对应原 cleanup 入口恢复与复验。等待固定监控返回并从其公开的 durable 工作目录核对终态，父成功标志为 `LONGHORN_V1_OPERATIONS_REMAINING_V2_MONITORING_PASS`；它仅签此次续跑范围，V2 完整验收须组合当前 CPU 与监控证据，不替代完整矩阵或最终干净回归。 |
| `--longhorn-v2-observability-monitoring` | 单独续验 LONGHORN-53 的监控安装及持续业务；存在原监控 journal 时先按上一入口的严格独立阶段清理，再核对原 CPU 独立证据，不重复 CPU 业务。默认仍拒绝身份不同的镜像缓存。仅此入口或上一并行父可显式设置 `LONGHORN_MONITORING_CACHE_RECONCILE_PROOF`（已审阅的旧缓存、发布 digest、原失败父 durable 和三集群身份 JSON）及其 `LONGHORN_MONITORING_CACHE_RECONCILE_SHA256`；通过现有租约通道传递，原 V2 setup 才能受控恢复单个 busybox，保留旧 digest 回退材料。证明材料缺失、原父仍活跃、身份或消费者变化立即停止。原产品、等待及清理步骤不变，需 `LONGHORN_V2_OBSERVABILITY_MONITORING_PASS`、scoped-clean、durable 双 0 与零失败；仅签监控子范围，整体 53 还须绑定当前 CPU 证据。 |
| `--longhorn-observability-support` | LONGHORN-34 支持包最窄修复分支：复用已核验的空业务 V1 基线，经原产品 StorageClass 创建确认写入卷，只执行原生支持包生成、公开元数据准入、私有下载/CRC、原数据跨节点重挂载和限定清理；保存拒绝前的属主与大小。不部署监控，不代表完整 LONGHORN-34 或 GA 通过 |
| `--longhorn-v1-operations-v2-disk-tail` | 两池原失败已归档、V2 前六故障（含 worker-reboot）已有独立验收且故障 journal 已回收时，在开发机仓库根目录启动。复用原三把父租约和一次同步：V1 先执行原 maintenance-cleanup，成功后从 maintenance 完成原 operations、系统恢复、灾备、升级和可观测性；V2 直接从原 single-disk 继续 rebuild-qos、all-replicas、platform/install、middleware 和 interrupt，不重跑已接受故障或要求已不存在的旧故障 journal。每阶段必须通过原业务、限定清理及 durable 双 0；一侧失败只阻断本侧依赖。V1 恢复链独占 `.149`，V2 仅用 `.146`–`.148`。父标志 `LONGHORN_V1_OPERATIONS_V2_DISK_TAIL_PASS` 仅签本次续跑范围；失败保留原状态并经对应原 cleanup 入口恢复及复验，不替代完整矩阵或最终干净回归。 |
| `--longhorn-v1-operations-v2-worker-tail` | 两池原失败均已归档，V1 限定清理完成、V2 原恢复 journal 保留且身份可核验时，在开发机仓库根目录启动的固定续跑入口。保留已接受的 ownership 六 scope 与 V2 前五故障证据；原三把父租约和一次同步后，V1 从 maintenance 起完成原 operations、系统恢复、灾备、升级和可观测性，V2 先经原 v2-failure-cleanup 恢复、健康基线与传输清理，再从原最窄 worker-reboot 起完成剩余四故障、platform/install、middleware 和 interrupt。每项沿用原产品入口、等待/超时、业务验证、限定清理和 durable 双 0；一侧失败只停止其依赖，另一侧继续。V1 恢复链独占 `.149`，V2 仅使用 `.146`–`.148`。父成功标志为 `LONGHORN_V1_OPERATIONS_V2_WORKER_TAIL_PASS`；失败保留原状态并使用对应原 cleanup 入口恢复及复验，不改签旧失败，也不代替完整矩阵与最终干净回归。 |
| `--longhorn-v1-ownership-tail-v2-failure-tail` | 两池失败均已归档、限定清理且原健康安装可复用时，由开发机仓库根目录启动的固定续跑入口。先保留并核对当前 CSI/preinstall/install/class 及 V2 前三故障证据；原三把父租约和一次同步后，V1 从 controller/request 两个原入口继续 operations、系统恢复、灾备、升级及可观测性，V2 先完成原最窄 remote-SPDK，再逐项完成另五故障、platform/install、middleware 和 interrupt。沿用各 owning scope 的等待与超时、业务验证、清理和 durable 双 0；一侧失败停止其依赖，另一侧继续。V1 恢复链独占 `.149`，V2 仅使用 `.146`–`.148`。父成功标志为 `LONGHORN_V1_OWNERSHIP_TAIL_V2_FAILURE_TAIL_PASS`；失败保留原日志和账本，使用对应原 cleanup 入口恢复并复验。续跑不改签旧失败父记录，也不代替完整矩阵与最终干净回归。 |
| `--longhorn-static-only` / `--longhorn-disabled` | 本地矩阵、语法、官方 Chart/模板、受影响 playbook、输入/所有权/清理及真实关闭路径契约；不连接实验室 |
| `--longhorn-uninstall-refusal-qualification` | LONGHORN-40 的非空存储保护前置资格：在原 Retain PV 的 Bound、Released 两阶段，通过 config+kubecli 请求卸载及集群销毁，均须非零且返回对应保护原因；原 PV/Volume、声明与删除确认开关不变，原生卸载 Job 不得创建。随后预绑定原卷，在另一 worker 读回数据并验证三副本健康；仅回收本轮业务和传输，保留健康安装。当前资格已通过四次拒绝、原 9 MiB 数据重绑和限定清理，69 项实际源码首尾一致、durable rc=0，见 `evidence/longhorn-uninstall-refusal-qualification-20261020.json`。此入口不证明空存储卸载、外部备份保留或卸载中断恢复，不作为完整 LONGHORN-40 PASS |
| `--longhorn-uninstall-backup-qualification` | LONGHORN-40 的外部备份保留前置资格：经 config+kubecli 创建一次真实完整备份，复用独立 CSI 恢复、源卷写隔离与跨 worker 重挂验证；冻结 volume.cfg、备份配置及全部引用块。测试夹具按原目标 UID/resourceVersion/spec 恢复其空目标，等待原生备份缓存回收，再限定清理源卷，逐对象读回并比对原摘要。目标恢复是明确的夹具清理，不证明产品支持用空 URL 断开既有目标。随后回收独立对象库及通道，保留健康安装；不执行卸载、不作为完整 LONGHORN-40 PASS |
| `--longhorn-uninstall` | 在卸载拒绝、备份保留的最窄资格通过后，执行两阶段非空保护及真实备份保留流程；源卷回收并恢复原目标凭据、清理通道后，以显式 config+kubecli 卸载意图执行官方 hook。复用原生卸载恢复的 Helm/CRD/自有类/节点声明检查，按 UID 保留共享快照资源；安装 namespace、主机与测试磁盘夹具清理后，从独立对象库逐项读回原备份配置、volume.cfg 和全部块，最后才回收本轮外部夹具。成功保留健康核心，Longhorn 为已卸载状态；当前完整模块已通过四次非空拒绝、Retain 重绑、独立备份恢复和四阶段 17 对象逐项保留；两份原生 Webhook Lease 保持原 UID，六主机/磁盘/后端/通道限定清理完成，共享快照 9 项及核心保持，69 项实际输入首尾一致、durable rc=0，见 `evidence/longhorn-uninstall-20261020.json`。不代表中断恢复、V2 或最终干净全量验收 |
| `--longhorn-artifacts` | 只读核对 24 项运行/升级镜像及 6 项既有支持镜像的固定发布证据、当前 TalkEdu manifest 和双架构 pin |
| `--longhorn-pool-probe` | 六台 Ceph-priority 主机的清洁验证及 18 块额外盘只读资格；不格式化磁盘 |
| `--longhorn-registry-qualification` | 六路独占 loopback SSH 转发、主动断开重连、初始化 helper 退出后的八轮独立进程访问；回收本轮监听/NAT/socket/目录并验证六机清洁。既有控制端 Registry 只读复用，不改产品 Registry 地址 |
| `--longhorn-core` | 消费当前传输资格，在干净节点通过真实 `download -D` 与 `setup longhorn-gate 90` 安装独立核心，Longhorn 保持关闭；Registry 保活退出会终止依赖的产品子树。成功保留已验证身份的健康核心，失败保存首诊断并限定回收 |
| `--longhorn-core-smoke` | 对已核验核心基线补跑固定 Kubernetes 生产冒烟：不同 worker 上的 HTTP 服务与客户端，通过集群 DNS、ClusterIP 完成实际写入/读回，清理独立随机 namespace。复用已发布 json-mock，成功 fingerprint 是安装前置；Ready 不替代本门禁 |
| `--longhorn-disk-qualification` | 健康核心上重新核验 18 盘资格，只使用三个 worker 各一块盘；先记录 UUID/稳定设备身份，再格式化、持久挂载、按身份卸载和清除本轮签名；复验 18 盘及核心。通过后写入绑定当前磁盘 fixture 代码的资格记录；保留核心 |
| `--longhorn-cache-qualification` | 健康核心上通过真实产品下载入口核对 30 项固定缓存；OCI/Docker 格式转换要求配置及有序层一致，gzip 重压缩还须实际下载本地差异层，分别核验压缩摘要/长度和固定配置中的有序 DiffID，不解包文件。已验证的发布 manifest 按固定摘要原子缓存，每次复用仍核验 SHA256；首次获取优先 TalkEdu，保留已发布 Docker Hub 回退，内容不符立即拒绝。不准备磁盘或凭据，不安装 Longhorn；回收转发并复验核心，保留正式镜像缓存 |
| `--longhorn-backup-host-probe` | 只读核对 `.47.130` 上的容器运行时及备份夹具预选端口；不拉镜像、不建容器、不启动 gate，不作为备份验收 |
| `--longhorn-backup-store-qualification` | 健康 Longhorn 基线上，用已发布固定 MinIO 镜像在跳板机建立独立 TLS 对象库；镜像 config、容器 ID、run label、三个 host bind 和 API/仅 loopback console 端口均绑定。本轮随机 bucket 的对象必须实际写入/读回，六节点以 CA 校验 HTTPS；成功、失败和中断均限定回收容器、凭据及独立数据目录，复验存储健康。节点到跳板机直连超时的实验室使用 `.135:23523` 上的无凭据 TLS 透传 Pod，经独占 SSH loopback 23524 连接跳板机对象库；Pod UID/spec、namespace UID、socket inode 和 SSH 监听进程/boot/start tick 均绑定并回收，不改 SSH/防火墙系统配置。对象仍只存跳板机，数据不落集群；这个传输入口随集群消失，灾难恢复时须重建可达路径。它不进入客户配置或产品默认。不配置产品 backup target，不作为 LONGHORN-14 备份恢复 PASS |
| `--longhorn-backup-accounts-qualification` | 计划备份账户夹具的最窄资格入口：复用已发布 MinIO/mc，在跳板机独立 TLS 对象库中建立两个限本轮 bucket 的短期账户，真实写入/读回/列举/删除，核对全局列表只返回授权 bucket；以 root 预先写入的独立 bucket 对象及管理员操作验证原生 AccessDenied，验证撤销旧账户后的原生 InvalidAccessKeyId，以及新账户持续读回。创建前持久化不存在的随机账户标识和 intent；创建响应丢失时只在 parent、name、description、显式策略均吻合后撤销。私钥由 mc 生成、仅进入 0600 journal/env 文件；恢复产品引用、撤销账户后才删除容器。固定 mc 会将 HEAD 的 AccessDenied/NoSuchKey 映射为客户端错误，门禁核对精确操作、目标与序列化原因；不接受泛化权限文本。当前独立账户资格已通过：两个 bucket-only 账户、4 次授权拒绝、1 次旧密钥拒绝，限定清理及 durable rc=0；见 `evidence/longhorn-backup-accounts-qualification-20261019.json`。无产品计划任务配置，不作为 LONGHORN-16 PASS |
| `--longhorn-recurring-selector-qualification` | LONGHORN-16 删除行为的最窄资格：经 config+kubecli 创建年度 snapshot 计划，将本轮真实业务卷加入独立组，再经产品删除计划，要求原生标签及所有子资源回收；原业务数据在另一 worker 重挂后保持。正常路径禁止使用孤立标签恢复开关；不等待定时快照、不签收保留/计划备份/轮换。Foreground 删除修复已通过当前现场资格，66 个实际冻结输入前后一致，原 9 MiB 数据跨 worker 重挂保持，限定清理和 durable rc=0 见 `tests/evidence/longhorn-recurring-selector-qualification-20261019.json` |
| `--longhorn-recurring-snapshot-qualification` | LONGHORN-16 的周期快照前置资格：产品声明创建每分钟 snapshot 计划，原生 RecurringJob→CronJob→Job→Pod UID 链及 Forbid/原重试配置保持；连续写入独立 fsync 文件，观察至少三次成功及三个快照身份，核对保留两份和旧身份消失。独立未选择卷保持业务摘要且不被该计划选中；通过产品将 Cron 暂调年度窗口并等既有 Job 完成后分离源卷，再恢复每分钟计划，要求至少两次 detached 跳过且快照集合不变。停止通过 config+kubecli，等待全部本轮 Job/Pod 后将快照 UID 交给业务清理，源卷删除后再等原生快照消失，并复验源卷重挂和 canary。当前专项已通过：7 个完成 Job、5 个快照身份、保留 2 份，分离后两个周期跳过，原 9 MiB 数据跨 worker 重挂保持；67 项实际冻结输入前后一致、限定清理和 durable rc=0 见 `tests/evidence/longhorn-recurring-snapshot-qualification-20261019.json`。无计划备份、账户轮换或外部恢复验收，不作为 LONGHORN-16 PASS |
| `--longhorn-recurring-declarations-qualification` | 健康存储和空受管计划配置上，通过 config+kubecli 创建无选择者的年度快照计划、更新 Cron 表达式并保留 CR UID，再从配置移除并等待原生 CR/CronJob 回收；核对原有外来计划身份/参数/标签不变。receipt 未确认创建 UID 或资源被替换时拒绝删除，保留恢复记录。不调度业务快照，不作为 LONGHORN-16 定时备份、保留和轮换 PASS |
| `--longhorn-preflight-qualification` | 复用本轮产品步骤 09 已生成的输入、values 和原有凭据，执行产品 `preflight`；随后通过实际 `bootstrap_node` 对六节点执行 server dry-run，核对 UID、标签及注解未改变。验证资源发现、所有权和并发补丁边界，不执行主机准备或持久化节点补丁；不替代 config+kubecli 步骤 09 的安装及业务验收 |
| `--longhorn-node-patch-diagnostics` | 保留远端原源码和原输入，用原 preflight 的旧 resourceVersion 重建 Node 补丁，仅 server dry-run；用于复现 409 冲突。它不同步源码、不修复节点、不构成验收，遇到首个拒绝即停止 |
| `--longhorn-ownership-request-refusal-qualification` | LONGHORN-03 的请求 ID 子资格：在本轮三副本业务卷上创建两份真实原生 Snapshot，等待返回 UID、`readyToUse`、创建时间及对应引擎快照，再经 config+kubecli 分别验证外来同名请求和已有请求参数冲突的精确拒绝。绑定完整声明、安装 namespace 和业务卷 UID；未知创建响应、替换或 backup/clone/CSI 消费者均停止并保留记录。快照 UID 转交原业务账本，依次原生删除快照和源卷并确认消失，不剥离 finalizer；确认文件跨 worker 重挂验证。仅回收本轮业务与传输，不替代完整 LONGHORN-03 或 V2。旧 `createSnapshot: false` 夹具因上游 GC 无效，不能作为资格证据 |
| `--longhorn-ownership-controller-refusal-qualification` | LONGHORN-03 的共享控制器归属子资格：在健康安装上创建零副本、暂停且使用独立选择器的 Deployment 声明，经 config+kubecli 要求拒绝第二套 snapshot-controller；不启动第二套控制器。回收绑定原 kube-system UID、创建响应 UID 和完整声明，并要求没有本 canary 的 ReplicaSet/Pod。原配置字节、共享快照、类和系统/存储工作负载保持；未知结果、替换或子资源均保留 journal。仅验证归属拒绝，不签收外部 controller 兼容、请求 ID 或完整 LONGHORN-03 |
| `--longhorn-ownership-class-refusal-qualification` | LONGHORN-03 的外来类拒绝子资格：健康安装上创建随机、非默认、无 provisioner 的 StorageClass canary，以 config+kubecli 声明同名类并要求归属拒绝；原 UID/声明、配置字节、全部类、共享快照及存储工作负载保持。回收须核对创建响应 UID、完整类声明且无 PV/PVC 消费者；未知结果、替换或外来消费者均保留 journal 并停止。仅验证该子场景，不代替外来请求 ID、共享 controller 或完整 LONGHORN-03 |
| `--longhorn-ownership-interruption-qualification` | LONGHORN-41 的归属夹具恢复子场景：健康存储上，固定 runner 启动仅创建独立 CRD 后等待的子进程；先实际 SIGTERM 验证 Python unwind，再实际 SIGKILL 验证原 UID journal 经固定 `business-clean` 实现恢复。禁止终止产品、控制器或业务进程；原配置、Helm/工作负载/凭据与共享快照身份须保持，未确认创建或对象替换时保留记录并停止。它不替代其它故障、主机、磁盘及所有运维模块的完整中断验收 |
| `--longhorn-ownership-preinstall-refusal-qualification` | LONGHORN-03 的 namespace/release/CRD 首装保护资格：卸载后健康核心上依次创建原 UID 绑定的 `longhorn-system` canary、Helm 可实际发现但无模板/工作负载的 release 元数据、独立 `.longhorn.io` CRD；每次经 config+kubecli 请求安装并要求对应拒绝，配置逐字恢复。namespace 回收前复用完整资源图检查，CRD 回收前要求无实例，release 仅回收原 Secret。未知响应、替换 UID、声明变化或外来资源均停止并保留 journal；不签收其它 CSI/StorageClass/请求身份场景或完整 LONGHORN-03 |
| `--longhorn-ownership-csi-refusal-qualification` | LONGHORN-03 的首装 CSI 保护资格：复用卸载后健康核心和精确 UID canary，临时将原 config 的安装开关置为 yes，经真实 `kubecli setup longhorn-gate 09` 请求首装，要求按无本地 receipt 的 CSI 原因拒绝；原 canary 规格/UID、共享快照与核心保持，配置逐字恢复，限定回收 canary 和传输。不能替代其它外来 namespace/release/CRD/类/请求身份场景，也不签收完整 LONGHORN-03 |
| `--longhorn-ownership-csi-probe` | 仅供 LONGHORN-03 的首装归属反证：要求本 runner 原生卸载后的健康核心、无 Longhorn namespace/release/CRD 或本地安装 receipt；创建一份返回 UID 绑定的 CSIDriver canary，仅执行原产品只读身份子检查，观察是否遗漏拒绝，随后按原 UID/spec 回收并复验共享快照及核心。绝不启动 Helm 安装或原生 deployer，不作为 config+kubecli 安全拒绝或 LONGHORN-03 PASS；未知创建结果保留 intent，禁止猜测清理 |
| `--longhorn-install` | 要求当前核心身份与磁盘资格，核对固定缓存并执行产品下载；记录主机状态、重新准备三块独立盘和临时 PKI，经产品步骤 09 安装，再验证三个独立副本、fsync 数据及换 worker 重挂载后的 SHA256。成功只回收业务探针和传输，保留健康存储；现场验收结果以实际 run 为准 |
| `--longhorn-install-recover` | 仅恢复本 runner 的安装：核对原 namespace、凭据 UID 和默认对象记录，先回收 UID 绑定的失败业务探针，再以产品配置和步骤 09 申请原生卸载。非空/保留卷、外来对象或原生卸载失败均停止；guard 与 Helm release 消失、共享快照资源身份不变后，才回收本轮磁盘、主机改动、凭据 namespace，并归档准确绑定的本地安装 receipt。固定 1.12.1 原生卸载会留下两类 Webhook Lease：仅绑定匹配官方工厂声明的当前节点/已释放持有者和原 UID，产品重入后新出现或被替换的对象仍阻断清理，未知 Lease 仍由完整 namespace 图拒绝。有本轮独立对象库时，还必须凭 retained-backup receipt 在卸载后逐对象读回，才使用显式后卸载清理；普通对象库或缺失恢复依据均不猜测删除。保留核心与共享快照系统；不剥离 finalizer，不作为完整卸载/故障中断验收 PASS |
| `--longhorn-uninstall-diagnostics` | 不同步源码，只读核对原 namespace UID、剩余 Longhorn CRD 名称、原生 Lease 元数据与声明，以及 Pod/Job/凭据对象的身份；Secret 和 ConfigMap 只输出 metadata。检查独立对象库的容器、存活、产品引用及通道是否仍在，不打印凭据，不修复或回收现场 |
| `--longhorn-business-recover` | 核对当前安装与原业务 journal，先回收本轮 UID 绑定的业务探针、快照、备份和 Retain 卷，再验证原 cluster/run 身份并回收本轮独立对象库、透传 Pod 与 SSH 通道；反向监听即便失去本地控制 socket，也须原 PID/boot/start tick/UID/绑定完全一致才可停止，替代监听拒绝回收。保留健康安装、独立盘、主机依赖及核心。不得代替安装卸载；外来资源或身份变化时停止并保留记录。只读派生 PodMetrics 不参与持久资源归属，实际 Pod 仍逐一核验。历史 RWO UID 覆写故障仅可复用 `longhorn-rwo-first-failure-20261019.json` 中 namespace、完整当前对象账本和两个 Event UID/名称/原始引用完全一致的归属证据；不授权同名旧 Pod、其它 Event 或其它资源 |
| `--longhorn-recurring-diagnostics` | 不同步源码，只读核对原 runtime journal 的 run/cluster/source UID，输出计划组、源卷公共标签/状态及原 RecurringJob/CronJob metadata；不输出 backup configuration 或凭据，不变更计划或选择标签 |
| `--longhorn-idempotence` | 要求健康安装且无旧业务 journal；在本次真实 PVC 确认写入后，以同一 config 再执行产品步骤 09，比较 Helm revision、工作负载/PVC/Secret UID、generation、spec 和凭据摘要，再校验原文件及换 worker 重挂载。回收本次业务和传输，保留健康安装。LONGHORN-07 当前结果及实际同步输入见 `tests/evidence/longhorn-idempotence-20261019.json`，不替代全量交付验收 |
| `--longhorn-rwo` | 健康安装上顺序验证 RWO 文件系统、RWOP 单 Pod 排他及原始 Block；要求 Kubernetes 实际拒绝冲突挂载、原写入不变，并跨 worker 重挂载复验。首个失败停止，回收本轮 UID 绑定的业务对象 |
| `--longhorn-rwx` | 双 worker 同时使用原生 NFSv4.1 共享卷，验证文件权限拒绝；按 Volume→ShareManager→Pod UID 链限定替换故障，记录两个真实客户端的 fsync、I/O 错误/重试和最大单次等待，要求替换后原挂载继续完成写入及原确认数据校验。就绪耗时与客户端观察窗口分别记录；零错误也不推导零中断。LONGHORN-09 当前双客户端各331次确认、零I/O错误、约11.74/11.76秒最大单次操作等待及限定清理见 `tests/evidence/longhorn-rwx-20261019.json`；锁排他在故障前验证，不冒称持锁跨故障reclaim |
| `--longhorn-replica-scale` | 独立业务卷按原 UID 与字段前值变更 3→2→3 副本，依次使用 least-effort、best-effort 并恢复原均衡策略；核对独立节点、健康副本身份、Engine 全 RW、确认数据及跨 worker 重挂。通过 config+kubecli 验证三故障域下请求四副本的拒绝。仅回收本轮业务卷，不修改全局策略；LONGHORN-27 当前结果、三次实际拓扑及冻结输入见 `tests/evidence/longhorn-replica-scale-20261019.json`；不替代新增节点、跨区均衡或其它故障验收 |
| `--longhorn-replica-failure` | 产品重入后在独立三副本业务卷持续追加 64 KiB 确认账本；每条仅在 fsync 后确认，I/O 错误单列并重试同一未确认位置。沿 Volume/Replica/InstanceManager/Pod 身份链，通过固定版本 mTLS 原生 `process delete --name ... --uuid ...` 停止一份副本进程，要求观察失败、三个独立 RW 副本恢复、健康幸存副本身份不变及恢复后新增确认写入。逐条核验全部确认数据并跨 worker 重挂复验；原始确认记录纳入 gate 日志。仅回收本轮业务卷和传输。进程故障已通过，212 条确认记录跨 worker 全部复验，见 `tests/evidence/longhorn-replica-failure-20261019.json`；磁盘故障尚未覆盖，LONGHORN-19 保持 pending |
| `--longhorn-engine-failure` | 产品重入后，顺序在三份独立三副本 V1 业务卷验证 engine 原生 UUID 进程停止、远端副本所在 Instance Manager Pod 删除、Manager Pod 删除。共享组件必须仅承载本轮卷及已核对的原生进程；Pod 删除绑定 UID/resourceVersion，恢复必须出现新进程 UUID 或原父对象下的新 Ready Pod。持续 fsync 账本逐条核验，错误/重试和客户端退出码单列；engine 场景采用官方裸 Pod 人工重建路径，不声明控制器自动重启或零中断。恢复后新写入、原 9 MiB 确认数据及两组账本均跨 worker 复验；仅回收本轮业务和传输。LONGHORN-21 当前三类故障、635 条确认记录与三份原 9 MiB 数据跨 worker 复验、限定清理和 durable rc=0 均通过，见 `tests/evidence/longhorn-engine-failure-20261019.json` |
| `--longhorn-performance` | 健康三副本的独立 2 GiB 卷上执行单客户端、64 MiB 数据集的顺序/随机读写；顺序 1 MiB × 256 次、随机 4 KiB × 4096 次，每次写入要求 fsync，每个读取和最终 16384 块全部校验。输出实际 p50/p95/p99、IOPS、吞吐及两次带时间/窗口的 metrics-server CPU/内存样本，原业务确认数据和跨 worker 重挂保持。文件使用 O_EXCL，替代文件拒绝清理。读路径使用页缓存，延迟只含 pread 或 pwrite+fsync、吞吐含生成/校验开销；不冒称冷缓存、裸盘上限、资源峰值或 SLA。LONGHORN-38 当前基线已通过，实际结果及冻结输入见 `tests/evidence/longhorn-performance-20261019.json`；故障与稳定性另行验收 |
| `--longhorn-clone` | 验证固定 V1 CSI 对不同源大小请求的原 PVC 事件拒绝；同大小原生 PVC 克隆必须使用不同 PV/Volume，初始数据相同，克隆写入不改变源卷，并跨 worker 重挂复验源数据。先回收克隆再回收源；不能以两卷 Bound 替代隔离断言。LONGHORN-13 当前结果及实际同步输入见 `tests/evidence/longhorn-clone-20261019.json` |
| `--longhorn-expansion` | 顺序验证在线文件系统、原始 Block、加密文件系统从 2 GiB 扩到 4 GiB，原 PVC/PV/Volume/Pod UID 与绑定不变，CSI 加密扩容凭据引用正确；原挂载越过旧边界真实写入、fsync 和读回，原确认数据不变。要求 Kubernetes 对 1 GiB 缩容返回容量校验拒绝，网络错误不算拒绝证据。模块保留健康安装并回收自己的三类卷。LONGHORN-11 当前结果与冻结输入见 `tests/evidence/longhorn-expansion-20261019.json` |
| `--longhorn-encryption` | 产品重入后分别验证加密 Filesystem/Block：主机只读核对原始 LUKS、mapper 及容量；每类按原 Pod UID 删除自己的消费者，等待 detached 和全部 worker 的 mapper 关闭，再逐一注入错误/缺失 passphrase。只接受原 Pod 的原生密钥错误：Filesystem 为 FailedMount，Block 为 FailedMapVolume，恢复原同 UID Secret 后用新客户端复验原 LUKS UUID/容量/数据，并跨 worker 重挂。密钥 escrow 0600，JSON Patch 绑定 UID/resourceVersion/data，存在其它卷消费者时拒绝注入；失败与中断先恢复原 key。随后在独立 TLS 对象库执行加密全量/增量备份、逐项对象读取及两个新加密卷恢复，核对含 16 MiB 头部的备份容量、CSI 逻辑容量、原 LUKS UUID和源卷隔离；不直接 format、rekey 或强制打开映射。原完整入口已通过 LONGHORN-31 独立验收，最终干净全量交付仍以矩阵为准 |
| `--longhorn-encryption-block` / `--longhorn-encryption-backup` | LONGHORN-31 的窄修复入口，复用已核验身份和健康状态的产品安装及加密 StorageClass。前者仅执行 raw Block 的错误/缺失密钥拒绝、原密钥恢复、数据及跨 worker 重挂；后者仅执行 TLS 对象库、加密全量/增量备份与两个独立恢复，备份请求仍经 config+kubecli 步骤 09。均要求各自 BUSINESS_PASS、SCOPED_CLEAN_PASS、终态 PASS 和 durable rc=0；失败先恢复原密钥和限定夹具，backup 入口额外回收对象库。单一分支成功不签收 LONGHORN-31，原完整入口与最终干净全量仍必须通过 |
| `--longhorn-network-policy` | 产品步骤 09 重入后，保留原 NetworkPolicy UID/spec，验证三个 Manager 的管理 API/指标、三个 Instance Manager 的合法连接，并配对独立 HTTP 正向对照验证同 namespace 无许可客户端、异 namespace 同标签客户端被原生 socket timeout 拒绝。覆盖三轮共 36 次拒绝观察及三个 HA API Server 的 Snapshot admission server dry-run；不持久化该诊断请求，不修改网络策略。系统 namespace 探针按随机创建意图及原 UID 回收，原业务数据跨 worker 重挂复验；失败或中断先恢复原密钥再处理网络探针。仅证明同 namespace 合法指标网络，跨 namespace Prometheus 采集由 LONGHORN-34 验收；空 webhook source CIDR 不声明来源拒绝。原完整入口已通过 LONGHORN-33 独立验收，跨 namespace 监控和 mTLS 仍由各自场景验证 |
| `--longhorn-security` | 产品步骤 09 重入后，真实 impersonation 请求验证无授权业务 ServiceAccount 对系统 Secret、Pod、Volume 列表的原生 Forbidden，并以 server dry-run 验证特权 Pod 创建被原生授权拒绝且不落地资源；合法 TLS 1.3 客户端必须读到固定 Instance Manager 的 HTTP/2 SETTINGS 与预期服务端证书，错误 CA、缺失证书及同 CA 错误名称只接受精确 TLS 告警，明文关闭须有前后合法对照。删除自身 writer 并等待卷 detached、原生进程为空后，以双 CA 信任过渡逐阶段更新同 UID Secret、核对全部挂载 SHA256，并逐个重启原父资源的 Manager/Instance Manager。最终新 CA 允许、旧 CA 拒绝，并通过 Manager/CSI 在仅信任新 CA 时重新挂载及读回原数据；新消费者按业务 journal UID 卸载，卷 detached 且原生进程为空后，才反向分阶段恢复原材料与组件，再回收探针并复验原数据及跨 worker 重挂。0600 escrow 绑定 run、namespace、Secret、卷及父资源身份；恢复失败停止后续清理并保留恢复输入。LONGHORN-32 当前独立验收已通过，证据见 `tests/evidence/longhorn-security-20261019.json`；不替代跨 namespace 监控或最终完整交付 |
| `--longhorn-reclaim` | Retain 卷删除原 PVC 后保留原 PV/Volume，按原 UID 原子变更 claimRef，跨 worker 重新绑定并校验原确认数据；Delete 类必须由控制器自动回收。通过真实产品入口验证不可变类参数和已有默认类保护，按 UID 回收临时默认类 canary 并复核所有原类身份/参数。LONGHORN-10 当前结果及实际同步输入见 `tests/evidence/longhorn-reclaim-20261019.json`；不替代全部卸载、灾备或最终交付验收 |
| `--longhorn-trim` | 独立三副本文件系统卷写入、fsync 并读回 64 MiB 随机文件，通过产品声明创建快照；删除文件并执行原生 TRIM 后，核对应用释放空间而有效快照仍保留每份副本的实际分配块。只删除本轮快照，经原生 purge、跨 worker 重挂及 TRIM，要求三份原副本的 `st_blocks` 都真实下降，原确认数据不变；不改全局保留策略。LONGHORN-29 现场通过，三份原副本各回收 67,096,576 字节宿主文件系统分配块，跨 worker 重挂后原 9 MiB 确认数据不变，业务夹具及传输已限定回收；绑定结果和实际同步输入见 `tests/evidence/longhorn-trim-20261019.json`，不外推底层 SSD 释放量 |
| `--longhorn-backup-refusal` | 独立 TLS 对象库资格后，仅对本轮夹具 Secret 逐一注入错误 S3 密钥、无关有效 PEM CA 和固定白名单节点上已核验关闭的 endpoint 端口；经 config+kubecli 以 60 秒 target-ready 超时证明对应原生错误、请求未创建、对象库无备份数据。每次恢复原凭据并分离空 target；最后正确配置完成全量备份、对象逐项读取和新卷恢复。成功/失败/中断都先恢复本轮 Secret，再按原 UID 清理业务与对象库；LONGHORN-15 当前三类拒绝、正确配置备份恢复、原凭据/目标恢复及限定清理均通过，实际输入见 `tests/evidence/longhorn-backup-refusal-20261019.json`；不替代计划、灾备或全量交付 |
| `--longhorn-snapshot` | 产品配置创建原生 Snapshot，重入保持 UID，同名异参须在产品预检明确拒绝；另建 CSI 快照，并将原生快照通过静态 CSI content 接入恢复。两个独立恢复卷均须匹配快照前确认数据、排除快照后源写入，恢复卷写入不能影响源；源跨 worker 重挂后再验。配置临时变更可按本次 receipt 恢复，Retain content/native snapshot 与业务卷按原 UID 回收，禁止强制 finalizer。当前 LONGHORN-12 结果及冻结输入见 `tests/evidence/longhorn-snapshot-20261019.json`，不替代外部备份或应用一致性验收 |
| `--longhorn-backup` | 先完成独立 TLS 对象库资格。要求原默认 target 为空且无备份资源；runner 以私有 Secret 准备凭据，再通过 config+kubecli 完成全量和增量备份。逐一实际读取备份元数据及全部引用块，记录非零长度和 SHA256；两个独立 CSI 恢复卷分别校验恢复点边界和源卷隔离，源跨 worker 重挂复验。清理先按 UID 原生删除本轮备份/BackupVolume，再按 journal 恢复原 target 配置及回收本轮 Secret，最后回收对象库。target 原生轮询时间不属于配置归属；外来资源、UID/配置/凭据变化均停止回收。LONGHORN-14 当前结果、冻结输入和适用边界见 `tests/evidence/longhorn-backup-20261019.json`，不替代其它备份/灾备项 |
| `--longhorn-recurring-patch-diagnostics` | 不同步源码，按原资格记录核对 RecurringJob UID，使用远端原 API 构造 Cron 更新并仅执行 server dry-run，返回原生字段归属错误；对象已回收时停止，不创建替代对象，不构成验收 |
| `--longhorn-backup-patch-diagnostics` | 不同步源码，只在原安装 namespace、空 target 和无备份消费者的基线上，对原 UID/原 spec 分别执行当前及旧 resourceVersion 的 server dry-run；核对持久配置未变，用于区分补丁形状与版本前置拒绝，不构成业务验收 |
| `--longhorn-command-error` | 仅分类当前日志准确引用、属主私有的夹具 stderr，输出字节数、SHA256、固定错误类别布尔值和完整行匹配的主机断言白名单；不输出原文、请求载荷或凭据，不读取历史 stderr |
| `--longhorn-account-error` | 沿当前日志引用的 0600 traceback 读取其准确命名的私有账户响应，只输出固定原生错误代码、类别、rc 和 SHA256；不打印密钥、签名 URL、原始响应或历史记录，不同步源码、不变更现场 |
| `--longhorn-status` / `--longhorn-log` | 只读查看本分路 durable 状态、独立矩阵计数或最近 80 行日志；不启动 gate 或同步源码 |
| `--longhorn-source-fingerprint` | 不同步源码，只读返回远端本轮实际输入的 SHA256 清单；覆盖 Longhorn 角色、helper、playbook 和产品入口，不读取 clusters、凭据或缓存。本地后续修改不能冒充该轮已执行输入 |
| `--longhorn-v2-host-inputs` | 保留原 26 台通用授权主机的只读盘点范围：六台存储池、七台大容量池及 `.47.128`、`.47.130–138`、`.47.141–143`。不使用集群、不获取租约或同步源码、不更改主机；不包含开发机129或新 V2 池。逐机报告读取失败；内核通过不代表设备空闲、模块可加载或 V2 产品验收通过 |
| `--longhorn-v2-pool-inputs` | 只读核验新授权的 `.47.146–148` 三台：真实 OS/内核、SSE4.2、模块可用性、内存/hugepages、服务和已有目录、整盘签名/holders 及稳定 by-id/WWN/序列号。仅输出公开身份和状态，不读取凭据文件、不分配资源或擦盘；固定入口将日志与 rc 归档到 `logs/longhorn-cases/v2-pool-inputs-*`。`LONGHORN_V2_POOL_INPUTS_PASS` 只证明采集完整；每台 `assessment.blockers` 才描述环境缺口，消除缺口后仍须获得租约并通过 config+kubecli 的 V2 平台及业务门禁 |
| `--longhorn-v2-registry-qualification` | 新三节点的最窄镜像传输资格：先完成静态及已发布制品门禁，再继承与 Ceph 互斥的固定租约；控制机通过原有 BatchMode 密钥采集三机输入，拒绝外来集群/非空 Ceph 状态。在独立 `longhorn-v2-gate` 目录验证传输重连和 120 秒进程生命周期；目标机本地端口沿用已验证的 23520，三台与 V1 不重叠，SSH control socket 按目录隔离。仅创建关闭 Longhorn 的 config/hosts 和精确归属的临时 SSH/iptables 夹具；成功或失败均尝试限定回收。不安装 Kubernetes、不准备磁盘/hugepages、不签收 V2 平台或业务。未知归属或回收失败保留目录并停止，禁止直接删除后重跑 |
| `--longhorn-v2-core` | V2 传输资格通过后，经 config+kubecli 在新三台上部署独立 Kubernetes 1.33.6 三节点核心；固定 25% CPU/单 reactor 的后续资源预算要求至少四个在线 CPU 和 5 GiB 总内存，实际 V2 仍须检查 kubelet allocatable。先只读核验清洁基线并绑定机器/裸盘身份、记录原主机名，通过产品标准 prepare 设置唯一节点主机名，保留原控制机 kubeconfig；产品下载与 setup 90 受传输进程身份和两小时超时监督。复用标准跨节点 DNS/ClusterIP HTTP 写入读回 smoke 并清理其 namespace；成功保留健康核心，回收传输并还原控制机上下文。首失败先保存诊断，再仅清理原三台的本轮核心并恢复原主机名；机器被替换、存储或传输未回收时停止。它不分配 hugepages、不格式化裸盘、不安装 Longhorn，也不代替 LONGHORN-44 或业务验收 |
| `--longhorn-v2-resources-qualification` | 健康 V2 核心上最窄的临时页池资格，不安装 Longhorn、不更改引导器或裸盘。全部主机先核验机器身份、空闲页池和可用内存，再记录原值；按官方运行时入口每台分配 1024 个 2 MiB 页，重启该台 kubelet 并等待最多 180 秒，核对实际 allocatable 能通过单 reactor、25% CPU 的产品资源准入。随后恢复原页池和 kubelet 容量，再执行跨节点业务 smoke。部分分配须有明确响应才可恢复；响应丢失、外部变更、页池占用或安装资源存在均停止回收并保留 journal。正常/失败/中断均执行限定恢复，不代表独占 CPU、anonymous memory 等组合或 LONGHORN-44 完整验收 |
| `--longhorn-v2-resources-recovery` | 仅恢复 20261020 已归档的首次分配迟到响应：精确匹配原 journal SHA256/ID、机器与 boot、原分配程序及 PID 已退出，三机页池必须符合仅首机完整且空闲的状态。先归档歧义 journal，再确认写入并按原值恢复页池和 kubelet 容量，最后复验跨节点业务。不接管其它写入，不代替资源资格 PASS。分配和释放等待上限为 600 秒，等待期间保留 runner 心跳 |
| `--longhorn-v2-recurring-{selector,snapshot,backup,rotation}-qualification` | 测试门禁扩展，归属 LONGHORN-48；原 V2 AIO／对象库资格须有效，空业务、原 target、外来计划/CronJob 身份及 detached-job=false 基线须保持。先执行 selector 验证产品声明、组选卷和原生标签清理；其后 snapshot/backup 分别观察至少三个真实产物、retain=2、未选中卷不变及两个 detached 跳过周期，backup 还核对后端保留配置和全部引用对象、独立新卷恢复与源隔离。rotation 通过产品切换 bucket-only Secret、核对撤销后的原生失败和新恢复点，先恢复引用与停止计划再回收业务、对象库及传输。四个入口均须精确业务结果、durable rc=0、各自 `LONGHORN_V2_RECURRING_*_QUALIFICATION_PASS` 及 scoped-clean；最小反证为各入口。四个专项及完整 LONGHORN-48 组合中的四项均已通过并复验原基线；独立专项不覆盖启用 detached 作业或周期全量间隔，该部分由完整模块中的分离卷专项验证 |
| `--longhorn-v2-backup-store-qualification` | 测试夹具扩展，归属 LONGHORN-48。原健康 V2 AIO／三盘基线无业务、备份消费者或私有夹具；固定 MinIO 在跳板机独立 TLS 容器上使用 23621/23622，V2 节点 `.146:23623` 仅透传 TLS，经本轮归属 SSH loopback 23624 到对象库。三台均以 CA 校验 HTTPS，随机 bucket 实际写入读回；完整 profile、容器／Pod／namespace UID、socket、进程／boot 身份绑定。限定回收后复验原 target UID/配置、空业务、原裸盘及健康核心。预期 `LONGHORN_V2_BACKUP_STORE_QUALIFICATION_PASS` 与 scoped-clean；最小反证为本入口。沿用已发布制品和 V1 默认配置，不配置产品 target，不代替备份恢复 PASS |
| `--longhorn-v2-backup-refusal-qualification` | 测试门禁扩展，归属 LONGHORN-48；有效的原 V2 对象库资格后，复用健康三盘／空业务及目标基线。仅在本轮 Secret 中依次注入错误密钥、无关 CA、已确认关闭的 `.146:23625` 端口，经 config+kubecli 以 60 秒 target-ready 等待证明原生对应错误、无请求和备份对象，每次恢复原密钥及空 target；正确配置后创建全量恢复点，逐对象读取、恢复独立 2 GiB 卷，核对时间边界和源卷跨节点数据。业务／备份、目标引用／凭据、对象库及传输按依赖限定回收。预期 `LONGHORN_V2_BACKUP_REFUSAL_QUALIFICATION_PASS` 和 scoped-clean；最小反证为本入口，不代替轮换、计划、SystemRestore 或 DR |
| `--longhorn-v2-backup-qualification` | 仅在上述独立对象库资格及其绑定源码／集群身份有效后执行。通过 config+kubecli 完成 V2 全量／增量备份，逐项读取元数据和全部引用块，再从两个恢复点各创建独立 2 GiB 恢复卷；验证旧数据边界、独立写入、源卷最新摘要及跨节点重挂。成功／失败／中断先按 UID 回收业务与备份，再恢复原 target/Secret，最后清理独立容器及通道。预期 `LONGHORN_V2_BACKUP_QUALIFICATION_PASS` 与 scoped-clean；最小反证为本入口，不包含加密备份、计划、SystemRestore 或 DR |
| `--longhorn-v2-snapshot-qualification` | 测试门禁变更，归属 LONGHORN-48；复用原健康 V2 AIO、原三块盘和安装依赖。经 config+kubecli 创建原生快照、同请求重入及同名异参拒绝，再分别用动态 CSI 快照和静态导入的原生快照创建独立 2 GiB full-copy 恢复卷。原卷写入快照后新数据，两个恢复卷各读回快照前摘要、独立写入，源卷保持新摘要并跨节点重挂；逐卷核对 PVC/PV/Volume 容量及三份 V2 原盘副本。仅回收本次源/恢复卷、快照、Retain content、客户端和 relay；预期 `LONGHORN_V2_SNAPSHOT_QUALIFICATION_PASS` 及 scoped-clean，最小反证为本入口。不代表后台完整性扫描、外部备份、计划、系统恢复或 DR 已验收 |
| `--longhorn-v2-volumes` | LONGHORN-47 完整模块，须先完成下列五类最窄资格；按访问模式→扩容→回收→密钥→TRIM 顺序，使用相同固定分支重新验证当前输入。各分支自行复查无残留基线并回收所属业务，首失败即停止，保留原健康安装、原生盘身份与页池；全部成功才输出 `LONGHORN_V2_VOLUMES_PASS` 及 scoped-clean。不是最终 Longhorn 全分路干净回归 |
| `--longhorn-v2-volume-modes-qualification` | 复用已完成资格的健康 V2-only AIO 安装，绑定原 cluster/namespace/node UID 与未改变的生产及核心依赖；基线须无业务卷、PV、namespace 或残留 journal。依次验证 RWO 文件系统、RWOP、应用 Block 的冲突写入拒绝与 9 MiB fsync/跨节点重挂，再验证两节点 NFS RWX 锁互斥、原 share-manager Pod 故障和两客户端恢复。首失败停止，回收限定探针并复验原健康存储，保留安装与页池；不代表 LONGHORN-47 的扩容、密钥与 trim 完整验收 |
| `--longhorn-v2-expansion-qualification` | 测试门禁变更，归属 LONGHORN-47；复用同一健康 V2-only AIO 基线和限定业务回收。通过 PVC/CSI 将文件系统、应用 Block、加密文件系统三类卷从 2 GiB 在线扩至 4 GiB，验证原 Pod/PVC/PV/Volume UID 保持、旧摘要、新地址空间的真实写入/fsync/读回、缩容拒绝及换节点重挂；新建加密卷须具有 1.12.1 保留标签，宿主只读核验原 LUKS UUID、完整 4 GiB 映射和额外 16 MiB 原始容量。预期 `LONGHORN_V2_EXPANSION_QUALIFICATION_PASS` 及 scoped-clean；最小反证为本入口，不替代完整 LONGHORN-47 |
| `--longhorn-v2-reclaim-qualification` | 测试门禁变更，归属 LONGHORN-47；复用无业务残留的原健康 V2 AIO 基线。Retain 卷删除 PVC 后核对原 PV/Volume UID 和原 claimRef，以新 PVC 的 UID 原子预绑定同一 PV，再从另一节点读回原摘要；Delete 卷须回收原 PVC、PV、原生卷。随后经 config+kubecli 分别拒绝原有类的不可变回收策略变更和外来默认类替换，验证全部类身份和参数保持。限定回收业务、临时默认类和 relay，保留存储/页池；预期 `LONGHORN_V2_RECLAIM_QUALIFICATION_PASS` 与 scoped-clean，最小反证为本入口 |
| `--longhorn-v2-encryption-qualification` | 测试门禁变更，归属 LONGHORN-47；原健康 V2 AIO 基线须无业务、密钥、请求或回收类 journal。加密 Filesystem 与应用 Block 各执行错误口令、缺失口令两分支：先按 UID 删除原客户端并等待 detached、三节点带 `-encrypted` 后缀的 LUKS mapper 关闭，再以原 Secret UID/resourceVersion 注入；仅接受原 Pod UID 与 FailedMount/FailedMapVolume 下的原生密钥错误。原密钥从私有 escrow 恢复，新客户端核对同一 LUKS UUID、映射容量、原确认数据及三副本，再换节点重挂；限定清理全部探针并复验原基线。预期 `LONGHORN_V2_ENCRYPTION_QUALIFICATION_PASS` 和 scoped-clean，最小反证为本入口；不包含备份恢复或改 keyslot |
| `--longhorn-v2-trim-qualification` | 测试门禁变更，归属 LONGHORN-47；复用原健康 V2 AIO，要求三块盘仅有本次一个未加密 RWO 卷和三份原副本。依据 1.12.1 SPDK lvstore 空闲 cluster 到原生 Node `storageAvailable` 的映射，按三盘实际分配量观测，等待超过两个 30 秒采样周期的稳定值。64 MiB 随机文件 fsync/读回后，经 config+kubecli 创建有效快照；删除文件并 TRIM 时保留分配，仅删除本次快照、跨节点重挂并再次 TRIM 后各盘实际减少至少 32 MiB，原确认数据不变。不套用 V1 `st_blocks`、removed-head-parent 或自动删快照开关；限定清理、原盘身份复验，预期 `LONGHORN_V2_TRIM_QUALIFICATION_PASS` 与 scoped-clean。最小反证为本入口；不外推虚拟化宿主/SSD 物理回收 |
| `--longhorn-v2-host-recovery` | 仅恢复 2026-10-20 socket 启动顺序资格失败留下的原 journal；绑定首失败 SHA、机器/boot、配置、DM 和三台状态，先全体只读检查，再停止 `.146` 已恢复的无消费者 daemon，按 socket→service 顺序恢复原状态，最后复验跨节点 HTTP。任何不符或响应丢失保留 journal；不适用于通用重试 |
| `--longhorn-v2-host-qualification` | 独立 V2 核心上的最窄主机夹具资格。三台先记录原主机文件与 iSCSI 状态，以及 multipath 服务/socket、配置哈希、boot/machine 和全部 DM 身份；存在多路径映射或未知状态时拒绝。仅临时停止无消费者的 multipath 服务/socket，不更改持久启用状态；随后完整恢复原状态并复验跨节点 HTTP。丢失响应或身份变化保留 journal，不安装 Longhorn、不分配页池、不写裸盘 |
| `--longhorn-v2-aio-install-qualification` | 资源资格完成后，在独立三节点核心上经 config+kubecli 下载和安装 1.12.1，仅启用 V2；使用三块已绑定 WWN 的 AIO 裸盘。核对原生 Node 磁盘 UUID、V2 卷/副本及三台 Instance Manager；业务写入 9 MiB 并 fsync，跨节点重挂后逐字节哈希一致。成功只回收业务探针和传输，保留健康存储、主机及页池 journal 供后续模块；失败优先原生卸载，再恢复原主机文件和页池，遇到身份不明则保留现场。此资格不等于双引擎、裸盘替换或完整 LONGHORN-46 |
| `--longhorn-recurring-backup-qualification` | LONGHORN-16 的计划备份前置，当前专项已通过，证据见 `tests/evidence/longhorn-recurring-backup-qualification-20261019.json`。通过 config+kubecli 创建每分钟 backup-force-create、retain=2 的独立计划与组选卷，观察至少三个原生完成备份并核对 Job/Pod UID 链；暂停计划后读取两份保留备份的全部引用对象，核对后端只剩两份配置。源卷分离后恢复频率，至少两个周期不创建新快照/备份、不自动附着；未选中卷读回不变。先由产品停止计划及原生子任务，再按精确 UID/spec 交接动态备份清理；独立 CSI 新卷恢复原数据、排除后续源写入并验证写隔离和源卷跨 worker 重挂。对象库、业务及传输均限定回收；只证明此资格，不代替账户轮换/原生失败及完整 LONGHORN-16 |
| `--longhorn-recurring-rotation-qualification` | LONGHORN-16 的凭据轮换与原生失败前置。经 config+kubecli 选择三个独立 bucket-only Secret，分别要求新 Backup UID 及签名配置读取；撤销旧身份后核对原生 InvalidAccessKeyId，撤销当前身份后必须出现新的原生失败点。仅取消本轮卷的原生组选择以排空在途任务，保持同一计划；修复后只允许已冻结 UID/spec 的预期失败，要求新的成功点、全部引用块读取及失败点配置不存在。先停止计划、恢复原引用并回收账户，再独立恢复验证时间边界、源隔离和跨 worker 重挂。当前专项通过：三个账户分别产生新备份，原生撤销拒绝、失败点外部元数据不存在、独立恢复及限定清理均通过，68 项冻结输入首尾一致、durable rc=0，见 `evidence/longhorn-recurring-rotation-qualification-20261020.json`。原生失败 Job 的在途排空按固定源码允许 600 秒，本轮约 371 秒；这项资格不代替完整 LONGHORN-16 |
| `--longhorn-recurring` | 依次执行计划 snapshot、backup、rotation 三个完整业务分支；每阶段要求停止计划、业务和限定清理标志，首失败停止，最终 durable rc=0 和传输/业务清理后才输出 LONGHORN_RECURRING_PASS。当前完整 V1 模块已通过：快照与备份各观察到 5 个身份、保留 2 份，分离卷各两个周期跳过；轮换证明三个不同成功备份身份、原生撤销失败、失败点无外部配置、新卷独立恢复与源卷重挂。68 项实际执行源码首尾一致，durable rc=0、零失败标志及全部限定清理见 `evidence/longhorn-recurring-20261020.json`。不代替 V2、其它待验项或最终干净全量验收 |
| `--longhorn-business-diagnostics` | 不同步源码，按原业务 namespace UID 读取 Pod/PVC/Event 和原生卷、副本、快照、备份、目标及 Instance Manager 的限定身份/状态字段；不输出 Secret、原始错误、URL 或任意 spec/status。对象库存在时核验原夹具身份并只读检查对象库与六节点 TLS 透传健康，失败仅报告异常类型和 errno，不输出凭据或 CA。诊断不是业务验收 |
| `--longhorn-storage-diagnostics` | 不同步源码的 Longhorn Pod/DaemonSet/Deployment、事件、节点 CR、Pod 描述和限定 Manager/deployer 日志；抽查固定控制节点 `.135` 与 worker `.212` 的磁盘/内存、CRI 镜像和 kubelet/containerd 日志。历史日志须按时间与本轮 Pod UID 区分，不能把旧错误冒领为当前根因 |
| `--longhorn-diagnostics` / `--longhorn-host-inputs` / `--longhorn-failure` / `--longhorn-image-diagnostics` / `--longhorn-preflight-diagnostics` | 不同步源码的只读诊断；前者收集核心节点、Pod、事件、Calico 及三个 API 服务的有效启动参数，主机入口核对六机相关 systemd 单元/drop-in 的路径/SHA256 及数据路径存在性，failure 入口仅按当前日志读取本轮 0600、原属主的 Python traceback，不遍历历史文件或凭据；镜像入口对比本地与固定发布 manifest 的配置、层和 DiffID，不输出配置环境变量；preflight 入口导入当前远端原源码和原输入，只执行只读产品前置，报告失败命令与调用栈，不运行 main 或输出 API 载荷。诊断文件保留在忽略的本地日志目录，不构成验收 PASS |
| `--longhorn-core-host-preflight` | 干净节点上的最窄主机输入门禁；检查相关单元、连字符前缀及全局 service drop-in，任何覆盖文件均阻断新核心；不删除文件 |
| `--longhorn-core-host-recover` | 仅处理已诊断的 `.72` 旧 SkyWalking API drop-in，路径与 SHA256 必须完全匹配，且相关服务已停；将原文件完整归档到控制权限的历史目录，daemon-reload 后复查六机输入与清洁状态。其它文件保持原样并拒绝恢复；不适用于健康核心或已安装存储 |
| `--longhorn-cancel-product` | 只终止当前 runner 拥有的唯一精确 `setup longhorn-gate 07`、`09` 或 `90` 产品子树，保留 durable wrapper 的 EXIT 回收；额外参数、相似步骤、外部进程或不唯一的产品进程均拒绝。不等同于通用强杀或安装恢复 |
| `--longhorn-middleware-reentry-cleanup` | 交付操作人在开发机仓库根目录执行，仅恢复 `longhorn-middleware-reentry-first-failure-20261022.json` 绑定的 LONGHORN-37 失败现场。获取原租约、通过静态及制品前置后，先核对原集群、数据库及监控 namespace UID，以及两版同输入 Helm release 的原 UID、摘要和真实 hook 窗口；不符即停止。资格通过才由原 cleanup 按原生 finalizer、CSI Delete 和依赖顺序回收 PXC、六个监控卷及所属 namespace/CRD/类，恢复输入、context、传输并复验原核心。沿用原 cleanup 的各对象超时（原生 PXC finalizer 最多 1800 秒）；失败保留 journal 和首失败，诊断后从本入口复验，不强删 finalizer 或 release Secret。只有 `LONGHORN_MIDDLEWARE_REENTRY_QUALIFICATION_PASS`、各 scoped-clean、`LONGHORN_MIDDLEWARE_CLEANUP_PASS` 及 durable 双 0 齐全才完成；这是精确恢复资格，不签收数据库业务或 LONGHORN-37 |
| `--longhorn-registry-recover` | 仅恢复原先未初始化核心的旧转发残留：按监听 PID、启动时间、boot ID、UID 和 loopback 端口绑定身份，拒绝替代进程，回收后执行固定清洁验证；不用于已安装存储 |

本分路经 `192.168.47.130` 租用 `.122.135/.40/.72` 为控制面，
`.122.212/.165/.238` 为 worker，在源码同步前持有 Ceph-priority、Ceph-compute
和 Redis-core 三把共享租约。数据挂载仅为 `/data/kubeauto-longhorn`；
设备只来自本轮只读资格生成的 18 盘 allowlist，不能将设备名猜测当作授权。
干净主机缺少 `/data` 时，由磁盘夹具在全部盘预检通过后创建并记录目录的设备号与 inode；
回收前复核身份及未知文件，只删除本轮创建的空父目录。已有 `/data` 保持原样，
符号链接、替换目录或丢失创建响应都会阻断回收，不通过临时 SSH 手工补目录。
开发机不能作为节点，已有数据库/存储集群不能作为本分路基线。

安装失败时先保留阶段、退出码、Pod/events、Manager 日志和 fixture journal。
业务探针按原 UID/PV claimRef 回收；存在 Longhorn CRD/卷时，产品 guard 阻止
清盘和恢复 iSCSI 服务，保留现场等待原生卸载或受控恢复。不得解除 finalizer、
强制 wipe 或直接借用失败环境继续测试。主机 journal 只处理两份公开产品文件及
`iscsid.service/socket` 的原状态；共享依赖软件包、已加载内核模块和固定镜像缓存保留。
最终交付仍须实现并验收全部适用矩阵项、完成一次干净完整专项和最终
`LAB_CLEAN_VERIFY_PASS`；尚未实现的分支明确拒绝执行。

## MongoDB 独立交付分路

MongoDB 使用 `mongodb-test-matrix.yaml` 的 43 项独立验收，不能继承核心、
PXC、Redis 或 Ceph 的业务 PASS。产品入口是部署目录的 `config.yml`、
`kubecli download -E mongodb` 和 `kubecli setup mongodb-gate 07 --tags mongodb`。
固定版本为 Kubernetes 1.33.6、Percona Operator 1.23.1 / Chart 1.23.2、
PSMDB 8.0.32-14、PBM 2.15.0。Kubernetes 1.33.6 的项目现场验证与上游
声明的测试范围分别判断，不能把本项目测试解释为厂商支持承诺。

本分路通过 `192.168.47.130` 租用 Ceph-priority 六台主机作为计算池：
`.122.135/.40/.72` 为控制面，`.122.212/.165/.238` 为 worker。它不消费
Ceph 数据、额外磁盘或既有 Ceph 验收。Longhorn 仅使用系统盘的独立目录
`/var/lib/kubeauto-mongodb-longhorn`，专属 namespace 为 `mongodb-lab-storage`。
数据库 namespace 为 `mongodb-gate`。禁止借用已有 `redis-gate` 或 `k8s-dev`。
运行器在同步控制节点源码之前取得既有共享租约，活动任务期间拒绝另一轮启动。

所有入口都从开发机执行 `bash tests/run_enterprise_regression.sh MODE`：

| 固定入口 | 证据与清理边界 |
| --- | --- |
| `--mongodb-sharding-restore` | M31 测试门禁：消费已校验逻辑存储夹具，在独立 config+kubecli 目标建立两个三成员 shard、三 config 成员和三 mongos，使用九个独立 2Gi 卷。迁移真实数据后开启 PITR、完成协调逻辑备份，校验 cfg/rs0/rs1 元数据及 oplog 对象 SHA256；用服务器时钟确定恢复点，在两个 shard 分别写入前后标记，并在截止后逆转 chunk 归属。全局可恢复时间覆盖后关闭 PITR，通过产品 backupSource+target_utc 恢复同一独立目标；验证双 shard 前标记包含、后标记排除、199 条基线、索引、chunk 范围及归属，以及 catalog UUID 与两个 shard 实际集合 UUID 一致。通过该一次性目标的既有系统 monitor 身份执行 TLS 只读 UUID 核验；该身份由 Operator 在每个 shard 本地创建，普通 mongos 自定义用户不作为 shard 本地身份，不增授角色。成功、失败或中断均限定回收目标、九卷、客户端和本 run 的 sharding 前缀；原副本集及已验收逻辑备份保持。预期 `MONGODB_SHARDING_RESTORE_PASS` 与 scoped-clean，最小反证为本入口；静态成功不代表现场签收 |
| `--mongodb-static` / `--mongodb-disabled` / `--mongodb-validation` | 本地语法、矩阵、版本、官方 Chart/CRD、模板、独立 tags、拒绝输入及关闭路径；不连接实验室 |
| `--mongodb-documentation` | 本地核对三份正式手册的内容评审记录、两类读者路径、实际图形渲染与适用产品证据。`tests/evidence/mongodb-documentation.json` 绑定正文及操作证据 SHA256；正文或证据变更、未关闭缺陷或缺失评审均拒绝。机器检查只证明评审绑定仍有效，不代替文字质量评审，也不连接实验室 |
| `--mongodb-sharding-scale` | 已验证的独立两 shard 基线上，经 config+kubecli 新增 rs2；实际搬入两个有数据的 chunk，核对摘要/索引，搬出数据和 primary 元数据，确认 donor 无用户数据库后再从产品配置移除；最后按 UID/claimRef 回收三份退役 PVC/PV。失败保留首诊断并走原有分片夹具整体清理；源副本集和备份保持独立。新增 shard 的 TLS SAN 必须预先具备，旧夹具证书缺失时拒绝 |
| `--mongodb-disaster-recovery` | 测试门禁变更，归属 MONGODB-26；复用已验收物理备份夹具的独立 TLS 对象存储，通过独立 config+kubecli 建一次性源，生成全新系统密码/应用密码/加密密钥/TLS，写入 Decimal128、Int64 和唯一索引并生成新物理备份。运行专属 `disaster/<run-id>` 前缀，不消费旧备份。保存 `0600` 凭据托管文件及其 SHA256 后，移除源 Backup CR（不含删远端数据 finalizer），按现有 UID/claimRef 边界删除源 CR、Pod、Secret、PVC/PV；证明全部消失并重新核对远端对象 SHA256，再仅凭托管文件及对象存储在新 CR UID、新卷上通过产品 `backupSource` 恢复，验证应用用户、BSON 精度、唯一索引及目标额外标记被排除。成功、失败或中断均回收一次性目标、卷、托管文件和本 run 前缀，核对原备份 SHA 与源健康；预期 `MONGODB_DISASTER_RECOVERY_PASS` 和 scoped-clean，最小反证为本入口。该场景模拟数据库集群丢失，保留 Kubernetes、管理节点和对象存储，不能外推为异地整站恢复或 Kubernetes 控制面重建 |
| `--mongodb-artifacts` | 只读核验 MongoDB 8 项、支持镜像 4 项和 CSI 13 项固定发布记录及当前 TalkEdu manifest；不重建或重发制品 |
| `--mongodb-ceph-pool-probe` | 六机 CPU、已有服务/目录及固定清洁验证；已有集群会阻断清洁判定 |
| `--mongodb-core` / `--mongodb-install` | 从已验证清洁现场通过产品入口安装专属核心、CSI、Operator 和数据库；安装还要求 TLS majority 读写及客户端回收；通过后保留健康基线 |
| `--mongodb-install-retry` | 仅恢复 CRD/控制器创建前失败的凭据阶段：核验 namespace 和全部资源身份，按 UID 删除本轮 namespace，再执行正常产品安装；存在 CRD、工作负载或外部资源即拒绝，保留核心与 CSI |
| `--mongodb-install-business` | 产品数据面已通过而安装业务断言失败时，先验证当前代次/TLS/仲裁/加密，按 UID 回收原安装客户端，再经产品步骤 07 复验业务；保留已收敛的数据库和 CSI |
| `--mongodb-reconcile` | 无故障残留的健康基线上更新本分路配置并执行产品步骤 07；预期配置变更不能当成二次执行幂等证据 |
| `--mongodb-platform` / `--mongodb-preflight` | 前者执行实际 Kubernetes DNS/跨节点 HTTP 业务及 CPU 检查；后者检查身份、独立调度域、节点压力、时钟和系统盘余量；均保留健康基线 |
| `--mongodb-storage` | 独立 PVC 写入、1→2 GiB 扩容、换 worker 重新挂载后的数据保留；回收并等待专属 Pod/PVC/PV 消失 |
| `--mongodb-data` / `--mongodb-transactions` / `--mongodb-security` | 真实 TLS 客户端验证 CRUD/BSON/索引/TTL、事务隔离与确认、凭据/权限/CA/hostname 拒绝；回收当前 run ID 的集合及客户端 |
| `--mongodb-rotation` | 更新原 Secret 内五类系统口令及应用口令，经 config+kubecli 收敛；新口令认证成功、旧口令逐项拒绝；内外 TLS 按双根过渡、新叶证书、单根三个阶段滚动所有成员，逐成员拒绝旧 CA；以相同过渡流程恢复原凭据并复验数据、PVC/Secret/CR 身份和配置，恢复失败保留私有 journal |
| `--mongodb-network-client` | 允许客户端、移除许可标签后拒绝连接、恢复标签后重新连接；仅为完整 network 项的部分证据 |
| `--mongodb-network` | 真实新连接验证应用许可/拒绝、三成员复制和监控端口隔离；通过 config+kubecli 临时将监控 namespace 设为数据库 namespace，逐成员抓取有效 exporter 指标，并证明监控权限不授予数据库访问；重新校验已通过逻辑备份的远端对象作为未变更备份依赖；恢复原 namespace/策略，复验拒绝访问、数据库与凭据/PVC 身份，失败保留 fault journal |
| `--mongodb-failover` | 主/副本 Pod 真实丢失，持续 majority+journal 确认账本与恢复；按 UID 注入并保留故障 journal，不能跳过残留恢复 |
| `--mongodb-maintenance` | 持续 majority+journal 写入期间，按 MongoDB rs0 selector drain 一台自有 worker；PDB 预算耗尽时用服务端 dry-run 证明第二次驱逐以 429 拒绝且 Pod UID 不变；恢复节点调度后逐成员真实驱逐并核验确认账本、PVC 保留和限定清理，不等同于全节点排空或主机重启 |
| `--mongodb-rebuild` | 核验单个健康从副本及其 CSI/PVC/PV 归属后，按官方逐成员流程先删除 PVC 再删除 Pod；必须等待新 Pod、PVC、PV 身份，核对三个成员的完整业务摘要和唯一索引，以及旧 PV 回收、其余 PVC 和凭据保留；恢复失败保留 fault journal 并阻断后续场景 |
| `--mongodb-quorum` | 临时策略双向隔离两个自有成员，并核验同 UID Pod 内 mongod 容器实际重启以断开既有复制连接；确认全部成员停止宣告可写主、majority 写入限时拒绝；finally 恢复原策略及标签，复验已确认数据与恢复写入，失败保留观测记录，恢复失败保留故障 journal |
| `--mongodb-idempotence` | 第二次真实产品执行前后比较 CR、Pod、PVC、Secret 身份及配置，排除正常 API bookkeeping 和 Helm 历史 Secret |
| `--mongodb-capacity` | M33 完整模块：顺序执行扩容、受限磁盘压力、单从副本复制滞后及恢复三个独立边界，每段均有健康/身份准入和限定清理；首个失败立即停止，不执行后段。仅当三段均完成时输出 `MONGODB_CAPACITY_SCOPED_CLEAN_PASS`、`MONGODB_CAPACITY_PASS`，仍不替代最终干净完整回归；产品卷已增大后不缩回 |
| `--mongodb-logging` | 最窄日志配置迁移：仅接受 runner 自有 1–10Gi 源和缺省/实验室策略；需要时复用确认账本扩容门禁，经 config+kubecli 扩至 10Gi 并核验三卷文件系统，再经同一入口设置每分钟检查、32MiB、保留一份的未压缩原生轮转。核验自有 ConfigMap、三个 sidecar 周期、真实轮转/空间、应用数据、PVC/PV/Secret/CR 身份与三类保留备份 SHA；限定清理本轮业务和客户端，保留不可逆扩容和期望轮转策略。扩容中断走 `--mongodb-capacity-recover` 后重试；不删除既有日志、不缩卷、不降低低空间告警。预期 `MONGODB_LOGGING_PASS` 及 scoped-clean；最小反证为本入口，不增加或替代矩阵覆盖 |
| `--mongodb-capacity-expand` | M33 的最窄扩容分支：健康副本集 1–7Gi 专属卷声明增加 1Gi，经 `config.yml + kubecli` 显式开启卷扩容；majority+j 连续写入维护确认账本，检查三块 PVC/PV UID、claimRef、实际 capacity、文件系统及完整账本（通过租用 worker 上只读 tune2fs 的原始块数与元数据开销精确核对 ext4 statfs；拒绝设备不匹配及 bigalloc），关闭原本关闭的扩容开关并保留增大后的声明；限定回收自有业务数据/客户端，预期 `MONGODB_CAPACITY_EXPAND_PASS`。本分支不替代尚待完成的磁盘压力/复制滞后及最终完整 M33 |
| `--mongodb-capacity-recover` | 只接受原 capacity journal、核心/CR/PVC/PV 身份、精确原配置或已应用配置及原客户端 UID；先终止该客户端的远端负载，产品入口完成已声明的扩容并核验文件系统，再恢复开关和清除自有数据/客户端。已经增大的容量不回退；身份或配置异常保留 journal，预期 `MONGODB_CAPACITY_RECOVER_PASS` |
| `--mongodb-capacity-pressure` / `--mongodb-capacity-pressure-recover` | M33 的最窄压力分支与故障恢复：核验自有从副本、三块 PVC/PV 和核心/CR 身份；带 run-ID 的专属填充目录按不超过 128MiB 的步长到约 75% 文件系统使用率，每步重新读取当前余量并留 8MiB 并发写入缓冲，最终余量在安全水位以上 16MiB 内；始终保留至少 1GiB，每步要求三成员 journal 写入；压力期间核验 512 条业务及索引的逐成员摘要。每次 dd 在容器内限时 20 秒、强制退出宽限 5 秒，清理等候写锁退出最多 35 秒，仅删除身份标记匹配的目录，恢复健康并核验安全余量；恢复失败保留 journal，替换的 Pod/卷或外来客户端一律拒绝。预期 `MONGODB_CAPACITY_PRESSURE_PASS` / `MONGODB_CAPACITY_PRESSURE_RECOVER_PASS`，不替代扩容、复制滞后和完整 M33 |
| `--mongodb-capacity-lag` / `--mongodb-capacity-lag-recover` | M33 的最窄复制滞后分支与恢复：复用已有网络故障模型，只隔离一个原健康从副本并验证同 UID mongod 真实重启以关闭旧复制连接；本地 TLS/数据库管理账号只读核验 appliedOpTime、wall time 和本次集合，要求 512 条已获 majority+journal 确认的数据在隔离成员仍不存在且本地应用位点未变。恢复前完整核验策略、成员、客户端及卷身份，按 UID/资源版本删除 deny，等待健康，再核验三成员完整业务/唯一索引摘要；回收本次数据/客户端和故障标记。预期 `MONGODB_CAPACITY_LAG_PASS` / `MONGODB_CAPACITY_LAG_RECOVER_PASS`；缺失恢复证据或外来对象拒绝修改，首失败和恢复失败 journal 保留，最小反证为对应入口 |
| `--mongodb-logical-backup` / `--mongodb-physical-backup` | 产品配置与不可变请求驱动 PBM；独立 TLS 对象存储验证非零备份对象、远端元数据和逐对象 SHA256；恢复原配置及业务夹具，保留自有备份 PVC/CR 供恢复场景消费；不替代恢复验收 |
| `--mongodb-backup-clients-recover` | 节点重启后恢复已有逻辑、物理及 PITR 备份的无状态 mc 客户端。只接受原备份 journal、资源 UID、源集群及卷绑定不变且无进行中目标/故障；按原 UID 替换旧 Never 客户端为 Always，通过持久恢复记录核对替代 UID，保留 MinIO/PVC/Secret 和全部备份对象。等待存储和客户端就绪后复验请求身份、全部对象 SHA256 及源身份，预期 `MONGODB_BACKUP_CLIENTS_RECOVER_PASS`；失败保留恢复记录，最小反证为本入口，不签收备份/性能模块 |
| `--mongodb-backup-refusal` | M24 测试门禁：从已验证的逻辑基线建立独立目标，目标 config+kubecli 分别验证随机错误 S3 凭据与自有存储 Service 无后端时的原生拒绝；临时关闭目标自有 allow 后隔离两个目标成员，确认无可写主且产品健康检查阻止备份 CR 创建。最后使用从未开启 PITR 的原基线请求缺失 oplog 的时点，要求固定上游缺失历史错误、未发出 PBM restore 且目标原标记不变。只有四类原因、源备份 SHA、配置/策略/标签/目标卷及失败前缀清理全部完成才输出 `MONGODB_BACKUP_REFUSAL_PASS`；失败或中断按原 journal 恢复自有 Service/成员故障后回收独立目标，恢复未完成保留 journal，最小反证为本入口。此入口仍需现场聚焦验收，不继承其它备份 PASS |
| `--mongodb-backup-refusal-oplog` | M24 缺失 oplog 的最窄分支，验证原生缺失历史错误、PBM 未派发及目标原业务标记不变；回收独立目标/卷/前缀并复验源备份 SHA。预期 `MONGODB_BACKUP_REFUSAL_OPLOG_ONLY_PASS`，不签收完整 M24；最小反证为本入口 |
| `--mongodb-backup-refusal-tail` | M24 仲裁/缺失 oplog 的最窄分支；复用已校验源备份，创建新的独立目标，先经产品显式关闭目标 namespace 的自有网络策略，再验证无仲裁拒绝和原生缺失 oplog 拒绝，最后回收目标/卷/故障/失败前缀。只输出 `MONGODB_BACKUP_REFUSAL_TAIL_PASS`，不能替代四项完整 M24。最小反证为本入口 |
| `--mongodb-logical-restore` / `--mongodb-physical-restore` | 通过独立部署目录的 `config.yml + kubecli` 创建同 namespace 的独立目标 CR/PVC；新 CA 与目标 SAN、原系统密码及加密密钥，目标配置不预建应用账号；创建目标前检查专属 CSI 在三个 worker hostname 域发布的 maximumVolumeSize（缺省时 capacity），每域须容纳完整目标 PVC，磁盘实际空闲不能代替分配额度；重新校验备份对象后按目标 UID 恢复，验证 BSON、索引、账号与备份边界；物理恢复另在私有诊断目录采集目标 CR/Pod/StatefulSet/Service/EndpointSlice 时序，单次只读 API 超时 5 秒、采样间隔 2 秒，退出时停止采集线程，不保存 Secret 或任意 Pod 配置；按 journal/UID/PV claimRef 回收目标并复验源集群，失败保留首失败和未回收目标记录 |
| `--mongodb-encryption` | M17：复用已校验物理备份，在独立目标以不存在的 Secret 验证产品 preflight 拒绝且 CR/PVC 未变；有效但错误的32字节密钥须触发 PBM Restore error 与固定 PSMDB 原生解密指纹。限定回收错误目标后，以原托管密钥恢复同一备份并验证 BSON、索引、用户与备份边界；两类目标及其卷均清理，源身份与备份摘要保持。预期 `MONGODB_ENCRYPTION_PASS`，最小反证为本入口；泛化网络/容量错误不计成功 |
| `--mongodb-pitr` | 独立 TLS S3 夹具启用连续 oplog 后执行逻辑基线备份；以数据库服务器时钟写入恢复点前后标记，等待 latestRestorableTime 覆盖目标并校验非零 oplog 对象 SHA256；关闭源 PITR 并恢复源配置后，通过独立目标 config+kubecli 按 UTC 恢复，验证前标记包含、后标记排除、BSON/索引和账号恢复；回收目标，保留已验证备份供数据保护分支消费 |
| `--mongodb-sharding-install` | 测试门禁变更，归属 MONGODB-27；通过独立 `mongodb-sharding-gate/config.yml`、绑定该目录的 inventory 与 `kubecli setup mongodb-sharding-gate 07 --tags mongodb` 安装两个三成员 shard、三个 config server 与三个 mongos；独立密码/CA/加密密钥和九个不与源共享的卷，三故障域及 TLS mongos majority+j 业务通过后移除客户端/集合并保留健康基线；预期 `MONGODB_SHARDING_INSTALL_PASS`，最小反证为本入口，后续分片数据/故障/迁移/恢复仍须独立验收 |
| `--mongodb-sharding-data` | 测试门禁变更，归属 MONGODB-28；只消费前项经身份、卷、TLS/加密和健康复验的产品分片基线；通过真实 TLS mongos 客户端建立 run-ID 数据库及唯一范围分片键，验证 200 条路由写入、重复分片键拒绝、更新/删除、位于不同 shard 的双记录事务提交及回滚；显式迁移两个已写入 chunk 并核验 catalog 归属交换、199 条记录和业务索引、迁移前后相同 SHA256；每次迁移上限 60 秒且等待原 shard 范围清理；最后 majority 删除该数据库、客户端并复验源身份/健康，预期 `MONGODB_SHARDING_DATA_PASS` 和 scoped-clean，最小反证为本入口；首失败及恢复失败 journal 均保留，不签收故障、扩缩或分片恢复 |
| `--mongodb-sharding-failure` | 测试门禁变更，归属 MONGODB-29；产品分片基线和 run-ID 跨 shard 业务数据先通过健康/摘要检查；分别对真实 rs0 Primary、cfg Primary、一个 mongos 注入 UID 约束的 Pod 丢失，等待替代 UID、完整健康和数据/索引摘要；随后从原 allow 策略排除两个 rs0 成员，以 deny 隔离复制通信（仅保留 DNS 与本集群 cfg 的启动访问）和限定容器重启关闭旧复制连接，等待三个成功 hello 后逐成员证明无可写 Primary，并用实际 mongos 对 rs0 范围验证 majority+j 写入在 15 秒内拒绝；恢复前一次性校验全部 policy/member/deny 身份，再恢复原策略/标签、回收 deny、复验三副本及原摘要；最后回收自有业务库/客户端，预期 `MONGODB_SHARDING_FAILURE_PASS` 和 scoped-clean，最小反证为本入口。首失败和未恢复 fault journal 保留；原 source 和存储夹具不变，未包含多角色同时故障或稳定性长测 |
| `--mongodb-sharding-recover` | 测试门禁恢复分支；仅消费原故障 journal、run-ID 和 UID 绑定的客户端，完整验证策略及成员归属后恢复网络，验证健康、原业务摘要，再删除自有数据库和客户端；保留健康产品分片基线与源副本集，预期 `MONGODB_SHARDING_RECOVER_PASS` 和 scoped-clean。缺失客户端、角色丢失未恢复或摘要失败时保留 journal，不签收 M29。最小反证为本入口 |
| `--mongodb-sharding-clean` | 核验源快照、核心 UID、目标资源 UID 和全部 PV claimRef 后限定回收分片目标；客户端创建前持久化 run ID、创建后保存 UID，跨进程清理按原记录执行。对于尚未保存客户端身份的旧失败门禁，只有已保存首失败诊断中的原 UID/run ID 才能通过运行时 `MONGODB_SHARDING_RECOVERY_CLIENT=UID,RUN_ID` 提供恢复证据；该参数仅允许此清理模式，同时核对客户端引用的目标 CA/凭据，不从当前同名 Pod 推断原身份。复验目标图和卷均消失以及源配置、身份和健康保留，预期 `MONGODB_SHARDING_CLEAN_PASS`；失败保留 journal，未知 finalizer 或外部卷一律拒绝，最小反证为本入口；完整清理先执行该边界再清理源副本集 |
| `--mongodb-restore-clean` | 恢复失败目标的最窄清理分支；核验目标 journal、源身份及全部目标卷归属，通过产品入口恢复必要的源渲染文件，再限定回收目标并复验源业务健康，保留已验证备份制品 |
| `--mongodb-logical-restore-inplace` / `--mongodb-physical-restore-inplace` | 源集群原地恢复的部分证据，不替代矩阵要求的独立新目标恢复；只消费对应已验证备份，重读远端校验值后按目标 CR UID 提交产品恢复请求；复验 BSON/索引与备份后写入消失，再恢复原配置和客户端；失败保留恢复 fault journal 并阻断其它模块，不能据 Pod 就绪假定数据一致 |
| `--mongodb-backup-clean` | 核验核心、资源 UID、请求指纹与配置恢复记录，限定回收备份夹具及其 PV；完整清理会先执行该边界 |
| `--mongodb-retention` | M25：复用已验证逻辑备份及其独立 TLS S3 夹具，通过源 config+kubecli 启用每分钟计划、count=2 和存储删除。至少三份真实定时备份分别达到 ready 并校验 SHA256，观察最早 CR/数据/元数据删除、两份 ready 保留；随后经产品入口停止计划并等待在途备份终态，恢复源配置，以实际保留备份恢复独立目标并验证 BSON/索引/用户/边界。最后按原 UID 清理本次所有定时 CR/对象和目标，原手工备份摘要保持。预期 `MONGODB_RETENTION_PASS`，最小反证为本入口；停止计划后不再声称继续执行 count 保留策略 |
| `--mongodb-retention-stop` | 仅在 M25 已证明不能继续且已保存首失败诊断时使用；不同步源码、不变更集群。验证 durable 父进程、唯一 retention 子树及进程启动身份后停止该场景，保留父门禁写出退出码与诊断；输出 `MONGODB_RETENTION_STOPPED`，随后必须运行原 journal 的 recovery，不能作为业务 PASS |
| `--mongodb-backup-refusal-stop` | M24 前置已证明不成立并保存首失败后使用；只通过内存传入的已验证进程控制路径，核验 durable 父进程、唯一 backup-refusal 子树及启动身份，停止该子树并保留父进程、租约持有者、首失败诊断及目标 journal。不同步源码、不操作集群；输出 `MONGODB_BACKUP_REFUSAL_STOPPED` 后等待 durable 非零终态，再由 `--mongodb-restore-clean` 恢复其目标边界。只有清理及健康复验通过才可另起完整 M24；不能作为验收 PASS |
| `--mongodb-rollback-stop` | M38 前置已证明不成立并保存调度、容量等首失败证据后使用；核验 durable 父进程、唯一 rollback 子树及进程启动身份，仅停止该子树。不同步源码、不操作集群，保留 journal；输出 `MONGODB_ROLLBACK_STOPPED` 后等待 durable 非零终态，再运行 `--mongodb-restore-clean` 限定回收目标并复验源健康。不能作为 M38 PASS |
| `--mongodb-stability-faults-stop` / `--mongodb-stability-stop` | M36 聚焦或完整场景的前置已被证据否定时使用；分别校验原 durable 模式、唯一场景子树和进程启动身份，只停止对应子树，不同步源码或变更集群。保存首诊断后等待 durable 非零终态，必须由 `--mongodb-stability-recover` 消费原节点、客户端及稳定性 journal 恢复现场；停止标志不是业务验收。Registry 重启恢复分别绑定性能 journal 或稳定性的逻辑备份与故障 journal，拒绝两者混用 |
| `--mongodb-retention-recover` | 仅消费原保留策略 journal、source/请求 UID、配置转换两端和故障归属；先通过产品入口停计划，观察 CR 中任务已禁用后才按 UID 取消本轮未完成请求；随后恢复原配置、限定清理目标/工作负载/定时制品并复验原手工备份和源健康。失败保留首失败与未恢复 journal；预期 `MONGODB_RETENTION_RECOVER_PASS`，最小反证为本入口，不替代 M25 的完整业务门禁 |
| `--mongodb-replica-scale` | M32 测试门禁：通过源 config+kubecli 执行三→五→三成员变更。只临时扩展已租约六节点中的两个 master，先验证五个真实 hostname/CSI 域，再逐成员校验 512 条业务数据、索引、投票配置和多数派数量；缩容前验证原三成员健康，缩回后保留原 PVC/凭据/配置，只核验并退役新增两卷。最后迁出临时 CSI 副本、恢复节点 taint/调度及主机服务/目录。预期 `MONGODB_REPLICA_SCALE_PASS` 与 scoped-clean，最小反证为本入口；失败保留首因与未完成恢复 journal |
| `--mongodb-replica-scale-recover` | 只消费原 run-ID、源 UID、客户端 UID 及配置两端。活跃阶段必须保留原客户端，完成三成员数据/卷复验和测试数据清理后才持久化客户端删除授权；该阶段中断可安全继续，不能凭当前同名对象补猜身份。原配置/卷/凭据每次重验，临时存储域恢复后才删除 journal。预期 `MONGODB_REPLICA_SCALE_RECOVER_PASS`；不签收 M32 业务扩缩容，最小反证为本入口 |
| `--mongodb-performance-client` / `--mongodb-performance` / `--mongodb-performance-recover` | M35 的异步客户端最窄分支、完整性能门禁和原 journal 恢复。固定 4096 条 1KiB payload；每个独立认证连接按四次 majority 读取、一次幂等 majority+j 写入循环，完整门禁测试 1/4/16 并发各 30 秒和四并发 180 秒真实 worker 重启。记录 ops/s、P50/P95/P99、错误/重试、逐连接确认账本和数据库/客户端 kubelet CPU、内存、卷及网络样本；源卷、凭据与配置保留。最窄分支仅运行 1/4/16 并发各三秒并输出 `MONGODB_PERFORMANCE_CLIENT_PASS`，不能签收 M35；完整通过才输出 `MONGODB_PERFORMANCE_PASS`。失败/中断按原创建 UID、源快照、核心与 boot-ID journal 限定恢复；未完成恢复保留 journal。最小反证为相应入口；现场仍待验收，测量包含客户端和复制 CSI 成本，不能作为生产吞吐保证 |
| `--mongodb-ownership-resources` / `--mongodb-ownership-resources-recover` | M40 的资源拒绝聚焦分支及按原 journal 的恢复：独立所有者创建外部 namespace、Service、NetworkPolicy 和另一 namespace 的暂停 CR。真实 config+kubecli 分别明确拒绝前三项，清理资格检查拒绝外部 CRD 消费者；隔离的授权 journal 验证 CRD UID 不符拒绝，不修改真实 CRD 或原授权文件。完整核对 UID、删除端点、namespace 内容及 source 快照后由独立所有者回收；身份模糊或外来资源出现即停止保留 journal。预期 `MONGODB_OWNERSHIP_RESOURCES_PASS` / `MONGODB_OWNERSHIP_RESOURCES_RECOVER_PASS`，最小反证为对应入口；不替代卷保护或 M41 |
| `--mongodb-ownership` | M40 完整模块，按顺序执行卷保护和资源拒绝两项；两项分别健康准入并限定清理，首失败立即停止，只有全部完成才输出 `MONGODB_OWNERSHIP_PASS`。尚不替代 M41 的完整成功/失败/中断清理验收 |
| `--mongodb-performance-node` | M35 节点故障的最窄分支，使用同一四并发 180 秒 majority+j 工作负载及真实 worker 重启，保留脱敏客户端首错误。中继只对原 workload/core/client/node/boot-ID journal 绑定的重启短暂等待，恢复原受控连接和精确 NAT 规则；节点恢复必须同时证明 Registry HTTP 可用，再释放故障 journal。成功输出 `MONGODB_PERFORMANCE_NODE_PASS`，不替代完整 M35 |
| `--mongodb-registry-recover` | 前一 durable 门禁退出后，先验证原核心身份，再回收原 Registry journal 限定的规则、socket 和监听器，重新建立并验证六节点端点。输出 `MONGODB_REGISTRY_RECOVER_PASS`，保留数据库、卷和备份；不允许在活动门禁中运行，不替代节点故障业务验证 |
| `--mongodb-cleanup-success` / `--mongodb-cleanup-failure` / `--mongodb-cleanup` / `--mongodb-cleanup-recover` | M41 最窄成功/主动失败分支、完整三出口模块和原 journal 恢复。每个子进程启动前，最多 180 秒等待三个 worker 域的 CSI 容量公告达到原卷请求值；逐域数值可见，超时或错误拓扑立即阻断，不能降低卷大小。复用已验收逻辑备份的独立目标，通过 config+kubecli 安装真实三成员数据库并写入确认数据；完整模块分别测试成功、主动失败和父进程发送 SIGTERM。子进程须返回精确预期退出码、业务标记和目标客户端/CR/卷清理证据，父进程核对独立外部 ConfigMap、源数据图、节点调度配置、备份对象 SHA 和无故障残留。恢复拒绝仍存活的原子进程 PID；不终止未知进程、不删除来源不明对象。只有三出口均通过才输出 `MONGODB_CLEANUP_PASS`；最窄分支分别为 `MONGODB_CLEANUP_SUCCESS_PASS` 和 `MONGODB_CLEANUP_FAILURE_PASS`。现场待验收，最终完整实验室清理仍由 M43 执行 |
| `--mongodb-ownership-pvc` | M40 的最窄产品修复验证：独立持有者创建并挂载 1Gi CSI 卷、写入校验内容，通过独立 config+kubecli 证明产品在新建 CR 前明确拒绝同名旧 PVC，复验卷完整对象及原内容未变；再由独立持有者核验全部 UID/claimRef 后回收 Pod、PVC/PV 和本地部署目录，原基线快照不变。预期 `MONGODB_OWNERSHIP_PVC_PASS`；不替代 namespace/release/CRD 等完整所有权模块，最小反证为本入口 |
| `--mongodb-ownership-pvc-recover` | 仅按原私有 journal 的核心 UID、run-ID、独立 Pod/PVC UID 和 PV claimRef 回收上述夹具；若意外生成了数据库 CR、资源被替换或卷被重绑，则停止并保留证据，不能删除卷掩盖产品错误。预期 `MONGODB_OWNERSHIP_PVC_RECOVER_PASS`；不作为 M40 业务验收 |
| `--mongodb-status` / `--mongodb-follow` | 只读 durable 状态和日志；状态为 running 或某模块通过都不表示交付完成 |
| `--mongodb-clean` | 先核验 owner、核心 UID、节点和故障 journal，再按产品、CSI、核心的依赖顺序限定回收；API 不可达不能自动当成清洁现场 |
| `--mongodb-cleanup-qualification` | 清理门禁的只读资格分支，尚不签收 M40/M41。先由创建时 namespace UID 或已保存备份的原 source UID 绑定根身份；检查所有可列举的命名空间资源、UID ownerReference 图、PVC/PV 完整绑定、CRD UID/schema 及全部命名空间消费者，未知对象拒绝且不删除。备份、恢复及分片夹具须先由各自入口回收；该入口只更新私有身份 journal，集群不变。预期 `MONGODB_CLEANUP_QUALIFICATION_PASS`，最小反证为本入口；正式清理仍需失败/中断和最终回收证据 |

M34 提供 `--mongodb-observability-scrape`、`--mongodb-observability-lag`、`--mongodb-observability-capacity` 和 `--mongodb-observability-backup` 最窄分支。第一项通过 config+kubecli 临时将监控 namespace 设为原自有 namespace，使用已发布 Prometheus 3.13.1 / KSM 2.18.0 校验三个真实 Exporter、成员健康/位点、三块卷及 Backup CR 完成时间；monitor 账号角色须与固定上游四个角色精确一致，允许诊断和 explainRole 的跨库读取（含应用及 PBM 配置），应用写入须以 mongosh 原生错误码 13 拒绝。读取授权探针使用不可能命中的 `_id` 条件，不返回存储内容；异常允许写入时仍清理本 run 集合。此账号并非只读指标身份，其凭据必须按客户手册保护。KSM 只读 CRD 发现及本 namespace Backup，容量仅允许三个 worker 的 `nodes/metrics`，不授予 `nodes/proxy`。后三项分别验证真实单成员复制隔离、保留至少 1GiB 的磁盘压力及独立 TLS 对象存储中的新产品备份；Prometheus 必须证明对应告警触发和恢复。短实验阈值不作为生产 SLO。

`--mongodb-rollback` 验证 M38 的保守自动化边界：复用已验收逻辑备份建立独立新卷目标，读取真实 FCV=8.0 且无转换状态，通过目标 config+kubecli 分别请求数据库 8.0.32-14→8.0.26-11 和 Operator 1.23.1→1.23.0，要求精确降级拒绝、非零退出，且目标 CR/Pod/PVC/Secret、控制器及业务标记保持。随后恢复原配置，以外部 backupSource 在该兼容目标执行真实逻辑恢复，校验 BSON、索引、用户和备份边界，限定回收目标，保留原备份与源。预期 `MONGODB_ROLLBACK_PASS` 与 scoped-clean；最小反证为此入口。它不宣称旧二进制能读取新卷，也不把 FCV 未提升作为降级许可；现场验收仍待完成。

`--mongodb-observability` 顺序执行全部 M34 断言，只有完整业务、限定清理和原备份 SHA256 复验均通过才输出 `MONGODB_OBSERVABILITY_PASS`。`--mongodb-observability-recover` 只消费原核心/run-ID/配置两端、客户端 UID 和监控资源 UID，先恢复故障，再回收监控 RBAC/进程/临时备份，恢复产品配置并复验原卷/凭据/备份。替代资源或未知所有者拒绝删除。最窄分支只输出对应聚焦 marker，不签收 M34；上述新增入口仍待现场验收。

`--mongodb-upgrade` 归属 M37，必须在干净的 runner 租用环境执行；存在数据库 namespace 或任何 MongoDB CRD 时拒绝，不把新 CRD 上的旧镜像视为升级。原安装入口显式构建 Operator 1.23.0 / Chart 1.23.1、PSMDB 8.0.26-11、Fluent Bit 5.0.9-1，核对真实旧 schema，生成并校验升级前逻辑备份。四并发、3600 秒 majority+journal 账本负载覆盖两次 config+kubecli 变更：先升级 Operator/CRD，再升级数据库和 Fluent Bit；每两秒采样要求至少两个 Ready 成员，核对 CRD UID 保留且 schema 前进、三成员实际二进制/镜像、FCV=8.0、全部 Pod 更新、数据/索引及卷/凭据身份。随后真实恢复升级前备份，并生成和独立恢复新版本备份，最后回收旧版本专项环境的数据库、卷及 CRD。失败保留升级 journal 与首诊断；节点最终清理由 M43 完成。本入口为该固定版本对的最小反证；专项验收以当前 M37 证据为准，不能替代最终干净全量 M43。

旧版安装已通过产品健康、但安装验证客户端启动失败时，使用 `--mongodb-install-business` 先复验产品、按原 UID 回收客户端，再通过同一产品入口和真实客户端完成安装验证。客户端首次拉取允许最多 600 秒；超时仍失败，不接受 Pending 作为业务结果。安装失败收尾同样先验证并回收客户端，身份拒绝时停止后续整环境清理。只有该恢复入口通过且原核心、旧版配置、实际旧 CRD、健康与故障残留均满足原升级门禁时，才运行 `--mongodb-upgrade-existing`：它复验 CSI、固定制品和既有健康安装，执行完整原升级模块。该入口不创建或替换旧基线，不签收干净全量；最终 `--mongodb-only` 仍从干净现场调用原 `--mongodb-upgrade`。

`--mongodb-upgrade-recover` 按原 journal 分两种范围：尚未应用的 Operator 阶段要求原核心、源卷/凭据、全部旧 CRD UID/schema、旧配置和实际旧二进制不变，校验备份后恢复原配置；已完成两阶段变更但后续断言失败的 server 阶段要求新配置、新 CRD schema 与原 UID、实际新二进制、原源卷/凭据全部匹配。后者保留新版本，校验并回收备份后，仅将原 cleanup receipt 的固定旧 schema 摘要更新为已证明的新摘要，再由既有归属图删除该专项数据库、卷和 CRD，保留核心与 CSI。两条路径均拒绝外部配置或身份漂移，不执行原地降级，不签收 M37；预期 `MONGODB_UPGRADE_RECOVER_PASS`。前者恢复后使用 `--mongodb-upgrade-existing`，后者产品范围清空后使用原 `--mongodb-upgrade` 重新构建旧版起点。账本核验以 256 条游标批次逐条检查全部确认序列，不能用 `toArray()` 全量物化长时负载记录，也不能减少核验数量。

`--mongodb-full-artifacts` 从已验证干净的六节点起点执行全量 wrapper 的制品阶段，用于聚焦验证实际模块输出、退出码、失败标志和成功标志解析；不会创建集群。成功标志可为独立一行，或标志后接空格与结果字段；相似前缀、嵌入文本和非零退出仍失败。预期 `MONGODB_FULL_ARTIFACTS_PASS`、durable rc=0、零失败及专项目录不存在；该聚焦标志不能签收 M43。通过后才允许 `--mongodb-only` 完整执行原计划。

安装业务验证完成后、发布 healthy 标志之前，runner 绑定本轮 namespace、源库、CRD 与产品根对象 UID。仅当前轮已完成 product 清理、且记录了不同于旧 receipt 的新 namespace 创建 UID 时，允许替换已退役 receipt；标签不能替代创建凭据。若升级已处于 `post-upgrade-backup-restored`、备份与故障 journal 均已回收，`--mongodb-upgrade-recover` 会复验精确新配置、原源库、固定新 CRD 和实际新二进制/FCV后完成专项清理，保留核心/CSI，不重建已删除的备份 journal，也不签收完整 M37。

`--mongodb-stability-faults` 是 M36 最窄故障分支，`--mongodb-stability` 是完整模块，`--mongodb-stability-recover` 仅恢复原 journal。副本集和独立双 shard 目标各执行四并发、4096 条 1KiB 数据、80/20 读写负载，逐条复核 majority+journal 确认账本；最窄分支每拓扑 180 秒，完整模块每拓扑 1800 秒。稳定性负载每 10 秒最多记录一次 `MONGODB_CLIENT_MEMORY`，包含进程 RSS、V8 堆、外部内存、样本数、重试数及实际 Node 版本，不输出 URI、命令内容或凭据；失败时随原客户端首诊断保留。该只读诊断不调整客户端预算或业务断言。稳定性负载在创建连接前使用实际 Node 24.18.1 的 `tls.setDefaultCACertificates`，将当前独立 shell 的默认信任库限定为本目标挂载的唯一 CA，并移除该负载连接 URI 中同一 `tlsCAFile` 参数，复用 Node 根证书存储；TLS、主机名及凭据校验保留。此测试客户端路径针对 Node #66002 所述的 TLS 重连原生上下文回收问题，常规客户端和生产声明不变；缺失、变化或重复 CA 参数拒绝启动。负载期间依次按 UID 重建数据库 Pod、Operator Pod，再重启一台白名单 worker，要求原控制器、卷、凭据和 CR 保持。分片数据跨 rs0/rs1，账本采用 hashed 分片。失败或中断先恢复节点和原控制器，再按原客户端 UID 停止负载、清理本 run 数据和独立目标，身份漂移拒绝修改。只有两种拓扑及备份 SHA256 复验完成才输出 `MONGODB_STABILITY_PASS`；聚焦只输出 `MONGODB_STABILITY_FAULTS_PASS`，不签收完整 M36。最小反证为各自入口，验收状态以当前矩阵及对应完整模块证据为准。

MongoDB 静态门禁使用固定 `promtool 3.13.1` 检查采集配置、规则语法与告警触发/恢复，可通过 `MONGODB_PROMTOOL` 指向本地二进制。工具取自已签收的 Prometheus 镜像：先验证 AMD64 manifest 与 layer 摘要，再提取工具；禁止使用未校验下载。静态模拟采样只证明规则语义，不构成现场证据。

每个新增或改变的断言先过本地契约，再运行对应最窄分支。失败保留首失败、
durable 状态及脱敏事件/控制器日志，按所属层定位；健康核心可以经身份、
服务、数据和无故障残留核验后复用，不因业务断言失败直接重装。
模块必须同时具备业务 marker、scoped clean、durable `rc=0` 和零失败 marker。
尚未实现的入口明确拒绝执行；不得将已有聚焦分支合并宣称完整验收。
M24 的错误凭据和服务不可达控制允许两种实际拒绝边界：请求存在时必须有匹配原生原因的失败终态；Operator 1.23.1 因 PBM 存储探测失败使集群停在 initializing、产品就绪检查提前拒绝时，必须同时证明产品非零退出、请求未创建、目标 UID/实际存储配置一致，以及本次执行时间内绑定该目标和真实成员的原生 PBM 错误。普通就绪超时、旧日志、其它集群错误及错误因果层均不能签收。
最终 `--mongodb-only` 须完整覆盖 43 项、从干净现场运行、执行最终限定清理并
获得 `LAB_CLEAN_VERIFY_PASS` 后才能签收。
该入口先要求其它 42 项完整通过、证据存在及当前静态输入摘要匹配，再只读验证六机清洁。父租约内按固定计划串行运行全部现场模块：真实旧版升级完成后清理核心，再从干净环境安装当前版本；全部业务、故障、数据保护、分片、容量、监控、负载、幂等及三出口清理完成后，执行最终产品/CSI/核心清理。模块标准输入固定为 `/dev/null`，避免 SSH 或客户端读走父循环的剩余计划；终态另核对实际执行步数，计划提前结束必须失败。每段保留日志摘要、退出码、失败数和精确成功标记，首个未满足门禁立即停止，不继续后续模块。只有部署目录消失且最终 `LAB_CLEAN_VERIFY_PASS` 成立才输出 `MONGODB_FULL_PASS`；前置聚焦证据不替代本轮完整运行。

## Redis 独立交付分路

Redis 的验收权威为 `redis-test-matrix.yaml`，产品入口是目标部署目录的
`config.yml` 与 `kubecli setup redis-gate 07 --tags redis`。Cluster 使用独立部署
目录 `redis-cluster-gate`，保留源 Sentinel 数据边界。不得借用其它中间件或核心
矩阵的 PASS，也不得用安装 smoke 签收 24 项完整验收。

先执行 `--redis-static-only`，再执行只读 `--redis-artifacts`。镜像必须已经具备
TalkEdu/Docker Hub 双架构发布证据，现场不能临时发现新的公共镜像源。
状态、日志、退出码和失败诊断由总运行器的 durable wrapper 管理；聚焦模块
只复用身份、服务、数据和无故障残留均已核验的 runner 基线。

| 固定入口 | 证据边界 |
| --- | --- |
| `--redis-full-clean-verify` | 完整门禁父租约、durable 子阶段与只读清理验证的最窄资格；不安装环境、不执行业务模块、不签收完整回归 |
| `--redis-cleanup-qualification` | 实际清理 CLI 的正常、外部资源拒绝和真实中断后重入；限定 UUID 命名空间，保留外部 canary，随后回收全部测试材料 |
| `--redis-platform` | 六节点身份、三个可调度故障域、时钟偏差、Pod 内 DNS，以及独立 CSI PVC 读写/扩容/保留与回收 |
| `--redis-install-only` | 专属核心/CSI/Redis 安装；保留健康基线，不是完整交付 |
| `--redis-sentinel-data` / `--redis-sentinel-security` | TLS 业务发现、读写、TTL，以及错误凭据/CA/ACL 拒绝 |
| `--redis-sentinel-failure` / `--redis-sentinel-quorum` | 单主切换、仲裁拒绝与恢复 |
| `--redis-maintenance-rolling` / `--redis-maintenance-drain` | 两组 StatefulSet 顺序滚动、PDB 与 Eviction API drain；临时借用已租约 master 134 补齐故障域，恢复 taint/调度、CSI、主机服务和限定数据目录 |
| `--redis-tls-rotation` / `--redis-password-rotation` / `--redis-idempotence` | 维护项的部分证据，不单独签收整个维护项 |
| `--redis-persistence` / `--redis-backup` | AOF 部分证据；产品快照与独立 RDB 恢复分别核验 |
| `--redis-capacity-lag` / `--redis-capacity` | 先最窄复制滞后，再完整内存、磁盘和 PVC 扩容 |
| `--redis-observability` / `--redis-performance` | 实际抓取/告警与固定负载的业务、延迟和资源证据 |
| `--redis-stability-faults` / `--redis-stability` | 先限定 Pod、两个 Operator 进程和白名单 worker 真重启；再至少 30 分钟 Sentinel/Cluster 持续业务、持久化与数据/PVC 保留，不以故障准备项代替 soak |
| `--redis-upgrade-prepare` / `--redis-upgrade-sentinel` | Sentinel-only 固定旧夹具准备，以及 config+kubecli 的 0.26→0.27 CRD/控制器与 8.10.1→8.10.2 滚动升级和限定版本对回退；结束保留当前固定版本 |
| `--redis-upgrade-cluster` | 固定 3 主 6 副本、受保护控制器下的 8.10.1↔8.10.2 修订镜像滚动迁移；逐成员版本、绝对 TTL、槽位、持久化和 PVC 保留，结束恢复当前镜像 |
| `--redis-crd-upgrade` | 显式产品请求更新固定官方 Chart 的四项 CRD，拒绝共享消费者并保存 schema 备份，核验 Sentinel/Cluster 数据与 PVC；这是最窄更新路径，不替代实际版本升级/回退验收 |
| `--redis-cluster-install` / `--redis-cluster-data` | 独立 Cluster、真实复制图、完整槽位、MOVED/ASK/事务边界 |
| `--redis-cluster-snapshot` | 独立一次性产品请求目录，三主 RDB 的 CRC/SHA256、完整无重叠槽范围及源 PVC 保留；不替代 Cluster 全拓扑灾备恢复 |
| `--redis-cluster-primary-fault` / `--redis-cluster-failure` | 先主故障夹具，再副本重建、多数派拒写和完整恢复 |
| `--redis-cluster-rebalance-seed` / `--redis-cluster-rebalance` | 先种子、确认、TTL 与清理，再产品驱动的 3→4→3 |
| `--redis-cluster-repair` | 限定旧故障测试键恢复，不签收任何故障或扩缩容项 |
| `--redis-cluster-clean` | 核验身份与故障 journal 后，回收失败的独立 Cluster；可限定恢复尚未注入故障的 stability seed 阶段，保留源 Sentinel 业务、PVC 和共享依赖，不签收安装或完整清理项 |
| `--redis-cluster-config` | 通过产品入口验证两角色启动配置、真实挂载和有效参数，以及变更后的数据/PVC 保留；不替代完整故障、扩缩与交付验收 |
| `--redis-cluster-probe` / `--redis-dns-probe` | 只读资源、真实成员与 DNS 诊断，不是业务 PASS |

上述入口都通过 `bash tests/run_enterprise_regression.sh MODE </dev/null` 调用。
不得直接启动远端 helper，或以临时 SSH/Helm 序列绕过租约、预检和证据采集。
Sentinel 密码轮换须先于共享认证 Secret 的独立 Cluster 安装；轮换门禁在修改前
拒绝其它 Redis 声明共享待轮换 Secret。Cluster 使用独立 CA，不能据此假定密码和 ACL 也独立。
Cluster 进程故障必须从所属白名单节点的宿主 PID namespace 发信号，核验精确
容器与进程身份以及实际 `T` 状态；容器内 PID 1 信号返回成功不足以证明暂停。

每个新断言先过对应确定性契约，再运行最窄分支；该分支通过后才能执行完整
模块。模块 PASS 需要业务标志、scoped clean、durable `rc=0` 和零失败标志。
最终 `--redis-only` 还必须重新建立干净现场、覆盖所有矩阵项、完成限定回收并
取得 `REDIS_CLEAN_VERIFY_PASS` 与 `LAB_CLEAN_VERIFY_PASS`。若完整场景实现不齐，
运行器必须明确拒绝交付，不将已完成模块的合集包装为完整回归。

## Elasticsearch 独立交付分路

验收权威为 `elasticsearch-test-matrix.yaml`：59 项、18 个逻辑阶段。业务基线为
ECK 3.5.0、ES/Kibana 9.5.6、Kubernetes 1.33.6，保留 EFK 9.5.1。
产品行为必须经过部署目录的 `config.yml` 与
`kubecli setup elasticsearch-gate 07 --tags elasticsearch`，不能用直接创建 CR
代替产品安装证据。以下命令由交付操作人在开发机仓库根目录执行，使用现有
`.venv`、固定 Helm/Node 工具及 BatchMode 密钥；不直接启动远端 helper。

```bash
set -euo pipefail
bash tests/run_enterprise_regression.sh --elasticsearch-contract
bash tests/run_enterprise_regression.sh --elasticsearch-artifacts
```

合同入口只做本地静态检查；制品入口只读核验当前消费镜像与发布证据、双架构
摘要、构建输入及离线 ECK 清单。正式发布 receipt 缺失、发布尚未成功或 manifest
身份变化即停止，禁止在现场临时发现镜像、覆盖正式 tag 或绕过此门禁。

现场启动由运行器持有 `.122.2` 的源池和控制源码租约，以及 `.47.130` 的
Ceph 优先池/控制租约；同一 run ID 的源 durable 结束与 guardian 结束均确认后
才释放。源为 `.122.243/.246/.217/.210/.216/.193/.212` 七台，前三台承担
实验室专用 ES master 故障域；DR 仅用 `.122.135/.40/.72`。bootstrap 只接受
已绑定 UID、版本与空业务边界的旧六节点集群，或经清洁验证的七台空主机；
拒绝外来 namespace、PV/PVC、身份变化及活动任务。控制机 `.122.2` 的 AIO、
Registry 与无关制品受保护，不能对控制机执行 wipe/prune。存储夹具每机仅创建
自有 32 GiB root loop 文件系统，不格式化裸盘；创建前要求 backing 文件系统
至少空闲 40 GiB，挂载后容量至少 30 GiB。两拓扑共存、standard 扩至四个 data、
全部 data 卷扩至 6 GiB 时，三副本调度需求为 144 GiB；按产品固定的 10% 预留、
100% 超配，六块 30 GiB 文件系统仍提供 162 GiB 调度容量。该预算用于 N−1
容量准入，不宣称所有卷写满后仍有相同余量；实际数据与 snapshot 占用仍受
25% 最低可用空间门禁约束。loop 结果不代表物理盘性能。

| 固定入口（均带 `--elasticsearch-` 前缀） | 前置与证据边界 |
| --- | --- |
| `inventory` / `inventory-secondary` | 只读发现容量和归属，不取得租约、不构成准入或 PASS |
| `admission` / `bootstrap` | 前者只检查源准入；后者在租约下退役已证明空业务的旧源，经产品入口新建固定 K8s，保留自有健康基线 |
| `registry-recover` / `bootstrap-resume` | 仅修复已证明缺失的控制机 Docker 网桥：拒绝其它运行容器或不完整目录清单，保留 Registry 容器/镜像/数据目录、目录清单与 AIO 身份；续跑仅接受原准入账本、硬件、配置/清单及仍为空的节点，跳过重复 wipe，从产品 download/setup 继续 |
| `storage` → `csi-probe` | 产品安装 Longhorn 1.12.1 V1；在 ES 放数据前验证 fsync/hash、同卷跨节点重挂与 1→2 GiB 在线扩容，回收探针 namespace/PV |
| `standard` / `compact` | 先完成 CSI；分别验证六成员专用角色与三成员混合角色、TLS、业务、幂等和身份保留 |
| `business` / `lifecycle` | 已验收两拓扑；数据模型、查询、索引迁移、ILM 和同版本 SmartCN |
| `security-identity` / `security-certificates` / `security-network` / `security-s3` | 分别接受 ES-24/25/27、ES-26、ES-29、ES-28；复用已核验两拓扑，每个入口独立完成业务与限定清理。`security` 顺序组合四项，前置失败立即停止，不作为局部修复的默认入口 |
| `security-certificate-restore` | 先核对源集群、两命名空间、当前证书引用、精确测试 Secret 及原业务账本，再经产品入口恢复原证书；恢复验证通过后才清理对应 UID 的测试 Secret。允许正常 Pod 滚动，保护 CR/PVC/凭据及确认数据；不重跑身份模块，不签署完整证书轮换 PASS |
| `faults` / `maintenance` | 已验收业务；确认账本下的故障/恢复、扩容、drain、卷扩容与磁盘水位，首失败停止依赖路径 |
| `backup-source` → `backup-refusal` → `backup-restore` → `backup-features` | HTTPS S3、真实 JVM CA、verify/analysis、拒绝路径、独立恢复和 feature states；四个入口分别对应最窄验收范围，`backup` 顺序执行完整链 |
| `dr-bootstrap` / `dr` | 源 `bootstrap`、`storage` 完成后，前者并行执行源 CSI 探针和独立目标的准入／平台／存储准备，两支分别保留日志与 durable 状态，成功保留健康前置；后者验证 readonly repository、切换/回切及业务对账，不能借用源 PVC/管理身份 |
| `observability` / `rally` / `soak` | 真实采样/告警、离线固定基准、两拓扑各至少 1800 秒含故障负载；`performance` 组合后两项 |
| `upgrade` / `coexistence` | 固定旧版经产品入口升级、拒绝降级、兼容恢复；唯一 ECK 下业务与 EFK/其它消费者隔离。保留共存夹具时先核对原 namespace、卷、凭据、哨兵/快照及两套业务账本，仅补反向退役与重入，不签署矩阵项或失败父运行 |
| `coexistence-restores` | 从已冻结失败输入核对并接续独立恢复探针，持久记录恢复、单边升级、业务退役重入及 EFK 退役重入的完成位置；核验原归属、哨兵、快照与账本后只补未完成场景，不重做已验证的仓库拒绝及共享控制器故障，不签矩阵项或失败父运行 |
| `coexistence-isolation` | 仅在原共存哨兵/快照及两套业务仍健康、namespace 归属匹配且无故障残留时，补仓库别名/请求重放拒绝、共享控制器恢复、独立恢复及单边升级/退役重入；限定清理，不重装已完成的前置、不补签失败父运行或完整矩阵项，最终完整共存仍由干净全量验收 |
| `cleanup-probes` / `logging` | 正常/失败/中断的客户端限定回收；共享 ECK 改动影响的三条 logging 路线使用其真实原门禁与清理，不消费历史 PASS |
| `restore-faults` / `logging-cleanup` / `cleanup` | 原 journal 的归属与身份校验后恢复故障/日志边界；最终 cleanup 原生退役产品、CSI、源/DR 和自有 loop/sysctl，拒绝未知残留 |
| `status` / `follow` | durable 状态、实时日志和矩阵计数；连接失败或进程仍在不构成成功 |
| `documentation` | 本地只读核对三份正式手册的既有作者检查、版本、内容摘要和无阻塞缺陷，独立接受 ES-58；不操作现场、不补签客户验收 |
| `only` | ES-01～58 全部具备当前 PASS 证据后，执行 ES-59 的一次干净完整回归；19 个验收子模块与独立 DR 准备旁路映射 18 个内部步骤 |

例如完成静态与制品门禁后，依次执行
`bash tests/run_enterprise_regression.sh --elasticsearch-bootstrap </dev/null`、
`--elasticsearch-storage`、`--elasticsearch-csi-probe`，再推进两拓扑和其依赖模块。
运行器负责同步、监督、心跳、首失败诊断与证据冻结；原始日志、退出文件和
finalized 文件归档在 `logs/elasticsearch-cases/`。模块只有业务 marker、限定
清理边界、durable 双 0 及零失败同时成立才能签收声明范围。失败先分类，
修门禁先补精确合同，仅重跑所属最窄分支；保留原失败和健康独立前置，不直接
删除 journal 或改矩阵为 pass。`cleanup` 也须经固定入口取得租约，拒绝时保留
现场诊断，不使用手工删 namespace、卷或 loop 绕过归属判断。

并行编排先以 `dr-bootstrap` 聚焦资格验证：源与目标主机集合必须不相交，
各进程显式消费所属集群 kubeconfig，外层运行器恢复原控制端上下文。两支均
使用现有产品入口和独立 durable 文件；一支失败停止自身后继，另一支完成
独立工作并保留真实结果。资格通过后，完整回归在源核心就绪时立即准备 DR，
同时推进源存储、CSI、两拓扑及后续源侧模块；进入备份恢复前必须等 DR 成功。
共享 `config.yml`、ECK、卷和故障状态的源侧变更保持串行，两个 SOAK 拓扑
沿已有隔离合同并行；不为提高并发数重复有效测试或借用外来集群。逻辑阶段
仍为 18，DR 准备不额外签收业务项，任一分支失败都不能签署完整交付。

新证据按矩阵项绑定实际模块依赖。共享 Python helper 的绑定包含选定入口、
本文件调用闭包、模块初始化和 CLI 分派；动态跨文件消费者由组件显式映射，
未知边界不自动缩减。原始字节来源与源码快照另存于本次日志目录，保留历史身份，
不因无关函数或单元测试改动而作废现场业务证据。全量记录也按各项的模块依赖
判断适用性；合同检查自身仍覆盖全部相关测试。旧记录缺少可验证的原始源码时，
先分析其证据缺口，不能猜测等价、修改旧 receipt 或为恢复计数重签 PASS。
运行中可执行 `bash tests/run_enterprise_regression.sh --elasticsearch-evidence-recover`
只读收取已结束子模块的原始日志、冻结输入和 durable 文件并逐项更新矩阵；
它不等待父运行结束，不修改现场或父退出状态，也不接受未结束、失败或缺失子项的模块。

`bash tests/run_enterprise_regression.sh --elasticsearch-upgrade-platform </dev/null`
只验证 ES-50：在独立 DR 目标准备已声明的旧版本、经过真实平台入口完成准入拒绝、
ECK 升级及并发 Operator 故障恢复，核对两边数据与身份并回收自有 EFK 夹具。
它复用健康源集群及 DR 的 Kubernetes/存储前置，不执行 Stack 升级和旧版恢复，
也不签 ES-51/52。平台完成判定须看到目标 Operator 进程对所有消费者的协调完成
记录及当前健康状态；仅 Pod Ready 或旧绿色状态不能完成平台账本。

进度格式、计数定义、校验失败及最近运行结果的表达，统一遵守
[主仓执行效率契约的矩阵汇报条款](../AGENTS.md#test-engineering-and-execution-efficiency-contract)。
ES 的矩阵是 `tests/elasticsearch-test-matrix.yaml`，分母为 59 项、18 个能力组。
汇报前执行下列只读入口，成功后使用其总数和逐组 ID；需要结构化数据时追加 `--json`：

```bash
.venv/bin/python tests/helpers/validate_elasticsearch_matrix.py tests/elasticsearch-test-matrix.yaml --status
```

该入口会先核验矩阵和绑定证据，失败时只输出拒绝原因并返回非零，不生成有效进度总览。
它不收取新证据、不修改矩阵，也不提供现场子项的运行状态；后者须从对应固定 runner 的
durable 状态、原始日志或独立路线收据核对后补充。尤其 ES-55 的三条日志路线属于同一父项，
其入口是 `logging`，能力组仍是矩阵中的 `coexistence`；部分路线通过不等于 ES-55 PASS。

ES-01～58 全部具备当前 PASS 证据后，才执行 ES-59：
`bash tests/run_enterprise_regression.sh --elasticsearch-only </dev/null`。
固定入口在任何现场变更前逐项核验前 58 项；缺项、pending、失败或失效证据均拒绝。
ES-59 运行期间另报其 run ID 和内部步骤进度，矩阵原有 PASS 不因开始新运行而清零；
最终回归失败保留其真实结果，仅按实际影响处理相关矩阵项。
ES-58 用 `bash tests/run_enterprise_regression.sh --elasticsearch-documentation`
独立验收既有文档检查；文档未变时不重新研究、重审正文或搭建现场。
必须当前 59/59、18/18、durable 双 0、零失败、`ELASTICSEARCH_FULL_GATE_PASS`
和 `LAB_CLEAN_VERIFY_PASS` 同时成立；完整阶段进度、官方 GA、已完成聚焦证据
及文档作者检查都不能代替这一出口，也不能代替客户签收。

`tests/` is the executable delivery knowledge base for kubeauto. It captures the reusable lessons from the Cursor-era regression work and the 2026-07-25 through 2026-07-29 Codex full-chain run. The source of truth is executable evidence, not a narrative claim or an old log.

## Acceptance contract

A delivery run is complete only when all of the following are true:

- the affected unit and contract tests pass;
- the current live scenario emits its documented success marker;
- the durable wrapper records `rc=0`;
- the supervisor reports zero failure markers;
- `enterprise-test-matrix.yaml` has no affected pending or failed item;
- the six-repository compatibility tests pass;
- final cleanup emits `LAB_CLEAN_VERIFY_PASS`.

Before any enterprise gate starts, the runner executes
`helpers/validate-test-matrix.py`. This parses the YAML and recalculates every
Tier total/status count and the overall `N/N` declaration from the item
details. It rejects stale summaries, duplicate IDs, missing/unknown statuses,
or any non-pass item; a delivery gate cannot emit PASS while this check fails.

Do not mark a matrix item pass from historical evidence. A Pod merely existing or showing `Running` is insufficient when the component has multiple containers, asynchronous custom resources, readiness conditions, data-path checks, or a required business read/write operation.

For a newly created Kubernetes cluster, Node/Pod `Ready` is also insufficient.
The fixed live gate must deploy an application, satisfy its readiness probe,
resolve a Service through cluster DNS, complete an HTTP write/read through the
ClusterIP data path, and emit `KUBERNETES_PRODUCTION_SMOKE_PASS`.  The reusable
implementation is `helpers/kubernetes-production-smoke.sh`.

## Laboratory topology

| Role | Address | Account | Contract |
| --- | --- | --- | --- |
| Development only | 192.168.47.129 | brinnatt | Edit and sync only; never a test node |
| Jumper | 192.168.47.130 | root | Rocky 8.10, Ansible 2.16 control path |
| Workers | 192.168.47.131-133 | root | Rocky 8.10, small memory, reserved disabled |
| Masters | 192.168.47.134-136 | root | Rocky 8.10, small memory, reserved disabled |
| Debian node | 192.168.47.128 | brinnatt + sudo | Debian compatibility |
| Reserved/runtime | 192.168.47.137 | root | Rocky 8.10, 8 CPU / about 16 GiB currently provisioned; live 2 CPU + 4 GiB reservation gate |
| AIO/control/reserved | 192.168.47.138 | ubuntu + sudo | Ubuntu AIO and primary regression control |
| Ceph OS qualification | 192.168.47.145 | root | Rocky Linux 10.2; Ceph qualification only |
| Ceph OS qualification | 192.168.47.146 | ly + sudo | Ubuntu 24.04; Ceph qualification only |
| Ceph OS qualification | 192.168.47.147 | ly + sudo | Ubuntu 26.04; Ceph qualification only |
| Ansible compatibility | 192.168.47.141 | root | Anolis OS 23.3 clean-snapshot control path |
| Ansible compatibility | 192.168.47.142 | root | openEuler 22.03 LTS-SP4 clean-snapshot control path |
| Ansible compatibility | 192.168.47.143 | root | openSUSE Leap 16.0 clean-snapshot control path |
| Ceph-priority shared | 192.168.122.135, .40, .72 | root | ceph-01 through ceph-03; three additional test disks per host |
| Ceph-priority shared | 192.168.122.212, .165, .238 | root | mceph-01 through mceph-03; three additional test disks per host |

The current external load-balancer VIP is `192.168.47.250:8443`. Address 140
belongs to a retired kubeauto lab layout and must not appear in active
regression configuration. Address 147 is reserved exclusively for the Ceph
Ubuntu 26.04 qualification profile and must not be reused by the core or
other middleware topology. Addresses 141-143 are dedicated compatibility
controls and must not be silently reused as ordinary Kubernetes nodes. A
negative unit-test fixture may contain a retired address only to prove that it
is removed.

The six Ceph-priority hosts are shared, lease-controlled capacity rather than a
permanent exclusive allocation. Ceph tests get first use because each host has
three additional disks intended for destructive storage scenarios. Every
runner must resolve and record the exact disk serial/WWN allowlist before any
write, and must release the host lease only after scoped cleanup verification.

The production reserved-resource sizing baseline remains 16 CPU / 32 GiB. The
smaller 137 lab host validates the effective 2 CPU + 4 GiB Allocatable delta and
cgroup placement; it must not be reported as proof that the lab itself has 32 GiB.

SSH keys are the primary authentication path. The scripts use `BatchMode=yes` for SSH/SCP/rsync. A snapshot restore may remove the installed key on 141-143; the fixed runner invokes `tests/helpers/lab-ssh-bootstrap.sh`, which accepts the already-authorized credential only through the runtime `LAB_SSH_PASSWORD` environment variable and then returns to `BatchMode=yes`. Do not copy that credential into files, command output or retained logs.

## One top-level runner

Run all lab work from the development host through:

```bash
bash tests/run_enterprise_regression.sh --all-delivery
```

`--all-delivery` is the unattended final-signoff sequence. It composes the
already focused and cleanup-safe modes in this order: jumper, nerdctl, Docker,
Kubernetes patch upgrade, the delivery-gaps full chain (which includes the
authoritative core topology regression), the Rocky 8 customer-binary build,
cross-distribution Ansible gates, and the nine Tier3 frozen-tool gates. A failed
sub-mode stops the sequence after that mode's mandatory cleanup.

Useful fixed modes:

```bash
bash tests/run_enterprise_regression.sh --status
bash tests/run_enterprise_regression.sh --follow
bash tests/run_enterprise_regression.sh
bash tests/run_enterprise_regression.sh --aio-only
bash tests/run_enterprise_regression.sh --jumper-only
bash tests/run_enterprise_regression.sh --nerdctl-only
bash tests/run_enterprise_regression.sh --docker-only
bash tests/run_enterprise_regression.sh --registry-reboot-only
bash tests/run_enterprise_regression.sh --upgrade-only
bash tests/run_enterprise_regression.sh --gaps-only
bash tests/run_enterprise_regression.sh --diagnose-gaps-last
bash tests/run_enterprise_regression.sh --ansible-os-probe
bash tests/run_enterprise_regression.sh --ansible-anolis-probe
bash tests/run_enterprise_regression.sh --ansible-anolis-container-probe
bash tests/run_enterprise_regression.sh --ansible-os-only
bash tests/run_enterprise_regression.sh --build-rocky8-kubecli
bash tests/run_enterprise_regression.sh --tier3-tools-only
bash tests/run_enterprise_regression.sh --mysql-only
bash tests/run_enterprise_regression.sh --mysql-status
bash tests/run_enterprise_regression.sh --mysql-follow
bash tests/run_enterprise_regression.sh --kafka-only
bash tests/run_enterprise_regression.sh --kafka-status
bash tests/run_enterprise_regression.sh --kafka-follow
bash tests/run_enterprise_regression.sh --kafka-clean-only
bash tests/run_enterprise_regression.sh --ceph-probe
bash tests/run_enterprise_regression.sh --ceph-os-probe
bash tests/run_enterprise_regression.sh --ceph-qualification-network-only
bash tests/run_enterprise_regression.sh --ceph-prepare-hosts-only
bash tests/run_enterprise_regression.sh --ceph-os-qualification-legacy
bash tests/run_enterprise_regression.sh --ceph-os-qualification-modern
bash tests/run_enterprise_regression.sh --ceph-os-qualification-rocky9
bash tests/run_enterprise_regression.sh --ceph-lab-bootstrap
bash tests/run_enterprise_regression.sh --ceph-lab-python-bootstrap
bash tests/run_enterprise_regression.sh --ceph-lab-runtime-bootstrap
bash tests/run_enterprise_regression.sh --ceph-supply-chain-only
bash tests/run_enterprise_regression.sh --ceph-slow-osd-fixed
bash tests/run_enterprise_regression.sh --ceph-observability
bash tests/run_enterprise_regression.sh --ceph-security
bash tests/run_enterprise_regression.sh --ceph-only
bash tests/run_enterprise_regression.sh --ceph-status
bash tests/run_enterprise_regression.sh --ceph-follow
bash tests/run_enterprise_regression.sh --ceph-clean-only
```

`--mysql-only` owns the independent Percona PXC matrix in
`mysql-test-matrix.yaml`. It uses the dedicated six-node MySQL laboratory,
does not enter the delivered core topology or `--all-delivery`, and always
performs scoped PXC cleanup and verification before and after the run.

`--kafka-only` owns the independent Strimzi/Kafka matrix in
`kafka-test-matrix.yaml`. It uses the approved six-node Kafka laboratory,
does not enter the delivered core topology, PXC branch, or `--all-delivery`,
and always performs Kafka-owned cleanup and verification before and after the
run. Use `--kafka-status` and `--kafka-follow` for durable state and live logs;
use `--kafka-cancel` only to stop the Kafka-owned durable job.

`--ceph-only` owns the independent Cephadm/Ceph-CSI matrix in
`ceph-test-matrix.yaml`. The six Ceph-priority hosts provide standalone storage;
the default control is `root@192.168.47.130`, which can reach both lab subnets.
Kubernetes uses masters/etcd `192.168.47.134-136` and workers
`192.168.47.131`, `192.168.47.132`, `192.168.47.137`, with reserved resources
disabled for this branch. The two pools are
leased separately and must pass read-only clean checks before installation.
The existing `k8s-dev` cluster in the supplemental Rocky 9 pool is not touched.
Every destructive device is bound to a current serial/WWN allowlist on storage
hosts only. Product destroy validates its inventory against the leased compute
hosts; Ceph FSID purge and disk checks never target compute hosts.
Default host-network ports prevent unmodified co-location: RGW and RBD CSI
metrics both use `8080`, and Ceph Alertmanager and Calico controllers use `9094`.
This branch validates standalone storage plus external CSI, not hyperconvergence.
`--ceph-probe` is read-only. `--ceph-os-probe` separately checks the six real OS
profiles and their selected kernel client capabilities without marking OS
qualification PASS. Ubuntu 26.04 requires the pinned Ansible 2.20.9 execution
image for Python 3.14; dual-published manifests are a live qualification
prerequisite. If a snapshot reset leaves one of the six
authoritative Ceph hosts named `localhost`, `--ceph-lab-bootstrap` may restore
only its registered hostname; it refuses to overwrite any other identity and
does not touch storage devices. The focused modes, beginning with
`--ceph-slow-osd-fixed`, run one owning scenario and verify its cleanup scope.
Prefer a healthy runner-owned baseline for focused repairs: verify its FSID,
product dependencies, node/resource identities, credentials, business hashes
and absence of injected faults before reuse. Restore the module's own faults
and fixtures without rebuilding unrelated prerequisites; if isolation cannot
be proved, rebuild the smallest affected environment. They remain blocked until the
ext-bin 1.16.0 and image catalogs have matching TalkEdu/Docker Hub digests
by default; the ext-bin image contains the checksum-pinned official cephadm.
The runner writes `ceph_csi_install: "no"` into its generated `config.yml` before
the product `08` entry converges standalone Ceph, then restores `"yes"` before
the normal `90` entry deploys Kubernetes and reconciles Ceph/CSI. This exposes
independent Ceph prerequisite failures before the more
expensive platform setup without omitting the combined product path.
Before enabling Ceph, the same product `08` entry must succeed with the default
disabled configuration, without gathering facts or creating any FSID/owner marker.
Publish each completed module as `pass` after its marker, durable `rc=0`, zero
failure markers and declared cleanup scope are verified. Bind evidence to the
product and owning test dependencies; a related change returns only affected
items to `pending`. Matrix progress, documentation and unrelated test changes
do not invalidate completed business evidence. Keep the aggregate delivery
result `PENDING` until one clean complete `--ceph-only` run passes all 28 cases,
the full case ledger, durable exit and final `LAB_CLEAN_VERIFY_PASS`.
`--ceph-only` also requires current slow-OSD and six-OS qualification evidence.
`--ceph-security` exercises the same product `08`/`90` path and business fixtures,
then verifies wrong CephX/S3 credentials and cross-pool denial before owned cleanup.
The pinned mc JSON error code proves S3 authentication rejection; a connection,
client-argument or unrelated service error cannot satisfy the negative test.
The upgrade gate requires both exact target daemon versions and the official
orchestrator's `in_progress=false`; version convergence can precede daemon-key
rotation and monitoring redeployments.
The pinned [orchestrator CLI](https://github.com/ceph/ceph/blob/v20.2.4/src/pybind/mgr/orchestrator/module.py)
returns `There are no upgrades in progress currently.`
at completion even with `--format json`; the gate accepts that exact terminal
response as well as an explicit completed status object, and rejects other text.
Its bounded 90-minute wall-time budget
includes CLI time, and a budget expiry records the last upgrade status for
classification rather than certifying a product failure from the timeout alone.
`--ceph-probe` and the focused/full prerequisites require the actual compute
`libceph` module to support secure msgr2 before creating fault devices or
deploying storage. The default `aes256k` credential additionally requires the
new key support (Linux 7.0 or a verified vendor backport). The explicit legacy
`aes` compatibility profile still uses GA kernel RBD/CephFS and secure msgr2;
the runner records its CVE-2025-30156 warning and must verify real mount,
read/write, recovery and performance behavior. Ceph-CSI is pinned to v3.17.1.
A module preflight is not business evidence. The runner never selects Alpha
RBD-NBD or silently downgrades the selected credential policy.
The monitoring focused gate preserves explicitly accepted legacy-key warnings.
It matches the aggregate health gauge and every active per-check gauge against
Ceph health JSON, rejects unrelated warnings, and verifies the controlled
`OSDMAP_FLAGS` gauge changes from inactive to active and back to zero through
one runner-owned `noout` maintenance flag, which must be absent at baseline.
The aggregate returns to its accepted baseline; security warnings are not muted.
After an upgrade, the gate waits for the official
`AUTH_INSECURE_ROTATING_SERVICE_KEY_TYPE` transition to expire naturally before
sampling that baseline. Waiting requires exact target daemon versions, completed
orchestration, an active `aes256k` service cipher and no unrelated health checks.
The fixed lab permits at most three service-ticket TTL intervals plus two minutes
(default TTL: one hour). It neither shortens TTL nor wipes rotating keys; the final
baseline still rejects the transition warning. See the official
[health-check procedure](https://docs.ceph.com/en/tentacle/rados/operations/health-checks/#auth-insecure-rotating-service-key-type).
The final Ceph gate additionally requires current real-host qualification for
Rocky 8/9/10 and Ubuntu 22/24/26 in `os_qualification`; Ubuntu 20 is outside
the supported qualification set because it is end-of-life. Its SHA256-bound
evidence records the OS, kernel, Python, runtime, actual product command,
orchestrator remote convergence, secure RBD/CephFS read-back, idempotence,
durable exit and cleanup. A container package-profile test or the fixed
Rocky 9 storage/Rocky 8 compute lab cannot certify another OS version. Missing
profiles block final sign-off before a new full-chain launch; focused work
remains available. Matrix status/evidence changes do not invalidate the OS
source binding (`ceph_gate_fingerprint --without-matrix`); product/gate changes do.
The OS qualification modes own three exact inventories. The legacy round uses
Rocky 8 and Ubuntu 22 with explicitly accepted `aes`; the modern round uses
Rocky 10 and Ubuntu 24/26 with `aes256k`, and the rocky9 round qualifies Rocky 9
with `aes256k` on the Ceph-priority pool. Each Kubernetes topology remains
within one reachable lab subnet; modern hosts use the product's supported
overlapping master/worker groups. Rocky 9 also overlaps master/worker groups
on the last three storage hosts, keeping the RGW hosts outside Kubernetes:
the official host-network RBD metrics and default RGW both bind TCP/8080,
and cordon does not exclude DaemonSets. The existing `.122.2` Kubernetes
cluster is not used. Qualification deliberately co-locates selected MGR or
non-RGW OSD fixtures with CSI clients on the Ceph-priority pool and authorized
OS controls, disables unconstrained monitoring placement, and preserves
hostnames (restoring the temporary Rocky 10 localhost fixture). This fixture
is not a customer hyperconvergence qualification. Each selected host must run
an orchestrator-deployed MGR and a node-pinned CSI Pod, verify both secure
kernel mounts, persistent hashes after detach/remount, 16 fsync samples with
zero errors, and an idempotent second product execution preserving PVC/Pod
and CSI credential identity. Draft JSON is published to ignored
`logs/ceph-os-qualification/` and bound to the matrix only after the supervisor
verifies current source, durable `rc=0`, zero failure markers and scoped cleanup.
The qualification runner probes storage-node-to-OS-host TCP/22 separately from
control SSH. For this NAT-separated lab only, failed management paths use
runner-owned loopback SSH reverse forwards from the authorized control and
exact `/32`, TCP/22 OUTPUT redirects. Ceph data traffic is not redirected.
The journal precedes mutation; cleanup removes the exact tagged rules, SSH
control processes and listeners before the clean marker. Customer configuration
and production SSH policy do not inherit this fixture.
The runner maintains journal-owned SSH masters throughout qualification. A lost
master is reconnected only while its exact NAT rules remain; foreign listeners
or missing rules block recovery. The network focused gate deliberately stops an
owned master, verifies listener removal, restores transport, and then cleans it.
The watcher is stopped before scoped cleanup, including handled failures.
`--ceph-qualification-network-only` proves this fixture and its cleanup under
both host leases and durable supervision before a qualification cluster launch.
`--ceph-prepare-hosts-only` exercises the exact production delegated hosts task
with eight local inventory fixtures and a private temporary file. It checks all
entries and an unrelated line after insertion and an idempotent second run.
Ansible delegation retains parallel forks, so the shared-file update uses
`throttle: 1` as prescribed by the official delegation guide. A missing node
entry can prevent API Server kubelet log/exec access even after a Job completes.
Its runtime-only `CEPH_QUALIFICATION_NETWORK_PROFILE=rocky9` selection also
verifies Registry HTTP `/v2/` from the last three Ceph hosts. If TCP/5000 to the
authorized control is unreachable, exact `/32` OUTPUT redirects use a separate
loopback reverse forward to that control's local Registry. Directly reachable
paths are untouched. Journaled rules, listeners and SSH masters are removed
before the clean marker. This lab fixture does not change customer image paths.
Qualification also rejects a cgroup-v1 host with a transient hybrid `0::/`
entry before storage mutation. Podman 4.9's official split-cgroup path prefers
that root entry and refuses it, including after a Calico cgroup2 mount is removed.
After scoped cleanup, `--ceph-lab-runtime-bootstrap` holds both host leases,
refuses live services/containers/Ceph data, reboots only affected legacy
qualification hosts, and verifies a changed boot ID and restored cgroup state.
It does not change kernel boot options, customer configuration or OCI runtimes.
An installed Ubuntu lab image may retain an enabled `file:/cdrom` source after
its installation media is removed. An `apt-get update` failure naming that
missing Release file is a package-source environment failure. Preserve the
original source and disable only the verified stale stanza using the official
APT [`Enabled: no` field](https://manpages.ubuntu.com/manpages/resolute/en/man5/sources.list.5.html);
require a successful `apt-get -o APT::Update::Error-Mode=any update` before
rerunning qualification. Keep this lab repair out of the production role.
`--ceph-lab-python-bootstrap` is an explicit lab environment recovery mode for
the six clean Rocky 8.10 compute hosts, not a production Python policy change.
It leases the pool, installs distribution-owned Python 3.12 from AppStream,
verifies RPM integrity and system-interpreter package ownership, then runs
100 standard-library import probes per host and 30 native Ansible ping rounds.
A failed prerequisite stops the mode without a success marker. The native RPM
may update an automatic `python3` alternative; the runner does not override
the distribution's links or remove the original interpreter. These probes
are environment evidence, not Ceph business or cross-OS delivery evidence.
Both Ceph-CSI charts are rendered with the product defaults before node access
bootstrap or live installation. The preflight rejects cross-release resource
ownership collisions, wrong namespaces and unowned ConfigMap mounts.
Every string in the Ceph task files also passes Jinja syntax parsing; Ansible
`--syntax-check` alone does not catch embedded shell/template delimiter conflicts.
When publication is explicitly authorized outside CI, the runtime-only
`CEPH_ARTIFACT_MODE=manual-talkedu` mode verifies every TalkEdu image manifest and
the official cephadm SHA256/version without claiming Docker Hub publication.
`CEPHADM_FILE` may supply a locally downloaded executable; the fixed runner
verifies its pinned checksum before transfer and atomically stages it after a
second remote check. Temporary download accelerators are not persisted. Both
modes keep the full business, fault, recovery and cleanup acceptance coverage.
Ceph cleanup verifies stopped Kubernetes runtimes, orphan `k8s.io` shim process
trees and known CNI links as well as Ceph and disk state. It refuses active
Kubernetes/runtime or foreign containerd namespaces. Stopping containerd alone
is not cleanup evidence: the official service uses `KillMode=process` and its
shims may keep node-cache and other host-network fixtures alive.
Before purging the storage FSID, cleanup verifies its ownership marker and
releases kernel RBD mappings whose sysfs `cluster_fsid` matches that FSID;
foreign or unbound mappings block cleanup. Only allowlisted OSD hosts use
`rm-cluster --zap-osds`; MGR-only qualification hosts do not scan or zap disks.
An unmap blocked in kernel D-state fails after a bounded wait, without waiting
again for the uninterruptible child. Preserve that failure, then verify whether
the mapping has disappeared before considering host recovery. A normal reboot
may itself wait on the blocked client; an accepted reboot request is not
recovery evidence. Require a changed boot ID, restored BatchMode SSH and zero
owned kernel mappings, then rerun `--ceph-clean-only` and require its final
`LAB_CLEAN_VERIFY_PASS` before starting another qualification round.

`run_tools_regression.sh` owns the separate `tools-test-matrix.yaml` branch.
The branch is independent from the enterprise and middleware runners: every
script under `tools/` is built and tested as a standalone CLI, with its own
import-boundary, security, functional, recovery and cleanup evidence. Before
review approval, only the non-mutating entry points are allowed:

```bash
bash tests/run_tools_regression.sh --preflight
bash tests/run_tools_regression.sh --status
```

The tools matrix contains 71 scenarios (70 applicable and one not applicable) plus a 102-item
`functional_inventory`. The inventory is the public-function checklist for all
ten CLIs: every command/role/data path/configuration mode/remote path and
failure-recovery behavior must point to a CLI, functional, security or recovery
case. Build success and `--help` are never sufficient evidence for an inventory
item. A functional case is PASS only after its isolated fixture reaches the
documented product result, verifies read-back/state/exit markers, and completes
scoped cleanup; partial execution remains pending.

New or changed matrix cases remain `pending` until review approves fixtures,
credentials, external-service versions and live cleanup scope. The current
matrix is approved, and `--full` still requires the explicit
`TOOLS_MATRIX_APPROVED=yes` gate; Tier3 `--help` results in the enterprise
matrix are not tools delivery evidence.

### MySQL 备份工具增量回归

两个备份 CLI 的变化使用原工具场景与固定镜像，独立于 core/middleware：

```bash
bash tests/run_tools_regression.sh --preflight
TOOLS_SELECTED=MyBackupCli,MyLogiBackupCli TOOLS_BUILD_HOST=ubuntu@192.168.47.138 bash tests/run_tools_regression.sh --build-only
MYSQL_BACKUP_STAGE=physical bash tests/run_tools_regression.sh --mysql-backup-live
MYSQL_BACKUP_STAGE=logical bash tests/run_tools_regression.sh --mysql-backup-live
MYSQL_BACKUP_STAGE=portability bash tests/run_tools_regression.sh --mysql-backup-live
# 聚焦证据核验、对应矩阵项 PASS 且输入未变化后，才允许最终干净完整范围：
bash tests/run_tools_regression.sh --mysql-backup-live
bash tests/run_tools_regression.sh --status
```

138 当前实测 Ubuntu 22.04；构建在 Rocky 8.10/glibc 2.28 容器内完成，仅重建上述两个标准库工具。
现场入口拒绝既存备份 fixture，先持有主机互斥锁，保留现有 registry、集群和缓存镜像。
逻辑回归顺序运行 MySQL 8.0.46、8.4.4、9.2.0，以控制小内存主机峰值；运行器拥有
512 MiB loop 文件系统，验证挂载、归档、恢复与失败清理，不触碰主机裸盘，不代表存储性能测试。
逻辑场景同时验证 gzip/bz2/xz 解码完整性和 SQL SHA256，并通过宿主机真实 systemd service
执行生成的备份任务，再核验退出码、业务标记及 timer/unit 清理；模拟 systemctl 仅用于隔离合同。
portability 在固定 Rocky 8、Debian 12、Ubuntu 24 容器内并行启动两份冻结程序，验证 ABI 与独立启动。
物理回归沿用 MySQL 8.4.4/XtraBackup 8.4.0-5.1。8.0.35-36 的布尔 lock-ddl 与
8.4.0-5 的 REDUCED 差异来自固定官方源码及确定性合同，不冒称实测了 MySQL 8.0.33 或原生 ARM64。

每轮 `logs/mysql-backups-<stage>-<time>` 保存本地 durable、远端双退出码、原始日志及 SHA256 校验的
证据归档。未完成远端工作时监控中断保留现场，先查原 durable，禁止覆盖 staging 目录启动另一轮。
成功需 `MYSQL_BACKUP_RELEASE_PASS`、双退出码 0、业务恢复读回与 `TOOLS_CLEAN_VERIFY_PASS`；
仅有单测、构建或运行中的容器不得签 GA。新增参数/行为覆盖归入 `TL-MBK-06`、`TL-MLB-13`，
最终范围归入 `TL-X-09`。无关工具证据核对依赖后复用，不触发整仓镜像构建或核心回归。

### Tools artifact gate

The independent MySQL tool branch has an additional supply-chain gate. New
fixtures must normally be registered and published by
`kubeauto-ext-images-dockerfile`'s GitHub Actions dual-push workflow before a
live run. A fixture already published in TalkEdu Hub may be consumed directly
when the matrix pins its current manifest digest, owner and cleanup scope and
the delivery does not modify or republish that artifact.
Record the official source, exact version, manifest digest or file SHA256,
owner and cleanup scope in the tools matrix. The runner pulls the TalkEdu
copy first and verifies the digest before handing a fixture to a test host;
dynamic mirror discovery and repeated public downloads are prohibited.

Migration 原生参数增量使用同一个固定入口，先聚焦、再完整验收；客户用法见
[`operations-manual.md`](../tools/mysqltools/operations-manual.md#迁移任务中的使用)：

```bash
# 仅构建三个 mysqltools；不触发镜像发布或其它工具构建。
TOOLS_BUILD_HOST=ubuntu@192.168.47.138 \
TOOLS_SELECTED=MyBackupCli,MyLogiBackupCli,MigrationCli \
bash tests/run_tools_regression.sh --build-only

MIGRATION_TEST_HOST=ubuntu@192.168.47.138 MIGRATION_STAGE=native \
bash tests/run_tools_regression.sh --migration-live
# 收取并核验 native 证据，更新适用前置项后才运行 full。
MIGRATION_TEST_HOST=ubuntu@192.168.47.138 MIGRATION_STAGE=full \
bash tests/run_tools_regression.sh --migration-live
```

`native` 同时验证三工具在 Rocky/Debian/Ubuntu 的冻结程序入口，及 Migration 的
实际 argv、配置继承、并发隔离、导出/导入失败、回滚重试与 SQL 读回。
`full` 拒绝缺失、pending 或输入/日志/归档 SHA256 失配的前置证据；通过后从干净夹具
执行上述场景及既有三版本迁移全链。两者独占主机租约，仅使用固定 digest，拒绝已有夹具，
回收本轮容器、报告、临时凭据和目录；保留其它服务与镜像。
验收必须同时具备 `MIGRATION_CLI_LIVE_REGRESSION_PASS`、
`TOOLS_CLEAN_VERIFY_PASS scope=migration`、`TOOLS_MIGRATION_EXIT rc=0`、
本地 runner rc=0 及 `.remote-status` 的 `0 0`。
`logs/tools-migration-<stage>-<run>.evidence.tar.gz` 保存实际参数和业务报告；
`tests/evidence/mysql-migration-20261109.json` 绑定源码、构建、依赖、失败分类和适用矩阵。

For `MigrationCli`, the live matrix requires fixed MySQL `8.0.46`, one pinned
`8.4.x` and one pinned `9.x` logical-migration fixture. For `MyBackupCli`, use
only a MySQL/XtraBackup combination explicitly supported by Percona's
compatibility documentation. A MySQL 9.x physical-backup combination without
that official support is an explicit boundary (`na`), not a pass. GitHub
Actions may fetch upstream artifacts, but the lab must remain runnable when
public network access is unavailable.

The runner is authoritative because it centralizes SSH, source synchronization, remote launch, durable PID/exit state, foreground log streaming, heartbeat, silent-stall diagnostics, final markers, and cleanup. Do not replace it with a sequence of manually approved SSH commands.

## Required lifecycle

For every stateful run:

1. Refuse an active durable job that owns the same hosts; stop only a verified stale owned job.
2. For a focused repair, restore only its owned faults, fixtures and processes.
   Retain a healthy runner-owned baseline after verifying ownership, dependencies,
   service health, business hashes and absence of fault residue. If isolation
   cannot be proved, clean the smallest affected environment through its runner.
   Full regression uses its normal complete cleanup.
3. Require the declared scoped-clean marker before a focused run, or
   `LAB_CLEAN_VERIFY_PASS` before a full run.
4. Sync current source to the relevant control host.
5. Launch through `run-durable-gate.sh` and the top-level supervisor.
6. Stream every remote log line to the foreground console.
7. Emit a heartbeat every 30 seconds. After 60 seconds with no new log bytes, print durable state and a process-tree diagnostic.
8. On success or failure, verify the module's declared cleanup scope before reuse.
   A monitoring disconnect alone does not invalidate a durable remote workload;
   reconnect and inspect its state before recovery. A completed full regression
   always requires final complete cleanup and `LAB_CLEAN_VERIFY_PASS`.

The remote workload must survive a local SSH/tail disconnect. The `.pid` and `.exit` state files are authoritative; a missing exit record is a failed/lost run, not a pass.

## Failure and root-cause protocol

When a gate fails:

1. Preserve the exact command, current log tail, PID/exit state and cluster events.
2. Determine whether the failure is in the gate, environment, registry/artifact path, runtime, Kubernetes, controller reconciliation, or product code.
3. Consult the official source and documentation for the pinned version. Prefer vendored official charts/CRDs/source when external access is limited.
4. Reproduce from a clean baseline and change one causal layer at a time.
5. Add a focused unit/contract test for the proven root cause.
6. Run the focused live gate, then the required full chain.
7. If the attempted fix does not pass, remove the speculative change and clean its data before continuing.

Examples captured by the current gates include registry HTTP readiness instead of container state, SHA256 validation and atomic restaging of Docker runtime artifacts, asynchronous RocketMQ Broker reconciliation, real LVM/NFS read/write, official Nacos schema import with external MySQL, MinIO Tenant health, and 9/9 executed network-check jobs.

## Test-gate change protocol

Test helpers, runners, fixtures, diagnostics, templates and matrix validators
are delivery code.  They must be validated before they are allowed to consume a
clean lab. Preserve every acceptance scenario. Plan dependency/resource
concurrency, evidence reuse and independent module
closure under the single authority in [AGENTS.md's execution-efficiency
contract](../AGENTS.md#test-engineering-and-execution-efficiency-contract).
For a new middleware branch, first complete the
[standard onboarding sequence](../docs/middleware/delivery-playbook.md#102-扩展步骤)
and its [module delivery-path contracts](../docs/middleware/delivery-playbook.md#71-独立测试分路).
Qualify scope, failure recovery and evidence reuse locally before the first live
launch; an existing fixed entry point alone does not prove those boundaries.
Execute the following evidence ladder in order:

1. Classify the failure and change as product, test gate, environment,
   supply-chain, runtime, Kubernetes/controller, or a proven combination.
   Name the owning matrix scenario, product entry point, terminal marker and
   smallest disproof command.
2. Pass the changed artifact's lowest-cost checks first: syntax/import checks,
   focused unit and contract tests, structured-data parsing, render checks, and
   resource contracts for names, namespaces, selectors, ports, images,
   ownership, markers and durable exit state.
3. Add a deterministic regression test for a proven test-gate defect, then use
   the narrowest fixed-runner branch that executes the affected product path.
   A static or focused failure blocks the full middleware gate.
4. Complete the affected focused module gates, then run one final clean full
   middleware regression. The runner may reuse healthy owned prerequisites
   between focused modules; stale faults, foreign resources or unverifiable
   identities require cleanup of the smallest affected dependency boundary.
   The final full run requires normal complete cleanup and `LAB_CLEAN_VERIFY_PASS`.
5. On a live failure, capture the first failed command, durable exit state,
   rendered inputs, resource state, Pod descriptions/events and controller logs
   before changing code. Abort after a failed prerequisite within its dependent
   branch; independently healthy branches follow the referenced isolation rules.

For each live launch, the log must identify the completed ladder level, the
evidence that unlocked it and the next permitted level.  Direct Helm, kubectl
or SSH remains diagnostic-only unless the test has already exercised the same
behavior through the documented `config.yml` and `kubecli` product entry point.
This protocol is an efficiency control, not a coverage waiver: matrix status,
durable `rc=0`, zero failure markers and final cleanup remain mandatory.

## China image-source contract

For a Docker Hub-origin fixture in an already-delivered path, preserve its explicit, reviewable fallback list. The existing gates try the pinned `docker.sparkcr.cn/<upstream-image>` accelerator before direct Docker Hub, retain the applicable TalkEdu and Huawei candidates, and keep the upstream reference last. A mirror-discovery page such as `https://status.anye.xyz/` is for operator review only; never consume its current recommendations dynamically in a delivery run.

An accelerator response is not evidence that the fixture is usable. The gate must complete pull, inspect the exact image, tag and push it into the local registry, then verify the expected registry tag/manifest before deploying a workload. Pin the real upstream tag throughout the source list, local-registry tag and Pod manifest; never retag a different patch release to impersonate the requested version.

New middleware branches do not persist dynamically discovered public accelerators. When an isolated middleware test requires temporary acceleration, inject the reviewed source through a non-persisted runtime parameter, verify the checksum or manifest digest, and remove the override after the run.

## Script map

Authoritative orchestration:

- `run_enterprise_regression.sh`: development-host owner of the full workflow and all supported modes; `--all-delivery` is the single final-signoff command.
- `run_unit_tests.sh`: isolated unit suite; writes logs to a temporary directory.
- `helpers/run-durable-gate.sh`: remote PID/exit wrapper.
- `helpers/lab-wipe-nodes.sh`: destructive lab cleanup plus independent verification.
- `helpers/sync-kubeauto.sh`: controlled source synchronization.
- `helpers/lab-control-ssh-bootstrap.sh`: key-only control-to-node access bootstrap; it transfers only the control public key and never persists a fallback password or copies a private key.

Current live gates:

- `helpers/regression-full.sh`: core topology, lifecycle, CNI, addon and reserved baseline.
- `helpers/regression-jumper.sh`: jumper/Ansible 2.16 path.
- `helpers/nerdctl-gate.sh`: containerd/nerdctl contract.
- `helpers/delivery-docker-gate.sh`: Docker, buildx, Compose, cri-dockerd and artifact recovery.
- `helpers/registry-reboot-gate.sh`: local Registry restart policy, real control-host reboot recovery and persisted-manifest integrity.
- `helpers/delivery-upgrade-smoke.sh`: real Kubernetes patch transition.
- `helpers/delivery-gaps-fullchain.sh` and `helpers/delivery-gap-retest.sh`: remaining storage, messaging, registry, observability and network workload gates.
- `helpers/build-tools-rocky8.sh` and `helpers/tier3-tools-gate.sh`: build every frozen tool on glibc 2.28, execute the exact outputs on real Rocky 8, and remove scoped build/runtime residue.

Recovery and historical phase scripts are retained because they encode useful focused diagnostics and safe resume points. They are not alternate top-level sign-off commands. Before reusing one, verify that its preconditions still match the matrix and run it through the same durable supervision and cleanup contract.

Retained focused/recovery assets:

- registry and image preparation: `bootstrap-brinnatt-mirrors.sh`, `registry-ready-gate.sh`, `registry_blob_integrity.py`;
- storage fixtures: `prep-node-lvm-loop.sh`, `prep-nfs-server.sh`, `mk-cluster-133.sh`;
- reserved-resource focus: `reserved-4g-gate.sh`, `rocky-reserved-gate.sh`, `run-reserved-fullchain.sh`, `verify-node-reserved.sh`;
- phase/resume diagnosis: `regression-phase3.sh`, `regression-remainder.sh`, `regression-resume.sh`, `regression-resume-g2.sh`, `regression-resume-g3.sh`, `resume-from-g8.sh`, `resume-g8b-only.sh`;
- superseded launch/smoke paths retained for logic reference: `regression.sh`, `start-full-regression.sh`, `delivery-gap-smoke.sh`.

The last group must not be used for final sign-off: it predates parts of the current durable supervisor and full cleanup contract. Reuse useful checks by migrating them into the authoritative runner/gates with unit coverage.

## Matrix state transitions

Before implementation, set only affected entries to `pending` and record the
changed dependency. Publish a module as `pass` as soon as its complete current
product evidence, durable `rc=0`, zero failure markers and declared scoped cleanup
are verified; do not wait for unrelated modules or the final full run. Unknown
dependencies require impact analysis before reuse. After a completed gate, update:

- the item status and evidence;
- its execution-group status;
- coverage totals;
- `meta.updated`, `meta.regression_log` and `meta.regression_result`;
- the overall PASS/PENDING/FAIL assessment.

Validate YAML parsing and count every `status: pass`, `status: pending`, and `status: fail`. Tier3 scope-defined skips remain explicit and are not silently counted as pass.

## Logs and retained knowledge

Runtime logs belong under `logs/` and are ignored by Git. They are transient diagnostic evidence and may include host-specific or obsolete data. Do not commit them.

When a log teaches a reusable lesson, convert that lesson into one or more of:

- a unit or contract test;
- a bounded helper/gate;
- an enterprise-matrix evidence entry;
- this handbook or another maintained document.

Then remove the raw log when it is no longer needed for the active investigation.

MongoDB 实验室的 Longhorn 系统快照会保留已删除的容量注入文件及旧日志块，卷内 `df` 恢复不等于宿主空间已释放。`--mongodb-storage-reclaim` 使用固定 1.10.1 原生 `snapshotDelete`、`trimFilesystem`、`snapshotPurge` 回收三个源卷的内部快照和空闲块；先验证源 CR UID、PVC/PV/CSI 绑定、卷和 engine UID、两个健康 RW 副本，拒绝用户快照、未知标签、重建或外部卷。它不修改卷配置，不直接删除副本文件，原生操作支持失败后重入。容量压力和监控容量模块结束后自动执行此收尾；每个 worker 必须保留至少 16 GiB 且 25% 宿主余量，预期 `MONGODB_STORAGE_RECLAIM_PASS`。该标志只证明夹具回收，不签收 MongoDB 业务模块；随后仍须验证源库、备份及原失败模块。

### Longhorn 所有权验收证据复用

`--longhorn-ownership-evidence-import` 是本地只读聚合入口，不同步源码或操作集群。
通过运行时环境变量 `LONGHORN_OWNERSHIP_IMPORT_REQUEST`、`LONGHORN_OWNERSHIP_IMPORT_SHA256`、
`LONGHORN_OWNERSHIP_ORIGINAL_ARCHIVE`、`LONGHORN_OWNERSHIP_CONTINUATION_ARCHIVE` 指定已审阅的六阶段候选、
候选 SHA256 及两批归档目录（各含 `frozen-inputs.json` 和 `frozen-source/`），然后运行
`bash tests/run_enterprise_regression.sh --longhorn-ownership-evidence-import`。
仅当六个原 scope 的冻结验证器、原始材料、106 项产品依赖及同一 fixture 的数据连续性全部通过时，
新聚合才输出 `LONGHORN_OWNERSHIP_PASS`；本地 `logs/longhorn-cases/ownership-evidence-*/`
同时保留 `ownership.exit`、`ownership.finalized`、`gate.log` 和 `status.json`。
任何材料缺失、哈希变化、终态非零或身份不一致均停止。原失败父链保持原状态；此入口不签署整体交付。

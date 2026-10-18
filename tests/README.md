# Enterprise regression handbook

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

The tools matrix contains 68 executable scenarios plus a 101-item
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
clean lab.  Preserve every acceptance scenario, but execute the following
evidence ladder in order:

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
   before changing code.  Abort after a failed prerequisite rather than running
   unrelated later stages to obtain secondary failures.

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

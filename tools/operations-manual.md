# 独立工具使用入口与配置规则

本文面向执行独立工具的运维人员，用于选择正确的命令、帮助入口和执行环境。`tools/` 中的 Python 工具各自独立，不能用 kubeauto 核心或另一个工具的配置代替本工具的参数。以下命令使用安装到 PATH 的可执行文件名；源码执行时改为 `python3 <源码路径>`，并安装该工具声明的依赖。冻结程序直接执行，不放到 Python 解释器后面。

## 按任务选择帮助和手册

`--help` 说明当前程序接受哪些选项，带子命令的工具还需查看该子命令的 `--help`。专项帮助不需要数据库或集群连接。文档中的版本范围表示对应实现或实测范围，不能由“能够识别版本”推导为所有版本组合已通过验收。

| 工具与源码 | 任务与帮助入口 | 配置、输出及执行边界 |
| --- | --- | --- |
| [NetCheckCli](NetCheckCli.py) | ICMP/TCP 稳定性：`NetCheckCli --help`，先 `--dry-run` | CLI 目标与 `NETCHECK_*` 环境变量；报告默认当前目录，日志默认 `./logs`；测量的是执行端网络 |
| [OvpnUserCli](OvpnUserCli.py) | 证书与客户端包：`OvpnUserCli --help`、`check --help`、`create --help`、`revoke --help`、`status --help` | `GENOVPN_*` 指定真实 PKI、服务配置及输出目录；完整操作说明嵌于各级帮助；不是远程 VPN 管理接口 |
| [CalicoPolicyCli](k8stools/CalicoPolicyCli.py) | 访问控制：`CalicoPolicyCli --help`、`plan --help`、`apply --help`、`delete --help` | 显式 context；host/both 还需 executor；源码 `MAINTAINER_DOC` 解释策略机制和风险，`--help` 含完整实例 |
| [KubeBackupCli](k8stools/KubeBackupCli.py) | API 配置备份：`KubeBackupCli --help`、`backup --help`、`restore --help` | kubeconfig/context、备份目录、映射及过滤选项；根帮助含恢复改写说明；不备份卷内文件或 etcd 数据 |
| [KubePublishCli](k8stools/KubePublishCli.py) | 镜像下载、打包、分发及删除：`KubePublishCli --help` | JSON 仅操作集合，其它设置用 CLI；根帮助列出完整选项、配置与实例，规则见下一节 |
| [KafkaCli](kafka/KafkaCli.py) | KRaft 部署与运维：`KafkaCli --help` | 根帮助的分步示例是本独立工具的操作手册；[JSON 模板](kafka/kafka.json.example) 含节点角色、编号与部署顺序；不等同于 kubeauto Kafka 中间件入口 |
| [MyBackupCli](mysqltools/MyBackupCli.py) | 物理备份、增量与 PITR：`MyBackupCli --help`、`--help-config`、`--help-native` | [MySQL 运维手册](mysqltools/operations-manual.md)、[带注释 JSON 模板](mysqltools/mybackup.conf.example)；配置执行端路径和实际启停目标 |
| [MyLogiBackupCli](mysqltools/MyLogiBackupCli.py) | SQL 逻辑备份：`MyLogiBackupCli --help`、`backup --help`、`timer install --help`、`--help-native` | 登录路径、独立挂载、归档轮换、systemd timer；[MySQL 运维手册](mysqltools/operations-manual.md#逻辑备份的执行环境与定时配置)；没有 restore 子命令 |
| [MigrationCli](mysqltools/MigrationCli.py) | MySQL 逻辑迁移：`MigrationCli --help`、`--guide`、`--help-native` | [配置模板](mysqltools/migration_config.json.example) 及 [MySQL 运维手册](mysqltools/operations-manual.md#迁移任务中的使用)；先读 `--guide`，再连库 `--dry-run` |
| [StarCli](starrocks/StarCli.py) | StarRocks 二进制部署、状态与清理：`StarCli --help` | 根帮助含 FE/BE/CN 完整部署实例；小写 `starcli` 是可选安装别名，远程交接也会优先查找该别名；配置冲突规则见下一节 |
| [StarMigrtCli](starrocks/starmigration/StarMigrtCli.py) | StarRocks SQL 导出和导入：`StarMigrtCli --help`、`dump --help`、`load --help` | [专项手册](starrocks/starmigration/README.md)；固定 StarRocks 3.5.7；先暂停源变更，导入空目标并自动校验 |

### 不同工具的 JSON 规则不能混用

**MyBackupCli。** `-c` 不填时使用程序默认值，不会自动读取 `/etc/mysql/backup.conf`。使用 `MyBackupCli --help-config` 获取完整注释模板；它与随附 `mybackup.conf.example` 使用相同字段。说明键 `_comment_*` 不参与执行，标准 JSON 不支持 `#` 和 `//` 行注释。文件权限必须为 `0600`。默认值、空字符串和 `null` 不等价，模板逐字段说明空字符串行为。

**KubePublishCli。** JSON 只读取 `download`、`pack`、`distribute`、`delete` 操作集合。runtime、namespace、输出目录、tar 路径、SSH 和 cleanup 均由 CLI 提供。非空 CLI 列表覆盖同名 JSON 列表；空开关可复用配置列表，并不取消其它 JSON 操作。不要在多操作配置后加一个 `--pack` 就认为只会打包。

例如，`publish.json` 只包含这一次任务的镜像和目标主机；示例名称须替换成实际交付身份：

```json
{
  "pack": ["registry.example.internal/team/app:1.0.0"],
  "distribute": ["worker-01.example.internal:22"]
}
```

```bash
KubePublishCli --config publish.json --local-runtime docker \
  --remote-runtime nerdctl --namespace k8s.io --output-dir /srv/image-packages \
  --ssh-user deploy --ssh-key /home/deploy/.ssh/id_ed25519
```

执行前确认本地已有该镜像、目标主机密钥已登记、执行账户能调用对应 runtime。分发必须等待远端 `load` 成功；遇到非零退出先检查错误和已加载目标，不把已成功加载的镜像当作自动回滚。`--cleanup` 仅按帮助清理自动生成归档；用户通过 `--tar` 提供的文件不是本次自动清理对象。

**StarCli。** 显式 CLI 参数优先于同名 JSON 字段，JSON 优先于程序默认值。未传入布尔开关不会取消 JSON 中的 `true`；需要关闭时修改配置。`--helper-address` 与 `--use-fqdn` 可同时使用，分别指定加入集群的 FE 和本机 FQDN 身份。`--force`/`--clean` 会操作真实服务、目录和数据，必须先核对目标并独立备份。

**KafkaCli。** 显式 CLI 参数优先于同名 JSON 字段，JSON 优先于程序默认值。按根帮助和 JSON 模板填写角色、存储目录、cluster ID 以及节点编号。环境变量主要用于帮助所列认证或默认值，不替代必填拓扑。模板中的第二、三台 controller 的 cluster ID 是待替换占位值，不可直接执行整份模板；首次格式化、后续 observer 加入和 `quorum-add-controller` 是不同步骤。预先声明首批选民时，所有首批 Controller 使用相同 `--initial-controllers` 列表及显式 `--cluster-id`，不加 `--join-quorum`。`--batch` 在执行第一台前检查全部节点目标，任一节点执行失败即停止后续节点。指标采集的文本和 JSON 两种输出均保留各段结果，全部成功退出 `0`，任一段失败退出 `1`；仅有 Controller 时，Broker 相关段失败也会返回 `1`。

**MigrationCli。** 按 `--guide` 的全局 options、单任务 options 和 CLI 合并规则选择配置；密码可引用 `${ENV_VAR}`。`--dry-run` 会实际连接源端和目标端预检，不是完全离线检查。工具不自动冻结业务写入，也不把 SQL 迁移变成复制拓扑管理。

**OvpnUserCli。** `revoke --user USER` 完成 CA 吊销、CRL 生成和客户端配置清理。服务端应配置 `crl-verify` 并部署生成的 CRL，按服务端流程处理已在线会话。

> CRL 生成失败或中断时，CA 可能已经记录吊销，但服务端仍使用原 CRL。重试同一 `revoke` 命令，工具会核验 CA 记录及归档证书的 CN、serial 后继续生成 CRL；归档身份不一致则拒绝。不要继续分发此用户的配置，也不要把 CA 的 `R` 状态单独当作服务端已拒绝连接的证明。

## 宿主机、工具容器与远程节点

| 执行方式 | 路径和权限由谁提供 | 结果如何判断 |
| --- | --- | --- |
| 宿主机直接执行 | 执行账户提供本地文件、客户端、socket、runtime、systemd 权限 | 核对工具退出码、目标身份、输出及业务结果 |
| 独立工具容器 | 显式挂载本工具所需配置、归档、凭据、数据和 socket；准备外部命令 | 网络可达不证明本地文件或服务管理可用；按具体工具的容器条件核对 |
| KafkaCli/StarCli 经 SSH 执行 | 目标节点提供安装包、Java、数据路径、systemd 等；源码交接另需 Python 及依赖 | 检查远程动作结果及限定清理；冻结文件直接执行，OS/架构必须匹配 |

MyBackupCli 同时需要 SQL 连接和文件读取。宿主机看到的 datadir 与 MySQL 容器内部路径可以不同；`mysql_datadir`、`mysql_socket`、`mysql_binlog_dir`、`mysql_binlog_index` 必须填写工具可见路径。恢复时还要管理实际 `mysql_container` 或 `mysql_service`。独立工具容器须使用同绝对路径挂载及同一 Docker daemon；不能在即将被停止的 MySQL 容器里执行完整恢复，详见 [MySQL 路径说明](mysqltools/operations-manual.md#物理备份执行位置与路径)。

Kubernetes 工具通过 API 访问集群，但 kubeconfig、归档和日志仍属于执行端。KubeBackupCli 显式传入 kubeconfig 或 context 时读取 kubeconfig；两者都未指定才先尝试 in-cluster、失败后使用默认 kubeconfig。CalicoPolicyCli 则要求显式 context，不能把二者的默认连接方式混为一谈。网络策略变更可能切断运维连接，应先审阅 `plan` 输出并按其手册保留恢复入口。

NetCheckCli 观测的是当前网络命名空间；在工具容器中运行会使用该容器的路由和 DNS。生产探测自选目标建议使用 `--no-default-nodes`，避免同时探测源码内置目标。`--dry-run` 只验证参数和前置，不能证明连通性。退出码 `2` 表示测量完成但未达到程序阈值，`0` 也不替代业务可用性探针。

> 多目标 ICMP 按工作线程分片轮询，每个目标的一批 ping 需要采样时间。测试时长应足以让每个目标完成采样；尚未采样的目标会保留在摘要中，标明“无数据”并使质量检查不通过。增加 `--duration` 或在资源允许时调整 `--max-icmp-workers` 后重新测量。用户中断退出 `1`，仍保留已生成的报告和摘要。

## 随附 shell 的边界

`starrocks/starrocks-3.5.7/fe-cn/scripts/` 的三个 shell 是 Compose 内部入口，没有通用 `-h`；**不要为了看帮助而直接运行它们**。部署及启动顺序以 [Compose 手册](starrocks/starrocks-3.5.7/README.md#52-启动与初始化) 为准。

| 脚本 | 实际配置来源及动作 | 重启与失败边界 |
| --- | --- | --- |
| `init-rustfs-cn.sh` | Compose 注入 AWS 凭据与 `RUSTFS_BUCKET`；通过 `http://rustfs:9000` 检查/创建桶 | 修改的是 S3 桶，不是宿主机同名目录；检查或建桶失败时非零退出 |
| `init-storage-volume-cn.sh` | `RUSTFS_BUCKET/REGION/ACCESS_KEY/SECRET_KEY` 不能为空；连接 `starrocks-fe:9030` 创建 `rustfs_volume` 并设为默认 | `IF NOT EXISTS` 保留已有元数据；更改密钥或端点不会自动更新旧卷，需按手册维护 |
| `healthcheck-cn.sh` | 当前 CN 的 `127.0.0.1:8040` 和 FE 上的 `information_schema.be_configs` 查询 | 返回有效节点 ID 才通过；健康检查不代替表读写；SQL 凭据由 Compose 的 `MYSQL_PWD` 提供 |

这些固定服务名和内部端口属于随附 Compose 网络；脚本不接受另一套宿主机目录或远程部署参数。初始化容器 `Exited (0)` 与数据库业务验收是不同结果。

## 如何核对拿到的程序与手册

先从实际安装路径执行帮助，防止 PATH 中同名旧程序覆盖新安装文件：

```bash
command -v MyBackupCli
sha256sum /usr/local/bin/MyBackupCli
/usr/local/bin/MyBackupCli --help
/usr/local/bin/MyBackupCli --help-config
```

将路径替换为实际安装位置，用交付清单中的程序 SHA256 核对身份。源码更新不会自动改变已冻结程序或生产上的文件。`--help-config` 无法识别时，应先核对程序版本及来源，不能仅替换配置后假定功能已经更新。日志出现密码掩码也不表示其它凭据材料可以公开。

帮助一致性检查只证明选项可查、模板对应和说明边界；完整备份恢复、远程部署及数据读回必须使用其所属矩阵和原始证据。项目当前验收状态使用 `bash tests/run_tools_regression.sh --status` 核验，不能用 `--help` 成功替代产品 PASS。

## 官方依据与维护

参数显示遵循 [Python argparse](https://docs.python.org/3.9/library/argparse.html)，路径解释遵循 [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)。物理备份需结合 [PXB 8.0 版本兼容说明](https://github.com/percona/pxb-docs/blob/f8a15e8e73028742f92f63bddb6fe365bca5e311/docs/server-backup-version-comparison.md)；证书动作使用 [Easy-RSA 3.0.7](https://github.com/OpenVPN/easy-rsa/tree/v3.0.7) 的 `build-client-full/revoke/gen-crl`。

各工具的版本、官方资料和既有验收来源由 `tests/tools-test-matrix.yaml` 的 `tool_catalog`、`official_sources` 及所属 evidence 维护；MySQL、StarRocks 专题手册保留各自官方说明。修改参数、默认值、拒绝条件或执行边界时，同步维护解析器帮助、专项帮助、配置示例和本文任务入口。

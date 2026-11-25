# MySQL 工具：物理备份恢复与原生参数使用指南

本文面向数据库运维人员。物理备份章节说明 MyBackupCli 如何操作单 MySQL 容器的数据与日志挂载，并完成备份、恢复和失败处置；后续章节保留三个独立工具的原生参数入口。执行人应理解 MySQL 事务、binlog 和 Docker bind mount，具备备份文件及实例启停权限。

三个工具彼此独立。以下命令假定相应可执行文件已安装到 PATH；使用源码时，把 `MyBackupCli` 换成 `python3 tools/mysqltools/MyBackupCli.py`，另外两个工具同理。

| 要完成的任务 | 入口 |
| --- | --- |
| 查看普通命令及常用示例 | `MyBackupCli --help`、`MyLogiBackupCli --help` |
| 为单 MySQL 容器配置物理备份 | [执行位置与路径](#物理备份执行位置与路径) |
| 完成全备、增量和恢复演练 | [物理备份与恢复主线](#物理备份与恢复主线) |
| 恢复中途失败或进入下一备份周期 | [物理恢复异常与保留策略](#物理恢复异常与保留策略) |
| 查看导出、前置检查或定时任务选项 | `MyLogiBackupCli backup --help`、`preflight --help`、`timer install --help` |
| 查看迁移任务配置、预演及失败处理 | `MigrationCli --guide`、`MigrationCli --help` |
| 查命令/阶段、完整拒绝清单和替代配置入口 | 任一工具的 `--help-native`，无需 root、数据库连接或备份目录 |
| 临时添加参数 | [命令行使用](#命令行使用) |
| 保存参数供以后使用 | [配置与定时任务](#配置与定时任务) |
| 参数被拒绝或上游报错 | [失败判断与恢复](#失败判断与恢复) |

## 物理备份执行位置与路径

本节针对 AMD64、Rocky Linux 8.10/9.8、单 MySQL 8.0.33 或 8.0.46 容器、未加密 InnoDB，数据、binlog 和临时目录分别 bind mount 的部署。这里说明当前实现与操作条件；完整验收以 `tests/tools-test-matrix.yaml` 的 TL-MBK-07～09 及其原始证据为准，不能从 MySQL 8.4 或某一个模式的通过推定全部版本通过。外置 general tablespace、独立 undo/redo 目录、keyring、复制拓扑和跨版本恢复没有纳入本节的验证范围。存在这些配置时应停止套用本主线，先制定对应恢复方案。

XtraBackup 通过 socket 查询运行实例，同时直接读取本地文件。SQL 返回的 `@@datadir` 是 MySQL 进程看到的路径，宿主机上的同一批文件可能位于另一个目录。仅能连上数据库不能证明备份到了正确目录。MyBackupCli 的全备、增量均显式传入配置中的 `--datadir`、`--log-bin` 和 `--log-bin-index`；启用容器管理时，还会核对运行实例的 SQL 路径与 Docker writable bind mounts 是否映射到配置的真实目录。

以以下挂载为例，表内两列不能混用：

| 对象 | MySQL 容器内路径 | 宿主机路径 |
| --- | --- | --- |
| datadir | `/var/lib/mysql` | `/data/deploy/mysql8.0.33/data` |
| socket | `/var/lib/mysql/mysql.sock` | `/data/deploy/mysql8.0.33/data/mysql.sock` |
| binlog 目录 | `/var/lib/mysql-logs` | `/data/deploy/mysql8.0.33/logs` |
| binlog index | `/var/lib/mysql-logs/mysql-bin.index` | `/data/deploy/mysql8.0.33/logs/mysql-bin.index` |
| tmpdir | `/var/lib/mysql-tmp` | `/data/deploy/mysql8.0.33/tmp` |

MySQL 的 `log_bin=/var/lib/mysql-logs/mysql-bin.log` 通常生成 `mysql-bin.000001` 和 `mysql-bin.index`；应以运行实例的 `@@log_bin_basename`、`@@log_bin_index` 及实际文件为准，不能直接把配置字符串末尾的 `.log` 当成归档文件前缀。

在宿主机执行以下只读核对。将容器名设为实际 `docker compose ps` 所列实例；不要根据镜像名猜容器名：

```bash
MYSQL_CONTAINER=mysql8.0.33
docker inspect --format '{{.Id}} {{json .Mounts}}' "$MYSQL_CONTAINER"
docker exec -it "$MYSQL_CONTAINER" mysql -uroot -p -e \
  'SELECT VERSION(),@@datadir,@@socket,@@log_bin_basename,@@log_bin_index,@@gtid_mode,@@global.time_zone;'
```

这里 `-p` 交互输入凭据，不将密码写入终端参数或文档。若目录不是 writable bind、路径无法一一对应，或启停属于另一 Docker daemon，先停止配置备份。不要增加宿主机 `/var/lib/mysql` 的软链接来掩盖路径错误。

**宿主机执行。** MyBackupCli、`mysql`、`mysqlbinlog`、`xtrabackup` 及压缩解压依赖须在执行环境中可用；不能假定官方 MySQL server 镜像包含 `mysqlbinlog`。本节 8.0 测试依赖为 Percona XtraBackup 8.0.35-36，使用前核对其支持版本说明与服务端版本；不要使用关闭版本检查的参数绕过兼容要求。运行 `mysql --version`、`mysqlbinlog --version`、`xtrabackup --version` 并记录结果。备份账号需具备该版本 XtraBackup 文档列出的权限（包括备份锁和日志刷新所需权限）；恢复导入还需要相应写入权限，应单独管理恢复凭据。

下面是宿主机配置模板。密码为待替换示例，必须在受控编辑器中替换；文件归执行人所有且权限 `0600`。目录、前缀、index 和容器名都必须来自上面的实值核对。备份目录不得位于 datadir 或 binlog 目录内部。

```json
{
  "backup_base": "/mybackup",
  "mysql_user": "mysql_backup",
  "mysql_password": "REPLACE_WITH_CONTROLLED_SECRET",
  "mysql_socket": "/data/deploy/mysql8.0.33/data/mysql.sock",
  "mysql_container": "mysql8.0.33",
  "mysql_datadir": "/data/deploy/mysql8.0.33/data",
  "mysql_binlog_dir": "/data/deploy/mysql8.0.33/logs",
  "mysql_binlog_prefix": "mysql-bin",
  "mysql_binlog_index": "/data/deploy/mysql8.0.33/logs/mysql-bin.index",
  "xtrabackup_parallel": 4,
  "xtrabackup_compress_threads": 4,
  "xtrabackup_use_memory": "1G",
  "xtrabackup_lock_ddl": "AUTO"
}
```

`mysql_container` 非空时，通过本机 Docker 管理目标容器，固定容器 ID 后再操作；不使用 `mysql_service` 启停数据库。对于容器部署，配置宿主机 `mysqld.service` 会管理错误的对象。只有 MySQL 本身由 systemd 管理时，才保持 `mysql_container` 为空并配置真实 `mysql_service`。

**独立工具容器执行。** 工具容器需要同一套二进制与依赖，且连接本机 Docker daemon。把宿主机数据、日志、备份目录以**相同绝对路径**挂进工具容器，沿用上述配置，避免 Docker inspect 返回的宿主机路径在工具容器中不可见。恢复需要写权限和原数字 UID/GID 的所有权恢复能力；只读数据挂载只能用于备份，不能直接用于恢复。Docker socket 访问赋予宿主机管理能力，仅允许可信运维镜像和管理员使用。

以下是执行位置模板。`MYSQL_BACKUP_TOOL_IMAGE` 必须由交付清单提供固定 digest，镜像内须已经装好 MyBackupCli、Docker CLI、mysql/mysqlbinlog/XtraBackup 及解压依赖；这里不指定一个未经验证的通用镜像代替它。先在宿主机设置该变量，再执行：

```bash
: "${MYSQL_BACKUP_TOOL_IMAGE:?请设置已验证的工具镜像完整digest引用}"
docker run --rm --user 0 --entrypoint MyBackupCli \
  -e TZ=Asia/Shanghai \
  -v /usr/share/zoneinfo/Asia/Shanghai:/usr/share/zoneinfo/Asia/Shanghai:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /data/deploy/mysql8.0.33:/data/deploy/mysql8.0.33 \
  -v /mybackup:/mybackup \
  -v /etc/mysql/backup.conf:/etc/mysql/backup.conf:ro \
  "$MYSQL_BACKUP_TOOL_IMAGE" full -c /etc/mysql/backup.conf
```

工具在独立容器中运行，才能在恢复时停止并重新启动 MySQL 容器。不要在将被停止的 MySQL 容器里运行完整恢复，否则停止目标容器也会终止恢复进程。直接在 MySQL 容器里进行备份需要工具及依赖真实存在、使用该容器可见路径；该执行方式没有等同于本节的独立工具容器生命周期验收。

## 物理备份与恢复主线

先在隔离恢复实例演练完整流程，再用于生产恢复。执行人必须核对 SQL 实例、挂载、版本、账号权限、可用空间和时区；恢复窗口内停止应用写入并隔离流量。`xtrabackup_use_memory=1G` 是 prepare 内存预算示例，线程数并非性能或容量保证。空间需要同时容纳原始压缩链、解压/prepare 工作副本、恢复后的数据以及保留的原目录，须按实际数据量和压缩比测量峰值，不能只给一个压缩全备的空间。

以下命令在配置对应的同一执行环境中运行。先将 `BACKUP_DATE` 设置为本次全备实际生成的日期；不能把示例日期直接用于另一批备份：

```bash
sudo MyBackupCli full -c /etc/mysql/backup.conf
sudo MyBackupCli incr -c /etc/mysql/backup.conf
sudo MyBackupCli incr -c /etc/mysql/backup.conf
mysql --socket=/data/deploy/mysql8.0.33/data/mysql.sock -umysql_backup -p \
  -e 'FLUSH BINARY LOGS;'
sudo MyBackupCli binlog -c /etc/mysql/backup.conf
```

每一步检查退出码为 0 及对应完成日志；全备和增量目录应有 `.backup_ok`。失败的目录不能作为下次增量基线。两次增量均属于最近一次有效全备，LSN 必须前后连续。`binlog` 日常入口只复制已关闭日志；上面的 MySQL 管理命令用于关闭物理快照坐标所在的当前日志，执行人须有相应刷新权限。若漏掉刷新和归档，恢复可能在停库前拒绝并报告 `Archived binlogs do not include the physical snapshot position`。此时补齐日志关闭、归档和重新审计，不能删除坐标检查绕过。业务环境中应按恢复点目标定期归档 binlog，不能用全备/增量成功代替持续日志保护。

```bash
BACKUP_DATE=2026-11-25
sudo MyBackupCli checksum -c /etc/mysql/backup.conf --date "$BACKUP_DATE"
sudo MyBackupCli restore-dry-run -c /etc/mysql/backup.conf --date "$BACKUP_DATE"
sudo cat "/mybackup/restore_audit/$BACKUP_DATE/restore.plan.json"
```

核对计划中的 `full`、全部 `incr` 和 `binlog` 为本次恢复点所属链，`status` 为 `READY`，且文件没有缺失。`restore-dry-run` 会检查备份、日志连续性及容器路径并生成计划，但没有执行解压、prepare 或实际恢复。计划可生成不等于数据可恢复。不要手工改计划以绕过校验。

恢复时，工具先复制备份到工作目录，解压并验证物理 binlog 坐标，再 prepare 全备与增量；这些步骤在停库之前完成，原始备份保留以便重试。随后停止真实实例、确认 SQL 已不可用、保存原数据与外置 binlog，再 copy-back、恢复所有权、启动并等待 SQL 就绪，最后执行 binlog 重放。

恢复时间采用 **mysqlbinlog 执行环境的本地时区**，与数据库 session 时区不同。下面例子要求执行端实际 `date +%z` 为 `+0800`；容器只有 `TZ=Asia/Shanghai` 而没有相应 zoneinfo 时可能仍按 UTC 解析，必须先核对。起始时间应不晚于所选物理快照的 binlog 坐标对应时刻，文件/位置下界由工具从最终物理备份坐标确定；把起始时间设在其后会跳过本应重放的事务。结束时间为业务确认的恢复截止边界，同一秒内的交易无法仅凭时间区分，需另行评估位置级恢复。

```bash
date '+%Y-%m-%d %H:%M:%S %z %Z'
BACKUP_DATE=2026-11-25
RECOVERY_START='2026-11-25 09:00:00'
RECOVERY_STOP='2026-11-25 10:00:00'
sudo MyBackupCli restore -c /etc/mysql/backup.conf --date "$BACKUP_DATE" \
  --confirm-destructive-restore \
  --binlog-start-time "$RECOVERY_START" --binlog-stop-time "$RECOVERY_STOP"
```

三个时间值均为待替换输入，须依据备份记录和业务事件确定。省略结束时间会重放到归档日志末尾，只适合明确要求恢复至该末尾的任务。只有退出 0 并出现 `MYSQL RESTORE COMPLETED SUCCESSFULLY!`，才表示工具完成流程；仍须用受控账号核对关键业务表、账目总额、事务完整性，并验证截止前记录存在、截止后记录不存在。必要时运行应用只读检查；未完成读回前不得恢复写流量。

## 物理恢复异常与保留策略

| 停止位置 | 数据状态与处置 |
| --- | --- |
| 版本、映射、客户端、解压或 prepare 失败 | 尚未覆盖原实例。保留首错和计划，核对版本、路径、权限、空间或缺档；修复后重新审计，不能接受没有完成标志的备份 |
| copy-back 失败 | 工具尝试恢复本次保存的原数据与外置 binlog；只有回滚成功才允许重新启动。若回滚本身失败，保持停库，先核对原目录、目标目录和日志保存位置，不手工启动半成品 |
| 启库或 PITR 失败 | copy-back 可能已经完成，PITR 也可能已经导入部分事务。保持应用写入隔离，保留退出码、计划、mysql 错误日志；修复原因后从保留的原始备份重新恢复，不能对部分导入结果盲目追加重放 |
| 服务停止后仍能执行 SQL | 启停对象与连接实例不一致，工具拒绝覆盖。核对容器 ID、socket 和 Docker daemon，不改保护逻辑继续执行 |

原 datadir 保存在同级 `data.bak_时间戳`；外置 binlog/index 保存在日志目录下 `.mysql-bin.before_restore_时间戳`（名称前缀随配置变化）。error/slow 日志保留。恢复的 binlog index 会改写为 MySQL 容器实际可见路径，所有权沿用原数字 UID/GID。上述保存目录不是自动永久归档：先完成读回、独立备份和恢复点签收，再按变更单回收；不要在排查期间删除它们。

恢复会形成新的数据库历史。工具以 `lock/restore-needs-full` 拒绝随后直接追加增量或 binlog，必须先完成一次新的 `full`，再安排新的 `incr`、`binlog`。每次全备具有独立 binlog 分代，避免恢复后重新编号的日志混入旧链。不要删除该保护文件强行续接旧链。

保留策略按备份日期目录管理，并保留窗口之前最近的有效全备作为增量恢复基线。定期核对仍需使用的恢复点、其增量与日志依赖后再运行：

```bash
sudo MyBackupCli purge -c /etc/mysql/backup.conf --backup-days 15 --binlog-days 30
```

15/30 天是示例，须满足业务 RPO、审计及灾备要求。`purge` 会删除过期归档，没有预演模式；执行前核对独立副本与最后成功恢复演练记录。不能通过删除近期备份内 mtime 较旧的表空间文件来节省空间，也不要让 MySQL 自身日志过期早于归档周期。

本节机制依据：[PXB 8.0 支持版本](https://docs.percona.com/percona-xtrabackup/8.0/supported-versions.html)、[增量 prepare](https://docs.percona.com/percona-xtrabackup/8.0/prepare-incremental-backup.html)、[恢复与权限](https://docs.percona.com/percona-xtrabackup/8.0/restore-a-backup.html)、[PITR](https://docs.percona.com/percona-xtrabackup/8.0/point-in-time-recovery.html)、[MySQL 8.0.33 mysqlbinlog 时间解析](https://github.com/mysql/mysql-server/blob/mysql-8.0.33/client/mysqlbinlog.cc)、[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)。上游支持条件、工具实现和实验室验收是三个不同层次；本节不承诺零数据损失或未经测量的恢复时长。

## 先选择参数要交给谁

例如 `--native-option mysqldump=--extended-insert` 的含义是“给 mysqldump 增加 `--extended-insert`”。等号左边是命令或阶段，右边是这个命令自己的选项。它不会自动给其它命令添加同一选项。

| 工具 | 等号左侧填写 | 实际用途 |
| --- | --- | --- |
| MigrationCli | `mysqldump` | 源库 SQL 导出，包括结构导出和逐表数据导出 |
| MigrationCli | `mysql` | 目标库 SQL 导入；不作用于源库或 PyMySQL 连接 |
| MyLogiBackupCli | `mysql` | 前置检查、版本与数据库信息查询 |
| MyLogiBackupCli | `mysqldump` | 导出 SQL |
| MyBackupCli | `mysql` | 版本、备份信息查询，以及恢复时写入 binlog；不是 XtraBackup 的连接配置 |
| MyBackupCli | `mysqlbinlog` | 恢复时解析 binlog；不作用于 binlog 文件复制 |
| MyBackupCli | `xtrabackup-backup` | `full`、`incr` 的备份阶段 |
| MyBackupCli | `xtrabackup-prepare` | `restore` 内部的全量及增量 prepare 阶段 |
| MyBackupCli | `xtrabackup-decompress` | `restore` 内部的解压阶段 |
| MyBackupCli | `xtrabackup-copy-back` | `restore` 内部的数据复制阶段 |

必须完整填写上表名称，例如 `prepare` 不能代替 `xtrabackup-prepare`。参数只在所选命令实际执行时使用：给 `full` 指定 prepare 参数并不会启动 prepare，也不能证明该参数已经生效。

## 命令行使用

每次 `--native-option` 只接收一个原生长参数；多个参数重复写。带值时使用 `--参数=值`，无值开关直接写 `--参数`。不接受 `-e` 这类短参数、独立位置参数或整串 shell 命令。

下面的逻辑备份示例要求已有 `mysql-backup` 登录路径、独立挂载的 `/backups`、可写备份目录，以及主次版本与服务端相同的 mysql/mysqldump 客户端。`app` 必须是备份账号可见的真实数据库。

```bash
# 仅给前置 mysql 查询设置连接超时；预检不执行 SQL 导出。
sudo MyLogiBackupCli preflight --database app \
  --native-option mysql=--connect-timeout=15

# 只改变 mysqldump 的 INSERT 输出形式。
sudo MyLogiBackupCli backup --database app \
  --native-option mysqldump=--extended-insert

# 同时给两个不同命令设置各自的选项。
sudo MyLogiBackupCli backup --database app \
  --native-option mysql=--connect-timeout=15 \
  --native-option mysqldump=--max-allowed-packet=256M
```

预检成功应输出 `MYSQL_BACKUP_PREFLIGHT_OK`；备份必须退出 0 并输出 `MYSQL_BACKUP_OK archive=...`，才表示工具完成了导出、归档回读和发布。预检成功不能替代实际备份成功；归档完成仍须按既定流程演练恢复和业务读回。

`15` 秒和 `256M` 是参数语法示例，不是生产统一配置。默认仍使用逐行 INSERT；显式添加 `--extended-insert` 会改变输出组织，但不能消除查询等待或 1412，也不会让 `--single-transaction` 隔离所有 DDL。

物理工具要求已有权限为 `0600`、通过版本兼容检查的配置文件；其中服务名、数据目录、socket 和备份目录必须对应实际实例。下面只给备份阶段添加限速，不改变 prepare：

```bash
sudo MyBackupCli full -c /etc/mysql/backup.conf \
  --native-option xtrabackup-backup=--throttle=100
```

`100` 是语法示例，应按实际 XtraBackup 版本的限速定义和备份窗口选择。检查本次日志中的 `RUN:` 是否包含 `--throttle=100`，同时确认命令退出码和备份完成结果；只有参数出现在命令行不能证明数据可恢复。默认日志为 `/var/log/mysqlbackup.log`，以实际 `MYSQLBACKUP_LOG_FILE` 设置为准。

路径含空格时，把整个参数值用单引号包住。例如在已经配置好 TLS 的环境中：

```bash
sudo MyLogiBackupCli backup --database app \
  --native-option 'mysqldump=--ssl-ca=/etc/mysql/backup CA.pem'
```

该文件必须真实存在、可读并符合当前 TLS 配置。单引号保护终端里的空格和 `$`；工具收到的是一个参数，不执行其中的 shell 代码。此例只设置 mysqldump 的 CA 路径，不配置前置 mysql 查询；需要两者一致时分别传入。设置 CA 路径也不等于已经完成服务端身份校验。

## 迁移任务中的使用

MigrationCli 的 `mysql` 是目标导入客户端；它的预检、建库和迁移后校验通过 Python 的 PyMySQL 连接执行。给 `mysql` 设置 `--connect-timeout` 只改变导入客户端的连接超时，不改变 PyMySQL 的预检连接。给 `mysqldump` 设置参数会作用于该任务实际执行的结构和数据导出命令。三个工具保持同一语法，命令所承担的任务见上表。

先复制 `migration_config.json.example` 并按照 `MigrationCli --guide` 填写真实的 source/target、账号环境变量和迁移模式，设置配置权限 `0600`。确认目标库为空、客户端版本经过预检，并已安排迁移窗口及应用写入控制。随后先预演，再使用相同参数迁移：

```bash
MigrationCli -c migration_config.json --dry-run \
  --native-option mysqldump=--net-buffer-length=32768 \
  --native-option mysql=--connect-timeout=45

MigrationCli -c migration_config.json \
  --native-option mysqldump=--net-buffer-length=32768 \
  --native-option mysql=--connect-timeout=45
```

`32768` 字节和 `45` 秒只是语法示例。预演会连接数据库并预检，不导出和导入 SQL，因此不能证明附加参数已经被客户端接受。正式迁移应退出 0，审计 JSON 的 `summary.failed` 为 0，各任务 `status` 为 `completed`，数据迁移任务的 `validation.passed` 为 true；再按业务要求核对关键表、视图、程序对象和应用查询。日志中的完整命令可用于确认参数送到了哪个客户端；仅看到命令或“预检通过”不足以签收迁移。

在现有 JSON 的 `options` 中添加以下字段可保存参数：

```json
{
  "options": {
    "native_options": {
      "mysqldump": ["--net-buffer-length=32768"],
      "mysql": ["--connect-timeout=45"]
    }
  }
}
```

这是合并片段，仍须保留原配置的 `migrations` 任务。每个 `migrations[].options.native_options` 可以按命令替换全局数组：只声明 `mysqldump` 时仍继承全局 `mysql`；声明 `"mysqldump": []` 时清除该命令的继承参数。CLI 参数随后追加到所有任务，不受任务空数组清除。需要撤回调整时，检查全局、任务和 CLI 三处，删除相应值，再预演核对实际命令。

迁移账号、库范围、GTID、结构模式、输出和兼容策略继续使用原有配置或 CLI 入口；原生参数不能覆盖这些约束。迁移失败可能留下部分目标数据。先保留首错、报告和目标状态，按 `--guide` 的失败处理确认后再清理或重试；不要直接对非空目标重跑，也不要把 `--force` 当作修复方法。`rollback_on_failure` 会删除目标库，只能在确认目标为本次迁移专用且无须保留时启用。

## 配置与定时任务

MyBackupCli 可以在现有 JSON 配置中合并下面的 `native_options` 字段；不要用这个片段替换整个配置文件：

```json
{
  "native_options": {
    "mysql": ["--connect-timeout=15"],
    "xtrabackup-backup": ["--throttle=100"],
    "xtrabackup-prepare": [],
    "xtrabackup-decompress": [],
    "xtrabackup-copy-back": [],
    "mysqlbinlog": []
  }
}
```

键名对应上表的命令或阶段，值必须是字符串数组；可省略不用的键或使用空数组。保存后仍保持配置权限 `0600`。配置参数先传入，CLI 参数追加在后；同一参数重复出现时由原生命令解释，不应依赖所有参数都具有“最后一个覆盖”的行为。撤回某项调整时，移除对应数组元素及命令行中的同名参数。

MyLogiBackupCli 不读取这个 JSON。一次 `backup` 的参数不会自动保存到定时任务，须在 `timer install` 时明确写入需要的全部选项：

```bash
sudo MyLogiBackupCli timer install --at 01:01 \
  --exec-path /usr/local/bin/MyLogiBackupCli \
  --database app --compression gz \
  --native-option mysqldump=--extended-insert
sudo MyLogiBackupCli timer status
```

`--exec-path` 必须是实际安装的可执行文件绝对路径。看到 `MYSQL_BACKUP_TIMER_INSTALLED` 和 `MYSQL_BACKUP_TIMER_OK` 表示任务已安装且 timer 状态正常，不表示业务备份已经完成；还须检查实际执行记录中的 `MYSQL_BACKUP_OK` 和 service 退出结果。

修改已安装任务时重新执行 `timer install`，同时声明完整的库范围、压缩方式和原生参数；新配置只影响以后启动的任务。回退时用上一个已验证参数集重新安装。停止计划任务使用 `sudo MyLogiBackupCli timer remove`，预期 `MYSQL_BACKUP_TIMER_REMOVED`；它不删除历史备份归档。

## Python 调用的扩展入口

维护 Python 集成时，三个独立脚本均提供 `native_arguments(target, *args, **kwargs)`，返回经同一保护规则校验的 argv 数组。例如 `native_arguments("mysqldump", "--net-buffer-length=32768", max_allowed_packet="256M")` 返回两个长参数；关键字中的下划线转为连字符，列表或元组生成重复参数，`True`/`None` 生成无值开关，`False` 生成 `=OFF`。这些参数仍须由对应客户端支持。

MigrationCli 的 `MySQLDumpManager.build_mysqldump_command()` 和 `build_mysql_command()` 也接受额外的 `*args`、`**kwargs`，在配置参数之后、数据库位置参数之前追加，并沿用保护规则。面向运维人员交付时直接使用本文的 CLI 和 JSON，不需要调用 Python 接口。

## 失败判断与恢复

扩展入口不能覆盖工具负责的凭据、输出位置、备份范围、一致性和恢复流程。完整清单直接从当前工具版本读取：

```bash
MyBackupCli --help-native
MyLogiBackupCli --help-native
MigrationCli --help-native
```

清单不是所有可用参数的白名单；未列出的选项仍须由对应版本的原生命令支持。保护检查也覆盖缩写、下划线和常见开关变体，另拒绝名称含密码、token 或密钥内容标识的参数；不要尝试换一种拼写绕过。

| 客户看到的问题 | 判断与正常入口 |
| --- | --- |
| “不支持的原生命令或阶段” | 对照阶段表，完整填写名称；逻辑工具不能接收 `xtrabackup-backup` |
| “格式错误”或“不接受短参数、位置参数” | 使用 `命令或阶段=--完整长参数名=值`；多个参数分别重复 `--native-option` |
| 连接或密码参数被拒绝 | 物理工具用现有 `mysql_*` 配置；逻辑备份用 `setup-login` 和 `--login-path`；迁移用 JSON `source`/`target`；不把密码放进原生参数 |
| 库范围参数被拒绝 | 逻辑备份使用 `--database`、`--exclude-database`；迁移使用 `source/target.database` 和迁移模式；当前没有原生表过滤绕过入口 |
| `--result-file`、`--target-dir`、`--datadir` 被拒绝 | 输出由工具管理；物理工具使用 `backup_base`、`mysql_datadir` 配置；逻辑工具使用既定备份挂载配置 |
| `--lock-ddl` 或恢复时间范围被拒绝 | 物理工具使用 `xtrabackup_lock_ddl`、`--binlog-start-time`、`--binlog-stop-time`；恢复仍须审计及显式确认 |
| `--force`、`--ignore-error` 或关闭一致性保护被拒绝 | 没有绕过入口；移除该选项，排查原失败原因，不能以忽略错误签完整备份 |
| mysql/mysqldump/XtraBackup 报 unknown option、非法取值或连接错误 | 参数已交给原生命令，但实际版本或环境不接受；保留首错、工具及原生命令版本，按对应版本官方说明纠正或移除本次参数 |

参数被拒绝时停止本次操作，不修改脚本绕过保护。回退到之前验证过的参数集，再执行预检和最小备份验证；失败后没有新的成功标记时不能把该轮当作完整备份。保留既有可用备份，不因排查删除归档。物理恢复具有覆盖数据的副作用，不应用执行恢复来试探一个参数是否有效。

上游参数的含义、默认值和版本限制分别参考 [MySQL 8.0 mysqldump](https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html)、[XtraBackup 8.0](https://docs.percona.com/percona-xtrabackup/8.0/) 和 [XtraBackup 8.4](https://docs.percona.com/percona-xtrabackup/8.4/)。另见 [MySQL 8.4 mysql 客户端](https://dev.mysql.com/doc/refman/8.4/en/mysql-command-options.html) 及 [PyMySQL 1.1.2 连接实现](https://github.com/PyMySQL/PyMySQL/blob/v1.1.2/pymysql/connections.py)，后者使用 Python 连接关键字参数，不能接收 MySQL CLI 参数。本工具负责参数路由和保护，不使不兼容的服务端、客户端或架构组合自动兼容。

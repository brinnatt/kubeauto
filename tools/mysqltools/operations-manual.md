# MySQL 工具：备份与迁移原生参数使用指南

本文面向已经部署 MyBackupCli、MyLogiBackupCli 或 MigrationCli 的数据库运维人员，说明如何添加工具尚未单独封装的原生命令选项。普通备份继续使用既有入口；不需要为了使用工具学习 Python 或阅读源码。本文不替代首次部署、版本兼容检查或恢复演练。

三个工具彼此独立。以下命令假定相应可执行文件已安装到 PATH；使用源码时，把 `MyBackupCli` 换成 `python3 tools/mysqltools/MyBackupCli.py`，另外两个工具同理。

| 要完成的任务 | 入口 |
| --- | --- |
| 查看普通命令及常用示例 | `MyBackupCli --help`、`MyLogiBackupCli --help` |
| 查看导出、前置检查或定时任务选项 | `MyLogiBackupCli backup --help`、`preflight --help`、`timer install --help` |
| 查看迁移任务配置、预演及失败处理 | `MigrationCli --guide`、`MigrationCli --help` |
| 查命令/阶段、完整拒绝清单和替代配置入口 | 任一工具的 `--help-native`，无需 root、数据库连接或备份目录 |
| 临时添加参数 | [命令行使用](#命令行使用) |
| 保存参数供以后使用 | [配置与定时任务](#配置与定时任务) |
| 参数被拒绝或上游报错 | [失败判断与恢复](#失败判断与恢复) |

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

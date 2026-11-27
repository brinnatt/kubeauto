# StarMigrtCli 使用手册

StarMigrtCli 将 StarRocks 库表导出为一个 SQL 文件，并将该文件导入另一套 StarRocks。生产操作按“暂停写入 → 导出 → 导入并校验 → 切换业务”执行。

> **适用范围**：StarRocks 3.5.7 普通内表；面向数据库管理员。支持单库、多库、选表、结构或数据导出，以及 SQL 文件压缩。源、目标不必互通，文件可以离线传输。它是全量逻辑迁移工具，不提供增量同步、整集群备份或跨表事务快照。大型数据集应先评估窗口与空间，必要时采用官方对象存储导出、导入通路。

## 目录

- [一、准备](#一准备)
- [二、完整迁移](#二完整迁移)
- [三、选择库表](#三选择库表)
- [四、结构、数据和压缩](#四结构数据和压缩)
- [五、参数参考](#五参数参考)
- [六、异常处理](#六异常处理)
- [七、工作机制与支持范围](#七工作机制与支持范围)

## 一、准备

### 1. 安装工具

使用独立可执行文件时，将 `StarMigrtCli` 放入执行主机的 `PATH`：

```bash
StarMigrtCli --version
StarMigrtCli --help
```

源码方式安装固定依赖后执行：

```bash
python3 -m venv "$HOME/.venvs/starmigrt"
. "$HOME/.venvs/starmigrt/bin/activate"
python -m pip install 'PyMySQL==1.1.2'
python StarMigrtCli.py --help
```

> 源码运行要求 Python 3.9+。以下命令使用独立可执行文件；源码方式将 `StarMigrtCli` 替换为 `python StarMigrtCli.py`。执行主机需要能访问所操作 FE 的 MySQL 端口，默认 `9030`。工具不依赖 StarCli 或 kubeauto。

### 2. 准备账号与存储

准备源端只读账号、目标端迁移账号，以及受控的文件存放目录。

> 源账号需要选中库表的 `SELECT` 权限和元数据可见性。目标账号需要建库、建表、插入以及查询权限；导入后的自动校验也使用该账号。已有目标库可以使用，但其中不得存在本次将创建的同名表。使用迁移专用的新库更容易隔离失败与回退。
>
> SQL 文件含业务数据，导出权限为 `0600`；请选择足够空间的目录，并使用受控通道传输。默认不覆盖已有文件。压缩需要 CPU，导入后数据、副本和索引需要目标存储空间；不能用压缩 SQL 文件大小估算目标容量。

### 3. 准备连接信息

下面的主线将源 `sales` 库迁移到目标同名库。在各执行主机上，将 `source-fe.example.internal`、`target-fe.example.internal` 和账号替换为实际值。

> 未设置密码环境变量或密码文件时，工具会交互提示输入密码，不回显。自动化任务可使用 `--password-file /etc/starmigrt/source.password`，文件必须属于执行用户且权限不高于 `0600`。也可通过秘密管理系统注入 `SR_PASSWORD`；不要把密码写入命令行或脚本。密码文件优先于环境变量，空密码须显式设置空的 `SR_PASSWORD`。
>
> 需要加密连接时，在对应命令增加 `--ssl-ca /etc/starmigrt/ca.pem`，并使用证书中的主机名。工具会验证 CA 和主机名，并在认证前拒绝服务器降级为明文。该参数不会替服务端启用 TLS。

## 二、完整迁移

```mermaid
flowchart LR
    A[暂停源写入与结构变更] --> B[dump：一个 SQL 文件]
    B --> C[传输并核对 SHA256]
    C --> D[load：检查目标并导入]
    D --> E[自动核对行数与内容]
    E --> F[业务验证与切换]
```

### 1. 暂停源端变更

暂停选中库表的应用写入、导入任务、DDL 和自动分区变更，等待正在执行的写入结束。保持这一状态，直到目标校验与业务切换完成。

> **一致性前提**：工具逐表读取，StarRocks 的该 SQL 通路不提供跨表、跨库一致性快照。持续停写用于避免导出不同表时读到不同业务时点；仅比较行数或观察没有新日志不能证明停写完成。暂停范围由应用、导入任务和调度配置共同决定，源只读账号不能代替停写。若无法安排停写窗口，请选择适合在线迁移的数据同步方案。

### 2. 导出 SQL

```bash
StarMigrtCli dump \
  --host source-fe.example.internal --user migration_reader \
  --database sales --output sales.sql
```

正常结束时显示：

```text
STARMIGRT_DUMP_OK tables=12 rows=800000 file=sales.sql sha256=...
```

> 表数、行数和摘要随实际数据变化。导出过程中显示每表进度，长任务每 15 秒输出当前阶段、对象和累计行数。退出码为 `0` 且出现 `STARMIGRT_DUMP_OK` 才表示文件生成完成；结构和数据直接保存在 `sales.sql`，无需其它文件。
>
> 文件写入期间使用同目录的 `sales.sql.partial`；发生错误或正常中断时删除临时文件。源数据不会被工具修改。源端结构或分区在导出期间变化时停止，不发布最终文件。

### 3. 传输并检查文件

在源执行机和目标执行机分别核对：

```bash
sha256sum sales.sql
```

> 两端摘要必须与导出完成时显示的摘要相同。源、目标在同一执行机上可直接使用原文件。SHA256 用于检测文件变化，不提供加密或来源认证；只导入可信来源的文件。

### 4. 导入并自动校验

```bash
StarMigrtCli load \
  --host target-fe.example.internal --user migration_writer \
  --input sales.sql
```

正常结束时显示：

```text
STARMIGRT_LOAD_OK tables=12 statements=... verification=content
```

> 导入会先完整检查文件，再检查全部目标对象；包含结构的文件遇到任何同名目标表时，在执行建库、建表和插入前停止。随后顺序执行 SQL，每条 INSERT 独立提交，首个错误、警告或提交尚未可见的状态都会停止。导入完成后自动扫描目标，比较列信息、行数和内容摘要；这些步骤全部通过后才返回 `0`。
>
> 目标在导入与校验期间应保持无业务写入和结构变更。数据不会作为整个文件一次性提交，失败后的已提交语句也不会自动回滚；处置方法见[异常处理](#六异常处理)。

### 5. 验证业务并切换

用目标上的关键业务查询核对结果，检查分区、副本、索引及外部依赖，按业务变更计划切换连接，然后恢复写入。

> 带数据导出的动态分区表会在目标关闭 `dynamic_partition.enable`，防止导入期间自动删除历史分区；源配置不变。应先检查目标保留范围，再逐表执行 SQL 文件末尾注释给出的 `ALTER TABLE`。确认调度不会删除需要保留的数据后，才恢复动态分区。自动数据校验不能代替业务关联、权限和容量验收。
>
> 切换前失败时，目标保持隔离，源端仍是权威数据；按原业务入口恢复源写入。切换后若目标已产生新写入，不能直接切回旧源，须先按业务回退方案处理新增数据。

## 三、选择库表

### 单库或多库

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --databases sales audit --output sales-audit.sql
```

### 全部可见业务库

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --all-databases --exclude-database 'tmp_*' --output business.sql
```

> `--all-databases` 只覆盖当前账号可见的业务库，排除 `information_schema`、`sys`、`_statistics_` 和 `starrocks_monitor`；不等于整个实例的全部对象。没有权限看到的库表不能由工具发现。视图、外表等不支持对象会明确报错，不会静默跳过；需要迁移这些对象时，应另行使用相应官方方法。

### 选择或排除表

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --database sales --table 'orders*' --exclude-table 'orders_tmp*' \
  --output orders.sql
```

> 表参数可重复，支持 shell 通配符 `*`、`?`、`[...]`，不是 SQL LIKE；请加引号防止 shell 展开。不带库名时匹配每个选中库的表名，带库名时匹配完整的 `库.表`。包含条件先执行，排除条件后执行；包含模式未匹配时停止。库表名含点号时应先使用 `--check` 核对范围。

### 修改目标库名

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --database sales --target-database sales_new --output sales-new.sql
```

> 单库用 `--target-database`；多库用可重复的 `--database-map sales=sales_new`。目标库名保存在 SQL 文件中，导入时不再指定库名。多个源库不能映射到同一个目标库，库名映射不修改业务字符串或集群属性中的外部引用。

### 只检查范围

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --database sales --check
```

> 检查读取版本、对象、结构和列元数据，不扫描业务数据、不生成文件。显示源表到目标表的映射，成功输出 `STARMIGRT_CHECK_OK`。它不检查目标容量，也不证明每个实际值都可编码；生产迁移仍按完整流程执行。

## 四、结构、数据和压缩

### 只导结构或只导数据

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --database sales --schema-only --output sales-schema.sql

StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --database sales --data-only --output sales-data.sql
```

按相同顺序导入：

```bash
StarMigrtCli load --host target-fe.example.internal --user migration_writer \
  --input sales-schema.sql

StarMigrtCli load --host target-fe.example.internal --user migration_writer \
  --input sales-data.sql
```

> 默认同时导出结构和数据，通常无需拆分。`--schema-only` 与 `--data-only` 互斥。只导结构不读取业务数据；只导数据要求目标已有空表、列信息一致，且动态分区和其它分区删除策略已经暂停。含活跃 TTL/retention 属性的表不能进行数据迁移，工具会明确停止；只导结构可保留这些定义。
>
> 副本数默认保持源值。需要适配目标节点数时，在包含结构的导出命令中增加 `--replication-num N`；单副本降低数据保护能力，不作为生产默认。不要用数据模式向已有业务表追加或同步数据。

### 压缩 SQL

```bash
StarMigrtCli dump --host source-fe.example.internal --user migration_reader \
  --database sales --output sales.sql.gz

StarMigrtCli load --host target-fe.example.internal --user migration_writer \
  --input sales.sql.gz
```

> `.gz` 后缀自动启用 gzip，导入时自动解压，不需要合并或手工解压。单行超过默认 INSERT 大小上限时，导出停止且不截断数据；可用 `--batch-mib` 调整上限后重新导出。

### 使用 MySQL 客户端导入

```bash
mysql --host=target-fe.example.internal --port=9030 --user=migration_writer \
  --password --default-character-set=utf8mb4 --show-warnings < sales.sql
```

> 普通 SQL 文件也可由 MySQL 命令行或 Navicat 的“运行 SQL 文件”导入。必须首错停止，不使用 `--force`，不启用 MySQL 锁表、外键或整文件事务包装；客户端 TLS 参数须单独配置。该方式不会执行 StarMigrtCli 的文件、空目标或自动内容检查，需自行检查目标为空、导入结果及业务数据。推荐主流程使用 `StarMigrtCli load`。

## 五、参数参考

| 参数 | 适用命令 | 默认及作用 |
| --- | --- | --- |
| `--host`、`--user` | dump/load | 必填，所操作 FE 的地址与账号 |
| `--port` | dump/load | `9030`，范围 1–65535 |
| `--password-file` | dump/load | 不设置时使用环境变量或交互输入；只读私有密码文件 |
| `--password-env` | dump/load | `SR_PASSWORD`，指定密码环境变量名称 |
| `--ssl-ca` | dump/load | 不设置时使用服务端普通 MySQL 连接；设置时强制验证 TLS |
| `--connect-timeout` | dump/load | `15` 秒，范围 1–120 |
| `--query-timeout` | dump/load | 每条查询或导入超时 `3600` 秒，范围 1–86400；网络读超时另加 30 秒 |
| `--database` / `--databases` / `--all-databases` | dump | 三选一，必须明确范围 |
| `--output` | dump | 必填的新文件；仅 `--check` 时不填写 |
| `--input` | load | 必填的 SQL 文件或 `.sql.gz` 文件 |
| `--table`、`--exclude-table`、`--exclude-database` | dump | 默认不过滤；可重复，支持通配符 |
| `--target-database` / `--database-map` | dump | 默认保留库名；分别用于单库和多库改名 |
| `--schema-only` / `--data-only` | dump | 默认结构加数据；两者互斥 |
| `--replication-num` | dump | 默认保留源值；范围 1–32767，只用于包含结构的导出 |
| `--batch-rows` | dump | 每条 INSERT 最多 `500` 行，范围 1–1000000 |
| `--batch-mib` | dump | 每条 INSERT 最大 `1` MiB，范围 1–256 |
| `--check` | dump | 默认关闭；启用时只预检，不生成文件 |

## 六、异常处理

> **导出失败或中断**：修复首个错误，确认源仍保持停写，然后重新运行导出命令。失败不会发布最终 SQL，正常退出会删除 `.partial`。若主机断电或进程被强制杀死，先确认没有导出进程，再删除该任务遗留的 `.partial`，或改用新的输出文件。不要将临时文件当作完成文件导入。
>
> **文件损坏或不完整**：导入在连接目标前停止。重新传输并核对 SHA256；源文件本身有问题则重新导出，不手工补写文件完成标记。
>
> **目标表已存在或数据表不为空**：先核对是否选错目标。工具不会删除、覆盖或合并已有数据；改用新的迁移专用库名重新导出，或由管理员在确认所有权、备份与删除范围后清理本次迁移对象。禁止忽略错误继续插入。
>
> **导入 SQL 报错、返回警告、连接中断或提交尚未可见**：立即停止业务切换，保留日志与失败目标。文件不是整库事务，已成功语句可能已经提交，不能直接重放整个文件。根据日志中的 SQL 序号定位输入语句；INSERT 含 `WITH LABEL`，可在对应目标库用 `SHOW LOAD WHERE LABEL = '实际label'` 查询任务。查不到记录不能证明从未提交。修复权限、容量、分区或兼容问题后，准备新的空目标库，再导出、导入和校验；不要依赖 label 作为永久去重机制。
>
> **自动校验失败**：目标保持隔离，源继续作为权威数据。检查目标是否存在并发写入、过滤警告或分区自动删除，修复后使用干净目标重新迁移；不通过手工修改校验值绕过。
>
> **退出码**：`0` 表示当前命令完成；`1` 表示运行错误；`2` 表示参数错误；`130` 表示中断。进度和诊断写入 stderr，完成结果写入 stdout；stderr 是输出通道名称，不表示其中所有内容都是错误。

## 七、工作机制与支持范围

> 导出使用流式游标逐表读取，按行数和字节上限生成 INSERT，不将整表放入内存。SQL 保留显式列名与目标库名，不生成 DROP、TRUNCATE 或覆盖写入。每个 INSERT 使用独立 label，便于查询提交状态。
>
> SQL 注释中保存结构信息、行数、内容摘要和文件完成校验，普通 MySQL 客户端会忽略这些注释。导入先检查完整文件和 SQL 语句边界，再连接目标；顺序执行后，以同样的类型编码扫描目标。内容摘要对每行计算 SHA256 后按模 2²⁵⁶ 求和，包含重复行、不依赖读取顺序；它是概率性数据校验，不能代替业务语义或完整 DDL 审核。
>
> 数据支持整数（含 LARGEINT）、DECIMAL、浮点、布尔、CHAR/VARCHAR/STRING、DATE/DATETIME、JSON 和 VARBINARY；保留 NULL、空串、Unicode 及转义字符。NaN、Infinity、复杂类型、生成列、视图、外表及活跃分区保留策略不在数据导出范围，遇到时明确停止，不丢列、不跳行。仅导结构可以处理普通内表的其它列类型。账号、权限、外部目录、存储卷、异步物化视图和集群配置应按各自管理方式迁移。
>
> 源码运行环境为 Python 3.9+，依赖固定为 PyMySQL 1.1.2。独立可执行文件的操作系统、架构与最低 glibc 要求以交付制品说明为准；不将单机实验吞吐外推为生产迁移窗口。可先选代表性表测量导出、传输、导入与自动校验耗时，再规划整体停写窗口。
>
> 官方接口依据：[StarRocks 3.5.7 INSERT](https://github.com/StarRocks/starrocks/blob/3.5.7/docs/en/sql-reference/sql-statements/loading_unloading/INSERT.md)、[SHOW CREATE TABLE](https://github.com/StarRocks/starrocks/blob/3.5.7/docs/en/sql-reference/sql-statements/table_bucket_part_index/SHOW_CREATE_TABLE.md)、[动态分区](https://github.com/StarRocks/starrocks/blob/3.5.7/docs/en/table_design/data_distribution/dynamic_partitioning.md)、[官方迁移通路](https://github.com/StarRocks/starrocks/blob/3.5.7/docs/en/administration/data_migration_tool.md)、[PyMySQL 1.1.2](https://github.com/PyMySQL/PyMySQL/tree/v1.1.2)。

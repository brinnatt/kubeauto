# Kubernetes 1.36 与 Harbor 2.16 开发维护手册

本文面向维护六仓源码、配置和交付流水线的开发人员。先了解Python、Ansible与
Kubernetes API，按本手册定位版本事实、扩展升级边及验证失败恢复。客户执行入口见
[运维手册](operations-manual.md)，请求及状态机制见[白皮书](technical-whitepaper.md)。
适用版本是Kubernetes `1.35.10 → 1.36.6`、Harbor官方`2.16.0`，不定义自动追随latest。

导航：[版本与归属](#版本与归属) · [产品调用与幂等](#产品调用与幂等)
· [Harbor维护接口](#harbor维护接口) · [验证与发布](#验证与发布)
· [后续版本维护](#后续版本维护)。

## 版本与归属

| 权威事实 | 归属 | 维护要求 |
| --- | --- | --- |
| 执行文件默认版本 | `common/constants.py`及所属模板/下载器 | `kubecli version`报告发行默认值，不伪装为在线探测 |
| 客户源状态 | 原`clusters/NAME/config.yml`、库存及在线观察 | 不在升级前改成目标模板或丢弃客户自定义值 |
| 精确迁移边 | `upgrade/product/hop.py`及本版`release.yaml` | 相邻minor、固定patch，缺少兼容依据时拒绝 |
| 制品消费身份 | `artifact-pins.json`及六仓Dockerfile/catalog | 完整tag、CPU、当前双仓manifest及内容核验；不因Git SHA自动重建 |
| 可接受的新主张 | `test-matrix.yaml`及`acceptance-plan.yaml` | 有效旧证据复用，变化断言补最窄真实验证 |
| 发布准入 | `upgrade/product/release_policy.py` | 源码开发使用矩阵；冻结发行只携带已接受身份和必要runtime数据 |

主仓负责功能和客户入口，产品镜像仓负责按正式产品tag打包；K8s二进制仓只负责
Kubernetes六文件，ext-bin负责扩展包并消费sp1，ext-images负责外部镜像目录。
每次跨仓只修改实际依赖集合；版本分支命名、稳定线保护与dev集成统一遵守
[主仓契约](../../AGENTS.md#版本分支与交付收尾)。不得为一次升级另建readback产品分支。

## 产品调用与幂等

CLI解析升级请求后进入`upgrade/product/entrypoint.py`。逐minor入口与旧patch
playbook分开；准备、控制面、逐节点、附加组件及最终配置各有交接边界。
`prepare.py`绑定库存、版本、源对象与恢复点，coordinator按固定依赖驱动阶段。
journal同时绑定cluster UID与实际输入摘要，既防止接管另一个集群，也防止代码漂移
后沿用不适用的恢复状态。

阶段先验证前置，再变更，再验证后置并记录完成。重入不能只看“完成”就跳过实际
身份核对；如节点文件、Pod、CA或配置不再符合记录，先停止诊断。业务确认账本在
升级前建立，验证时只读回，不先补写新数据掩盖丢失。原调度状态要保持：原来cordon
的master不能顺便uncordon，原来可调度的worker在成功后恢复调度。

`next_api.py`观察API发现、streaming RBAC、gitRepo卷和DRA资源；它拒绝不支持的
消费者，但不删除资源或自动扩权。`next_config.py`只原子发布本跳实际推进的字段，
其它客户配置保持原文。不要以提高“成功率”为由把未知字段过滤掉或删除拒绝条件。

中断或失败必须保留第一个错误与durable状态，分类为产品、门禁、环境、供应链、
运行时或Kubernetes控制器问题。只修改已证明的因果层。一次被否决的修复不成为
后续基线；必须回收其代码、故障和测试残留。

## Harbor维护接口

`kubecli upgrade NAME --harbor OPERATION`独立调用`upgrade/product/harbor.yml`，
不会执行Kubernetes升级或全部安装角色。它只接受一个明确Harbor管理主机。
backup从原`harbor.yml`读取配置，其它操作从私有恢复receipt取原配置及CA，避免
目标模板覆盖旧身份。Ansible模块位于`upgrade/product/library/harbor_migration.py`，
在被管机只依赖Python标准库，不要求其安装项目应用或PyYAML。

| 操作 | 输入与真实前置 | 输出/幂等边界 |
| --- | --- | --- |
| `backup` | 健康原实例、原CA、尚不存在且位于数据树外的恢复目录；或经严格身份核对的无归档`stopped`检查点 | 完整停写备份；检查点仅续归档，不启动业务、不再次SAVE，拒绝覆盖旧恢复点 |
| `upgrade` | 同一原点receipt及SHA匹配的桥接/目标包 | 按官方迁移链推进；成功重入核对目标健康；中断检查点须先诊断 |
| `restore` | 完整原点文件、原主机/存储/证书身份 | 完整原点恢复，保留目标隔离目录；成功重入验证原版健康 |
| `export` | 已停止且一致的原备份边界 | 逻辑数据库及受控文件导出，不是跨CPU复制PG物理目录 |
| `import` | 干净目标、受控transfer目录、固定目标包 | 按受支持条件初始化逻辑目标；必须验证全部原账本 |
| `abort-import` | 本次import自有checkpoint及干净隔离条件 | 只撤回自有目标，不能借此清空原服务 |

restore/abort-import不能因为正在修复的矩阵尚pending而失去入口；恢复模块仍须
严格验证原checkpoint、文件摘要、机器、数据树及容器所有权。发行二进制另受已接受
release policy约束，不通过设置WIP或删除准入来绕过。

队列快照遵循实际引擎格式：旧Redis支持的RDB头与Valkey 9.0.3官方写入的
`VALKEY080`分别校验，不把Valkey误当Redis，也不推导Valkey快照可以降级回Redis。
官方依据见[固定源码记录](evidence/valkey-rdb-format.json)。`stopped`检查点续跑先核对
原主机、配置、全部停止容器和镜像身份，且不得存在任何归档/逻辑输出；只完成
原停止边界的归档。部分输出、未知phase或身份变化均拒绝，首失败保留。

### Compose、数据及命令边界

产品读取实际`docker-compose config --format json`，验证core、registry、数据库、
队列的bind源/目标，以及容器工作目录、服务名、镜像和CPU。官方短挂载写法可能
带尾斜杠，比较应按路径语义处理；不能把普通尾斜杠误报为越界，也不能将路径解析成
另一数据树后放行。外来容器接触数据树、旧镜像tag被替换或不明持久化条件都要拒绝。

新建角色在解包前保证主机具备`tar`与`gzip`；不能假定最小化OS镜像自带这些工具。
依赖安装只在新建分支执行，失败立即停止；既有运行实例不因幂等检查安装额外软件。
读取版本时以安装后的原配置认证，而非再次生成密码，认证结果同样须经过原CA校验。

官方prepare会迁移配置、生成Compose，启动core后才发生schema迁移。产品根据
真实数据库主版本与schema状态推进，不把“容器启动”当成数据迁移完成。
2.13/2.14的队列持久化与2.16 Valkey之间还需要核对实际引擎版本、RDB设置及keyspace。
额外DB、表空间、全局角色设置或未知持久化模式不在当前逻辑迁移路径内。

逻辑导出依赖官方Compose中的`log`服务：数据库使用`tcp://localhost:1514`的syslog
驱动，必须先启动日志服务，再单独启动数据库；不能在全部服务停止后只启动数据库。
成功和失败出口都停止数据库及日志服务，保持原容器身份、原完整快照和业务停写。
导出启动失败时保留`portable-export-intent`及首失败；仅在无已有导出文件、原备份
摘要和原停写容器身份均通过核验时重试。已有逻辑输出或未知目标checkpoint拒绝覆盖。
对应精确合同同时模拟syslog依赖、启动失败停回和不覆盖已有输出，不能只测试命令返回0。

命令输出可能包含凭据，只进入0600私有诊断日志。命令超时/中断要终止并回收本命令
进程组、保存首失败，不能留下prepare写入进程继续改变数据。prepare使用唯一命名容器，
只能按自身身份回收，不按宽泛名称删除其它容器。备份与隔离目录在业务恢复验证前
继续保留，文件hash应流式计算，避免一次读入大archive造成内存峰值。

### Harbor API与业务校验

本项目消费固定2.16的`/api/v2.0`接口和OCI Distribution协议。`systeminfo`用于版本，
`health`用于组件健康；projects、members、users、robots及artifact接口分别验证
项目身份、权限、用户、自动化身份及制品记录。业务ledger使用升级前原ID和原secret，
verify仅GET及客户端pull/拒绝push，不能创建缺失对象或刷新secret。

`systeminfo`的HTTP 200不保证包含版本：官方处理器仅对已认证请求返回受保护字段，
匿名结果会省略`harbor_version`。维护模块使用原`harbor.yml`中的管理员凭据访问
本机监听，仍以原域名校验证书及原CA，不经DNS访问可能尚未切换的另一实例。
凭据缺失、失效或实际版本不符时必须在停写备份前拒绝；不得从镜像tag猜测正在运行
的版本，也不得通过重置密码或关闭TLS使检查通过。

API写请求必须检查HTTP状态及响应，不把超时当成确定未提交。创建机器人响应的secret
只能私有持久化一次；结果未知时先按原对象查询确认，不能盲目创建第二身份。
Webhook、复制及异步扫描任务按其任务状态判断，HTTP请求被接受不等于后台完成。
项目不重试未知结果的破坏性API。扩展接口时先读
[固定版本Swagger](https://github.com/goharbor/harbor/blob/v2.16.0/api/v2.0/swagger.yaml)
和[开发说明](https://github.com/goharbor/harbor/blob/v2.16.0/api/v2.0/README.md)，
分页、资源名编码、权限及错误语义按实际接口分别处理。

Portal的API Explorer使用当前身份执行真实API，写操作会改变生产状态；它不是
只读诊断面板。探索接口时先选择自有非生产项目、核对HTTP方法与请求作用域，
再按固定Swagger的状态码和响应判断结果；未知结果先查询，不盲目重复写请求。

Harbor官方提供扫描器、复制适配器及Webhook等扩展接口。扩展会增加凭据、队列、
重试、失败恢复与升级兼容边界；当前迁移不注册新适配器。贡献流程、Helm构建或
第三方SDK示例可用于理解上游维护，但不能成为客户本项目的新执行入口。

兼容表按复制适配器的pull、push、代理等能力和引入版本分别列示，自动流水线覆盖、
官方身份服务验证与用户报告也分别解释。一个表格单元成立，不能推导某适配器的
全部方向、OIDC提供者或扫描器组合均已认证；新增消费者按实际接口补验证，不改变
本跳保留原配置和未启用功能的边界。

### 官方贡献与定制边界

本项目封装官方完整离线发行包，不编译或替换其中的Portal、Core、Registry等服务。
上游`make install`同时包含编译、镜像构建与启动，`make package_offline`则生成新的
发行包；它们不是本项目`kubecli setup`的等价命令。官方站点的编译示例仍含旧版
Python/Go最低要求，不能作为2.16的构建工具链依据；若以后确需维护上游补丁，应
从固定tag的构建定义重新确定工具链、CPU和签名来源，使用新的制品修订名重新验收。

Portal的品牌定制通过源码`setting.json`及图片资源进入前端构建，新增语言通过翻译
JSON和语言表进入构建；它们不是修改运行中`harbor.yml`即可生效的选项。上游明确
逐发行验证中英文，其它语言还需自行核对。本跳保持官方界面及语言资源，不交付
前端开发代理、品牌包或新增翻译；开发代理示例中的关闭后端证书验证不得进入生产。

官方贡献文档的前端本地启动、翻译PR、Hugo写作规范、Make开发目标及Python/Robot
E2E脚本接入，属于上游源码贡献流程，未采用为本项目维护入口。这些流程的执行
步骤从本次客户操作范围排除，但其共享API、TLS、错误与凭据边界仍由
[Harbor API与业务校验](#harbor-api与业务校验)覆盖。特别是上游旧E2E示例打印的
Authorization和请求体不得照搬到项目日志；本项目使用私有日志与固定runner。

## 验证与发布

本地检查须在导入产品前设置临时日志目录，Python使用`-B`，退出回收。例如仅检验
Harbor离线输入和业务夹具时：

```bash
task_log_dir=$(mktemp -d /tmp/kubeauto-upgrade-test.XXXXXX)
trap 'rm -rf -- "$task_log_dir"' EXIT
KUBEAUTO_LOG_DIR="$task_log_dir" .venv/bin/python -B -m unittest \
  upgrade.tests.test_harbor_session upgrade.tests.test_harbor_inputs \
  upgrade.tests.test_harbor_fixture upgrade.tests.test_harbor_inventory -v
```

先过语法、配置解析、实际模板/Compose渲染和最窄确定性合同，再进入持租约的固定
runner。runner自身是交付代码，需要验证首失败停止、模块前后置、拒绝外来所有权、
中断、durable状态、证据归档及清理交接。只返回成功marker的stub不能证明真实handoff。
固定现场入口位于`upgrade/lab/`，测试合同位于`upgrade/tests/`；不将测试脚本塞入
组件制品仓的生产升级目录。

Compose独立资格的清理不调用Kubernetes销毁入口。先停止自有容器、清理归属明确的
数据，并在控制机保留逐文件SHA256核验的完整恢复档案；确认没有Kubernetes服务、
数据或剩余容器后，才由固定主机清理器回收运行时。恢复档案按同主机的已完成快照
复用相同内容，不能按阶段名称推断新旧，也不能把失败的半成品当作恢复依据。
清理补测保留独立attempt及durable状态；父运行的历史失败不得改写为成功。

每个子项结束立即收取日志、exit/finalized、实际业务和清理证据。原始敏感材料受控
保存在被Git忽略的归档，正式JSON引用其SHA256及边界。父项只有全范围覆盖才签PASS；
独立AMD64迁移资格不能替新建或最终验收签名。本次运行验收范围为x86；ARM64保留官方来源、签名、双架构制品与静态检查，明确未经项目原生实测。状态读取：

```bash
.venv/bin/python -B upgrade/validate.py upgrade/v1.36.6/test-matrix.yaml --status
```

最终验收沿用`next-control.sh`的`final`入口，先校验UPG-01至14全部有效。
同一组共享锁内依次完成Kubernetes主链、新建和真实入口校验及清理，再交接Harbor
迁移、原点恢复、新建、两种逻辑导入及清理。只有两段的全部声明阶段均在第一次尝试
成功、durable为0且所有权释放后，才写组合回执并输出`UPGRADE_NEXT_FINAL_PASS`。
资格阶段的预期PDB故障、失败修复子运行不重签为干净最终结果。

两个独立作用域之间若发生交接门禁失败，先保留原父日志和退出状态，证明前一段的
全部阶段均首次成功、限定清理完成，且后一段从未取得节点所有权。只有产品、二进制、
配置和其余门禁输入逐项未变时，才能在正式验收计划的`final_kubernetes_reuse`中绑定
原run和原输入清单摘要，由同一个`final`入口续接。它复验七步原始日志、durable与清理
标志，拒绝失败/重试阶段、依赖漂移、已有Harbor状态和活动owner；在新的完成记录中
保留两个作用域的真实run身份，原失败父状态不变。续接仍须从干净节点完整执行Harbor
六阶段及清理。任何产品或制品输入变化都不能使用此限定恢复路径。

镜像制品仓发布统一使用各自`build.yml`的annotated tag入口；主仓产品二进制使用
`release.yml`与精确产品版本tag。普通分支push不触发构建。发布前
核对精确分支/SHA、mode及实际允许集合；selected/changed只处理本次新增、变化或
已证明缺失/失效的制品，完整既有tag复用。两端都有制品时不因历史记录或Git SHA变化
重新打镜像；一端缺失按现有digest补推。完整模式不等于本次授权整仓重建。

15项全部有效后，在持有原始验收证据的受控工作区导出发行policy；该文件可提交，
只含接受身份与摘要，不含私有日志或凭据。示例在主仓根目录执行：

```bash
.venv/bin/python -B - <<'PY'
from pathlib import Path
from upgrade.product.release_policy import export
root = Path.cwd()
export(root, root / 'upgrade/v1.36.6/test-matrix.yaml',
       root / 'upgrade/v1.36.6/release-policy.json')
PY
```

导出会重验15项完整证据，任一pending/stale即拒绝。干净CI和产品源码包不携带私有
原始证据，因此PyInstaller配方只验证已导出的policy、精确矩阵摘要、产品源码及运行
数据摘要，不重新打开实验室资料。缺少policy、矩阵或产品输入变化均拒绝；改变代码
后须先完成实际受影响验收再重新导出，不能手改policy。独立tools、文档和原始证据
不进入产品构建输入摘要；该摘要仅验证当前发行输入，不改变既有image:tag复用规则。

15项全部有效后才构建正式产品。产品镜像仓使用同一目标版本
分支及准确产品tag。主仓release只构建kubecli；产品运行镜像继续保留现有`build.py`
生成的完整dist，不能在此次升级中删除独立tools的既有打包能力。
tag发布是有外部结果的动作，不能用检查源码的成功代替Actions真实结论与manifest回查。

## 后续版本维护

下一minor以最新已接受版本的正式交付和适用维护基线为源，先核对dev独立功能与
六仓版本配对，再按主仓分支规则建立唯一正式版本线。不得从稳定1.33线重新做下一跳，
也不得无审查地以未验收dev快照代替已交付源。旧正式分支、tag和原证据保留供客户恢复。

维护`research.yaml`中的四类官方资料目录与版本差异，未变结论按依赖复用；新增主题
消化到三份手册的实际章节。每个变更明确原主张、变化输入、最小反证与受影响ID，
不能因一个hash变化作废所有业务证据。官方tested、supported、最低版本、稳定API、
源码pin与项目实测分别记录，缺少官方逐版表时沿已授权边界补最小集成，不能编造认证。

最终收尾核对六仓分支、HEAD、用户改动、临时目录、远程租约、进程、容器和归档。
IDE看见上游隐藏文件时先检查嵌套Git/证据排除，不批量删除正式原始证据。临时clone
放工作区外；需保存的固定源码使用无`.git`快照，运行日志和缓存不得进入提交。

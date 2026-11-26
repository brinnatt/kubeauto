# Kubeauto delivery contract

These instructions apply to the entire repository.

## Start here

本文件是六仓共享交付规则的唯一维护入口；各仓 AGENTS.md 只补本仓职责、固定入口和差异。
兄弟仓文件不会因目录相邻而自动继承，进入任一兄弟仓须显式阅读本文件。
同一任务已读且未变的规则直接复用，不因互相引用反复通读。
用户当次授权优先；子目录规则细化所属路径，不能扩大授权、混用矩阵或削弱共享准入。
发现实质冲突时先核对当前分支的实现与权威来源，修正冲突后再执行相关动作，不自行任选一条。

| 任务 | 专用入口与验收归属 |
| --- | --- |
| 核心功能维护 | 下列核心测试入口与 `tests/enterprise-test-matrix.yaml` |
| 中间件 | `docs/middleware/delivery-playbook.md` 与所属组件矩阵、固定 runner |
| 独立工具 | [tools 边界](#independent-tools-boundary-and-delivery-path)、`tests/tools-test-matrix.yaml`、`tests/run_tools_regression.sh` |
| 既有组件维护、逐 minor 升级和版本交付 | 当前正式版本分支的 `upgrade/AGENTS.md`；升级专用材料归 `upgrade/` |
| 镜像或二进制打包 | 所属兄弟仓 AGENTS.md、README、有效 workflow 与清单 |
| 文档或提示词 | [文档标准](docs/documentation-quality-standard.md)，只做内容与必要静态检查 |

先判定本次任务归属，再读对应入口；读取入口不触发实现、构建或现场测试。
跨仓读取先核对配对分支，稳定 `master/main`、正式版本与 `dev` 的版本上下文见
[分支规则](#版本分支与交付收尾)。checkout 不匹配时先用 `git show <ref>:<path>`
读取对应版本，不擅自切换他人活动工作区，也不拿相邻仓的当前文件冒充配对版本。
稳定分支没有 `upgrade/` 时，先只读定位已授权的版本分支，不为读取入口补建目录。

Before changing core code or running a core lab test, read:

1. `tests/README.md`
2. `tests/enterprise-test-matrix.yaml`
3. The relevant production code and unit/contract tests
4. The matching official documentation and source for the pinned component version

For a new middleware branch, also read and follow `docs/middleware/delivery-playbook.md`.

### 增量交付与范围判定

本节统一规定六仓的任务范围、制品复用和发布准入。各仓验收阶梯、交付规范及临时脚本均在本次用户授权范围内适用，不得自行增加全量构建或无关回归前置条件。

1. 六仓是兼容性审计单元。实际修改、构建、发布和回归按本次变更及已证明的依赖影响确定；“发布单元”“制品前置”“完整验收”均不授权重建全部镜像或回归无关功能。
2. 执行前区分本次新增、变化、已证明缺失或失效、可复用的制品，确定允许发布集合。外部镜像以完整 `image:tag` 为交付身份：核对目录映射及当前双端双架构 manifest 后复用，不要求历史发布记录或旧 digest 一直可读。仅一端缺失时按现有端 digest 补推，两端明确缺失才构建；既有 tag 不覆盖，打包修订使用新 tag。构建输入 fingerprint 只校验一次运行内 plan/run 一致性，不因 Git SHA 或历史指纹变化自动重建；只有前三类进入构建集合，不能将本组件消费的全部镜像直接当成重建集合。依赖未知先做影响分析；网络、认证或证据读取失败先定位原因，不得据此默认重建。
3. 任何可能触发发布的入口（含 tag 推送和失败重跑）执行前，在对话中列明仓库、分支、SHA、实际 mode、镜像名单与数量及必要性，并核对实际发布集合包含于允许集合。集合无法确定或越界时停止触发；`changed` 也须核对真实计划，不能只凭模式名判断。增量任务使用 `changed/selected`；会处理整仓的 `full` 或其它入口，仅在用户明确授权本次整仓全量范围后使用，不从“开始实现和回归”推定授权。通用发布器的模式验收可用明确授权的轻量镜像名单覆盖限定范围的 `full`，须先证明实际计划未越界；这只验收发布器机制，不签整仓镜像构建。模式名不等于重建集合，已满足复用条件的制品仍复用。
4. 完整验收覆盖本次交付的全部适用矩阵项；新增中间件仍须一次最终干净完整回归。既有有效依赖证据按执行效率契约复用，共享契约变化只补受影响场景，不降低验收覆盖，也不扩大到无关产品。仅修改文档或提示词时按文档契约检查。
5. 范围解释、选型和评审建议默认在对话中给出，不新建 `internal/`、方案稿或状态报告。正式矩阵、必要验收证据和最终客户文档仍按各自契约维护。已授权范围内自主推进；确需扩大范围时，先完成可独立完成的工作，再说明具体增量及依据，取得对应授权。

### 企业文档质量契约

编写、修订或评审技术文档及提示词，统一执行
[`docs/documentation-quality-standard.md`](docs/documentation-quality-standard.md)；
中间件另读交付规范第九章。官方研究、正文、迭代、验收和模板直接按该标准执行，
不在六仓 AGENTS.md 复制另一套质量规则。维护指引时先定位规则的主要归属，
修正原条款并保留引用；不以追加同义段落解决冲突，不把历史状态写成当前指令。

### Independent `tools/` boundary and delivery path

`tools/` is a separate product surface, not part of kubeauto core or any
middleware branch. Every Python file below `tools/` is a complete standalone
CLI and must remain independently buildable, runnable and supportable.

- A tool may use only the Python standard library and explicitly documented,
  pinned third-party packages required by that tool. It must not import or call
  `common/`, `controller/`, `model/`, `service/`, `kubecli.py`, `runtime_hook.py`,
  another repository, or another `tools/` script/module. Do not add a shared
  helper to make tools appear independent; duplicate a small local helper when
  that is the clearest standalone design.
- `tools-onefile.spec` must produce one executable per script. Its analysis
  path, hidden imports and data files must not pull kubeauto application
  modules. A tool change is reviewed as a tool release even when core code is
  unchanged.
- Tools use the dedicated matrix and runner in `tests/tools-test-matrix.yaml`
  and `tests/run_tools_regression.sh`. They do not consume, alter or inherit
  PASS evidence from `enterprise-test-matrix.yaml`, middleware matrices, or
  the core `run_enterprise_regression.sh` path. The core and middleware gates
  must not call a tools gate as an implicit prerequisite.
- For a new tool or a changed scenario, its affected matrix items remain
  `pending` until review approval; unchanged accepted evidence follows the
  shared reuse rules. No live mutation of that scope is allowed before approval.
  The review package must include the exact product
  command, expected marker, official documentation/source reference, fixture
  boundary, cleanup scope and smallest disproof command for every case.
- The tools ladder is: matrix/schema validation and AST import-boundary check;
  Python syntax/import smoke and focused deterministic tests; isolated CLI
  contract tests; applicable clean per-tool integration scenarios; the owning
  matrix's final clean affected-scope regression; then scoped cleanup verification.
  New tools qualify every applicable item; maintenance reuses unaffected evidence
  under the shared scope rules, without an implicit all-tools rerun. A failure stops at the
  first unmet prerequisite and is classified before code changes.
- Tool tests must exercise the tool's own CLI entry point. Direct `kubectl`,
  `docker`, database clients, SSH or broker commands are diagnostics/fixtures
  only and cannot replace tool evidence. External services and temporary
  fixtures are owned by the tools runner and are removed on success, failure
  and interruption.
- A tools delivery PASS requires current matrix evidence for every applicable
  item, durable runner `rc=0`, zero failure markers, all required success
  markers, SHA256/build provenance for frozen artifacts, and final
  `TOOLS_CLEAN_VERIFY_PASS`. Historical Tier3 `--help` evidence is not a tools
  delivery sign-off.

## Non-negotiable delivery rules

- Every feature that consumes external images must inventory its production, upgrade, rollback, backup, performance and test-infrastructure dependencies before live testing. Follow the scope and reuse rules above. For artifacts that need publication, add official-source, version-pinned Dockerfiles under the owning functional directory in `kubeauto-ext-images-dockerfile`, register the dual-push CI entries and pass the catalog validator. Reused and newly published artifacts must both have verified TalkEdu and Docker Hub manifests/digests before the normal live gate; do not discover missing images during live testing.
- Prefer `hub.talkedu.cn` and Huawei mirrors for the China delivery path; retain Docker Hub/upstream fallbacks. Downloads must be checksum-verified and atomically replaced so partial files cannot be accepted.
- Preserve the pinned accelerator and fallback configuration in already-delivered code unless a bug is proven with current evidence. For a newly added middleware branch, public accelerators and proxy mirrors are test-time, runtime-only aids because they may disappear or be blocked without notice; never persist a dynamically discovered accelerator in that branch's production code, CI, documentation or default configuration. Inject temporary test sources through non-persisted runtime parameters and verify checksums or manifest digests before use.
- Use the smallest readable, maintainable, Pythonic change consistent with upstream design.
- Prove a suspected defect at its owning product entry point through the cheapest sufficient deterministic reproduction or verified clean lab boundary. A healthy runner-owned baseline may be reused under the efficiency contract; do not require live testing to prove a syntax or deterministic CLI defect. Classify failures through the test-gate ladder.
- A failed attempted fix is not a new baseline. Remove its speculative code and lab residue before continuing.
- Track acceptance and schedule work through the matrix and evidence rules in
  [the execution-efficiency contract](#test-engineering-and-execution-efficiency-contract).
- Ceph is an explicitly approved exception: preserve the nine modular documents
  in `docs/storage/ceph/`. Fix proven documentation bugs only; do not restructure
  or consolidate them to match the general middleware document layout.

## Test-engineering and execution-efficiency contract

测试排程、依赖证据复用、验收汇报、首失败处理和最终验收的唯一规则正文在
[测试手册的执行契约](tests/README.md#test-gate-change-protocol)。涉及测试设计、
门禁或验收时读取该节；单纯文档/提示词维护不因此启动产品回归。

### Mandatory test-gate change ladder

门禁修改依次执行[测试手册的唯一阶梯](tests/README.md#mandatory-test-gate-change-ladder)，
本处保留历史链接入口，不复制步骤。矩阵必须保持全部适用能力的验收覆盖；
阶段性子项通过不代替完整交付。

## Fixed lab authority

测试前按所属 runner 取得主机、磁盘及共享资源租约；只读盘点不等于租约。
禁止借用外来集群或清理身份未知的资源，固定 runner 负责限定范围的前后复验。

- Development only: `192.168.47.129` (`brinnatt`); never use it as a test node.
- Jumper: `192.168.47.130` (`root`, Rocky Linux 8.10).
- Workers: `192.168.47.131-133` (`root`, Rocky Linux 8.10).
- Masters: `192.168.47.134-136` (`root`, Rocky Linux 8.10).
- Debian: `192.168.47.128` (`brinnatt`, passwordless sudo).
- Reserved-resource host: `192.168.47.137` (`root`, Rocky Linux 8.10, 8 CPU / about 16 GiB as currently provisioned).
- Ubuntu all-in-one/control/large-memory host: `192.168.47.138` (`ubuntu`, passwordless sudo).
- Anolis compatibility control: `192.168.47.141` (`root`, verified Anolis OS 23.3).
- openEuler compatibility control: `192.168.47.142` (`root`, verified openEuler 22.03 LTS-SP4).
- openSUSE compatibility control: `192.168.47.143` (`root`, verified openSUSE Leap 16.0).
- Supplemental large-capacity pool (available to this single Codex session for
  middleware, Prometheus, EFK and other resource-heavy tests):
  `192.168.122.2`, `192.168.122.217`, `192.168.122.243`, `192.168.122.246`,
  `192.168.122.193`, `192.168.122.210`, `192.168.122.216` (all currently
  Rocky Linux 9.8, `root`; `.2` is the control host and the other six form the
  disposable cluster pool). These hosts are not assigned to a permanent
  feature; the runner must lease them per scenario and prevent overlap.
- Ceph-priority shared pool (available to this single Codex session; each host
  has three additional test disks): `192.168.122.135` (`ceph-01`),
  `192.168.122.40` (`ceph-02`), `192.168.122.72` (`ceph-03`),
  `192.168.122.212` (`mceph-01`), `192.168.122.165` (`mceph-02`) and
  `192.168.122.238` (`mceph-03`) (all `root`). Prefer this pool for Ceph
  because its extra disks are reserved for destructive storage scenarios, but
  it remains a shared pool and may be leased to other tests when ownership,
  disk allowlists and cleanup boundaries do not overlap.
- Additional Ceph OS-qualification controls supplied by the delivery owner:
  Rocky Linux 10.2 `192.168.47.145` (`root`), Ubuntu 24.04
  `192.168.47.146` (`ly`, authorized root via sudo), and Ubuntu 26.04
  `192.168.47.147` (`ly`, authorized root via sudo). `192.168.47.145` remains
  Ceph-only. The delivery owner additionally authorized `.146` and `.147`
  for the shared Longhorn V2 pool below. Use BatchMode key authentication; do not persist
  their passwords. A qualification result requires the actual host OS and
  kernel to pass the secure msgr2 plus `aes256k` client probe.
- Longhorn V2 shared pool, explicitly authorized on 2026-10-20:
  `192.168.47.146` and `192.168.47.148` (Ubuntu 24.04.3), and
  `192.168.47.147` (Ubuntu 26.04), each with one additional 100 GiB raw disk.
  The supplied account is `ly` with administrative authority; existing root
  BatchMode keys are verified and preferred. Never persist supplied passwords.
  These hosts must be leased against overlapping Ceph qualification before
  mutation. Admit each disk by verified stable hardware identity, never merely
  its kernel device name. Read-only inventory is not a disk lease or V2 PASS.
- Independent Longhorn V2 DR target, explicitly authorized on 2026-10-20:
  `192.168.47.149` (Ubuntu 24.04, supplied account `ubuntu24` with sudo authority).
  Use the fixed runtime-only SSH/sudo bootstrap helpers and BatchMode keys;
  never persist the supplied password. Keep its cluster and lease independent
  from the `.146`–`.148` source. Verify actual memory, kernel >=6.7, an unused
  raw disk and stable hardware identity before admission; provisioning alone
  is not qualification or permission to wipe an unverified disk.
- `192.168.122.1` is permanently forbidden. Any address not explicitly listed
  in this section is forbidden as a test machine, inventory host, SSH target,
  Registry endpoint or fallback. Historical fixtures containing other
  `192.168.122.*` addresses must not be reused.
- Disable reserved resources on small-memory 131-136. Exercise the real 2 CPU + 4 GiB reservation behavior only on 137/138; keep the customer sizing baseline (16 CPU / 32 GiB) distinct from the smaller lab capacity.
- Use key authentication with `BatchMode=yes`. Do not copy the existing lab fallback credential into new files or print it in logs/output.
- Snapshot restores may remove SSH keys on all test hosts. Restore access through the fixed lab bootstrap helper using the runtime-only `LAB_SSH_PASSWORD`; never persist the fallback credential.

## Authoritative commands

- Unit tests: `bash tests/run_unit_tests.sh`
- Full autonomous delivery sign-off: `bash tests/run_enterprise_regression.sh --all-delivery`
- Core topology regression: `bash tests/run_enterprise_regression.sh`
- Live status: `bash tests/run_enterprise_regression.sh --status`
- Live log: `bash tests/run_enterprise_regression.sh --follow`
- Ansible OS compatibility probe/gate: `bash tests/run_enterprise_regression.sh --ansible-os-probe` / `--ansible-os-only`
- Cleanup: `bash tests/helpers/lab-wipe-nodes.sh`
- Cleanup verification: `bash tests/helpers/lab-wipe-nodes.sh --verify`

Use the fixed runner instead of ad-hoc SSH command sequences. It owns source sync, durable remote state, foreground streaming, heartbeat, diagnostics, cleanup, and evidence collection.

上述命令按所属矩阵、[执行效率契约](#test-engineering-and-execution-efficiency-contract)及
[测试门禁阶梯](#mandatory-test-gate-change-ladder)使用，不是每次任务的必跑清单。

Do not commit runtime logs, PID files, Python caches, remote cluster directories, or registry data. Preserve reusable knowledge in tests, helpers, the matrix, or documentation. 本地单元/合同测试（含直接调用 unittest 的聚焦测试）须在导入产品模块前将 `KUBEAUTO_LOG_DIR` 指向本轮临时目录，并在退出时回收；沿用 `tests/run_unit_tests.sh` 或 `upgrade/README.md` 的既有隔离方式，不把测试日志留在工作区。上游源码核对所需的 Git clone 放在工作区外；需保留在证据目录中的固定源码使用不含 `.git` 的快照，不遗留嵌套仓库或相应 IDE VCS 映射。已有正式证据绑定的原始日志不得作为临时垃圾删除。

## 六仓协作与安全边界

请使用中文协作；适用资料与执行入口按本文件开头的任务分路读取。

本次任务首次进入六仓时，完成一次六仓信息审计：读取以下六份 AGENTS.md，检查各仓 `git status --short`、当前分支及 HEAD，并阅读本次依赖所涉及的 Dockerfile、有效 workflow 和版本定义；按配对分支读取；后续按相关输入变化补查，交付前再次核对六仓状态。读取不等于授权修改，保护已有用户改动和正在运行的其它任务。

| 仓库（均位于 `/home/brinnatt/projects`） | 权威职责与依赖 |
| --- | --- |
| `kubeauto` | 产品配置、版本常量、安装与运维入口、测试矩阵和实验室运行手册；`common/constants.py`、`conf/config.yml`、`tests/README.md`、`tests/enterprise-test-matrix.yaml` |
| `kubeauto-dockerfile` | 按 kubeauto release tag 构建产品运行镜像；Dockerfile、`.github/workflows/build_and_push.yml` |
| `kubeauto-k8s-bin-dockerfile` | Kubernetes 六项二进制；Dockerfile、`multi-platform-download.sh`、`.github/workflows/build.yml` |
| `kubeauto-ext-bin-dockerfile` | `/extra` 扩展二进制及校验清单；Dockerfile、下载脚本、workflow；依赖 sp1 固定制品 |
| `kubeauto-ext-bin-sp1-dockerfile` | `/ext-bin` 中 nginx、chrony、keepalived 编译制品；Dockerfile、workflow；由 ext-bin 消费 |
| `kubeauto-ext-images-dockerfile` | 外部组件及其运维、升级、测试镜像目录；Dockerfile、CI matrix、catalog validator、发布证据 |

各仓可以独立开发。跨仓变更先列出实际依赖映射、受影响版本/消费者、验证入口及清理边界，再按依赖顺序推进。缺少某仓、指引或依赖证据时先定位本地路径和现有文档，明确缺口，不自行假定兼容。

### 版本分支与交付收尾

本节是六仓分支、目录与收尾的单一规则，升级策略和兄弟仓入口引用本节，不另设副本。
发布通道要求用于新建或修改的入口；历史分支以本分支实际 README/workflow 为准，
提示词维护不授权迁移构建实现或改变旧版本 pin。

- 默认稳定分支保留已交付的 Kubernetes `v1.33.6` 基线：主仓使用现有 `master`，其余仓使用 `main`，不为统一名字新增重复分支。它们只接收已核验适用的修复、优化与交付维护，禁止整条合并升级版或 dev 的组件 pin；升级专用镜像目录留在对应版本分支和 dev，原 GA 的精确 tag、构建输入及历史证据保留。
- `dev` 是最新已接受版本的集成线，接收新功能和后续升级成果；集成前核对实际依赖仓、正式版本分支、原 dev 独立提交与适用证据，采用保留双方历史的合并，不能强制重置丢失已有提交。合入 dev 不等于客户生产验证，也不自动晋升默认稳定分支或启动发布。
- 下一跳从上一正式版本已接受并冻结的六仓精确组合建立。若需吸收 dev 已接受的新功能或通用交付改进，先盘点双方独有提交、组件 pin、消费者和证据适用性；仅在 dev 增量全部适用于该版本时保留双方历史合并，否则选择性回移。完成受影响合同及必要的最小验收后，在原正式版本分支记录维护基线，绑定六仓源码、实际制品及证据，再作为下一跳来源。不得删除重建旧分支、直接从未验收 dev 拉下一跳，或因结构变化整体作废历史证据；纯分支、目录或文档整理不触发全量构建与无关回归。原 release、源基线和现场记录保留原身份，新修订单独记录，不把源码冻结声称为新镜像发布。
- 稳定源码和旧制品是恢复材料，不构成 Kubernetes 原地降级保证。客户恢复必须使用升级前配置、证书、控制面快照及一致的业务/卷恢复点，按原组件已验收恢复路径执行；不得以切换 Git 分支或旧镜像替代数据与控制面恢复。

- 同一仓库、同一 Kubernetes 版本只维护一个正式版本分支。同仓发布与只读核验复用既有通用 workflow 的模式/job，版本和组件选择使用清单或参数；不得为单个组件或版本新增专属 workflow、发布脚本及重复 Dockerfile。镜像制品仓的通用入口须完整提供 `selected/changed/full/verify/readback`；新增组件维护目录和所属配方，不缩减既有模式或另建发布通道。不为规避 workflow 触发而长期保留 `-readback`、`-verify` 等同版本分支。确需临时分支或 worktree 时先说明隔离原因和回收条件；收尾时核对独立提交、内容归属、活动任务及证据引用，将有效改动纳入正式分支，再删除本次临时的本地和远端引用、移除干净的临时 worktree。历史证据保留原 SHA/run 身份，不随分支整理重签。
- 兄弟仓的组件行为夹具、模拟服务、故障注入和 smoke 归本仓 `tests/`；`upgrade/` 仅保留正式发布清单、配方、执行器及交付回读，不混入测试工具。主仓跨 minor 专用测试仍按 `upgrade/AGENTS.md` 归 `upgrade/tests/`；目录整理同步当前引用，历史证据保留原路径和身份。
- `kubeauto` 与 `kubeauto-dockerfile` 是产品开发与打包的一对；每条在维护的 Kubernetes 版本分支必须同名配对，并核对打包源码与该基线的对应关系。Kubernetes 分支名不等于产品 release tag；正式打包必须使用属于该基线的已发布产品修订，不覆盖旧 tag，也不把旧 tag 打出的镜像声称为新基线。六仓均须维护与主仓一致的正式 Kubernetes 版本分支，即使制品未变也建立同名分支，指向该版本核验采用的兼容源码；后续版本从上一正式版本延续，不从可能含其它新特性的 dev 盲目取基线。分支对齐不等于组件升级或制品发布，未变制品继续复用。
- 开发、测试、回归、文档和教案等所有任务均须完成工作区收尾。交付前复核本地及远端正式分支、工作区和临时资源；不能仅凭 `git status` 为空认定干净，须盘点未跟踪及被忽略的实际文件，检查嵌套 `.git`（含软链接）、IDE VCS 映射及已登记 worktree。忽略规则不是清理措施，切分支也不会自动回收未跟踪数据；不得通过补充 ignore 隐藏跨分支残留。按路径核实独立改动、活动任务和证据归属，回收本次临时文件、缓存、仓库、重复映射和过期 worktree，保护用户原有改动及正式证据。需保留且依赖分支内路径的原始证据放在仓库外、用途明确的隔离工作区，以已验收的精确提交使用 detached HEAD，不占用用户的正式分支，也不新增重复分支；迁移前后核验文件清单、内容摘要和原验收入口，记录存放入口与回收条件，禁止删除或改签历史证据。用户日常 checkout 不承载这类证据和运行目录；收尾实际验证用户日常 checkout 可切换相关正式分支，再切回约定分支，复查 Git 状态和物理目录，在稳定 master/main 下确认本次升级残留不存在。未覆盖的仓库、分支或工作区不得声称已检查。推送、核验和删除分支均须检查真实 Actions 触发条件，分支对齐不扩大镜像发布或产品回归范围。
- 六仓 Actions 统一由显式推送匹配的 Git tag 触发，生效 workflow 及备用模板均只声明 `push.tags`；不保留分支 push、定时或手动 dispatch 旁路。普通提交、合并及分支对齐不得启动 Actions。发布与独立只读 workflow 使用不相交的 tag 前缀；共用 workflow 必须显式选择只读 mode，核验失败不转为发布。tag 只选择明确版本或冻结名单，禁止回落为隐式整仓构建。tag 删除不执行发布，既有 tag 不移动、不覆盖。具体命名及参数归所属仓入口，打 tag 仍须满足既有发布集合与验收前置。


## GitHub 认证与发布证据入口

本开发环境的 `ext-image-delivery` fine-grained GitHub 凭据位于 `/home/brinnatt/.ext-image-env`（应为文件属主私有、权限 `0600`）。它是受保护的凭据材料，**不是保证可 source 的 shell env 文件**。不得提交、复制到兄弟仓/远程节点、打印内容、启用 shell trace，或把 token 放入命令行、URL、日志和异常输出。

需要访问私有仓 Actions 时，先检查现有 `gh` 认证或该文件的存在与权限；在进程内存中读取并只向 `api.github.com` 的 Authorization header 注入凭据。使用 GitHub REST API 官方版本 `2022-11-28`；下载 artifact 的签名重定向 URL 时不转发 Authorization header。凭据名称不代表对所有仓都具备权限：核验目标仓最小读取/操作权限，401/403 分类为认证/授权问题，不当作网络不可达。已有授权内自主读取发布证据；不得因开发机 Docker Hub 不通就索要已提供的凭据、扩大 token 权限或重建成功制品。

Git 源码传输和 Actions API 使用不同认证边界。现有 `origin` 使用 GitHub SSH；先通过 `GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10' git ls-remote --heads origin` 核验目标分支和 SHA，再使用同一固定 Git 入口提交已验证的所属仓变更。Actions token 的仓库元数据即使显示 `push/admin`，也不能证明 token 获得 `contents=read/write`；遇到 `Resource not accessible by personal access token` 时检查 `X-Accepted-GitHub-Permissions`，并复用现有 SSH Git 认证，不索取重复凭据或扩大 token 权限。源码推送前审查明确文件列表，保留无关用户改动；普通分支推送后核对远端 ref/SHA，不等待不存在的 Actions；只有本次动作实际触发 workflow 时，才由 Actions 凭据读取并核验真实 run、SHA 和制品。

发布证据保留 run URL、结论、输入 fingerprint、两端双架构 digest、artifact SHA256 和 durable rc；失败先按所属仓阶梯定位，再按范围判定规则选择修复或重跑集合。凭据读取方法依照官方认证说明：https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api 。本地路径仅为认证入口，客户文档和镜像中不得包含它或任何秘密。

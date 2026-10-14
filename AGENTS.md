# Kubeauto delivery contract

These instructions apply to the entire repository.

## Start here

Before changing code or running a lab test, read:

1. `tests/README.md`
2. `tests/enterprise-test-matrix.yaml`
3. The relevant production code and unit/contract tests
4. The matching official documentation and source for the pinned component version

For a new middleware branch, also read and follow `docs/middleware/delivery-playbook.md`.

### 企业文档质量契约

编写、修订或评审技术文档前，必须读取并执行 `docs/documentation-quality-standard.md`；中间件同时遵守交付规范第九章。该标准是六仓文档质量的单一权威来源，适用于客户内容、操作步骤、原理解释、参考及文档相关提示词。

- 按交付版本、能力和客户任务确定有限研究范围，通读相关官方主题、参考和发布说明；依赖只展开实际消费的接口与限制。按主题复用来源与结论，清晰的官方解释消化后直接重组，源码仅核实具体歧义、版本差异和集成行为；不递归官网导航或重复审计。缺口如实说明，不用链接代替解释。
- 以 Ceph 九模块文档的机制解释和生产闭环为质量标杆。按读者任务组织新手成功路径、常用生产操作、进阶专题、参数速查和故障入口；逐节讲清机制因果、选择依据、操作、结果、限制与恢复，不能只列功能和命令。
- 准确且多使用 Mermaid mindmap、flowchart、sequenceDiagram、stateDiagram 或数据图表解释复杂关系，渲染并核对图文；不以字数、图数、标题齐全或关键词作为质量验收。
- 生产步骤必须明确版本、角色、位置、上下文、输入来源、前置检查、产品入口、等待/超时、预期业务结果、失败停止、回退和复验；性能、稳定性、安全、容量、数据保护与特殊场景均须有可判断的依据。
- 来源覆盖、内容评审、静态检查和操作证据分别判断。两类读者路径检查默认是文档检查，不操作现场；独立评审按本次明确要求安排。复用适用的现有产品证据，只有新增或改变操作行为时才补最小验证；产品 GA 不能替代文字质量评审，文档修改也不重复产品 GA。
- 文档工作默认只做官方资料理解、正文整理、一次完整检查及具体缺陷修正；不默认增加代理、开发测试门禁、发布验证镜像或搭建实验室。新增中间件仍按既有产品契约验收，文档不另建测试工程。
- 文档工作不授权修改无关功能或 Ceph 九份标杆文档；按实际影响执行最小验证，纯标准变更不启动实验室回归。

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
- Before review approval, the tools matrix remains `pending` and no live lab
  mutation is allowed. The review package must include the exact product
  command, expected marker, official documentation/source reference, fixture
  boundary, cleanup scope and smallest disproof command for every case.
- The tools ladder is: matrix/schema validation and AST import-boundary check;
  Python syntax/import smoke and focused deterministic tests; isolated CLI
  contract tests; one clean per-tool integration scenario; one clean complete
  tools regression; then scoped cleanup verification. A failure stops at the
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

Inspect all six sibling repositories under `/home/brinnatt/projects` before and after a change. Preserve unrelated user changes.

## Non-negotiable delivery rules

- Treat the six repositories as one release unit. Audit constants, image tags, Dockerfiles, GitHub Actions, TalkEdu private-registry push, Docker Hub push, and fallback order after every relevant change.
- For every future feature branch that depends on external images, make `kubeauto-ext-images-dockerfile` an artifact prerequisite rather than discovering images during live testing. Inventory production, upgrade, rollback, backup, performance and test-infrastructure images; reuse an existing exact pin or add an official-source, version-pinned Dockerfile under the owning functional directory; register it in the dual-push CI matrix; pass the catalog validator; and verify the TalkEdu and Docker Hub manifests/digests after publication before starting the normal live gate. Prefer the TalkEdu copy in China. Treat a dynamic public mirror only as a temporary runtime bridge before fixed artifacts are published; never make repeated public downloads the normal test path.
  Example (illustrative): a MySQL test may reuse published `mysql:8.0.46`, `mysql:8.4.x` and `mysql:9.x` images; a minimal Rocky Linux image may package a large file such as `StarRocks-3.5.12-centos-amd64.tar.gz`; Kafka tests may package `kafka_2.13-4.3.1.tgz` or `apache/kafka:4.3.1`. GitHub Actions, running against international upstream sources, can build and dual-push the same immutable artifact to the TalkEdu Huawei Cloud registry and Docker Hub. After the dual-push succeeds, China test and delivery paths pull `hub.talkedu.cn` first for high-speed access and retain the verified Docker Hub/upstream fallback, so normal testing does not depend on repeated downloads or continued public-network availability.
- Prefer `hub.talkedu.cn` and Huawei mirrors for the China delivery path; retain Docker Hub/upstream fallbacks. Downloads must be checksum-verified and atomically replaced so partial files cannot be accepted.
- Preserve the pinned accelerator and fallback configuration in already-delivered code unless a bug is proven with current evidence. For a newly added middleware branch, public accelerators and proxy mirrors are test-time, runtime-only aids because they may disappear or be blocked without notice; never persist a dynamically discovered accelerator in that branch's production code, CI, documentation or default configuration. Inject temporary test sources through non-persisted runtime parameters and verify checksums or manifest digests before use.
- Use the smallest readable, maintainable, Pythonic change consistent with upstream design.
- Reproduce from a verified clean lab before calling a failure a product bug. Separate test-gate, environment, supply-chain, runtime, Kubernetes, controller, and product failures with evidence.
- A failed attempted fix is not a new baseline. Remove its speculative code and lab residue before continuing.
- Track acceptance per matrix item. Mark a module pass as soon as its product
  gate has current success markers, durable module rc=0, zero failure markers
  and verified cleanup of its owned fixtures; do not wait for unrelated modules
  or the final full run. On a proven related change, return only affected items
  to pending and record the dependency and disproof command. Evidence for
  unchanged dependencies remains valid; unknown dependencies require impact
  analysis, not automatic reuse or blanket invalidation. Module progress does
  not substitute for final clean full-run sign-off.
- Customer-facing middleware documentation covers exactly four content types: user manual, operations manual, technical whitepaper and development manual. Keep the technical whitepaper and development manual separate; combine the user and operations content into one `operations-manual.md` when the component follows the established PXC style. Integrate official references into the applicable whitepaper or development manual. Do not deliver proposal/review drafts, retrospectives, project-status narratives, internal test chronology, standalone source indexes or an extra README as customer documentation. Write from the customer's product, task and operational perspective with formal terminology, continuous executable main paths and clearly quoted exception/rollback/risk branches.
- Ceph is an explicitly approved exception: preserve the nine modular documents
  in `docs/storage/ceph/`. Fix proven documentation bugs only; do not restructure
  or consolidate them to match the general middleware document layout.

## Test-engineering and execution-efficiency contract

Test helpers, runners, templates, matrix validators and diagnostics are production
delivery code. A test-gate bug that consumes a clean lab run is a delivery defect,
not an acceptable testing cost. Preserve 100% acceptance coverage; reduce waste by
ordering evidence correctly, never by skipping scenarios.

- Before any stateful or full-chain run, every changed test artifact must pass its
  cheapest relevant contract checks: shell syntax, YAML/JSON parsing, template
  rendering, resource name/namespace/ownership assertions, marker/exit-state
  assertions and focused unit tests. A static contract failure blocks live work.
- New or changed assertions must first run in the narrowest focused branch that
  proves the assertion against its owning product path. Only after that branch is
  green may the fixed runner start one clean full middleware regression. Do not
  restart a full lab merely to discover syntax, quoting, resource-name, namespace,
  port-forward, selector or marker mistakes.
- Exercise customer behavior through the product entry point (`config.yml` plus
  the documented `kubecli` workflow). Direct Helm, kubectl or SSH may provide
  diagnostics, fixtures or independent API checks, but cannot replace evidence
  that the product configuration rendered and converged correctly.
- On every failed gate, preserve its first failure, durable state and diagnostics;
  classify it as product, test-gate, environment, supply-chain, runtime or
  Kubernetes/controller failure before changing code. A test-gate fix requires a
  regression unit/contract test for the exact cause before any rerun. Change one
  causal layer at a time and remove rejected attempts and lab residue.
- Use the runner's durable status, log and focused modes to reuse a validated
  boundary. A focused repair restores and verifies only the failed module's
  owned objects, faults, flags and processes; retain healthy shared prerequisites
  and unchanged completed evidence. Before reuse, verify ownership, dependency
  identity, shared service health, data integrity and absence of fault residue.
  Rebuild only the smallest dependent environment when these checks fail or
  cannot prove isolation. Do not duplicate setup or repeat unrelated scenarios.
  A complete clean regression remains mandatory after accepted changes, with its
  terminal marker, durable `rc=0`, zero failure markers, current matrix evidence
  and final `LAB_CLEAN_VERIFY_PASS`.
- Never optimize by downgrading coverage, accepting incomplete module evidence
  or treating a running Pod as a business result.

### Mandatory test-gate change ladder

This ladder is mandatory whenever a helper, runner, fixture, test template,
diagnostic, matrix validator or test assertion changes.  It applies equally to
new middleware and maintenance of delivered branches.

1. Classify the proposed change before running it: product code, test-gate code,
   environment/supply-chain, or a combination.  Record the owning scenario, the
   exact product entry point, the expected success marker, and the smallest
   command that can disprove the assumption.  Do not describe a test-gate
   failure as a product failure without that proof.
2. Complete the static preflight appropriate to every changed file before any
   remote mutation: shell syntax and strict-mode control flow; Python import and
   focused unit tests; YAML/JSON parsing; Ansible syntax; Helm/template render;
   resource name, namespace, selector, port, image, ownership, marker and exit
   contracts.  Add or update a deterministic unit/contract test for the exact
   defect before its next live execution.
3. Run the narrowest fixed-runner branch that reaches the changed assertion.
   Its setup, module-scoped cleanup and diagnostics remain runner-owned. A failed preflight
   or focused gate blocks a full middleware run; fix the single proven causal
   layer, re-run its contract, then re-run only that focused branch.
4. Finish the affected module gates before starting one final clean full
   middleware regression. A healthy runner-owned baseline is reusable for
   focused gates; an existing cluster alone is not residue. The runner must
   prove no stale fault, foreign resource or failed-module fixture remains.
   The final full run still requires its normal clean setup and final cleanup.
5. After a full-chain failure, collect the first failing command, durable exit
   state, rendered inputs, owned-resource state, Pod descriptions/events and
   relevant controller logs before editing.  Stop the scenario at a failed
   prerequisite: never spend the remaining stages of an invalid run merely to
   collect secondary failures.

Every live-launch update must state the completed ladder level, the evidence
that unlocked it, and the next permitted level.  No test-only shortcut may
replace the product entry point, and no direct command may silently mutate a
customer path merely to make a test convenient.  The full acceptance contract
still requires current clean evidence for every affected matrix item; this
ladder removes invalid executions, not customer coverage.

## Fixed lab authority

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
  `192.168.47.147` (`ly`, authorized root via sudo). Use only for the
  Ceph qualification branch after BatchMode key bootstrap; do not persist
  their passwords. A qualification result requires the actual host OS and
  kernel to pass the secure msgr2 plus `aes256k` client probe.
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

Every stateful scenario requires verification of its declared cleanup scope
before and after execution. A focused module may preserve a verified healthy
runner-owned baseline, but must emit its scoped-clean marker and durable rc=0.
Final delivery requires the complete clean regression, expected terminal marker,
durable rc=0, zero failure markers, current matrix evidence and final
`LAB_CLEAN_VERIFY_PASS`; individual module passes do not imply delivery PASS.

Do not commit runtime logs, PID files, Python caches, remote cluster directories, or registry data. Preserve reusable knowledge in tests, helpers, the matrix, or documentation.

## 六仓协作与安全边界

请使用中文协作。先读适用的父目录及本仓 AGENTS.md，再读测试矩阵、运行手册和所涉及固定版本的官方文档、源码。不得用历史 PASS 或猜测替代当前证据。

每次进入六仓中的任一仓，先完成一次六仓信息审计：读取以下六份 AGENTS.md，检查各仓 `git status --short`、当前分支及 HEAD，并阅读本次依赖所涉及的 Dockerfile、有效 workflow 和版本定义；交付前再次检查。读取不等于授权修改，保护已有用户改动和正在运行的其它任务。

| 仓库（均位于 `/home/brinnatt/projects`） | 权威职责与依赖 |
| --- | --- |
| `kubeauto` | 产品配置、版本常量、安装与运维入口、测试矩阵和实验室运行手册；`common/constants.py`、`conf/config.yml`、`tests/README.md`、`tests/enterprise-test-matrix.yaml` |
| `kubeauto-dockerfile` | 按 kubeauto release tag 构建产品运行镜像；Dockerfile、`.github/workflows/build_and_push.yml` |
| `kubeauto-k8s-bin-dockerfile` | Kubernetes 六项二进制；Dockerfile、`multi-platform-download.sh`、`.github/workflows/build.yml` |
| `kubeauto-ext-bin-dockerfile` | `/extra` 扩展二进制及校验清单；Dockerfile、下载脚本、workflow；依赖 sp1 固定制品 |
| `kubeauto-ext-bin-sp1-dockerfile` | `/ext-bin` 中 nginx、chrony、keepalived 编译制品；Dockerfile、workflow；由 ext-bin 消费 |
| `kubeauto-ext-images-dockerfile` | 外部组件及其运维、升级、测试镜像目录；Dockerfile、CI matrix、catalog validator、发布证据 |

各仓可以独立开发。独立变更只修改所属产品；跨仓变更先列出实际依赖映射、受影响版本/消费者、验证入口及清理边界，再按依赖顺序推进。六仓是兼容性审计单元，不意味着每次都重建全部镜像或回归无关功能。缺少某仓、指引或依赖证据时先定位本地路径和现有文档，明确缺口，不自行假定兼容。

涉及制品时核对消费者常量/模板、Dockerfile 固定版本、有效 Actions 触发条件、AMD64/ARM64、TalkEdu 与 Docker Hub tag/digest、下载校验和回退顺序。国内路径优先 `hub.talkedu.cn/kubeauto`，保留已验证的 Docker Hub/上游回退；禁止把临时发现的公共代理固化到新功能。新增中间件须先读 `kubeauto/docs/middleware/delivery-playbook.md`，制品通过后才能进入现场门禁。

不修改不相关功能、既有制品、workflow 或 Ceph 文档结构。只有当前证据证明缺陷时，才在明确的影响范围内修复。拒绝的修复须撤回其代码和测试残留。测试和发布使用所属仓的固定入口，保留首失败、durable 状态、成功标志与清理证据；禁止以其它仓 PASS 替代本仓验收。实验主机白名单、共享资源租约和清理权限以 `kubeauto/AGENTS.md` 为准；没有租约不得借用现存集群，开发机不得作为测试节点。

## GitHub 认证与发布证据入口

本开发环境的 `ext-image-delivery` fine-grained GitHub 凭据位于 `/home/brinnatt/.ext-image-env`（应为文件属主私有、权限 `0600`）。它是受保护的凭据材料，**不是保证可 source 的 shell env 文件**。不得提交、复制到兄弟仓/远程节点、打印内容、启用 shell trace，或把 token 放入命令行、URL、日志和异常输出。

需要访问私有仓 Actions 时，先检查现有 `gh` 认证或该文件的存在与权限；在进程内存中读取并只向 `api.github.com` 的 Authorization header 注入凭据。使用 GitHub REST API 官方版本 `2022-11-28`；下载 artifact 的签名重定向 URL 时不转发 Authorization header。凭据名称不代表对所有仓都具备权限：核验目标仓最小读取/操作权限，401/403 分类为认证/授权问题，不当作网络不可达。已有授权内自主读取发布证据；不得因开发机 Docker Hub 不通就索要已提供的凭据、扩大 token 权限或重建成功制品。

Git 源码传输和 Actions API 使用不同认证边界。现有 `origin` 使用 GitHub SSH；先通过 `GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10' git ls-remote origin refs/heads/dev` 核验读取、目标分支和 SHA，再使用同一固定 Git 入口提交已验证的所属仓变更。Actions token 的仓库元数据即使显示 `push/admin`，也不能证明 token 获得 `contents=read/write`；遇到 `Resource not accessible by personal access token` 时检查 `X-Accepted-GitHub-Permissions`，并复用现有 SSH Git 认证，不索取重复凭据或扩大 token 权限。源码推送前审查明确文件列表，保留无关用户改动；推送后由 Actions 凭据读取并核验真实 run、SHA 和制品。

发布/触发前核对仓库、分支、SHA、workflow mode 与所选镜像数量。记录 run URL、结论、输入 fingerprint、两端双架构 digest、artifact SHA256 和 durable rc；失败先按所属仓阶梯定位，不全量重跑。凭据读取方法依照官方认证说明：https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api 。本地路径仅为认证入口，客户文档和镜像中不得包含它或任何秘密。

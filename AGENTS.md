# Kubeauto delivery contract

These instructions apply to the entire repository.

## Start here

Before changing code or running a lab test, read:

1. `tests/README.md`
2. `tests/enterprise-test-matrix.yaml`
3. The relevant production code and unit/contract tests
4. The matching official documentation and source for the pinned component version

For a new middleware branch, also read and follow `docs/middleware/delivery-playbook.md`.

### 增量交付与范围判定

本节统一规定六仓的任务范围、制品复用和发布准入。各仓验收阶梯、交付规范及临时脚本均在本次用户授权范围内适用，不得自行增加全量构建或无关回归前置条件。

1. 六仓是兼容性审计单元。实际修改、构建、发布和回归按本次变更及已证明的依赖影响确定；“发布单元”“制品前置”“完整验收”均不授权重建全部镜像或回归无关功能。
2. 执行前区分本次新增、变化、已证明缺失或失效、可复用的制品，确定允许发布集合。核对精确版本、目录映射、构建策略/context fingerprint、目标架构、发布记录及当前双端 digest 后复用；只有前三类进入构建集合，不能将本组件消费的全部镜像直接当成重建集合。依赖未知先做影响分析；网络、认证或证据读取失败先定位原因，不得据此默认重建。
3. 任何可能触发发布的入口（含自动 push 和失败重跑）执行前，在对话中列明仓库、分支、SHA、实际 mode、镜像名单与数量及必要性，并核对实际发布集合包含于允许集合。集合无法确定或越界时停止触发；`changed` 也须核对真实计划，不能只凭模式名判断。增量任务使用 `changed/selected`；`full` 或其他会重建整仓的入口，仅在用户明确授权本次整仓全量范围后使用，不从“开始实现和回归”推定授权。
4. 完整验收覆盖本次交付的全部适用矩阵项；新增中间件仍须一次最终干净完整回归。既有有效依赖证据按执行效率契约复用，共享契约变化只补受影响场景，不降低验收覆盖，也不扩大到无关产品。仅修改文档或提示词时按文档契约检查。
5. 范围解释、选型和评审建议默认在对话中给出，不新建 `internal/`、方案稿或状态报告。正式矩阵、必要验收证据和最终客户文档仍按各自契约维护。已授权范围内自主推进；确需扩大范围时，先完成可独立完成的工作，再说明具体增量及依据，取得对应授权。

### 企业文档质量契约

编写、修订或评审技术文档前，必须读取并执行 `docs/documentation-quality-standard.md`；中间件同时遵守交付规范第九章。该标准是六仓文档质量的单一权威来源，适用于客户内容、操作步骤、原理解释、参考及文档相关提示词。

- 按交付版本、能力和客户任务确定有限研究范围，通读相关官方主题、参考和发布说明；依赖只展开实际消费的接口与限制。按主题复用来源与结论，清晰的官方解释消化后直接重组，源码仅核实具体歧义、版本差异和集成行为；不递归官网导航或重复审计。缺口如实说明，不用链接代替解释。
- 以 Ceph 九模块文档的机制解释和生产闭环为质量标杆。按读者任务组织新手成功路径、常用生产操作、进阶专题、参数速查和故障入口；逐节讲清机制因果、选择依据、操作、结果、限制与恢复，不能只列功能和命令。
- 准确且多使用 Mermaid mindmap、flowchart、sequenceDiagram、stateDiagram 或数据图表解释复杂关系，渲染并核对图文；不以字数、图数、标题齐全或关键词作为质量验收。
- 白皮书必须消化交付范围内全部适用的官方技术白皮书、架构原理、设计与生产资料，先解释核心产品的技术体系，再解释 Operator 和本项目集成；不得用功能概述、官网链接或集成例外代替机制推导。
- 写作前核对当前矩阵、evidence 和绑定原始日志，将相关交付能力的真实实践、输入输出、测量值、故障及恢复结果转化为对应章节正文。采用“图解/实践/真实数据 + 详细因果解释”帮助客户理解和判断；只挂证据链接、只写应测项目或仅称 GA 不合格。测量条件、错误与重试、实测与示意及不可外推边界必须明确，不重跑已有有效回归取材。具体执行标准第 3.1、7.1、8、11、12 节。
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

## Non-negotiable delivery rules

- Every feature that consumes external images must inventory its production, upgrade, rollback, backup, performance and test-infrastructure dependencies before live testing. Follow the scope and reuse rules above. For artifacts that need publication, add official-source, version-pinned Dockerfiles under the owning functional directory in `kubeauto-ext-images-dockerfile`, register the dual-push CI entries and pass the catalog validator. Reused and newly published artifacts must both have verified TalkEdu and Docker Hub manifests/digests before the normal live gate; do not discover missing images during live testing.
- Prefer `hub.talkedu.cn` and Huawei mirrors for the China delivery path; retain Docker Hub/upstream fallbacks. Downloads must be checksum-verified and atomically replaced so partial files cannot be accepted.
- Preserve the pinned accelerator and fallback configuration in already-delivered code unless a bug is proven with current evidence. For a newly added middleware branch, public accelerators and proxy mirrors are test-time, runtime-only aids because they may disappear or be blocked without notice; never persist a dynamically discovered accelerator in that branch's production code, CI, documentation or default configuration. Inject temporary test sources through non-persisted runtime parameters and verify checksums or manifest digests before use.
- Use the smallest readable, maintainable, Pythonic change consistent with upstream design.
- Reproduce from a verified clean lab before calling a failure a product bug. Separate test-gate, environment, supply-chain, runtime, Kubernetes, controller, and product failures with evidence.
- A failed attempted fix is not a new baseline. Remove its speculative code and lab residue before continuing.
- Track acceptance and schedule work through the matrix and evidence rules in
  [the execution-efficiency contract](#test-engineering-and-execution-efficiency-contract).
- Customer-facing middleware documentation covers exactly four content types: user manual, operations manual, technical whitepaper and development manual. Keep the technical whitepaper and development manual separate; combine the user and operations content into one `operations-manual.md` when the component follows the established PXC style. Integrate official references into the applicable whitepaper or development manual. Do not deliver proposal/review drafts, retrospectives, project-status narratives, internal test chronology, standalone source indexes or an extra README as customer documentation. Write from the customer's product, task and operational perspective with formal terminology, continuous executable main paths and clearly quoted exception/rollback/risk branches.
- Ceph is an explicitly approved exception: preserve the nine modular documents
  in `docs/storage/ceph/`. Fix proven documentation bugs only; do not restructure
  or consolidate them to match the general middleware document layout.

## Test-engineering and execution-efficiency contract

Test helpers, runners, templates, matrix validators and diagnostics are production
delivery code. A test-gate bug that consumes a clean lab run is a delivery defect,
not an acceptable testing cost. Complete the designed matrix and production
delivery requirements to reach GA readiness; preserve 100% acceptance coverage.
GA readiness, document acceptance and actual customer sign-off are separate
claims: submit reviewable evidence and never sign on the customer's behalf.

- New middleware must qualify its delivery path before its first live run.
  Follow [the standard onboarding sequence](docs/middleware/delivery-playbook.md#102-扩展步骤):
  compare the existing mature runners, reuse applicable supervision and recovery
  contracts, and declare only this product's differences in its existing matrix
  and tests. Its local contracts must prove actual launch scope, independent
  module entry points and handoffs, first-failure stopping, scoped recovery,
  dependency-based evidence invalidation, and the final complete-run path.
  A runner that can only restart a broad chain is not ready for live testing.
  Check the actual command plan against the authorized scope before execution;
  a mode name or an automated request to rebuild/retest is not authorization.
  A local change that unexpectedly invalidates unrelated evidence blocks the
  next launch until its impact is explained or the gate is repaired. Use the
  existing runners, matrix and focused contracts; this is not a requirement to
  build a new framework or repeat unrelated delivered branches.
- Plan execution from the matrix capabilities and remaining evidence gaps.
  Use existing matrix/evidence records to identify prerequisites, owning product
  entry points, host/disk leases, shared mutable resources, capacity budgets and
  success/cleanup exits. Serialize actual dependencies and conflicting mutations;
  run ready independent branches concurrently through fixed runners when their
  ownership, supervision and cleanup boundaries are proven. Bound concurrency
  by CPU, memory, disk, network and shared-control capacity. Do not leave an
  available authorized pool idle merely to follow matrix row order. This does
  not require a new scheduler, framework or additional agents.
- Reuse applicable official and project evidence under the delivery playbook's
  official-evidence rules. Check pinned inputs, dependency identities, scenario
  coverage and raw/durable evidence before reuse. Return only proven affected
  items to pending and record the dependency and smallest disproof command;
  unchanged dependencies retain valid evidence. Unknown dependencies require
  impact analysis, not automatic reuse or blanket invalidation. Official GA
  results establish upstream conditions, not a project PASS.
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
  Complete the clean regression required by the owning matrix after accepted
  product or test-gate changes; a new middleware requires its complete clean run.
  Require its terminal marker, durable `rc=0`, zero failure markers, current
  matrix evidence and final `LAB_CLEAN_VERIFY_PASS`.
- Isolate a failure to branches whose prerequisites or shared state it invalidates.
  Continue independent ready work after proving its boundary remains healthy.
  Mark each completed module pass promptly when its current product success
  markers, durable module `rc=0`, zero failure markers and scoped cleanup are
  verified. Preserve a failed parent's real status; bind independently accepted
  child evidence to its own inputs, logs and cleanup. A passing subcase signs
  only its declared scope, not an incomplete matrix item or the final delivery.
- Give every focused qualification or repair a matrix gap, minimal disproof
  and completion condition. Once satisfied, consume that evidence and advance
  the owning acceptance path; do not keep adding qualifications, repeat accepted
  audits or rebuild unrelated prerequisites without a proven gap or change.
  Continue authorized work toward the remaining matrix and final clean run.
- 矩阵是验收与汇报的唯一主线。固定汇报为：
  `矩阵验收 X/N PASS，待验收 Y 项（失败 F 项）；能力阶段 S/T；当前：矩阵 ID、名称、所属子项及执行状态；下一出口：…`。
  本条是各中间件进度汇报的单一规则；组件手册只补矩阵路径、只读状态命令及产品差异，
  不复制另一套计数定义。汇报前收取已结束模块的证据，再使用所属矩阵的固定只读入口
  校验并生成同一份状态快照；总数、分组和明细均取自该快照，不凭记忆、上次回复、
  摘要或日志 marker 心算。只有已核验的当前完整证据才能支持 PASS。
  N 是适用父项数，X 是其中 pass 数，Y 是 pending 与 fail 数之和（N=X+Y），
  F 是 fail 数且包含在 Y 内，不能再加一次。S 是所属父项全部 pass 的能力组数，
  T 是矩阵能力组总数；对用户解释为“已全部验收的能力组”，不称为本轮完成阶段。
  校验失败时改报“矩阵校验未通过，当前验收计数待核验”，列出拒绝的 ID、原因及最小
  修复出口；若引用原计数，必须标成“矩阵登记值，尚未通过当前校验”。不得继续使用
  正常 PASS 总览、擅自扣减猜测有效数、批量重置状态或为恢复计数修改旧证据。
  模块结束后立即收取、核验并更新矩阵，不等待父链结束；独立子模块通过不能改签父链结果。
  启动、阶段切换和异常时，用表格逐组列出已通过及未通过的 ID；能力组保留原 ID 并加中文
  含义，待验收项保留矩阵原 ID 和名称。表格另列当前子项、实际执行状态及下一出口；
  较长的已通过清单可压缩为连续 ID 区间，不能隐藏未通过项或用执行模块名替换能力组。
  子项沿父项原 ID/名称汇报，不增加总分母，未完成父项不能计 PASS；子项独立通过须说明
  其证据和剩余子项，没有独立记录时只能称“已观察到成功标志，尚待核验”。
  矩阵状态与最近运行结果分别说明：父项 pending、最近尝试失败时，同时报该 ID 的 pending、
  失败 run ID/退出码及失败子项；“矩阵失败 F=0”不表示运行从未失败。执行中、未启动、
  阻塞、子项已完成但父项待验收必须明确区分，不自行向矩阵增加状态字段。
  最终完整回归仍是矩阵自身的最终项；只有该项运行时，额外标明其 run ID、
  `最终回归：已完成步骤/本轮计划步骤`，不得以它替换矩阵验收或能力阶段计数。
  保留旧日志不等于旧证据失效，也不等于当前 PASS；按实际依赖核验后复用。
  受影响项退回 pending 时，必须列出 ID、变更依赖、最小补测和计数变化；未知依赖先分析，
  不得整体归零，不以“历史覆盖/当前有效/本轮”三个未解释的总数代替矩阵汇报。
  每次计数变化同时说明“哪些 ID 从何状态变为何状态、X/S 前后值”；仅换 run ID、
  续跑或启动最终回归不改变已接受状态。计数未变时说明当前子项的实际进展，不制造增量。
  每轮执行进度绑定 run ID 和范围，从已接受的 ID 推导；拒绝混用运行或丢失同轮已完成项，
  保留原状态。实际并行分支、阻塞与下一出口必须据现场说明，尚未启动的分支不得称为并行中。
  During long waits, provide a heartbeat at least every 60 seconds, even when
  counts are unchanged. Identify the actual image/scenario, build/compile/test
  step, elapsed time, recent observable progress and next exit; shard counts or
  repeated "still running" messages alone are insufficient. Estimate remaining
  time from comparable measured stage durations, the critical path and elapsed
  time; state uncertainty and do not extrapolate linearly from stage counts.
  At a configured timeout, a material overrun against comparable measurements,
  or loss of observable progress, diagnose the first slow/stalled step and state
  the evidence for continuing, cancelling or repairing it. Do not shorten a
  required test window or bypass acceptance to meet an estimate. This reporting
  rule does not require extra tests, remote polling or a new progress framework.
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
   defect before its next live execution. For continuation or composed runners,
   that contract must also check the invoked modules' real preconditions,
   postconditions and refusal rules against the actual call order;
   success-marker stubs alone do not prove the handoff.
3. Run the narrowest fixed-runner branch that reaches the changed assertion.
   Its setup, module-scoped cleanup and diagnostics remain runner-owned. A failed preflight
   or focused gate blocks a full middleware run; fix the single proven causal
   layer, re-run its contract, then re-run only that focused branch.
4. Finish the affected module gates before starting one final clean full
   middleware regression. The fixed final launcher must verify current PASS
   evidence for every applicable prerequisite matrix item, including documentation;
   exclude only the final regression item itself. Missing, pending, failed or
   stale evidence refuses launch before any lab mutation. A contract must prove
   this refusal through the real launcher. A healthy runner-owned baseline is reusable for
   focused gates; an existing cluster alone is not residue. The runner must
   prove no stale fault, foreign resource or failed-module fixture remains.
   The final full run still requires its normal clean setup and final cleanup.
5. After a full-chain failure, collect the first failing command, durable exit
   state, rendered inputs, owned-resource state, Pod descriptions/events and
   relevant controller logs before editing. Stop the dependent scenario at a failed
   prerequisite: never spend the remaining stages of that invalid path collecting
   secondary failures. Independent work follows the branch-isolation rule above.

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

Every stateful scenario requires verification of its declared cleanup scope
before and after execution. A focused module may preserve a verified healthy
runner-owned baseline, but must emit its scoped-clean marker and durable rc=0.
Final delivery requires the complete clean regression, expected terminal marker,
durable rc=0, zero failure markers, current matrix evidence and final
`LAB_CLEAN_VERIFY_PASS`; individual module passes do not imply delivery PASS.

Do not commit runtime logs, PID files, Python caches, remote cluster directories, or registry data. Preserve reusable knowledge in tests, helpers, the matrix, or documentation.

## 六仓协作与安全边界

请使用中文协作。先读适用的父目录及本仓 AGENTS.md，再读测试矩阵、运行手册和所涉及固定版本的官方文档、源码。不得用未经适用性核对的历史 PASS 或猜测替代当前证据。

本次任务首次进入六仓时，完成一次六仓信息审计：读取以下六份 AGENTS.md，检查各仓 `git status --short`、当前分支及 HEAD，并阅读本次依赖所涉及的 Dockerfile、有效 workflow 和版本定义；后续按相关输入变化补查，交付前再次核对六仓状态。读取不等于授权修改，保护已有用户改动和正在运行的其它任务。

| 仓库（均位于 `/home/brinnatt/projects`） | 权威职责与依赖 |
| --- | --- |
| `kubeauto` | 产品配置、版本常量、安装与运维入口、测试矩阵和实验室运行手册；`common/constants.py`、`conf/config.yml`、`tests/README.md`、`tests/enterprise-test-matrix.yaml` |
| `kubeauto-dockerfile` | 按 kubeauto release tag 构建产品运行镜像；Dockerfile、`.github/workflows/build_and_push.yml` |
| `kubeauto-k8s-bin-dockerfile` | Kubernetes 六项二进制；Dockerfile、`multi-platform-download.sh`、`.github/workflows/build.yml` |
| `kubeauto-ext-bin-dockerfile` | `/extra` 扩展二进制及校验清单；Dockerfile、下载脚本、workflow；依赖 sp1 固定制品 |
| `kubeauto-ext-bin-sp1-dockerfile` | `/ext-bin` 中 nginx、chrony、keepalived 编译制品；Dockerfile、workflow；由 ext-bin 消费 |
| `kubeauto-ext-images-dockerfile` | 外部组件及其运维、升级、测试镜像目录；Dockerfile、CI matrix、catalog validator、发布证据 |

各仓可以独立开发。跨仓变更先列出实际依赖映射、受影响版本/消费者、验证入口及清理边界，再按依赖顺序推进。范围、制品复用及发布准入统一遵守[主仓增量交付规则](#增量交付与范围判定)，不在各仓另设全量前置条件。缺少某仓、指引或依赖证据时先定位本地路径和现有文档，明确缺口，不自行假定兼容。

涉及制品时核对消费者常量/模板、Dockerfile 固定版本、有效 Actions 触发条件、AMD64/ARM64、TalkEdu 与 Docker Hub tag/digest、下载校验和回退顺序。国内路径优先 `hub.talkedu.cn/kubeauto`，保留已验证的 Docker Hub/上游回退；禁止把临时发现的公共代理固化到新功能。新增中间件须先读 `kubeauto/docs/middleware/delivery-playbook.md`，制品通过后才能进入现场门禁。

不修改不相关功能、既有制品、workflow 或 Ceph 文档结构。只有当前证据证明缺陷时，才在明确的影响范围内修复。拒绝的修复须撤回其代码和测试残留。测试和发布使用所属仓的固定入口，保留首失败、durable 状态、成功标志与清理证据；禁止以其它仓 PASS 替代本仓验收。实验主机白名单、共享资源租约和清理权限以 `kubeauto/AGENTS.md` 为准；没有租约不得借用现存集群，开发机不得作为测试节点。

## GitHub 认证与发布证据入口

本开发环境的 `ext-image-delivery` fine-grained GitHub 凭据位于 `/home/brinnatt/.ext-image-env`（应为文件属主私有、权限 `0600`）。它是受保护的凭据材料，**不是保证可 source 的 shell env 文件**。不得提交、复制到兄弟仓/远程节点、打印内容、启用 shell trace，或把 token 放入命令行、URL、日志和异常输出。

需要访问私有仓 Actions 时，先检查现有 `gh` 认证或该文件的存在与权限；在进程内存中读取并只向 `api.github.com` 的 Authorization header 注入凭据。使用 GitHub REST API 官方版本 `2022-11-28`；下载 artifact 的签名重定向 URL 时不转发 Authorization header。凭据名称不代表对所有仓都具备权限：核验目标仓最小读取/操作权限，401/403 分类为认证/授权问题，不当作网络不可达。已有授权内自主读取发布证据；不得因开发机 Docker Hub 不通就索要已提供的凭据、扩大 token 权限或重建成功制品。

Git 源码传输和 Actions API 使用不同认证边界。现有 `origin` 使用 GitHub SSH；先通过 `GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10' git ls-remote origin refs/heads/dev` 核验读取、目标分支和 SHA，再使用同一固定 Git 入口提交已验证的所属仓变更。Actions token 的仓库元数据即使显示 `push/admin`，也不能证明 token 获得 `contents=read/write`；遇到 `Resource not accessible by personal access token` 时检查 `X-Accepted-GitHub-Permissions`，并复用现有 SSH Git 认证，不索取重复凭据或扩大 token 权限。源码推送前审查明确文件列表，保留无关用户改动；推送后由 Actions 凭据读取并核验真实 run、SHA 和制品。

发布证据保留 run URL、结论、输入 fingerprint、两端双架构 digest、artifact SHA256 和 durable rc；失败先按所属仓阶梯定位，再按范围判定规则选择修复或重跑集合。凭据读取方法依照官方认证说明：https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api 。本地路径仅为认证入口，客户文档和镜像中不得包含它或任何秘密。

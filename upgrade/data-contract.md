# 升级文件的数据契约

本文件解释方案数据如何维护。当前没有解析这些文件并操作产品的代码；任何
未来自动化都必须先实现并证明下列拒绝规则，不能把 YAML 可解析视为可执行。

## 1. 文件与单一归属

| 文件 | 权威范围 | 不负责的事实 |
| --- | --- | --- |
| `catalog.yaml` | 组件域、盘点入口、依赖关系和候选路线 | 不改变运行默认值，不签兼容/PASS |
| `official-sources.yaml` | 已取得的官方结论、边界与原文获取身份 | 不代表全部官方目录研究完成 |
| `v1.33.6/inventory.yaml` | 指定六仓提交的源码声明快照 | 不是客户实装、发布成功或 GA 复核 |
| 各版本 `release.yaml` | 源/目标、六仓与制品身份、决策、迁移和缺口 | 不自动覆盖生产常量或现存集群配置 |
| 目标 `research.yaml` | 官方四类目录及主题的阅读/排除、项目去向和增量 | 不是额外客户来源索引，不虚构阅读 |
| 目标 `acceptance-plan.yaml` | 依赖影响、旧证据关系和待执行范围 | 不持有另一个验收状态计数 |
| 目标 `test-matrix.yaml` | 本次升级主张的正式父项及其状态 | 不改签旧核心/中间件矩阵 |
| 源版 `evidence/` 与 `evidence-manifest.yaml` | 原正式记录和矩阵的字节副本、来源/原 ID/摘要 | 不改写原状态，不冒签目标实测；不提交运行日志或秘密 |
| 后续 `目标/evidence/` | 本次新增证据及复用适用性结论 | 不把已复制等同于原始归档闭包或目标 PASS |

生产当前默认版本仍以代码、配置及所属制品仓契约为准；release 是冻结交付清单，
实现时要校验两者一致。发现旧文档表格与源码不同，登记对应来源，不把旧表抄成
新真相，也不在本目录任务中批量修订无关文档。

## 2. 路径、时间与观察

所有 `repo/path`、`path`、`matrix_path` 等机器字段以主仓根目录为相对基准，
兄弟仓使用 `../仓库名/…`；Markdown 链接以所在文件为基准。
组件 `id` 对应 catalog 唯一 ID，引用既有矩阵必须同时有路径与原 ID。
`source_ids` 对应官方来源 ID，不能把主张只挂到一个无版本首页。
矩阵 `depends_on` 表示接受该父项证据所需的前置，不要求停止所有独立资料研究；
执行有状态步骤时仍须满足真实运行前置与租约，不能据此提前启动依赖场景。

`recorded_on` 是文件整理日期，`observed_on` 是数据实际观察日期，不能用更新文件
时间冒充重新取证。Git SHA 必须完整；网络材料可用固定 tag/commit 或内容摘要
绑定，滚动页面记录版本适用边界。官方页面缓存与稳定入口不同步时分别记录，
用 release/artifact 交叉核验，不悄悄拼接成同一天的官方完整状态。

## 3. 版本、研究与决策状态

- `schema_version` 是方案字段版本，当前为 `1`，不是产品版本。
- `null` 表示未知或尚未实现，`[]` 表示目前没有已确认条目；二者均不能推导出
  “不需要”。空发布集合只表示本轮没有发布授权，正式冻结须有 `scope_frozen`。
- 源记录 `kind: accepted-source-baseline`，其 `accepted_ga_baseline` 记录交付负责人
  确认的全功能 GA；`identity_verified` 只描述精确身份关联。目标 `kind: target-plan`。
  源档案的 `release_frozen: false` 表示身份记录待齐，不撤销既有交付 GA；目标
  冻结须关键约束与身份齐全，冻结也不代表目标产品已验收。
- 组件决策 `investigate/keep/update/bridge/replace/not-applicable` 分别表示待查、
  保持、更新、桥接、替换、有依据不适用；`keep` 也必须核验维护期、安全及支持。
- 研究 `pending/understood/excluded/unavailable` 表示阅读处置。`excluded` 须有
  官方适用依据；`understood` 须有机制、约束、正文/实现归属和版本关系，不能只填 URL。
- 正式矩阵只允许 `pending/pass/fail`。决策/研究状态与验收状态不是同一维度。
- `gaps[].blocks_through` 指该阶段的出口仍被此缺口阻断；不是要求等待该阶段才
  能研究或修复，也不阻塞已证明独立的工作。

## 4. 完整盘点与冻结规则

快照分层保存全部 `v_*` 字段、component_images 的解析结果、ext-bin/sp1 ENV/
FROM 版本、配置显式 pin、Python 固定依赖、外部镜像 catalog 全部映射及关键源码
摘要。它能证明“这些源码声明了什么”，不能证明已枚举模板/Chart/fixture 的所有
隐式运行依赖。目录 `inventory_gaps` 保留剩余盘点范围；闭合前不能宣称全组件
搭配矩阵已完成或冻结构建集合。

每个实际消费镜像需补充用途（production/upgrade/recovery/backup/performance/
test-infrastructure）、所属组件、触发条件、双端架构 digest、发布记录、构建策略与
context fingerprint、是否带自有补丁。catalog 全量存在不是允许全量重建的理由。
context 源码摘要只供定位，不能伪装为既有 CI 的 build-policy/context fingerprint。

`release.yaml` 的目标 `component_decisions` 对 catalog 每个组件域都有记录；
冻结时须逐具体组件/Chart/镜像展开，给出确切版本及两端/过渡支持依据。所有
仍受支持路径和共享控制器消费者必须有明确去向，不允许用空 target 隐式继承。

冻结迁移边至少具备：精确源/目标/桥接、官方升级依据、产品入口、依赖、动作
顺序、健康/数据判据、不可逆点和恢复路径。未实现的命令保持 null；不能用
任意 shell 字符串填满字段来假装已具备运行资格。

迁移边与证据引用必须按如下字段展开，不以一句“按官方升级”代替：

| 对象 | 冻结所需字段 |
| --- | --- |
| 组件 pin | component_id、名称/类型、准确版本、官方来源、Chart/镜像/二进制身份、架构、配套组合、维护期及自有补丁 |
| 迁移边 | edge_id、source、target、bridge、depends_on、official_source_ids、product_entry、preconditions、steps、success_conditions、irreversible_point、recovery、cleanup |
| 证据关系 | matrix_path + 原 ID、输入/依赖、原日志/归档 SHA256、durable/marker/清理、适用主张、差异、reuse/supplement/rerun 决策、最小补测 |
| 证据副本 | source_path、copy_path、sha256、bytes、kind、copy_verified、original_claim_refs；原路径按源根解释，副本内容不改写 |
| 场景组合 | 支持路径、具体拓扑/环境/架构/组件组合、覆盖子项、等价依据、资源预算、独立清理与失败出口 |
| 发布记录 | 所属仓库/提交、实际 mode、允许和实际镜像集、build-policy/context fingerprint、发布 run、两端双架构 digest 与制品 SHA256 |

## 5. 评审与变更传播

正式评审区分 `design_review`、`implementation_authorized`、`release_frozen`、
`product_acceptance`、`customer_signoff`，作者自检不能替代用户决策或客户签收。
用户已经接受的方案方向在范围内落实，不重复索要正常步骤授权；关键目标组合、
范围扩大和生产变更按实际授权管理，不从默认值推定。

修改一个事实时，只更新其主要归属和受影响引用。源码 SHA 变化要比较实际输入，
纯文档变化不使全部现场证据失效；源 pin、模板或补丁变化则分析实际依赖。
更新来源观察日期不自动更换目标版本。变更目标 patch/组件组合必须重新计算影响
并保存原依据，不覆盖旧 release/tag 或改写旧证据使其匹配新输入。

`planned_evidence_methods` 是父项的证明方式计划，不能按其中出现 `new-live-evidence`
就自动安排整条重跑。`new_live_steps: []` 与 `live_scope_frozen: false` 表示尚未
确定现场集合，不是零风险声明。`reuse_evidence` 当前列出已复制候选，只有明确
的断言、差异判断和适用性结论才能接受复用。`cost_control` 的空上限不授权现场。

归档副本为历史资料，校验其原状态、原路径和旧 schema 时使用原契约；不得把
目标 schema 强套给副本并要求改写。维护者先读清单和本次关联项，不重复通读
全部历史冻结输入。完整校验一次后，输入未变的检查结论可以按摘要复用。

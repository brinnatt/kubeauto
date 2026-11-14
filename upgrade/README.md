# Kubernetes 逐版本升级交付方案

本目录面向版本维护、开发、测试与交付人员，管理从稳定交付基线出发的逐 minor
升级。目标是在官方兼容边界内复用制品和有效证据，只补变化所影响的验证，保留
已交付 `v1.33.6` 的成果。当前是可评审的方案与盘点基线，没有实现新的升级能力。

## 阅读与工作入口

| 要完成的任务 | 文件 | 得到的结果 |
| --- | --- | --- |
| 进入升级维护任务 | [AGENTS.md](AGENTS.md) | 授权、归属、工作顺序与拒绝条件 |
| 决定版本、分支和维护政策 | [policy.md](policy.md) | 锁版规则、支持期、桥接与六仓身份 |
| 按步骤推进第一跳 | [workflow.md](workflow.md) | 每阶段输入、动作、停止与完成条件 |
| 决定复用和最小补测 | [acceptance.md](acceptance.md) | 证据适用性、回归范围与 GA 出口 |
| 查已复制的旧证据 | [v1.33.6/evidence-manifest.yaml](v1.33.6/evidence-manifest.yaml) | 正式副本、来源、摘要、原 ID 和未完成的适用性检查 |
| 维护 YAML 或评审其含义 | [data-contract.md](data-contract.md) | 字段、路径、状态、空值与权威来源 |
| 查组件和依赖入口 | [catalog.yaml](catalog.yaml) | 全局组件域、版本位置、消费关系与验收归属 |
| 查已核实官方约束 | [official-sources.yaml](official-sources.yaml) | 结论、版本、来源、获取日期和限制 |
| 区分当前源码与客户基线 | [v1.33.6/release.yaml](v1.33.6/release.yaml)、[inventory.yaml](v1.33.6/inventory.yaml) | 六仓只读快照、常量/镜像/制品/工具版本及未核验边界 |
| 确认第一跳候选与缺口 | [v1.34.13/release.yaml](v1.34.13/release.yaml) | 目标选择、组件决策和阻断项 |
| 完成官方资料研究 | [v1.34.13/research.yaml](v1.34.13/research.yaml) | 四类资料基线与增量的处理记录 |
| 制定并验收第一跳范围 | [v1.34.13/acceptance-plan.yaml](v1.34.13/acceptance-plan.yaml)、[test-matrix.yaml](v1.34.13/test-matrix.yaml) | 既有矩阵映射、最小反证、独立升级验收与最终范围 |

文件各自拥有一个事实。`catalog.yaml` 不复制版本快照，目标版本不默认继承旧版本
PASS，官方来源不代表项目实测，验收计划不代表验收已经通过。

交付负责人已确认 `v1.33.6` 的全部功能、组件、中间件和存储均已 GA，生产稳定。
这是本次升级的既定基线，不重新论证旧版本是否 GA。新版本的验收按具体主张
继承适用证据并补齐变化部分；证据身份待关联与旧版本是否 GA 是两件事。

## 当前路线与范围

2026-11-13 从 `dl.k8s.io` 查询得到的候选路线：

```text
v1.33.6 → v1.34.13 → v1.35.10 → v1.36.6 → v1.37.2
```

这里只创建源基线记录和第一跳目录。后续候选保存在 `catalog.yaml`，必须以上一跳
被接受的精确交付为前提再建立目标目录；候选不是发布承诺。2026-11-14 整理文件
沿用上述观察日期，没有假称当天重新查询稳定入口。

当前六仓 `dev` 快照中的默认 K8s 是 `v1.33.6`。客户历史 release、实际启用组件、
制品 digest 和原始验收证据仍需关联，不能把开发 HEAD 签成客户原始交付。
关联缺口只影响相应目标复用结论，不触发旧版本全量重测。
第一跳组件目标值尚未冻结，全部正式升级矩阵项保持 `pending`；方案审阅不产生现场证据。

正式副本目前为 133 份 JSON 记录和 10 份矩阵快照，逐字节校验结果见清单。
官方已取得资料保存在 `official-snapshots/`，由 `official-sources.yaml` 绑定摘要。
原始运行归档未在本轮整体复制/复核，目标适用性尚未全部接受；这两个边界不影响
已确认的源 GA，也不构成全量回归理由。现场步骤和费用上界仍待目标组合研究后
冻结，不能据当前组件清单推导测试天数或承诺节省比例。

## 目录独立与既有能力

新增升级专用文件、后续专用自动化和证据留在此目录。主项目、存储和中间件目录
继续负责生产实现，原 runner 保持归属。历史证据实际复制到
[`v1.33.6/evidence/`](v1.33.6/evidence/)，通过
[`evidence-manifest.yaml`](v1.33.6/evidence-manifest.yaml) 查来源、SHA256、原矩阵 ID
及复制边界。目标计划只引用 `upgrade/` 内副本；副本中的原路径不改写，按清单
映射定位。复制完整性与目标适用性分别审核。独立升级矩阵记录“从哪一
组合迁移到哪一组合”的新增主张，不取代组件自身的完整功能验收，也不修改其旧状态。

当前生产升级入口是 `config.yml + kubecli upgrade`。现有
[`--upgrade-only`](../tests/helpers/delivery-upgrade-smoke.sh) 证明固定补丁迁移，
不是这里规划的跨 minor runner。后续跨 minor runner 的真实命令、marker、资源
租约、清理范围和合同必须实现并通过准入后才能执行；本轮不提供虚假的可运行命令。

## 本轮可执行的本地检查

在主仓根目录、现有 `.venv` 环境中运行以下只读检查，不安装依赖，不连接实验室：

```bash
git diff --check
.venv/bin/python -B - <<'PY'
from pathlib import Path
import yaml

for path in sorted(Path('upgrade').rglob('*.yaml')):
    with path.open(encoding='utf-8') as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise SystemExit(f'YAML 根节点必须是 mapping: {path}')
    print(f'YAML 可解析: {path}')
PY
```

预期没有空白错误，每份 YAML 可解析。这些检查只证明文件格式；还须按文档标准
检查来源、链接、版本与职责一致性、首次维护者和熟练维护者路径。当前 YAML 是
声明式方案，没有配套执行校验器；格式通过不能解锁构建或现场工作。

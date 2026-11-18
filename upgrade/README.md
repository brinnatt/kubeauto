# Kubernetes 逐版本升级交付方案

本目录面向版本维护、开发、测试与交付人员，管理从稳定交付基线出发的逐 minor
升级。目标是在官方兼容边界内复用制品和有效证据，只补变化所影响的验证，保留
已交付版本的成果。`v1.33.6 → v1.34.13` 的原验收记录保留在对应版本目录；
当前授权任务是 `v1.34.13 → v1.35.10`，按该版本的固定范围实施和验收。
当前状态只取自目标矩阵，不把源 GA、方案冻结或单个模块成功当成目标交付通过。

当前维护入口为 [下一跳操作手册](v1.35.10/operations-manual.md)、
[固定组合](v1.35.10/release.yaml)、[验收范围](v1.35.10/acceptance-plan.yaml) 和
[升级矩阵](v1.35.10/test-matrix.yaml)。运行只读状态检查：

```bash
.venv/bin/python -B upgrade/validate.py upgrade/v1.35.10/test-matrix.yaml --status
```

下文首跳的计划状态、候选和子入口说明保留其编写时边界；不得将其当作当前下一跳
的执行授权或能力状态。各版本独立使用所属矩阵、正式证据和操作手册。

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
沿用上述观察日期。首跳在 2026-11-14 已再次核对 `stable-1.34.txt` 返回
`v1.34.13`；这不表示后续各 minor 当天均已重新查询。

当前六仓 `dev` 快照中的默认 K8s 是 `v1.33.6`。客户历史 release、实际启用组件、
制品 digest 和原始验收证据仍需关联，不能把开发 HEAD 签成客户原始交付。
关联缺口只影响相应目标复用结论，不触发旧版本全量重测。
第一跳完整组件组合尚未冻结。源身份关联已取得正式记录，Kubernetes 二进制批次
已独立构建发布；实时验收状态只用下方只读命令查询，方案审阅不产生现场证据。

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

当前生产升级入口是 `config.yml + kubecli upgrade`。旧
[`93.upgrade.yml`](../playbooks/93.upgrade.yml) 现已限定为同 minor 的补丁升级：
读取每台 master 实际进程版本，核对本地二进制与配置目标，拒绝 minor 变化及降级。
这道保护不实现首跳迁移；合同见
[`legacy-upgrade-boundary-contracts.json`](v1.34.13/evidence/legacy-upgrade-boundary-contracts.json)。现有
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

# Check formal upgrade records; upstream snapshots can be multi-document manifests.
paths = [*Path('upgrade').glob('*.yaml'), *Path('upgrade').glob('v*/*.yaml')]
for path in sorted(paths):
    with path.open(encoding='utf-8') as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise SystemExit(f'YAML 根节点必须是 mapping: {path}')
    print(f'YAML 可解析: {path}')
PY
```

预期没有空白错误，每份升级声明 YAML 可解析。这些检查只证明文件格式；还须按文档标准
检查来源、链接、版本与职责一致性、首次维护者和熟练维护者路径。当前 YAML 是
声明式记录。新增只读校验与合同入口如下：

```bash
.venv/bin/python -B upgrade/validate.py upgrade/v1.34.13/test-matrix.yaml --status
.venv/bin/python -B - <<'PY'
import os
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix='kubeauto-upgrade-unit-logs-') as log_dir:
    result = subprocess.run(
        [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'upgrade/tests', '-v'],
        env={**os.environ, 'KUBEAUTO_LOG_DIR': log_dir},
    )
raise SystemExit(result.returncode)
PY
.venv/bin/python -B upgrade/validate.py upgrade/v1.34.13/test-matrix.yaml --combination-preflight
```

第一条必须先通过证据校验才输出同一份状态快照；发现陈旧输入时拒绝输出正常
PASS 总览，不改写矩阵。第二条把产品模块导入和测试夹具产生的运行日志隔离到临时目录，
退出时清理，测试结果仍输出到终端；聚焦运行单个测试时沿用同一隔离方式。
它不访问网络或实验室；制品合同需要相邻的
`kubeauto-k8s-bin-dockerfile` checkout，缺失时会明确跳过，不能据此接受制品。
第三条检查冻结声明、精确组件覆盖、显式 pin、已取得的官方快照摘要和迁移/恢复字段；
当前组合尚未冻结，预期返回 1 并列出缺口，不修改矩阵。它只证明记录完备和身份，
不能认证官方兼容或替代内容评审。
`--require-pass` 检查全部父项，`--final-prerequisites` 额外执行组合检查及执行入口检查，
并排除最终项自身；当前前置未齐时二者预期拒绝。
上述命令不构建、不发布、不升级集群，也不能替代真实最终 launcher 的拒绝合同。

源只读预检子入口位于 [`product/kubernetes_preflight.py`](product/kubernetes_preflight.py)。
它校验相邻 minor、目标 kubectl 摘要与客户端版本，逐个检查显式提供的 API 后端
精确源版本及 `/readyz`，再核对完整预期节点集合、Ready 与 kubelet 精确源版本。
健康判定依赖 HTTP 请求成功，不解析面向人工的 verbose 正文；TLS 校验始终开启。
本地合同通过真实 CLI 和隔离的 kubectl 响应夹具验证，命令如下：

```bash
.venv/bin/python -B -m unittest upgrade.tests.test_kubernetes_preflight -v
.venv/bin/python -B upgrade/product/kubernetes_preflight.py --help
```

成功输出 `UPGRADE_SOURCE_PREFLIGHT_OBSERVED_SUCCESS`，以独占方式创建权限 `0600`
的 JSON receipt；失败返回 `1`、输出 `UPGRADE_SOURCE_PREFLIGHT_REFUSED`，不覆盖
旧 receipt，不回显可能含凭据的 kubectl 错误内容。已有文件须保留并换用新的
run 所属输出路径，不删除旧证据以通过检查。
此子入口尚未接入 `config.yml + kubecli upgrade`，也不负责发现全部 API 后端、
控制器/proxy 身份、组件组合、制品准入、租约、容量、业务账本和恢复点；其成功
不授予变更许可，不签署 UPG-05。混合版本源必须进入后续 journal 恢复路径，
不能将它当成初始健康源接受。实际实验室命令仍由通过资格的固定 runner 提供。

配置到源预检的内部连接见 [`product/inventory.py`](product/inventory.py) 和
[`product/inventory.yml`](product/inventory.yml)。它通过实际产品 runner，由 Ansible
解析同一份 `hosts + config.yml`，使用实际生效的节点名模板和各 master 的安全端口，
生成完整节点映射与 API 后端清单。它比较 inventory 的完整组成员与实际观察事件，
拒绝 `--limit` 隐藏节点、重复节点名、未评审地址别名及运行时/网络混用；AIO
同一主机的多角色会去重，但不遗漏角色。随后调用上述源 API/节点预检，不手工
重写产品变量优先级或节点名规则。

这段观察只在控制端执行 `assert/debug`，不采集远程 facts。产品 runner 仅为这个
固定 playbook 跳过可能引导安装远程 Python 的步骤，其它任务仍保留原兼容门禁。
runner 临时目录清理前只保留预检所需字段，不复制 inventory 凭据或任意任务输出。
当前本地合同与适用边界见
[`inventory-source-contracts.json`](v1.34.13/evidence/inventory-source-contracts.json)：

```bash
.venv/bin/python -B -m unittest upgrade.tests.test_inventory -v
```

这些内部步骤仍未构成完整 `kubecli upgrade` 首跳入口；调用方还须绑定原始配置与
inventory、制品身份、租约、恢复点、业务账本和 journal。配置观察与 API 观察通过
不能签署 UPG-05，也不能据此改变实验室。

节点内部阶段见 [`product/node.yml`](product/node.yml)：先核验所有 API 后端已为目标
版本、节点 UID 与源状态、eviction API 和精确运行时制品，再执行保留 PDB 与本地
数据保护的 drain。drain 成功后，containerd 路径按 `2.1.4 → 2.2.10 → 2.3.7`
逐边核验 CRI 和保留的 sandbox；Docker 路径保持 Engine `28.5.2`，只将
cri-dockerd `0.3.26` 更新为 `0.4.10`。运行时通过后才替换该节点的 kubelet/proxy，
核验同一 UID 的目标 kubelet 与 Ready，再恢复调度。首失败不继续后续节点、不自动
降级 metadata、不解除 cordon，源配置漂移进入诊断和重新评审。

这些是外层编排调用的内部任务文件，不能独立授予变更许可。调用方仍须提供冻结
组合、按架构核验的制品、源配置与 unit 摘要、租约、恢复点和 journal 续跑证明；
完整产品入口和现场 runner 尚未闭合。当前本地调用与拒绝证据见
[`runtime-stage-contracts.json`](v1.34.13/evidence/runtime-stage-contracts.json)，
记录适用范围、首失败及两套 Ansible 版本的实际结果；夹具成功不是现场 CRI 或 GA。

etcd 内部阶段见 [`product/etcd.yml`](product/etcd.yml)，仅处理 `3.6.4 → 3.6.16`
同 minor 补丁迁移。它先核验完整成员、cluster/member ID、revision、每架构制品、
现有服务定义和实际进程身份，再逐成员替换；每次重启后核验全部端点健康及原成员
身份，最后重新观察全部目标成员。失败停止后续成员，不重建成员、清理数据/WAL
目录或自动降级。不接受未经评审的 systemd drop-in。恢复点、租约和 journal 仍由
外层准入负责，当前阶段不能独立执行；两套 Ansible 的离线证据见
[`etcd-stage-contracts.json`](v1.34.13/evidence/etcd-stage-contracts.json)。

Metrics Server 后置内部阶段位于 [`product/metrics_stage.py`](product/metrics_stage.py)。
它在所有 API/节点到达 `v1.34.13` 后，仅将既有 Deployment 镜像从 `v0.8.0`
滚动到 `v0.9.1`，先做 server dry-run，并用 UID、resourceVersion 和源镜像保护
实际 JSON Patch；不重放含删除动作的安装任务。它保留九个源对象及权限/配置，
读取真实节点与业务 Pod Metrics API，核对全部容器、Pod 身份和推进的样本时间。
源指标不健康时拒绝改动；目标超时或并发漂移时停止，不自动回退。

本地聚焦合同命令为：

```bash
.venv/bin/python -B -m unittest upgrade.tests.test_metrics_stage -v
```

当前证据见 [`metrics-stage-contracts.json`](v1.34.13/evidence/metrics-stage-contracts.json)。
该阶段仍待接入完整 `config.yml + kubecli upgrade` 准入、journal 与固定现场 runner；
离线夹具、制品回读和指标接口合同均不构成现场或 GA PASS。

Local Path Provisioner 后置内部阶段见
[`product/local_path_stage.py`](product/local_path_stage.py)。它只处理已交付的
`v0.0.31 → v0.0.37` 控制器和 BusyBox `1.37` helper 变更，先验证两项实际
server dry-run，再更新 helper、后更新控制器。保留原 StorageClass、RBAC、
ConfigMap 中的路径和目录脚本，以及准入 PV/PVC 的 UID、绑定、节点亲和性与卷路径。
源配置自定义脚本、helper 或路径模式须先评审，不自动开启不安全兼容选项。

官方实现可能在重载失败后沿用旧配置，故目标 Ready 之后仍须执行调用方拥有的
真实新建卷写读/删除与清理探针，并复验旧卷账本。阶段本身不删除客户卷，不负责
冻结制品、供给静默窗口、租约、恢复点或 journal，也不独立授予变更许可。
本地命令与证据为：

```bash
.venv/bin/python -B -m unittest upgrade.tests.test_local_path_stage -v
```

[`localpath-stage-contracts.json`](v1.34.13/evidence/localpath-stage-contracts.json)
只记录离线阶段合同；完整产品入口与上述真实数据探针仍待接通。

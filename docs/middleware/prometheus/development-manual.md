# Prometheus 开发手册

## 1. 代码与配置契约

版本源为 `common/constants.py`，客户默认值为 `conf/config.yml`，核心模板为 `roles/cluster-addon/templates/prometheus/values.yaml.j2`，核心任务为 `roles/cluster-addon/tasks/prometheus.yml`，扩展任务为 `prometheus-optional.yml`。扩展 Chart 位于 `roles/cluster-addon/files/`，必须先通过 ext-images 双推和摘要校验。

任何扩展改动都必须保持以下契约：`prom_thanos_install`、`prom_adapter_install`、`prom_blackbox_install` 默认 `"no"`；扩展依赖 `prom_install: "yes"`；未启用时不创建资源；启用任一扩展前必须运行 `kubecli download -E prometheus-optional`，使固定双推镜像先进入本地 Registry；启用后使用固定本地 Chart/镜像和可审计的 Service、Secret、RBAC；同一配置重复执行只做声明式收敛；把开关改为 `"no"` 不执行隐式卸载，卸载必须以 `prom_optional_uninstall: "yes"` 显式发起，且不删除 PVC。

配置到资源的映射必须保持一一对应：

| 配置 | 任务/模板行为 | 关闭时的硬性断言 | 开启后的业务验收 |
| --- | --- | --- | --- |
| `prom_install` | `prometheus.yml` + 88.0.0 values | 不创建核心 release/CRD/PVC | Targets、Rules、PromQL、Grafana、告警和故障恢复 |
| `prom_thanos_install` | values 渲染 `spec.thanos`、`replicaExternalLabelName: prometheus_replica` 与 discovery；任务创建使用同一副本标签的 Querier | 核心关闭时拒绝扩展 | Store API、去重和历史查询 |
| `prom_adapter_install` | 固定 Adapter Chart + 规则；空 URL 回落核心 Service | 核心关闭时拒绝扩展 | APIService Available 和实际 MetricValue |
| `prom_blackbox_install` | 固定 exporter Chart，并以 `fullnameOverride=blackbox-exporter` 锁定 Service；每个 `name/module/url` target 渲染一个 Probe | 核心关闭时拒绝扩展，非法 target 拒绝渲染 | Probe Target `UP`、`probe_success` |

测试脚本可以临时覆盖 values 来验证开启路径，但不得把测试覆盖写回 `conf/config.yml` 或客户集群配置。`prom_blackbox_probe_targets` 为空是合法且安全的默认状态。每项必须含 DNS 标签 `name`、已启用 exporter 模块 `module` 和 HTTP(S) `url`，Role 在渲染前校验；`prom_adapter_prometheus_url` 为空时使用核心 Prometheus Service，接入 Thanos 必须显式填 Querier 地址。

## 2. 测试与交付

先运行 `bash tests/run_unit_tests.sh`，再运行 `bash tests/run_enterprise_regression.sh --prometheus-only`。核心与可选分路都必须从清洁实验室开始，保留 durable 状态、失败诊断和清理证据。矩阵条目只有在当前运行产生成功标记、`rc=0`、零 failure marker 和 `LAB_CLEAN_VERIFY_PASS` 后才可置为 pass。

可选分路必须覆盖 Thanos Store/Dedup/历史查询、Adapter APIService 和实际 Custom Metric、Blackbox Probe `probe_success`；核心分路必须覆盖 HA/PDB/PVC、Targets/Rules、Grafana、告警、故障恢复、升级回滚和 PromQL 并发。变更后同步更新 `tests/enterprise-test-matrix.yaml`、版本矩阵及三份客户文档。

## 3. 参考入口

完整核心配置和原理说明见 `docs/operations-manual.md`、`docs/whitepaper/12-addons-observability.md` 与 `docs/development-manual.md`。扩展的官方语义分别以 Thanos、prometheus-adapter 和 blackbox_exporter 在锁定版本的文档/源码为准，不得用动态镜像标签或未审查公共加速源替代固定制品。

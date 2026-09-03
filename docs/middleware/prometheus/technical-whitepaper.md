# Prometheus 技术白皮书

## 1. 架构与边界

基础栈由 kube-prometheus-stack 88.0.0 管理 Prometheus Operator、Prometheus、Alertmanager、Grafana、node-exporter 和 kube-state-metrics。Prometheus 以拉取模型写入本地 TSDB；Operator 通过 CRD 生成配置和工作负载；Alertmanager 负责分组、抑制和通知；Grafana 只读查询接口。

扩展均是独立数据路径：Thanos Sidecar 从每个 Prometheus 暴露 Store API，Querier 通过 DNS 发现并按 `prometheus_replica` 去重。Role 在启用 Thanos 时显式配置同名 `replicaExternalLabelName`，避免把上游默认值作为跨版本的隐式契约；配置对象存储 Secret 后 Sidecar 才负责封闭块上传。Store Gateway、Compactor、Receive 和 Ruler 不属于当前自动收敛范围，因此 Secret 的存在不等价于已具备长期历史查询或桶保留能力。prometheus-adapter 查询 Prometheus 或 Querier，将规则映射为 Custom Metrics API，供 HPA 使用；blackbox-exporter 接收每个显式 Probe 请求并从调用方视角返回 `probe_*` 指标，不能替代应用内部 RED 指标。

`prom_install`、`prom_thanos_install`、`prom_adapter_install` 和 `prom_blackbox_install` 默认值均为 `"no"`。扩展开关不会隐式安装核心栈，核心关闭时不得创建任何扩展资源。启用任一扩展前，控制节点必须运行 `kubecli download -E prometheus-optional`，把 Thanos、Adapter 和 Blackbox 的固定双推镜像上传到本地 Registry；该制品前置不改变任何安装开关。Blackbox Role 对官方 Chart 显式设置 `fullnameOverride=blackbox-exporter`，使 Probe 使用稳定的集群内 Service DNS，而不依赖 Helm release 与 Chart 名拼接规则。Adapter 与 metrics-server 分工不同：前者提供 Custom/External Metrics，后者提供 Resource Metrics。

| 配置开关 | 默认状态 | 资源边界 | 数据/控制边界 |
| --- | --- | --- | --- |
| `prom_install` | 关闭 | 核心监控栈及其 CRD、PVC、Secret | PromQL、规则评估、告警和 Grafana 查询 |
| `prom_thanos_install` | 关闭 | Sidecar、discovery Service、Querier | 只在配置对象存储 Secret 后上传块；Querier 提供聚合、去重、历史查询 |
| `prom_adapter_install` | 关闭 | Adapter、RBAC、Custom Metrics APIService | 空 `prom_adapter_prometheus_url` 严格回落到核心 Service；只按 rules 将 PromQL 结果映射为 Kubernetes Custom Metrics |
| `prom_blackbox_install` | 关闭 | Exporter Deployment/Service | 只执行 `name/module/url` 显式声明的 Probe；不自动发现业务目标 |

因此可选分路的现场验收不改变生产默认值；它们是按需启用的独立能力，关闭时资源集合应为空。

## 2. 一致性、安全与容量

Prometheus 副本各自拥有 TSDB，Thanos 去重依赖 external label 键和 Querier `--query.replica-label` 完全一致。单个副本或 Store 故障时应保留 warning 并按 partial-response 策略处理，不得把少数派结果冒充完整数据。对象存储凭据只放 Secret，查询和 exporter Service 默认 ClusterIP；对外暴露必须经认证的 Ingress/Gateway。

Adapter 是 Kubernetes API 聚合层的一部分：HPA 请求 `custom.metrics.k8s.io`，kube-apiserver 转发至 Adapter，Adapter 再查询 Prometheus 兼容 API。它不采集指标，也不能替代 metrics-server。Blackbox 的 `Probe` 是 Prometheus Operator 的声明式对象；每一个配置目标有独立 job，便于按入口、协议和模块定位 `probe_success`、`probe_duration_seconds` 与 exporter 自身的 `up`。

扩展资源请求和副本数必须纳入 Node Allocatable 评估。一个桶只运行一个 Compactor；Probe 目标、Adapter rules 和网络策略必须显式评审。升级前完成 Chart provenance、镜像 manifest digest、Helm server-side diff 和回滚演练。

## 3. 版本与官方依据

当前锁定：Chart 88.0.0、Prometheus v3.13.1-distroless、Operator v0.93.0、Alertmanager v0.33.1、Grafana 13.1.1、Thanos v0.41.0、prometheus-adapter v0.12.0、blackbox-exporter v0.27.0。权威依据为 Prometheus、Prometheus Operator、Thanos、prometheus-adapter 和 blackbox_exporter 官方发布与配置文档；本仓库固定制品和 `common/constants.py` 是交付版本源。

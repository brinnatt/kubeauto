# Prometheus 操作手册

## 1. 默认行为与配置

Prometheus 栈及扩展均为可选能力，权威配置文件是
`clusters/<cluster>/config.yml`（模板示例见 `conf/config.yml`）。默认值为：

```yaml
prom_install: "no"
prom_namespace: "monitor"
prom_storage_class: ""
prom_chart_ver: "__prom_chart__"
prom_thanos_install: "no"
prom_thanos_replicas: 2
prom_thanos_objectstorage_secret: ""
prom_adapter_install: "no"
prom_adapter_prometheus_url: ""
prom_blackbox_install: "no"
prom_blackbox_replicas: 2
prom_blackbox_probe_targets: []
prom_optional_uninstall: "no"
```

`prom_install: "no"` 不创建命名空间、Helm release、CRD、Pod、PVC 或 Secret。
Thanos、prometheus-adapter、blackbox-exporter 不会因测试或其他开关自动启用，且三者都依赖 `prom_install: "yes"`。启用任一扩展前必须先执行 `kubecli download -E prometheus-optional`，将已固定并双推的扩展镜像预置到集群本地 Registry；核心栈仍使用 `kubecli download -E prometheus`。

| 配置项 | 默认值 | 开启后的实际资源 | 主要使用方式 |
| --- | --- | --- | --- |
| `prom_install` | `"no"` | kube-prometheus-stack、Prometheus、Alertmanager、Grafana、node-exporter、kube-state-metrics | PromQL、仪表盘和告警 |
| `prom_thanos_install` | `"no"` | Prometheus Thanos sidecar、discovery Service、`prom_thanos_replicas` 个 Querier | 统一 Query API、HA 副本去重；对象存储 Secret 只允许 Sidecar 上传封闭块 |
| `prom_adapter_install` | `"no"` | Adapter Deployment、RBAC、`custom.metrics.k8s.io` APIService | 将 PromQL 规则映射为 Custom Metrics，供 HPA 或自动化使用 |
| `prom_blackbox_install` | `"no"` | `prom_blackbox_replicas` 个 blackbox-exporter Service Pod | 仅对 `prom_blackbox_probe_targets` 声明的 URL 产生 `probe_*` 指标 |

开关从 `"no"` 改为 `"yes"` 才创建或收敛对应资源。把开关改回 `"no"` 不会删除已运行的扩展，以避免一次配置回退误删生产监控入口；需要卸载时必须使用 `prom_optional_uninstall: "yes"` 的显式变更路径。扩展测试使用的临时目标、规则和 Querier 不会写入客户默认配置。

## 2. 安装与使用

启用核心栈并按需打开扩展：

```yaml
prom_install: "yes"
prom_namespace: "monitor"
prom_storage_class: "<生产 StorageClass>"
prom_thanos_install: "yes"       # 双副本查询去重；长期存储另配置对象存储 Secret
prom_adapter_install: "yes"      # Custom Metrics API/HPA
prom_blackbox_install: "yes"     # HTTP/TCP/DNS/ICMP 探测器
prom_blackbox_replicas: 2
prom_blackbox_probe_targets:
  - name: customer-http
    module: http_2xx
    url: https://app.example.com/healthz
```

执行 `kubecli setup <cluster> 07`。核心安装通过后才会收敛扩展：Thanos 创建双副本 Querier，Adapter 注册 `custom.metrics.k8s.io`，Blackbox 创建双副本 exporter Service。Probe 必须由用户明确创建并指定目标；安装 exporter 不会凭空产生探测目标。

Adapter 的 `prom_adapter_prometheus_url: ""` 有确定含义：实际渲染为 `http://prometheus-operated.<namespace>.svc.cluster.local:9090`，不是空地址。接入 Thanos 时显式设置 `prom_adapter_prometheus_url: "http://thanos-querier.<namespace>.svc.cluster.local"`；Adapter 使用固定默认规则，新增业务指标规则须按变更流程审查并验证 API 中的实际 `MetricValue`。

Thanos 只在 `prom_thanos_install: "yes"` 时加入 Sidecar 与 Querier。Role 显式渲染 `replicaExternalLabelName: prometheus_replica`，Querier 使用同一键去重，两台 Prometheus 的同一时间序列在 Querier 中作为一条结果查询；不得用不同键名覆盖其中任一侧。`prom_thanos_objectstorage_secret` 指向同命名空间、键名为 `thanos.yaml` 的 Secret；设置后 Sidecar 上传封闭 TSDB 块。对象存储的 Store Gateway、Compactor、Receive、Ruler 属于独立生产扩展，不能因为设置该 Secret 而假定已部署或已具备长期历史查询、压缩和保留能力。

Blackbox 的目标由 `prom_blackbox_probe_targets` 显式声明，例如：

```yaml
prom_blackbox_probe_targets:
  - name: customer-http       # DNS 标签，生成 kubeauto-blackbox-customer-http Probe
    module: http_2xx          # blackbox-exporter 中已启用的模块
    url: https://app.example.com/healthz
```

每一个目标生成一个独立的 `Probe`，job 为 `kubeauto-blackbox-<name>`。Role 将官方 Chart 的 `fullnameOverride` 固定为 `blackbox-exporter`，因此 Probe 在集群内始终通过 `blackbox-exporter.<namespace>.svc.cluster.local:9115` 调用 Exporter。只安装 exporter 或将目标列表留空都不会探测任何地址；目标变更后检查 Prometheus Targets 中对应 job 为 `UP`，并确认 `probe_success{job="kubeauto-blackbox-customer-http"} == 1`。`module` 只选择 exporter 已提供的模块，新增模块需经独立配置和安全评审。Adapter 的资源指标接口是 `custom.metrics.k8s.io`，不替代 metrics-server 的 `metrics.k8s.io` 资源指标；上线前应以实际 API 查询返回的 MetricValue 验收。

## 3. 使用与验收路径

核心栈用于 Kubernetes 自身和工作负载的指标、规则、Alertmanager 与 Grafana。仅需基础监控时只设置 `prom_install: "yes"`，三项扩展开关保持 `"no"`。

需要跨副本统一查询时才启用 Thanos。访问集群内地址 `http://thanos-querier.<namespace>.svc.cluster.local:9090`，查询时由 Querier 按 `prometheus_replica` 去重；验收 `/api/v1/stores` 中两个 Sidecar Store 均为健康，再对同一 PromQL 分别以 `dedup=false` 和默认去重查询，确认原始副本数大于去重结果。

需要按业务指标扩缩时才启用 Adapter。先确认 Prometheus 或 Querier 中存在带 Kubernetes 资源标签的业务序列，再查询：

```bash
kubectl get apiservice v1beta1.custom.metrics.k8s.io
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1
```

只有 APIService 为 `Available=True` 且目标 MetricValue 非空，才可以创建 `autoscaling/v2` HPA。CPU、内存 HPA 继续使用 metrics-server，不依赖 Adapter。

需要从调用方视角验证 HTTP/HTTPS 入口时才启用 Blackbox。Probe 的 `url` 是被探测的真实入口，不是 exporter 地址；Prometheus 先请求 exporter，再由 exporter 请求该 URL。生产 HTTPS Probe 不得以 `insecure_skip_verify: true` 绕过证书问题，目标网络策略、DNS、CA 和出口授权应在变更前完成。

## 3. 卸载、升级与回滚

升级只修改已评审的固定版本并重新执行 setup；Helm 使用 `upgrade --install`，PVC 和 Secret 默认保留。扩展卸载需先在变更单确认：将要移除的扩展开关设为 `"no"`，临时设置 `prom_optional_uninstall: "yes"` 执行一次 setup，核对 Querier、Adapter、Exporter 和带 `kubeauto.io/component=prometheus-optional` 标签的 Probe 已删除后，再恢复 `prom_optional_uninstall: "no"`。该动作不删除 Prometheus 数据 PVC。回滚使用：

```bash
helm history prometheus -n monitor
helm rollback prometheus <REVISION> -n monitor --wait
helm uninstall thanos-querier prometheus-adapter blackbox-exporter -n monitor --wait
```

卸载扩展不会删除 Prometheus 数据 PVC；删除 PVC 必须单独审批并完成备份核验。

## 4. 验收与故障边界

使用 `bash tests/run_enterprise_regression.sh --prometheus-only`。核心和扩展分路必须分别出现 `PROMETHEUS_FULL_GATE_PASS`、`PROMETHEUS_OPTIONAL_FULL_GATE_PASS`、durable `rc=0` 和最终 `LAB_CLEAN_VERIFY_PASS`。Pod Running 不是业务验收；需检查 Targets、PromQL、Adapter API 数值、Probe `probe_success` 和 Querier Store/Dedup。原理与安全边界见[技术白皮书](./technical-whitepaper.md)。

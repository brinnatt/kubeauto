# EFK / Loki 日志平台用户与运维手册

| 文档属性 | 内容 |
| --- | --- |
| 适用版本 | ECK 3.5.0、Elasticsearch/Kibana 9.5.1、Fluent Bit 5.1.1、Loki 3.7.6、Alloy 1.18.1 |
| Kubernetes 基线 | v1.33.6 |
| 产品入口 | 集群 `config.yml` 与 `kubecli setup <cluster> 07` |
| 默认状态 | `logging_install: "no"`，不创建日志资源 |
| 交付原则 | 一个集群只选择 EFK 或 Loki；EFK 内只选择 direct 或 kafka-buffer |

## 1. 产品范围与路线选择

日志平台负责采集 Kubernetes 容器标准输出和标准错误，形成检索、可视化、告警及数据保护能力。客户在部署前根据查询模型、成本、缓冲需求和已有平台能力选择一条路线，产品不会自动迁移历史数据，也不会同时写入两个后端。

| 路线 | 数据链 | 适用场景 | 关键依赖 |
| --- | --- | --- | --- |
| EFK direct | `CRI -> Fluent Bit -> Elasticsearch -> Kibana` | 需要全文检索、索引生命周期和 Elasticsearch 快照，链路优先简洁 | 三个故障域、块存储、对象存储、Prometheus |
| EFK Kafka-buffer | `CRI -> Fluent Bit -> Kafka -> Logstash -> Elasticsearch -> Kibana` | 已交付 Kafka，要求 Elasticsearch 维护期间保留可重放缓冲 | direct 的全部依赖及已交付 Kafka |
| Loki | `CRI -> Alloy -> Gateway -> Loki -> Grafana` | 以标签索引和对象存储控制长期日志成本 | 三个故障域、块存储、S3 兼容对象存储、Prometheus/Grafana |

EFK 与 Loki 互斥；`direct` 与 `kafka-buffer` 也互斥。发现 `logging` 命名空间带有另一条路线的所有权标签时，产品在创建或采用资源前返回 `existing kubeauto logging solution=<existing>, requested=<requested>`，运维人员必须先完成原路线的数据导出和下线，不得修改标签绕过保护。

## 2. 容量规划

### 2.1 EFK

Elasticsearch 固定三个数据节点，每个节点请求和限制 8 GiB 内存，JVM 堆固定 4 GiB。生产节点应为三个独立故障域，并为操作系统页缓存、容器运行时和故障恢复预留额外内存；8 GiB 是 Elasticsearch Pod 的交付基线，不是节点总内存建议。

单节点数据盘最低容量按下式估算：

```text
每日原始日志量 x 保留天数 x 索引膨胀系数 x (1 + 副本数) / 数据节点数 x 安全余量
```

索引膨胀系数应使用预生产实测值；没有实测时不得把压缩收益计入承诺。水位、段合并、快照临时空间和节点重建均会消耗额外容量。默认模板使用 3 个主分片、1 个副本及 30 天 ILM 保留期。

Fluent Bit 每个节点请求 50m CPU、100 MiB 内存，限制 500m CPU、500 MiB 内存。Kafka-buffer 的 Logstash 每副本请求 500m CPU、1 GiB 内存，限制 2 CPU、2 GiB 内存；两个副本使用同一消费者组。

### 2.2 Loki

Loki 使用三个 single-binary Pod，`replication_factor: 3`，每个 Pod 请求 500m CPU 和 2 GiB 内存、限制 2 CPU 和 4 GiB 内存，默认 50 GiB PVC。对象存储承担长期 chunks、ruler 和 admin 数据，PVC 用于本地 WAL、缓存和恢复窗口，不能因已配置对象存储而省略。

Alloy 每节点一个 Pod，请求 50m CPU、128 MiB 内存，限制 500m CPU、512 MiB 内存。positions 位于节点目录 `/var/lib/alloy`；节点永久丢失时可能重新读取仍存在的 CRI 文件，查询和告警应允许短期重复。

## 3. 前置门禁

执行位置为安装了 Kubeauto 的控制节点，账号须能读取集群配置、访问 Kubernetes API，并能通过密钥登录目标节点。开始部署前必须确认：

- Kubernetes v1.33.6 节点全部 Ready，至少有三个可调度故障域；
- Prometheus 已由本项目交付，Grafana 在 Loki 路线中可用；
- EFK 目标节点 `vm.max_map_count >= 1048576`；
- StorageClass 支持 `ReadWriteOnce`，容量和回收策略符合客户数据保留要求；
- 对象存储桶、最小权限访问密钥、DNS、证书链和时间同步已经就绪；
- EFK 入口 TLS Secret 已存在；Loki Gateway 认证 Secret 已存在；
- Kafka-buffer 使用本项目已交付的 Kafka，不部署第二套 Strimzi Operator。

检查集群和存储：

```bash
kubectl --kubeconfig clusters/<cluster>/kubectl.kubeconfig get node,storageclass
```

检查现有路线所有权：

```bash
kubectl --kubeconfig clusters/<cluster>/kubectl.kubeconfig get namespace logging -o jsonpath='{.metadata.labels.kubeauto\.io/logging-solution}{"\n"}'
```

> **异常处理：已经存在另一条日志路线**
>
> 停止部署。先按第 13 章完成原路线的采集停止、快照或对象存储核验、资源清理和 clean verify。不得删除所有权标签、复用原 PVC 或让两个采集器同时读取同一批 CRI 文件。

## 4. Secret 与外部依赖准备

Secret 的值由客户密码库或密钥管理系统生成，本手册只规定名称和键。密码、私钥、Access Key 和 Token 不得写入 Git、`config.yml`、命令历史或工单正文。

| 路线 | 资源 | 必需键 | 消费方 |
| --- | --- | --- | --- |
| EFK | `logging-ingress-tls` | `tls.crt`、`tls.key` | Kibana Ingress |
| EFK | `logging-snapshot-s3` | `s3.client.default.access_key`、`s3.client.default.secret_key` | Elasticsearch keystore |
| EFK | `logging-efk-writer` | `username`、`password` | 产品引导最小权限用户，再派生采集 Secret |
| Loki | `loki-s3` | `LOKI_S3_ACCESS_KEY`、`LOKI_S3_SECRET_KEY` | Loki |
| Loki | `loki-gateway-auth` | `.htpasswd` | Gateway |
| Loki | `loki-gateway-client` | `LOKI_GATEWAY_USERNAME`、`LOKI_GATEWAY_PASSWORD` | Alloy 与 Grafana 数据源 |

HTTPS 对象存储还需 CA ConfigMap，固定键为 `ca.crt`。EFK 使用 `logging_efk_snapshot_ca_configmap`，Loki 使用 `logging_loki_storage_ca_configmap`。证书必须覆盖配置的 S3 DNS 名称；不得使用跳过证书校验代替正确 CA。

## 5. EFK direct 部署

在 `clusters/<cluster>/config.yml` 合并以下顶层配置，保留文件中的其他既有键。节点名必须与 Kubernetes `metadata.name` 完全一致：

```yaml
prom_install: "yes"
logging_install: "yes"
logging_solution: "efk"
logging_namespace: "logging"
logging_efk_delivery: "direct"
logging_efk_es_nodes: ["worker-a", "worker-b", "worker-c"]
logging_efk_storage_class: "production-rwo"
logging_efk_es_pvc_size: "200Gi"
logging_efk_es_memory_request: "8Gi"
logging_efk_es_memory_limit: "8Gi"
logging_efk_es_heap: "4g"
logging_efk_kibana_replicas: 2
logging_efk_kibana_host: "kibana.example.com"
logging_ingress_controller: "ingress-nginx"
logging_ingress_class: "nginx"
logging_ingress_tls_secret: "logging-ingress-tls"
logging_efk_snapshot_endpoint: "https://s3.example.com:9000"
logging_efk_snapshot_region: "region-1"
logging_efk_snapshot_bucket: "logging-snapshots"
logging_efk_snapshot_path_style: false
logging_efk_snapshot_secret: "logging-snapshot-s3"
logging_efk_snapshot_ca_configmap: "logging-snapshot-ca"
logging_efk_writer_secret: "logging-efk-writer"
logging_efk_writer_user: "fluent-bit"
logging_efk_retention_days: 30
```

执行产品入口：

```bash
kubecli setup <cluster> 07
```

任务依次完成配置断言、所有权拒绝、节点与 Secret 前置检查、ECK CRD/Operator 校验、Elasticsearch/Kibana 发布、最小权限 writer/ILM/索引模板/快照仓库/SLM 初始化，最后发布 Fluent Bit。相同输入重复执行应保持 PVC、客户 Secret 和工作负载身份，不产生无意义替换。

## 6. EFK Kafka-buffer 部署

该路线不是 direct 的附加输出。将第 5 章的配置改为以下顶层键，并同时使用已交付 Kafka 的有效配置：

```yaml
logging_solution: "efk"
logging_efk_delivery: "kafka-buffer"
kafka_install: "yes"
logging_efk_kafka_topic: "efk-replay"
logging_efk_kafka_group: "efk-replay"
logging_efk_kafka_user: "efk-pipeline"
logging_efk_logstash_replicas: 2
```

```bash
kubecli setup <cluster> 07
```

产品创建 KafkaTopic、SCRAM-SHA-512 KafkaUser 和最小 ACL，Fluent Bit 仅输出到 Kafka；Logstash 两副本使用同一消费者组写 Elasticsearch。渲染清单中出现 direct Elasticsearch output、第二个 Strimzi Operator 或明文 SASL 密码均视为失败。

## 7. Loki 部署

先确认 EFK 已下线，再在集群配置中合并：

```yaml
prom_install: "yes"
logging_install: "yes"
logging_solution: "loki"
logging_namespace: "logging"
logging_loki_storage_secret: "loki-s3"
logging_loki_storage_ca_configmap: "loki-object-storage-ca"
logging_loki_storage_class: "production-rwo"
logging_loki_storage_endpoint: "https://s3.example.com:9000"
logging_loki_storage_region: "region-1"
logging_loki_bucket_chunks: "loki-chunks"
logging_loki_bucket_ruler: "loki-ruler"
logging_loki_bucket_admin: "loki-admin"
logging_loki_gateway_auth_secret: "loki-gateway-auth"
logging_loki_gateway_client_secret: "loki-gateway-client"
logging_loki_ingress_host: "logs.example.com"
logging_loki_retention_days: 30
logging_loki_memory_request: "2Gi"
logging_loki_memory_limit: "4Gi"
```

```bash
kubecli setup <cluster> 07
```

固定 Loki Chart 18.9.0 部署 Loki 3.7.6 三副本，Alloy Chart 1.11.1 部署 Alloy 1.18.1。Gateway 使用既有 htpasswd Secret；Alloy 和 Grafana 使用客户端 Secret。未认证 Gateway 请求必须返回 401，正确凭据必须返回 200。

## 8. 业务验收与日常巡检

资源 Ready 只是前置状态。最终业务验收必须创建唯一日志标志，从客户查询入口读回，并验证数量；同时检查每个 Prometheus 副本的 targets、rules `health` 和 `lastError`。

EFK 健康与存储：

```bash
kubectl -n logging get elasticsearch,kibana,pod,pvc,ingress
```

Loki 健康与存储：

```bash
kubectl -n logging get statefulset,daemonset,pod,pvc,service
```

日常巡检至少覆盖：集群健康、未分配分片或 Loki ring、PVC 水位、对象存储错误、采集器重试、Kafka consumer lag、查询 P95/P99、错误数及 CPU/内存水位、告警 firing/resolved 闭环和最近一次备份可恢复性。

### 8.1 监控与告警

EFK 发布 Fluent Bit ServiceMonitor 和三条采集告警；Loki 使用固定 Chart 的 dashboards、rules、alerts 和 ServiceMonitor。验收必须逐个 Prometheus Pod 查询 API，不能只检查 PrometheusRule 对象存在。规则 `health` 必须为 `ok`，`lastError` 必须为空。

### 8.2 性能与背压

性能基线应固定日志数量、行大小、并发、持续时间和资源配置，记录写入完成时间、可查询时间、P95/P99、错误数及 CPU/内存水位。维护窗口中临时阻断下游后，采集器必须重试；恢复后固定批次应全部可查询。Kafka-buffer 还须证明 lag 增长并回落至 0。

## 9. 备份与恢复

### 9.1 EFK

SLM 使用 `logging-s3` repository 对 `k8s-*` 建立快照。每天检查策略执行状态；季度至少在隔离索引名下真实恢复一次，校验文档数量或 hash，再删除隔离索引。恢复不得覆盖当前写入索引或 `.security*`、`.kibana*` 系统索引。

### 9.2 Loki

对象存储是 chunks 和索引的长期数据源，应使用对象存储平台的版本控制、复制或灾备策略。Loki PVC 不是对象存储备份。恢复演练至少重启对象存储服务并验证历史唯一日志仍可查询；跨集群恢复必须保持 schema、租户、桶前缀和加密材料一致。

RPO 与 RTO 由客户业务等级确定。没有经过当前环境恢复演练的对象副本不能写成已验证备份。

## 10. 凭据轮换与安全

轮换采用“创建新值、更新服务端、重启消费者、验证新值、拒绝旧值、保留数据、撤销旧值”的顺序。Kubernetes Secret 更新不会自动改变已运行容器的环境变量，因此 EFK 采集器/Logstash 或 Loki Gateway/Alloy 必须滚动重启。轮换期间不得在日志中输出 Secret data。

EFK writer 轮换后重新执行产品入口，使 Elasticsearch 用户和派生 Secret 收敛，再滚动重启对应采集器；新凭据认证成功且旧凭据返回 401 后才完成。Loki 同时更新 htpasswd Secret 和客户端 Secret，再依次滚动 Gateway 与 Alloy；Grafana 数据源也应重新加载客户端 Secret。

RBAC 只允许采集器读取 Pod、Namespace 和日志元数据，不允许读取任意 Secret。错误 CA、错误主机名、错误密码和未认证请求必须失败；禁止使用 `curl -k` 或关闭 TLS 作为生产修复。

> **回滚：新凭据发布后业务链路未恢复**
>
> 在旧凭据仍有效且已经保存在密码库的前提下，恢复原 Secret，滚动重启消费者并重新执行认证、写入和读取验收。若旧凭据已撤销，必须完成服务端与所有消费者的同一变更，不得把新旧密码同时长期保留。

## 11. 升级与回滚

变更前记录 change ID，保存 Chart/CR/values/manifest/history、PVC UID、版本、集群健康、唯一数据标志和最近可恢复备份。先查目标版本官方兼容矩阵和 breaking changes，在预生产按顺序升级，不跨越未经验证的主版本。

Loki 配置或 Chart 变更使用 Helm revision 回滚，但 schema 和对象格式变化不能靠 Helm rollback 撤销；这类失败应修复前滚，或在旧版本隔离集群从变更前对象存储副本恢复。Elasticsearch 数据格式升级同样禁止把 `spec.version` 直接改回旧版。

配置变更演练应修改一个可逆值，确认 revision/generation 变化、业务日志仍可查询、PVC UID 不变，再恢复原值并重复验证。

> **回滚：可逆配置发布失败**
>
> 停止继续变更，保存首个失败命令、渲染输入、事件和控制器日志。Loki 回滚到本次变更前的精确 Helm revision；EFK 恢复本次 change ID 保存的配置并重新执行 `kubecli setup <cluster> 07`。完成后验证健康、PVC UID、历史日志和新写入，不能只以 rollout 完成作为结果。

## 12. 故障处理

| 现象 | 首要证据 | 处理边界 |
| --- | --- | --- |
| 日志不可查询 | 源 Pod 日志行数、采集器日志/指标、后端查询 HTTP/JSON 状态 | 先区分未采集、认证失败、下游拒绝和查询窗口错误 |
| Elasticsearch yellow/red | `_cluster/health`、allocation explain、PVC/节点事件 | 不删除索引或 PVC 制造 green；先恢复故障域或容量 |
| Loki 写入失败 | Gateway 状态、Alloy write 错误、ring、对象存储错误 | 保留 positions/WAL，恢复认证、DNS、网络或对象存储 |
| Kafka lag 增长 | Topic offset、consumer group lag、Logstash 健康 | 允许维护窗口增长；恢复后必须回落到 0 |
| TLS 失败 | 证书链、SAN、有效期、客户端 CA | 更新正确 CA/证书，不跳过校验 |
| Pod 反复重启 | describe/events、上一次容器日志、资源水位 | 分类为容量、配置、探针或依赖故障后单层修复 |

> **异常处理：门禁或现场测试失败**
>
> 保留首个失败命令、durable exit、渲染配置、Pod describe/events 和控制器日志，先分类为产品、测试门禁、环境、供应链、运行时或 Kubernetes/controller。修复一个已证明的因果层，清理该次失败残留，从可验证 clean boundary 只重跑最窄分支；focused 通过后再执行一次完整回归。

## 13. 下线与清理

下线前停止新增采集，等待 Fluent Bit/Alloy 缓冲和 Kafka lag 清零，完成 EFK 快照或 Loki 对象存储核验，记录保留责任。随后执行日志专属 cleanup；不得使用宽泛标签删除其他中间件或手工清空共享对象存储。

测试环境的固定清理入口由 runner 自动调用：

```bash
bash tests/helpers/logging-cleanup.sh
```

```bash
bash tests/helpers/logging-cleanup.sh --verify
```

只有输出 `LOGGING_CLEAN_VERIFY_PASS` 才表示日志命名空间、工作负载、PVC、测试 RBAC、Operator 和运行时临时制品均已按所有权回收。生产下线前必须另行执行客户数据保留审批，不得直接套用测试环境删除策略。

## 14. 交付验收

专项入口和观察入口：

```bash
bash tests/run_enterprise_regression.sh --logging-only
```

```bash
bash tests/run_enterprise_regression.sh --logging-status
```

```bash
bash tests/run_enterprise_regression.sh --logging-follow
```

交付签收必须同时满足：三条客户可选路线取得当前 clean evidence；矩阵 `44/44`；主进程输出 `LOGGING_FULL_GATE_PASS`；durable 状态为 `LOGGING_GATE_EXIT rc=0`；零 failure marker；最终输出 `LOGGING_CLEAN_VERIFY_PASS`；单元测试、六仓契约和总交付回归全部通过。缺少任何一项都不能以“部分通过”交付。

## 15. 每日、每周和每月工作节奏

### 15.1 每日值班检查（15 分钟内）

先确认入口、后端和采集器，再确认数据，不要从“某个 Pod 是 Running”推导平台健康：

```bash
KC="kubectl --kubeconfig=clusters/<cluster>/kubectl.kubeconfig"
$KC -n logging get pods -o wide
$KC -n logging get pvc
$KC -n logging get events --sort-by=.lastTimestamp | tail -n 30
```

EFK direct 或 Kafka-buffer：

```bash
$KC -n logging get elasticsearch logging -o jsonpath='{.status.health}{" "}{.status.availableNodes}{"/"}{.status.expectedNodes}{"\n"}'
$KC -n logging get kibana logging -o jsonpath='{.status.health}{" "}{.status.availableNodes}{"/"}{.status.expectedNodes}{"\n"}'
$KC -n logging get pods -l app.kubernetes.io/name=fluent-bit
```

通过受控 API 通道查询 Elasticsearch，不在命令行写入密码：

```bash
ES_POD="$($KC -n logging get pod -l common.k8s.elastic.co/type=elasticsearch -o jsonpath='{.items[0].metadata.name}')"
$KC -n logging port-forward "pod/$ES_POD" 19200:9200 >/tmp/es-forward.log 2>&1 & ES_PF=$!
trap 'kill "$ES_PF" 2>/dev/null || true' EXIT
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 \
  -u "${ES_USER:?从密码库注入}:${ES_PASSWORD:?从密码库注入}" \
  https://logging-es-http.logging.svc:19200/_cluster/health?pretty
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 \
  -u "${ES_USER}:${ES_PASSWORD}" \
  'https://logging-es-http.logging.svc:19200/_cat/indices/k8s-*?format=json&bytes=gb'
```

Kafka-buffer 额外检查消费者组：

```bash
$KC -n kafka get kafkatopic efk-replay -o yaml
$KC -n kafka get kafkauser efk-pipeline -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
$KC -n logging logs -l app.kubernetes.io/name=logstash --since=15m | grep -E 'error|retry|dead.?letter' || true
```

Loki：

```bash
$KC -n logging get statefulset loki -o jsonpath='{.status.readyReplicas}{"/"}{.spec.replicas}{"\n"}'
$KC -n logging get ring -o name 2>/dev/null || true
$KC -n logging get pods -l app.kubernetes.io/name=alloy
```

每日必须记录：后端健康状态、最老未处理事件时间、PVC 使用率、最近一次快照状态、Kafka lag（如适用）、查询 P95 和 firing 告警。任何一项无法取得数据都应创建事件，而不是填写“正常”。

### 15.2 每周巡检

- 查看 Elasticsearch `_cat/shards`，确认没有持续 `UNASSIGNED`；对未分配分片执行 `_cluster/allocation/explain`，记录原因后再处理。
- 查看 ILM/SLM 执行历史，确认最近一次删除和快照均有成功记录；失败策略不得通过手工删除状态掩盖。
- 查看 Loki 对象存储错误、compactor、ingester WAL 恢复日志和查询慢日志，统计高基数标签 Top N。
- 统计每节点 Fluent Bit/Alloy 读取文件数量、重试次数、丢弃计数和 positions 文件大小。
- 在非高峰时段运行固定查询集，保存响应时间和结果数量；查询窗口、标签、用户权限必须保持一致。

### 15.3 每月和季度工作

- 每月审查保留期、日均/峰值日志量、索引膨胀率、对象存储成本和未来 90 天容量预测。
- 每月抽样检查日志脱敏规则，确认 Token、Cookie、身份证号等敏感字段未进入后端。
- 每季度在隔离索引或隔离集群执行一次真实恢复，验证时间范围、文档数、权限和应用查询，不得只验证文件存在。
- 每季度进行一次单成员故障和一次节点维护演练，记录 RTO、数据缺口和重复率。

## 16. 日志增长过快、索引和磁盘处置

### 16.1 先定位增长来源

增长过快时先冻结证据（时间、索引、Namespace、Pod、采集器），再采取删除动作：

```bash
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 -u "$ES_AUTH" \
  'https://logging-es-http.logging.svc:19200/_cat/indices/k8s-*?h=index,docs.count,store.size,creation.date.string&s=store.size:desc&format=json'
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 -u "$ES_AUTH" \
  -H 'content-type: application/json' -X POST \
  'https://logging-es-http.logging.svc:19200/k8s-*/_search?size=0' \
  -d '{"aggs":{"by_namespace":{"terms":{"field":"kubernetes.namespace_name.keyword","size":50}}}}'
```

对 Loki，先使用窄时间窗口和低基数标签查询；不要使用 `{job=~".*"}` 这类全量正则扫描。检查 Alloy stream 数量、`loki_write` 重试和对象存储 prefix 增长，确认是生产流量、解析重复还是异常循环输出。

### 16.2 正常清理

EFK 的正常清理由 ILM 执行。需要提前清理时，先列出候选完整索引并获得变更审批，再按索引删除；禁止按通配符删除未核验名称：

```bash
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 -u "$ES_AUTH" \
  -X DELETE 'https://logging-es-http.logging.svc:19200/k8s-2026.08.01'
```

删除后等待 segment merge 并复查磁盘，不要立即判定空间已经回收。Loki 的删除必须使用已评审的 retention/schema 或 compactor 机制；直接删除对象存储 prefix 会破坏索引与 chunk 对应关系，禁止作为日常清理手段。

### 16.3 磁盘接近满的应急顺序

1. 记录当前 `_cluster/health`、PVC 使用率、最大索引、最近快照和业务事件；暂停非必要的高噪声应用输出。
2. 当 Elasticsearch 达到 high/flood-stage 水位时，先按批准的保留策略删除最老完整索引，确认 `_cat/indices` 中已消失，再观察 merge 和水位。
3. 删除仍不足时，临时降低保留期并让 ILM 执行；不得删除当前热索引、`.security-*` 或 `.kibana*`。
4. 仅在清理动作保护了可运行空间后扩容 PVC。先确认 StorageClass `allowVolumeExpansion: true`，逐个 PVC 扩容并观察文件系统：

```bash
$KC get storageclass <storage-class> -o jsonpath='{.allowVolumeExpansion}{"\n"}'
$KC -n logging patch pvc/<es-pvc-name> --type=merge \
  -p '{"spec":{"resources":{"requests":{"storage":"300Gi"}}}}'
$KC -n logging get pvc/<es-pvc-name> -w
```

5. 扩容后验证 Elasticsearch 节点磁盘、分片分配、写入和查询；保留事件和审批记录。

Loki 磁盘满时优先保护 WAL 和对象存储上传路径：暂停高噪声采集、按 retention 清理已确认的对象数据，再扩容 PVC。不要删除 `/var/lib/alloy` 或 Loki WAL 目录来“腾空间”，那会丢失未确认批次和读取位置。

> **回滚：误删或清理后数据不完整**
>
> 立即停止进一步删除，保存后端和对象存储审计日志。EFK 从最近成功 SLM 快照恢复到隔离索引，再与当前索引比较时间范围和文档数；Loki 按对象存储版本/复制副本恢复缺失 prefix，并保持原 schema、租户和加密配置。恢复验证完成前不得把隔离数据直接覆盖生产索引。

## 17. 常见异常定位手册

### 17.1 日志完全不可见

按数据链从左到右逐层验证：源 Pod 是否产生日志，节点文件是否有新增，Fluent Bit/Alloy 是否读取，输出是否被认证或 NetworkPolicy 拒绝，后端是否接受，查询时间范围和权限是否正确。每层保存一个唯一标志和计数，禁止同时修改采集器、后端和网络策略。

### 17.2 EFK 为 yellow/red

```bash
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 -u "$ES_AUTH" \
  'https://logging-es-http.logging.svc:19200/_cluster/health?level=indices&pretty'
curl --fail-with-body --cacert ./secrets/logging-es-ca.crt \
  --resolve logging-es-http.logging.svc:19200:127.0.0.1 -u "$ES_AUTH" \
  -H 'content-type: application/json' -X POST \
  'https://logging-es-http.logging.svc:19200/_cluster/allocation/explain' -d '{}'
```

先区分未分配副本、节点离线、磁盘水位、分片过滤和恢复速度。不能通过把副本数改为 0 或删除 PVC 伪造 green；恢复故障节点或容量后重新观察分片恢复。

### 17.3 Kafka lag 持续增长

确认 Producer 是否仍在写入、Topic 分区是否足够、Logstash Pod 是否 Ready、消费者组是否发生 rebalance、Elasticsearch 是否限流。维护期间 lag 增长是可接受的暂态，恢复验收必须证明 lag 回到 0 且固定批次在 Elasticsearch 可查；只看到 Logstash 日志“connected”不算恢复。

### 17.4 Loki 查询慢或结果为空

先缩小时间范围和标签选择，再检查 querier、gateway、对象存储和 ingester 日志。结果为空时确认租户/认证、stream 标签和日志时间戳（纳秒）是否正确；查询窗口错误不能归因于采集丢失。高基数标签应回收为正文过滤字段，并重新评估写入和查询资源。

### 17.5 TLS、认证或权限失败

检查证书 SAN、有效期、签发 CA、Secret 键名和服务 DNS；用错误密码验证 401，用正确密码验证 200，再确认后端 writer 的索引范围。禁止使用 `-k`、跳过证书验证或给采集器绑定管理员角色。Secret 更新后必须滚动重启读取环境变量的消费者。

## 18. 客户操作记录模板

每次生产操作至少记录以下字段，并与变更单关联：

| 字段 | 示例含义 |
| --- | --- |
| change ID | 唯一变更编号 |
| 影响路线 | `efk/direct`、`efk/kafka-buffer` 或 `loki` |
| 起止时间与操作者 | 用于审计和 RTO 统计 |
| 变更前证据 | 健康、PVC、快照、lag、唯一日志标志 |
| 实际命令与结果 | 不包含密码、Token 或私钥 |
| 失败分类 | 产品、环境、供应链、运行时、Kubernetes/controller 或操作错误 |
| 回滚条件与结果 | 精确 revision、快照或对象副本 |
| 变更后验收 | 新写入、历史查询、告警恢复、容量和权限 |
| 数据保留决定 | 删除索引/对象前的审批和保留期限 |

该记录是客户长期运维资产，不应以终端截图替代；密码库、审计系统和对象存储审计日志分别保存敏感数据和操作证据。

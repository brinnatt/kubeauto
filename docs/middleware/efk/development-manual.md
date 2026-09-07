# EFK / Loki 日志平台开发手册

| 属性 | 内容 |
| --- | --- |
| 适用组件 | EFK direct、EFK Kafka-buffer、Loki + Alloy |
| 当前固定版本 | ECK 3.5.0、Elasticsearch/Kibana 9.5.1、Fluent Bit 5.1.1、Loki 3.7.6 / Chart 18.9.0、Alloy 1.18.1 / Chart 1.11.1 |
| 产品入口 | `conf/config.yml`、生成集群配置、`kubecli setup <cluster> 07` |
| 测试入口 | `tests/run_enterprise_regression.sh --logging-only` |

## 1. 模块边界

日志平台是默认关闭的 cluster-addon 分路。`logging_install: "no"` 时不得创建 namespace、CRD、Operator、Secret、PVC、工作负载或监控对象。启用后，`logging_solution` 只能为 `efk` 或 `loki`；EFK 的 `logging_efk_delivery` 只能为 `direct` 或 `kafka-buffer`。

| 文件 | ownership 与职责 |
| --- | --- |
| `conf/config.yml` | 用户可见默认值、类型、前置条件和路线选择 |
| `common/constants.py` | 组件版本、Chart/清单和镜像固定清单 |
| `roles/cluster-addon/tasks/main.yml` | 只在开关启用时 include 日志任务 |
| `roles/cluster-addon/tasks/logging.yml` | 断言、所有权拒绝、渲染、server dry-run、发布和收敛 |
| `roles/cluster-addon/templates/logging/efk.yaml.j2` | ECK Elasticsearch/Kibana、Ingress、监控对象 |
| `roles/cluster-addon/templates/logging/fluent-bit-direct.yaml.j2` | direct 采集器、RBAC、Service 和文件缓冲 |
| `roles/cluster-addon/templates/logging/fluent-bit-kafka.yaml.j2` | Topic/User/ACL、Kafka 采集器与 Logstash |
| `roles/cluster-addon/templates/logging/loki-values.yaml.j2` | Loki 三副本、对象存储、Gateway 和 mixin |
| `roles/cluster-addon/templates/logging/alloy-values.yaml.j2` | Alloy 发现、relabel、positions 和 Loki write |
| `tests/logging-test-matrix.yaml` | 44 项验收状态与当前证据 |
| `tests/helpers/logging-regression.sh` | 产品入口、业务、故障、变更和性能现场门禁 |
| `tests/helpers/logging-cleanup.sh` | 日志所有权范围内的清理与 clean verify |

## 2. 配置契约

新增变量必须使用 `logging_` 前缀，在 `conf/config.yml` 提供默认值、注释和类型，并在单元测试中覆盖无效组合。Secret 配置只接受资源名，不接受明文值。前置断言按以下顺序执行：枚举和类型、共享 Prometheus、路线互斥、节点和存储、Secret/CA、再创建 namespace。

同名资源已有不同路线 ownership 时返回非零，不能覆盖标签。EFK direct 与 kafka-buffer 的 collector 模板按条件只渲染一个。Kafka-buffer 复用 Kafka 分路及其 Strimzi Operator，不得在日志任务中建立第二个 Operator。

## 3. 渲染与发布规则

EFK 清单先通过 API Server dry-run，再用 `kubeauto-logging-efk` field manager 执行 server-side apply。ECK CRD 和 Operator 来自固定官方清单，分别使用独立 field manager；只有官方清单与项目镜像引用的已知差异可被接管。

Elasticsearch writer 在采集器发布前创建：任务等待 ECK TLS 和管理 Secret，通过验证服务 DNS SAN 的临时 port-forward 调用安全 API，创建最小角色、用户、ILM、索引模板、snapshot repository 和 SLM，再派生采集 Secret。不得把管理密码传给 Fluent Bit 或 Logstash。

Loki/Alloy 使用仓库内固定 tgz 和 SHA256。模板只引用 Secret，敏感值不进入 values。`helm upgrade --install` 的失败恢复和历史数量应与固定 Helm 主版本兼容；变更前保存 release revision、values 和 manifest。对象 schema 或 Elasticsearch 数据格式变化不是 Helm rollback 能力。

## 4. 资源、安全与幂等

所有 namespaced 资源带 `app.kubernetes.io/managed-by=kubeauto` 和日志 component 标签；集群级 RBAC 使用精确名称和 selector。采集器只具有 Pod、Namespace、Node 和日志读取权限，不具有 Secret 读取权限。TLS 客户端必须使用 CA 和正确主机名，不得持久化动态代理或跳过验证。

资源 requests/limits 是产品契约：Elasticsearch 8 GiB/4 GiB heap，Fluent Bit 50m/100 MiB 到 500m/500 MiB，Logstash 500m/1 GiB 到 2 CPU/2 GiB，Loki 500m/2 GiB 到 2 CPU/4 GiB，Alloy 50m/128 MiB 到 500m/512 MiB。修改资源后必须重新执行调度、压力、背压和故障恢复场景。

相同配置二次执行比较 PVC、客户 Secret、关键 CR 和 workload UID。允许 Helm revision 或 generation 因声明式协调变化，但没有配置差异时不能替换 PVC/Secret 或丢失数据。固定目标变更使用 change ID；同名异参不得静默采用。

## 5. 供应链与六仓

六仓是一个发布单元：`kubeauto`、`kubeauto-dockerfile`、`kubeauto-k8s-bin-dockerfile`、`kubeauto-ext-bin-dockerfile`、`kubeauto-ext-bin-sp1-dockerfile`、`kubeauto-ext-images-dockerfile`。版本变更先盘点生产、升级、回滚、备份、性能和测试辅助制品，在 ext-images 的 owning 目录添加固定官方来源 Dockerfile，登记 GitHub Actions 双推矩阵，再验证 TalkEdu 和 Docker Hub manifest digest。

中国现场优先 TalkEdu 固定副本，并保留 Docker Hub/上游回退。公共代理只能通过未持久化的测试参数临时注入，不能写入代码、CI、默认配置或客户文档。Chart、CRD 和文件下载必须 SHA256 校验并原子替换。

## 6. 测试工程契约

任何产品或测试门禁变更按以下顺序执行：

1. 分类为产品、test-gate、环境/供应链或组合，记录 owning scenario 和预期 marker。
2. 执行 shell、Python、YAML、Jinja/Helm render、资源名/namespace/selector/port/image/marker 静态检查。
3. 为精确根因增加确定性单测，运行最窄 focused 分支。
4. focused 绿色后只启动一次 clean full logging regression。
5. 全链失败先保存首个命令、durable 状态、渲染输入、events 和 controller 日志，再修改单一因果层。

固定命令：

```bash
bash tests/run_unit_tests.sh
```

```bash
bash tests/run_enterprise_regression.sh --logging-only
```

```bash
bash tests/run_enterprise_regression.sh --logging-status
```

```bash
bash tests/run_enterprise_regression.sh --logging-follow
```

runner 负责 source sync、durable PID/exit、前台流式日志、30 秒 heartbeat、静默诊断、失败后的 scoped cleanup 和最终 clean verify。kubectl、Helm 或 SSH 只能用于诊断、fixture 和独立 API 检查，不能替代 `config.yml + kubecli` 产品入口证据。

## 7. 矩阵语义

`LOGGING-01..05` 是静态和供应链；`06..11` 是前置；`12..29` 覆盖 EFK direct/Kafka-buffer；`30..37` 覆盖 Loki；`38..44` 覆盖变更、Secret 轮换、性能背压、三状态清理、路线冲突和文档。

每个 case 只有在当前现场写入 evidence TSV 并输出 `LOGGING_CASE_PASS` 后才可作为本轮证据。矩阵保持 pending 直到三条路线均完成；历史日志、Pod Running、单次 API 200 或静态 grep 都不能直接变更为 pass。

最终成功同时要求 `LOGGING_FULL_GATE_PASS`、durable `LOGGING_GATE_EXIT rc=0`、零 failure marker、矩阵 44/44、六仓契约、单元测试和 `LOGGING_CLEAN_VERIFY_PASS`。

## 8. Cleanup 设计

默认 `logging` namespace 的 cleanup 可删除日志分路拥有的 cluster role、ECK Operator、临时本地 Registry 和节点覆盖，并恢复测试前 StorageClass/taint。指定其他 `LOGGING_NAMESPACE` 时只允许删除该 namespace，不能触碰当前路线的 cluster-scoped 资源。这个边界用于正常、失败和 interrupted 三种状态的隔离演练。

cleanup 必须可重复执行。验证不仅检查 namespace 不存在，还检查 Operator、测试进程、Registry 数据、节点 hosts/containerd 临时覆盖和 taint 恢复。新的 fixture、finalizer 或集群级对象必须同步扩展 cleanup 与单测，不能依赖 namespace 级联删除碰运气。

## 9. 文档与官方依据

客户文档只保留本目录的 `operations-manual.md`、`technical-whitepaper.md` 和 `development-manual.md`。新增配置、Secret、端口、资源名、版本、主路线或回滚边界时三份文档必须同步，技术白皮书与开发手册不能合并。

官方核验入口：

- ECK 3.5：<https://www.elastic.co/guide/en/cloud-on-k8s/3.5/index.html>
- Elasticsearch：<https://www.elastic.co/guide/en/elasticsearch/reference/current/index.html>
- Fluent Bit：<https://docs.fluentbit.io/manual>
- Loki HTTP API 与升级：<https://grafana.com/docs/loki/latest/reference/loki-http-api/>、<https://grafana.com/docs/loki/latest/setup/upgrade/>
- Alloy Kubernetes 安装：<https://grafana.com/docs/alloy/latest/set-up/install/kubernetes/>

评审固定版本行为时优先对应 tag 的官方发布说明、Chart/CRD 和源码，不用 latest 页面覆盖固定版本事实。链接、版本、许可证和兼容性在每次交付时重新核验。

## 10. 配置到资源的完整映射

下面的映射是评审、故障定位和变更影响分析的唯一索引。新增变量必须同时更新 `conf/config.yml`、Jinja 模板、本文和文档契约测试。

| 配置 | direct / Kafka 资源 | Loki 资源 | 变更风险 |
| --- | --- | --- | --- |
| `logging_install` | 是否执行整个日志任务 | 是否执行整个日志任务 | 关闭只允许无资源，不得保留孤儿对象 |
| `logging_solution` | `efk.yaml` 与 Fluent Bit 分支 | Loki/Alloy Helm values | 改变路线会触发 ownership 拒绝 |
| `logging_efk_delivery` | direct 或 Kafka Topic/Logstash | 不适用 | 不得双写 |
| `logging_efk_es_nodes` | Elasticsearch NodeSet 节点调度 | 不适用 | 必须三台不同可调度节点 |
| `logging_efk_storage_class`/`logging_loki_storage_class` | ES/Loki PVC | Loki WAL/cache PVC | PVC 创建后不可原地更换 StorageClass |
| `logging_efk_snapshot_*` | S3 repository/SLM | 不适用 | endpoint、CA、bucket 变化影响恢复 |
| `logging_loki_bucket_*` | 不适用 | chunks/ruler/admin 前缀 | schema 与桶前缀必须保持一致 |

模板评审必须回答三个问题：资源由谁拥有、Secret 从哪里来、失败后哪个控制器负责重试。若回答不了，不能通过代码评审。

## 11. EFK 渲染与调谐细节

`logging.yml` 的顺序是有意设计的：先验证路线和 ownership，再创建 namespace；先安装 Ingress 并等待 controller Ready，再应用依赖 admission 的资源；先应用 ECK CRD/Operator，再应用 Elasticsearch/Kibana；先等待 TLS 和管理 Secret，再创建 writer、ILM、模板、快照仓库和采集 Secret；最后才发布 Fluent Bit。任何一步失败都不得继续后续资源。

ECK 清单使用 server-side apply 与独立 field manager；业务清单使用 `kubeauto-logging-efk`。CRD 已由 Kubeauto field manager 管理时，Helm 或 kubectl 不得重新夺取同一字段；升级 CRD 必须先查 schema 与 conversion webhook，再进行 dry-run。

Elasticsearch writer bootstrap 使用临时 port-forward 和 ECK CA。port-forward 只用于 API 诊断和引导，不写入工作负载配置；API 请求必须校验证书 SAN。引导生成的采集 Secret 只含专用 writer 凭据，绝不能复制 `elastic` 管理密码。

Kafka-buffer 的资源依赖顺序是 Kafka CR Ready、KafkaUser Secret 出现、Topic/ACL 生效、客户端 CA 派生、Fluent Bit Kafka output、Logstash consumer group。不能通过把 SASL 密码写入 ConfigMap 或 values 绕过 Secret 尚未就绪。

## 12. Loki/Alloy values 设计细节

Loki values 必须明确写出：三个 single-binary 副本、required anti-affinity、`replication_factor: 3`、TSDB schema、对象存储 endpoint/CA、WAL、Gateway Basic Auth、资源 requests/limits、PDB 和 ServiceMonitor。省略这些字段会让 Chart 使用随版本变化的默认值，不能视为生产配置。

Alloy values 必须明确写出：CRI 文件 glob、Kubernetes discovery、metadata relabel、低基数标签、positions 路径、write endpoint、TLS CA、客户端 Secret 和重试/批量参数。将任意 Pod label 全量提升为 Loki label 是禁止的高基数变更；增加标签前必须给出基数预算、查询用例和压测证据。

## 13. 代码变更工作流

1. 新字段先在配置中提供默认值和关闭行为，明确是否影响两条路线。
2. 在模板中使用 StrictUndefined 兼容写法，给资源、Secret、Service、selector 加稳定名称。
3. 在 `test_logging_delivery.py` 增加渲染、互斥、ownership、marker 和禁止泄露断言。
4. 更新三份客户文档的配置表、运维动作和机制边界。
5. 更新 `logging-test-matrix.yaml`，将受影响 case 置为 pending，禁止沿用历史 pass。
6. 执行 focused contract；只有 focused 通过才允许远端 runner。

修改已有资源前先判断字段由 Kubernetes、ECK、Strimzi、Helm 还是 Kubeauto field manager 拥有。对 Operator 生成字段直接 patch 会在下一轮调谐被覆盖；正确做法是修改 CR 或 values，再观察 generation、observedGeneration 和 controller event。PVC、Secret、TLS CA、索引模板和数据 schema 的修改必须写出迁移与回滚路径。

## 14. 证据边界与失败分类

| 证据 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| Helm lint/template | YAML、values 和模板语法 | API schema、Admission、业务可用 |
| server dry-run | API schema、字段类型和准入 | 控制器已完成调谐、数据可写 |
| Pod Ready | 容器探针成功 | 日志已接收、查询和恢复 |
| 后端 HTTP 200 | 本次请求被接受 | 快照成功、历史数据完整 |
| `LOGGING_CASE_PASS` | 一个明确场景完成 | 其他场景或另一条路线 |
| clean verify | 测试资源按边界回收 | 客户生产数据已备份 |

失败必须分类为产品、test-gate、环境、供应链、运行时或 Kubernetes/controller。不能把“脚本未等待控制器收敛”报告为产品故障，也不能用历史通过日志替代当前 clean evidence。测试门禁修复必须先补精确 focused contract，再运行一次完整回归。

## 15. 交付前审查

版本、Chart、镜像 digest、许可证、官方链接、入口、Secret、端口、资源名和清理边界必须在三份文档中一致。配置示例只能包含资源名，不得包含密码、Token、私钥或动态代理。

固定交付命令：

```bash
./.venv/bin/python tests/helpers/validate-test-matrix.py tests/logging-test-matrix.yaml --require-pass
bash tests/run_unit_tests.sh
bash tests/run_enterprise_regression.sh --logging-only
```

必须取得 `LOGGING_FULL_GATE_PASS`、`LOGGING_GATE_EXIT rc=0`、零 failure marker、`44/44` 和 `LOGGING_CLEAN_VERIFY_PASS`。三条路线都要有当前证据；只跑其中一条不能宣称日志能力交付。

版本变更后还要检查六仓常量、Dockerfile、CI 双推矩阵、TalkEdu manifest digest、Docker Hub 回退、下载列表和文档链接。任何新镜像先进入 ext-images 制品门禁，再开始现场测试。

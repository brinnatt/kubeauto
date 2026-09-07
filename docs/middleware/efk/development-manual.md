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

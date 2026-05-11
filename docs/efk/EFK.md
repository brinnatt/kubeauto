# T9、日志

## T9.1、日志架构

监控篇（T4）管的是**指标和告警**：CPU、延迟、错误率这类数，用来盯健康、触发告警。本篇管的是**日志**：谁在什么时间、打印了什么内容，用来还原现场、排查根因。

**生产上这两套一般都要上**，但职责不同：**指标**适合按时间聚合、做告警规则；**日志**适合按关键字、上下文检索。

选型上也就不同：指标常用 Prometheus 这类时序库，日志常用 ES、OpenSearch、托管日志等。

**不要把两类数据混进同一种存储里凑合用**（例如用日志库硬扛全量指标，或把海量原始日志塞进时序库），既贵又难维护，也不是常见做法。

本节对齐 [Kubernetes：Logging Architecture](https://kubernetes.io/docs/concepts/cluster-administration/logging/)，并结合生产习惯写清：**镜像固定 tag、校验日期、可照着 apply 的 YAML**。文中配图来自 **Kubernetes 官网同一套素材**（已下载到本文 `images/`，便于离线阅读）。

### T9.1.1、概念与依据

**应用日志**：看业务行为、排错、审计。**容器里最省事的做法**：写 **stdout / stderr**，由运行时交给 kubelet，再在节点上落盘；平时用 `kubectl logs` 就能看。

**集群级日志（cluster-level logging）**：官方定义是——日志的**存储与生命周期**要和节点、Pod、容器**脱钩**，否则容器崩了、Pod 被赶走、节点挂了，你只依赖本地文件会抓瞎。Kubernetes **不提供**日志存储产品，只提供 API 与节点侧行为；**存哪、查哪、保留多久**由你自己选（EFK、托管日志、Kafka 等）。

**和监控怎么配合**：业务容器可以同时做两件事——一边按 Prometheus 约定暴露 **`/metrics` 端点**（给监控抓），一边往 **stdout/stderr 打日志**（给日志采集收）。**前者不等于后者**：不能指望只靠日志解决告警，也不能只靠指标还原每一行日志原文；两条链路各自部署、各自存储即可。

```mermaid
flowchart LR
  subgraph obs["可观测"]
    M[指标 Prometheus]
    L[日志 后端]
  end
  APP[工作负载] --> M
  APP --> L
```

---

### T9.1.2、标准输出与节点行为

下面这张图对应官方「节点如何处理容器日志」的说明：运行时接管 stdout/stderr，与 kubelet 之间用 **CRI 日志格式**衔接（具体实现随运行时变化，但对 kubelet 的接口是统一的）。

![节点级日志示意（Kubernetes 官网）](./images/logging-node-level.png)

**你可以把一条日志从容器里到 `kubectl logs` 的路径理解成：**

```mermaid
flowchart LR
  C[容器 stdout/stderr]
  R[容器运行时 CRI]
  K[kubelet 读日志文件]
  U["kubectl logs 与 API"]
  C --> R --> K --> U
```

**kubelet 轮转（生产必看）**：自 v1.21 起相关能力已稳定。可在 [kubelet 配置](https://kubernetes.io/docs/reference/config-api/kubelet-config.v1beta1/) 里调 `containerLogMaxSize`（默认约 10Mi）、`containerLogMaxFiles`（默认约 5）等；大流量集群还可关注 `containerLogMaxWorkers`、`containerLogMonitorInterval`。目的是**别让单容器日志把节点盘打满**。

**`kubectl logs` 的边界**：官方写明——通常只能看到**当前正在写的那份**文件；若已轮转，更早的不一定还能从这个命令拿到。**长期留存、合规检索**必须走集群级后端，别指望 `kubectl logs` 当归档库。

**可选（Alpha）**：从 v1.32 起有 `PodLogsQuerySplitStreams` 特性门，可把 stdout / stderr 分开查，默认关。见官方 [Container log streams](https://kubernetes.io/docs/concepts/cluster-administration/logging/#container-log-streams)。

**最小示例**（与官方 [counter-pod.yaml](https://k8s.io/examples/debug/counter-pod.yaml) 同结构，镜像与监控篇一致）：

```yaml
# counter-pod.yaml
apiVersion: v1
kind: Pod
metadata:
  name: counter
spec:
  containers:
    - name: count
      image: busybox:1.37
      args:
        - /bin/sh
        - -c
        - 'i=0; while true; do echo "$i: $(date)"; i=$((i+1)); sleep 1; done'
```

```bash
kubectl apply -f counter-pod.yaml
kubectl logs counter
kubectl logs counter -c count
kubectl logs counter --previous
```

重复实验若遇名称冲突：`kubectl delete pod counter --ignore-not-found`。

> **版本与镜像约定（与监控篇一致）**
>
> **`image:` 一律固定 tag**；升级前到 **GitHub Releases** 或镜像仓库核对 **latest 稳定版**（非 prerelease），并更新下表与校验日期。  
>
> **本文同步校验日期：2026-04-21**  
>
> | 组件 | 镜像 / 标签 | 说明 |
> |------|-------------|------|
> | busybox | `busybox:1.37` | 与 [T4 监控篇](../prometheus/prometheus.md) 一致 · [Hub](https://hub.docker.com/_/busybox/tags) |
> | Fluent Bit | `fluent/fluent-bit:5.0.3` | 节点/sidecar 常用 · [Releases](https://github.com/fluent/fluent-bit/releases/latest) |
> | 官方文档中的 fluentd 示例 | `registry.k8s.io/fluentd-gcp:1.30` | 与 [官方页面示例](https://kubernetes.io/docs/concepts/cluster-administration/logging/) 同源，偏 GKE；**自建集群请改 OUTPUT**，勿照搬 `google_cloud` |

---

### T9.1.3、三类集群级路径

官方在 [Cluster-level logging architectures](https://kubernetes.io/docs/concepts/cluster-administration/logging/#cluster-level-logging-architectures) 里只归纳三类，生产里一般是「三选一或组合」。

```mermaid
flowchart LR
  A[节点 DaemonSet 代理] --> BE[(日志后端)]
  B[Sidecar] --> BE
  C[应用直推] --> BE
```

| 路径 | 一句话 | 常见场景 |
|------|--------|----------|
| 节点代理 | 每节点一个采集进程，读本节点容器日志目录，再发到后端 | **默认首选**，不动业务镜像 |
| Sidecar | 跟业务同 Pod：要么把文件日志打到 sidecar 的 stdout，要么在 sidecar 里跑采集进程 | 写文件、多格式分流、节点策略不满足时 |
| 应用直推 | 进程内 SDK 直接发到后端 | 强绑定某云/厂商 SDK，或作补充 |

---

### T9.1.4、落地与示例

这一节把上面三种「集群级日志」拆成四段，每一段都配有**官方示意图**和**能直接 apply 的 YAML**（需要你先装好集群和 `kubectl`）。阅读顺序建议：先看 **T9.1.4.1**，这是大多数公司默认采用的形态；只有业务把日志写进**文件**、或者节点上统一采集满足不了时，再往下看 Sidecar 两种；应用内直推放在最后，用得相对少。

**怎么选，一句话对照**：

- **多数情况**：用 **T9.1.4.1 节点代理**就够了，业务继续往 stdout 打日志即可。  
- **业务必须写文件**：先看能不能改成 stdout；改不了，再用 **T9.1.4.2** 或 **T9.1.4.3**（Sidecar 会多占资源，要心里有数）。  
- **应用直推（T9.1.4.4）**：Kubernetes 不管你怎么实现，适合已经接了某云日志 SDK 的场景，或当补充手段。

---

#### T9.1.4.1、节点代理

![使用节点级 logging agent（Kubernetes 官网）](./images/logging-with-node-agent.png)

**这是在干什么**：在**每一台工作节点**上跑一个采集程序（一般用 **DaemonSet** 部署，保证每台机器都有一个 Pod）。这个程序去读「本节点上、所有容器打出来的日志文件」（常见目录是 `/var/log/pods/...`，具体以你集群为准），解析后再发到 Elasticsearch、Kafka、云厂商日志服务等。

**为什么常用**：业务不用改镜像、不用加边车，只要像平常一样往 **标准输出** 打日志，节点上的代理自然会收集到。

**什么时候不够**：代理默认跟的是「标准输出落到节点上的那套日志」。如果你的程序只往**自己容器里某个路径**写文件，又不跟别的容器共享卷，节点上的统一采集**可能读不到**，这时就要用后面的 Sidecar，或者改程序写法。

```mermaid
flowchart TB
  subgraph n["工作节点"]
    P1[Pod]
    P2[Pod]
    D[容器日志目录]
    X[DaemonSet 代理]
  end
  P1 --> D
  P2 --> D
  D --> X
  X --> BK[(后端)]
```

---

#### T9.1.4.2、Sidecar 流式

![流式 Sidecar（Kubernetes 官网）](./images/logging-with-streaming-sidecar.png)

**这是在干什么**：业务坚持把日志写进**文件**（例如 `/var/log/1.log`），你又希望后面仍然走「标准输出 → 节点采集」这条老路。做法是：加一个（或多个）**小容器**，和主容器**共享同一块卷**，用小容器里的 `tail -F` 去读文件，把读到的内容**打印到自己的标准输出**。这样一来，kubelet 照样按「容器标准输出」收集，节点上的日志代理也能接着收。

**和上一段的关系**：上一段是「节点上一个代理管全机」；这里是「**单个 Pod 里多几个容器配合**」，专门解决「写文件」的问题。

**操作要点**：主容器写文件；**每个要单独看的日志流，对应一个 sidecar**，下面示例里是两个文件，所以有两个 sidecar。用 `kubectl logs` 时可以按**容器名**分别看。

```mermaid
flowchart LR
  F[应用写文件]
  T[sidecar tail -F]
  O[sidecar stdout]
  F --> T --> O
  O --> K["kubelet 与节点代理"]
```

```yaml
# two-files-counter-pod-streaming-sidecar.yaml
apiVersion: v1
kind: Pod
metadata:
  name: counter
spec:
  containers:
    - name: count
      image: busybox:1.37
      args:
        - /bin/sh
        - -c
        - >
          i=0;
          while true;
          do
            echo "$i: $(date)" >> /var/log/1.log;
            echo "$(date) INFO $i" >> /var/log/2.log;
            i=$((i+1));
            sleep 1;
          done
      volumeMounts:
        - name: varlog
          mountPath: /var/log
    - name: count-log-1
      image: busybox:1.37
      args: [/bin/sh, -c, 'tail -n+1 -F /var/log/1.log']
      volumeMounts:
        - name: varlog
          mountPath: /var/log
    - name: count-log-2
      image: busybox:1.37
      args: [/bin/sh, -c, 'tail -n+1 -F /var/log/2.log']
      volumeMounts:
        - name: varlog
          mountPath: /var/log
  volumes:
    - name: varlog
      emptyDir: {}
```

```bash
kubectl apply -f two-files-counter-pod-streaming-sidecar.yaml
kubectl logs counter -c count-log-1
kubectl logs counter -c count-log-2
```

**磁盘上多占一份**：同一条日志，文件里有一份，经 sidecar 打出标准输出后，节点上还会再记一份。官方也提醒这样会**接近双倍占用**。所以能改的话，还是尽量让应用直接打 **stdout/stderr**，让 kubelet 去轮转，更省事。

---

#### T9.1.4.3、Sidecar 采集器

![Sidecar 内跑 logging agent（Kubernetes 官网）](./images/logging-with-sidecar-agent.png)

**和 T9.1.4.2 的差别**：上一段是「用 `tail` 把文件**转成**标准输出，再走节点上那套采集」。这一段是「在 Pod 里再跑一个**真正的采集程序**（例如 Fluent Bit、fluentd），直接从共享卷里 **tail 文件**，然后**按你的配置发到后端**（不一定再经过业务容器的标准输出）」。

**什么时候会用到**：例如你希望**解析规则、过滤规则**只作用在这一个应用上，不想在节点全局 DaemonSet 里写一大坨；或者多租户隔离、合规要求必须在 Pod 内完成转发等。

**代价**：每个业务 Pod 都要多跑一个采集容器，**CPU、内存要算进容量规划**。另外，`kubectl logs` 只能看**某个容器的标准输出**，**看不到**「已经发到 Elasticsearch 里」的那条日志；要看转发是否成功，要么看**采集容器自己的日志**，要么到**日志后端**里查。

**和官方文档对齐**：[Kubernetes 文档](https://kubernetes.io/docs/concepts/cluster-administration/logging/#sidecar-container-with-a-logging-agent) 里的示例用的是 **fluentd**，输出写到 **`google_cloud`**，镜像为 **`registry.k8s.io/fluentd-gcp:1.30`**（给 **GCP** 用的）。自建集群一般是 **Fluent Bit** 或自管 fluentd，把配置里的 **`OUTPUT`** 改成你的 ES、Kafka、HTTP 等。下面用 **Fluent Bit 5.x**，先把日志打到 **stdout**，方便你确认管道通了；上线时只改 **`[OUTPUT]`** 即可。

```yaml
# fluent-bit-sidecar-cm.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
data:
  fluent-bit.conf: |
    [SERVICE]
        Flush     1
        Log_Level info

    [INPUT]
        Name  tail
        Tag   count.format1
        Path  /var/log/1.log

    [INPUT]
        Name  tail
        Tag   count.format2
        Path  /var/log/2.log

    [OUTPUT]
        Name  stdout
        Match *
        Format json_lines
```

```yaml
# counter-with-fluent-bit-sidecar.yaml
apiVersion: v1
kind: Pod
metadata:
  name: counter
spec:
  containers:
    - name: count
      image: busybox:1.37
      args:
        - /bin/sh
        - -c
        - >
          i=0;
          while true;
          do
            echo "$i: $(date)" >> /var/log/1.log;
            echo "$(date) INFO $i" >> /var/log/2.log;
            i=$((i+1));
            sleep 1;
          done
      volumeMounts:
        - name: varlog
          mountPath: /var/log
    - name: count-agent
      image: fluent/fluent-bit:5.0.3
      command: ["/fluent-bit/bin/fluent-bit"]
      args: ["-c", "/fluent-bit/etc/fluent-bit.conf"]
      volumeMounts:
        - name: varlog
          mountPath: /var/log
        - name: config-volume
          mountPath: /fluent-bit/etc
  volumes:
    - name: varlog
      emptyDir: {}
    - name: config-volume
      configMap:
        name: fluent-bit-config
```

```bash
kubectl apply -f fluent-bit-sidecar-cm.yaml
kubectl apply -f counter-with-fluent-bit-sidecar.yaml
kubectl logs counter -c count-agent
```

上面命令里，`kubectl logs` 看到的是 **Fluent Bit 容器**打到标准输出的内容（示例里配成了 JSON 行）。等你把 **`[OUTPUT]`** 改成真实后端后，要到 **Kibana、OpenSearch Dashboards** 或云控制台里去查业务日志。[官方 fluentd 示例](https://kubernetes.io/docs/concepts/cluster-administration/logging/#sidecar-container-with-a-logging-agent) 可以对照着看：结构一样，只是采集软件和 **`OUTPUT`** 随环境换。

**（插槽：把 OUTPUT 接到 OpenSearch/Elasticsearch 等并验证检索通过后，可在此处补一张 Discover/检索界面截图。）**

---

#### T9.1.4.4、应用直推

![从应用直接暴露/推送日志（Kubernetes 官网）](./images/logging-from-application.png)

**这是在干什么**：不经过节点上的统一采集，应用在代码里用 **SDK 或 HTTP** 等，把日志**直接发到**日志平台或消息队列。

**Kubernetes 的态度**：官方写明，这类做法**不在 Kubernetes 核心概念里细讲**。也就是说：**怎么鉴权、失败重试、打爆了怎么办（背压）、字段长什么样**，都要你们自己在应用或 SDK 层设计。适合已经**绑定了某家云日志**、或必须绕过节点采集策略的情况；也可以和节点采集**同时存在**，作为补充。

```mermaid
flowchart LR
  APP[应用 Pod] --> SDK["SDK 或协议"]
  SDK --> BE[(日志后端)]
```

---

下一节 **「T9.2、日志 EFK」** 会用 **ECK** 部署 **Elasticsearch、Kibana**，再用 **Fluent Bit DaemonSet** 把节点上的容器日志送进 ES（与本文 **T9.1** 的采集组件一致）。若团队已经统一用 **Fluentd**，只要把输出指向同一套 ES 即可。

## T9.2、日志 EFK

**Elasticsearch** 负责日志存储与检索；**Kibana** 提供查询与可视化；**Fluent Bit** 以 **DaemonSet** 在各节点读取容器日志并写入 ES，采集模型与 **T9.1** 节点代理一致。已标准化 **Fluentd** 时，将输出指向本节同一 ES 端点即可。

**Elastic Cloud on Kubernetes（ECK）** 是 Elastic 在 Kubernetes 上编排 Elasticsearch、Kibana 等的官方 Operator，负责自定义资源生命周期、TLS 与滚动升级。本节操作步骤依据 ECK 官方安装与部署文档；Fluent Bit 写入 ES 的行为依据 [Elasticsearch 输出插件](https://docs.fluentbit.io/manual/pipeline/outputs/elasticsearch)。

**官方文档**：[Elastic Cloud on Kubernetes](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s) · [ECK Guide](https://www.elastic.co/guide/en/cloud-on-k8s/current/index.html)

### T9.2.1、数据怎么流

应用将日志写入 **stdout/stderr**，由容器运行时落盘至节点；**Fluent Bit** 读取节点上的容器日志文件并转发至 **Elasticsearch**；**Kibana** 查询 ES 中的索引。**elastic-operator** 根据 `Elasticsearch`、`Kibana` 等 CR 管理 Pod、PVC 与证书，不参与日志数据面转发。

```mermaid
flowchart TB
  subgraph node["工作节点"]
    P[业务 Pod stdout/stderr]
    L[容器日志在节点上的路径]
    FB[Fluent Bit Pod]
  end
  subgraph logging_ns["命名空间 logging（工作负载）"]
    ES[(Elasticsearch)]
    KB[Kibana]
  end
  subgraph esys["命名空间 elastic-system"]
    OP[elastic-operator]
  end
  P --> L --> FB -->|HTTPS 9200| ES
  ES --> KB
  OP -.监视 CR 并协调 Pod/PVC/证书.-> ES
  OP -.监视 CR.-> KB
```

---

### T9.2.2、版本与校验

以 **GitHub Release、Elastic 下载页、[支持矩阵](https://www.elastic.co/support/matrix#matrix_kubernetes)** 为版本准绳；变更时同步更新下表与 GitOps 中的固定镜像 / 清单 URL。

**本文同步校验日期：2026-04-21**

| 组件 | 版本 / 来源 | 说明 |
|------|-------------|------|
| ECK Operator | `3.3.2` | [YAML 安装](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/install-using-yaml-manifest-quickstart) 中的 `crds.yaml`、`operator.yaml` · [Releases](https://github.com/elastic/cloud-on-k8s/releases) · [下载](https://www.elastic.co/downloads/elastic-cloud-kubernetes) |
| Kubernetes | **1.31–1.35** | [ECK：Supported versions](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s#k8s-supported) |
| Elastic Stack（`spec.version`） | **9.3.3** | 与 [Elasticsearch 部署快速入门](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/elasticsearch-deployment-quickstart) 示例一致；ECK 支持 Stack **8+、9+**；默认镜像仓库 **`docker.elastic.co`** |
| Fluent Bit | `fluent/fluent-bit:5.0.3` | 与 **T9.1** 一致 · [Releases](https://github.com/fluent/fluent-bit/releases) |

---

### T9.2.3、前提条件

- **命名空间**：Elasticsearch、Kibana、Fluent Bit 部署在 **`logging`**（`kubectl create namespace logging`）。ECK Operator 运行在 **`elastic-system`**（由 `operator.yaml` 创建）；业务负载勿与 Operator 混部于 **`elastic-system`**。
- **存储**：数据目录使用 **`volumeClaimTemplates`** 申请 PVC；将示例中的 **`storageClassName`** 替换为集群内可用 StorageClass（`kubectl get sc`）。试跑可用单节点与小容量盘；生产按容量、副本、可用区规划，参见 [Volume claim templates](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/volume-claim-templates)。
- **资源与节点**：示例 CPU/内存为联调下限；生产按数据量与查询负载扩容。节点需满足内存与 **`vm.max_map_count`** 等要求，参见 [Elasticsearch：系统配置与堆](https://www.elastic.co/guide/en/elasticsearch/reference/current/setup-configuration-memory.html)。节点可调度的可用内存不足约 **2GiB** 时，ES Pod 可能持续 **Pending**。
- **`node.store.allow_mmap`**：快速入门默认 **`false`**；生产是否启用 **mmap** 及调优见 [Virtual memory](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/virtual-memory)。
- **网络（托管集群）**：**EKS** 须允许控制面与节点间 **TCP 443** 通信，以满足 **ValidatingWebhook** 要求，见 YAML 安装说明中的 **EKS** 章节。**GKE** 须具备完成 RBAC 绑定的管理员权限。
- **凭据**：ECK 为 **`elastic`** 用户生成密码并存入 Secret；**禁止**将密码写入 Git 仓库或 ConfigMap 明文。

---

### T9.2.4、安装 ECK Operator

**参考文档**

| 内容 | 链接 |
|------|------|
| 安装总览（YAML、Helm、OpenShift、离线等） | [Guide：Install ECK](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-installing-eck.html) · [Docs：Install ECK](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/install) |
| kubectl：CRD 与 Operator | [YAML manifests](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/install-using-yaml-manifest-quickstart) |
| Helm | [Helm chart](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/install-using-helm-chart) |
| 离线 / 私有镜像仓库 | [Air-gapped](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/air-gapped-install) |
| 升级 Operator | [Upgrade ECK](https://www.elastic.co/docs/deploy-manage/upgrade/orchestrator/upgrade-cloud-on-k8s) |

安装将写入集群级 **CRD**、**elastic-system** 命名空间、RBAC、**ValidatingWebhookConfiguration** 等对象，清单见 YAML 安装说明。**删除** ECK 相关 **CRD** 将级联删除集群内由该 Operator 管理的 Elastic 自定义资源，须在变更流程中评估影响。

```mermaid
flowchart LR
  S1["kubectl create -f .../crds.yaml"] --> S2["kubectl apply -f .../operator.yaml"]
  S2 --> R["elastic-operator Running"]
  R --> OK["可创建 Elasticsearch / Kibana CR"]
```

**安装**（版本与官方 YAML 快速入门一致，便于在流水线中固定 URL）：

```bash
kubectl create -f https://download.elastic.co/downloads/eck/3.3.2/crds.yaml
kubectl apply -f https://download.elastic.co/downloads/eck/3.3.2/operator.yaml
```

**验收**：

```bash
kubectl -n elastic-system logs -f statefulset.apps/elastic-operator
kubectl -n elastic-system get pods
```

**`elastic-operator-0`** 为 **`1/1 Running`** 后继续后续步骤。**Helm** 或 **离线**部署按上表文档配置 Chart 参数、镜像同步与 **`container-registry`** 等。

---

### T9.2.5、部署 Elasticsearch（ECK）

在 **`logging`** 命名空间应用 Elasticsearch CR。YAML 在 [Elasticsearch 部署快速入门](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/elasticsearch-deployment-quickstart) 基础上补充 **resources**、**volumeClaimTemplates** 与 **`node.roles`**。**`count: 1`** 用于试跑与联调；**生产**将 **`count`** 提升至 **3** 及以上，并按官方说明实施高可用、索引生命周期（ILM）与快照等。

Elastic 当前公开文档未单独提供「ECK 编排」位图；安装 Operator 时创建的集群级 **CRD**、**ValidatingWebhookConfiguration** 等见 [YAML 安装说明](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/install-using-yaml-manifest-quickstart)。下图归纳 **`logging`** 命名空间内 **Elasticsearch CR** 经 **elastic-operator** 落地后的主要对象及 **Fluent Bit** 写入路径（与 **T9.2.1**、**T9.2.6** 衔接）。

```mermaid
flowchart TB
  subgraph adm[管理员]
    KA["kubectl apply Elasticsearch CR"]
  end
  subgraph esys[elastic-system]
    OP[elastic-operator]
  end
  subgraph logns[logging]
    ESCR["Elasticsearch CR quickstart"]
    STS[Elasticsearch StatefulSet]
    POD[Elasticsearch Pod]
    PVC[PVC 数据卷]
    SVC["Service quickstart-es-http HTTPS:9200"]
    S_TLS[HTTP 证书 Secret]
    S_EL["Secret quickstart-es-elastic-user"]
    FB[Fluent Bit DaemonSet]
  end
  KA --> ESCR
  ESCR --> OP
  OP --> STS
  OP --> PVC
  OP --> SVC
  OP --> S_TLS
  OP --> S_EL
  STS --> POD
  POD --> PVC
  FB -->|HTTPS 写索引| SVC
```

```yaml
# elasticsearch-eck.yaml（请把 storageClassName 改成你的 StorageClass）
apiVersion: elasticsearch.k8s.elastic.co/v1
kind: Elasticsearch
metadata:
  name: quickstart
  namespace: logging
spec:
  version: 9.3.3
  nodeSets:
    - name: default
      count: 1
      config:
        node.roles: ["master", "data", "ingest"]
        node.store.allow_mmap: false
      podTemplate:
        spec:
          containers:
            - name: elasticsearch
              resources:
                limits:
                  memory: 2Gi
                  cpu: "1"
                requests:
                  memory: 2Gi
                  cpu: "500m"
      volumeClaimTemplates:
        - metadata:
            name: elasticsearch-data
          spec:
            accessModes:
              - ReadWriteOnce
            resources:
              requests:
                storage: 30Gi
            storageClassName: standard
```

```bash
kubectl apply -f elasticsearch-eck.yaml
kubectl -n logging get elasticsearches.elasticsearch.k8s.elastic.co
kubectl -n logging get es
kubectl -n logging get pods -l elasticsearch.k8s.elastic.co/cluster-name=quickstart
```

> **运维说明**  
> - `kubectl get` 使用的是 CRD 注册的 **复数资源名**（本例为 **`elasticsearches`**），与 YAML 中 `kind: Elasticsearch` 的写法不同；在 CRD 已安装的集群上可使用短名 **`es`**。  
> - 若需核对 API 资源清单：`kubectl api-resources --api-group=elasticsearch.k8s.elastic.co`。

**就绪判定**：`kubectl -n logging get es`（或上表完整资源名）里 **PHASE** 为 **Ready**；**HEALTH** 以 **green** 为目标。若仅 **单数据节点** 而索引 **副本数大于 0**，副本无法分配到其他节点时，集群可能长期为 **yellow**（主分片可用、副本未齐）。联调环境可将副本调为 **0**；生产冗余需 **增加数据节点** 并保留合理副本策略。

集群内访问 ES 使用 Service **`quickstart-es-http`**：**HTTPS**、**9200**，证书由 ECK 管理。扩展配置见 [Elasticsearch configuration](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/elasticsearch-configuration)、[Configure deployments](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/configure-deployments)。

---

### T9.2.6、部署 Kibana（ECK）

按 [Kibana 部署快速入门](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/kibana-instance-quickstart) 创建 **`Kibana`** 自定义资源；**`spec.version`** 与 **Elasticsearch** 保持一致（本文与 **T9.2.5** 示例均为 **9.3.3**，升级时请对照 [ECK 与 Stack 支持矩阵](https://www.elastic.co/support/matrix#matrix_kubernetes) 与发行说明改同一套版本号）。

清单文件名 **`kibana-eck.yaml`**，建议和 **`elasticsearch-eck.yaml`** 放在同一目录；下面第一段是**最小配置**（默认 ClusterIP，配合 **方式一** 的 port-forward）。需要 NodePort 或 LoadBalancer 时，在**同一份文件**的 **`spec`** 里加上 **方式二** 的 **`http`** 段，再执行 **`kubectl apply`**，不要只改 Service。

```yaml
# kibana-eck.yaml（最小示例；NodePort 见方式二整段示例）
apiVersion: kibana.k8s.elastic.co/v1
kind: Kibana
metadata:
  name: quickstart
  namespace: logging
spec:
  version: 9.3.3
  count: 1
  elasticsearchRef:
    name: quickstart
```

```bash
kubectl apply -f kibana-eck.yaml
kubectl -n logging get kibanas.kibana.k8s.elastic.co
kubectl -n logging get kb
kubectl -n logging get pods -l kibana.k8s.elastic.co/name=quickstart
```

> **运维说明**：`kubectl get` 使用 **`kibanas`**（短名 **`kb`**），与 `kind: Kibana` 不同；使用单数 **`kibana`** 作为资源类型会报错 *the server doesn't have a resource type kibana*。  核对 API：`kubectl api-resources --api-group=kibana.k8s.elastic.co`。
>
> **就绪与访问**：Kibana 启动过程中会经历 **preboot** 与正式 HTTP 服务阶段，**5601** 为 **HTTPS**。短时内出现 **`Readiness probe ... unexpected EOF`** 多与探针命中进程切换窗口有关；以 **`kubectl get pods`** 最终 **Ready** 及日志中出现 **`Kibana is now available`** 作为可用判据。持续不 Ready 时检查 **内存配额**（默认 **1Gi** 仅适用于最小演示，生产按负载上调）及 **Elasticsearch** 是否已达 **Ready**（`kubectl get es`）。

**访问方式与网络**：ECK 创建的 HTTP Service 名一般为 **`quickstart-kb-http`**，默认 **ClusterIP**，仅在集群内可达；自定义与暴露方式见官方 [Accessing services](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-services.html)。

**方式一：`kubectl port-forward`（联调）**

在**已安装 kubectl、并已配置 kubeconfig 的管理机**上执行转发；监听的是**该机器的本机回环地址**，不是 Kubernetes 节点 IP。  

- 在 **Windows** 办公机安装 [kubectl](https://kubernetes.io/docs/tasks/tools/#kubectl-windows) 并指向集群后，于 **PowerShell** 执行下文命令，再在同一台 Windows 上用 **Edge / Chrome** 打开 **`https://127.0.0.1:5601`** 即可（**`127.0.0.1` 表示本机**，与操作系统是否为 Linux 无关）。  
- 若仅在 **无图形界面的 Linux 节点**（如 **master-01**）上 SSH 执行 `port-forward`，无法在「服务器本地」弹出浏览器；应在**带浏览器的管理机**上安装 kubeconfig 并转发，或改用 **方式二** / **Ingress**。

```bash
kubectl -n logging port-forward svc/quickstart-kb-http 5601:5601
```

协议为 **`https://127.0.0.1:5601`**（ECK 对 Kibana 启用 **TLS**）。证书校验按基线导入 **CA**（例如自 **Secret `quickstart-kb-http-certs-public`** 导出 `tls.crt`）或在受控环境临时信任。登录凭据见 **T9.2.7**（**`elastic`** 用户）。

**方式二：NodePort / LoadBalancer**  

不要指望 **`kubectl edit service quickstart-kb-http`** 长期生效：这个 Service 是 Operator 根据 **`Kibana`** 对象算出来的，你手改类型，下一轮调和又会被改回 **ClusterIP**。正确做法和官方 [Accessing services](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-services.html) 一样：在**上面的 `kibana-eck.yaml`** 里， **`spec`** 中 **`version` / `count` / `elasticsearchRef` 不动**，和它们并列加上 **`http.service.spec`**；需要公网负载均衡就把 **`type`** 换成 **`LoadBalancer`**。

```yaml
# kibana-eck.yaml（含 NodePort 的完整示例，可覆盖最小示例整文件）
apiVersion: kibana.k8s.elastic.co/v1
kind: Kibana
metadata:
  name: quickstart
  namespace: logging
spec:
  version: 9.3.3
  count: 1
  elasticsearchRef:
    name: quickstart
  http:
    service:
      spec:
        type: NodePort
        ports:
          - name: https
            port: 5601
            targetPort: 5601
            # nodePort: 30560   # 不写则由集群自动分配
```

执行 **`kubectl apply -f kibana-eck.yaml`**，再 **`kubectl get svc -n logging quickstart-kb-http`** 看 **NodePort**。浏览器访问 **`https://节点IP:NodePort`**，仍是 HTTPS。长期生产建议 **Ingress + TLS + 鉴权**，避免长期只暴露 NodePort 当唯一入口（暴露面大、审计也不方便）。

> **运维说明**  
>
> **`targetPort` 必须是 5601**（容器监听端口），和对外 **port** 别乱改；**`nodePort`** 要在集群允许范围内。  
>
> **Fleet** 是 Elastic 自带的 **Elastic Agent 统一管理**（在 Kibana 里配策略、装集成、纳管主机上的 Agent）。**本文 T9.2 用 Fluent Bit 采日志，可以不启用 Fleet。** 只有当你同时用 Fleet 又给 Kibana 开了 NodePort 时，历史上个别 ECK 小版本出过兼容性问题；若碰到，查你正在用的 **ECK 版本发行说明**。官方概念见 [Fleet 概述](https://www.elastic.co/guide/en/fleet/current/fleet-overview.html)。

**上线验收（对照做，做完算过）**

本文 **T9.2** 的目标是 **日志进 ES、在 Kibana 里能查**，不是上齐 SIEM/EDR。验收按下面几条做即可；日志里其它 INFO 大多不用逐条消化。

**必过项（少一条都不算上线成功）**

1. **`kubectl -n logging get pods`**：Kibana Pod **Ready**，Elasticsearch **Ready**（与 **T9.2.5** 一致）。  
2. 能打开 Kibana（**方式一** 或 **方式二** 或你们自己的 Ingress），用 **T9.2.7** 的 **`elastic`** 登录。  
3. **T9.2.8** 跑起来之后，在 **Discover** 里能搜到容器日志（或你们约定的索引/数据流）。

**常见日志：要不要管**

```mermaid
flowchart TD
  A[看 Kibana 容器日志] --> B{Pod Ready 且能登录能查日志?}
  B -->|否| C[先按上文查内存 ES 是否 Ready 与网络 TLS]
  B -->|是| D{是否只做本文日志方案?}
  D -->|是| E[health-diagnostic 中 Index does not exist 多为 INFO 写入运行说明即可]
  D -->|否 还要 Fleet 或安全中心| F[按 Elastic 官方单独做 Agent 与集成 别和日志验收混为一谈]
  A --> G{出现 ERROR 且界面功能真的坏了?}
  G -->|是| H[按当前 Stack 版本查官方排障与 ECK 文档]
  G -->|否 只是连不上 telemetry.elastic.co 等| I[内网环境常见 合规上按公司要求配置遥测 见下文链接]
```

**`health-diagnostic` 和 `PermissionError: Index does not exist` 是啥意思**

Kibana 里带着 **Security** 相关插件，启动后会跑一些内部检查，会去 Elasticsearch 里查一类给 **Endpoint 诊断、遥测**用的数据流。你按本文只装 **ES + Kibana + Fluent Bit**，**从来没装 Elastic Agent / Defend**，这些数据流本来就不存在。代码里把「索引不存在」也归到权限检查路径里打出来，所以你会看到 **`PermissionError: Index does not exist`**，前面往往是 **`[INFO]`** 不是 **`[ERROR]`**。一句话：**不等于集群坏了，也不挡你查日志。** 若以后要收 Endpoint 诊断数据或关掉诊断上报，以 Elastic 文档为准：[Elastic Defend 诊断数据](https://www.elastic.co/guide/en/security/current/endpoint-diagnostic-data.html)。产品侧对诊断数据流与系统角色的演进，可参考仓库说明 [elastic/kibana#85391](https://github.com/elastic/kibana/issues/85391)。

**许可证、Fleet、遥测：什么时候才要动**

- **许可证**：基础版本来就不会开放所有付费功能。日志里出现 *License is not available…* 之类，只要 **你要用的页面（例如 Discover）能用**，按公司采购流程决定是否买商业版；别被日志吓到。  
- **Fleet / Agentless**：只有你真的要用 **Fleet 纳管 Agent** 才去排相关 ERROR；本文日志路径不依赖 Fleet。  
- **往外发使用数据**：涉合规时，在 **`kibana.yml`** 里按官方 [Telemetry settings](https://www.elastic.co/docs/reference/kibana/configuration-reference/telemetry-settings) 配置（ECK 下通过 **`spec.config`** 或 **`SecureSettings`** 注入，具体见 [Kibana configuration（ECK）](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/kibana-configuration)）。改之前先在预发对一下当前小版本文档。

**生产环境还建议补上的配置（和上文同一份 CR）**

- **资源**：默认 **1Gi** 只够演示。生产在 **`spec.podTemplate`** 里给 **`kibana`** 容器写够 **requests/limits**，避免 OOM 和调度抖动，按官方 [Kibana configuration（ECK）](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/kibana-configuration) 做。  
- **对外访问**：正式用户走 **Ingress / Gateway**，TLS 和鉴权交给入口，不要长期靠 **`kubectl port-forward`**。  
- **版本**：**`spec.version`** 与 **Elasticsearch** 成套升级，先看 [ECK 支持矩阵](https://www.elastic.co/support/matrix#matrix_kubernetes)，再动生产。

---

### T9.2.7、elastic 用户密码

**`elastic` 是谁**：Elasticsearch 内置的超级用户（**`superuser`**），权限最大。ECK 部署 Elasticsearch 时会自动创建该用户，并把**随机生成的密码**放进 Kubernetes **Secret** 里，不会在 YAML 里明文写死。官方说明见 [Accessing services：取 elastic 密码](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-services.html#k8s-authentication)。

**Secret 叫什么**：固定规则是 **`{Elasticsearch 的 metadata.name}-es-elastic-user`**，和 **Elasticsearch CR 所在命名空间**一致。本文 **T9.2.5** 里资源名是 **`quickstart`**、命名空间是 **`logging`**，所以 Secret 全名是 **`quickstart-es-elastic-user`**。你若改了 `metadata.name`，Secret 前缀跟着变，**Kibana 登录、Fluent Bit、curl 测 ES** 都要用新名字取密码。

**Secret 里有什么**：键名就是 **`elastic`**（用户名），值是 **Base64** 存的一份密码字符串。下面两条命令取出来的都是**解码后的明文密码**，任选其一即可。

```bash
# 与本文示例一致：命名空间 logging，Elasticsearch 名 quickstart
kubectl -n logging get secret quickstart-es-elastic-user -o go-template='{{.data.elastic | base64decode}}{{"\n"}}'
```

```bash
# 等价写法（Linux / macOS 常见；Windows 若管道异常请用上一条）
kubectl -n logging get secret quickstart-es-elastic-user -o jsonpath='{.data.elastic}' | base64 -d
echo
```

**密码用在哪（和本文其它节对齐）**

- **Kibana 网页登录**：用户名填 **`elastic`**，密码用上面命令打印出来的值（见 **T9.2.6**）。  
- **Fluent Bit 写 ES**：**T9.2.8** 里用同一 Secret 做专用凭据，不要把密码写进 ConfigMap。  
- **命令行测 ES**：对 **`quickstart-es-http`** 做 HTTPS 访问时，**`-u elastic:密码`**，并带上 ECK 提供的 **CA**（见 **T9.2.8** 与官方 [K8s HTTPS 设置](https://www.elastic.co/docs/deploy-manage/security/k8s-https-settings)）。

**取不到 Secret 时**：先 **`kubectl -n logging get es`** 看 Elasticsearch 是否 **Ready**；再 **`kubectl -n logging get secret`** 核对是否真有 **`…-es-elastic-user`**，名称是否和 **`metadata.name`** 对得上。

> **生产建议**  
>
> **`elastic` 只宜管理员与应急使用**，权限过大。业务写入（如 Fluent Bit、应用）应在 Elasticsearch 里建**专用用户与角色**，只给必要索引权限；改密、轮转按公司安全制度，在 ES 侧操作后同步更新 K8s Secret 与引用方。ECK 还可配合自定义用户与证书，见 [Elasticsearch configuration](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/elasticsearch-configuration) 与官方安全文档。

---

### T9.2.8、Fluent Bit：DaemonSet 采集并写入 ES

本节把**每个节点**上的容器日志送进 **T9.2.5** 里那套 **Elasticsearch**，和 **T9.1** 说的「读节点上 `/var/log/containers`」是同一路径。采集组件用 **Fluent Bit**，镜像与 **T9.1**、**T9.2.1** 表里一致：**`fluent/fluent-bit:5.0.3`**（与 [GitHub Releases 当前最新稳定 tag](https://github.com/fluent/fluent-bit/releases/latest) 对齐，后续你只管把 tag 换成当时的 latest 再测一遍）。

写法按官方来：**输入与过滤**见 [Tail](https://docs.fluentbit.io/manual/pipeline/inputs/tail)、[Kubernetes Filter](https://docs.fluentbit.io/manual/pipeline/filters/kubernetes)；**写到 ES** 见 [Elasticsearch 输出](https://docs.fluentbit.io/manual/pipeline/outputs/elasticsearch)；**TLS** 见 [Transport security](https://docs.fluentbit.io/manual/administration/transport-security)。连 ECK 的 HTTPS 与证书怎么取，见 [K8s HTTPS 设置](https://www.elastic.co/docs/deploy-manage/security/k8s-https-settings)。

**前置条件（少一步后面必挂）**：**`kubectl -n logging get es`** 里 **Elasticsearch 已 Ready**；同一命名空间已有 Service **`quickstart-es-http`**、Secret **`quickstart-es-elastic-user`**、**`quickstart-es-http-certs-public`**（ECK 创建 ES 后就会有）。你若改了 **T9.2.5** 里的集群名 **`quickstart`**，下面所有资源名里的 **`quickstart`** 都要一起改。

```mermaid
flowchart TB
  subgraph node[某个工作节点]
    LP["/var/log/containers/*.log"]
    FB[Fluent Bit 容器]
    LP --> FB
  end
  subgraph logging[命名空间 logging]
    SVC["Service quickstart-es-http :9200 HTTPS"]
    ES[(Elasticsearch Pods)]
    FB -->|"HTTPS 写入 bulk"| SVC
    SVC --> ES
  end
```

**清单文件**：**`fluent-bit-es.yaml`**，建议和 **`elasticsearch-eck.yaml` / `kibana-eck.yaml`** 放同一目录，方便 Git 管理。

**步骤 1：单独放密码的 Secret（不要写进 ConfigMap）**

密码来源与 **T9.2.7** 相同。下面这条不依赖 **`base64 -d`**，Linux / Windows 上只要 **kubectl** 好用就能跑：

```bash
kubectl -n logging create secret generic fluent-bit-es-auth \
  --from-literal=elastic_password="$(kubectl -n logging get secret quickstart-es-elastic-user -o go-template='{{.data.elastic | base64decode}}')" \
  --dry-run=client -o yaml | kubectl apply -f -
```

生产上更稳妥的是给 Fluent Bit 单独建 **Elasticsearch 专用账号**，只给 **`k8s-*`** 索引写权限，这里仍用 **`elastic`** 是为了和全文示例一口气跑通；落地按 **T9.2.7** 生产建议改用户。

**步骤 2：RBAC + ConfigMap + DaemonSet（生产默认开 TLS 校验）**

**企业生产基线（与官方文档一致，可写进变更/评审）**

下面不是「经验帖」，而是**按厂商文档应做到的最低一致动作**；示例 YAML 已按此对齐，你扩容量时仍以同一套链接里的说明为准。

1. **连 Elasticsearch**：使用 **HTTPS**，并对服务端证书做校验（**`TLS.Verify On`** + **`tls.ca_file`**）。CA 材料与 ECK 下发的 Secret 一致，见 Elastic [K8s HTTPS 设置](https://www.elastic.co/docs/deploy-manage/security/k8s-https-settings)；TLS 通用参数见 Fluent Bit [Transport security](https://docs.fluentbit.io/manual/administration/transport-security)（文档强调生产应启用校验，而非长期关闭）。
2. **Elasticsearch 输出插件**：行为以官方 [Elasticsearch 输出](https://docs.fluentbit.io/manual/pipeline/outputs/elasticsearch) 为准——**`suppress_type_name`**、**`generate_id`**、**`buffer_size` / `Buffer_Size`**、需要时 **`compress`**；采集 **Kubernetes** 日志时建议 **`Replace_Dots On`**（**`replace_dots`**），避免 **`app.kubernetes.io/*`** 等带点 label 在文档里变成嵌套对象，与索引里已把 **`kubernetes.labels.app`** 映射成 **text** 冲突（表现为 **`document_parsing_exception`** / **`Can't get text on a START_OBJECT`**）。排障见同页 **Troubleshooting**。
3. **凭据与 Secret**：密码、API 密钥只进 **Kubernetes Secret**，不写进 ConfigMap；与 Elastic [Accessing services](https://www.elastic.co/guide/en/cloud-on-k8s/current/k8s-services.html#k8s-authentication) 及内置用户管理一致；生产缩小权限见 [Elasticsearch configuration（ECK）](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/elasticsearch-configuration)。
4. **采集与缓冲**：**Tail**、**Kubernetes** 过滤、**`Mem_Buf_Limit`** 等单位与含义见官方 [配置说明](https://docs.fluentbit.io/manual/administration/configuring-fluent-bit) 及各插件页；DaemonSet 的 **CPU/内存 requests、limits** 按集群节点日志量评审，并与下游 ES 写入能力匹配。
5. **下游 Elasticsearch 容量**：单节点示例仅用于联调；生产索引速度、线程池、拒绝率等按 Elastic 当前版本 [Indexing speed / 容量规划](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/important-settings-configuration)（以你实际打开的 **Reference** 版本为准）执行，避免只调大 Fluent Bit、ES 侧仍 429。
6. **可观测与排障**：Fluent Bit 提供 [Monitoring / Metrics](https://docs.fluentbit.io/manual/administration/monitoring) 与 **`Trace_Error`** 等诊断手段（同 Elasticsearch 输出文档）；与 ES 侧慢日志、集群健康一并看，避免只看容器 stdout。

要点说明一下，避免你对着 YAML 懵：

- **读日志**：挂宿主机的 **`/var/log`**；再挂 **`/var/lib/docker/containers`** 是为了部分 Docker 运行时解析软链接，**纯 containerd** 的节点可以删掉这一挂卷（见 YAML 后说明）。
- **写 ES**：访问 **`quickstart-es-http.logging.svc:9200`**，必须 **HTTPS**。**`TLS.Verify On`**，CA 用 ECK 公开的 **`quickstart-es-http-certs-public`** 里的 **`tls.crt`** 挂进容器，路径 **`/etc/es-http-ca/tls.crt`**，和官方 curl 示例用的是同一套材料。
- **对接 ES 9.x**：**`Suppress_Type_Name On`**（去掉 `_type`，见插件文档里 Elastic Cloud 8+ 说明）；**`Generate_ID On`** 减少 bulk 写入报错（官方 Troubleshooting 里对 **`create`** 与 data stream 的说明）。
- **读 ES 返回体**：**`Buffer_Size`**（插件文档里的 **`buffer_size`**）用来放大 Elasticsearch 输出里 HTTP 客户端可读响应的上限。默认过小会出现 **`[http_client] cannot increase buffer: ... max=32000`**，随后 **`failed to flush chunk` / `cannot be retried`**。生产建议显式设为 **`512k`** 或更大（日志量极大时再调），见 [Elasticsearch 输出 · buffer_size](https://docs.fluentbit.io/manual/pipeline/outputs/elasticsearch)。
- **索引名**：**`Logstash_Format On`** + **`Logstash_Prefix k8s`**，落到 ES 里是 **`k8s-YYYY.MM.DD`**，和 **T9.2.9** 里数据视图 **`k8s-*`** 对齐。

```yaml
# fluent-bit-es.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fluent-bit
  namespace: logging
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: fluent-bit-read
rules:
  - apiGroups: [""]
    resources: ["pods", "namespaces"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: fluent-bit-read
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: fluent-bit-read
subjects:
  - kind: ServiceAccount
    name: fluent-bit
    namespace: logging
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-config
  namespace: logging
data:
  fluent-bit.conf: |
    [SERVICE]
        Flush           1
        Daemon          Off
        Log_Level       info
        Parsers_File    parsers.conf

    [INPUT]
        Name              tail
        Tag               kube.*
        Path              /var/log/containers/*.log
        multiline.parser  docker, cri
        Mem_Buf_Limit     50MB
        Skip_Long_Lines   On

    [FILTER]
        Name                kubernetes
        Match               kube.*
        Merge_Log           On

    [OUTPUT]
        Name                es
        Match               kube.*
        Host                quickstart-es-http.logging.svc
        Port                9200
        Logstash_Format     On
        Logstash_Prefix     k8s
        Suppress_Type_Name  On
        Generate_ID         On
        HTTP_User           elastic
        HTTP_Passwd         ${ES_PASSWORD}
        TLS                 On
        TLS.Verify          On
        tls.ca_file         /etc/es-http-ca/tls.crt
        Buffer_Size         512k
        Replace_Dots        On
  parsers.conf: |
    [PARSER]
        Name        docker
        Format      json
        Time_Key    time
        Time_Format %Y-%m-%dT%H:%M:%S.%L%z
    [PARSER]
        Name        cri
        Format      regex
        Regex       ^(?<time>[^ ]+) (?<stream>stdout|stderr) (?<logtag>[^ ]*) (?<message>.*)$
        Time_Key    time
        Time_Format %Y-%m-%dT%H:%M:%S.%L%z
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluent-bit
  namespace: logging
  labels:
    app: fluent-bit
spec:
  selector:
    matchLabels:
      app: fluent-bit
  template:
    metadata:
      labels:
        app: fluent-bit
    spec:
      serviceAccountName: fluent-bit
      tolerations:
        - operator: Exists
      containers:
        - name: fluent-bit
          image: fluent/fluent-bit:5.0.3
          command: ["/fluent-bit/bin/fluent-bit"]
          args: ["-c", "/fluent-bit/etc/fluent-bit.conf"]
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: "1"
              memory: 512Mi
          env:
            - name: ES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: fluent-bit-es-auth
                  key: elastic_password
          volumeMounts:
            - name: config
              mountPath: /fluent-bit/etc
            - name: varlog
              mountPath: /var/log
              readOnly: true
            - name: varlibdockercontainers
              mountPath: /var/lib/docker/containers
              readOnly: true
            - name: es-http-ca
              mountPath: /etc/es-http-ca
              readOnly: true
      volumes:
        - name: config
          configMap:
            name: fluent-bit-config
        - name: varlog
          hostPath:
            path: /var/log
        - name: varlibdockercontainers
          hostPath:
            path: /var/lib/docker/containers
        - name: es-http-ca
          secret:
            secretName: quickstart-es-http-certs-public
            items:
              - key: tls.crt
                path: tls.crt
```

**步骤 3：应用并验收**

```bash
kubectl apply -f fluent-bit-es.yaml
kubectl -n logging rollout status daemonset/fluent-bit --timeout=120s
kubectl -n logging get pods -l app=fluent-bit
```

任选一个 Fluent Bit Pod 看日志，没有连续 **`error`**、`ES` 拒绝连接之类即可。再到 **T9.2.9** 用 **`k8s-*`** 建数据视图、在 **Discover** 里能看到新日志，整条链路才算过。

**纯 containerd、不跑 Docker 的节点**：删掉 DaemonSet 里 **`varlibdockercontainers`** 的 **`volumeMount`** 和 **`volumes`** 那一段即可，**`/var/log`** 必须保留。

> **运维说明**  
> - 若 **`quickstart-es-http-certs-public`** 里没有 **`tls.crt`** 而有 **`ca.crt`**，把上面 Secret **`items`** 里的 **`key`** 改成 **`ca.crt`**，**`tls.ca_file`** 仍指向挂载目录下的文件名（与 **`path`** 一致）。  
> - 集群若启用了 **Pod Security** 等策略，采集类 DaemonSet 可能要单独放命名空间或加 **`securityContext`**，按你们平台规范调，不在此展开。  
> - **绝对不要**在生产长期 **`TLS.Verify Off`**。只有临时联调、且内网已控风险时才可以关校验，见 Fluent Bit 文档里「生产应启用校验」的说明。

**出问题时按这个顺序查**

```mermaid
flowchart TD
  A[Fluent Bit Pod 异常] --> B[kubectl logs 该 Pod]
  B --> C{报 TLS 或 401?}
  C -->|TLS| D[Secret 是否挂对 tls.crt / ca.crt]
  C -->|401| E[fluent-bit-es-auth 密码是否与 ES 一致]
  C -->|连接拒绝| F[kubectl get ep -n logging quickstart-es-http]
  B --> G{仍不明}
  G --> H[kubectl -n logging get es 是否 Ready]
  H --> I[ES Pod 与 Service 是否同命名空间 logging]
```

1、**`[http_client] cannot increase buffer ... max=32000`（可定因）**  

这是 **Fluent Bit 读 Elasticsearch HTTP 响应的缓冲区上限太小**，不是 Pod 被系统 **OOM**（OOM 会看到 **`OOMKilled`**）。日志一多、bulk 响应变大就失败，表现为**先正常后报错**。处理： **`[OUTPUT]`** 里设置 **`Buffer_Size 512k`**（或更大），见上文示例与 [Elasticsearch 输出](https://docs.fluentbit.io/manual/pipeline/outputs/elasticsearch)。仍不够时再加大或配合 **`Compress gzip`**（插件文档 **`compress`**）。

2、**已调 `Buffer_Size`，日志里不再出现 `http_client`，但仍 `failed to flush` / `cannot be retried`（尤其只发生在某台 control-plane 节点）**  

这说明 **根因已不是（或不只是）读响应缓冲**，必须看到 **ES 返回的具体错误**：在 **`[OUTPUT]`** 里临时加 **`Trace_Error On`**（见同插件文档 **Troubleshooting**），滚动 Pod 后抓 **`[output:es:es.0]`** 打出来的请求/响应再定因。同时确认 **`kubectl -n logging describe pod <该 Pod>`** 里用的 ConfigMap **资源版本已更新**（旧 Pod 可能仍挂着旧配置）。

3、**`document_parsing_exception` / `kubernetes.labels.app` / `Can't get text on a START_OBJECT`（可定因）**  

ES 返回 **400**，日志里带 **`failed to parse field [kubernetes.labels.app] of type [text]`**，预览值像 **`{kubernetes={io/component=metrics}}`**。含义是：索引里 **`kubernetes.labels.app`** 已被动态映射成 **字符串**，但部分 Pod 带 **`app.kubernetes.io/component`** 等 label，**metadata 在文档里既可能是纯 `app` 字符串，又可能被展成嵌套对象**，类型打架就写入失败。控制面/监控类 Pod（**node-exporter、prometheus-adapter** 等）常见，所以往往**先在某个 master 上的 Fluent Bit** 爆量失败。  

**处理（与官方插件说明一致）**：在 **`[OUTPUT]`** 中加 **`Replace_Dots On`**（见 **`replace_dots`**），把字段名里的 **`.`** 换成 **`_`**，避免 ES 把 **`app`** 误当成可嵌套路径。上文 YAML 已默认打开。  

---

### T9.2.9、Kibana 实践

前面 **Fluent Bit** 已经把容器日志写进 **Elasticsearch**（本文示例索引前缀 **`k8s-`**，与 **T9.2.8** 里 **`Logstash_Prefix k8s`**、**`Logstash_Format On`** 一致）。**生产里真正干活的界面**，长期就是 **Kibana**：**值班的**靠它**查问题翻日志**；**业务或开发**用大盘**盯发版、看流量和错误趋势**；**平台组**管**索引、备份、谁有权限**；**需要时**再上**日志告警**。第一次接触 EFK 的人容易在菜单里迷路，或 Discover 里看到一堆字段名不敢下手——**先把下面三句话记住**，后面就顺了：

- **数据在 Elasticsearch 的索引里**（本文是**按天滚动的** **`k8s-YYYY.MM.DD`**，逻辑上你用一个 **`k8s-*`** 就全扫到）。  
- **Kibana 不存业务日志**；你选的 **数据视图**只是告诉 Kibana「查哪一批索引、默认用哪个时间字段」——不建对数据视图，**Discover** 和后面 **Lens 里做的图**都对不上（Lens 从哪进见 **T9.2.9.3**，侧栏通常**没有**单独一项叫 Lens）。  
- **KQL 过滤、Dashboard、告警规则**里写的字段名，必须和 **Discover 左侧字段列表**里的一致；**T9.2.8** 里 **`Replace_Dots On`** 会改掉带点字段的路径，**别照抄老文档里带点号的示例**。

> **本文本节校验日期：2026-04-27**（Kibana/Console/Data views 等表述对照 [Kibana 文档](https://www.elastic.co/docs) 与 [Kibana 发行说明](https://www.elastic.co/docs/release-notes/kibana)。**Stack 版本**与 **T9.2.2** 表中的 **9.3.3** 一致，升级时先改 CR 与镜像再回头核对本节步骤。）

**官方文档**：

| 模块 | 覆盖功能 | 什么时候看 | 官方链接 |
|------|----------|------------|----------|
| 业务操作主线 | Discover、Dashboard、可视化（Lens 为默认编辑器）、基础分析 | 第一次接触 Kibana，先把查日志和做大盘跑通 | [Explore and analyze](https://www.elastic.co/docs/explore-analyze) |
| 查询语言（KQL） | 日志过滤条件、字段查询、范围查询 | 日常值班查问题，默认先用 KQL | [KQL](https://www.elastic.co/docs/explore-analyze/query-filter/languages/kql) |
| 平台治理模块 | 用户角色、Space、部署升级、连接治理 | 上生产前做权限、分权、变更与升级评审 | [Deploy and manage](https://www.elastic.co/docs/deploy-manage) |
| 数据治理模块 | 索引、映射、摄入、ILM、快照 | 控成本、保留策略、合规留存、灾备恢复 | [Manage data](https://www.elastic.co/docs/manage-data) |
| 查询进阶（ES\|QL） | 管道式查询、聚合分析、复杂结果处理 | KQL 不够用时再上，先试后再看完整语法 | [Try ES\|QL](https://www.elastic.co/docs/explore-analyze/discover/try-esql) · [ES\|QL reference](https://www.elastic.co/docs/reference/query-languages/esql) |

```mermaid
flowchart TB
  subgraph goal[本文已打通]
    FB[Fluent Bit]
    ES[(Elasticsearch<br/>k8s-YYYY.MM.DD)]
    FB --> ES
  end
  subgraph kib[Kibana 企业生产主路径]
    DV[数据视图 Data view]
    DC[Discover 查障]
    LS[Lens 做图]
    DB[Dashboards 大盘]
    RL[Rules 规则]
    CO[Connectors 通知]
    SM[Stack Management]
    DT[Dev Tools Console]
  end
  ES --> DV
  DV --> DC
  DC --> LS
  LS --> DB
  DC --> RL
  RL --> CO
  SM --> DV
  SM --> SM2[Index / ILM / 快照等]
  DT --> ES
```

**导图**（和界面上的名字对齐，不然后面步骤会对不上号）：

```mermaid
flowchart LR
  A[你关心业务] --> B{先回答三个问题}
  B --> C[日志进哪个索引模式]
  C -->|本文固定答案| D["k8s-*<br>（匹配 k8s- 前缀按天索引）"]
  B --> E[用哪个时间字段做横轴]
  E -->|本文固定答案| F["@timestamp"]
  B --> G[要筛 Pod / 命名空间 / 关键字]
  G --> H[Discover 里用 KQL<br>或 过滤器 点选]
  D --> I[在 Discover 点开一条日志<br>重点看 kubernetes.* Pod/命名空间 和 log 日志正文]
  F --> I
  H --> I
```

**怎么读这一节**：

1. **T9.2.9.0** 先把**左侧那一大串英文菜单**对上号，知道**该点哪、别乱点哪**。  
2. 接着从 **T9.2.9.1** 起动手：**数据视图** → **Discover** → 大盘与告警等；细节都在后面小节。  

#### T9.2.9.0、先认路

Kibana v9.3.3 下常见分组名：**Analytics、Elasticsearch、Observability、Security、Management**；**具体文案、是否折叠、多一条少一条**会随**许可证、功能开关、Space 默认解决方案**变一点，以你环境为准。官方说明导航习惯见 [Find apps and objects](https://www.elastic.co/docs/explore-analyze/find-and-organize/find-apps-and-objects)）。

**企业生产**：先把下面这张表和图刻在脑子里，**比死记英文按钮有用**——你会知道**容器日志进 `k8s-*` 这条线**，主要动 **Analytics + Elasticsearch（索引类）+ Management**；**别在没接数据时跟 Observability/Security 死磕，以为 EFK 没装好**。

**先记住两个省时间的操作（不必每次从根菜单点三级）**

- **顶栏全局搜索框**：直接敲 **`Discover`**、**`Index management`**、**`Data views`** 等，可跳到具体页；`Ctrl`+`/`（Windows/Linux）或 `Cmd`+`/`（macOS）聚焦搜索框。
- **你当前在的哪个 Space**（工作区，细节见 **T9.2.9.7**）：可以把 Space 想成**互不干扰的若干套 Kibana「抽屉」**——**数据视图、大盘、已保存的 Discover 会话**等都按 **Space 分开存**。你**从 Space A 切到 Space B**，**左侧还是 Discover**，但**打开/保存 过的那些对象列表可能变少或变多**，因为那是**另一套抽屉里的东西**，**不是 Elasticsearch 里日志没写进、也不是删库了**，只是**换了个工作区**。  

**侧栏各分区在「本文：Fluent Bit → `k8s-*` 索引」下怎么用（一张总表）**

| 侧栏大分区 | 你列的典型入口 | 是干什么的 | 备注 |
|------------|----------------|------------|------|
| **Analytics** | **Discover** | 按时间看日志、写 **KQL**、展开单条 JSON | 本文**主界面**，细节见 **T9.2.9.2** |
| **Analytics** | **Dashboard** | 把图、表、**Saved search** 拼成一页 | 新建面板时**默认进 Lens 编辑器**（见 **T9.2.9.3**）；数据视图仍用 **`k8s-*`** |
| **Analytics** | **Maps** / **Map** 类 | 地理可视化 | 除非日志里有 **geo 字段**要做地理大屏 |
| **Analytics** | **Visualize library** | 浏览、新建、保存 **Lens** 与旧版可视化到「库」里 | **新建可视化**同样默认 **Lens**；适合单独建图再挂到多个 Dashboard（见 **T9.2.9.3**） |
| **Analytics** | **Machine Learning**（或 **ML** 菜单） | 无监督、异常、预测等 | 有**数据量、角色与许可**要求；和「先查全量日志」不是同一条路 |
| **Elasticsearch** | **Home** / **Getting started** | 总览、上手引导、快捷入口 | 快速进 **Add data** 等；**不替代**你自建 `k8s-*` 数据视图 |
| **Elasticsearch** | **Index Management** | 看索引列表、分片、**ilm** 状态、打开 **Edit settings** 等 | 与 **T9.2.9.6**（索引/ILM/快照等治理）**配套看**；**直接看到 `k8s-*`** 有没有 |
| **Elasticsearch** | **Playground**、**Synonyms**、**Query rules** 等 | **搜索体验**（同义词、查询规则、**检索实验**等） | 偏 **站内外搜索/语义检索**；**不是**查容器 stdout 的必经之路 |
| **Elasticsearch** | **Agents**（在 **Elasticsearch** 分组下若出现） | 多与 **ES 本侧/推理/检索**相关能力挂钩 | 采集面纳管在 **Management → Integrations / Fleet**；**本文**采日志是 **Fluent Bit**，见 **T9.2.8** |
| **Observability** | **Overview、Logs、Infrastructure、APM…**、**Alerts**、**SLOs**、**Cases** 等 | Elastic **可观测**解决方案：**指标、链路等需 Agent/预置数据流**时好用 | 你**只**按本文上 **EFK** 时，这里**可能空/不对准 `k8s-*`** 很正常；**查容器日志**仍以 **Discover** 为主 |
| **Security** | **Dashboard、Rules、Alerts、Attack Discovery** 等 | **安全/SIEM** 工作面 | 需要 **Security 数据、规则包** 等，先读 **T9.2.9.8** 防期望错位 |
| **Management** | **Stack Management** | 用户/角色、**Data views**、**Spaces**、部分 **Rules** 入口、**Connectors** 等 | **T9.2.9.1** 建数据视图、**T9.2.9.7** 做权限/空间 都常落在这；菜单名**有时直接叫 Management** |
| **Management** | **Dev Tools** | **Console** 调 ES、**Kibana `kbn:`** API 等 | 见 **T9.2.9.5** |
| **Management** | **Integrations**、**Fleet**、**Stack Monitoring**、**Cloud Connect**、**Streams** 等 | **接 Elastic Agent/Beat**、**监控本栈**、**云连接**、**可观测/日志流** 类产品能力 | 本文不装 Agent 时：**Fleet/Integrations 不是完成 EFK 的前提**；**要盯 ES 健康**再上 **Stack Monitoring**，也见 **T9.2.9.8** |

```mermaid
flowchart TB
  subgraph EFK[本文 EFK 容器日志 主线]
    A1[侧栏进 Analytics - Discover]
    A2[同一数据视图 k8s-*]
    A3[必要时 Dashboard 或 Visualize library<br/>打开 Lens 编辑器]
    A4[Management - Dev Tools 排障]
    A5[Elasticsearch 分组 - Index Management]
    A6[Stack Management 数据视图 / 用户与角色]
  end
  subgraph skip[没接对数据时 易误以为坏了]
    O1[Observability 若干页]
    S1[Security 若干页]
  end
  A1 --> A2 --> A3
  A2 --> A4
  A5 --> A2
  A6 --> A2
  O1 -.->|别把这区当查本文 k8s 日志的主路 仍用 Discover| A1
  S1 -.->|安全线不是本文 EFK 主路 查容器日志仍用 Discover| A1
```

```mermaid
flowchart LR
  Q[我今天要干什么]
  Q -->|翻 Pod 日志 关键字| D[Analytics - Discover]
  Q -->|看大盘 出报表| P[Dashboard / Visualize library<br/>均可进 Lens]
  Q -->|索引是不是炸了 要删| I[Elasticsearch - Index Management]
  Q -->|curl 式查 ES/试 DSL| T[Management - Dev Tools]
  Q -->|建只读同事 分 Space| M[Stack Management 用户/角色/Spaces]
  Q -->|日志超阈值 通知| R[见 T9.2.9.4 规则 与 连接器]
  Q -->|上 SIEM/端点/复杂 Observe| X[先读完 T9.2.9.8 再动 Security/Obs 全家桶]
```

> 若**看不到**上表中的某一条：多半是 **License** 或**管理员关掉了功能**；**不影响**你按 **Discover + `k8s-*` + Index Management** 把本文 EFK 跑通。有分歧以 **v9.3.3 官方** [Kibana 文档](https://www.elastic.co/docs) 与**你们集群管理员**为准。

![logging-kibana-modules](./images/logging-kibana-modules.png)

#### T9.2.9.1、建数据视图

1. 用 **T9.2.7** 拿到的密码登录，用户名写 **`elastic`**，或你们已经建好的**业务只读/平台账号**。**不要**让所有人都用超级账号；落地方式见 **T9.2.9.7**。  
2. 进 **Stack Management**（v9.x 里也可能显示为 **Management**，以你界面为准）→ **Data views**（以前叫 *index pattern* 的那套，**概念是同一个，叫法换了**），点 **Create data view**。建一条：  
   - **Name**：自定，如 **`Kubernetes 容器日志`**。  
   - **Index pattern**（界面上**仍用英文这个名字**）：填 **`k8s-*`**，和 **T9.2.8** 的 **`Logstash_Prefix k8s`** 对牢，否则 Discover 会查不到。  
   - **Timestamp field**：选 **`@timestamp`**。不选的话，**全局时间轴、Dashboard 时间选择器**很多功能会缺一条腿。  
3. 若下拉里暂时看不到 **`k8s-*`**，先到 **Stack Management → Index Management** 看是否已有 **`k8s-2026.04.27`** 这种按日命名索引；**没有**就回去查 **T9.2.8** Fluent Bit 与 ES 是否 Ready。**有索引但列表不刷新**时，等几十秒或重进页面；仍不行再对一下当前账号是否有 **`view_index_metadata`** 等读索引元数据的权限。  
4. 只想临时试一把、不打算保存成团队可见的对象时，可以在 **Discover** 或 **Visualize library（Lens）** 里创建数据视图时选 **Use without saving**（**临时数据视图**），关页面或切应用会丢，适合个人排障。正式环境仍建议**保存**并交给权限管理。  

官方：[Data views](https://www.elastic.co/docs/explore-analyze/find-and-organize/data-views) · [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started)

> **和 ES\|QL 的关系**：在 **ES\|QL** 模式下，不少分析可以**不经过**你刚建的数据视图（官方说明见 [Data views 概述](https://www.elastic.co/docs/explore-analyze/find-and-organize/data-views)）。**本文主线**是「数据视图 + Discover 常规 KQL」——和大多数值班同事的习惯一致，先把这条路走顺再玩 ES\|QL。

![logging-kibana-data-view](./images/logging-kibana-data-view.png)

#### T9.2.9.2、Discover

本节与 **T9.2.2** 的 **Kibana/Elasticsearch 9.3.3**（**Elastic Stack** 当前**推荐**稳定小版本，可对照 [Kibana 发行说明](https://www.elastic.co/docs/release-notes/kibana)）、**T9.2.9.1** 的 **数据视图 `k8s-*`** 及**时间字段 `@timestamp`** 保持一致。产品行为以 [Discover](https://www.elastic.co/docs/explore-analyze/discover) 与 [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started) 等官方页面为准，**本小节在 2026-04-27 前**已通读并引用上述链路的当前稳定版内容；KQL 语法以 [KQL](https://www.elastic.co/docs/explore-analyze/query-filter/languages/kql) 为权威。侧栏分组与菜单**英文/中文**以实际界面为准，与 **T9.2.9.0** 一致。

**1. 作用**

**Discover** 是 Kibana 中面向 Elasticsearch 数据的**主查询与浏览界面**：按时间范围检索文档，用 KQL 或 Lucene 过滤，查看字段与单条结构，并可将当前视图保存为会话供复用或接入大盘。相对 `kubectl logs`，在集群级日志场景下多三类能力：**全文与字段检索**、**跨节点与多副本统一视图**、**保存/共享同一条检索条件**（仍须配合集群级采集与存储，见 **T9.1**）。

**2. 进入面板**

- **菜单**：**Analytics → Discover**（**T9.2.9.0**）。首次进入若要求选择数据视图，须选用 **T9.2.9.1** 中已建好的 **`k8s-*`**。  
- **无数据**：在 **Index Management** 中看不到**当日或近期**的 **`k8s-` + 日期** 形式的索引时，应回到 **T9.2.8** 与 **ES** 连通性排查，勿仅在 Kibana 中反复切换数据视图。

**3. 排障思路：先省查询，再定界，再读正文**

界面上的点击顺序见官方 [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started)。下面这张图回答的是**为什么要这么干**：**时间窗**先把 Elasticsearch 扫描范围压下去（算力与费用）；**KQL** 按 **Kubernetes** 常见层次把候选日志缩到「某个命名空间 / Pod / 容器」；**log 与展开文档**才是读堆栈、核对镜像与节点的地方；只有需要**交接或进大盘**时才值得**保存会话**（见 **T9.2.9.3**）。这与「从左到右点一遍菜单」不是一回事。

```mermaid
flowchart TB
  START([数据视图已是 k8s-*<br/>见 T9.2.9.1])
  START --> W[时间窗<br/>对齐告警或发布时段]
  W --> NS[KQL 定界<br/>namespace 到 pod 到 container]
  NS --> LG[读 log 正文<br/>stderr 关键字 堆栈]
  LG --> JD[展开 JSON<br/>镜像 host label 等]
  JD --> Q{同一套条件要<br/>给别人或进大盘?}
  Q -->|是| SV[保存 Discover 会话]
  SV --> NX[T9.2.9.3 Dashboard 等]
  Q -->|否 继续排查| W
```

**4. 界面分区与职责**

Kibana **9.3.x** 的 Discover 为**通用数据探索**；未处于 Observability、Security 等**专项方案上下文**时，即为默认布局（与 [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started) 一致）。下面按「你第一眼会点的控件」拆开写；**英文标签**以你界面为准（有的环境带 `#` 数字角标，那是字段数量提示，不是注释）。

```mermaid
flowchart TB
  subgraph top[顶区]
    T1[时间范围 相对时间 自动刷新]
    T2[数据视图 选择器]
    T3[查询语言 KQL 默认 可改 Lucene]
  end
  subgraph main[主区]
    M1[时间直方图 可点选区间缩小查询窗]
    M2[Documents Patterns Field statistics 等标签]
    M3[结果表 列与展开]
  end
  subgraph flanks[侧栏 字段列表]
    F1[Popular Available Empty Meta 等分组]
    F2[单字段 Top 值 加列 过滤器]
  end
  top --> main
  flanks --> M3
  M3 --> F2
```

**4.1 侧栏：Popular fields、Available fields、Empty fields、Meta fields**

这几块都是「字段列表」里的分区，方便你从几百个字段里**先找到该点的名**，再展开看 Top 值、加进表、加过滤器（侧栏搜索与「推荐字段」行为见 [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started) 中 **Explore the fields in your data**）。给运行时字段等设 **field popularity** 时，还会影响排序，见同页 **Add a field to your data view**。

| 界面上的名字（大意） | 是什么 | 生产上怎么用（本文 `k8s-*` 日志） |
|----------------------|--------|-----------------------------------|
| **Popular fields**（常带数量 `#`） | 根据**使用习惯**往上排的字段：默认按「被放进表格的次数」从多到少（新数据视图会随团队使用逐渐稳定） | 常见会先出现 `log`、`kubernetes.*`；没有也不代表字段不存在，到 **Available** 里搜 |
| **Available fields**（`#`） | 在当前**时间范围 + KQL** 命中结果下，**推断有值**、可供筛选和展示的字段（底层结合 **Field capabilities** 与查询，极少数边界情况与「直觉」不完全一致属已知限制） | **以这里显示的字段名为准**写 KQL、做过滤器；点字段可看 Top 值，点 **+** 加列 |
| **Empty fields**（`#`） | 映射或数据视图里**列得出**、但在**当前查询与时间窗**下**看不到值**的字段 | 可能是这段时间确实没打到（例如某 label 只有个别 Pod 有）；也可能是窗太窄，先放大时间再判断「真没有」 |
| **Meta fields**（`#`） | 文档元数据字段，如 **`_id`**、**`_index`**、**`_score`**、**`_source`** 等，含义以 Elasticsearch 为准 | 看日志落在哪一天索引用 **`_index`**（对应 **`k8s-YYYY.MM.DD`**）；看整段原始 JSON 用 **`_source`**；一般不在这里改映射。详见 [Document metadata fields](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/document-metadata-fields) |

**Available 里字段太多时，先盯这几类（本文 `k8s-*` 容器日志）**

不必一次性看懂几百个名字：用侧栏**搜索框**敲 **`kube`、`log`、`stream`** 就能把最常用的捞出来。**下面这些是值班最常点的**，和 **T9.2.8** Fluent Bit 管线一致；名字若略有出入，以你环境里 **Available** 为准。**第 6 节**有更完整的对照和 KQL 示例。

| 优先看的字段（典型名） | 意义 |
|------------------------|--------------|
| `@timestamp` | 这条日志的时间，对齐顶栏时间轴、对齐故障时段 |
| `log`（或解析出来的业务正文字段） | **一行日志正文**：关键字、堆栈、错误码都在这里搜 |
| `stream` | **`stdout` 还是 `stderr`**，只想看报错时常筛 `stderr` |
| `kubernetes.namespace_name` | **哪个命名空间**（环境、租户、团队边界） |
| `kubernetes.pod_name` | **哪个 Pod**，定界到副本 |
| `kubernetes.container_name` | 同一 Pod **哪个容器**打的（业务容器、sidecar 分开看） |
| `kubernetes.host` | **落在集群里哪台节点**，怀疑单机网络或磁盘时有用 |
| `kubernetes.container_image` | **镜像名与标签**，发版对比常用 |

其他大批字段多半是：**业务 JSON 解出来的键**、**集成附带字段**、或 **`kubernetes.labels` 经 `Replace_Dots` 后的长名字**；用到再搜即可，别强迫自己先背全库。

**4.2 时间直方图：Auto interval、Break down by**

- **Auto interval（自动间隔）**：时间轴上的桶宽不是手写「固定 1 分钟」，而是由界面按当前时间跨度**自动选间隔**，底层对应一类「目标桶数」思路，与 Elasticsearch **auto_date_histogram** 聚合一致（默认常见为**目标约 10 个桶**，实际桶数不超过目标），见 [Auto-interval date histogram aggregation](https://www.elastic.co/docs/reference/aggregations/search-aggregations-bucket-autodatehistogram-aggregation)。**你要记住的**：先看**形状和峰值时段**，再点某段把时间窗缩进去，比死记间隔更有意义。
- **Break down by「按某某拆分」**（若你的主题显示该选项）：在**同一条时间轴**上，再按某个**维度字段**把每条柱子拆开或分色（多为 **keyword** 或可聚合的类型）。**典型用法**：按 `kubernetes.namespace_name`（或映射里的 **`.keyword`**）对比各命名空间条数；选项灰掉或报错多半是**字段类型不适合拆分**，换字段即可。

**4.3 主区中部标签：Documents、Patterns、Field statistics**

主区**时间图下方**通常有标签页（名称以界面为准），和官方 [Run a pattern analysis](https://www.elastic.co/docs/explore-analyze/discover/run-pattern-analysis-discover)、[View field statistics](https://www.elastic.co/docs/explore-analyze/discover/show-field-statistics) 对应。

| 标签 | 是什么 | 怎么用 |
|------|--------|--------|
| **Documents** | 文档表：逐条看日志，展开 JSON | 值班主战场；与 **`@timestamp`、Summary** 等列配合见下 **4.4** |
| **Patterns** | 对**文本字段**做**归类**，把相似日志句子收成几条模式，顺带可看占比 | 日志轰炸时先看**哪几种句式**，再回 **Documents** 展开个别样本；可选分析字段、对模式加过滤器。详见官方 [Run a pattern analysis…](https://www.elastic.co/docs/explore-analyze/discover/run-pattern-analysis-discover) |
| **Field statistics** | 当前命中集合上，按字段做**分布、基数、数值范围**等汇总 | 做大盘前先确认字段有没有脏值、偏态；**仅在默认 Discover 模式**可用，**ES\|QL 模式**下不可用，见 [View field statistics](https://www.elastic.co/docs/explore-analyze/discover/show-field-statistics) |

```mermaid
flowchart LR
  subgraph tabs[时间图下方]
    D[Documents]
    P[Patterns]
    S[Field statistics]
  end
  D --> A[逐条 展开 log kubernetes]
  P --> B[先看类别 再降噪]
  S --> C[先看分布 再选图表字段]
  B --> D
  C --> D
```

**4.4 表格列：`@timestamp`、`Summary`、命中条数**

- **`@timestamp` 列**：数据视图在 **T9.2.9.1** 里绑定的**时间字段**（本文为 **`@timestamp`**），与顶栏 **Time filter** 一致；点列头一般可排序（具体以界面为准）。
- **Summary 列**：每条日志一行**摘要**；当你从侧栏把字段**加进表格**后，Summary 的处理方式会按版本变化，常见是**让位给具体列**（官方 [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started)：**When you add fields to the table, the Summary column is replaced**）。
- **Documents / 命中数**：表示在当前过滤条件下**命中的规模**；表格里**实际展示的行数**还受 **`discover:sampleSize`**、分页等限制（见 [Customize the Discover view](https://www.elastic.co/docs/explore-analyze/discover/document-explorer)），**不等于**「索引里总共只有这么几条」。

**4.5 与上文衔接**

- **时间过滤**全局行为见 [Filtering in Kibana](https://www.elastic.co/docs/explore-analyze/query-filter/filtering)。  
- **列宽、行高、全屏、折叠字段列表**等布局见 [Document explorer](https://www.elastic.co/docs/explore-analyze/discover/document-explorer)。  
- 生产上仍是：**先缩时间，再加 KQL**，减轻 Elasticsearch 压力（与 **3** 一致）。

**5. 查询与保存（企业常用项）**

| 能力 | 生产上建议用法 | 官方 |
|------|----------------|------|
| 时间窗与自动刷新 | **时间范围**要包住整起故障或整段发版窗口；没有需要时，别把查询时间拉得过大；自动刷新也别设太勤，避免和正常业务一起把 Elasticsearch 打满 | [Discover](https://www.elastic.co/docs/explore-analyze/discover)、[Filtering in Kibana](https://www.elastic.co/docs/explore-analyze/query-filter/filtering) |
| KQL 与 Lucene | **默认 KQL** 做字段与布尔组合；**Lucene** 在搜索栏侧切换。KQL 仅**过滤**文档，不负责聚合与排序，勿与 [Query DSL](https://www.elastic.co/docs/reference/query-languages/querydsl)（JSON，走 `_search` 等 API）在心智上混用；概念导读见 [Query DSL（Explore）](https://www.elastic.co/docs/explore-analyze/query-filter/languages/querydsl) | [KQL](https://www.elastic.co/docs/explore-analyze/query-filter/languages/kql)、[Lucene](https://www.elastic.co/docs/explore-analyze/query-filter/languages/lucene-query-syntax) |
| **ES\|QL** | 不依赖数据视图的**另一种**查数方式，适合表格化、与 **ES\|QL** 图表化路径配合；**本文 EFK 主线**仍以「**数据视图 + KQL**」与值班习惯对齐，与 **T9.2.9.1** 脚注一致 | [Try ES\|QL](https://www.elastic.co/docs/explore-analyze/discover/try-esql) |
| 过滤器与搜索栏 | 侧栏对字段**加/减**生成过滤器，与搜索栏 KQL 组合使用 | [Get started 中 Search and filter 路径](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started) |
| 保存与复用 | 顶栏保存当前 **Discover 会话**（**9.3.3** 等界面中常见 **Session** 等文案，以实际为准），可再加入 **Dashboard**；与保存对象、复用方式的关系见 [Save a search for reuse](https://www.elastic.co/docs/explore-analyze/discover/save-open-search) | 同上及 [Get started with Discover](https://www.elastic.co/docs/explore-analyze/discover/discover-get-started) |
| 字段与模式 | **Field statistics**、**Patterns** 与侧栏 Top 值配合用：先看分布与模式，再回 **Documents** 逐条看。细节见 **4.3** | [Run pattern analysis…](https://www.elastic.co/docs/explore-analyze/discover/run-pattern-analysis-discover) · [View field statistics](https://www.elastic.co/docs/explore-analyze/discover/show-field-statistics) |
| 长时查询 | 大数据量时可用后台查询类能力，避免浏览器长时间无响应（是否开启受部署与权限约束） | [Run queries in the background](https://www.elastic.co/docs/explore-analyze/discover/background-search) |

上述「时间 + 搜索栏 + 过滤器」在 Kibana 多数应用中与查询结果取交集；具体组合方式见 [Filtering in Kibana](https://www.elastic.co/docs/explore-analyze/query-filter/filtering)。

```mermaid
flowchart TB
  TF[顶栏 时间选择器] --> I[与搜索与过滤器 求交集]
  SB[搜索栏 KQL 或 Lucene] --> I
  PF[侧栏 点字段 生成的 结构化过滤器] --> I
  I --> OUT[表与直方图 仅展示 命中 文档子集]
```

**6. 与本文 Fluent Bit 字段的对应关系**

下表来自 **T9.2.8** 中 **Kubernetes 过滤器**与 **Merge_Log** 等常见配置。**若** Parser 或 **Merge** 与本文不一致，**以当前索引 mapping 与 Discover 左侧「可用字段」为准**。开启 **`Replace_Dots On`** 时，**带点的 label 路径在文档中会以替换后的字段名**出现，**不要**沿用带点号的旧文章示例。

| 字段 / 前缀 | 含义 | 生产上用法示例 |
|-------------|------|----------------|
| `@timestamp` | 文档时间，驱动时间轴 | 与顶栏时间选择器一致，排障**优先**对齐本字段 |
| `log` 或解出的正文行 | 容器**一行**原始输出；若应用为 JSON 行且被合并，可多出**业务子字段** | 关键字、堆栈、错误码，例如：`log: *NullPointerException*` |
| `stream` | **stdout** 或 **stderr** | 仅看标准错误，例如：`stream: "stderr"` |
| `kubernetes.pod_name` | Pod 名 | 定界副本，例如：`kubernetes.pod_name: "myapp-7d4f*"` |
| `kubernetes.namespace_name` | 命名空间 | 分环境/租户，例如：`kubernetes.namespace_name: "prod"` |
| `kubernetes.container_name` | 容器名 | 同 Pod 多容器时与 `pod_name` 联用 |
| `kubernetes.container_image`、`kubernetes.host` | 镜像、**所在节点** | 发版对比、怀疑单机问题时筛选 |
| `kubernetes.labels` 经 Replace_Dots 后可能为 **`kubernetes_labels_...`** 等 | Workload 标签的落库形态因映射而异 | 与 **Deployment/Service** 对 label 时**以 Discover 展示名为准**；**T9.2.9.3、T9.2.9.4** 中引用字段须与此一致 |

**7. 示例 KQL（请按实际命名空间与 Pod 名替换）**

```text
kubernetes.namespace_name: "default" and kubernetes.pod_name: "counter*" and log: *error*
```

**8. 与全局验收的对应关系**

下列与 **T9.2.9.9** 的清单一一对应，在 Discover 中即可逐项自证，不必另写脚本：数据视图为 **`k8s-*`** 且能命中**新产生**的日志、字段名与 **T9.2.8**（含 **`Replace_Dots`**）一致。另请记住：**在 Discover 中缩小显示范围不会减少已写入索引的数据量**；在采集侧**减量**、用 **ILM** 等控容量，见 **T9.2.9.9** 与 **T9.2.8**。

#### T9.2.9.3、可视化（Lens）与 Dashboard

**先说清楚：Analytics 里常常看不到一项就叫「Lens」**  

和官方一致（**Kibana 9.3.3**，见 **T9.2.2**）：左侧常见的是 **Discover、Dashboard、Maps、Machine Learning、Visualize library** 等。**Lens** 是 **Kibana 自带的拖拽式可视化编辑器名称**，多数场景下从 **Dashboard** 或 **Visualize library** 进去新建「可视化」时就会打开它，而不是单独占一个一级菜单。[Create a dashboard](https://www.elastic.co/docs/explore-analyze/dashboards/create-dashboard) 写明：在大盘里 **Add new visualizations** 时，**Lens 是默认编辑器**；从 **Visualize library** 新建也同样落在 Lens，详见 [Lens](https://www.elastic.co/docs/explore-analyze/visualize/lens)、[Visualize Library](https://www.elastic.co/docs/explore-analyze/visualize/visualize-library)。菜单找不到时，用顶栏 **全局搜索** 搜 **`Lens`**、**`Dashboard`**、**`Visualize`**（[Find apps and objects](https://www.elastic.co/docs/explore-analyze/find-and-organize/find-apps-and-objects)）。

**两条正式路径（任选其一，都会进 Lens）**

1. **Analytics → Dashboard**：**Create dashboard**（或打开已有大盘点 **Edit**）→ **Add new visualization** / **Add panel**（文案以你界面为准）→ 进入 **Lens**。从大盘里进的，做完常用 **Save and return** 留在当前大盘；也可 **Save to library** 存进可视化库以后多处复用（见 Lens 文档「Create visualizations」里两种保存方式的说明）。  
2. **Analytics → Visualize library**：**Create visualization**（或等价入口）→ 进入 **Lens**，保存时可写入库、再挂到 Dashboard。

**在 Lens 里务必对齐的三件事**  

- **Data view（数据视图）**：选 **`k8s-*`**，与 **T9.2.9.1**、Discover 一致，否则和值班用的是两套索引范围。  
- **时间**：顶栏时间范围要盖住你要看的故障或发版窗口；空数据先怀疑时间窗再怀疑聚合。  
- **字段**：拆分维度、分桶尽量用 **keyword** 或可聚合类型；**text** 类字段若 mapping 带了 **`.keyword`**，图形里通常要选 **`.keyword`**。拿不准时在 Discover 看 **Field statistics** 或 ES mapping，避免「图上始终没数」。

**Dashboard 用来干什么**  

一页里叠多个面板（Lens 图、表格、Saved search 等）+ **顶栏时间选择器** + 可选 **Controls**（例如按命名空间下拉的选项列表），适合 **NOC 大屏、发版窗口、周报复盘**。**Edit** 编辑布局；**Add from library** 从库里拽已有 Lens 与已保存检索。**Share**、嵌入、导出前的权限与「谁能改」要在团队里说清楚；外链不要随手发到公网（见 [Share dashboards](https://www.elastic.co/docs/explore-analyze/dashboards/sharing)、[Dashboards 总览](https://www.elastic.co/docs/explore-analyze/dashboards)）。

**和 Discover 怎么接上**  

Discover 里 **Save** 的会话可作为 **Saved search** 再进 Dashboard（见 [Save a search for reuse](https://www.elastic.co/docs/explore-analyze/discover/save-open-search)）。Lens 里在 **`k8s-*`** 上做好的图 **Save** 后，再在 Dashboard 里 **Add from library** 或编辑大盘时插入即可。

**生产上少踩坑**  

- 图上不出数：多半是 **数据视图不是 `k8s-*`**、**时间窗不对**，或 **聚合字段类型不对**（该用 **keyword / .keyword** 却用了 **text**）。  
- **大盘链接**给别的部门前：想清楚 **时间范围里有没有敏感内容**、**对方账号是否有权读这批索引**、**分享的是只读还是可编辑**。

```mermaid
flowchart TB
  subgraph entry[Lens 两条入口]
    E1["Analytics Dashboard Create 或 Edit 后 Add visualization"]
    E2["Analytics Visualize library Create visualization"]
  end
  E1 --> LensEd["Lens 数据视图同 k8s 通配"]
  E2 --> LensEd
  Disc["Discover 先摸清字段类型与 KQL"] --> LensEd
  LensEd --> SV["保存 Save and return 或 Save to library"]
  SV --> DB["Dashboard 多面板 Controls 时间"]
  Disc --> DB
  DB --> SH["分享与投屏 注意权限与敏感信息"]
```

官方：[Create a dashboard](https://www.elastic.co/docs/explore-analyze/dashboards/create-dashboard) · [Lens](https://www.elastic.co/docs/explore-analyze/visualize/lens) · [Visualize Library](https://www.elastic.co/docs/explore-analyze/visualize/visualize-library) · [Dashboards](https://www.elastic.co/docs/explore-analyze/dashboards) · [Add controls](https://www.elastic.co/docs/explore-analyze/dashboards/add-controls) · [Share dashboards](https://www.elastic.co/docs/explore-analyze/dashboards/sharing)

#### T9.2.9.4、Rules 与 Connectors（日志告警）

本节前提与上文锁死，别自改一套：**Elasticsearch / Kibana 版本**见 **T9.2.2**（示例 **9.3.3**）；**数据视图**必须是 **T9.2.9.1** 建的 **`k8s-*`**，时间字段 **`@timestamp`**；**KQL 里写的字段名**与 **T9.2.8**、**T9.2.9.2 第 6 节**一致（含 **`Replace_Dots On`** 后的名字）。你还没跑通 **Fluent Bit → ES → Discover** 时，先别做告警，否则全是空跑或误报。

**1. 先分清三件事（避免排障时走错门）**

- **本文要用的规则类型**：**Elasticsearch query**（在创建规则时选这个类型）。它支持用 **[KQL](https://www.elastic.co/docs/explore-analyze/query-filter/languages/kql)**、**Lucene**、**Query DSL** 或 **ES|QL** 定义条件；**EFK 值班习惯**下优先 **KQL + 数据视图 `k8s-*`**，和 **Discover** 同一套话。细则见官方 [Elasticsearch query rule](https://www.elastic.co/docs/explore-analyze/alerting/alerts/rule-type-es-query)。
- **菜单从哪进（与 9.3.x 官方一致）**：**Stack Management → Alerts and insights → Rules** 管规则列表；**Connectors** 在 **Stack Management** 里单独一页（各 Space 一份，和 **T9.2.9.7** 说的 Space 隔离一致）。找不到就用顶栏 **全局搜索** 搜 **`Rules`**、**`Connectors`**（[Find apps and objects](https://www.elastic.co/docs/explore-analyze/find-and-organize/find-apps-and-objects)）。
- **不要和这些混一谈**：**Observability / Security / APM** 里也有各自的 **Rules / Alerts**，数据源、许可和权限模型不同；**SIEM 检测规则**不是本节路径。**本文只写「容器日志进 `k8s-*` → 用 Stack 侧 Elasticsearch query 告警」**。

**2. 权限与前置（生产一踩一个坑）**

- 能建规则、配连接器，需要 Kibana 功能权限里 **Management → Stack Rules**、**Actions and Connectors** 等到位；细则见 [Alerting set up](https://www.elastic.co/docs/explore-analyze/alerting/alerts/alerting-setup) 的 **Security** 小节。日常别用 **`elastic` 做规则 owner**（见 **T9.2.9.7**）；规则后台跑查询用的是创建时的 **API key** 权限快照，后人改角色可能导致规则悄悄失效，官方在 **Set up** 里写明了这一点。
- 自建栈若要用**邮件**等连接器，通常还要配 **`kibana.yml`**（例如 **`xpack.encryptedSavedObjects.encryptionKey`**、邮件域白名单等），见同一页 [Alerting set up](https://www.elastic.co/docs/explore-analyze/alerting/alerts/alerting-setup)。ECK 托管 Kibana 时把这些写进 **Kibana CR 的 `spec.config`**，别只会在 UI 里点。

**3. 推荐操作顺序（先连接器，后规则）**

1. **Connectors**：**Stack Management → Connectors → Create connector**。选类型（常用：**Webhook**、**Slack**、**Email**、**Microsoft Teams** 等，完整列表见 [Available connectors](https://www.elastic.co/docs/reference/kibana/connectors-kibana)）。填 URL/凭据后**先 Test**，再保存。连接器说明总入口：[Connectors](https://www.elastic.co/docs/deploy-manage/manage-connectors)。
2. **Rules**：**Stack Management → Alerts and insights → Rules → Create rule** → 选 **Elasticsearch query**。
3. **查询方式**：选 **KQL**（或你团队统一的语言）；**Data view** 下拉选 **`k8s-*`**（与 **T9.2.9.1** 名称一致，不是手敲索引别名）。
4. **KQL**：把你在 **Discover** 里已经验证过的条件贴进来。示例（命名空间、Pod 名请换成你的；字段以 Discover 为准）：

```text
kubernetes.namespace_name: "default" and stream: "stderr" and log: *error*
```

5. **度量与阈值**：先做 **`count`**（文档条数）最直观；阈值用 **`is above`** 等，与官方 [Elasticsearch query rule](https://www.elastic.co/docs/explore-analyze/alerting/alerts/rule-type-es-query) 一致。
6. **时间窗口（Time window）**：例如「过去 5 分钟」——窗口越大，每次规则触发的查询越重，**别一上来就 24h、72h**。
7. **检查间隔（Check interval）**：官方建议一般**小于**时间窗口，避免漏检；同时 **间隔越短，ES/Kibana 负担越大**。默认栈里常有**最短间隔**等护栏，见 [Kibana alerting: performance and scaling](https://www.elastic.co/docs/deploy-manage/production-guidance/kibana-alerting-production-considerations)。
8. **Test query**：用界面里的 **Test query** 确认命中数合理，再保存规则（同上规则类型文档）。
9. **去重**：**Exclude matches from previous run** 默认常开，避免同一条日志反复告警；若你用分组（group by）高基数字段，行为以官方说明为准。
10. **Actions**：选刚建的 **Connector**，动作频率用 **alert summary** 或 **On status changes** 等压噪音，避免每分钟一封邮件（官方在 [Create and manage rules](https://www.elastic.co/docs/explore-analyze/alerting/alerts/create-manage-rules) 的 **Actions** 里有示例说明）。
11. **Rule scope（9.3+）**：若界面让你选规则可见范围，本文场景选 **Stack Management / Stack rules** 一类即可，避免误选成仅 Observability 应用上下文导致别的同事在 **Stack Management** 里看不到（见 [Elasticsearch query rule](https://www.elastic.co/docs/explore-analyze/alerting/alerts/rule-type-es-query) 的 **Define the conditions** 里对 **scope** 的说明）。

**4. 和 Discover 的闭环**

告警里的 **KQL、时间窗**应能在 **Discover**（**T9.2.9.2**）用同一 **数据视图 `k8s-*`** 复现；值班收到通知后，用通知里的 **link** 或自己打开 **Discover** 同条件排查。告警**不会**减少索引体积；要降噪、省盘，仍在 **Fluent Bit / ILM** 上做（**T9.2.9.9、T9.2.9.6**）。

```mermaid
flowchart TB
  subgraph pre[前置 与上文一致]
    DV[数据视图 k8s-*]
    FB[Fluent Bit 字段名]
    DV --- FB
  end
  subgraph conn[第一步 Connectors]
    C1[Stack Management Connectors]
    C2[Create 选类型 Webhook 邮件等]
    C3[Test 通过后保存]
    C1 --> C2 --> C3
  end
  subgraph rule[第二步 Elasticsearch query 规则]
    R1[Stack Management Alerts and insights Rules]
    R2[Create rule 选 Elasticsearch query]
    R3[KQL + Data view k8s-*]
    R4[Test query 调时间窗与阈值]
    R5[挂 Action 选 Connector]
    R1 --> R2 --> R3 --> R4 --> R5
  end
  subgraph prod[生产注意]
    P1[检查间隔 别压到 ES 扛不住]
    P2[权限 用业务角色 不用 elastic 长期拥有规则]
    P3[Space 规则与连接器只在当前 Space]
  end
  pre --> conn
  conn --> rule
  rule --> prod
  prod --> DISC[Discover 同条件复盘]
```

**5. 验收（你做完应对得上号）**

- 故意打一条满足 KQL 的测试日志（或临时放宽阈值），在 **Rules** 详情里能看到规则执行成功，**Connector** 能收到一次通知（测完改回阈值或禁用规则，别留「永远 firing」在生产里）。
- **Last response** 无报错；若 **Errored actions**，到规则 **History** 里看原因（常见：邮件域不在白名单、Webhook 超时、连接器删了但规则还引用）。

#### T9.2.9.5、Dev Tools（Console）与查询工具

本节和上文绑在一起用，别跳步：**Stack 版本**见 **T9.2.2**（示例 **9.3.3**）；**日志索引**是 **T9.2.8** 写出来的 **`k8s-YYYY.MM.DD`**，在 Kibana 里用数据视图 **`k8s-*`**（**T9.2.9.1**）；登录账号与 TLS/CA 仍按 **T9.2.7**、**T9.2.5** 来。你还没在 **Discover** 里看到日志时，先在 **T9.2.8** 排连通，别在 Console 里空转。

**1. Console 是什么、权限从哪来**

**Console** 是 Kibana 里的交互界面，用来对 **[Elasticsearch REST API](https://www.elastic.co/docs/reference/elasticsearch/rest-apis)** 和 **[Kibana API](https://www.elastic.co/docs/api/doc/kibana)** 发请求并看返回。官方说明见 [Run API requests with Console](https://www.elastic.co/docs/explore-analyze/query-filter/tools/console)。

生产上要记牢一点：请求是 **经 Kibana 代你发往 Elasticsearch** 的，用的是 **你当前浏览器登录身份** 的权限，**不是**你在笔记本上 `curl https://es:9200` 那条路。账号读不了 **`k8s-*`**，Console 里同样会 403。日常别用 **`elastic`** 长期操作（**T9.2.9.7**）。

**2. 怎么进界面**

- 侧栏 **Management → Dev Tools**，左侧选 **Console**（与 **T9.2.9.0** 一致）。  
- 找不到就用顶栏 **全局搜索** 搜 **`Dev Tools`** 或 **`Console`**（[Find apps and objects](https://www.elastic.co/docs/explore-analyze/find-and-organize/find-apps-and-objects)）。  
- 部分页面底部可以展开 **Persistent Console**，和 Dev Tools 里的是**同一套能力、同一份历史**（官方 Console 页有写）。

**3. 语法两档（抄错前缀必报错）**

| 打谁 | 写法 | 说明 |
|------|------|------|
| **Elasticsearch** | 直接写 `GET /_cluster/health`、`GET k8s-*/_count`，**不要**加 `kbn:` | 路径、方法与 [REST APIs](https://www.elastic.co/docs/reference/elasticsearch/rest-apis) 一致 |
| **Kibana** | 路径前加 **`kbn:`**，例如 `GET kbn:/api/index_management/indices` | 具体路径以 [Kibana API](https://www.elastic.co/docs/api/doc/kibana) 为准；升级后偶有微调，以文档和本集群返回为准 |

请求体用 Console 简写（`GET` 下一行跟 JSON）即可；注释可用 **`#`**、`//` 或块注释，方便你在脚本里标注「只读 / 危险」（见官方 Console 页 **Comments**）。

**4. 建议你先跑通的只读命令（全部针对本文 `k8s-*`）**

在编辑器里**一次只选中一条**，点 **运行**（**Ctrl/Cmd + Enter**）。这些都不改数据，适合新建环境验收。

**4.1 集群与索引是否活着**

```js
GET /_cluster/health?filter_path=status,number_of_nodes,timed_out
```

```js
GET /_cat/indices/k8s-*?v&s=index
```

**4.2 文档量（和 Discover 时间窗不是同一个概念，这里数的是索引里命中条件的条数）**

```js
GET k8s-*/_count
```

**4.3 看字段长什么样（排障「规则 / Lens 里写的字段名为什么不对」时极有用）**

```js
GET k8s-*/_field_caps?fields=*&include_unmapped=true
```

若只想看某天的索引，把 `k8s-*` 换成 **`k8s-2026.05.08`** 这类具体名字（与 **T9.2.8** 按日滚动一致）。

**4.4 拉两条最新日志核对管线（`size` 别开大；高峰慎用）**

```js
GET k8s-*/_search
{
  "size": 2,
  "sort": [ { "@timestamp": "desc" } ],
  "_source": ["@timestamp", "log", "stream", "kubernetes.namespace_name", "kubernetes.pod_name", "kubernetes.container_name"],
  "query": {
    "range": {
      "@timestamp": { "gte": "now-30m" }
    }
  }
}
```

字段名若与上表不一致，以你集群 **`_field_caps` 或 Discover 左侧**为准（**Replace_Dots** 见 **T9.2.8、T9.2.9.2**）。

**4.5 需要走 Kibana 管理面时（示例：索引管理 API）**

```js
GET kbn:/api/index_management/indices
```

返回结构随版本变化，**只当「能通」的自测**；日常列表仍建议 **Index Management** 页面（**T9.2.9.6**）对照。

**5. 官方里还值得你用的能力（和值班的关系）**

- **自动补全 / 变量 / 历史**：减少手敲路径错误；**History** 里会留下曾跑过的请求，**别在公用机器上留敏感查询**；需要留存时用 **Export requests** 导出 TXT，**脱敏后再进 Git**（见 Console 页 **Import and export**、**History**）。  
- **Copy as cURL**：给自动化或工单复现时，记得在外部环境自己加认证（官方写在外部跑要补 [authentication](https://www.elastic.co/docs/api/doc/kibana/authentication)）。  
- **Open API reference**：选中请求后在菜单里打开对应 API 文档，避免凭记忆猜参数。

**6. 同一层里的 Search Profiler（慢查询）**

入口仍在 **Dev Tools** 里，见 [Search Profiler](https://www.elastic.co/docs/explore-analyze/query-filter/tools/search-profiler)。把 **Index** 过滤成 **`k8s-*`** 或具体 `k8s-日期`，把你怀疑慢的 **Query DSL** 贴进去点 **Profile**，看各 shard 耗时分解。注意官方提醒：**累计时间**和墙钟时间不是一回事（并行分片时会差很多）。**生产高峰**少对超大时间范围做 profiling，避免和真实流量抢资源。

**7. Grok Debugger（和 T9.2.8 的解析对齐）**

见 [Grok Debugger](https://www.elastic.co/docs/explore-analyze/query-filter/tools/grok-debugger)。把 **Fluent Bit** 里一行典型 **`log`** 贴到 **Sample Data**，在 Kibana 里调 **Grok Pattern**，**Simulate** 通过后再回写到采集配置（**T9.2.8**）。栈上开了安全时，官方要求具备 **`manage_pipeline`** 一类权限才能用 Grok Debugger，缺权限就找管理员开角色。

**8. 什么时候用谁（避免「只会点 Discover」或「只会乱搜 ES」）**

```mermaid
flowchart TB
  subgraph daily[日常值班]
    D[Discover + KQL 数据视图 k8s-*]
  end
  subgraph console[要证据 / 要快]
    C1[Console 只读 cat count mapping 小 search]
    C2[和 Discover 同字段名对齐]
  end
  subgraph slow[慢或要优化 DSL]
    P[Search Profiler 限索引与时间范围]
  end
  subgraph parse[解析不对]
    G[Grok Debugger 调 pattern]
    FB[回到 T9.2.8 改 Parser]
  end
  daily --> C1
  C1 --> C2
  C2 --> P
  G --> FB
  FB --> daily
```

**9. 治理类操作放哪**

**删索引、改 ILM、快照** 能在 Console 里用 ES API 做，但生产上更推荐：**变更走评审 + 有回滚**，和 **T9.2.9.6** 的界面与流程对齐；Console 适合**临时确认**和**脚本化前的试探**，别养成「随手 DELETE」的习惯。

**10. 平台侧可选开关**

若合规要求 **禁止终端用户直接调 API**，可在 **`kibana.yml`**（ECK 即 Kibana CR 的 **`spec.config`**）里设 **`console.ui.enabled: false`**，见 Console 文档 **Disable Console**。关掉后 Kibana 要重启加载资源，官方提示可能短暂变慢。

#### T9.2.9.6、Stack Management：索引治理（模板、ILM、快照）

**先把线头对齐**：**Elasticsearch / Kibana** 版本见 **T9.2.2**（示例 **9.3.3**）；容器日志进集群后，索引名是 **T9.2.8** 定下来的 **`k8s-YYYY.MM.DD`**，Kibana 里用 **`k8s-*`** 数据视图（**T9.2.9.1**）。本节解决三件事：**看清磁盘与分片**、**按公司保留期自动删旧索引**、**用快照做官方支持的备份**。没跑通采集前，你在 UI 里看不到像样的 **`k8s-*`**，别急着调 ILM。

**1. 从哪进（9.3.x）**

- **Index Management**（索引列表、模板、单个索引操作）：侧栏 **Elasticsearch** 分组里点 **Index Management**，或全局搜索 **`Index Management`**（与 [Manage indices in Kibana](https://www.elastic.co/docs/manage-data/data-store/perform-index-operations) 一致）。  
- **Index Lifecycle Policies**：全局搜索 **`Index Lifecycle`** / **`ILM`**，进 **Index Lifecycle Policies** 页面（见 [Create an ILM policy](https://www.elastic.co/docs/manage-data/lifecycle/index-lifecycle-management/configure-lifecycle-policy)）。  
- **Snapshot and Restore**（仓库、SLM、手动快照）：全局搜索 **`Snapshot`**；**ECK 不会替你建好仓库和默认 SLM**，必须按 [Configuring snapshots on ECK](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/cloud-on-k8s) 自己做。

**2. 权限（`elastic` 最全，但生产上要分角色）**

**`elastic` 就是内置超级用户（`superuser`）**，权限最大，联调、紧急处理都可以用它登录（**T9.2.7** 已说明）。这里不是说「不能用 `elastic`」，而是说：**不要只靠这一个账号扛所有日常操作**。在 Kibana 里改索引、模板、ILM、快照，需要的是一串具体的 **Elasticsearch 集群/索引权限**（例如 **`monitor`**、索引上的 **`view_index_metadata` / `manage`**、**`manage_index_templates`** 等），官方列表见 [Manage indices in Kibana · Required permissions](https://www.elastic.co/docs/manage-data/data-store/perform-index-operations#required-permissions)。快照与 SLM 另有 **`manage_slm`**、**`cluster:admin/snapshot/*`** 等要求，见 [Create snapshots · Prerequisites](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots#prerequisites)。**生产上**给平台同事建**专用角色**，权限收敛到「能管 `k8s-*`、模板、ILM、快照」即可，**`elastic` 留给少数管理员与应急**，这样审计和密钥轮转才说得清；细拆法见 **T9.2.9.7**。

**3. Index Management：你每天第一眼该看什么**

1. 打开 **Index Management → Indices**，搜索栏填 **`k8s-`**，只看本文日志索引。  
2. 看 **健康、主分片数、文档量、存储**，有没有 **unassigned** 或 **red**（先回 **T9.2.5 / ES 资源与存储**，别在 Kibana 硬删索引凑绿）。  
3. 点进某个 **`k8s-日期`**，能看 **mapping、settings、统计**；需要时从详情进 **Discover** 抽查文档（官方 [Navigate the Index Management page](https://www.elastic.co/docs/manage-data/data-store/perform-index-operations#navigate-the-index-management-page)）。  
4. 选中一个或多个索引，**Manage index** 里有哪些动作（关闭、force merge、删索引、**Add lifecycle policy** 等），以 [Index operations reference](https://www.elastic.co/docs/manage-data/data-store/index-operations-reference) 为准，**生产上删索引、force merge 要走变更**，别一个人手滑。

**4. 模板（Index / Component templates）：和 T9.2.8 什么关系**

- **Fluent Bit 已经能写进 ES** 时，映射多半由**动态模板**长成；要上生产，建议在 **Index Management → Index templates / Component templates** 给 **`k8s-*`** 配**显式模板**，统一 **`number_of_shards` / `number_of_replicas`**、关键字段类型（例如正文 **`text` + `keyword`**、标签稠密对象考虑 **`flattened`**），减少以后改 mapping 的痛苦。操作向导见 [Templates](https://www.elastic.co/docs/manage-data/data-store/templates)。  
- **`Replace_Dots On`**（**T9.2.8**）管的是「带点路径别和动态映射打架」；**模板**管的是「类型与分片策略」。两件事可以同时做，在架构评审里写死一条。  
- **index_patterns** 用 **`k8s-*`** 不会撞上内置的 **`logs-*-*`** 模板；若以后还接 Elastic Agent，注意官方说的 **priority / 命名冲突**（见 [Templates · Avoid index pattern collisions](https://www.elastic.co/docs/manage-data/data-store/templates#avoid-index-pattern-collisions)）。  

下面这张图来自 Elastic 文档（**创建索引模板向导**），用来对照你在 Kibana 里点的页面是不是同一套（与当前 Stack 小版本相比，边框文案可能略有出入，以你界面为准）。

![Kibana 中创建索引模板向导（Elastic 官方素材）](./images/kibana-index-template-wizard-official.png)

**5. ILM：结合「Fluent Bit 按天建新索引」怎么落地**

**现状**：**T9.2.8** 里 **`Logstash_Format On`** + **`Logstash_Prefix k8s`**，每天一个新索引名 **`k8s-YYYY.MM.DD`**，相当于**按日历切分**，**不是** ES 在写的「rollover 别名」那套。

**推荐做法（简单、少坑）**

1. 建一条 **ILM 策略**：例如 **Hot** 里可以不折腾或只做必要设置；**Delete** 里设 **`min_age: 14d`**（数字按你们合规改），执行 **`delete`**。具体按钮顺序见 [Create an ILM policy](https://www.elastic.co/docs/manage-data/lifecycle/index-lifecycle-management/configure-lifecycle-policy)。  
2. **让以后新建的 `k8s-*` 自动带上策略**：在上面的 **索引模板** 的 **Index settings** 里加 **`index.lifecycle.name`** 指向该策略（不必配 **`rollover_alias`**，因为你没有用 ILM 的 rollover 去起新索引）。设置项说明见 [ILM index settings](https://www.elastic.co/docs/reference/elasticsearch/configuration-reference/index-lifecycle-management-settings)。  
3. **库里已经有一堆历史 `k8s-*`**：在 **Index Management** 里多选索引 → **Add lifecycle policy**，挂同一条策略（官方 [Apply ILM to an existing index](https://www.elastic.co/docs/manage-data/lifecycle/index-lifecycle-management/policy-apply)）。  
4. **官方红线**：**不要**给「现有索引手工挂载」的策略里带 **rollover**——rollover 类策略应通过**模板**去管新索引；否则 rollover 出来的新索引**不会自动继承**策略，详见同一页 **policy-apply** 开头的警告。你们当前是 Fluent Bit **按天起名**，最稳的是策略以 **删除 / 可选温冷迁移** 为主，别硬套 rollover。  
5. **怎么看跑没跑起来**：在索引详情或 [Check ILM status](https://www.elastic.co/docs/manage-data/lifecycle/index-lifecycle-management/policy-view-status) 里看阶段；卡住时查 [Fix ILM errors](https://www.elastic.co/docs/troubleshoot/elasticsearch/index-lifecycle-management-errors)。

**ILM 通用说明**（冷热阶段、force merge、 shrink 等）见 [Index lifecycle management](https://www.elastic.co/docs/manage-data/lifecycle/index-lifecycle-management)；**集群内各节点版本要一致**，混版本集群上 ILM 行为官方不保证。

```mermaid
flowchart TB
  subgraph observe[先看 Index Management]
    O1[搜索 k8s-*]
    O2[健康 分片 存储]
    O3[必要时 Manage index 单索引操作]
  end
  subgraph template[再上模板]
    T1[Component 模板 可复用 mapping/settings]
    T2[Index 模板 index_patterns k8s-*]
    T3[settings 里 index.lifecycle.name]
  end
  subgraph ilm[ILM 策略]
    L1[新建策略 以删除或温冷为主]
    L2[新建索引 模板自动套用]
    L3[老索引 Add lifecycle policy]
  end
  subgraph snap[快照 与 ILM 独立]
    S1[ECK 注册快照仓库]
    S2[SLM 定时 + 保留期]
    S3[恢复演练 别只备份不试]
  end
  observe --> template
  template --> ilm
  observe --> snap
```

**6. 快照与 SLM：备份只能走这条官方路**

- 官方明确：**拷贝节点数据目录不是备份**，恢复集群要用 **Snapshot**（见 [Snapshot and restore](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore) 开篇警告）。  
- **ECK**：仓库与策略要**自己配**，跟 Elastic Cloud 托管默认仓库**不是一回事**，步骤跟 [Configuring snapshots on ECK](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/cloud-on-k8s)。仓库类型与注册方式见 [Self-managed repository types](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/self-managed)（底层 ES 通用概念与 ECK 文档一起读）。  
- Kibana 里 **Snapshot and Restore**：建 **Repository**、建 **SLM policy**（定时、`indices`、`include_global_state`、保留规则），见 [Create, monitor and delete snapshots](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots)。生产建议至少：**业务数据（含 `k8s-*`）+ 集群状态** 有固定节奏；敏感集群状态可单独仓库（同页 **Dedicated cluster state snapshots**）。  
- **恢复**：先读 [Restore a snapshot](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/restore-snapshot)，在测试集群演练，别第一次就在生产上「试手气」。

```mermaid
flowchart LR
  subgraph wrong[错误心态]
    W1[拷 ES 数据目录]
    W2[只靠磁盘扩容]
  end
  subgraph right[正确基线]
    R1[注册快照仓库 S3 等]
    R2[SLM 定时快照 + 保留]
    R3[定期演练恢复到空集群]
  end
  wrong -.->|不支持| X[官方不保证可恢复]
  right --> OK[可审计的 RPO/RTO]
```

**7. 验收（你做完应能对上号）**

- **Index Management** 里能列出 **`k8s-*`**，单索引 **mapping** 与 **T9.2.9.2 / T9.2.8** 字段习惯一致。  
- **ILM**：新产生的 **`k8s-日期`** 索引详情里能看到已挂载策略；超过保留天的旧索引按策略进入 **delete**（时间以策略为准，别指望「改完立刻全没」）。  
- **快照**：SLM **History** 或 API 能看到成功执行；**故意**在测试环境做一次 **restore** 流程通。  

#### T9.2.9.7、Spaces、用户与角色

**和上文对齐**：**Stack** 见 **T9.2.2**（示例 **9.3.3**）；日志在 **`k8s-*`**（**T9.2.8 / T9.2.9.1**）；**`elastic`** 密码与性质见 **T9.2.7**。本节解决：**谁进哪个 Space、谁能读/谁能管索引和告警、对象怎么在多环境之间搬**。你只有单团队、单 Space，也可以先做**角色**，以后加 Space 不用返工。

**1. 先把概念说清楚**

- **Space**：一套**独立的 Kibana 已保存对象**（数据视图、Dashboard、规则、连接器等）。用户能进哪些 Space、在每个 Space 里能点哪些功能，由**角色**决定（见 [Spaces](https://www.elastic.co/docs/deploy-manage/manage-spaces)）。官方提醒：**只在 Space 里藏菜单不等于安全**，真正收口要靠 **Elasticsearch / Kibana 权限**（同页 **Define access to a space** 附近说明）。  
- **角色（Role）**：一边是 **Elasticsearch**（集群权限、**索引**上能干什么），一边是 **Kibana**（按 Space 配置**功能**读写）。用户拿多个角色时，权限是**并集**，不会「加一个角色反而变窄」——要收权限得改/删角色（见 [Role management using Kibana](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/kibana-role-management)）。  
- **用户（User）**：本文用 Kibana 的 **Native** 用户即可入门（[Native user authentication](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/native)）。以后要接 **SSO**（SAML、OIDC、LDAP 等）再走 [User authentication](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/user-authentication)。  
- **`elastic`**：仍是**超级用户**（[Built-in users](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/built-in-users)）。**装集群、救急、建第一个管理员角色**可以用它；日常值班、开发查日志应用下面建的**业务账号**。ECK 上密码与轮转见 [Orchestrator-managed users](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/orchestrator-managed-users-overview) 链到 [managed credentials ECK](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/managed-credentials-eck)。

**2. 管理入口（记不住就全局搜索）**

| 事 | 怎么走 |
|----|--------|
| Space | **Stack Management → Spaces**（或搜 **`Spaces`**） |
| 角色 | **Stack Management → Security → Roles**（或搜 **`Roles`**）；需要 **`manage_security`** 集群权限才能进（见 [Kibana role management · Required permissions](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/kibana-role-management#required-permissions)） |
| 用户 | **Stack Management → Security → Users**（或搜 **`Users`**） |
| 已保存对象 | **Stack Management → Saved Objects**（或搜 **`Saved Objects`**） |

全局搜索习惯见 [Find apps and objects](https://www.elastic.co/docs/explore-analyze/find-and-organize/find-apps-and-objects)。

**3. 建议先做 Space（新建环境最小集）**

1. 用 **`elastic`**（或已有管理员）登录。  
2. **Stack Management → Spaces → Create space**。  
3. 填**名称**、**描述**；**URL identifier**（会进浏览器路径 `/s/xxx/`）**保存后不能改**，起名时想清楚。  
4. 保存。默认还有一个 **Default** Space，可继续当「平台自用」或只留一个 Space 也行。  

规则、连接器**按 Space 隔离**（**T9.2.9.4**）：业务 Space 和平台 Space 分开时，告警对象不会跟开发实验混在一抽屉里。删 Space 会**删掉里面所有对象**，操作前确认（见 [Spaces · Delete a space](https://www.elastic.co/docs/deploy-manage/manage-spaces#delete-a-space)）。

**4. 建角色：两条线一起配（索引 + Kibana）**

详细结构见 [Defining roles](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/defining-roles) 与 [Kibana role management](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/kibana-role-management)。下面给你**本文 `k8s-*` 专用**的两种角色思路，名字可改成你公司的规范。

**4.1 角色 A：只读看日志（开发 / 值班）**

1. **Roles → Create role**，名称示例 **`k8s_logs_reader`**。  
2. **Index privileges**：**Indices** 填 **`k8s-*`**；**Privileges** 至少 **`read`**、**`view_index_metadata`**（官方在 Kibana 角色文档里对看数据就这么建议）。  
3. **Kibana privileges → Add Kibana privilege**：**Spaces** 选你的业务 Space（不要一上来 **All Spaces**，除非你真想全员全空间）。**Privilege** 用 **Custom**，例如：**Analytics** 里 **Discover、Dashboard、Visualize library** 给 **Read**；**Management** 里与 **Stack Management、索引、快照、Dev Tools** 相关项设为 **None**（避免手滑删索引、改集群级设置）。  
4. **Stack Rules / Actions**：若你希望此人**也能收告警但不在此 Space 建规则**，按你们流程单独加；默认只读日志可全部 **None**。  
5. 保存。

**4.2 角色 B：平台运维（索引 / ILM / 快照 / 规则）**

1. 名称示例 **`k8s_platform_ops`**。  
2. **Index privileges**：对 **`k8s-*`** 给予与 **T9.2.9.6** 一致的能力（至少能管生命周期时要有 **`manage`** 或官方要求的组合，以你实际执行的 **ILM、删索引** 为准）；若还要在 **Index Management** 里看系统索引行为，按 [Manage indices · Required permissions](https://www.elastic.co/docs/manage-data/data-store/perform-index-operations#required-permissions) 补 **`monitor`** 等集群权限。  
3. **Cluster privileges**：按需勾选 **`monitor`**、**`manage_index_templates`**；快照与 SLM 需要 **`manage_slm`**、**`cluster:admin/snapshot/*`** 等（见 [Create snapshots · Prerequisites](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots#prerequisites)）。  
4. **Kibana privileges**：在指定 Space（或单独 **Platform** Space）给 **Management** 下 **Index Management、Stack Rules、Actions and Connectors、Snapshot and Restore** 等 **All**；是否开放 **Dev Tools** 由你司规定。  
5. 保存。

**4.3 用户与验证**

1. **Users → Create user**，建 **`dev_zhang`** 之类账号，只挂 **`k8s_logs_reader`**。  
2. 再建平台账号挂 **`k8s_platform_ops`**（可再叠加更严的审批流程，本文不展开）。  
3. **退出后用新用户登录**：应只能进被授权的 Space，**Discover** 里数据视图仍指向 **`k8s-*`**（需在**该 Space** 内建或复制一份数据视图，见下节）。官方完整示例流程见 [Quickstart: Native user and role management](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/quickstart)。

**内置角色**一览与用途见 [Built-in roles](https://www.elastic.co/docs/reference/elasticsearch/roles)。**`kibana_admin`** 能管 Space，相当于全局 Kibana 管理，**别随手发给业务**。

**5. 数据视图、Dashboard 和 Space**

**数据视图 `k8s-*`** 是已保存对象，**跟着 Space 走**（**T9.2.9.1** 是在某个 Space 里建的）。新开 Space 后，要么在该 Space **再建一条 `k8s-*` 数据视图**，要么用 **Saved Objects** 从 Default **复制/导出导入**（见 [Saved objects · Copy to other spaces](https://www.elastic.co/docs/explore-analyze/find-and-organize/saved-objects#saved-objects-copy-to-other-spaces)）。否则用户进新 Space 会提示没有数据视图。

**6. Saved objects（导出、升级、多环境）**

- 入口：[Saved objects](https://www.elastic.co/docs/explore-analyze/find-and-organize/saved-objects)。**Import/Export** 用 **NDJSON**；导入默认可能覆盖同名对象，生产用前先在测试 Kibana 试一遍。  
- **版本**：导出文件**不能往旧版本 Kibana 导**；同主版本内跨小版本一般可按官方兼容表操作（见 [Compatibility across versions](https://www.elastic.co/docs/explore-analyze/find-and-organize/saved-objects#compatibility-across-versions)）。与 **T9.2.9.9** 升级前读 **Release notes** 一起看。  
- **权限**：能进 **Saved Objects Management** 的人能力很大（官方 [Permissions](https://www.elastic.co/docs/explore-analyze/find-and-organize/saved-objects#permissions)），只给平台角色。

**7. 采集账号别和 Kibana 登录混一谈**

**Fluent Bit** 写 ES 用的是 **Elasticsearch 用户**（**T9.2.8** 里示例仍是 **`elastic`**，生产应换成**仅有 `k8s-*` 写权限**的专用用户）。那是**数据面账号**，不是本节给同事登录 Kibana 的账号；密钥只在 **Secret** 里轮转，别和 Kibana 密码共用一套流程。

**8. 可选：ECK 上用文件固化「救命角色」**

若你希望某些角色**只能运维在集群里改、没人能在 UI 里删掉**，可用 **`roles.yml`** 挂进 **Elasticsearch CR**（见 [Defining roles · File-based role management](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/defining-roles#roles-management-file) 里的 ECK 示例）。这是进阶手段，和 **T9.2.5** 的 GitOps 一起评审。

```mermaid
flowchart TB
  subgraph order[推荐顺序]
    S1[Spaces 划工作区]
    S2[Roles 索引 k8s-* + Kibana 功能]
    S3[Users 绑定角色]
    S4[各 Space 补数据视图与对象]
    S5[新用户登录验收]
    S1 --> S2 --> S3 --> S4 --> S5
  end
```

```mermaid
flowchart LR
  subgraph es[Elasticsearch 侧]
    IP[Index k8s-* read 或 manage]
    CP[Cluster monitor 模板 快照等]
  end
  subgraph kb[Kibana 侧]
    KP[按 Space 的 Discover Dashboard Management]
  end
  U[用户] --> R[角色]
  R --> es
  R --> kb
```

#### T9.2.9.8、Kibana 里其他大块

**本文主线**：**Fluent Bit → Elasticsearch（`k8s-*`）→ Kibana（Discover / Lens / 规则等）**，见 **T9.2.8～T9.2.9.6**。下面这些是 **Kibana 侧栏里常见、但数据模型不同** 的模块。排障时**先别往 Observability / Security 里钻**，除非你确认已经按官方把对应数据源接进来了。

**1. 对照表（新建环境怎么选）**

| 模块 | 和 **`k8s-*` 容器 stdout 日志**的关系 | 你现在只有本文配置时 | 官方从哪读 |
|------|----------------------------------------|----------------------|------------|
| **Stack Monitoring** | 管 **Elasticsearch / Kibana 等 Elastic 组件**自己的指标和日志，不是业务 Pod 日志 | 默认**没有**完整监控页数据；要上监控集群或自监控再配 | [Stack monitoring](https://www.elastic.co/docs/deploy-manage/monitor/stack-monitoring) · **ECK**：[Enable on ECK](https://www.elastic.co/docs/deploy-manage/monitor/stack-monitoring/eck-stack-monitoring)（Sidecar **Metricbeat / Filebeat** 把数据打到 **`spec.monitoring`** 指向的集群；生产更建议**独立监控集群**，少玩「生产集群监控自己」当长期方案） |
| **Observability**（Logs / APM / Infrastructure / Synthetics 等） | **Logs** 可以和 ES 里日志共用索引，但 UI 往往假设 **Elastic Agent / 集成** 那套路；**APM、RUM、Profiling** 要探针或 Agent，**不是** Fluent Bit 自动就有 | 没有 Agent 时，**别指望** Observability 里和本文一模一样的开箱体验；业务查询仍以 **Discover + `k8s-*`** 为准 | [Observability 概述](https://www.elastic.co/docs/solutions/observability) · [Get started](https://www.elastic.co/docs/solutions/observability/get-started) |
| **Security**（SIEM / Defend / 检测规则等） | **安全运营**另一条产品线，索引、规则、许可和 **EFK 日志值班** 不是一回事 | **T9.2** 已说本文不上齐 Security；容器 stdout 进 **`k8s-*`** 也**不等于**满足 SIEM 检测包的数据模型 | [Elastic Security 概述](https://www.elastic.co/docs/solutions/security) |
| **Maps** | 要有**地理字段**或 ES 里可绑定的地理数据；纯文本容器日志通常用不上 | 没有 `geo_point` 一类字段就别硬做地图 | [Maps](https://www.elastic.co/docs/explore-analyze/visualize/maps) |
| **Canvas** | 大屏演示向，从 ES 拉数做 workpad | 官方写明：**仅对已升级且已有 workpad 的环境**提供等限制，见下 | [Canvas](https://www.elastic.co/docs/explore-analyze/visualize/canvas) |
| **Machine Learning** | 异常检测、数据帧分析等，**吃数据量与订阅级别**；和 **T9.2.9.4** 的 **Stack Rules** 不是同一套规则引擎 | 小集群先别当必选项；要上再看许可与数据准备 | [What is Elastic Machine Learning?](https://www.elastic.co/docs/explore-analyze/machine-learning) |
| **Fleet / Integrations** | Elastic 官方采集与集成入口 | **本文**用 **Fluent Bit**，不要求 Fleet；以后要双轨再评估 | [Fleet](https://www.elastic.co/docs/reference/fleet)（与 **T9.2.9.0** 一致） |

**2. 排障时别走岔路**

- **业务 Pod 打 stdout、Fluent Bit 已写入 `k8s-*`**：优先 **Discover**、**T9.2.9.5 Console**、**索引治理 T9.2.9.6**。  
- **怀疑 ES/Kibana 自己慢、挂、GC**：才去看 **Stack Monitoring** 是否已按 ECK 打开、监控集群是否真有数据。  
- **要看调用链、RUM、基础设施指标**：属于 **Observability** 能力扩展，按官方 **Get started** 另立项，别和「日志进 ES」混成一条需求。  

```mermaid
flowchart TB
  Q[问题现象]
  Q -->|查某 Pod 某行日志| D[Discover 数据视图 k8s-*]
  Q -->|ES 健康 分片 磁盘| IM[T9.2.9.6 Index Management / ILM]
  Q -->|Kibana 规则 连接器| R[T9.2.9.4 Rules Connectors]
  Q -->|ES 进程 线程池 堆栈| SM[Stack Monitoring 需先启用]
  Q -->|调用链 用户体验| OB[Observability 需 Agent 探针等]
  SM -.->|未配置 monitoring| SMN[页面空 不是业务日志丢了]
```

**3. Canvas 与版本提示**

当前官方 **Canvas** 页写明：**Canvas is only available for upgraded installations with existing workpads**（见 [Canvas](https://www.elastic.co/docs/explore-analyze/visualize/canvas) 开篇）。全新安装若看不到或不可用，以你当前 **Kibana** 界面和 [Kibana release notes](https://www.elastic.co/docs/release-notes/kibana) 为准，别当采集故障去查。

#### T9.2.9.9、真要少写磁盘（在 Fluent Bit 挡，不在 Discover 挡）

**三件事别混**：

1. **Discover / KQL**：只影响**你看什么**，**不写回索引**，**不省** ES 磁盘。  
2. **Kibana / ES 里给角色做文档级权限（DLS）**：别人**搜不到**某些文档，但数据**仍占**索引空间（合规常用，和「省钱删流量」不是同一把刀）。  
3. **Fluent Bit 在进 ES 之前丢记录**：**写入量**才真的下来；配合 **T9.2.9.6 的 ILM**，磁盘才有长期可控性。

```mermaid
flowchart LR
  P[Pod stdout]
  FB[Fluent Bit 管道]
  ES[(Elasticsearch k8s-*)]
  KB[Kibana Discover]
  P --> FB
  FB -->|可选 在此 drop| X[丢弃 不进 ES]
  FB -->|默认 全文发送| ES
  ES --> KB
  KB -->|仅过滤展示| V[界面 不删索引数据]
```

**1. 过滤器放哪、顺序很重要**

**T9.2.8** 里已有 **Kubernetes** 过滤器，把 **`kubernetes.namespace_name`** 等字段补全。要对这些字段做 **grep / rewrite_tag**，必须把对应 **FILTER** 写在 **kubernetes 过滤器之后**（管道从上到下执行）。输入与过滤器总览见 [Tail](https://docs.fluentbit.io/manual/pipeline/inputs/tail)、[Kubernetes Filter](https://docs.fluentbit.io/manual/pipeline/filters/kubernetes)。

**2. 用 Grep 按命名空间或标签丢日志（示例思路）**

官方 [Grep](https://docs.fluentbit.io/manual/pipeline/filters/grep) 支持 **`regex` / `exclude`**，嵌套字段用 [Record Accessor](https://docs.fluentbit.io/manual/administration/configuring-fluent-bit/classic-mode/record-accessor)。下面示例是 **classic `fluent-bit.conf` 风格**，与 **T9.2.8** 一致；**命名空间、正则按你环境改**，先在测试节点验证再全量 rollout。

**示例 A：不要 `kube-system` 命名空间的日志（排除）**

```ini
[FILTER]
    Name    grep
    Match   kube.*
    Exclude kubernetes.namespace_name ^kube-system$
```

**示例 B：只要 `prod`、`staging`（保留匹配；多条件用 `logical_op` 等，见 Grep 文档）**

```ini
[FILTER]
    Name    grep
    Match   kube.*
    Regex   kubernetes.namespace_name ^(prod|staging)$
```

字段名若与上不同（例如 **`Replace_Dots`** 后 label 路径变了），以 **Discover** 或 **T9.2.9.5** 的 **`_field_caps`** 为准，别照抄死。

**3. rewrite_tag：做分流，不是小白必选项**

[Rewrite Tag](https://docs.fluentbit.io/manual/pipeline/filters/rewrite-tag) 可以把不同日志打到**不同 tag**，再接到**不同 OUTPUT**（例如一路 ES、一路 `stdout` 调试，或一路丢弃）。会改变路由和缓冲行为，生产上要单独评审；一般**先用 grep 解决「要不要进 ES」**就够了。

**4. 变更怎么上线才像生产**

1. 在 **ConfigMap**（**T9.2.8** 的 **`fluent-bit-es.yaml`**）里改过滤器，**Git 留痕**。  
2. **`kubectl apply`** 后看 **DaemonSet rollout** 是否全节点成功。  
3. 用 **T9.2.9.5** 对 **`k8s-*` 做 `_count`** 或在 ES 侧看写入速率，确认被排除的命名空间**不再出现**或**条数下降**。  
4. 观察 Fluent Bit **metrics**（见 [Monitoring](https://docs.fluentbit.io/manual/administration/monitoring)）里 **filter drop** 相关计数，避免「配了 exclude 但 Match 没对上导致全没过滤」。  

**5. 全局验收清单（EFK 新建环境打勾用）**

1. **数据视图**：模式 **`k8s-*`**，时间字段 **`@timestamp`**，与 **T9.2.8** 的 **`k8s-YYYY.MM.DD`** 一致（**T9.2.9.1**）。  
2. **Discover**：新日志在预期时间窗内可见；字段名与 **`Replace_Dots`** 一致（**T9.2.9.2**），**Lens / 规则**不混用旧字段名。  
3. **ILM + 快照**：已按 **T9.2.9.6** 评审，不是磁盘红了再补。  
4. **权限**：**T9.2.9.7** 业务角色日常登录；**`elastic`** 仅管理员与应急。  
5. **采集减量（可选）**：若已加 **grep / 分流**，验证被挡命名空间**不再写入**或写入量符合预期。  
6. **升级**：改 **ECK / Stack** 前读 [Elasticsearch release notes](https://www.elastic.co/docs/release-notes/elasticsearch)、[Kibana release notes](https://www.elastic.co/docs/release-notes/kibana)、[ECK 与 K8s 支持矩阵](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s#k8s-supported)，同步改 **T9.2.2** 的 GitOps 版本号；大版本跃迁另对照官方升级指南。  

---

### T9.2.10、（可选）Kafka 缓冲

**默认不要上**：你按 **T9.2.8** 已经 **Fluent Bit → HTTPS → ECK Elasticsearch（`k8s-*`）** 跑通时，**多数集群不必**再加 Kafka。加一层中间件就多 **Topic、消费者、滞后（lag）、升级、鉴权** 一整套运维；只有**容量或架构上真需要**再做。

**和上文对齐**：采集端仍是 **T9.2.8** 的 **DaemonSet + `Tag kube.*` + Kubernetes 过滤器**；下游 **Elasticsearch** 仍是 **T9.2.5** 的 **`quickstart-es-http.logging.svc:9200`**、索引名 **`k8s-YYYY.MM.DD`**（**`Logstash_Format`/`Logstash_Prefix`**）；**Kibana 数据视图**仍是 **`k8s-*`**（**T9.2.9.1**）。本节只改**中间这一段**：日志先进 **Kafka**，再由**消费者**写 ES，**不要在没消费者的情况下**把 Fluent Bit 唯一出口改成 Kafka。

**1. 什么时候值得上（生产上常见理由）**

- **ES 写入尖峰明显**：批量发版、全量日志突增，ES 经常出现 **429 / bulk 拒绝**，而 Fluent Bit 侧已按 **T9.2.8** 把缓冲、`Buffer_Size` 等调到合理仍顶不住。  
- **要写多个下游**：同一份日志既要进 ES，又要进别的系统（对象存储、数仓、风控流水线等），用 Kafka 做**解耦总线**比 Fluent Bit 多路复制好维护。  
- **需要独立扩缩消费者**：ES 维护窗口想**短暂停写**，仍希望采集端不停、消息积在 Kafka（**注意磁盘与 retention**，不是无限堆）。

**2. 目标拓扑（一条链，别双写）**

生产上**不推荐**长期「Fluent Bit 同时写 ES 又写 Kafka」当主方案（双份写入、一致性问题、故障点更多）。常见做法是：

1. **Fluent Bit 只产出到 Kafka**（或先灰度一部分 `Match`）。  
2. **消费者**（下面以 **Logstash** 为例）从 Topic 读 JSON，再 **bulk 写入 ES**，索引模式与 **T9.2.8** 一致 **`k8s-%{+YYYY.MM.dd}`**（或等价 `index` 模板），这样 **Discover / ILM** 不用换故事。

```mermaid
flowchart LR
  subgraph before[本文已实践 T9.2.8]
    FB1[Fluent Bit DaemonSet]
    ES1[(ECK Elasticsearch)]
    FB1 -->|HTTPS bulk| ES1
  end
```

```mermaid
flowchart LR
  subgraph after[上 Kafka 后推荐形态]
    FB2[Fluent Bit 仅 Producer]
    K[(Kafka Cluster)]
    LS[Logstash 或 等价消费者]
    ES2[(ECK Elasticsearch k8s-* )]
    FB2 -->|Kafka 协议| K --> LS -->|HTTPS bulk| ES2
    KB[Kibana Discover 不变]
    ES2 --> KB
  end
```

**3. 在 K8s 里怎么落 Kafka（Strimzi 路线）**

自管集群最省事的路径之一是用 **Strimzi** 运维 Kafka（Operator + `Kafka` / `KafkaTopic` 等 CR）。入门与安装顺序见 [Strimzi Quickstarts](https://strimzi.io/quickstarts/)、组件说明见 [Strimzi Overview](https://strimzi.io/docs/operators/latest/overview)。生产要点（和写进变更评审的内容）：

- **命名空间**：可与 **`logging` 分开**（例如 **`kafka`**），RBAC、网络策略按公司规范；Fluent Bit / Logstash 要能 **DNS 解析** 到 bootstrap 地址（集群内 **`*-kafka-bootstrap:9092`** 一类，以 Strimzi 生成的 Service 为准）。  
- **副本与存储**：单节点只适合联调；生产至少按 **高可用** 示例与磁盘、**replication factor** 评审。  
- **Topic**：单独用 **`KafkaTopic`** 管理，提前定好 **分区数**（影响并行度）、**retention**（磁盘上限）、**compression**（`compression.type` 等，见 [Kafka 文档](https://kafka.apache.org/documentation/)）。Topic 名示例：`k8s-container-logs`（自定，与消费者订阅一致）。  
- **安全**：生产应 **TLS + SASL/SCRAM 或 mTLS**、Kafka **ACL**，与 **T9.2.8** 里 ES 的 TLS 习惯一致；Strimzi 配监听与用户的细节以其当前文档为准。

**4. Fluent Bit 侧：Kafka 输出插件**

官方 [Kafka output](https://docs.fluentbit.io/manual/pipeline/outputs/kafka) 基于 **librdkafka**。和 **T9.2.8** 的 classic 配置衔接时，典型在 **Kubernetes 过滤器之后**增加 **`[OUTPUT]`**（或把原 **`es`** 输出**替换**为 **`kafka`**，切换窗口见下文）：

- **`Brokers`**：Strimzi bootstrap **Service:端口**（集群内地址）。  
- **`Topics`**：上一步建的 Topic。  
- **`Format json`**：便于 Logstash **`codec => json`** 解析（默认即为 JSON 时可按文档确认）。  
- **`rdkafka.*`**：可按官方建议调 **`request.required.acks`**、**`log.connection.close`** 等（见插件页说明）；生产务必理解 **acks** 与丢数风险。  
- **`message_key` / `message_key_field`**：可用 **`kubernetes.pod_name`** 等做 **Kafka 分区键**，让同一 Pod 日志进同一分区、顺序更稳（字段名以你记录为准）。  
- **云上 MSK**：若用 **Amazon MSK IAM**，需 **Fluent Bit 4.0.4+** 且打开 **`aws_msk_iam`**（同页 **AWS MSK IAM** 小节）；本文 **T9.2.1** 为 **5.0.3**，版本上满足，但权限与网络另审。

**5. 消费者侧：Logstash → ES（与 `k8s-*` 对齐）**

用官方 [**kafka input**](https://www.elastic.co/docs/reference/logstash/plugins/plugins-inputs-kafka) 订阅 Topic，`codec => json`（或 `json_lines`，与 Fluent Bit 实际格式一致即可），**elasticsearch output** 的 **`index`** 使用与 **T9.2.8** 相同的按日模式，例如：

```ruby
input {
  kafka {
    bootstrap_servers => "my-cluster-kafka-bootstrap.kafka.svc:9092"
    topics            => ["k8s-container-logs"]
    codec             => "json"
    # 生产再加 security_protocol、sasl 等
  }
}
output {
  elasticsearch {
    hosts    => ["https://quickstart-es-http.logging.svc:9200"]
    index    => "k8s-%{+YYYY.MM.dd}"
    user     => "…"
    password => "…"
    ssl      => true
    cacert   => "…"
    # 与 ES 9.x 兼容的 ecs/data stream 选项按你们规范与插件文档取舍
  }
}
```

**`user`/`password`/`cacert`** 与 **T9.2.7 / T9.2.8** 一致，**不要**用 **`elastic`** 做长期管道账号，应建**仅有写 `k8s-*` 权限**的专用用户（同 **T9.2.7** 生产建议）。Logstash 自身版本需与 **Elasticsearch 9.3.x** 兼容，以 [Elastic 支持矩阵](https://www.elastic.co/support/matrix) 与你们镜像为准。

**自研消费者**也可以，只要：**JSON 解析稳定**、**批量 bulk**、**重试与死信**、**监控 consumer lag** 四件事齐活。

**6. 切换顺序（减少事故）**

1. **先**把 Kafka、Topic、**Logstash（或消费者）**跑通，确认能写出 **`k8s-*`**，**Discover** 能查到**新**日志。  
2. **再**改 Fluent Bit：灰度节点或先双写短窗口（仅过渡期）→ 最终切到 **仅 Kafka**（或保留直连 ES 的灾备管道，需书面预案）。  
3. 全程盯 **consumer lag**、ES **ingest 线程池 / bulk 拒绝率**、Kafka **磁盘**。  
4. 回滚思路：Fluent Bit **改回 `es` 输出** 指向 **T9.2.8** 原配置；Kafka 里积压按 retention 与业务决定是否另消费。

**7. 验收**

- **Kibana**：数据视图 **`k8s-*`**，新日志时间线连续；字段与 **Replace_Dots** 规则仍与 **T9.2.9.2** 一致。  
- **ES**：无持续 **429**；索引仍按日 **`k8s-日期`**。  
- **Kafka**：Topic 生产消费速率匹配，**lag** 有告警阈值。

---

### T9.2.11、对照 T9.1

把 Kubernetes 官方归纳的「集群级日志三条路」和本文已经一步步搭好的 **ECK + Fluent Bit + Kibana** 对上号。后面做架构评审、扩容、换人或接告警工单时，先看这里不容易和 **T9.1**、**T9.2.8** 打架。

官方说法见 [Cluster-level logging architectures](https://kubernetes.io/docs/concepts/cluster-administration/logging/#cluster-level-logging-architectures)（和 **T9.1.3** 是同一出处）。

**怎么读**：**T9.2.11.1** 路径映射表与图 → **T9.2.11.2** 数据面简图（对齐 **T9.2.1**）→ **T9.2.11.3** 上线核对（对齐 **T9.2.2～T9.2.10**）。

#### T9.2.11.1、三类路径映射

```mermaid
flowchart TB
  subgraph k8s_official["K8s 官方三类 见 T9.1.3"]
    A[节点级采集代理 DaemonSet]
    B[Sidecar]
    C[应用内直推后端]
  end
  subgraph t92["本文 T9.2 已落地"]
    FB["Fluent Bit DaemonSet 见 T9.2.8"]
    ES["Elasticsearch 见 T9.2.5"]
    KB["Kibana 见 T9.2.6 与 T9.2.9"]
    T91["Sidecar 见 T9.1.4.2 与 T9.1.4.3"]
    T914["应用直推 见 T9.1.4.4"]
  end
  A --> FB
  FB -->|"HTTPS 写入 k8s 按日索引"| ES
  ES --> KB
  B --> T91
  C --> T914
```

| K8s 官方路径（T9.1） | 在本文 T9.2 里是什么 | 文档落点 |
|---------------------|---------------------|----------|
| **节点级日志代理（推荐 DaemonSet）** | **Fluent Bit** 每节点一份，读本节点 **`/var/log/pods`** 等容器日志目录，经 **T9.2.8** 写入 **Elasticsearch**；索引 **`k8s-YYYY.MM.DD`**，Kibana 数据视图 **`k8s-*`**（**T9.2.9**） | **T9.1.4.1** ↔ **T9.2.1**、**T9.2.8** |
| **Sidecar** | 本文 **没有**再搭一套 Sidecar 采集；业务若坚持写文件、节点策略不够，仍按 **T9.1.4.2 / T9.1.4.3** 做；日志进 stdout 后仍由节点上的 **Fluent Bit** 收走 | **T9.1.4.2～4.3** |
| **应用内直推** | Kubernetes 不规定实现方式；可与 ES 并存，但属于**另一条管线**（SDK、索引命名、权限要单独设计），**不要**和 **`k8s-*` 容器 stdout 采集**混成一套默认假设 | **T9.1.4.4** |

可选缓冲：**T9.2.10** 的 **Kafka** 插在 Fluent Bit 与下游之间时，数据面仍是「节点代理 → 后端」，只是中间多一层解耦；和 K8s 官方三类划分不冲突。

#### T9.2.11.2、数据面简图

与 **T9.2.1** 同一条链路，扫一眼对照用。

```mermaid
flowchart LR
  subgraph node["工作节点"]
    APP["业务 Pod 标准输出与标准错误"]
    FILES["节点容器日志文件"]
    FB2["Fluent Bit"]
  end
  subgraph logging["命名空间 logging"]
    ES2["Elasticsearch"]
    KB2["Kibana"]
  end
  APP --> FILES --> FB2 -->|"TLS 9200 写入 k8s 按日索引"| ES2
  ES2 --> KB2
```

> **插图插槽（可选）**：若你们有对内架构评审材料，可在此放一张「节点 → Fluent Bit → ES → Kibana」拓扑截图，路径建议 `./images/t9211-efk-data-plane.png`。

#### T9.2.11.3、上线核对

下面每条都能在本文前面找到具体步骤；这里只列**决策和命令级核对**，避免泛泛而谈。

**1）Kubernetes 与 ECK**

- 集群版本在 [ECK Supported versions](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s#k8s-supported) 范围内（与 **T9.2.2** 表一致：**1.31–1.35**）。
- `kubectl get pods -n elastic-system`：Operator Pod 正常；版本与 **T9.2.2** 中 **ECK Operator** 行一致。
- `kubectl get elasticsearch.k8s.elastic.co,kibana.k8s.elastic.co -n logging`：`READY` 与 **T9.2.5 / T9.2.6** 一致。

**2）Elasticsearch 资源与虚拟内存**

- PVC、StorageClass、`volumeClaimTemplates` 与容量规划一致；缩容受限见 [Updating volume claims](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/volume-claim-templates#k8s-volume-claim-templates-update)。
- 生产推荐：宿主机 **`vm.max_map_count=1048576`**（**Elasticsearch 8.16+**；更低版本阈值见官方），并**不要**在 ES 里写死 `node.store.allow_mmap: false` 凑合；试跑/受限集群才用 quickstart 那套关 mmap。细则见 [Virtual memory（ECK）](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/virtual-memory) 与 [vm.max_map_count（自管说明）](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/vm-max-map-count)。

**3）安全与入口**

- ES / Kibana 对外或对内访问：**TLS**、**Ingress 或 port-forward** 策略与 **T9.2.6 / T9.2.7** 一致；HTTP 证书与 [K8s HTTPS 设置](https://www.elastic.co/docs/deploy-manage/security/k8s-https-settings) 做法对齐。
- **`elastic`** 仅作管理与排障；采集与自动化用**专用用户 + 角色**（见 **T9.2.7**、**T9.2.9.7**）。凭据只进 **Secret**，不进 ConfigMap 明文。

**4）Fluent Bit（与 T9.2.8 一字对齐则不会歪）**

- 镜像版本与 **T9.2.2** 一致；写 ES 时 **校验服务端证书**（生产禁用「跳过校验」类配置）。
- 输出索引仍为 **`k8s-YYYY.MM.DD`**（**`Logstash_Format On`** + **`Logstash_Prefix k8s`**）；过滤 **`Match`** 与你们约定的 **`kube.*`** 一致，避免和 **T9.2.9.x** 字段规则打架。
- **`Replace_Dots On`** 时，KQL / 告警里字段名以 **Discover 左侧列表**为准，勿照抄带点号的旧示例。

**5）Kibana 与治理**

- 数据视图 **`k8s-*`**、时间字段与 **T9.2.9** 一致；索引生命周期、快照、模板见 **T9.2.9.6**；告警见 **T9.2.9.4**。

**6）若启用 Kafka（T9.2.10）**

- Topic、Fluent Bit **Kafka Output**、下游 **Logstash / 消费组** 与切换顺序按 **T9.2.10** 执行；回灌后仍验证 **`k8s-*`** 与无持续 **429**。

**7）变更与升级**

- 改 ES / Kibana `spec`、扩节点、升 Stack 版本：集群要有**额外 CPU/内存/盘**扛滚动；用 `kubectl apply` 渐进变更，盯 Operator 日志与 Pod 事件。见 [Update your deployments](https://www.elastic.co/docs/deploy-manage/deploy/cloud-on-k8s/update-deployments)。

---

下一节 **T9.3、Loki** 按 **Helm** 走 **Loki + Promtail（+ 可选 Grafana）**；和本文 **EFK** 是两条常见路线，**二选一为主**即可。ES 前若要加 **Kafka** 缓冲见 **T9.2.10**；**Loki** 与 **Elasticsearch** 怎么选见 **T9.3.1**。

## T9.3、Loki

[Grafana Loki](https://grafana.com/docs/loki/latest/) 按**标签**索引元数据，正文日志压缩成块存起来，不像传统全文检索那样给每个词建倒排索引，成本通常更友好。查询用 **LogQL**，和 **Prometheus / Grafana** 一套习惯。**采集**：官方常提 **Promtail**、**Grafana Alloy**（承接原 Grafana Agent）。本节按 **Promtail + Helm** 落地；你已在 **T9.1 / T9.2** 用的 **Fluent Bit** 也可直接输出到 Loki（[Fluent Bit Loki 输出](https://docs.fluentbit.io/manual/pipeline/outputs/loki)），不冲突。

### T9.3.1、和 ES 怎么选（大白话）

| 维度 | Loki | Elasticsearch（EFK） |
|------|------|----------------------|
| 索引侧重 | 主要索引**标签**；正文走块存储与压缩 | 常做**全文**检索，资源占用更高 |
| 查询 | **LogQL**，跟 Prom 栈对齐 | **KQL / DSL** 等，生态成熟 |
| 典型场景 | 已有 **Grafana**，想控成本、标签统一 | 强检索、复杂分析、合规检索 |

```mermaid
flowchart LR
  subgraph efk[EFK]
    FB1[采集] --> ES[(Elasticsearch)]
    ES --> KB[Kibana]
  end
  subgraph loki["Loki 栈"]
    PR[Promtail] --> LO[(Loki)]
    LO --> GF[Grafana]
  end
```

### T9.3.2、日志怎么走

```mermaid
flowchart LR
  P[Pod stdout]
  N[节点 /var/log/pods]
  PT[Promtail DaemonSet]
  GW[Loki Gateway]
  L[(Loki 存储)]
  P --> N --> PT --> GW --> L
```

部署模式（单体 / 简单可扩展 / 微服务）见 [Deployment modes](https://grafana.com/docs/loki/latest/get-started/deployment-modes/)。**学习和小集群**用 **Monolithic**（单进程，与文档里旧名 **Single Binary** 同类）即可；上生产再接对象存储、多副本与读写拆分。

下面是 [Loki overview](https://grafana.com/docs/loki/latest/get-started/overview/) 官网同款示意图（本仓库从 [grafana/loki 文档源](https://github.com/grafana/loki/blob/main/docs/sources/get-started/loki-overview-2.png) 同步到本地，便于离线阅读）：

![Loki 日志栈：Agent、Loki、Grafana](./images/loki-overview-2.png)

### T9.3.3、版本与校验

升级或排错前，对照 [Release notes](https://grafana.com/docs/loki/latest/release-notes/)、[Helm 安装](https://grafana.com/docs/loki/latest/setup/install/helm/) 与 [Chart README](https://github.com/grafana-community/helm-charts/blob/main/charts/loki/README.md)。

**本文本节校验日期：2026-04-20**

| 组件 | 版本 / 来源 | 说明 |
|------|-------------|------|
| Loki 镜像 | **3.7.1**（Chart `appVersion`，可再 pin tag） | [grafana/loki Releases](https://github.com/grafana/loki/releases)；Chart 见下 |
| Loki Helm Chart | **13.2.0**（与 `helm search repo grafana-community/loki` 一致即可） | [grafana-community/helm-charts](https://github.com/grafana-community/helm-charts) |
| Promtail Chart | `helm search repo grafana/promtail` 当前 stable | 镜像多为 `grafana/promtail` |
| Grafana Chart | `helm search repo grafana/grafana` 当前 stable | 本文用于 Explore 查日志 |

推荐用 **OCI** 装 Loki：`oci://ghcr.io/grafana-community/helm-charts/loki`。下文同时写 **HTTP 仓库**，方便内网镜像站对齐。

### T9.3.4、开工前要准备什么

与 **T9.2** 相同：组件放在 **`logging`** 命名空间；没有则先建：

```bash
kubectl create namespace logging
```

示例 **Monolithic** 打开 **MinIO** 子 Chart 时会多占 **PVC**；把 YAML 里的 **StorageClass** 换成你集群真实值（`kubectl get sc`）。**生产**请接云对象存储或自建 S3 兼容端点，不要长期把 MinIO 当唯一真源；参数见 [Helm reference](https://grafana.com/docs/loki/latest/setup/install/helm/reference/) 与 [Install monolithic](https://grafana.com/docs/loki/latest/setup/install/helm/install-monolithic/)。

### T9.3.5、装 Loki（Monolithic）

下面 **values** 对齐官方 [Install the monolithic Helm chart — Single Replica](https://grafana.com/docs/loki/latest/setup/install/helm/install-monolithic/)，额外固定 **`deploymentMode: Monolithic`**、**`-n logging`**。若 Helm 报未知字段，以 `helm show values grafana-community/loki --version 13.2.0` 为准。

将以下内容保存为 `loki-values.yaml`：

```yaml
# loki-values.yaml（学习/联调用；生产请换对象存储与高可用参数）
loki:
  commonConfig:
    replication_factor: 1
  schemaConfig:
    configs:
      - from: "2024-04-01"
        store: tsdb
        object_store: s3
        schema: v13
        index:
          prefix: loki_index_
          period: 24h
  pattern_ingester:
    enabled: true
  limits_config:
    allow_structured_metadata: true
    volume_enabled: true
  ruler:
    enable_api: true

minio:
  enabled: true

deploymentMode: Monolithic

singleBinary:
  replicas: 1

backend:
  replicas: 0
read:
  replicas: 0
write:
  replicas: 0
ingester:
  replicas: 0
querier:
  replicas: 0
queryFrontend:
  replicas: 0
queryScheduler:
  replicas: 0
distributor:
  replicas: 0
compactor:
  replicas: 0
indexGateway:
  replicas: 0
bloomPlanner:
  replicas: 0
bloomBuilder:
  replicas: 0
bloomGateway:
  replicas: 0
```

安装（**固定 Chart 小版本**，避免他人 `helm install` 时偷偷跨大版本）：

```bash
helm repo add grafana-community https://grafana-community.github.io/helm-charts
helm repo update

helm upgrade --install loki grafana-community/loki -n logging -f loki-values.yaml --version 13.2.0
```

OCI 写法（等价，镜像源更省事时可用）：

```bash
helm upgrade --install loki oci://ghcr.io/grafana-community/helm-charts/loki -n logging -f loki-values.yaml --version 13.2.0
```

查看 Pod（会包含 Loki、Gateway、缓存、MinIO 等，略等几分钟变 Running）：

```bash
kubectl get pods -n logging
```

```mermaid
flowchart LR
  GW["Service loki-gateway"]
  SB[Loki singleBinary]
  MI[(MinIO)]
  GW --> SB
  SB -->|S3| MI
```

刚装完本节时，**logging** 里先有 **Gateway + Loki + MinIO**（以及 Chart 带出的缓存等）；**Promtail / Grafana** 在 **T9.3.6、T9.3.7** 再补上去。

**Helm 报错时**：先跑 `helm show values grafana-community/loki --version 13.2.0` 核对字段。个别版本示例仍写 **`deploymentMode: SingleBinary`**，与 **Monolithic** 在 [Chart Upgrading](https://github.com/grafana-community/helm-charts/blob/main/charts/loki/README.md#upgrading) 里属同一类部署，按报错二选一；跨大版本升级必须先读该章节。

### T9.3.6、装 Promtail

Promtail 走 **Grafana 官方 Helm 仓库**里的 **`grafana/promtail`**（和 **`grafana-community`** 不是同一个 `helm repo`，两个都要 add）。

客户端 URL 一般指向 **本 Release 名** 下的 Gateway。Release 名 **`loki`**、命名空间 **`logging`** 时，推送地址常为：

`http://loki-gateway.logging.svc.cluster.local/loki/api/v1/push`

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm upgrade --install promtail grafana/promtail -n logging \
  --version 6.17.1 \
  --set "config.clients[0].url=http://loki-gateway.logging.svc.cluster.local/loki/api/v1/push"
```

版本号与 **T9.4.1** 对齐；若 `helm search` 已有更新小版本，以仓库为准。若你改了 Helm Release 名，Service 前缀会跟着变，用 `kubectl -n logging get svc` 找带 **gateway** 的那条再拼 URL。

```bash
kubectl get pods -n logging -l app.kubernetes.io/name=promtail
```

默认会挂 **`/var/log/pods`**，和 **T9.1** 说的一致。节点没有 `/var/lib/docker/containers` 时，按当前 Chart 的 `values` 删掉多余 hostPath 即可。

### T9.3.7、装 Grafana 接 Loki

只调 API 可以不用 Grafana；**日常查日志**用 **Grafana Explore** 最顺手。新建 `grafana-values.yaml`（**adminPassword 必须改掉**；生产用 **Secret** 或 SSO，勿照抄）：

```yaml
# grafana-values.yaml
adminUser: admin
adminPassword: "请改成强密码"

service:
  type: ClusterIP

datasources:
  datasources.yaml:
    apiVersion: 1
    datasources:
      - name: Loki
        type: loki
        url: http://loki-gateway.logging.svc.cluster.local
        access: proxy
        isDefault: true
```

```bash
# 若已在 T9.3.6 执行过 helm repo add grafana，则只需 helm repo update
helm upgrade --install grafana grafana/grafana -n logging -f grafana-values.yaml
```

```bash
kubectl -n logging port-forward svc/grafana 3000:80
```

浏览器访问 `http://127.0.0.1:3000`，进 **Explore**，选 **Loki**，用 **LogQL** 试查（例如 `{namespace="default"}`）。

**（插槽：此处贴本环境 Grafana Explore 查询截图，便于后来者对照 UI。）**

**T9.3.5～T9.3.7 都做完后**，命名空间里采集与查询关系可概括成：

```mermaid
flowchart TB
  subgraph logging["命名空间 logging"]
    PT[Promtail DaemonSet]
    GW["Service loki-gateway"]
    SB[Loki singleBinary]
    MI[(MinIO 子 Chart)]
    PT -->|push API| GW
    GW --> SB
    SB -->|S3| MI
  end
  GR[Grafana] -->|LogQL| GW
```

### T9.3.8、和 T9.1 / T9.2 的对应

| 上文 | 本节落点 |
|------|----------|
| **T9.1** 采集路径、Sidecar | **Promtail** 就是典型的**节点级 DaemonSet** 采集 |
| **T9.2** EFK → ES + Kibana | 本节走 **Loki + Grafana**（或只 API），两套路线二选一或按业务拆分 |
| **Fluent Bit** | 已在前面出现过；若要**只保留 Fluent Bit 推到 Loki**，按 [Loki output](https://docs.fluentbit.io/manual/pipeline/outputs/loki) 配置即可，不必再装 Promtail |

### T9.3.9、（可选）Traefik 与入口

入口用 **Traefik** 时，访问日志一般开 **access log**（多为 stdout、JSON），**Promtail** 随节点日志一并采集即可，不必为 Traefik 单独再起一套采集。具体开关以你当前的 Traefik 主版本文档为准。

**（插槽：若要大盘，可到 Grafana 官网选 Traefik 类 Dashboard 模板，导入后把 LogQL 里的 `job`、标签改名与你环境一致。）**

---

下一节 **T9.4、Promtail** 写配置要点、Helm 改法与 **Alloy** 迁移（官方已宣布 Promtail **EOL**），并与 **T9.3.6** 的安装衔接。

## T9.4、Promtail

> **官方结论（2026）**：[Promtail](https://grafana.com/docs/loki/latest/send-data/promtail/) 已于 **2026-03-02 EOL**（不再演进，商业支持结束），**新立项与扩容**请优先 [Grafana Alloy](https://grafana.com/docs/alloy/latest/) 采集并写入 Loki；**lambda-promtail** 不在此 EOL 范围内。本节仍按 **T9.3.6** 已装的 Promtail 讲清配置骨架，便于维护存量集群，并按 [Migrate to Alloy](https://grafana.com/docs/loki/latest/setup/migrate/migrate-to-alloy/) 替换采集端。

**T9.3** 里 Helm 已起 DaemonSet；落地时你要动的主要是 **`config.clients`（推到哪）**、**`positions`（断点续读）**、**`scrape_configs`（采哪些文件 / Pod）**、**`pipeline_stages`（解析与打标签）**。字段级说明已收进官方文档与 Chart 默认模板，下面只保留**生产上容易踩坑**的部分；需要查全量参数时，以 `helm show values grafana/promtail` 与 [Promtail 主文档](https://grafana.com/docs/loki/latest/send-data/promtail/) 为准。

### T9.4.1、版本与校验

**本文本节校验日期：2026-04-21**

| 项目 | 说明 |
|------|------|
| Promtail Helm Chart | `grafana/promtail`，示例 **6.17.1**（以 `helm search repo grafana/promtail --versions` 为准）；Chart 已 **deprecated**，长期应迁 **Alloy** |
| Promtail 镜像 | 建议与 **Loki 同发行线**（**T9.3** 使用 Loki **3.7.1** 时，镜像宜 **3.7.1**：在 `values` 里设 `image.tag: "3.7.1"`，覆盖 Chart 默认 `appVersion`） |
| Loki 写入地址 | 与 **T9.3** 一致：`http://loki-gateway.logging.svc.cluster.local/loki/api/v1/push`（Release 名 **`loki`**、命名空间 **`logging`**） |

### T9.4.2、配置骨架

```mermaid
flowchart LR
  subgraph nd["节点"]
    POD["/var/log/pods/..."]
    POS["positions 文件"]
  end
  subgraph pr["Promtail Pod"]
    SC[scrape_configs]
    PL[pipeline_stages]
    CL[clients]
  end
  LG[loki-gateway]
  LO[Loki]
  POD --> SC
  SC --> PL
  PL --> CL
  CL -->|HTTP POST| LG
  LG --> LO
  POS <--> SC
```

**三件事别搞混**：

1. **clients.url**：必须带 **`/loki/api/v1/push`**；多租户时再加 **`tenant_id`**（与 Loki 配置一致）。
2. **positions**：路径要在容器里**可写**（Chart 默认常挂 **`/run/promtail`**）。
3. **标签**：进 Loki 的是**标签集合**；**高基数**字段（请求 ID、用户 ID 等）不要做成标签，除非你算过成本。

### T9.4.3、Helm 改法

`grafana/promtail` 的默认值在 **`config`** 下拼装完整 **`config.file`**，常见改法：

- **只改推送地址**（与 **T9.3.6** 的 `--set config.clients[0].url=...` 等价）；
- **改镜像版本**：`image.tag`；
- **改抓取 / 管道**：动 **`config.snippets`**、**`config.snippets.extraScrapeConfigs`**、**`config.snippets.extraRelabelConfigs`** 等（**键名以当期 Chart 为准**）。

示例片段（**字段名请对照** `helm show values grafana/promtail --version 6.17.1`）：

```yaml
# promtail-values.yaml 片段
image:
  tag: "3.7.1"

config:
  clients:
    - url: http://loki-gateway.logging.svc.cluster.local/loki/api/v1/push
  positions:
    filename: /run/promtail/positions.yaml
```

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
helm upgrade --install promtail grafana/promtail -n logging -f promtail-values.yaml --version 6.17.1
```

### T9.4.4、K8s 抓取要点

- **DaemonSet 一节点一实例**：只读**本节点**日志；**Kubernetes SD** 下务必用 **`relabel`** 保证目标 Pod 落在本节点（Chart 默认模板里已有典型 **`__meta_kubernetes_pod_node_name`** 一类处理，见 `values.yaml` 里 **`snippets.scrapeConfigs`**）。
- **重复日志**：多个 `job` 若匹配同一文件，会向 Loki 打**重复流**，排障时先合并 / 收紧 `scrape_configs`。
- **与 Prometheus 对齐**：`namespace`、`pod`、`container` 等标签习惯与 Prometheus 一致时，**日志与指标**更好关联。

```mermaid
flowchart TB
  SD["K8s SD 本节点 Pod"]
  RB[relabel_configs]
  PT["tail 日志路径"]
  PL[pipeline_stages]
  SD --> RB --> PT --> PL
```

### T9.4.5、管道示例

下面示意：**JSON 行**里抽出 `level` 打成标签，再按标签**丢掉 debug**（完整阶段列表以官方说明为准；Helm 默认往往已有 **`cri: {}`** 处理容器运行时格式，勿与业务 JSON 阶段重复打架）。

```yaml
pipeline_stages:
  - json:
      expressions:
        level: level
  - labels:
      level:
  - match:
      selector: '{level="debug"}'
      action: drop
```

**（插槽：可贴 `kubectl -n logging logs` 里 Promtail 正常工作的片段截图。）**

### T9.4.6、迁到 Alloy（推荐）

1. 读 [Migrate to Alloy](https://grafana.com/docs/loki/latest/setup/migrate/migrate-to-alloy/)，用官方迁移工具把 **Promtail 配置转成 Alloy**（命令以文档为准）。
2. 推送端仍指向 **同一 Loki**（`loki-gateway.logging.svc.cluster.local`），与 **T9.3.5** 不冲突。
3. 新装采集端**不要再押注 Promtail** 长期演进。

```mermaid
flowchart LR
  A[存量 Promtail] -->|迁移| B[Grafana Alloy]
  B -->|push| G[loki-gateway]
  G --> L[Loki]
```

**（插槽：可贴 Alloy 运行后 Grafana Explore 查询截图。）**

---

## T9.5、报警

日志告警常见三条路：**Grafana 里对 Loki 做告警规则**（官方当前主推的用法之一）、**Loki Ruler 用 LogQL 评估规则并通知 Alertmanager**、**采集端从日志生成指标再给 Prometheus**（Promtail 已 EOL，长期请用 Alloy 等价能力）。下面按「先易后难」排，并与 **T9.3、T9.4** 的 **Loki / Promtail** 安装方式对齐，不再使用已废弃的 **`loki-stack`** 与旧外链图。

### T9.5.1、版本与校验

**本文本节校验日期：2026-04-21**

| 项目 | 说明 |
|------|------|
| Loki | 与 **T9.3.5** 一致，示例 **3.7.1**（Chart **13.2.0**） |
| 告警规则语言 | **LogQL**（Ruler）或 **Grafana 表达式**（Grafana Alerting），见 [Alerting](https://grafana.com/docs/loki/latest/alerting/) |
| Alertmanager | 与现有监控栈一致；若用 **kube-prometheus-stack**，版本以 `helm search` / 官方发行为准 |
| 采集端 | **Promtail** 已 **EOL**（**T9.4**）；新装请 **Grafana Alloy** |

### T9.5.2、三条路怎么选

```mermaid
flowchart TB
  subgraph grafana["方案A Grafana 告警"]
    GA[Grafana] -->|查 Loki| LK[(Loki)]
    GA --> AM1[通知渠道]
  end
  subgraph ruler["方案B Loki Ruler"]
    RU[Loki Ruler] -->|LogQL| LK2[(Loki)]
    RU --> AM2[Alertmanager]
  end
  subgraph pipe["方案C 日志造指标"]
    AG["Promtail 或 Alloy"] -->|metrics| PR[Prometheus]
    PR --> AM3[Alertmanager]
  end
```

- **方案 A**：团队已用 **Grafana 10/11**，优先在 **Grafana → Alerting** 建规则，数据源选 **Loki**，不必先改 Loki Helm。详见 [Grafana Alerting](https://grafana.com/docs/grafana/latest/alerting/) 与 Loki 文档中的 [Alerting](https://grafana.com/docs/loki/latest/alerting/) 说明。
- **方案 B**：要把规则**当作代码**进集群、与 Prometheus 规则风格统一，或要 **Ruler API / lokitool 同步**时，用 **Ruler**。配置见 [Configure](https://grafana.com/docs/loki/latest/configure/#ruler) 与下文示例。
- **方案 C**：已有 **Prometheus** 生态，希望**先变成指标再告警**时用；注意 **Promtail** 维护结束，新装请规划 **Alloy**（**T9.4.6**）。

### T9.5.3、方案 A：Grafana 里告警（推荐入门）

1. 确认 **T9.3.7** 已把 **Loki** 加成 Grafana 数据源（`http://loki-gateway.logging.svc.cluster.local`）。  
2. 在 Grafana 中新建告警规则，查询类型选 **Loki**，用 **LogQL** 写条件（例如错误日志条数、关键字出现次数）。  
3. 配置通知渠道（企业微信、邮件、PagerDuty 等）。  

**（插槽：可贴「Alerting → New alert rule → Loki 查询」界面截图。）**

生产上记得：**评估间隔、For 持续时间、无数据/错误状态**与值班流程一致；复杂条件可用多查询与表达式（以当前 Grafana 版本文档为准）。

### T9.5.4、方案 B：Loki Ruler + Alertmanager

**T9.3.5** 里若只有 **`loki.ruler.enable_api: true`**，还不足以把告警送到 Alertmanager，需要补上 **`alertmanager_url`**、**规则存储** 等。请先 **`helm show values grafana-community/loki --version 13.2.0`** 看清本版 Chart 是用 **`loki.ruler`** 还是 **`loki.rulerConfig`**，再合并，避免和旧文 **`loki-stack`** 混用。

下面示例与官方 [Alerting](https://grafana.com/docs/loki/latest/alerting/) 中 ruler 结构一致：请用其**替换** **T9.3.5** 里原先那段只含 `enable_api` 的 **`ruler`**，保持 **`loki:`** 下只有一个 **`ruler`** 块。

```yaml
# 合并进 loki-values.yaml 的 loki: 段（路径按集群改）
loki:
  ruler:
    enable_api: true
    alertmanager_url: http://alertmanager-operated.monitoring.svc:9093
    enable_alertmanager_v2: true
    rule_path: /tmp/loki/scratch
    storage:
      type: local
      local:
        directory: /var/loki/rules
    ring:
      kvstore:
        store: inmemory
```

Alertmanager 的 Service 名、命名空间按你集群实际改（常见 **kube-prometheus-stack** 为 `monitoring`）。应用：

```bash
helm upgrade loki grafana-community/loki -n logging -f loki-values.yaml --version 13.2.0
```

规则内容与 **Prometheus 告警规则** 相同，但 **`expr` 里是 LogQL**。官方示例（节选）：

```yaml
groups:
  - name: loki_demo
    rules:
      - alert: HighErrorRate
        expr: |
          sum(rate({namespace="default", app="nginx-demo"} |= "error" [5m])) by (job)
            /
          sum(rate({namespace="default", app="nginx-demo"}[5m])) by (job)
            > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "nginx error ratio high"
```

把规则文件放到 Ruler 能读到的存储（**local** 时需挂卷或 ConfigMap；生产多用 **S3/GCS** 等，见 [Ruler storage](https://grafana.com/docs/loki/latest/alerting/#ruler-storage)）。规则上线可用 **`lokitool`**（Loki **≥3.1**，见官方 [Interacting with the Ruler](https://grafana.com/docs/loki/latest/alerting/#interacting-with-the-ruler)）。

```mermaid
sequenceDiagram
  participant R as Loki Ruler
  participant L as Loki
  participant A as Alertmanager
  R->>L: 评估 LogQL
  R->>A: 触发告警
```

**（插槽：可贴 Alertmanager UI 中由 Loki 触发的告警条目截图。）**

### T9.5.5、方案 C：日志里造指标再走 Prometheus

思路：**Promtail（或 Alloy）pipeline** 里用 **metrics** 阶段暴露 Counter/Histogram，Prometheus 通过 **ServiceMonitor** 抓取 **`/metrics`**，再用 **PrometheusRule** 告警。`grafana/promtail` Chart 支持 **`serviceMonitor.enabled: true`**，标签要对上 Prometheus Operator 的 **`serviceMonitorSelector`**。

示例片段（**pipeline 以 CRI 解析为准**，不要用已废弃的单独 **`docker: {}`** 硬套；标签 `app` 需与你 workload 一致）：

```yaml
# promtail-values-patch.yaml 片段（与 T9.4.3 合并时对照 helm show values）
serviceMonitor:
  enabled: true
  additionalLabels:
    release: prometheus

config:
  snippets:
    pipelineStages:
      - cri: {}
    extraRelabelConfigs: []
  # 在 snippets.scrapeConfigs / extraScrapeConfigs 中按需挂载下面 pipeline，勿重复抓取同一文件
```

**metrics 阶段**示例（逻辑说明用；键名以 Promtail 版本为准）：

```yaml
pipeline_stages:
  - cri: {}
  - match:
      selector: '{app="nginx-demo"}'
      stages:
        - regex:
            expression: '.*(?P<method>GET|POST) .*'
        - metrics:
            nginx_http_requests:
              type: Counter
              description: "nginx requests"
              source: method
              config:
                action: inc
```

Prometheus 里指标名多为 **`promtail_custom_<name>[_total]`**（Counter 带 `_total`），**PrometheusRule** 的 `expr` 以 **Prometheus UI / Grafana Explore（Metrics）** 里实际 series 为准。

**（插槽：可贴 Prometheus Targets 中 promtail 与自定义指标查询截图。）**

### T9.5.6、联调用小应用（可选）

需要一个固定打日志的 workload 时，可用现代镜像，例如 **`nginx:1.27-alpine`**，**ClusterIP** + `kubectl port-forward` 访问，避免旧文里的 **NodePort + 固定机器 IP**。

```yaml
# nginx-demo.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-demo
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nginx-demo
  template:
    metadata:
      labels:
        app: nginx-demo
    spec:
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports:
            - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: nginx-demo
  namespace: default
spec:
  selector:
    app: nginx-demo
  ports:
    - port: 80
      targetPort: 80
```

```bash
kubectl apply -f nginx-demo.yaml
kubectl port-forward svc/nginx-demo 8080:80
# 另开终端：curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
```

LogQL 里用 **`{app="nginx-demo"}`** 或 **`{namespace="default", app="nginx-demo"}`** 与 **T9.5.4** 示例一致即可。

---

## T9.6、LogQL

**LogQL** 是 Loki 的查询语言，和 PromQL 有渊源，但面向日志。详细语法以官方 [Query Loki](https://grafana.com/docs/loki/latest/query/) 为准；下面只整理**生产里天天会用到的骨架**，避免把参考手册整本贴进来。

### T9.6.1、版本与校验

**本文本节校验日期：2026-04-21**

| 项目 | 说明 |
|------|------|
| Loki | 与 **T9.3** 一致，示例 **3.7.x**（Chart **13.2.0**） |
| 查询入口 | **Grafana Explore**、**LogCLI**（[logcli](https://grafana.com/docs/loki/latest/query/logcli/)）、告警规则里的 **LogQL**（**T9.5**） |
| 必读 | [Log queries](https://grafana.com/docs/loki/latest/query/log_queries/) · [Metric queries](https://grafana.com/docs/loki/latest/query/metric_queries/) |

### T9.6.2、两类查询

```mermaid
flowchart LR
  subgraph logq["日志查询"]
    S1["标签选择器"] --> P1["管道可选"]
    P1 --> OUT["日志行"]
  end
  subgraph met["指标查询"]
    S2["标签选择器"] --> P2["解析与过滤"]
    P2 --> R1["聚合与区间"]
    R1 --> NUM["数值序列"]
  end
```

- **日志查询**：结果还是日志内容，适合排障、检索。  
- **指标查询**：在日志上算 **rate、count_over_time** 等，结果给大盘或告警（**T9.5.4**）。

基本形状（官方写法）：

```logql
{ log stream selector } | log pipeline
```

**流选择器必选**，**管道可选**；Loki **只索引标签和时间**，正文靠管道里过滤和解析（见 [Labels](https://grafana.com/docs/loki/latest/get-started/labels/)）。

### T9.6.3、流选择器（标签）

大括号里用 **`=`、`!=`、`=~`、`!~`** 匹配标签，规则和 Prometheus 类似（见 [Prometheus 标签匹配](https://prometheus.io/docs/prometheus/latest/querying/basics/#instant-vector-selectors)）。

新版本里常有 **`service_name`** 等约定标签，便于和 Grafana 里日志视图联动，具体以你集群里 **Explore → 标签浏览器** 看到的为准。

**（插槽：可贴本环境 Grafana Explore 里标签键列表截图。）**

与 **T9.5.6** 的 **nginx-demo** 对齐的示例：

```logql
{namespace="default", app="nginx-demo"}
```

### T9.6.4、管道：先过滤行，再解析

**行过滤**尽量靠前，少扫数据：

| 运算符 | 含义 |
|--------|------|
| `|=` | 行包含子串 |
| `!=` | 行不包含 |
| `|~` | 行匹配正则（RE2） |
| `!~` | 正则不匹配 |

**解析**：`| json`、`| logfmt`、`| regexp "..."` 等，把字段抽成标签再在管道里用（详见官方 [Log queries](https://grafana.com/docs/loki/latest/query/log_queries/)）。

示例：只看包含 `error` 的行再解析 JSON：

```logql
{namespace="default", app="nginx-demo"} |= "error" | json
```

**格式化展示**（不改变 Loki 里存的数据）：`| line_format "..."`、`| label_format ...`（官方 [Query](https://grafana.com/docs/loki/latest/query/) 页有示例）。

### T9.6.5、指标查询（告警 / 大盘）

在日志选择器 + 管道之后，对**范围区间**做聚合，常见：

- **`rate()`**：每秒条目数  
- **`count_over_time()`**：区间内条数  
- **`sum(... ) by (label)`**：按标签聚合  

示例：按 **pod** 看 5 分钟请求日志条数变化（标签名以你环境为准）：

```logql
sum by (pod) (rate({namespace="default", app="nginx-demo"}[5m]))
```

**错误率**一类写法（与 **T9.5.4** 思路一致，按业务改标签）：

```logql
sum(rate({namespace="default", app="nginx-demo"} |= "error" [5m])) by (job)
  /
sum(rate({namespace="default", app="nginx-demo"}[5m])) by (job)
```

区间写法 **`[5m]`** 与 PromQL 习惯一致；更复杂的函数、**unwrap**、**二进制运算**见 [Metric queries](https://grafana.com/docs/loki/latest/query/metric_queries/)。

### T9.6.6、注释与排障习惯

单行或行尾可用 **`#`** 注释（见官方说明）。  

**生产注意**：

1. **标签越少越准**：选择器越宽，扫的 chunk 越多，查询越慢、越贵。  
2. **高基数不要塞进标签**（采集端就要克制，见 **T9.4**）。  
3. **大时间范围 + 宽选择器** 容易超时，先缩时间窗或加 `|=` 过滤。  
4. 与 **T9.3.7** 数据源 URL 一致时，**Grafana Explore** 里试通后再写进 **T9.5** 告警规则。

```mermaid
flowchart TB
  A["标签尽量收窄"] --> B["行过滤 含子串或正则"]
  B --> C["json 或 logfmt"]
  C --> D["rate 等聚合"]
```

**（插槽：可贴一条本环境完整 LogQL 在 Explore 中的查询结果截图。）**

---


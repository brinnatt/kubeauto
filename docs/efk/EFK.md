# T9、日志

## T9.1、日志架构

监控篇（T4）管的是**指标和告警**：CPU、延迟、错误率这类数，用来盯健康、触发告警。本篇管的是**日志**：谁在什么时间、打印了什么内容，用来还原现场、排查根因。**生产上这两套一般都要上**，但职责不同：**指标**适合按时间聚合、做告警规则；**日志**适合按关键字、上下文检索。选型上也就不同：指标常用 Prometheus 这类时序库，日志常用 ES、OpenSearch、托管日志等。**不要把两类数据混进同一种存储里凑合用**（例如用日志库硬扛全量指标，或把海量原始日志塞进时序库），既贵又难维护，也不是常见做法。

本节对齐 [Kubernetes：Logging Architecture](https://kubernetes.io/docs/concepts/cluster-administration/logging/)，并结合生产习惯写清：**镜像固定 tag、校验日期、可照着 apply 的 YAML**。文中配图来自 **Kubernetes 官网同一套素材**（已下载到本文 `images/`，便于离线阅读）。

### T9.1.1、概念与依据

**应用日志**：看业务行为、排错、审计。**容器里最省事的做法**：写 **stdout / stderr**，由运行时交给 kubelet，再在节点上落盘；平时用 `kubectl logs` 就能看。

**集群级日志（cluster-level logging）**：官方定义是——日志的**存储与生命周期**要和节点、Pod、容器**脱钩**，否则容器崩了、Pod 被赶走、节点挂了，你只依赖本地文件会抓瞎。Kubernetes **不提供**日志存储产品，只提供 API 与节点侧行为；**存哪、查哪、保留多久**由你自己选（EFK、托管日志、Kafka 等）。

**和监控怎么配合**：业务容器可以同时做两件事——一边按 Prometheus 约定暴露 **`/metrics` 端点**（给监控抓），一边往 **stdout/stderr 打日志**（给日志采集收）。**前者不等于后者**：不能指望只靠日志解决告警，也不能只靠指标还原每一行日志原文；两条链路各自部署、各自存储即可。

```mermaid
flowchart LR
  subgraph obs[可观测]
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
  U[kubectl logs / API]
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
> **`image:` 一律固定 tag**；升级前到 **GitHub Releases** 或镜像仓库核对 **latest 稳定版**（非 prerelease），并更新下表与校验日期。  
>
> **本文同步校验日期：2026-04-16**  
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

下面按官方顺序，把三类路径拆成可对照的图 + 可执行的 YAML。**侧重点是「节点代理最常见；Sidecar 有代价；应用直推 Kubernetes 不管」**。

#### T9.1.4.1、节点代理

![使用节点级 logging agent（Kubernetes 官网）](./images/logging-with-node-agent.png)

**做法**：用 **DaemonSet** 跑采集容器（Fluent Bit、fluentd 等），挂载节点上容器日志目录（常见在 `/var/log/pods` 一带，以集群为准），解析后送到 ES、OpenSearch、Kafka、云日志等。

**优点**：每节点**一个**代理，业务零改动。  
**局限**：主要覆盖「标准流落盘」那条链路；应用只写**未共享**的容器内路径时，往往要配合 Sidecar 或改写入方式。

```mermaid
flowchart TB
  subgraph n[工作节点]
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

**场景**：业务写文件，你希望仍走 kubelet 的 stdout 管线，或要把多路日志拆成多个 `kubectl logs` 流。

**做法**：**emptyDir 共享卷**，主容器写文件；**每个文件一个 sidecar**，`tail -F` 跟文件并打到 **该 sidecar 自己的 stdout**。

```mermaid
flowchart LR
  F[应用写文件]
  T[sidecar tail -F]
  O[sidecar stdout]
  F --> T --> O
  O --> K[kubelet/节点代理]
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

**生产注意**：官方也提醒——**文件一份 + 再经 stdout 采集**，节点上占用可能接近**翻倍**；能改应用就优先写 **stdout/stderr**，轮转交给 kubelet 与集群级策略。

---

#### T9.1.4.3、Sidecar 采集器

![Sidecar 内跑 logging agent（Kubernetes 官网）](./images/logging-with-sidecar-agent.png)

**场景**：节点级 DaemonSet 不够灵活（解析规则、租户隔离等），在 Pod 里再跑一个**采集进程**，直接 tail 共享卷里的文件再转发。

**代价**：**每个 Pod 多一份采集**，CPU/内存要算进容量；这类转发**不是**业务容器 stdout，**别指望**用 `kubectl logs` 看到「已经发到后端」的内容（要看采集容器日志或后端）。

官方示例仍是 **fluentd + `google_cloud`**，镜像为 **`registry.k8s.io/fluentd-gcp:1.30`**（旧 `k8s.gcr.io` 已弃用），面向 **GCP**。自建集群建议用 **Fluent Bit**，把 **OUTPUT** 换成你的 ES、Kafka、HTTP 等。下面给 **Fluent Bit 5.x**、**OUTPUT stdout** 的最小可跑通示例（验证管道；上生产只改 `[OUTPUT]`）。

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

与 [官方 fluentd 清单](https://kubernetes.io/docs/concepts/cluster-administration/logging/#sidecar-container-with-a-logging-agent) 对照时，记住：**采集器可替换**，**OUTPUT 必须跟你的后端一致**。

**（插槽：生产环境把 Fluent Bit 的 OUTPUT 换成 OpenSearch/Elasticsearch 等后，可在此处补一张「Kibana/Discover 或后端检索」截图，便于验收。）**

---

#### T9.1.4.4、应用直推

![从应用直接暴露/推送日志（Kubernetes 官网）](./images/logging-from-application.png)

官方说明：从每个应用**直接**把日志推给后端，**已超出 Kubernetes 核心文档范围**——鉴权、重试、背压、字段规范都要你自己定。适合已深度绑定某日志平台的团队，或作为节点采集的补充。

```mermaid
flowchart LR
  APP[应用 Pod] --> SDK[SDK/协议]
  SDK --> BE[(日志后端)]
```

---

下一节 **「T9.2、日志 EFK」** 在集群里落 **Elasticsearch、Kibana** 与 **DaemonSet 采集**（文内以 **Fluentd** 演示）；与本节 **Fluent Bit** 不冲突，同属可观测数据管道，生产可择一或混用。

## T9.2、日志 EFK

前面大家介绍了 Kubernetes 集群中的几种日志收集方案，Kubernetes 中比较流行的日志收集解决方案是 Elasticsearch、Fluentd 和 Kibana（EFK）技术栈，也是官方现在比较推荐的一种方案。

`Elasticsearch` 是一个实时的、分布式的可扩展的搜索引擎，允许进行全文、结构化搜索，它通常用于索引和搜索大量日志数据，也可用于搜索许多不同类型的文档。

Elasticsearch 通常与 `Kibana` 一起部署，Kibana 是 Elasticsearch 的一个功能强大的数据可视化 Dashboard，Kibana 允许你通过 web 界面来浏览 Elasticsearch 日志数据。

`Fluentd`是一个流行的开源数据收集器，我们将在 Kubernetes 集群节点上安装 Fluentd，通过获取容器日志文件、过滤和转换日志数据，然后将数据传递到 Elasticsearch 集群，在该集群中对其进行索引和存储。

我们先来配置启动一个可扩展的 Elasticsearch 集群，然后在 Kubernetes 集群中创建一个 Kibana 应用，最后通过 DaemonSet 来运行 Fluentd，以便它在每个 Kubernetes 工作节点上都可以运行一个 Pod。

> 如果你了解 EFK 的基本原理，只是为了测试可以直接使用 Kubernetes 官方提供的 addon 插件的资源清单，地址：https://github.com/kubernetes/kubernetes/blob/master/cluster/addons/fluentd-elasticsearch/，直接安装即可。

### 安装 Elasticsearch 集群

在创建 Elasticsearch 集群之前，我们先创建一个命名空间，我们将在其中安装所有日志相关的资源对象。

```
kubectl create ns logging
```

#### 环境准备

ElasticSearch 安装有最低安装要求，如果安装后 Pod 无法正常启动，请检查是否符合最低要求的配置，要求如下：

![es 集群要求](https://picdn.youdianzhishi.com/images/20210420112844.png)

这里我们要安装的 ES 集群环境信息如下所示：

![es 集群环境](https://picdn.youdianzhishi.com/images/20210420113128.png)

这里我们使用一个 NFS 类型的 StorageClass 来做持久化存储，当然如果你是线上环境建议使用 Local PV 或者 Ceph RBD 之类的存储来持久化 Elasticsearch 的数据。

此外由于 ElasticSearch 7.x 版本默认安装了 `X-Pack` 插件，并且部分功能免费，需要我们配置一些安全证书文件。

**1、生成证书文件**

```
# 运行容器生成证书
$ docker run --name elastic-certs -i -w /app elasticsearch:7.12.0 /bin/sh -c  \
  "elasticsearch-certutil ca --out /app/elastic-stack-ca.p12 --pass '' && \
    elasticsearch-certutil cert --name security-master --dns \
    security-master --ca /app/elastic-stack-ca.p12 --pass '' --ca-pass '' --out /app/elastic-certificates.p12"
# 从容器中将生成的证书拷贝出来
$ docker cp elastic-certs:/app/elastic-certificates.p12 .
# 删除容器
$ docker rm -f elastic-certs
# 将 pcks12 中的信息分离出来，写入文件
$ openssl pkcs12 -nodes -passin pass:'' -in elastic-certificates.p12 -out elastic-certificate.pem
```

**2、添加证书到 Kubernetes**

```
# 添加证书
$ kubectl create secret -n logging generic elastic-certs --from-file=elastic-certificates.p12
# 设置集群用户名密码
$ kubectl create secret -n logging generic elastic-auth --from-literal=username=elastic --from-literal=password=ydzsio321
```

#### 安装 ES 集群

首先添加 ELastic 的 Helm 仓库：

```
helm repo add elastic https://helm.elastic.co
helm repo update
```

ElaticSearch 安装需要安装三次，分别安装 Master、Data、Client 节点，Master 节点负责集群间的管理工作；Data 节点负责存储数据；Client 节点负责代理 ElasticSearch Cluster 集群，负载均衡。

首先使用 `helm pull` 拉取 Chart 并解压：

```
helm pull elastic/elasticsearch --untar --version 7.12.0
cd elasticsearch
```

在 Chart 目录下面创建用于 Master 节点安装配置的 values 文件：

```
# values-master.yaml
## 设置集群名称
clusterName: "elasticsearch"
## 设置节点名称
nodeGroup: "master"

## 设置角色
roles:
  master: "true"
  ingest: "false"
  data: "false"

# ============镜像配置============
## 指定镜像与镜像版本
image: "elasticsearch"
imageTag: "7.12.0"
## 副本数
replicas: 3

# ============资源配置============
## JVM 配置参数
esJavaOpts: "-Xmx1g -Xms1g"
## 部署资源配置(生成环境一定要设置大些)
resources:
  requests:
    cpu: "2000m"
    memory: "2Gi"
  limits:
    cpu: "2000m"
    memory: "2Gi"
## 数据持久卷配置
persistence:
  enabled: true
## 存储数据大小配置
volumeClaimTemplate:
  storageClassName: nfs-storage
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 5Gi

# ============安全配置============
## 设置协议，可配置为 http、https
protocol: http
## 证书挂载配置，这里我们挂入上面创建的证书
secretMounts:
  - name: elastic-certs
    secretName: elastic-certs
    path: /usr/share/elasticsearch/config/certs

## 允许您在/usr/share/elasticsearch/config/中添加任何自定义配置文件,例如 elasticsearch.yml
## ElasticSearch 7.x 默认安装了 x-pack 插件，部分功能免费，这里我们配置下
## 下面注掉的部分为配置 https 证书，配置此部分还需要配置 helm 参数 protocol 值改为 https
esConfig:
  elasticsearch.yml: |
    xpack.security.enabled: true
    xpack.security.transport.ssl.enabled: true
    xpack.security.transport.ssl.verification_mode: certificate
    xpack.security.transport.ssl.keystore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    xpack.security.transport.ssl.truststore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    # xpack.security.http.ssl.enabled: true
    # xpack.security.http.ssl.truststore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    # xpack.security.http.ssl.keystore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
## 环境变量配置，这里引入上面设置的用户名、密码 secret 文件
extraEnvs:
  - name: ELASTIC_USERNAME
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: username
  - name: ELASTIC_PASSWORD
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: password

# ============调度配置============
## 设置调度策略
## - hard：只有当有足够的节点时 Pod 才会被调度，并且它们永远不会出现在同一个节点上
## - soft：尽最大努力调度
antiAffinity: "soft"
tolerations:
  - operator: "Exists" ##容忍全部污点
```

然后创建用于 Data 节点安装的 values 文件：

```
# values-data.yaml
# ============设置集群名称============
## 设置集群名称
clusterName: "elasticsearch"
## 设置节点名称
nodeGroup: "data"
## 设置角色
roles:
  master: "false"
  ingest: "true"
  data: "true"

# ============镜像配置============
## 指定镜像与镜像版本
image: "elasticsearch"
imageTag: "7.12.0"
## 副本数(建议设置为3，我这里资源不足只用了1个副本)
replicas: 1

# ============资源配置============
## JVM 配置参数
esJavaOpts: "-Xmx1g -Xms1g"
## 部署资源配置(生成环境一定要设置大些)
resources:
  requests:
    cpu: "1000m"
    memory: "2Gi"
  limits:
    cpu: "1000m"
    memory: "2Gi"
## 数据持久卷配置
persistence:
  enabled: true
## 存储数据大小配置
volumeClaimTemplate:
  storageClassName: nfs-storage
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 10Gi

# ============安全配置============
## 设置协议，可配置为 http、https
protocol: http
## 证书挂载配置，这里我们挂入上面创建的证书
secretMounts:
  - name: elastic-certs
    secretName: elastic-certs
    path: /usr/share/elasticsearch/config/certs
## 允许您在/usr/share/elasticsearch/config/中添加任何自定义配置文件,例如 elasticsearch.yml
## ElasticSearch 7.x 默认安装了 x-pack 插件，部分功能免费，这里我们配置下
## 下面注掉的部分为配置 https 证书，配置此部分还需要配置 helm 参数 protocol 值改为 https
esConfig:
  elasticsearch.yml: |
    xpack.security.enabled: true
    xpack.security.transport.ssl.enabled: true
    xpack.security.transport.ssl.verification_mode: certificate
    xpack.security.transport.ssl.keystore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    xpack.security.transport.ssl.truststore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    # xpack.security.http.ssl.enabled: true
    # xpack.security.http.ssl.truststore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    # xpack.security.http.ssl.keystore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
## 环境变量配置，这里引入上面设置的用户名、密码 secret 文件
extraEnvs:
  - name: ELASTIC_USERNAME
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: username
  - name: ELASTIC_PASSWORD
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: password

# ============调度配置============
## 设置调度策略
## - hard：只有当有足够的节点时 Pod 才会被调度，并且它们永远不会出现在同一个节点上
## - soft：尽最大努力调度
antiAffinity: "soft"
## 容忍配置
tolerations:
  - operator: "Exists" ##容忍全部污点
```

最后一个是用于创建 Client 节点的 values 文件：

```
# values-client.yaml
# ============设置集群名称============
## 设置集群名称
clusterName: "elasticsearch"
## 设置节点名称
nodeGroup: "client"
## 设置角色
roles:
  master: "false"
  ingest: "false"
  data: "false"

# ============镜像配置============
## 指定镜像与镜像版本
image: "elasticsearch"
imageTag: "7.12.0"
## 副本数
replicas: 1

# ============资源配置============
## JVM 配置参数
esJavaOpts: "-Xmx1g -Xms1g"
## 部署资源配置(生成环境一定要设置大些)
resources:
  requests:
    cpu: "1000m"
    memory: "2Gi"
  limits:
    cpu: "1000m"
    memory: "2Gi"
## 数据持久卷配置
persistence:
  enabled: false

# ============安全配置============
## 设置协议，可配置为 http、https
protocol: http
## 证书挂载配置，这里我们挂入上面创建的证书
secretMounts:
  - name: elastic-certs
    secretName: elastic-certs
    path: /usr/share/elasticsearch/config/certs
## 允许您在/usr/share/elasticsearch/config/中添加任何自定义配置文件,例如 elasticsearch.yml
## ElasticSearch 7.x 默认安装了 x-pack 插件，部分功能免费，这里我们配置下
## 下面注掉的部分为配置 https 证书，配置此部分还需要配置 helm 参数 protocol 值改为 https
esConfig:
  elasticsearch.yml: |
    xpack.security.enabled: true
    xpack.security.transport.ssl.enabled: true
    xpack.security.transport.ssl.verification_mode: certificate
    xpack.security.transport.ssl.keystore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    xpack.security.transport.ssl.truststore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    # xpack.security.http.ssl.enabled: true
    # xpack.security.http.ssl.truststore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
    # xpack.security.http.ssl.keystore.path: /usr/share/elasticsearch/config/certs/elastic-certificates.p12
## 环境变量配置，这里引入上面设置的用户名、密码 secret 文件
extraEnvs:
  - name: ELASTIC_USERNAME
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: username
  - name: ELASTIC_PASSWORD
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: password

# ============Service 配置============
service:
  type: NodePort
  nodePort: "30200"
```

现在用上面的 values 文件来安装：

```
# 安装 master 节点
helm install es-master -f values-master.yaml --namespace logging .
# 安装 data 节点
helm install es-data -f values-data.yaml --namespace logging .
# 安装 client 节点
helm install es-client -f values-client.yaml --namespace logging .
```

> 在安装 Master 节点后 Pod 启动时候会抛出异常，就绪探针探活失败，这是个正常现象。在执行安装 Data 节点后 Master 节点 Pod 就会恢复正常。

#### 安装 Kibana

Elasticsearch 集群安装完成后接下来配置安装 Kibana

使用 `helm pull` 命令拉取 Kibana Chart 包并解压：

```
helm pull elastic/kibana --untar --version 7.12.0
cd kibana
```

创建用于安装 Kibana 的 values 文件：

```
# values-prod.yaml
## 指定镜像与镜像版本
image: "kibana"
imageTag: "7.12.0"

## 配置 ElasticSearch 地址
elasticsearchHosts: "http://elasticsearch-client:9200"

# ============环境变量配置============
## 环境变量配置，这里引入上面设置的用户名、密码 secret 文件
extraEnvs:
  - name: "ELASTICSEARCH_USERNAME"
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: username
  - name: "ELASTICSEARCH_PASSWORD"
    valueFrom:
      secretKeyRef:
        name: elastic-auth
        key: password

# ============资源配置============
resources:
  requests:
    cpu: "500m"
    memory: "1Gi"
  limits:
    cpu: "500m"
    memory: "1Gi"

# ============配置 Kibana 参数============
## kibana 配置中添加语言配置，设置 kibana 为中文
kibanaConfig:
  kibana.yml: |
    i18n.locale: "zh-CN"

# ============Service 配置============
service:
  type: NodePort
  nodePort: "30601"
```

使用上面的配置直接安装即可：

```
helm install kibana -f values-prod.yaml --namespace logging .
```

下面是安装完成后的 ES 集群和 Kibana 资源：

```
[root@node2 ~]# kubectl get pods -n logging
NAME                            READY   STATUS              RESTARTS   AGE
elasticsearch-client-0          1/1     Running             0          13m
elasticsearch-data-0            1/1     Running             0          17m
elasticsearch-master-0          1/1     Running             0          14m
elasticsearch-master-1          1/1     Running             0          16m
elasticsearch-master-2          1/1     Running             0          18m
kibana-kibana-66f97964b-pmqlq   1/1     Running             0          31s
[root@node2 ~]# kubectl get svc -n logging
NAME                            TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)                         AGE
elasticsearch-client            NodePort    10.102.35.207   <none>        9200:30200/TCP,9300:30078/TCP   33m
elasticsearch-client-headless   ClusterIP   None            <none>        9200/TCP,9300/TCP               33m
elasticsearch-data              ClusterIP   10.97.179.233   <none>        9200/TCP,9300/TCP               37m
elasticsearch-data-headless     ClusterIP   None            <none>        9200/TCP,9300/TCP               37m
elasticsearch-master            ClusterIP   10.97.35.120    <none>        9200/TCP,9300/TCP               46m
elasticsearch-master-headless   ClusterIP   None            <none>        9200/TCP,9300/TCP               46m
kibana-kibana                   NodePort    10.106.97.8     <none>        5601:30601/TCP                  35s
```

上面我们安装 Kibana 的时候指定了 30601 的 NodePort 端口，所以我们可以从任意节点 `http://IP:30601` 来访问 Kibana。

![Kibana 登录页面](https://picdn.youdianzhishi.com/images/20210420173518.png)

我们可以看到会跳转到登录页面，让我们输出用户名、密码，这里我们输入上面配置的用户名 elastic、密码 ydzsio321 进行登录。登录成功后进入如下所示的 Kibana 主页：

![Kibana 主页面](https://picdn.youdianzhishi.com/images/20210420173726.png)

### 部署 Fluentd

`Fluentd` 是一个高效的日志聚合器，是用 Ruby 编写的，并且可以很好地扩展。对于大部分企业来说，Fluentd 足够高效并且消耗的资源相对较少，另外一个工具`Fluent-bit`更轻量级，占用资源更少，但是插件相对 Fluentd 来说不够丰富，所以整体来说，Fluentd 更加成熟，使用更加广泛，所以我们这里也同样使用 Fluentd 来作为日志收集工具。

#### 工作原理

Fluentd 通过一组给定的数据源抓取日志数据，处理后（转换成结构化的数据格式）将它们转发给其他服务，比如 Elasticsearch、对象存储等等。Fluentd 支持超过 300 个日志存储和分析服务，所以在这方面是非常灵活的。主要运行步骤如下：

- 首先 Fluentd 从多个日志源获取数据
- 结构化并且标记这些数据
- 然后根据匹配的标签将数据发送到多个目标服务去

![fluentd 架构](https://picdn.youdianzhishi.com/images/7moPNc.jpg)

#### 配置

一般来说我们是通过一个配置文件来告诉 Fluentd 如何采集、处理数据的，下面简单和大家介绍下 Fluentd 的配置方法。

##### 日志源配置

比如我们这里为了收集 Kubernetes 节点上的所有容器日志，就需要做如下的日志源配置：

```
<source>
  @id fluentd-containers.log
  @type tail                             # Fluentd 内置的输入方式，其原理是不停地从源文件中获取新的日志。
  path /var/log/containers/*.log         # 挂载的服务器Docker容器日志地址
  pos_file /var/log/es-containers.log.pos
  tag raw.kubernetes.*                   # 设置日志标签
  read_from_head true
  <parse>                                # 多行格式化成JSON
    @type multi_format                   # 使用 multi-format-parser 解析器插件
    <pattern>
      format json                        # JSON 解析器
      time_key time                      # 指定事件时间的时间字段
      time_format %Y-%m-%dT%H:%M:%S.%NZ  # 时间格式
    </pattern>
    <pattern>
      format /^(?<time>.+) (?<stream>stdout|stderr) [^ ]* (?<log>.*)$/
      time_format %Y-%m-%dT%H:%M:%S.%N%:z
    </pattern>
  </parse>
</source>
```

上面配置部分参数说明如下：

- id：表示引用该日志源的唯一标识符，该标识可用于进一步过滤和路由结构化日志数据
- type：Fluentd 内置的指令，`tail` 表示 Fluentd 从上次读取的位置通过 tail 不断获取数据，另外一个是 `http` 表示通过一个 GET 请求来收集数据。
- path：`tail` 类型下的特定参数，告诉 Fluentd 采集 `/var/log/containers` 目录下的所有日志，这是 docker 在 Kubernetes 节点上用来存储运行容器 stdout 输出日志数据的目录。
- pos_file：检查点，如果 Fluentd 程序重新启动了，它将使用此文件中的位置来恢复日志数据收集。
- tag：用来将日志源与目标或者过滤器匹配的自定义字符串，Fluentd 匹配源/目标标签来路由日志数据。

##### 路由配置

上面是日志源的配置，接下来看看如何将日志数据发送到 Elasticsearch：

```
<match **>
  @id elasticsearch
  @type elasticsearch
  @log_level info
  include_tag_key true
  type_name fluentd
  host "#{ENV['OUTPUT_HOST']}"
  port "#{ENV['OUTPUT_PORT']}"
  logstash_format true
  <buffer>
    @type file
    path /var/log/fluentd-buffers/kubernetes.system.buffer
    flush_mode interval
    retry_type exponential_backoff
    flush_thread_count 2
    flush_interval 5s
    retry_forever
    retry_max_interval 30
    chunk_limit_size "#{ENV['OUTPUT_BUFFER_CHUNK_LIMIT']}"
    queue_limit_length "#{ENV['OUTPUT_BUFFER_QUEUE_LIMIT']}"
    overflow_action block
  </buffer>
</match>
```

- match：标识一个目标标签，后面是一个匹配日志源的正则表达式，我们这里想要捕获所有的日志并将它们发送给 Elasticsearch，所以需要配置成`**`。
- id：目标的一个唯一标识符。
- type：支持的输出插件标识符，我们这里要输出到 Elasticsearch，所以配置成 elasticsearch，这是 Fluentd 的一个内置插件。
- log_level：指定要捕获的日志级别，我们这里配置成 `info`，表示任何该级别或者该级别以上（INFO、WARNING、ERROR）的日志都将被路由到 Elsasticsearch。
- host/port：定义 Elasticsearch 的地址，也可以配置认证信息，我们的 Elasticsearch 不需要认证，所以这里直接指定 host 和 port 即可。
- logstash_format：Elasticsearch 服务对日志数据构建反向索引进行搜索，将 logstash_format 设置为 `true`，Fluentd 将会以 logstash 格式来转发结构化的日志数据。
- Buffer： Fluentd 允许在目标不可用时进行缓存，比如，如果网络出现故障或者 Elasticsearch 不可用的时候。缓冲区配置也有助于降低磁盘的 IO。

##### 过滤

由于 Kubernetes 集群中应用太多，也还有很多历史数据，所以我们可以只将某些应用的日志进行收集，比如我们只采集具有 `logging=true` 这个 Label 标签的 Pod 日志，这个时候就需要使用 filter，如下所示：

```
# 删除无用的属性
<filter kubernetes.**>
  @type record_transformer
  remove_keys $.docker.container_id,$.kubernetes.container_image_id,$.kubernetes.pod_id,$.kubernetes.namespace_id,$.kubernetes.master_url,$.kubernetes.labels.pod-template-hash
</filter>
# 只保留具有logging=true标签的Pod日志
<filter kubernetes.**>
  @id filter_log
  @type grep
  <regexp>
    key $.kubernetes.labels.logging
    pattern ^true$
  </regexp>
</filter>
```

#### 安装

要收集 Kubernetes 集群的日志，直接用 DasemonSet 控制器来部署 Fluentd 应用，这样，它就可以从 Kubernetes 节点上采集日志，确保在集群中的每个节点上始终运行一个 Fluentd 容器。当然可以直接使用 Helm 来进行一键安装，为了能够了解更多实现细节，我们这里还是采用手动方法来进行安装。

首先，我们通过 ConfigMap 对象来指定 Fluentd 配置文件，新建 fluentd-configmap.yaml 文件，文件内容如下：

```
kind: ConfigMap
apiVersion: v1
metadata:
  name: fluentd-conf
  namespace: logging
data:
  # 容器日志
  containers.input.conf: |-
    <source>
      @id fluentd-containers.log
      @type tail                              # Fluentd 内置的输入方式，其原理是不停地从源文件中获取新的日志
      path /var/log/containers/*.log          # Docker 容器日志路径
      pos_file /var/log/es-containers.log.pos  # 记录读取的位置
      tag raw.kubernetes.*                    # 设置日志标签
      read_from_head true                     # 从头读取
      <parse>                                 # 多行格式化成JSON
        # 可以使用我们介绍过的 multiline 插件实现多行日志
        @type multi_format                    # 使用 multi-format-parser 解析器插件
        <pattern>
          format json                         # JSON解析器
          time_key time                       # 指定事件时间的时间字段
          time_format %Y-%m-%dT%H:%M:%S.%NZ   # 时间格式
        </pattern>
        <pattern>
          format /^(?<time>.+) (?<stream>stdout|stderr) [^ ]* (?<log>.*)$/
          time_format %Y-%m-%dT%H:%M:%S.%N%:z
        </pattern>
      </parse>
    </source>

    # 在日志输出中检测异常(多行日志)，并将其作为一条日志转发
    # https://github.com/GoogleCloudPlatform/fluent-plugin-detect-exceptions
    <match raw.kubernetes.**>           # 匹配tag为raw.kubernetes.**日志信息
      @id raw.kubernetes
      @type detect_exceptions           # 使用detect-exceptions插件处理异常栈信息
      remove_tag_prefix raw             # 移除 raw 前缀
      message log
      multiline_flush_interval 5
    </match>

    <filter **>  # 拼接日志
      @id filter_concat
      @type concat                # Fluentd Filter 插件，用于连接多个日志中分隔的多行日志
      key message
      multiline_end_regexp /\n$/  # 以换行符“\n”拼接
      separator ""
    </filter>

    # 添加 Kubernetes metadata 数据
    <filter kubernetes.**>
      @id filter_kubernetes_metadata
      @type kubernetes_metadata
    </filter>

    # 修复 ES 中的 JSON 字段
    # 插件地址：https://github.com/repeatedly/fluent-plugin-multi-format-parser
    <filter kubernetes.**>
      @id filter_parser
      @type parser                # multi-format-parser多格式解析器插件
      key_name log                # 在要解析的日志中指定字段名称
      reserve_data true           # 在解析结果中保留原始键值对
      remove_key_name_field true  # key_name 解析成功后删除字段
      <parse>
        @type multi_format
        <pattern>
          format json
        </pattern>
        <pattern>
          format none
        </pattern>
      </parse>
    </filter>

    # 删除一些多余的属性
    <filter kubernetes.**>
      @type record_transformer
      remove_keys $.docker.container_id,$.kubernetes.container_image_id,$.kubernetes.pod_id,$.kubernetes.namespace_id,$.kubernetes.master_url,$.kubernetes.labels.pod-template-hash
    </filter>

    # 只保留具有logging=true标签的Pod日志
    <filter kubernetes.**>
      @id filter_log
      @type grep
      <regexp>
        key $.kubernetes.labels.logging
        pattern ^true$
      </regexp>
    </filter>

  ###### 监听配置，一般用于日志聚合用 ######
  forward.input.conf: |-
    # 监听通过TCP发送的消息
    <source>
      @id forward
      @type forward
    </source>

  output.conf: |-
    <match **>
      @id elasticsearch
      @type elasticsearch
      @log_level info
      include_tag_key true
      host elasticsearch-client
      port 9200
      user elastic # FLUENT_ELASTICSEARCH_USER | FLUENT_ELASTICSEARCH_PASSWORD
      password ydzsio321
      logstash_format true
      logstash_prefix k8s
      request_timeout 30s
      <buffer>
        @type file
        path /var/log/fluentd-buffers/kubernetes.system.buffer
        flush_mode interval
        retry_type exponential_backoff
        flush_thread_count 2
        flush_interval 5s
        retry_forever
        retry_max_interval 30
        chunk_limit_size 2M
        queue_limit_length 8
        overflow_action block
      </buffer>
    </match>
```

上面配置文件中我们只配置了 docker 容器日志目录，收集到数据经过处理后发送到 `elasticsearch-client:9200` 服务。

然后新建一个 fluentd-daemonset.yaml 的文件，文件内容如下：

```
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fluentd-es
  namespace: logging
  labels:
    k8s-app: fluentd-es
    kubernetes.io/cluster-service: "true"
    addonmanager.kubernetes.io/mode: Reconcile
---
kind: ClusterRole
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: fluentd-es
  labels:
    k8s-app: fluentd-es
    kubernetes.io/cluster-service: "true"
    addonmanager.kubernetes.io/mode: Reconcile
rules:
  - apiGroups:
      - ""
    resources:
      - "namespaces"
      - "pods"
    verbs:
      - "get"
      - "watch"
      - "list"
---
kind: ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: fluentd-es
  labels:
    k8s-app: fluentd-es
    kubernetes.io/cluster-service: "true"
    addonmanager.kubernetes.io/mode: Reconcile
subjects:
  - kind: ServiceAccount
    name: fluentd-es
    namespace: logging
    apiGroup: ""
roleRef:
  kind: ClusterRole
  name: fluentd-es
  apiGroup: ""
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluentd
  namespace: logging
  labels:
    app: fluentd
    kubernetes.io/cluster-service: "true"
spec:
  selector:
    matchLabels:
      app: fluentd
  template:
    metadata:
      labels:
        app: fluentd
        kubernetes.io/cluster-service: "true"
    spec:
      tolerations:
        - key: node-role.kubernetes.io/master
          effect: NoSchedule
      serviceAccountName: fluentd-es
      containers:
        - name: fluentd
          image: quay.io/fluentd_elasticsearch/fluentd:v3.2.0
          volumeMounts:
            - name: fluentconfig
              mountPath: /etc/fluent/config.d
            - name: varlog
              mountPath: /var/log
            - name: varlibdockercontainers
              mountPath: /var/lib/docker/containers
              readOnly: true
      terminationGracePeriodSeconds: 30
      volumes:
        - name: fluentconfig
          configMap:
            name: fluentd-conf
        - name: varlog
          hostPath:
            path: /var/log
        - name: varlibdockercontainers
          hostPath:
            path: /var/lib/docker/containers
```

我们将上面创建的 fluentd-config 这个 ConfigMap 对象通过 volumes 挂载到了 Fluentd 容器中，另外为了能够灵活控制哪些节点的日志可以被收集，所以我们这里还添加了一个 nodSelector 属性：

```
nodeSelector:
  beta.kubernetes.io/fluentd-ds-ready: "true"
```

意思就是要想采集节点的日志，那么我们就需要给节点打上上面的标签。

提示

如果你需要在其他节点上采集日志，则需要给对应节点打上标签，使用如下命令：`kubectl label nodes node名 beta.kubernetes.io/fluentd-ds-ready=true`。

另外由于我们的集群使用的是 kubeadm 搭建的，默认情况下 master 节点有污点，所以如果要想也收集 master 节点的日志，则需要添加上容忍：

```
tolerations:
  - operator: Exists
```

> 另外需要注意的地方是，如果更改了 docker 的根目录，则在 volumes 和 volumeMount 里面都需要更改，保持一致。

分别创建上面的 ConfigMap 对象和 DaemonSet：

```
$ kubectl create -f fluentd-configmap.yaml
configmap "fluentd-conf" created
$ kubectl create -f fluentd-daemonset.yaml
serviceaccount "fluentd-es" created
clusterrole.rbac.authorization.k8s.io "fluentd-es" created
clusterrolebinding.rbac.authorization.k8s.io "fluentd-es" created
daemonset.apps "fluentd" created
```

创建完成后，查看对应的 Pods 列表，检查是否部署成功：

```
$ kubectl get pods -n logging
NAME                            READY   STATUS    RESTARTS   AGE
elasticsearch-client-0          1/1     Running   0          64m
elasticsearch-data-0            1/1     Running   0          65m
elasticsearch-master-0          1/1     Running   0          73m
fluentd-5rqbq                   1/1     Running   0          60m
fluentd-l6mgf                   1/1     Running   0          60m
fluentd-xmfpg                   1/1     Running   0          60m
kibana-kibana-66f97964b-mdspc   1/1     Running   0          63m
```

Fluentd 启动成功后，这个时候就可以发送日志到 ES 了，但是我们这里是过滤了只采集具有 `logging=true` 标签的 Pod 日志，所以现在还没有任何数据会被采集。

下面我们部署一个简单的测试应用， 新建 counter.yaml 文件，文件内容如下：

```
apiVersion: v1
kind: Pod
metadata:
  name: counter
  labels:
    logging: "true" # 一定要具有该标签才会被采集
spec:
  containers:
    - name: count
      image: busybox
      args:
        [
          /bin/sh,
          -c,
          'i=0; while true; do echo "$i: $(date)"; i=$((i+1)); sleep 1; done',
        ]
```

该 Pod 只是简单将日志信息打印到 `stdout`，所以正常来说 Fluentd 会收集到这个日志数据，在 Kibana 中也就可以找到对应的日志数据了，使用 kubectl 工具创建该 Pod：

```
$ kubectl create -f counter.yaml
$ kubectl get pods
NAME                             READY   STATUS    RESTARTS   AGE
counter                          1/1     Running   0          9h
```

Pod 创建并运行后，回到 Kibana Dashboard 页面，点击左侧最下面的 `Management` -> `Stack Management`，进入管理页面，点击左侧 `Kibana` 下面的 `索引模式`，点击 `创建索引模式` 开始导入索引数据：

![create index](https://picdn.youdianzhishi.com/images/20210424172229.png)

在这里可以配置我们需要的 Elasticsearch 索引，前面 Fluentd 配置文件中我们采集的日志使用的是 logstash 格式，定义了一个 `k8s` 的前缀，所以这里只需要在文本框中输入 `k8s-*` 即可匹配到 Elasticsearch 集群中采集的 Kubernetes 集群日志数据，然后点击下一步，进入以下页面：

![index config](https://picdn.youdianzhishi.com/images/20210424172356.png)

在该页面中配置使用哪个字段按时间过滤日志数据，在下拉列表中，选择`@timestamp`字段，然后点击 `创建索引模式`，创建完成后，点击左侧导航菜单中的 `Discover`，然后就可以看到一些直方图和最近采集到的日志数据了：

![log data](https://picdn.youdianzhishi.com/images/20210424172654.png)

现在的数据就是上面 Counter 应用的日志，如果还有其他的应用，我们也可以筛选过滤：

![counter log data](https://picdn.youdianzhishi.com/images/20210424180009.png)

我们也可以通过其他元数据来过滤日志数据，比如您可以单击任何日志条目以查看其他元数据，如容器名称，Kubernetes 节点，命名空间等。

#### 日志分析

上面我们已经可以将应用日志收集起来了，下面我们来使用一个应用演示如何分析采集的日志。示例应用会输出如下所示的 JSON 格式的日志信息：

```
{"LOGLEVEL":"WARNING","serviceName":"msg-processor","serviceEnvironment":"staging","message":"WARNING client connection terminated unexpectedly."}
{"LOGLEVEL":"INFO","serviceName":"msg-processor","serviceEnvironment":"staging","message":"","eventsNumber":5}
{"LOGLEVEL":"INFO","serviceName":"msg-receiver-api":"msg-receiver-api","serviceEnvironment":"staging","volume":14,"message":"API received messages"}
{"LOGLEVEL":"ERROR","serviceName":"msg-receiver-api","serviceEnvironment":"staging","message":"ERROR Unable to upload files for processing"}
```

因为 JSON 格式的日志解析非常容易，当我们将日志结构化传输到 ES 过后，我们可以根据特定的字段值而不是文本搜索日志数据，当然纯文本格式的日志我们也可以进行结构化，但是这样每个应用的日志格式不统一，都需要单独进行结构化，非常麻烦，所以建议将日志格式统一成 JSON 格式输出。

我们这里的示例应用会定期输出不同类型的日志消息，包含不同日志级别（INFO/WARN/ERROR）的日志，一行 JSON 日志就是我们收集的一条日志消息，该消息通过 fluentd 进行采集发送到 Elasticsearch。这里我们会使用到 fluentd 里面的自动 JSON 解析插件，默认情况下，fluentd 会将每个日志文件的一行作为名为 `log` 的字段进行发送，并自动添加其他字段，比如 `tag` 标识容器，`stream` 标识 stdout 或者 stderr。

由于在 fluentd 配置中我们添加了如下所示的过滤器：

```
<filter kubernetes.**>
  @id filter_parser
  @type parser                # multi-format-parser多格式解析器插件
  key_name log                # 在要解析的记录中指定字段名称
  reserve_data true           # 在解析结果中保留原始键值对
  remove_key_name_field true  # key_name 解析成功后删除字段。
  <parse>
    @type multi_format
    <pattern>
      format json
    </pattern>
    <pattern>
      format none
    </pattern>
  </parse>
</filter>
```

该过滤器使用 `json` 和 `none` 两个插件将 JSON 数据进行结构化，这样就会把 JSON 日志里面的属性解析成一个一个的字段，解析生效过后记得刷新 Kibana 的索引字段，否则会识别不了这些字段，通过 `管理` -> `索引模式` 点击刷新字段列表即可。

下面我们将示例应用部署到 Kubernetes 集群中：(dummylogs.yaml)

```
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dummylogs
spec:
  replicas: 3
  selector:
    matchLabels:
      app: dummylogs
  template:
    metadata:
      labels:
        app: dummylogs
        logging: "true" # 要采集日志需要加上该标签
    spec:
      containers:
        - name: dummy
          image: cnych/dummylogs:latest
          args:
            - msg-processor
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dummylogs2
spec:
  replicas: 3
  selector:
    matchLabels:
      app: dummylogs2
  template:
    metadata:
      labels:
        app: dummylogs2
        logging: "true" # 要采集日志需要加上该标签
    spec:
      containers:
        - name: dummy
          image: cnych/dummylogs:latest
          args:
            - msg-receiver-api
```

直接部署上面的应用即可：

```
$ kubectl apply -f dummylogs.yaml
$ kubectl get pods -l logging=true
NAME                         READY   STATUS    RESTARTS   AGE
counter                      1/1     Running   0          22h
dummylogs-6f7b56579d-7js8n   1/1     Running   5          15h
dummylogs-6f7b56579d-wdnc6   1/1     Running   5          15h
dummylogs-6f7b56579d-x4twn   1/1     Running   5          15h
dummylogs2-d9b978d9b-bchks   1/1     Running   5          15h
dummylogs2-d9b978d9b-wv7rj   1/1     Running   5          15h
dummylogs2-d9b978d9b-z2r26   1/1     Running   5          15h
```

部署完成后 dummylogs 和 dummylogs2 两个应用就会开始输出不同级别的日志信息了，记得要给应用所在的节点打上 `beta.kubernetes.io/fluentd-ds-ready=true` 的标签，否则 fluentd 不会在对应的节点上运行也就不会收集日志了。正常情况下日志就已经可以被采集到 Elasticsearch 当中了，我们可以前往 Kibana 的 Dashboard 页面查看:

![img](https://picdn.youdianzhishi.com/images/20200428092342.png)

我们可以看到可用的字段中已经包含我们应用中的一些字段了。找到 `serviceName` 字段点击我们可以查看已经采集了哪些服务的消息：

![img](https://picdn.youdianzhishi.com/images/20200428092559.png)

可以看到我们收到了来自 `msg-processor` 和 `msg-receiver-api` 的日志信息，在最近 15 分钟之内，`api` 服务产生的日志更多，点击后面的加号就可以只过滤该服务的日志数据：

![img](https://picdn.youdianzhishi.com/images/20200428092903.png)

我们可以看到展示的日志数据的属性比较多，有时候可能不利于我们查看日志，此时我们可以筛选想要展示的字段:

![img](https://picdn.youdianzhishi.com/images/20200428093202.png)

我们可以根据自己的需求选择要显示的字段，现在查看消息的时候就根据清楚了：

![img](https://picdn.youdianzhishi.com/images/20200428093343.png)

比如为了能够更加清晰的展示我们采集的日志数据，还可以将 `eventsNumber` 和 `serviceName` 字段选中添加：

![img](https://picdn.youdianzhishi.com/images/20200428093646.png)

然后同样我们可以根据自己的需求来筛选需要查看的日志数据：

![img](https://picdn.youdianzhishi.com/images/20200428093815.png)

如果你的 Elasticsearch 的查询语句比较熟悉的话，使用查询语句能实现的筛选功能更加强大，比如我们要查询 `mgs-processor` 和 `msg-receiver-api` 两个服务的日志，则可以使用如下所示的查询语句：

```
serviceName:msg-processor OR serviceName:msg-receiver-api
```

直接搜索框中输入上面的查询语句进行查询即可：

![img](https://picdn.youdianzhishi.com/images/20200428094158.png)

接下来我们来创建一个图表来展示已经处理了多少 `msg-processor` 服务的日志信息。在 Kibana 中切换到 `Visualize` 页面，点击 `Create new visualization` 按钮选择 `Area`，选择 `k8s-*` 的索引，首先配置 Y 轴的数据，这里我们使用 `eventsNumber` 字段的 `Sum` 函数进行聚合：

![img](https://picdn.youdianzhishi.com/images/20200428095222.png)

然后配置 X 轴数据使用 `Date Histogram` 类型的 `@timestamp` 字段：

![img](https://picdn.youdianzhishi.com/images/20200428095344.png)

配置完成后点击右上角的 `Apply Changes` 按钮则就会在右侧展示出对应的图表信息：

![img](https://picdn.youdianzhishi.com/images/20200428095631.png)

这个图表展示的就是最近 15 分钟内被处理的事件总数，当然我们也可以自己选择时间范围。我们还可以将 `msg-receiver-api` 事件的数量和已处理的消息总数进行关联，在该图表上添加另外一层数据，在 Y 轴上添加一个新指标，选择 `Add metrics` 和 `Y-axis`，然后同样选择 `sum` 聚合器，使用 `volume` 字段：

![img](https://picdn.youdianzhishi.com/images/20200428100341.png)

点击 `Apply Changes` 按钮就可以同时显示两个服务事件的数据了。最后点击顶部的 `save` 来保存该图表，并为其添加一个名称。

在实际的应用中，我们可能对应用的错误日志更加关心，需要了解应用的运行情况，所以对于错误或者警告级别的日志进行统计也是非常有必要的。现在我们回到 `Discover` 页面，输入 `LOGLEVEL:ERROR OR LOGLEVEL:WARNING` 查询语句来过滤所有的错误和告警日志：

![img](https://picdn.youdianzhishi.com/images/20200428101527.png)

错误日志相对较少，实际上我们这里的示例应用会每 15-20 分钟左右就会抛出 4 个错误信息，其余都是警告信息。同样现在我们还是用可视化的图表来展示下错误日志的情况。

同样切换到 `Visualize` 页面，点击 `Create visualization`，选择 `Vertical Bar`，然后选中 `k8s-*` 的 Index Pattern。

![img](https://picdn.youdianzhishi.com/images/20200428102104.png)

现在我们忽略 Y 轴，使用默认的 `Count` 设置来显示消息数量。首先点击 `Buckets` 下面的 `X-axis`，然后同样选择 `Date histogram`，然后点击下方的 `Add`，添加 `Sub-Bueckt`，选择 `Split series`:

![img](https://picdn.youdianzhishi.com/images/20200428102530.png)

然后我们可以通过指定的字段来分割条形图，选择 `Terms` 作为子聚合方式，然后选择 `serviceName.keyword` 字段，最后点击 `apply` 生成图表：

![img](https://picdn.youdianzhishi.com/images/20200428102913.png)

现在上面的图表以不同的颜色来显示每个服务消息，接下来我们在搜索框中输入要查找的内容，因为现在的图表是每个服务的所有消息计数，包括正常和错误的日志，我们要过滤告警和错误的日志，同样输入 `LOGLEVEL:ERROR OR LOGLEVEL:WARNING` 查询语句进行搜索即可：

![img](https://picdn.youdianzhishi.com/images/20200428103237.png)

从图表上可以看出来 `msg-processor` 服务问题较多，只有少量的是 `msg-receiver-api` 服务的，当然我们也可以只查看 `ERROR` 级别的日志统计信息：

![img](https://picdn.youdianzhishi.com/images/20200428103446.png)

从图表上可以看出来基本上出现错误日志的情况下两个服务都会出现，所以这个时候我们就可以猜测两个服务的错误是非常相关的了，这对于我们去排查错误非常有帮助。最后也将该图表进行保存。

最后我们也可以将上面的两个图表添加到 `dashboard` 中，这样我们就可以在一个页面上组合各种可视化图表。切换到 `dashboard` 页面，然后点击 `Create New Dashboard` 按钮：

![img](https://picdn.youdianzhishi.com/images/20200428104152.png)

选择 `Add an existing` 链接：

![img](https://picdn.youdianzhishi.com/images/20200428104225.png)

然后选择上面我们创建的两个图表，添加完成后同样保存该 `dashboard` 即可：

![img](https://picdn.youdianzhishi.com/images/20200428104516.png)

到这里我们就完成了通过 Fluentd 收集日志到 Elasticsearch，并通过 Kibana 对日志进行了分析可视化操作。

### 安装 Kafka

对于大规模集群来说，日志数据量是非常巨大的，如果直接通过 Fluentd 将日志打入 Elasticsearch，对 ES 来说压力是非常巨大的，我们可以在中间加一层消息中间件来缓解 ES 的压力，一般情况下我们会使用 Kafka，然后可以直接使用 `kafka-connect-elasticsearch` 这样的工具将数据直接打入 ES，也可以在加一层 Logstash 去消费 Kafka 的数据，然后通过 Logstash 把数据存入 ES，这里我们来使用 Logstash 这种模式来对日志收集进行优化。

首先在 Kubernetes 集群中安装 Kafka，同样这里使用 Helm 进行安装：

```
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

首先使用 `helm pull` 拉取 Chart 并解压：

```
helm pull bitnami/kafka --untar --version 12.17.5
cd kafka
```

这里面我们指定使用一个 `StorageClass` 来提供持久化存储，在 Chart 目录下面创建用于安装的 values 文件：

```
# values-prod.yaml
## Persistence parameters
##
persistence:
  enabled: true
  storageClass: "nfs-storage"
  accessModes:
    - ReadWriteOnce
  size: 5Gi
  ## Mount point for persistence
  mountPath: /bitnami/kafka

# 配置zk volumes
zookeeper:
  enabled: true
  persistence:
    enabled: true
    storageClass: "nfs-storage"
    accessModes:
      - ReadWriteOnce
    size: 8Gi
```

直接使用上面的 values 文件安装 kafka：

```
$ helm install kafka -f values-prod.yaml --namespace logging .
Release "kafka" does not exist. Installing it now.
NAME: kafka
LAST DEPLOYED: Tue Apr 27 18:46:01 2021
NAMESPACE: logging
STATUS: deployed
REVISION: 1
TEST SUITE: None
NOTES:
** Please be patient while the chart is being deployed **

Kafka can be accessed by consumers via port 9092 on the following DNS name from within your cluster:

    kafka.logging.svc.cluster.local

Each Kafka broker can be accessed by producers via port 9092 on the following DNS name(s) from within your cluster:

    kafka-0.kafka-headless.logging.svc.cluster.local:9092

To create a pod that you can use as a Kafka client run the following commands:

    kubectl run kafka-client --restart='Never' --image docker.io/bitnami/kafka:2.8.0-debian-10-r0 --namespace logging --command -- sleep infinity
    kubectl exec --tty -i kafka-client --namespace logging -- bash

    PRODUCER:
        kafka-console-producer.sh \

            --broker-list kafka-0.kafka-headless.logging.svc.cluster.local:9092 \
            --topic test

    CONSUMER:
        kafka-console-consumer.sh \

            --bootstrap-server kafka.logging.svc.cluster.local:9092 \
            --topic test \
            --from-beginning
```

安装完成后我们可以使用上面的提示来检查 Kafka 是否正常运行：

```
$ kubectl get pods -n logging -l app.kubernetes.io/instance=kafka
kafka-0             1/1     Running   0          7m58s
kafka-zookeeper-0   1/1     Running   0          7m58s
```

用下面的命令创建一个 Kafka 的测试客户端 Pod：

```
$ kubectl run kafka-client --restart='Never' --image docker.io/bitnami/kafka:2.8.0-debian-10-r0 --namespace logging --command -- sleep infinity
pod/kafka-client created
```

然后启动一个终端进入容器内部生产消息：

```
# 生产者
$ kubectl exec --tty -i kafka-client --namespace logging -- bash
I have no name!@kafka-client:/$ kafka-console-producer.sh --broker-list kafka-0.kafka-headless.logging.svc.cluster.local:9092 --topic test
>hello kafka on k8s
>
```

启动另外一个终端进入容器内部消费消息：

```
# 消费者
$ kubectl exec --tty -i kafka-client --namespace logging -- bash
I have no name!@kafka-client:/$ kafka-console-consumer.sh --bootstrap-server kafka.logging.svc.cluster.local:9092 --topic test --from-beginning
hello kafka on k8s
```

如果在消费端看到了生产的消息数据证明我们的 Kafka 已经运行成功了。

### Fluentd 配置 Kafka

现在有了 Kafka，我们就可以将 Fluentd 的日志数据输出到 Kafka 了，只需要将 Fluentd 配置中的 `<match>` 更改为使用 Kafka 插件即可，但是在 Fluentd 中输出到 Kafka，需要使用到 `fluent-plugin-kafka` 插件，所以需要我们自定义下 Docker 镜像，最简单的做法就是在上面 Fluentd 镜像的基础上新增 kafka 插件即可，Dockerfile 文件如下所示：

```
FROM quay.io/fluentd_elasticsearch/fluentd:v3.2.0
RUN echo "source 'https://mirrors.tuna.tsinghua.edu.cn/rubygems/'" > Gemfile && gem install bundler
RUN gem install fluent-plugin-kafka -v 0.16.1 --no-document
```

使用上面的 `Dockerfile` 文件构建一个 Docker 镜像即可，我这里构建过后的镜像名为 `cnych/fluentd-kafka:v0.16.1`。接下来替换 Fluentd 的 Configmap 对象中的 `<match>` 部分，如下所示：

```
# fluentd-configmap.yaml
kind: ConfigMap
apiVersion: v1
metadata:
  name: fluentd-conf
  namespace: logging
data:
  ......
  output.conf: |-
    <match **>
      @id kafka
      @type kafka2
      @log_level info

      # list of seed brokers
      brokers kafka-0.kafka-headless.logging.svc.cluster.local:9092
      use_event_time true

      # topic settings
      topic_key k8slog
      default_topic messages  # 注意，kafka中消费使用的是这个topic
      # buffer settings
      <buffer k8slog>
        @type file
        path /var/log/td-agent/buffer/td
        flush_interval 3s
      </buffer>

      # data type settings
      <format>
        @type json
      </format>

      # producer settings
      required_acks -1
      compression_codec gzip

    </match>
```

然后替换运行的 Fluentd 镜像：

```
# fluentd-daemonset.yaml
image: cnych/fluentd-kafka:v0.16.1
```

直接更新 Fluentd 的 Configmap 与 DaemonSet 资源对象即可：

```
kubectl apply -f fluentd-configmap.yaml
kubectl apply -f fluentd-daemonset.yaml
```

更新成功后我们可以使用上面的测试 Kafka 客户端来验证是否有日志数据：

```
$ kubectl exec --tty -i kafka-client --namespace logging -- bash
I have no name!@kafka-client:/$ kafka-console-consumer.sh --bootstrap-server kafka.logging.svc.cluster.local:9092 --topic messages --from-beginning
{"stream":"stdout","docker":{},"kubernetes":{"container_name":"count","namespace_name":"default","pod_name":"counter","container_image":"busybox:latest","host":"node1","labels":{"logging":"true"}},"message":"43883: Tue Apr 27 12:16:30 UTC 2021\n"}
......
```

### 安装 Logstash

虽然数据从 Kafka 到 Elasticsearch 的方式多种多样，我们这里还是采用更加流行的 Logstash 方案，上面我们已经将日志从 Fluentd 采集输出到 Kafka 中去了，接下来我们使用 Logstash 来连接 Kafka 与 Elasticsearch 间的日志数据。

首先使用 `helm pull` 拉取 Chart 并解压：

```
helm pull elastic/logstash --untar --version 7.12.0
cd logstash
```

同样在 Chart 根目录下面创建用于安装的 Values 文件，如下所示：

```
# values-prod.yaml
fullnameOverride: logstash

persistence:
  enabled: true

logstashConfig:
  logstash.yml: |
    http.host: 0.0.0.0
    # 如果启用了xpack，需要做如下配置
    xpack.monitoring.enabled: true
    xpack.monitoring.elasticsearch.hosts: ["http://elasticsearch-client:9200"]
    xpack.monitoring.elasticsearch.username: "elastic"
    xpack.monitoring.elasticsearch.password: "ydzsio321"

# 要注意下格式
logstashPipeline:
  logstash.conf: |
    input { kafka { bootstrap_servers => "kafka-0.kafka-headless.logging.svc.cluster.local:9092" codec => json consumer_threads => 3 topics => ["messages"] } }
    filter {}  # 过滤配置（比如可以删除key、添加geoip等等）
    output { elasticsearch { hosts => [ "elasticsearch-client:9200" ] user => "elastic" password => "ydzsio321" index => "logstash-k8s-%{+YYYY.MM.dd}" } stdout { codec => rubydebug } }

volumeClaimTemplate:
  accessModes: ["ReadWriteOnce"]
  storageClassName: nfs-storage
  resources:
    requests:
      storage: 1Gi
```

其中最重要的就是通过 `logstashPipeline` 配置 logstash 数据流的处理配置，通过 `input` 指定日志源 kafka 的配置，通过 `output` 输出到 Elasticsearch，同样直接使用上面的 Values 文件安装 logstash 即可：

```
$ helm upgrade --install logstash -f values-prod.yaml --namespace logging .
Release "logstash" does not exist. Installing it now.
NAME: logstash
LAST DEPLOYED: Tue Apr 27 20:22:45 2021
NAMESPACE: logging
STATUS: deployed
REVISION: 1
TEST SUITE: None
NOTES:
1. Watch all cluster members come up.
  $ kubectl get pods --namespace=logging -l app=logstash -w
```

安装启动完成后可以查看 logstash 的日志：

```
$ logstash kubectl get pods --namespace=logging -l app=logstash
NAME         READY   STATUS    RESTARTS   AGE
logstash-0   1/1     Running   0          2m8s
$ kubectl logs -f logstash-0 -n logging
......
{
"docker" => {},
"stream" => "stdout",
"message" => "46921: Tue Apr 27 13:07:15 UTC 2021\n",
"kubernetes" => {
            "host" => "node1",
          "labels" => {
    "logging" => "true"
},
        "pod_name" => "counter",
"container_image" => "busybox:latest",
  "container_name" => "count",
  "namespace_name" => "default"
},
"@timestamp" => 2021-04-27T13:07:15.761Z,
"@version" => "1"
}
```

由于我们启用了 debug 日志调试，所以我们可以在 logstash 的日志中看到我们采集的日志消息，到这里证明我们的日志数据就获取成功了。

现在我们可以登录到 Kibana 可以看到有如下所示的索引数据了：

![img](https://picdn.youdianzhishi.com/images/20210427210958.png)

然后同样创建索引模式，匹配上面的索引即可：

![img](https://picdn.youdianzhishi.com/images/20210427211119.png)

创建完成后就可以前往发现页面过滤日志数据了：

![img](https://picdn.youdianzhishi.com/images/20210427211331.png)

到这里我们就实现了一个使用 `Fluentd+Kafka+Logstash+Elasticsearch+Kibana` 的 Kubernetes 日志收集工具栈，这里我们完整的 Pod 信息如下所示：

```
$ kubectl get pods -n logging
NAME                            READY   STATUS    RESTARTS   AGE
elasticsearch-client-0          1/1     Running   0          128m
elasticsearch-data-0            1/1     Running   0          128m
elasticsearch-master-0          1/1     Running   0          128m
fluentd-6k52h                   1/1     Running   0          61m
fluentd-cw72c                   1/1     Running   0          61m
fluentd-dn4hs                   1/1     Running   0          61m
kafka-0                         1/1     Running   3          134m
kafka-client                    1/1     Running   0          125m
kafka-zookeeper-0               1/1     Running   0          134m
kibana-kibana-66f97964b-qqjgg   1/1     Running   0          128m
logstash-0                      1/1     Running   0          13m
```

当然在实际的工作项目中还需要我们根据实际的业务场景来进行参数性能调优以及高可用等设置，以达到系统的最优性能。

## T9.3、Loki

Grafana Loki 是一套可以组合成一个功能齐全的日志堆栈组件，与其他日志记录系统不同，Loki 是基于仅索引有关日志元数据的想法而构建的：标签（就像 Prometheus 标签一样）。日志数据本身被压缩然后并存储在对象存储（例如 S3 或 GCS）的块中，甚至存储在本地文件系统上，轻量级的索引和高度压缩的块简化了操作，并显着降低了 Loki 的成本，Loki 更适合中小团队。由于 Loki 使用和 Prometheus 类似的标签概念，所以如果你熟悉 Prometheus 那么将很容易上手；也可以直接和 Grafana 集成，只需要添加 Loki 数据源就可以开始查询日志数据了。

Loki 还提供了一个专门用于日志查询的 LogQL 查询语句，类似于 PromQL，通过 LogQL 我们可以很容易查询到需要的日志，也可以很轻松获取监控指标。Loki 还能够将 LogQL 查询直接转换为 Prometheus 指标。此外 Loki 允许我们定义有关 LogQL 指标的报警，并且由于它与 Prometheus 兼容，因此可以将它们和 Alertmanager 进行对接。

Grafana Loki 主要由 3 部分组成:

- loki: 日志记录引擎，负责存储日志和处理查询
- promtail: 代理，负责收集日志并将其发送给 loki
- grafana: UI 界面

### 概述

Loki 是一组可以组成功能齐全的日志收集堆栈的组件，与其他日志收集系统不同，Loki 的构建思想是仅为日志建立索引标签，而使原始日志消息保持未索引状态。这意味着 Loki 的运营成本更低，并且效率更高。

#### 多租户

Loki 支持多租户，以使租户之间的数据完全分离。当 Loki 在多租户模式下运行时，所有数据（包括内存和长期存储中的数据）都由租户 ID 分区，该租户 ID 是从请求中的 `X-Scope-OrgID` HTTP 头中提取的。 当 Loki 不在多租户模式下时，将忽略 Header 头，并将租户 ID 设置为 `fake`，这将显示在索引和存储的块中。

#### 运行模式

![Loki 运行模式](https://picdn.youdianzhishi.com/images/20210504185732.png)

Loki 针对本地运行（或小规模运行）和水平扩展进行了优化吗，Loki 带有单一进程模式，可在一个进程中运行所有必需的微服务。单进程模式非常适合测试 Loki 或以小规模运行。为了实现水平可伸缩性，可以将 Loki 的微服务拆分为单独的组件，从而使它们彼此独立地扩展。每个组件都产生一个用于内部请求的 gRPC 服务器和一个用于外部 API 请求的 HTTP 服务，所有组件都带有 HTTP 服务器，但是大多数只暴露就绪接口、运行状况和指标端点。

Loki 运行哪个组件取决于命令行中的 `-target` 标志或 Loki 的配置文件中的 `target：<string>` 配置。 当 target 的值为 `all` 时，Loki 将在单进程中运行其所有组件。，这称为`单进程`或`单体模式`。 使用 Helm 安装 Loki 时，单单体模式是默认部署方式。

当 target 未设置为 all（即被设置为 `querier`、`ingester`、`query-frontend` 或 `distributor`），则可以说 Loki 在`水平伸缩`或`微服务模式`下运行。

Loki 的每个组件，例如 `ingester` 和 `distributors` 都使用 Loki 配置中定义的 gRPC 侦听端口通过 gRPC 相互通信。当以单体模式运行组件时，仍然是这样的：尽管每个组件都以相同的进程运行，但它们仍将通过本地网络相互连接进行组件之间的通信。

单体模式非常适合于本地开发、小规模等场景，单体模式可以通过多个进程进行扩展，但有以下限制：

- 当运行带有多个副本的单体模式时，当前无法使用本地索引和本地存储，因为每个副本必须能够访问相同的存储后端，并且本地存储对于并发访问并不安全。
- 各个组件无法独立缩放，因此读取组件的数量不能超过写入组件的数量。

#### 组件

![Loki 组件](https://picdn.youdianzhishi.com/images/20210506102731.png)

##### Distributor

`distributor` 服务负责处理客户端写入的日志，它本质上是日志数据写入路径中的**第一站**，一旦 `distributor` 收到日志数据，会将其拆分为多个批次，然后并行发送给多个 `ingester`。

`distributor` 通过 gRPC 与 `ingester` 通信，它们都是无状态的，可以根据需要扩大或缩小规模。

**Hashing**

`Distributors` 将一致性 hash 和可配置的复制因子结合使用，以确定 `Ingester` 服务的哪些实例应该接收指定的流。

流是一组与租户和唯一标签集关联的日志，使用租户 ID 和标签集对流进行 hash 处理，然后使用哈希查询要发送流的 `Ingesters`。

存储在 Consul 中的哈希环被用来实现一致性哈希，所有的 `ingester` 都会使用自己拥有的一组 Token 注册到哈希环中，每个 Token 是一个随机的无符号 32 位数字，与一组 Token 一起，`ingester` 将其状态注册到哈希环中，状态 `JOINING` 和 `ACTIVE` 都可以接收写请求，而 `ACTIVE` 和 `LEAVING` 的 `ingesters` 可以接收读请求。在进行哈希查询时，`distributors` 只使用处于请求的适当状态的 ingester 的 Token。

为了进行哈希查找，`distributors` 找到最小合适的 Token，其值大于日志流的哈希值，当复制因子大于 1 时，属于不同 `ingesters` 的下一个后续 Token（在环中顺时针方向）也将被包括在结果中。

这种哈希配置的效果是，一个 `ingester` 拥有的每个 Token 都负责一个范围的哈希值，如果有三个值为 0、25 和 50 的 Token，那么 3 的哈希值将被给予拥有 25 这个 Token 的 `ingester`，拥有 25 这个 Token 的 `ingester`负责`1-25`的哈希值范围。

**Quorum(仲裁)一致性**

由于所有的 `distributors` 共享对同一哈希环的访问权，所以写请求可以被发送到任何 `distributor`。

为了确保查询结果的一致性，Loki 在读和写上使用 [Dynamo 式](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf)的仲裁一致性方式，这意味着 `distributor` 将等待至少一半加一个 `ingesters` 的响应，然后再对发送的客户端进行响应。

##### Ingester

`ingester` 服务负责将日志数据写入长期存储后端（DynamoDB、S3、Cassandra 等）。此外 `ingester` 会验证摄取的日志行是按照时间戳递增的顺序接收的（即每条日志的时间戳都比前面的日志晚一些），当 `ingester` 收到不符合这个顺序的日志时，该日志行会被拒绝并返回一个错误。

- 如果传入的行与之前收到的行完全匹配（与之前的时间戳和日志文本都匹配），传入的行将被视为完全重复并被忽略。
- 如果传入的行与前一行的时间戳相同，但内容不同，则接受该日志行。这意味着同一时间戳有两个不同的日志行是可能的。

来自每个唯一标签集的日志在内存中被建立成 `chunks(块)`，然后可以根据配置的时间间隔刷新到支持的后端存储。在下列情况下，块被压缩并标记为只读：

- 当前块容量已满（该值可配置）
- 过了太长时间没有更新当前块的内容
- 刷新了

每当一个数据块被压缩并标记为只读时，一个可写的数据块就会取代它。如果一个 `ingester` 进程崩溃或突然退出，所有尚未刷新的数据都会丢失。Loki 通常配置为多个副本（通常是 3 个）来**降低**这种风险。

当向持久存储刷新时，该块将根据其租户、标签和内容进行哈希处理，这意味着具有相同数据副本的多个 `ingesters` 实例不会将相同的数据两次写入备份存储中，但如果对其中一个副本的写入失败，则会在备份存储中创建多个不同的块对象。有关如何对数据进行重复数据删除，请参阅 Querier。

**WAL**

上面我们也提到了 `ingesters` 将数据临时存储在内存中，如果发生了崩溃，可能会导致数据丢失，而 WAL 就可以帮助我们来提高这方面的可靠性。

在计算机领域，WAL（Write-ahead logging，预写式日志）是数据库系统提供原子性和持久化的一系列技术。

在使用 WAL 的系统中，所有的修改都先被写入到日志中，然后再被应用到系统状态中。通常包含 redo 和 undo 两部分信息。为什么需要使用 WAL，然后包含 redo 和 undo 信息呢？举个例子，如果一个系统直接将变更应用到系统状态中，那么在机器断电重启之后系统需要知道操作是成功了，还是只有部分成功或者是失败了（为了恢复状态）。如果使用了 WAL，那么在重启之后系统可以通过比较日志和系统状态来决定是继续完成操作还是撤销操作。

`redo log` 称为重做日志，每当有操作时，在数据变更之前将操作写入 redo log，这样当发生断电之类的情况时系统可以在重启后继续操作。`undo log` 称为撤销日志，当一些变更执行到一半无法完成时，可以根据撤销日志恢复到变更之间的状态。

Loki 中的 WAL 记录了传入的数据，并将其存储在本地文件系统中，以保证在进程崩溃的情况下持久保存已确认的数据。重新启动后，Loki 将**重放**日志中的所有数据，然后将自身注册，准备进行后续写操作。这使得 Loki 能够保持在内存中缓冲数据的性能和成本优势，以及持久性优势（一旦写被确认，它就不会丢失数据）。

##### 查询前端

查询前端是一个可选的服务，提供 `querier` 的 API 端点，可以用来加速读取路径。当查询前端就位时，应将传入的查询请求定向到查询前端，而不是 `querier`, 为了执行实际的查询，群集中仍需要 `querier` 服务。

查询前端在内部执行一些查询调整，并在内部队列中保存查询。`querier` 作为 workers 从队列中提取作业，执行它们，并将它们返回到查询前端进行汇总。`querier` 需要配置查询前端地址（通过`-querier.frontend-address` CLI 标志），以便允许它们连接到查询前端。

查询前端是无状态的，然而，由于内部队列的工作方式，建议运行几个查询前台的副本，以获得公平调度的好处，在大多数情况下，两个副本应该足够了。

**队列**

查询前端的排队机制用于：

- 确保可能导致 `querier` 出现内存不足（OOM）错误的查询在失败时被重试。这允许管理员可以为查询提供不足的内存，或者并行运行更多的小型查询，这有助于降低总成本。
- 通过使用先进先出队列（FIFO）将多个大型请求分配到所有 `querier` 上，以防止在单个 `querier` 中传送多个大型请求。
- 通过在租户之间公平调度查询。

**分割**

查询前端将较大的查询分割成多个较小的查询，在下游 `querier` 上并行执行这些查询，并将结果再次拼接起来。这可以防止大型查询在单个查询器中造成内存不足的问题，并有助于更快地执行这些查询。

**缓存**

查询前端支持缓存指标查询结果，并在后续查询中重复使用。如果缓存的结果不完整，查询前端会计算所需的子查询，并在下游 `querier` 上并行执行这些子查询。查询前端可以选择将查询与其 step 参数对齐，以提高查询结果的可缓存性。结果缓存与任何 loki 缓存后端（当前为 memcached、redis 和内存缓存）兼容。

##### Querier

`Querier` 查询器服务使用 LogQL 查询语言处理查询，从 `ingesters` 和长期存储中获取日志。

查询器查询所有 `ingesters` 的内存数据，然后再到后端存储运行相同的查询。由于复制因子，查询器有可能会收到重复的数据。为了解决这个问题，查询器在内部对具有相同纳秒时间戳、标签集和日志信息的数据进行重复数据删除。

##### Chunk 格式

```
 -------------------------------------------------------------------
  |                               |                                 |
  |        MagicNumber(4b)        |           version(1b)           |
  |                               |                                 |
  -------------------------------------------------------------------
  |         block-1 bytes         |          checksum (4b)          |
  -------------------------------------------------------------------
  |         block-2 bytes         |          checksum (4b)          |
  -------------------------------------------------------------------
  |         block-n bytes         |          checksum (4b)          |
  -------------------------------------------------------------------
  |                        #blocks (uvarint)                        |
  -------------------------------------------------------------------
  | #entries(uvarint) | mint, maxt (varint) | offset, len (uvarint) |
  -------------------------------------------------------------------
  | #entries(uvarint) | mint, maxt (varint) | offset, len (uvarint) |
  -------------------------------------------------------------------
  | #entries(uvarint) | mint, maxt (varint) | offset, len (uvarint) |
  -------------------------------------------------------------------
  | #entries(uvarint) | mint, maxt (varint) | offset, len (uvarint) |
  -------------------------------------------------------------------
  |                      checksum(from #blocks)                     |
  -------------------------------------------------------------------
  |                    #blocks section byte offset                  |
  -------------------------------------------------------------------
```

`mint` 和 `maxt`分别描述了最小和最大的 Unix 纳秒时间戳。

##### Block 格式

一个 block 由一系列日志 entries 组成，每个 entry 都是一个单独的日志行。

请注意，一个 block 的字节是用 Gzip 压缩存储的。以下是它们未压缩时的形式。

```
  -------------------------------------------------------------------
  |    ts (varint)    |     len (uvarint)    |     log-1 bytes      |
  -------------------------------------------------------------------
  |    ts (varint)    |     len (uvarint)    |     log-2 bytes      |
  -------------------------------------------------------------------
  |    ts (varint)    |     len (uvarint)    |     log-3 bytes      |
  -------------------------------------------------------------------
  |    ts (varint)    |     len (uvarint)    |     log-n bytes      |
  -------------------------------------------------------------------
```

`ts` 是日志的 Unix 纳秒时间戳，而 len 是日志条目的字节长度。

##### Chunk 存储

Chunk 存储是 Loki 的长期数据存储，旨在支持交互式查询和持续写入，不需要后台维护任务。它由以下部分组成:

- 一个 chunks 索引，这个索引可以通过以下方式支持：Amazon DynamoDB、Google Bigtable、Apache Cassandra。
- 一个用于 chunk 数据本身的键值（KV）存储，可以是：Amazon DynamoDB、Google Bigtable、Apache Cassandra、Amazon S3、Google Cloud Storage。

> 与 Loki 的其他核心组件不同，块存储不是一个单独的服务、任务或进程，而是嵌入到需要访问 Loki 数据的 `ingester` 和 `querier` 服务中的一个库。

块存储依赖于一个统一的接口，用于支持块存储索引的 `NoSQL` 存储（DynamoDB、Bigtable 和 Cassandra）。这个接口假定索引是由以下项构成的键的条目集合。

- 一个哈希 key，对所有的读和写都是必需的。
- 一个范围 key，写入时需要，读取时可以省略，可以通过前缀或范围进行查询。

该接口在支持的数据库中的工作方式有些不同：

- `DynamoDB` 原生支持范围和哈希键，因此，索引条目被直接建模为 DynamoDB 条目，哈希键作为分布键，范围作为 DynamoDB 范围键。
- 对于 `Bigtable` 和 `Cassandra`，索引条目被建模为单个列值。哈希键成为行键，范围键成为列键。

一组模式集合被用来将读取和写入块存储时使用的匹配器和标签集映射到索引上的操作。随着 Loki 的发展，Schemas 模式也被添加进来，主要是为了更好地平衡写操作和提高查询性能。

##### 读取路径

日志读取路径的流程如下所示：

- 查询器收到一个对数据的 HTTP 请求。
- 查询器将查询传递给所有 `ingesters` 以获取内存数据。
- `ingesters` 收到读取请求，并返回与查询相匹配的数据（如果有的话）。
- 如果没有 `ingesters` 返回数据，查询器会从后端存储加载数据，并对其运行查询。
- 查询器对所有收到的数据进行迭代和重复计算，通过 HTTP 连接返回最后一组数据。

##### 写入路径

![write path](https://picdn.youdianzhishi.com/images/20210505174014.png)

整体的日志写入路径如下所示：

- `distributor` 收到一个 HTTP 请求，以存储流的数据。
- 每个流都使用哈希环进行哈希操作。
- `distributor` 将每个流发送到合适的 `ingester` 和他们的副本（基于配置的复制因子）。
- 每个 `ingester` 将为日志流数据创建一个块或附加到一个现有的块上。每个租户和每个标签集的块是唯一的。
- `distributor` 通过 HTTP 连接响应一个成功代码。

### 安装

首先添加 Loki 的 Chart 仓库：

```
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
```

获取 `loki-stack` 的 Chart 包并解压：

```
helm pull grafana/loki-stack --untar --version 2.3.1
```

`loki-stack` 这个 Chart 包里面包含所有的 Loki 相关工具依赖，在安装的时候可以根据需要开启或关闭，比如我们想要安装 Grafana，则可以  在安装的时候简单设置 `--set grafana.enabled=true` 即可。默认情况下 `loki`、`promtail` 是自动开启的，也可以根据我们的需要选择使用 `filebeat` 或者 `logstash`，同样在 Chart 包根目录下面创建用于安装的 Values 文件：

```
# values-prod.yaml
loki:
  enabled: true
  replicas: 1
  persistence:
    enabled: true
    storageClassName: nfs-storage

promtail:
  enabled: true

grafana:
  enabled: true
  service:
    type: NodePort
  persistence:
    enabled: true
    storageClassName: nfs-storage
    accessModes:
      - ReadWriteOnce
    size: 1Gi
```

然后直接使用上面的 Values 文件进行安装即可：

```
helm upgrade --install loki -n logging -f values-prod.yaml .
Release "loki" does not exist. Installing it now.
NAME: loki
LAST DEPLOYED: Sat May  8 11:58:50 2021
NAMESPACE: logging
STATUS: deployed
REVISION: 1
NOTES:
The Loki stack has been deployed to your cluster. Loki can now be added as a datasource in Grafana.

See http://docs.grafana.org/features/datasources/loki/ for more detail.
```

安装完成后可以查看 Pod 的状态：

```
$ kubectl get pods -n logging
NAME                            READY   STATUS    RESTARTS   AGE
loki-0                          1/1     Running   0          153m
loki-grafana-86f4f9cbcc-kls6j   1/1     Running   0          153m
loki-promtail-69w7b             1/1     Running   0          153m
loki-promtail-mzk77             1/1     Running   0          150m
loki-promtail-pnn97             1/1     Running   0          151m
```

这里我们为 Grafana 设置的 NodePort 类型的 Service：

```
$ kubectl get svc -n logging
NAME            TYPE        CLUSTER-IP       EXTERNAL-IP   PORT(S)        AGE
loki            ClusterIP   10.105.185.97    <none>        3100/TCP       156m
loki-grafana    NodePort    10.102.226.255   <none>        80:30029/TCP   156m
loki-headless   ClusterIP   None             <none>        3100/TCP       156m
```

可以通过 NodePort 端口 `30029` 访问 Grafana，使用下面的命令获取 Grafana 的登录密码：

```
kubectl get secret --namespace logging loki-grafana -o jsonpath="{.data.admin-password}" | base64 --decode ; echo
```

使用用户名 `admin` 和上面的获取的密码即可登录 Grafana，由于 Helm Chart 已经为 Grafana 配置好了 Loki 的数据源，所以我们可以直接获取到日志数据了。点击左侧 `Explore` 菜单，然后就可以筛选 Loki 的日志数据了：

![Loki Explore](https://picdn.youdianzhishi.com/images/20210508143951.png)

我们使用 Helm 安装的 Promtail 默认已经帮我们做好了配置，已经针对 Kubernetes 做了优化，我们可以查看其配置：

```
$ kubectl get cm loki-promtail -n logging -o yaml
apiVersion: v1
data:
  promtail.yaml: |
    client:
      backoff_config:
        max_period: 5m
        max_retries: 10
        min_period: 500ms
      batchsize: 1048576
      batchwait: 1s
      external_labels: {}
      timeout: 10s
    positions:
      filename: /run/promtail/positions.yaml
    server:
      http_listen_port: 3101
    target_config:
      sync_period: 10s
    scrape_configs:
    - job_name: kubernetes-pods-name
      pipeline_stages:
        - docker: {}
      kubernetes_sd_configs:
      - role: pod
      relabel_configs:
      - source_labels:
        - __meta_kubernetes_pod_label_name
        target_label: __service__
      - source_labels:
        - __meta_kubernetes_pod_node_name
        target_label: __host__
      - action: drop
        regex: ''
        source_labels:
        - __service__
      - action: labelmap
        regex: __meta_kubernetes_pod_label_(.+)
      - action: replace
        replacement: $1
        separator: /
        source_labels:
        - __meta_kubernetes_namespace
        - __service__
        target_label: job
      - action: replace
        source_labels:
        - __meta_kubernetes_namespace
        target_label: namespace
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_name
        target_label: pod
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_container_name
        target_label: container
      - replacement: /var/log/pods/*$1/*.log
        separator: /
        source_labels:
        - __meta_kubernetes_pod_uid
        - __meta_kubernetes_pod_container_name
        target_label: __path__
    - job_name: kubernetes-pods-app
      pipeline_stages:
        - docker: {}
      kubernetes_sd_configs:
      - role: pod
      relabel_configs:
      - action: drop
        regex: .+
        source_labels:
        - __meta_kubernetes_pod_label_name
      - source_labels:
        - __meta_kubernetes_pod_label_app
        target_label: __service__
      - source_labels:
        - __meta_kubernetes_pod_node_name
        target_label: __host__
      - action: drop
        regex: ''
        source_labels:
        - __service__
      - action: labelmap
        regex: __meta_kubernetes_pod_label_(.+)
      - action: replace
        replacement: $1
        separator: /
        source_labels:
        - __meta_kubernetes_namespace
        - __service__
        target_label: job
      - action: replace
        source_labels:
        - __meta_kubernetes_namespace
        target_label: namespace
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_name
        target_label: pod
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_container_name
        target_label: container
      - replacement: /var/log/pods/*$1/*.log
        separator: /
        source_labels:
        - __meta_kubernetes_pod_uid
        - __meta_kubernetes_pod_container_name
        target_label: __path__
    - job_name: kubernetes-pods-direct-controllers
      pipeline_stages:
        - docker: {}
      kubernetes_sd_configs:
      - role: pod
      relabel_configs:
      - action: drop
        regex: .+
        separator: ''
        source_labels:
        - __meta_kubernetes_pod_label_name
        - __meta_kubernetes_pod_label_app
      - action: drop
        regex: '[0-9a-z-.]+-[0-9a-f]{8,10}'
        source_labels:
        - __meta_kubernetes_pod_controller_name
      - source_labels:
        - __meta_kubernetes_pod_controller_name
        target_label: __service__
      - source_labels:
        - __meta_kubernetes_pod_node_name
        target_label: __host__
      - action: drop
        regex: ''
        source_labels:
        - __service__
      - action: labelmap
        regex: __meta_kubernetes_pod_label_(.+)
      - action: replace
        replacement: $1
        separator: /
        source_labels:
        - __meta_kubernetes_namespace
        - __service__
        target_label: job
      - action: replace
        source_labels:
        - __meta_kubernetes_namespace
        target_label: namespace
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_name
        target_label: pod
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_container_name
        target_label: container
      - replacement: /var/log/pods/*$1/*.log
        separator: /
        source_labels:
        - __meta_kubernetes_pod_uid
        - __meta_kubernetes_pod_container_name
        target_label: __path__
    - job_name: kubernetes-pods-indirect-controller
      pipeline_stages:
        - docker: {}
      kubernetes_sd_configs:
      - role: pod
      relabel_configs:
      - action: drop
        regex: .+
        separator: ''
        source_labels:
        - __meta_kubernetes_pod_label_name
        - __meta_kubernetes_pod_label_app
      - action: keep
        regex: '[0-9a-z-.]+-[0-9a-f]{8,10}'
        source_labels:
        - __meta_kubernetes_pod_controller_name
      - action: replace
        regex: '([0-9a-z-.]+)-[0-9a-f]{8,10}'
        source_labels:
        - __meta_kubernetes_pod_controller_name
        target_label: __service__
      - source_labels:
        - __meta_kubernetes_pod_node_name
        target_label: __host__
      - action: drop
        regex: ''
        source_labels:
        - __service__
      - action: labelmap
        regex: __meta_kubernetes_pod_label_(.+)
      - action: replace
        replacement: $1
        separator: /
        source_labels:
        - __meta_kubernetes_namespace
        - __service__
        target_label: job
      - action: replace
        source_labels:
        - __meta_kubernetes_namespace
        target_label: namespace
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_name
        target_label: pod
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_container_name
        target_label: container
      - replacement: /var/log/pods/*$1/*.log
        separator: /
        source_labels:
        - __meta_kubernetes_pod_uid
        - __meta_kubernetes_pod_container_name
        target_label: __path__
    - job_name: kubernetes-pods-static
      pipeline_stages:
        - docker: {}
      kubernetes_sd_configs:
      - role: pod
      relabel_configs:
      - action: drop
        regex: ''
        source_labels:
        - __meta_kubernetes_pod_annotation_kubernetes_io_config_mirror
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_label_component
        target_label: __service__
      - source_labels:
        - __meta_kubernetes_pod_node_name
        target_label: __host__
      - action: drop
        regex: ''
        source_labels:
        - __service__
      - action: labelmap
        regex: __meta_kubernetes_pod_label_(.+)
      - action: replace
        replacement: $1
        separator: /
        source_labels:
        - __meta_kubernetes_namespace
        - __service__
        target_label: job
      - action: replace
        source_labels:
        - __meta_kubernetes_namespace
        target_label: namespace
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_name
        target_label: pod
      - action: replace
        source_labels:
        - __meta_kubernetes_pod_container_name
        target_label: container
      - replacement: /var/log/pods/*$1/*.log
        separator: /
        source_labels:
        - __meta_kubernetes_pod_annotation_kubernetes_io_config_mirror
        - __meta_kubernetes_pod_container_name
        target_label: __path__
......
```

### 收集 Traefik 日志

这里我们以收集 Traefik 为例，为 Traefik 定制一个可视化的 Dashboard，默认情况下访问日志没有输出到 stdout，我们可以通过在命令行参数中设置 `--accesslog=true` 来开启，此外我们还可以设置访问日志格式为 json，这样更方便在 Loki 中查询使用：

```
containers:
- args:
  - --accesslog=true
  - --accesslog.format=json
  ......
```

默认 traefik 的日志输出为 stdout，如果你的采集端是通过读取文件的话，则需要用 filePath 参数将 traefik 的日志重定向到文件目录。

修改完成后正常在 Grafana 中就可以看到 Traefik 的访问日志了：

![Traefik Logs](https://picdn.youdianzhishi.com/images/20210508170819.png)

然后我们还可以导入 Dashboard 来展示 Traefik 的信息：https://grafana.com/grafana/dashboards/13713，在 Grafana 中导入 13713 号 Dashboard：

![导入 Dashboard](https://picdn.youdianzhishi.com/images/20210508171115.png)

不过要注意我们需要更改 Dashboard 里面图表的查询语句，将 job 的值更改为你实际的标签，比如我这里采集 Traefik 日志的最终标签为 `job="kube-system/traefik"`：

![修改标签](https://picdn.youdianzhishi.com/images/20210508172644.png)

此外该 Dashboard 上还出现了 `Panel plugin not found: grafana-piechart-panel` 这样的提示，这是因为该面板依赖 `grafana-piechart-panel` 这个插件，我们进入 Grafana 容器内安装重建 Pod 即可：

```
$ kubectl exec -it loki-grafana-864fc6999c-z9587 -n logging -- /bin/bash
bash-5.0$ grafana-cli plugins install grafana-piechart-panel
installing grafana-piechart-panel @ 1.6.1
from: https://grafana.com/api/plugins/grafana-piechart-panel/versions/1.6.1/download
into: /var/lib/grafana/plugins

✔ Installed grafana-piechart-panel successfully

Restart grafana after installing plugins . <service grafana-server restart>
```

由于上面我们安装的时候为 Grafana 持久化了数据，所以删掉 Pod 重建即可：

```
kubectl delete pod loki-grafana-864fc6999c-z9587 -n logging
pod "loki-grafana-864fc6999c-z9587" deleted
```

最后调整过后的 Traefik Dashboard 大盘效果如下所示：

![Grafana Traefk Dashboard](https://picdn.youdianzhishi.com/images/20210508174428.png)

## T9.4、Promtail

Promtail 是 Loki 官方支持的日志采集端，在需要采集日志的节点上运行采集代理，再统一发送到 Loki 进行处理。除了使用 Promtail，社区还有很多采集日志的组件，比如 fluentd、fluent bit 等，都是比较优秀的。

但是 Promtail 是运行 Kubernetes 时的首选客户端，因为你可以将其配置为自动从 Promtail 运行的同一节点上运行的 Pod 中抓取日志。Promtail 和 Prometheus 在 Kubernetes 中一起运行，还可以实现非常强大的调试功能，如果 Prometheus 和 Promtail 使用相同的标签，用户还可以使用 Grafana 根据标签集在指标和日志之间切换。

此外如果你想从日志中提取指标，比如计算某个特定信息的出现次数，Promtail 效果也是非常友好的。本文将介绍 Promtail 中的核心概念以及了解下如何设置 Promtail 来处理你的日志行数据，包括提取指标与标签等。

### 配置

Promtail 是负责收集日志发送给 loki 的代理程序。Promtail 默认通过一个 `config.yaml` 文件进行配置，其中包含 Promtail 服务端信息、存储位置以及如何从文件中抓取日志等配置。

要指定加载哪个配置文件，只需要在命令行下通过 `-config.file` 参数传递 YAML 配置文件即可。此外我们还可以通过在配置文件中使用环境变量引用来设置需要的配置，但是需要在命令行中配置 `-config.expand-env=true`。

然后可以使用 `${VAR}` 来配置，其中 `VAR` 是环境变量的名称，每个变量的引用在启动时被环境变量的值替换，替换是区分大小写的，而且在 YAML 文件被解析之前发生，对未定义变量的引用将被替换为空字符串，除非你指定了一个默认值或自定义的错误文本，要指定一个默认值：

```
${VAR:default_value}
```

其中 `default_value` 是在环境变量未定义的情况下要使用的默认值。

默认的 `config.yaml` 配置文件支持的内容格式为：

```
# 配置 Promtail 服务端
[server: <server_config>]

# 描述 Promtail 如何连接到 Loki 的多个实例，向每个实例发送日志。
# WARNING：如果其中一个远程 Loki 服务器未能回应或回应时出现任何可重试的错误，这将影响其他配置的远程 Loki 服务器发送日志。
# 发送是在单线程上完成的!
# 如果你想向多个远程 Loki 实例发送，一般建议并行运行多个  promtail 客户端。
clients:
  - [<client_config>]

# 描述了如何将读取的文件偏移量保存到磁盘上
[positions: <position_config>]

# 抓取日志配置
scrape_configs:
  - [<scrape_config>]

# 配置被 watch 的目标如何 tailed
# Configures how tailed targets will be watched.
[target_config: <target_config>]
```

#### server

`server` 属性配置了 Promtail 作为 HTTP 服务器的行为。

```
# 禁用 HTTP 和 GRPC 服务
[disable: <boolean> | default = false]

# HTTP 服务监听的主机
[http_listen_address: <string>]

# HTTP 服务监听的端口（0表示随机）
[http_listen_port: <int> | default = 80]

# gRPC 服务监听主机
[grpc_listen_address: <string>]

# gRPC 服务监听的端口（0表示随机）
[grpc_listen_port: <int> | default = 9095]

# 注册指标处理器
[register_instrumentation: <boolean> | default = true]

# 优雅退出超时时间
[graceful_shutdown_timeout: <duration> | default = 30s]

# HTTP 服务读取超时时间
[http_server_read_timeout: <duration> | default = 30s]

# HTTP 服务写入超时时间
[http_server_write_timeout: <duration> | default = 30s]

# HTTP 服务空闲超时时间
[http_server_idle_timeout: <duration> | default = 120s]

# 可接收的最大 gRPC 消息大小
[grpc_server_max_recv_msg_size: <int> | default = 4194304]

# 可发送的最大 gRPC 消息大小
[grpc_server_max_send_msg_size: <int> | default = 4194304]

# 对 gRPC 调用的并发流数量的限制 (0 = unlimited)
[grpc_server_max_concurrent_streams: <int> | default = 100]

# 只记录给定严重程度或以上的信息，支持的值：[debug, info, warn, error]
[log_level: <string> | default = "info"]

# 所有 API 路由服务的基本路径(e.g., /v1/).
[http_path_prefix: <string>]

# 目标管理器检测 promtail 可读的标志，如果设置为 false 检查将被忽略
[health_check_target: <bool> | default = true]
```

#### client

`client` 属性配置了 Promtail 如何连接到 Loki 的实例。

```
# Loki 正在监听的 URL，在 Loki 中表示为 http_listen_address 和 http_listen_port
# 如果 Loki 在微服务模式下运行，这就是 Distributor 的 URL，需要包括 push API 的路径。
# 例如：http://example.com:3100/loki/api/v1/push
url: <string>

# 默认使用的租户 ID，用于推送日志到 Loki。
# 如果省略或为空，则会假设 Loki 在单租户模式下运行，不发送 X-Scope-OrgID 头。
[tenant_id: <string>]

# 发送一批日志前的最大等待时间，即使该批次日志数据未满。
[batchwait: <duration> | default = 1s]

# 在向 Loki 发送批处理之前要积累的最大批处理量（以字节为单位）。
[batchsize: <int> | default = 102400]

# 如果使用了 basic auth 认证，则需要配置用户名和密码
basic_auth:
  [username: <string>]
  [password: <string>]
  # 包含basic auth认证的密码文件
  [password_file: <filename>]

# 发送给服务器的 Bearer token
[bearer_token: <secret>]

# 包含 Bearer token 的文件
[bearer_token_file: <filename>]

# 用来连接服务器的 HTTP 代理服务器
[proxy_url: <string>]

# 如果连接到一个 TLS 服务器，配置 TLS 认证方式。
tls_config:
  # 用来验证服务器的 CA 文件
  [ca_file: <string>]
  # 发送给服务器用于客户端认证的 cert 文件
  [cert_file: <filename>]
  # 发送给服务器用于客户端认证的密钥文件
  [key_file: <filename>]
  # 验证服务器证书中的服务器名称是这个值。
  [server_name: <string>]
  # 如果为 true，则忽略由未知 CA 签署的服务器证书。
  [insecure_skip_verify: <boolean> | default = false]

# 配置在请求失败时如何重试对 Loki 的请求。
# 默认的回退周期为：
# 0.5s, 1s, 2s, 4s, 8s, 16s, 32s, 64s, 128s, 256s(4.267m)
# 在日志丢失之前的总时间为511.5s(8.5m)
backoff_config:
  # 重试之间的初始回退时间
  [min_period: <duration> | default = 500ms]
  # 重试之间的最大回退时间
  [max_period: <duration> | default = 5m]
  # 重试的最大次数
  [max_retries: <int> | default = 10]

# 添加到所有发送到 Loki 的日志中的静态标签
# 使用一个类似于 {"foo": "bar"} 的映射来添加一个 foo 标签，值为 bar
# 这些也可以从命令行中指定：
# -client.external-labels=k1=v1,k2=v2
# (或 --client.external-labels 依赖操作系统)
# 由命令行提供的标签将应用于所有在 "clients" 部分的配置。
# 注意：如果标签的键相同，配置文件中定义的值将取代命令行中为特定 client 定义的值
external_labels:
  [ <labelname>: <labelvalue> ... ]

# 等待服务器响应一个请求的最长时间
[timeout: <duration> | default = 10s]
```

#### positions

`positions` 属性配置了 Promtail 保存文件的位置，表示它已经读到了文件什么程度。当 Promtail 重新启动时需要它，以允许它从中断的地方继续读取日志。

```
# positions 文件的路径
[filename: <string> | default = "/var/log/positions.yaml"]

# 更新 positions 文件的周期
[sync_period: <duration> | default = 10s]

# 是否忽略并覆盖被破坏的 positions 文件
[ignore_invalid_yaml: <boolean> | default = false]
```

#### scrape_configs

`scrape_configs` 属性配置了 Promtail 如何使用指定的发现方法从一系列目标中抓取日志。

```
# 用于在 Promtail 中识别该抓取配置的名称。
job_name: <string>

# 描述如何对目标日志进行结构化
[pipeline_stages: <pipeline_stages>]

# 如何从 jounal 抓取日志
[journal: <journal_config>]

# 如何从 syslog 抓取日志
[syslog: <syslog_config>]

# 如何通过 Loki push API 接收日志 (例如从其他 Promtails 或 Docker Logging Driver 中获取的数据)
[loki_push_api: <loki_push_api_config>]

# 描述了如何 relabel 目标，以确定是否应该对其进行处理
relabel_configs:
  - [<relabel_config>]

# 抓取日志静态目标配置
static_configs:
  - [<static_config>]

# 包含要抓取的目标文件
file_sd_configs:
  - [<file_sd_configs>]

# 描述了如何发现在同一主机上运行的 Kubernetes 服务
kubernetes_sd_configs:
  - [<kubernetes_sd_config>]
```

#### pipeline_stages

`pipeline_stages` 用于转换日志条目和它们的标签，该管道在发现操作结束后执行，`pipeline_stages` 对象由一个阶段列表组成。

```
- [
    <docker> |
    <cri> |
    <regex> |
    <json> |
    <template> |
    <match> |
    <timestamp> |
    <output> |
    <labels> |
    <metrics> |
    <tenant>,
  ]
```

在大多数情况下，你用 `regex` 或 `json` 阶段从日志中提取数据，提取的数据被转化为一个临时的字典 Map 对象，然后这些数据是可以被 promtail 使用的，比如可以作为标签的值或作为输出。此外，除了 docker 和 cri 之外，任何其他阶段都可以访问提取的数据。在后面 `pipeline` 部分会详细介绍如何配置。

#### loki_push_api

`loki_push_api` 属性配置 Promtail 来暴露一个 [Loki push API 服务](https://grafana.com/docs/loki/latest/api#post-lokiapiv1push)。每个配置了 `loki_push_api` 的任务都会暴露这个 API，并且需要一个单独的端口。

```
# push 服务配置选项
[server: <server_config>]

# 标签映射，用于添加到发送到 push API 的每一行日志上
labels:
  [ <labelname>: <labelvalue> ... ]

# promtail 是否应该从传入的日志中传递时间戳
# 当为 false 时，promtail 将把当前的时间戳分配给日志
[use_incoming_timestamp: <bool> | default = false]
```

比如下面的配置示例，将 Promtail 作为一个 Push 接收器启动，并将接受来自其他 Promtail 实例或 `Docker Logging Dirver` 的日志。

```
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://ip_or_hostname_where_Loki_run:3100/loki/api/v1/push

scrape_configs:
  - job_name: push1
    loki_push_api:
      server:
        http_listen_port: 3500
        grpc_listen_port: 3600
      labels:
        pushserver: push1
```

注意必须提供 `job_name`，并且在多个 `loki_push_api` 与 `scrape_configs` 之间必须是唯一的，它将被用来注册监控指标。

由于一个新的服务器实例被创建，所以 `http_listen_port` 和 `grpc_listen_port` 必须与 promtail 服务器配置部分不同（除非它被禁用）。

#### relabel_configs

`Relabeling` 是一个强大的工具，可以在目标日志被抓取之前动态地重写其标签集。每个抓取配置可以配置多个 relabeling 步骤，按照它们在配置文件中出现的顺序应用于每个目标的标签集。

在 `relabeling` 之后，如果 `instance` 标签在 relabeling 的时候没有被设置，则默认设置为 `__address__` 的值，`__scheme__` 和 `__metrics_path__` 标签被分别设置为目标的协议和 metrics 指标路径。`__param_<name>` 标签被设置为第一个传递的 URL 参数 `<name>` 的值。

在 `relabeling` 阶段，以 `__meta_` 为前缀的额外标签也是可用的，它们是由提供目标的服务发现机制设置的，并且在不同的机制之间有所不同。

在目标 `relabeling` 完成后，以 `__` 开头的标签将从标签集中删除。

如果一个 `relabeling` 操作只需要临时存储一个标签值（作为后续重新标注步骤的输入），请使用 `__tmp` 标签名称前缀。

```
# 从现有标签中选择 values 值的源标签
# 它们的内容使用配置的分隔符连接起来，并与配置的正则表达式相匹配，以进行替换、保留和删除操作。
[ source_labels: '[' <labelname> [, ...] ']' ]

# 连接源标签值之间的分隔符
[ separator: <string> | default = ; ]

# 在一个 replace 替换操作后结果值被写入的标签
# 它对替换动作是强制性的，Regex 捕获组是可用的。
[ target_label: <labelname> ]

# 正则表达式，提取的值与之匹配
[ regex: <regex> | default = (.*) ]

[ modulus: <uint64> ]

Replacement 值：如果正则表达式匹配，则对其进行 regex 替换
[ replacement: <string> | default = $1 ]

# 根据正则匹配结果执行的动作
[ action: <relabel_action> | default = replace ]
```

`<regex>` 是任何有效的 `RE2` 正则表达式，它是 replace、keep、drop、labelmap、labeldrop 和 labelkeep 操作的必要条件，该正则表达式在两端都是固定的，要取消对正则的锚定，请使用 `.*<regex>.*`。

`<relabel_action>` 决定了要采取的 `relabeling` 动作：

- `replace`：将正则表达式与连接的 `source_labels` 匹配，然后设置 `target_label` 为 `replacement`，用 replacement 中的匹配组引用（${1}、${2}…）替换其值，如果正则表达式不匹配，则不会进行替换。
- `keep`：删除那些 regex 与 `source_labels` 不匹配的目标。
- `drop`：删除与 regex 相匹配的 `source_labels` 目标。
- `hashmod`：将 `target_label` 设置为 `source_labels` 的哈希值的模。
- `labelmap`：将正则表达式与所有标签名称匹配，然后将匹配的标签值复制到由 `replacement` 给出的标签名中，replacement 中的匹配组引用（${1}, ${2}, ...）由其值代替。
- `labeldrop`：将正则表达式与所有标签名称匹配，任何匹配的标签都将从标签集中删除。
- `labelkeep`：将正则表达式与所有标签名称匹配，任何不匹配的标签将被从标签集中删除。

使用 `labeldrop` 和 `labelkeep` 时必须注意，一旦标签被移除，logs 仍然是唯一的标签。

#### static_configs

`static_configs` 静态配置允许指定一个目标列表和标签集：

```
# 配置发现在当前节点上查找
# 这是 Prometheus 服务发现代码所要求的，但并不适用于Promtail，它只能查看本地机器上的文件。
# 因此，它应该只有 localhost 的值，或者可以完全移除它，Promtail 会使用 localhost 的默认值。
targets:
  - localhost

# 定义一个要抓取的日志文件和一组可选的附加标签，以应用于由__path__定义的文件日志流。
labels:
  # 要加载日志的路径，可以使用 glob 模式(e.g., /var/log/*.log).
  __path__: <string>

  # 添加的额外标签
  [ <labelname>: <labelvalue> ... ]
```

比如这里我们配置一个如下所示的静态配置：

```
server:
  http_listen_port: 9080
  grpc_listen_port: 0
positions:
  filename: /var/log/positions.yaml # 这个位置需要是可以被promtail写入的
client:
  url: http://ip_or_hostname_where_Loki_run:3100/loki/api/v1/push
# 抓取配置
scrape_configs:
  - job_name: system
    pipeline_stages:
    static_configs:
      - targets:
          - localhost
        labels:
          job: varlogs # 在 Prometheus中，job 标签对于连接指标和日志很有用
          host: yourhost # `host` 标签可以帮助识别日志来源
          __path__: /var/log/*.log # 路径匹配使用了一个第三方库: https://github.com/bmatcuk/doublestar
```

#### file_sd_config

基于文件的服务发现提供了一种更通用的方式来配置静态目标。它读取一组包含零个或多个 `<static_config>` 列表的文件。对所有定义文件的改变通过监视磁盘变化来应用。文件可以以 YAML 或 JSON 格式提供。JSON 文件必须包含一个静态配置的列表，使用这种格式。

```
[
  {
    "targets": [ "localhost" ],
    "labels": {
      "__path__": "<string>", ...
      "<labelname>": "<labelvalue>", ...
    }
  },
  ...
]
```

此外文件内容也将以指定的刷新间隔定期重新读取。在 `relabeling` 标记阶段，每个目标都有一个元标签 `__meta_filepath`，它的值被设置为被提取的目标文件路径。

```
# 从中提取目标文件的模式。
files:
  [ - <filename_pattern> ... ]

# 重新读取文件的刷新频率
[ refresh_interval: <duration> | default = 5m ]
```

其中 `<filename_pattern>` 可以是一个以 `.json`、`.yml` 或 `.yaml` 结尾的路径，最后一个路径段可以包含一个匹配任何字符序列的 `*`，例如 `my/path/tg_*.json`。

#### kubernetes_sd_config

Kubernetes SD 配置允许从 Kubernetes 的 REST API 中检索抓取的目标，并始终与集群状态保持同步。关于 Kubernetes 发现的配置选项，如下所示：

```
# Kubernetes API 地址
# 如果留空，Prometheus 将被假定在集群内运行，并将自动发现 API 服务器并使用 pod 的 CA 证书和 bearer token 文件（在 /var/run/secrets/kubernetes.io/serviceaccount/ 目录下面）
[ api_server: <host> ]

# 发现的 Kubernetes 角色
role: <role>

# 可选的认证信息
basic_auth:
  [ username: <string> ]
  [ password: <secret> ]
  [ password_file: <string> ]

[ bearer_token: <secret> ]
[ bearer_token_file: <filename> ]
[ proxy_url: <string> ]

# TLS 配置
tls_config:
  [ <tls_config> ]

# 可选的命名空间发现，如果省略，将使用所有命名空间。
namespaces:
  names:
    [ - <string> ]
```

其中 `<role>` 必须是 `endpoints`、`service`、`pod`、`node` 或 `ingress`。具体的配置使用可以完全参考 Prometheus 中的基于 Kubernetes 的发现机制，可以查看 Promtheus 自动发现配置文件：https://github.com/prometheus/prometheus/blob/main/documentation/examples/prometheus-kubernetes.yml 了解更多配置。

### pipeline

在 Promtail 中一个 pipeline 管道被用来转换一个单一的日志行、标签和它的时间戳。一个 pipeline 管道是由一组 stages 阶段组成的，在 Promtail 配置红一共有 4 种类型的 stages。

1. `Parsing stages`(解析阶段) 用于解析当前的日志行并从中提取数据，提取的数据可供其他阶段使用。
2. `Transform stages`(转换阶段) 用于对之前阶段提取的数据进行转换。
3. `Action stages`(处理阶段) 用于从以前阶段中提取数据并对其进行处理，包括：
4. 添加或修改现有日志行标签
5. 更改日志行的时间戳
6. 修改日志行内容
7. 在提取的数据基础上创建一个 metrics 指标
8. `Filtering stages`(过滤阶段) 可选择应用一个阶段的子集，或根据一些条件删除日志数据。

一个典型的 pipeline 将从解析阶段开始（如 regex 或 json 阶段）从日志行中提取数据。然后有一系列的处理阶段配置，对提取的数据进行处理。最常见的处理阶段是一个 `labels stage` 标签阶段，将提取的数据转化为标签。

需要注意的是现在 pipeline 不能用于重复的日志，例如，Loki 将多次收到同一条日志行：

- 从同一文件中读取的两个抓取配置
- 文件中重复的日志行被发送到一个 pipeline，不会做重复数据删除

然后，Loki 会在查询时对那些具有完全相同的纳秒时间戳、标签与日志内容的日志进行一些重复数据删除。

下面的配置示例可以很好地说明我们可以通过 pipeline 来对日志行数据实现什么功能：

```
scrape_configs:
  - job_name: kubernetes-pods-name
    kubernetes_sd_configs: ....
    pipeline_stages:
      # 这个阶段只有在被抓取地目标有一个标签名为 name 且值为 promtail 地时候才会执行
      - match:
          selector: '{name="promtail"}'
          stages:
            # regex 阶段解析出一个 level、timestamp 与 component，在该阶段结束时，这几个值只为 pipeline 内部设置，在以后地阶段可以使用这些值并决定如何处理他们。
            - regex:
                expression: '.*level=(?P<level>[a-zA-Z]+).*ts=(?P<timestamp>[T\d-:.Z]*).*component=(?P<component>[a-zA-Z]+)'

            # labels 阶段从前面地 regex 阶段获取 level、component 值，并将他们变成一个标签，比如 level=error 可能就是这个阶段添加地一个标签。
            - labels:
                level:
                component:

            # 最后，时间戳阶段采用从 regex 提取地 timestamp，并将其变成日志的新时间戳，并解析为 RFC3339Nano 格式。
            - timestamp:
                format: RFC3339Nano
                source: timestamp

      # 这个阶段只有在抓取的目标标签为 name，值为 nginx，并且日志行中包含 GET 字样的时候才会执行
      - match:
          selector: '{name="nginx"} |= "GET"'
          stages:
            # regex 阶段通过匹配一些值来提取一个新的 output 值。
            - regex:
                expression: \w{1,3}.\w{1,3}.\w{1,3}.\w{1,3}(?P<output>.*)

            # output 输出阶段通过将捕获的日志行设置为来自上面 regex 阶段的输出值来更改其内容。
            - output:
                source: output

      # 这个阶段只有在抓取到目标中有标签 name，值为 jaeger-agent 时才会执行。
      - match:
          selector: '{name="jaeger-agent"}'
          stages:
            # JSON 阶段将日志行作为 JSON 字符串读取，并从对象中提取 level 字段，以便在后续的阶段中使用。
            - json:
                expressions:
                  level: level

            # 将上一个阶段中的 level 值变成一个标签。
            - labels:
                level:

  - job_name: kubernetes-pods-app
    kubernetes_sd_configs: ....
    pipeline_stages:
      # 这个阶段只有在被抓取的目标的标签为 "app"，名称为grafana 或 prometheus 时才会执行。
      - match:
          selector: '{app=~"grafana|prometheus"}'
          stages:
            # regex 阶段将提取一个 level 合 componet 值，供后面的阶段使用，允许 level 被定义为 lvl=<level> 或 level=<level>，组件被定义为 logger=<component> 或 component=<component>
            - regex:
                expression: ".*(lvl|level)=(?P<level>[a-zA-Z]+).*(logger|component)=(?P<component>[a-zA-Z]+)"

            # 然后标签阶段将从上面 regex 阶段提取的 level 和 component 变为标签。
            - labels:
                level:
                component:

      # 只有当被抓取的目标有一个标签 "app"，其值为 "some-app"，并且日志行不包含 "info" 一词时，这个阶段才会执行。
      - match:
          selector: '{app="some-app"} != "info"'
          stages:
            # regex 阶段尝试通过查找日志中的 panic 来提取 panic 信息
            - regex:
                expression: ".*(?P<panic>panic: .*)"

            # metrics 阶段将增加一个 Promtail 暴露的 panic_total 指标，只有当从上面的 regex 阶段获取到 panic 值的时候，该 Counter 才会增加。
            - metrics:
                panic_total:
                  type: Counter
                  description: "total count of panic"
                  source: panic
                  config:
                    action: inc
```

下面我们先简单描述下每个阶段可以使用的数据有哪些。

- `标签集`：当前日志行的标签集合，初始化是与日志一起被抓取的标签集，标签集只由处理阶段进行修改，但过滤阶段会从中读取，最终的标签集将由 Loki 建立索引，并可用与查询。
- `提取的键值对`：在解析阶段提取的键值对集合，后续的阶段对提取的 Map 进行操作，或者对它们进行转换，或者对它们进行处理。在一个 pipeline 的末端，提取的 Map 会被丢弃掉，为了使一个解析阶段有用，它必须总要与至少一个处理阶段配对。提取的 Map 被初始化，其初始化标签是与日志行一起抓取的，这个初始数据允许在只操作提取的 Map 的 pipeline 阶段内对标签的值进行处理。例如，从文件中提取的日志条目有一个标签 filename，其值是被提取的文件路径，当一个 pipeline 执行该日志时，最初提取的 Map 将包含使用与标签相同值的文件名。
- `日志时间戳`：日志行的当前时间戳，处理阶段可以修改这个值。如果不设置，则默认为日志被抓取的时间。时间戳的最终值会发送给 Loki。
- `日志行`：当前的日志行，以文本形式表示，初始化为 Promtail 抓取的文本。处理阶段可以修改这个值。日志行的最终值将作为日志的文本内容发送给 Loki。

### 阶段

上面我们结束了 Promtail 的一个 pipeline 中有 4 中类型的阶段，下面我们再分别对这 4 中类型阶段进行简单说明。

#### 解析阶段

解析阶段包括：docker、cri、regex、json 这几个 stage。

##### docker

`docker` 阶段通过使用标签的 Docker 日志格式来解析日志数据进行数据提取。直接使用 `docker: {}` 即表示是一个 docker 阶段。

与大多数阶段不同，docker 阶段不提供配置选项，只支持特定的 Docker 日志格式，来自 Docker 的每一行日志都被写成 JSON 格式，其键值如下。

- `log`：日志行的内容
- `stream`：`stdout` 或者 `stderr`
- `time`：日志行的时间戳字符串

例如配置下面的 pipeline：

```
- docker: {}
```

将会解析 Docker 日志成如下所示格式：

```
{
  "log": "log message\n",
  "stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z"
}
```

在提取的数据集中，将创建以下键值对：

- `output`： `log message\n`
- `stream`： `stderr`
- `timestamp`：`2019-04-30T02:12:41.8443515`

##### cri

通过使用标准 CRI 格式解析日志行来提取数据。使用语法一样是直接使用 `cri: {}` 即可，与大多数阶段不同，cri 阶段不提供配置选项，只支持特定的 CRI 日志格式。CRI 指定的日志行是以空格分隔的值，有以下组成部分：

- `log`：整个日志行的内容
- `stream`：`stdout` 或者 `stderr`
- `time`：日志行的时间戳字符串

组件之间不允许有空白，在下面的例子中，只有第一行日志可以使用 cri 阶段进行正确格式化。

```
"2019-01-01T01:00:00.000000001Z stderr P test\ngood"
"2019-01-01 T01:00:00.000000001Z stderr testgood"
"2019-01-01T01:00:00.000000001Z testgood"
```

例如配置下面的 pipeline：

```
- cri: {}
```

当我们有如下所示的日志行数据：

```
"2019-04-30T02:12:41.8443515Z stdout xx message"
```

在提取的数据集中，将创建以下键值对：

- `output`: `message`
- `stream`: `stdout`
- `timestamp`: `2019-04-30T02:12:41.8443515`

##### regex

使用正则表达式提取数据，在 regex 中命名的捕获组支持将数据添加到提取的 Map 映射中。配置格式如下所示：

```
regex:
  # RE2 正则表达式，每个捕获组必须被命名。
  expression: <string>

  # 从指定名称中提取数据，如果为空，则使用 log 信息。
  [source: <string>]
```

其中的 `expression` 是一个 [Google RE2 正则表达式](https://github.com/google/re2/wiki/Syntax)字符串，每个捕获组将被设置为到提取的 Map 中去，每个捕获组也必须命名：`(?P<name>re)`，捕获组的名称将被用作提取的 Map 中的键。

另外需要注意，在使用双引号时，必须转义正则表达式中的所有反斜杠。例如下面的几个表达式都是有效的：

- `expression: \w*`
- `expression: '\w*'`
- `expression: "\\w*"`

但是下面的这几个是无效的表达式：

- `expression: \\w*` - 在使用双引号时才转义反斜线
- `expression: '\\w*'` - 在使用双引号时才转义反斜线
- `expression: "\w*"` - 在使用双引号的时候，反斜杠必须被转义

例如我们使用下的不带 `source` 的 pipeline 配置：

```
- regex:
    expression: "^(?s)(?P<time>\\S+?) (?P<stream>stdout|stderr) (?P<flags>\\S+?) (?P<content>.*)$"
```

当我们要抓取的日志数据为：

```
2019-01-01T01:00:00.000000001Z stderr P i'm a log message!
```

该 pipeline 执行后以下键值对将被添加到提取的 Map 中去：

- `time`: `2019-01-01T01:00:00.000000001Z`
- `stream`: `stderr`
- `flags`: `P`
- `content`: `i'm a log message`

如果我们使用带上 `source` 的 pipeline 配置：

```
- json:
    expressions:
      time:
- regex:
    expression: "^(?P<year>\\d+)"
    source: "time"
```

如果需要抓取的日志数据为：

```
{ "time": "2019-01-01T01:00:00.000000001Z" }
```

则第一阶段将把以下键值对添加到提取的 Map 中：

- `time`: `2019-01-01T01:00:00.000000001Z`

而 regex 阶段将解析提取的 Map 中的时间值，并将以下键值对追加到提取的 Map 中去：

- `year`: `2019`

##### json

通过将日志行解析为 JSON 来提取数据，也可以接受 `JMESPath` 表达式来提取数据，配置格式如下所示：

```
json:
  # JMESPath 表达式的键/值对集合，键将是提取的数据中的键，而表达式将是值，被评估为来自源数据的 JMESPath。
  #
  # JMESPath 表达式可以通过用双引号来包装一个键完成，然后在 YAML 中必须用单引号包装起来，这样它们就会被传递给 JMESPath 解析器进行解析。
  expressions:
    [ <string>: <string> ... ]

  [source: <string>]
```

该阶段使用 Golang JSON 反序列化，提取的数据可以持有非字符串值，本阶段不做任何类型转换，在下游阶段将需要对这些值进行必要的类型转换，可以参考后面的 `template` 阶段了解如何进行转换。

> 注意：如果提取的值是一个复杂的类型，比如数组或 JSON 对象，它将被转换为 JSON 字符串，然后插入到提取的数据中去。

例如我们使用如下所示的 pipeline 配置：

```
- json:
    expressions:
      output: log
      stream: stream
      timestamp: time
```

要抓取的日志行数据为：

```
{
  "log": "log message\n",
  "stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z"
}
```

在提取的数据集中，将创建以下键值对：

- `output`: `log message\n`
- `stream`: `stderr`
- `timestamp`: `2019-04-30T02:12:41.8443515`

然后我们还可以用下面的 pipeline 配置来提前数据：

```
- json:
    expressions:
      output: log
      stream: stream
      timestamp: time
      extra:
- json:
    expressions:
      user:
    source: extra
```

要抓取的日志行数据为：

```
{
  "log": "log message\n",
  "stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z",
  "extra": "{\"user\":\"marco\"}"
}
```

第一个 json 阶段执行后将在提取的数据集中创建以下键值对：

- `output`: `log message\n`
- `stream`: `stderr`
- `timestamp`: `2019-04-30T02:12:41.8443515`
- `extra`: `{"user": "marco"}`

然后经过第二个 json 阶段执行后将把提取数据中的 extra 值解析为 JSON，并将以下键值对添加到提取的数据集中：

- `user`: `marco`

此外我们还可以使用 JMESPath 表达式来解析有特殊字符的 JSON 字段（比如 `@` 或 `.`），比如我们现在有如下所示的 pipeline 配置：

```
- json:
    expressions:
      output: log
      stream: '"grpc.stream"'
      timestamp: time
```

需要抓取的日志数据如下所示：

```
{
  "log": "log message\n",
  "grpc.stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z"
}
```

在提取的数据集中，将创建以下键值对。

- `output`: `log message\n`
- `stream`: `stderr`
- `timestamp`: `2019-04-30T02:12:41.8443515`

需要注意的是在引用 `grpc.stream` 时，如果没有用单引号包裹的双引号，将无法正常工作。

#### 转换阶段

转换阶段用于对之前阶段提取的数据进行转换。

##### multiline

多行阶段将多行日志进行合并，然后再将其传递到 pipeline 的下一个阶段。

一个新的日志块由**第一行正则表达式**来识别，任何与表达式不匹配的行都被认为是前一个匹配块的一部分。配置格式如下所示：

```
multiline:
  # RE2 正则表达式，如果匹配将开始一个新的多行日志块
  # 这个表达式必须被提供
  firstline: <string>

  # 解析的最大等待时间（Go duration）: https://golang.org/pkg/time/#ParseDuration.
  # 如果在这个最大的等待时间内没有新的日志，那么当前日志块将被继续发送。
  # 如果被观察的应用程序因为异常而down掉了，该参数很有用，没有新的日志出现，并且异常块会在最大等待时间过后发送
  # 默认为 3s
  max_wait_time: <duration>

  # 一个多行日志块有的最大行数，如果该块有更多的行，就会认为是新的日志行
  # 默认为 128 行
  max_lines: <integer>
```

比如现在我们有一个 flask 应用，下面的日志数据包含异常信息：

```
[2020-12-03 11:36:20] "GET /hello HTTP/1.1" 200 -
[2020-12-03 11:36:23] ERROR in app: Exception on /error [GET]
Traceback (most recent call last):
  File "/home/pallets/.pyenv/versions/3.8.5/lib/python3.8/site-packages/flask/app.py", line 2447, in wsgi_app
    response = self.full_dispatch_request()
  File "/home/pallets/.pyenv/versions/3.8.5/lib/python3.8/site-packages/flask/app.py", line 1952, in full_dispatch_request
    rv = self.handle_user_exception(e)
  File "/home/pallets/.pyenv/versions/3.8.5/lib/python3.8/site-packages/flask/app.py", line 1821, in handle_user_exception
    reraise(exc_type, exc_value, tb)
  File "/home/pallets/.pyenv/versions/3.8.5/lib/python3.8/site-packages/flask/_compat.py", line 39, in reraise
    raise value
  File "/home/pallets/.pyenv/versions/3.8.5/lib/python3.8/site-packages/flask/app.py", line 1950, in full_dispatch_request
    rv = self.dispatch_request()
  File "/home/pallets/.pyenv/versions/3.8.5/lib/python3.8/site-packages/flask/app.py", line 1936, in dispatch_request
    return self.view_functions[rule.endpoint](**req.view_args)
  File "/home/pallets/src/deployment_tools/hello.py", line 10, in error
    raise Exception("Sorry, this route always breaks")
Exception: Sorry, this route always breaks
[2020-12-03 11:36:23] "GET /error HTTP/1.1" 500 -
[2020-12-03 11:36:26] "GET /hello HTTP/1.1" 200 -
[2020-12-03 11:36:27] "GET /hello HTTP/1.1" 200 -
```

显然我们更希望将上面的 Exception 多行日志识别为一个日志块，在这个示例中，所有的日志块都是括号包括的时间开始的，所以我们可以用 `firstline` 正则表达式：`^\[\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2}\]` 来配置一个多行阶段，这将匹配上面我们的异常日志的开头部分，但是不会匹配后面的异常行，直到 `Exception: Sorry, this route always breaks` 这一行日志，这些将被识别为单个日志块，在 Loki 中也是以一个日志条目出现的。

```
multiline:
  # 识别时间戳作为多行日志的第一行，注意这里字符串应该使用单引号。
  firstline: '^\[\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2}\]'

  max_wait_time: 3s
```

这个示例是假设我们对日志格式没有进行控制，所以我们需要一个更复杂的正则表达式来匹配第一行日志，但是如果我们能够控制被观察的日志格式，那么我们就可以简化第一行的匹配规则。

下面的是一个简单的 `Akka HTTP` 服务的日志：

```
[2021-01-07 14:17:43,494] [DEBUG] [akka.io.TcpListener] [HelloAkkaHttpServer-akka.actor.default-dispatcher-26] [akka://HelloAkkaHttpServer/system/IO-TCP/selectors/$a/0] - New connection accepted
[2021-01-07 14:17:43,499] [ERROR] [akka.actor.ActorSystemImpl] [HelloAkkaHttpServer-akka.actor.default-dispatcher-3] [akka.actor.ActorSystemImpl(HelloAkkaHttpServer)] - Error during processing of request: 'oh no! oh is unknown'. Completing with 500 Internal Server Error response. To change default exception handling behavior, provide a custom ExceptionHandler.
java.lang.Exception: oh no! oh is unknown
    at com.grafana.UserRoutes.$anonfun$userRoutes$6(UserRoutes.scala:28)
    at akka.http.scaladsl.server.Directive$.$anonfun$addByNameNullaryApply$2(Directive.scala:166)
    at akka.http.scaladsl.server.ConjunctionMagnet$$anon$2.$anonfun$apply$3(Directive.scala:234)
    at akka.http.scaladsl.server.directives.BasicDirectives.$anonfun$mapRouteResult$2(BasicDirectives.scala:68)
    at akka.http.scaladsl.server.directives.BasicDirectives.$anonfun$textract$2(BasicDirectives.scala:161)
    at akka.http.scaladsl.server.RouteConcatenation$RouteWithConcatenation.$anonfun$$tilde$2(RouteConcatenation.scala:47)
    at akka.http.scaladsl.util.FastFuture$.strictTransform$1(FastFuture.scala:40)
  ...
```

简单一看和其他日志一样，我们来看看日志的格式：

```
<configuration>
    <appender name="FILE" class="ch.qos.logback.core.FileAppender">
        <file>crasher.log</file>
        <append>true</append>
        <encoder>
            <pattern>&ZeroWidthSpace;[%date{ISO8601}] [%level] [%logger] [%thread] [%X{akkaSource}] - %msg%n</pattern>
        </encoder>
    </appender>

    <appender name="ASYNC" class="ch.qos.logback.classic.AsyncAppender">
        <queueSize>1024</queueSize>
        <neverBlock>true</neverBlock>
        <appender-ref ref="STDOUT" />
    </appender>

    <root level="DEBUG">
        <appender-ref ref="ASYNC"/>
    </root>

</configuration>
```

对于 Logback 配置来说，没有什么特别之处，除了在每个日志行的开头有一个 ``，这是零宽度空格的 HTML 代码，它使得识别第一行变得更加简单了，这里我们使用的第一行匹配正则表达式为：`\x{200B}\[`，`200B` 是零宽度空格字符的 Unicode 编码：

```
multiline:
  # 将零宽度的空格确定为多行块的第一行，注意该字符串应使用单引号。
  firstline: '^\x{200B}\['

  max_wait_time: 3s
```

##### template

`template` 阶段可以使用 [Go 模板语法](https://golang.org/pkg/text/template/)来操作提取的数据。模板阶段主要用于在将数据设置为标签之前对其他阶段的数据进行操作，例如用下划线替换空格，或者将大写的字符串转换为小写的字符串。模板也可以用来构建具有多个键的信息。模板阶段也可以在提取的数据中创建新的键。

配置格式如下所示：

```
template:
  # 要解析的提取数据中的名称，如果提前数据中的key不存在，将为其添加一个新的值
  source: <string>

  # 使用的 Go 模板字符串。 除了正常的模板之外
  # functions, ToLower, ToUpper, Replace, Trim, TrimLeft, TrimRight,
  # TrimPrefix, TrimSuffix, and TrimSpace 都是可以使用的函数。
  template: <string>s
```

比如下面的 pipeline 配置：

```
- template:
    source: new_key
    template: "hello world!"
```

假如还没有任何数据被添加到提取的数据中，这个阶段将首先在提取的数据 Map 中添加一个空白值的 `new_key`，然后它的值将被设置为 `hello world!`。

在看下面的模板阶段配置：

```
- template:
    source: app
    template: "{{ .Value }}_some_suffix"
```

这个 pipeline 在现有提取的数据中获取键为 app 的值，并将 `_som_suffix` 附加到值后面。例如，如果提前的数据 Map 的键为 app，值为 loki，那么这个阶段将把值从 loki 修改为 `loki_som_suffix`。

```
- template:
    source: app
    template: "{{ ToLower .Value }}"
```

这个 pipeline 从提取的数据中获取键为 app 的值，并将其值转换为小写。例如，如果提取的数据键 app 的值为 LOKI，那么这个阶段将把值转换为小写的 loki。

```
- template:
    source: output_msg
    template: "{{ .level }} for app {{ ToUpper .app }}"
```

这个 pipeline 从提取的数据中获取 `level` 与 `app` 的值，一个新的 `output_msg` 将被添加到提取的数据中，值为上面模板的计算结果。

例如，如果提取的数据中包含键为 app，值为 loki 的数据，level 的值为 warn，那么经过该阶段后会添加一个新的数据，键为 `output_msg`，其值为 `warn for app LOKI`。

任何先前提取的键都可以在模板中使用，所有提取的键都可用于模板的扩展。

```
- template:
    source: app
    template: "{{ .level }} for app {{ ToUpper .Value }} in module {{.module}}"
```

上面的这个 pipeline 从提取的数据中获取 level、app 合 module 值。例如，如果提取的数据包含值为 loki 的 app，level 的值为 warn，moudule 的值为 test，则这个阶段会将提取数据 app 的值更改为 `warn for app LOKI in module test`。

任何之前获取的键都可以在模板中使用，此外，如果 `source` 是可用的，它可以在模板中被称为 `.Value`，我们这里 app 被当成了 source，所以它可以在模板中通过 `.Value` 使用。

```
- template:
    source: app
    template: '{{ Replace .Value "loki" "blokey" 1 }}'
```

这里的模板使用 Go 的 `string.Replace`函数，当模板执行时，从提取的 Map 数据中的键为 app 的全部内容将最多有 1 个 loki 的实例被改为 blokey。

另外有一个名为 `Entry` 的特殊键可以用来引用当前行，当你需要追加或预设日志行的时候，这应该会很有用。

```
- template:
    source: message
    template: "{{.app }}: {{ .Entry }}"
- output:
    source: message
```

例如，上面的片段会在日志行前加上应用程序的名称。

> 在 Loki2.3 中，所有的 [sprig 函数](http://masterminds.github.io/sprig/)都被添加到了当前的模板阶段，包括 ToLower & ToUpper、Replace、Trim、Regex、Hash 和 Sha2Hash 函数。

#### 处理阶段

用于从以前阶段中提取数据并对其进行处理。

##### timestamp

设置日志条目的时间戳值，当时间戳阶段不存在时，日志行的时间戳默认为日志条目被抓取的时间。

配置格式如下所示：

```
timestamp:
  source: <string>

  # 解析时间字符串的格式，可以只有预定义的格式有：[ANSIC UnixDate RubyDate RFC822
  # RFC822Z RFC850 RFC1123 RFC1123Z RFC3339 RFC3339Nano Unix
  # UnixMs UnixUs UnixNs].
  format: <string>

  # 如果格式无法解析，可尝试的 fallback 的格式
  [fallback_formats: []<string>]

  # IANA 时区数据库字符串
  [location: <string>]

  # 在时间戳无法提取或解析的情况下，应采取何种行动。有效值为：[skip, fudge]，默认为 fudge。
  [action_on_failure: <string>]
```

其中的 `format` 字段可以参考格式如下所示：

- `ANSIC`: `Mon Jan \_2 15:04:05 2006`
- `UnixDate`: `Mon Jan_2 15:04:05 MST 2006`
- `RubyDate`: `Mon Jan 02 15:04:05 -0700 2006`
- `RFC822`: `02 Jan 06 15:04 MST`
- `RFC822Z`: `02 Jan 06 15:04 -0700`
- `RFC850`: `Monday, 02-Jan-06 15:04:05 MST`
- `RFC1123`: `Mon, 02 Jan 2006 15:04:05 MST`
- `RFC1123Z`: `Mon, 02 Jan 2006 15:04:05 -0700`
- `RFC3339`: `2006-01-02T15:04:05-07:00`
- `RFC3339Nano`: `2006-01-02T15:04:05.999999999-07:00`

另外支持常见的 Unix 时间戳：

- `Unix`: 1562708916 or with fractions 1562708916.000000123
- `UnixMs`: 1562708916414
- `UnixUs`: 1562708916414123
- `UnixNs`: 1562708916000000123

自定义格式是直接传递格 GO 的 `time.Parse` 函数中的 layout 参数，如果自定义格式没有指定 year，Promtail 会认为应该使用系统时钟的当前年份。

自定义格式使用的语法是使用时间戳的每个组件的特定值来定义日期和时间（例如 Mon Jan 2 15:04:05 -0700 MST 2006），下表显示了应在自定义格式中支持的参考值。

![支持的参考值](https://picdn.youdianzhishi.com/images/20210503141106.png)

`action_on_failure` 设置定义了在提取的数据中不存在 `source` 字段或时间戳解析失败的情况下，应该如何处理，支持的动作有：

- `fudge（默认）`：将时间戳更改为最近的已知时间戳，总计 1 纳秒（以保证日志顺序）
- `skip`：不改变时间戳，保留日志被 Promtail 抓取的时间

比如使用下面的 pipeline 配置：

```
- timestamp:
    source: time
    format: RFC3339Nano
```

经过上面的 timestamp 阶段在提取的数据中查找一个 time 字段，并以 `RFC3339Nano` 格式化其值（例如，2006-01-02T15:04:05.9999999-07:00），所得的时间值将作为时间戳与日志行一起发送给 Loki。

##### output

设置日志行文本，配置格式如下所示：

```
output:
  source: <string>
```

比如我们有一个如下配置的 pipeline：

```
- json:
    expressions:
      user: user
      message: message
- labels:
    user:
- output:
    source: message
```

需要收集的日志为：

```
{ "user": "alexis", "message": "hello, world!" }
```

在经过第一个 json 阶段后将提前以下键值对到数据中：

- `user`: `alexis`
- `message`: `hello, world!`

然后第二个 label 阶段将把 `user=alexis` 添加到输出的日志标签集中，最后的 output 阶段将把日志数据从原来的 JSON 更改为 message 的值 `hello, world!` 输出。

##### labels

更新日志的标签集，并一起发送给 Loki。配置格式如下所示：

```
labels:
  # Key 是必须的，是将被创建的标签名称。
  # Values 是可选的，提取的数据中的名称，其值将被用于标签的值。
  # 如果是空的，值将被推断为与键相同。
  [ <string>: [<string>] ... ]
```

比如我们有一个如下所示的 pipeline 配置：

```
- json:
    expressions:
      stream: stream
- labels:
    stream:
```

需要处理的日志数据为：

```
{
  "log": "log message\n",
  "stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z"
}
```

第一个 json 阶段将提取 `stream` 到 Map 数据中，其值为 `stderr`。然后在第二个 labels 阶段将把这个键值对变成一个标签，在发送到 Loki 的日志行中将包括标签 `stream`，值为 `stderr`。

##### metrics

根据提取的数据计算指标。需要注意的是，创建的 metrics 指标不会被推送到 Loki，而是通过 Promtail 的 `/metrics` 端点暴露出去，Prometheus 应该被配置为可以抓取 Promtail 的指标，以便能够检索这个阶段所配置的指标数据。

配置格式如下所示：

```
# 一个映射，key为metric的名称，value是特定的metric类型
metrics:
  [<string>: [ <metric_counter> | <metric_gauge> | <metric_histogram> ] ...]
```

- **metric_counter**：定义一个 Counter 类型的指标，其值只会不断增加。
- **metric_gauge**：定义一个 Gauge 类型的指标，其值可以增加或减少。
- **metric_histogram**：定义一个直方图指标。

比如我们有一个如下所示的 pipeline 配置用于定义一个 Counter 指标：

```
- metrics:
    log_lines_total:
      type: Counter
      description: "total number of log lines"
      prefix: my_promtail_custom_
      max_idle_duration: 24h
      config:
        match_all: true
        action: inc
    log_bytes_total:
      type: Counter
      description: "total bytes of log lines"
      prefix: my_promtail_custom_
      max_idle_duration: 24h
      config:
        match_all: true
        count_entry_bytes: true
        action: add
```

这个流水线先创建了一个 `log_lines_total` 的 Counter，通过使用 `match_all: true` 参数为每一个接收到的日志行增加。

然后还创建了一个 `log_bytes_total` 的 Counter 指标，通过使用 `count_entry_bytes: true` 参数，将收到的每个日志行的字节大小加入到指标中。

这两个指标如果没有收到新的数据，将在 24h 后小时。另外这些阶段应该放在 pipeline 的末端，在任何标签阶段之后。

```
- regex:
    expression: "^.*(?P<order_success>order successful).*$"
- metrics:
    successful_orders_total:
      type: Counter
      description: "log lines with the message `order successful`"
      source: order_success
      config:
        action: inc
```

比如上面这个 pipeline 首先尝试在日志中找到成功的订单，将其提取为 `order_success` 字段，然后在 metrics 阶段创建一个名为 `successful_orders_total` 的 Counter 指标，其值是在只有提取的数据中有 `order_success` 的时候才会增加。这个 pipeline 的结果是一个指标，其值只有在 Promtail 抓取的日志中带有 `order successful` 文本的日志时才会增加。

```
- regex:
    expression: "^.* order_status=(?P<order_status>.*?) .*$"
- metrics:
    successful_orders_total:
      type: Counter
      description: "successful orders"
      source: order_status
      config:
        value: success
        action: inc
    failed_orders_total:
      type: Counter
      description: "failed orders"
      source: order_status
      config:
        value: fail
        action: inc
```

上面这个 pipeline 首先会尝试在日志中找到格式为 `order_status=<value>` 的文本，将 `<value>` 提取到 `order_status` 中。该指标阶段创建了 `successful_orders_total` 和 `failed_orders_total` 指标，只有当提取数据中的 `order_status` 的值分别为 `success` 或 `fail` 时才会增加。

##### tenant

设置日志要使用的租户 ID 值，从提取数据中的一个字段获取，如果该字段缺失，将使用默认的 Promtail 客户端租户 ID。配置格式如下所示：

```
tenant:
  # source 或 value 配置选项是必须的，但二者不能同时使用（它们是互斥的）
  [ source: <string> ]

  # 当前阶段执行时用来设置租户 ID 的值。
  # 当这个阶段被包含在一个带有 "match" 的条件管道中时非常有用。
  [ value: <string> ]
```

比如我们有如下所示的 pipeline 配置：

```
pipeline_stages:
  - json:
      expressions:
        customer_id: customer_id
  - tenant:
      source: customer_id
```

需要获取的日志数据为：

```
{
  "customer_id": "1",
  "log": "log message\n",
  "stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z"
}
```

第一个 json 阶段将提取 `customer_id` 的值到 Map 中，值为 1。在第二个租户阶段将把 `X-Scope-OrgID` 请求 Header 头（Loki 用来识别租户）设置为提取的 `customer_id` 的值，也就是 1.

另外一种场景是用配置的值来覆盖租户 ID，如下所示的 pipeline 配置：

```
pipeline_stages:
  - json:
      expressions:
        app:
        message:
  - labels:
      app:
  - match:
      selector: '{app="api"}'
      stages:
        - tenant:
            value: "team-api"
  - output:
      source: message
```

需要收集的日志数据为：

```
{
  "app": "api",
  "log": "log message\n",
  "stream": "stderr",
  "time": "2019-04-30T02:12:41.8443515Z"
}
```

这个 pipeline 将：

- Decode JSON 日志
- 设置标签 `app="api"`
- 处理匹配阶段，检查 `{app="api"}` 选择器是否匹配，如果匹配了则执行子阶段，也就是这里的租户阶段，覆盖值为 `"team-api"` 的租户。

此外在处理阶段还有 `labeldrop` 阶段，它从标签集中删除标签，这些标签与日志条目一起被发送到 Loki。还有一个 `labelallow` 阶段，它只允许将所提供的标签包含在与日志条目一起发送给 Loki 的标签集中。

#### 过滤阶段

可选择应用一个阶段的子集，或根据一些条件删除日志数据。

##### match

当一个日志条目与可配置的 LogQL 流选择器和过滤表达式相匹配时，有条件地应用一组阶段或删除日志数据。配置语法格式如下所示：

```
match:
  # LogQL 流选择器合过滤表达式。
  selector: <string>

  # pipeline 名称，当定义的时候，在 pipeline_duration_seconds 直方图中创建一个额外的标签，该值与 job_name 使用下划线连接。
  [pipeline_name: <string>]

  # 决定当选择器与日志行匹配时采取什么动作。
  # 默认是 keep，当设置为 drop 时，日志将被删除，以后的指标将不会被记录。
  [action: <string> | default = "keep"]

  # 如果你指定了 `action: drop` 那么 `logentry_dropped_lines_total` 这个指标将为每一个被丢弃的行而增加
  # 默认情况下，reaseon 标签是 `match_stage`，但是你可以选择指定一个自定义值用于该指标的 `reason` 标签。

  [drop_counter_reason: <string> | default = "match_stage"]

  # 只有当选择器与日志的标签相匹配时，才会出现嵌套的流水线阶段：
  stages:
    - [
        <regex_stage>
        <json_stage> |
        <template_stage> |
        <match_stage> |
        <timestamp_stage> |
        <output_stage> |
        <labels_stage> |
        <metrics_stage> |
        <tenant_stage>
      ]
```

比如我们现在有一个如下所的 pipeline 配置：

```
pipeline_stages:
  - json:
      expressions:
        app:
  - labels:
      app:
  - match:
      selector: '{app="loki"}'
      stages:
        - json:
            expressions:
              msg: message
  - match:
      pipeline_name: "app2"
      selector: '{app="pokey"}'
      action: keep
      stages:
        - json:
            expressions:
              msg: msg
  - match:
      selector: '{app="promtail"} |~ ".*noisy error.*"'
      action: drop
      drop_counter_reason: promtail_noisy_error
  - output:
      source: msg
```

要处理的日志数据为：

```
{ "time":"2012-11-01T22:08:41+00:00", "app":"loki", "component": ["parser","type"], "level" : "WARN", "message" : "app1 log line" }
{ "time":"2012-11-01T22:08:41+00:00", "app":"promtail", "component": ["parser","type"], "level" : "ERROR", "message" : "foo noisy error" }
```

第一个 json 阶段将在第一个日志行的提取 Map 数据中添加值 `app=loki`，然后经过第二个 labels 阶段将 `app` 转换成一个标签。对于第二行日志也遵循同样的流程，只是值变成了 `promtail`。

然后在第三个 match 阶段使用 LogQL 表达式 `{app="loki"}` 进行匹配，只有在标签 `app=loki` 的时候才会执行嵌套 json 阶段，这里合我们的第一行日志是匹配的，然后嵌套的 json 阶段将 `message` 数据提取到 Map 数据中，key 变成了 `msg`，值为 `app1 log line`。

接下来执行第四个 match 阶段，需要匹配 `app="pokey"`，很显然这里我们都不匹配，所以嵌套的 json 子阶段不会被执行。

然后执行的第五个 match 阶段，将会删掉任何具有 `app="promtail"` 标签并包括 `noisy error` 文本的日志数据，并且还将增加 `logentry_drop_lines_total` 指标，标签为 `reason="promtail_noisy_error"`。

最后的 output 输出阶段将日志行的内容改为提取数据中的 msg 的值。我们这里的示例最后输出为 `app1 log line`。

##### drop

drop 阶段可以让我们根据配置来删除日志。需要注意的是，如果你提供多个选项配置，它们将被视为 `AND` 子句，其中每个选项必须为真才能删除日志。如果你想用一个 `OR`子句来删除，那么就指定多个删除阶段。配置语法格式如下所示：

```
drop:
  [source: <string>]

  # RE2 正则表达式，如果提供了 source，则会尝试匹配 source
  # 如果没有提供 source，则会尝试匹配日志行数据
  # 如果提供的正则匹配了日志行或者 source，则该行日志将被删除。
  [expression: <string>]

  # 只有在指定 source 源的情况下才能指定 value 值。
  # 指定 value 与 regex 是错误的。
  # 如果提供的值与`source`完全匹配，该行将被删除。
  [value: <string>]

  # older_than 被解析为 Go duration 格式
  # 如果日志行的时间戳大于当前时间减去所提供的时间，则将被删除
  [older_than: <duration>]

  # longer_than 是一个以 bytes 为单位的值，任何超过这个值的日志行都将被删除。
  # 可以指定为整数格式的字节数：8192，或者带后缀的 8kb
  [longer_than: <string>|<int>]

  # 每当一个日志行数据被删除，指标 `logentry_dropped_lines_total` 都会增加。
  # 默认的 reason 标签是 `drop_stage`，然而你可以选择指定一个自定义值，用于该指标的 "reason" 标签。
  [drop_counter_reason: <string> | default = "drop_stage"]
```

比如我们有一个如下所示的简单 drop 阶段配置：

```
- drop:
    expression: ".*debug.*"
```

该阶段将删除任何带有 `debug` 字样的日志行。

如果是下面的配置示例：

```
- json:
    expressions:
      level:
      msg:
- drop:
    source: "level"
    expression: "(error|ERROR)"
```

则下面的日志数据都将被删除：

```
{"time":"2019-01-01T01:00:00.000000001Z", "level": "error", "msg":"11.11.11.11 - "POST /loki/api/push/ HTTP/1.1" 200 932 "-" "Mozilla/5.0 (Windows; U; Windows NT 5.1; de; rv:1.9.1.7) Gecko/20091221 Firefox/3.5.7 GTB6"}
{"time":"2019-01-01T01:00:00.000000001Z", "level": "ERROR", "msg":"11.11.11.11 - "POST /loki/api/push/ HTTP/1.1" 200 932 "-" "Mozilla/5.0 (Windows; U; Windows NT 5.1; de; rv:1.9.1.7) Gecko/20091221 Firefox/3.5.7 GTB6"}
```

然后使用下面的配置来删除老的日志数据：

```
- json:
    expressions:
      time:
      msg:
- timestamp:
    source: time
    format: RFC3339
- drop:
    older_than: 24h
    drop_counter_reason: "line_too_old"
```

> 需要注意的是为了让 `old_than` 发挥作用，你必须在应用 drop 阶段之前，使用时间戳阶段来设置抓取日志行的时间戳。

比如当前的摄取时间为 `2021-05-01T12:00:00Z`，当从文件中读取时，会删除这个日志行：

```
{"time":"2021-05-01T12:00:00Z", "level": "error", "msg":"11.11.11.11 - "POST /loki/api/push/ HTTP/1.1" 200 932 "-" "Mozilla/5.0 (Windows; U; Windows NT 5.1; de; rv:1.9.1.7) Gecko/20091221 Firefox/3.5.7 GTB6"}
```

但是下面的日志数据不会被删除：

```
{"time":"2021-05-03T12:00:00Z", "level": "error", "msg":"11.11.11.11 - "POST /loki/api/push/ HTTP/1.1" 200 932 "-" "Mozilla/5.0 (Windows; U; Windows NT 5.1; de; rv:1.9.1.7) Gecko/20091221 Firefox/3.5.7 GTB6"}
```

在这个例子中，当前时间是 ``2021-05-03T16:00:00Z`，`older_than`是 24h。所有时间戳超过`2021-05-02T16:00:00Z` 的日志行都将被删除。

这个删除阶段删除的所有行也将增加 `logentry_drop_lines_total` 指标，并标明原因为 `"line_too_old"`。

下面是另外一个复杂点的配置：

```
- json:
    expressions:
      time:
      msg:
- timestamp:
    source: time
    format: RFC3339
- drop:
    older_than: 24h
- drop:
    longer_than: 8kb
- drop:
    source: msg
    regex: ".*trace.*"
```

上面的 pipeline 执行后将删除掉所有超过 24 小时**或者**超过 8kb 的日志**或者** json 的 msg 值中包含 `trace` 字样的日志。

### Scraping

Promtail 可以通过 YAML 文件中的 `scrape_configs` 配置来自动发现日志文件并从中提取标签，该语法与 Promtheus 中的配置比较类似。

`scrape_configs` 包含一个或多个配置条目，会对每个发现的目标执行日志抓取任务。

```
scrape_configs:
  - job_name: local
    static_configs:
      - ...

  - job_name: kubernetes
    kubernetes_sd_config:
      - ...
```

但是需要注意如果有一个以上的抓取配置与你的日志匹配了，那么可能会得到重复的日志数据，因为日志是在不同的流中发送的，可能会有不同的标签。

Promtail 中存在几种不同类型的标签：

- 以`__`(两个下划线)开头的标签是**内部标签**，它们通常来自动态数据源，比如服务发现。一旦重新打上标签，它们就会从标签集中删除，如果要保留内部标签发送到 Loki，请重新命名它们，使它们不以 `__` 开头，可以参考下面的 `Relabeling` 部分配置。
- 以 `__meta_kubernetes_pod_label_*` 开头的标签是**元标签**，是根据你的 Kubernetes Pod 的标签生成的。比如你的有一个 Pod 的标签名称是 `foobar`，那么 `scrape_configs` 部分将收到一个名为 `__meta_kubernetes_pod_label_name` 的内部标签，值会被设置为 `foobar`。
- 其他 `__meta_kubernetes_*` 开头的标签是基于其他 Kubernetes 元数据生成的，比如 Pod 的命名空间（`__meta_kubernetes_namespace`）或 Pod 内部的容器名称（`__meta_kubernetes_pod_container_name`）等等，关于 Kubernetes 元标签的完整列表，可以参考 [Prometheus 文档](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config)的说明，因为这二者实现方式是一致的。
- `__path__` 标签是一个特殊的标签，Promtail 在发现后使用它来计算要读取的文件位置，允许使用通配符，例如 `/var/log/*.log` 用于获取指定目录中所有带有 log 扩展名的文件，而 `/var/log/**/*.log` 用于递归匹配文件与目录。
- 在 `__path__` 中找到的每个文件都会添加文件名标签，以确保日志流的唯一性，它被设置为该行被读取的文件的绝对路径。

Promtail 可以使用 Kubernetes API 来发现作为目标的 Pod，但需要注意它**只能与 Promtail 运行在同一个节点上的 Pod 中读取日志**，Promtail 会在每个目标中查询一个 `__host__` 标签，并验证它是否与 Promtail 的主机名相同。

所以任何时候使用 Kubernetes 服务发现，都必须有一个 `relabel_config` 配置，从 `__meta_kubernetes_pod_node_name` 元标签创建一个中间的 `__host__` 标签。

```
relabel_configs:
  - source_labels: ["__meta_kubernetes_pod_node_name"]
    target_label: "__host__"
```

#### Relabeling

`Relabeling` 表示修改 labels 标签：添加、修改或删除。我们可以通过 `scrape_configs` 中的 `relabel_configs` 来进行 Relabel 操作。由于采用的与 Prometheus 一样的 Relabel 机制，所以操作方式与 Prometheus 是一致的。

**正则表达式**

- Prometheus 使用 `RE2` 正则表达式
- 固定的：正则表达式 `bar` 不会匹配 `foobar`
- `.*bar.*` 则不固定
- 也可以使用捕获组：`(.*)bar` 针对 `foobar` 会创建一个 `$1` 的变量，它的值是 `foo`

**正则示例**

- `prom|alert` 将会匹配 `prom` 与 `alert`
- `201[78]` 将匹配 `2017` 与 `2018`
- `promcon(20.+)` 将匹配 `promcon2020`、`promcon20xx` 等等，如果是 `promcon2018`，则 `$1` 的值为 `2018`。

`relabel_configs` 中我们可以通过配置一个 drop 操作来拒绝目标：如果标签值与指定的正则表达式匹配则被 drop 掉。当一个目标被 drop 掉，拥有的 `scrape_config` 将不会处理来自该特定来源的日志，其他没有 drop 动作的 `scrape_configs` 从同一目标读取的日志仍然可以使用并转发给 Loki。

`relabel_configs` 的一个常见用例就是将一个内部标签如 `__meta_kubernetes_*` 转换为一个中间的内部标签如 `__service__`，然后这个中间的内部标签可以根据 value 值被 drop 掉，或者转化为最终的外部标签，如 `__job__`。

**示例**

如果一个标签（例子中的 `__service__`）为空，则放弃抓取目标：

```
- action: drop
    regex: ''
    source_labels:
    - __service__
```

如果任何一个 `source_labels` 标签包含一个值，则删除抓取目标：

```
- action: drop
    regex: .+
    separator: ''
    source_labels:
    - __meta_kubernetes_pod_label_name
    - __meta_kubernetes_pod_label_app
```

通过重命名一个内部标签来持久化，这样它就会被发送到 Loki：

```
- action: replace
    source_labels:
    - __meta_kubernetes_namespace
    target_label: namespace
```

通过映射保留所有的 Kubernetes Pod 标签，比如将 `__meta_kube__meta_kubernetes_pod_label_foo` 映射为 `foo`：

```
- action: labelmap
    regex: __meta_kubernetes_pod_label_(.+)
```

## T9.5、报警

对于生产环境以及一个有追求的运维人员来说，哪怕是毫秒级别的宕机也是不能容忍的。对基础设施及应用进行适当的日志记录和监控非常有助于解决问题，还可以帮助优化成本和资源，以及帮助检测以后可能会发生的一些问题。前面我们学习使用了 Prometheus 来进行监控报警，但是如果我们使用 Loki 收集日志是否可以根据采集的日志来进行报警呢？答案是肯定的，而且有两种方式可以来实现：Promtail 中的 metrics 阶段和 Loki 的 ruler 组件。

### 测试应用

比如现在我们有一个如下所的 nginx 应用用于 Loki 日志报警：

```
# nginx-deploy.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
spec:
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:1.7.9
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: nginx
  labels:
    app: nginx
spec:
  ports:
  - name: nginx
    port: 80
    protocol: TCP
  selector:
    app: nginx
  type: NodePort
```

为方便测试，我们这里使用 NodePort 类型的服务来暴露应用，直接安装即可：

```
kubectl apply -f nginx-deploy.yaml
```

我们可以通过如下命令来来模拟每隔10s访问 Nginx 应用：

```
$ while true; do curl --silent --output /dev/null --write-out '%{http_code}' http://192.168.31.75:32096/; sleep 10; echo; done
200
200
```

### metrics 阶段

前面我们提到在 Promtail 中通过一系列 Pipeline 来处理日志，其中就包括一个 metrics 的阶段，可以根据我们的需求来增加一个监控指标，这就是我们需要实现的基于日志的监控报警的核心点，通过结构化日志，增加监控指标，然后使用 Prometheus 结合 Alertmanager 完成之前我们非常熟悉的监控报警。

首先我们需要安装 Prometheus 与 Alertmanager，可以手动安装，也可以使用 Prometheus Operator 的方式，可以参考[监控报警](https://www.qikqiak.com/monitor/prometheus/)章节相关内容，比如这里我们选择使用 Prometheus Operator 的方式。

前面我们已经使用 `loki-stack` 这个 Helm Chart 安装了 Loki，接下来我们需要重新更新用于安装的 Values 文件：

```
# values-prod.yaml
loki:
  enabled: true
  persistence:
    enabled: true
    accessModes:
    - ReadWriteOnce
    size: 2Gi
    storageClassName: nfs-storage
promtail:
  enabled: true
  serviceMonitor:
    enabled: true
    additionalLabels:
      app: prometheus-operator
      release: prometheus
  pipelineStages:
  - docker: {}
  - match:
      selector: '{app="nginx"}'
      stages:
      - regex:
          expression: '.*(?P<hits>GET /.*)'
      - metrics:
          nginx_hits:
            type: Counter
            description: "Total nginx requests"
            source: hits
            config:
              action: inc
grafana:
  enabled: true
  service:
    type: NodePort
  persistence:
    enabled: true
    storageClassName: nfs-storage
    accessModes:
      - ReadWriteOnce
    size: 1Gi
```

上面最重要的部分就是为 Promtail 添加了 `pipelineStages` 配置，用于对日志行进行转换，在这里我们添加了一个 `match` 的阶段，会去匹配具有 `app=nginx` 这样的日志流数据，然后下一个阶段是利用正则表达式过滤出包含 GET 关键字的日志行。

在 metrics 指标阶段，我们定义了一个 `nginx_hits` 的指标，Promtail 通过其 `/metrics` 端点暴露这个自定义的指标数据。这里我们定义的是一个 `Counter` 类型的指标，当从 regex 阶段匹配上后，这个计数器就会递增。

为了在 Prometheus 中n能够这个指标，我们通过 `promtail.serviceMonitor.enable=true` 开启了一个 ServiceMonitor。接下来重新更新 Loki 应用，使用如下所示的命令即可：

```
helm upgrade --install loki -n logging -f values-prod.yaml .
```

更新完成后会创建一个 ServiceMonitor 对象用于发现 Promtail 的指标数据：

```
$ kubectl get servicemonitor -n logging
NAME            AGE
loki-promtail   17m
```

如果你使用的 Prometheus-Operator 默认不能发现 logging 命名空间下面的数据，则需要创建如下所示的一个 Role 权限：

```
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  labels:
    app.kubernetes.io/component: prometheus
    app.kubernetes.io/name: prometheus
    app.kubernetes.io/part-of: kube-prometheus
    app.kubernetes.io/version: 2.26.0
  name: prometheus-k8s
  namespace: logging
rules:
- apiGroups:
  - ""
  resources:
  - services
  - endpoints
  - pods
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - extensions
  resources:
  - ingresses
  verbs:
  - get
  - list
  - watch
- apiGroups:
  - networking.k8s.io
  resources:
  - ingresses
  verbs:
  - get
  - list
  - watch
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: prometheus-k8s
  namespace: logging
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: prometheus-k8s
subjects:
- kind: ServiceAccount
  name: prometheus-k8s
  namespace: monitoring
```

正常在 Prometheus 里面就可以看到 Promtail 的抓取目标了：

![promtail targets](https://picdn.youdianzhishi.com/images/20210515153513.png)

如果你使用的是 Prometheus Operator 自带的 Grafana，则需要手头添加上 Loki 的数据源：

![添加Loki](https://picdn.youdianzhishi.com/images/20210515154149.png)

当然如果你直接使用 loki-stack 中的 Grafana 则不需要，总之现在当我们访问测试应用的时候，在 Loki 中是可以查看到日志数据的：

![日志数据](https://picdn.youdianzhishi.com/images/20210515154332.png)

而且现在在 Prometheus 中还可以查询到我们在 Promtail 中添加的 metrics 指标数据：

![监控指标](https://picdn.youdianzhishi.com/images/20210515154538.png)

接下来我们就可以根据我们的需求来创建报警规则了，由于我们这里使用的 Prometheus Operator，所以可以直接创建一个 PrometheusRule 资源对象即可：

```
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  labels:
    prometheus: k8s
    role: alert-rules
  name: promtail-nginx-hits
  namespace: monitoring
spec:
  groups:
    - name: nginx-hits
      rules:
        - alert: LokiNginxHits
          annotations:
            summary: nginx hits counter
            description: 'nginx_hits total insufficient count ({{ $value }}).'
          expr: |
            sum(increase(promtail_custom_nginx_hits[1m])) > 2
          for: 2m
          labels:
            severity: critical
```

这里我们配置了名为 nginx_hits 的报警规则，这些规则在同一个分组中，每隔一定的时间间隔依次执行。触发报警的阈值通过 `expr` 表达式进行配置。我们这里表示的是1分钟之内新增的总和是否大于2，当 `expor` 表达式的条件持续了2分钟时间后，报警就会真正被触发，报警真正被触发之前会保持为 Pending 状态。

![prometheus rules](https://picdn.youdianzhishi.com/images/20210515155346.png)

然后具体想要把报警发送到什么地方去，可以根据标签去配置 receiver，比如可以通过 WebHook 来接收。我们在 AlertManager 中也是可以看到接收到的报警事件的。

![报警事件](https://picdn.youdianzhishi.com/images/20210515160213.png)

### Ruler 组件

上面的方式虽然可以实现我们的日志报警功能，但是还是不够直接，需要通过 Promtail 去进行处理，那么我们能否直接通过 Loki 来实现报警功能呢？其实在 Loki2.0 版本就提供了报警功能，其中有一个 Ruler 组件可以持续查询一个 rules 规则，并将超过阈值的事件推送给 AlertManager 或者其他 Webhook 服务，这也就是 Loki 自带的报警功能了，而且是兼容 AlertManager 的。

首先我们需要开启 Loki Ruler 组件，同样更新 loki-stack 安装的 Values 文件（可以去掉 metrics 阶段的方式）：

```
# values-prod.yaml
loki:
  enabled: true
  persistence:
    enabled: true
    accessModes:
    - ReadWriteOnce
    size: 2Gi
    storageClassName: nfs-storage
  # Needed for Alerting: https://grafana.com/docs/loki/latest/alerting/
  config:
    ruler:
      # rules规则存储
      # 主要支持本地存储（local）和对象文件系统（azure, gcs, s3, swift）
      storage:
        type: local
        local:
          directory: /rules
      rule_path: /tmp/scratch  # rules临时规则文件存储路径
      alertmanager_url: http://alertmanager-main.monitoring.svc:9093  # alertmanager地址
      ring:  # ruler服务的一致性哈希环配置，用于支持多实例和分片
        kvstore:
          store: inmemory
      enable_api: true
  # 配置报警规则
  alerting_groups:
  - name: nginx-rate
    rules:
    - alert: LokiNginxRate
      expr: sum(rate({app="nginx"} |= "error" [1m])) by (job)
            /
          sum(rate({app="nginx"}[1m])) by (job)
            > 0.01
      for: 1m
      labels:
        severity: critical
      annotations:
        summary: loki nginx rate
        description: high request latency

promtail:
  enabled: true

grafana:
  enabled: true
  service:
    type: NodePort
  persistence:
    enabled: true
    storageClassName: nfs-storage
    accessModes:
      - ReadWriteOnce
    size: 1Gi
```

我们首先通过 `loki.config.ruler` 对 Ruler 组件进行配置，比如指定 Alertmanager 的地址，规则存储方式等，然后通过 `loki.alerting_groups` 配置了报警规则，Loki 的 rulers 规则和结构与 Prometheus 是完全兼容，唯一的区别在于查询语句（LogQL）不同，在Loki中我们用 `LogQL` 来查询日志，一个典型的 rules 配置文件如下所示：

```
groups:
  # 组名称
  - name: xxxx
    rules:
      # Alert名称
      - alert: xxxx
        # logQL查询语句
        expr: xxxx
        # 产生告警的持续时间 pending.
        [ for:  | default = 0s ]
        # 自定义告警事件的label
        labels:
        [ :  ]
        # 告警时间的注释
        annotations:
        [ :  ]
```

比如我们这里配置的规则 `sum(rate({app="nginx"} |= "error" [1m])) by (job) / sum(rate({app="nginx"}[1m])) by (job) > 0.01` 表示通过日志查到 nginx 日志的错误率大于1%就触发告警，同样重新使用上面的 values 文件更新 Loki：

![logql 查询](https://picdn.youdianzhishi.com/images/20210515164732.png)

更新完成后我们查看 Loki 的日志可以看到一些关于上面我们配置的报警规则的信息：

```
$ kubectl logs -f loki-0 -n logging
......
level=info ts=2021-05-15T08:52:48.25436331Z caller=metrics.go:83 org_id=..data traceID=7a526c23619c6b4e latency=fast query="sum by(job)(rate({app=\"nginx\"} |= \"error\"[1m])) / sum by(job)(rate({app=\"nginx\"}[1m])) > 0.01" query_type=metric range_type=instant length=0s step=0s duration=6.615062ms status=200 throughput=2.3MB total_bytes=15kB
level=info ts=2021-05-15T08:53:08.271608857Z caller=metrics.go:83 org_id=..2021_05_15_08_49_25.017497657 traceID=2d7b255ccae2692e latency=fast query="sum by(job)(rate({app=\"nginx\"} |= \"error\"[1m])) / sum by(job)(rate({app=\"nginx\"}[1m])) > 0.01" query_type=metric range_type=instant length=0s step=0s duration=55.011001ms status=200 throughput=297kB total_bytes=16kB
```

同样在`1m`之内如果持续超过阈值，则会真正触发报警规则，触发后我们在 Alertmanager 也可以看到对应的报警信息了：

![alertmanager 报警](https://picdn.youdianzhishi.com/images/20210515165610.png)

到这里我们就完成了使用 Loki 基于日志的监控报警。

## T9.6、LogQL

受 PromQL 的启发，Loki 也有自己的查询语言，称为 LogQL，它就像一个分布式的 grep，可以聚合查看日志。和 PromQL 一样，LogQL 也是使用标签和运算符进行过滤的，主要有两种类型的查询功能：

- 查询返回日志行内容
- 通过过滤规则在日志流中计算相关的度量指标

### 日志查询

一个基本的日志查询由两部分组成。

- `log stream selector`（日志流选择器）
- `log pipeline`（日志管道）

![log stream selector](https://picdn.youdianzhishi.com/images/20210518145747.png)

由于 Loki 的设计，所有 LogQL 查询必须包含一个日志流选择器。

**日志流选择器**决定了有多少日志流（日志内容的唯一来源，如文件）将被搜索到，一个更细粒度的日志流选择器将搜索到流的数量减少到一个可管理的数量。所以传递给日志流选择器的标签将影响查询执行的性能。

而日志流选择器后面的**日志管道**是可选的，日志管道是一组阶段表达式，它们被串联在一起应用于所过滤的日志流，每个表达式都可以过滤、解析和改变日志行内容以及各自的标签。

下面的例子显示了一个完整的日志查询的操作：

```
{container="query-frontend",namespace="loki-dev"} |= "metrics.go" | logfmt | duration > 10s and throughput_mb < 500
```

该查询语句由以下几个部分组成：

- 一个日志流选择器 `{container="query-frontend",namespace="loki-dev"}`，用于过滤 `loki-dev` 命名空间下面的 `query-frontend` 容器的日志
- 然后后面跟着一个日志管道 `|= "metrics.go" | logfmt | duration > 10s and throughput_mb < 500`，这管道表示将筛选出包含 `metrics.go` 这个词的日志，然后解析每一行日志提取更多的表达并进行过滤

> 为了避免转义特色字符，你可以在引用字符串的时候使用单引号，而不是双引号，比如 `\w+1` 与 "\w+" 是相同的。

### Log Stream Selector

日志流选择器决定了哪些日志流应该被包含在你的查询结果中，选择器由**一个或多个键值对**组成，其中每个键是一个**日志标签**，每个值是该标签的值。

日志流选择器是通过将键值对包裹在一对大括号中编写的，比如：

```
{app="mysql",name="mysql-backup"}
```

上面这个示例表示，所有标签为 app 且其值为 mysql 和标签为 name 且其值为 mysql-backup 的日志流将被包括在查询结果中。

其中标签名后面的 `=` 运算符是一个标签匹配运算符，LogQL 中一共支持以下几种标签匹配运算符：

- `=`: 完全匹配
- `!=`: 不相等
- `=~`: 正则表达式匹配
- `!~`: 正则表达式不匹配

例如：

- `{name=~"mysql.+"}`
- `{name!~"mysql.+"}`
- `{name!~"mysql-\\d+"}`

适用于 [Prometheus 标签选择器](https://prometheus.io/docs/prometheus/latest/querying/basics/#instant-vector-selectors)的规则同样适用于 Loki 日志流选择器。

**偏移量修饰符**

偏移修饰符允许改变查询中范围向量的时间偏移。例如，以下表达式对 MySQL 作业的最近 10 分钟到 5 分钟（而不是最近 5 分钟）内的所有日志进行计数。注意，偏移量修饰符总是需要紧跟在范围向量选择器之后。

### Log Pipeline

日志管道可以附加到日志流选择器上，以进一步处理和过滤日志流。 它通常由一个或多个表达式组成，每个表达式针对每个日志行依次执行。 如果一个表达式过滤掉了日志行，则管道将在此处停止并开始处理下一行。一些表达式可以改变日志内容和各自的标签，然后可用于进一步过滤和处理后续表达式或指标查询。

一个日志管道可以由以下部分组成。

- 日志行过滤表达式
- 解析器表达式
- 标签过滤表达式
- 日志行格式化表达式
- 标签格式化表达式
- Unwrap 表达式

其中 unwrap 表达式是一个特殊的表达式，只能在度量查询中使用。

#### 日志行过滤表达式

日志行过滤表达式用于对匹配日志流中的聚合日志进行分布式 grep。

编写入日志流选择器后，可以使用一个**搜索表达式**进一步过滤得到的日志数据集，搜索表达式可以是文本或正则表达式，比如：

- `{job="mysql"} |= "error"`
- `{name="kafka"} |~ "tsdb-ops.*io:2003"`
- `{name="cassandra"} |~ "error=\\w+"`
- `{instance=~"kafka-[23]",name="kafka"} != "kafka.server:type=ReplicaManager"`

上面示例中的 `|=`、`|~` 和 `!=` 是**过滤运算符**，支持下面几种：

- `|=`：日志行包含的字符串
- `!=`：日志行不包含的字符串
- `|~`：日志行匹配正则表达式
- `!~`：日志行与正则表达式不匹配

过滤运算符可以是链式的，并将按顺序过滤表达式，产生的日志行必须满足每个过滤器，比如：

```
{job="mysql"} |= "error" != "timeout"
```

当使用 `|~`和 `!~` 时，可以使用 Golang 的 RE2 语法的正则表达式，默认情况下，匹配是区分大小写的，可以用 `(?i)` 作为正则表达式的前缀，切换为不区分大小写。

虽然日志行过滤表达式可以放在管道的任何地方，但最好把它们放在开头，这样可以提高查询的性能，当某一行匹配时才做进一步的后续处理。例如，虽然结果是一样的，但下面的查询 `{job="mysql"}|="error"|json | line_format "{{.err}}"` 会比 `{job="mysql"} | json | line_format "{{.message}}" |= "error"` 更快，**日志行过滤表达式是继日志流选择器之后过滤日志的最快方式**。

#### 解析器表达式

解析器表达式可以解析和提取日志内容中的标签，这些提取的标签可以用于标签过滤表达式进行过滤，或者用于指标聚合。

提取的标签键将由解析器进行自动格式化，以遵循 Prometheus 指标名称的约定（它们只能包含 ASCII 字母和数字，以及下划线和冒号，不能以数字开头）。

例如下面的日志经过管道 `| json` 将产生以下 Map 数据：

```
{ "a.b": { "c": "d" }, "e": "f" }
```

->

```
{a_b_c="d", e="f"}
```

在出现错误的情况下，例如，如果该行不是预期的格式，该日志行不会被过滤，而是会被添加一个新的 `__error__` 标签。

需要注意的是如果一个提取的标签键名已经存在于原始日志流中，那么提取的标签键将以 `_extracted` 作为后缀，以区分两个标签，你可以使用一个标签格式化表达式来强行覆盖原始标签，但是如果一个提取的键出现了两次，那么只有最新的标签值会被保留。

目前支持 `json`、`logfmt`、`regexp` 和 `unpack` 这几种解析器。

我们应该尽可能使用 `json` 和 `logfmt` 等预定义的解析器，这会更加容易，而当日志行结构异常时，可以使用 `regexp`，可以在同一日志管道中使用多个解析器，这在你解析复杂日志时很有用。

##### JSON

json 解析器有两种模式运行。

- 1. 没有参数。

如果日志行是一个有效的 json 文档，在你的管道中添加 `| json` 将提取所有 json 属性作为标签，嵌套的属性会使用 `_` 分隔符被平铺到标签键中。

> 注意：数组会被忽略。

例如，使用 json 解析器从以下文件内容中提取标签。

```
{
  "protocol": "HTTP/2.0",
  "servers": ["129.0.1.1", "10.2.1.3"],
  "request": {
    "time": "6.032",
    "method": "GET",
    "host": "foo.grafana.net",
    "size": "55",
    "headers": {
      "Accept": "*/*",
      "User-Agent": "curl/7.68.0"
    }
  },
  "response": {
    "status": 401,
    "size": "228",
    "latency_seconds": "6.031"
  }
}
```

可以得到如下所示的标签列表：

```
"protocol" => "HTTP/2.0"
"request_time" => "6.032"
"request_method" => "GET"
"request_host" => "foo.grafana.net"
"request_size" => "55"
"response_status" => "401"
"response_size" => "228"
"response_latency_seconds" => "6.031"
```

- 1. 带参数的

在你的管道中使用 `|json label="expression", another="expression"` 将只提取指定的 json 字段为标签，你可以用这种方式指定一个或多个表达式，与 `label_format` 相同，所有表达式必须加引号。

当前仅支持字段访问（`my.field`, `my["field"]`）和数组访问（`list[0]`），以及任何级别嵌套中的这些组合（`my.list[0]["field"]`）。

例如，`|json first_server="servers[0]", ua="request.headers[\"User-Agent\"]` 将从以下日志文件中提取标签：

```
{
  "protocol": "HTTP/2.0",
  "servers": ["129.0.1.1", "10.2.1.3"],
  "request": {
    "time": "6.032",
    "method": "GET",
    "host": "foo.grafana.net",
    "size": "55",
    "headers": {
      "Accept": "*/*",
      "User-Agent": "curl/7.68.0"
    }
  },
  "response": {
    "status": 401,
    "size": "228",
    "latency_seconds": "6.031"
  }
}
```

提取的标签列表为：

```
"first_server" => "129.0.1.1"
"ua" => "curl/7.68.0"
```

如果表达式返回一个数组或对象，它将以 json 格式分配给标签。例如，`|json server_list="services", headers="request.headers` 将提取到如下标签：

```
"server_list" => `["129.0.1.1","10.2.1.3"]`
"headers" => `{"Accept": "*/*", "User-Agent": "curl/7.68.0"}`
```

##### logfmt

`logfmt` 解析器可以通过使用 `|logfmt` 来添加，它将从 logfmt 格式的日志行中提前所有的键和值。

例如，下面的日志行数据：

```
at=info method=GET path=/ host=grafana.net fwd="124.133.124.161" service=8ms status=200
```

将提取得到如下所示的标签：

```
"at" => "info"
"method" => "GET"
"path" => "/"
"host" => "grafana.net"
"fwd" => "124.133.124.161"
"service" => "8ms"
"status" => "200"
```

##### regexp

与 `logfmt` 和 `json`（它们隐式提取所有值且不需要参数）不同，`regexp` 解析器采用单个参数 `| regexp "<re>"` 的格式，其参数是使用 Golang RE2 语法的正则表达式。

正则表达式必须包含至少一个命名的子匹配（例如`(?P<name>re)`），每个子匹配项都会提取一个不同的标签。

例如，解析器 `| regexp "(?P<method>\\w+) (?P<path>[\\w|/]+) \\((?P<status>\\d+?)\\) (?P<duration>.*)"` 将从以下行中提取标签：

```
POST /api/prom/api/v1/query_range (200) 1.5s
```

提取的标签为：

```
"method" => "POST"
"path" => "/api/prom/api/v1/query_range"
"status" => "200"
"duration" => "1.5s"
```

##### unpack

`unpack` 解析器将解析 json 日志行，并通过打包阶段解开所有嵌入的标签，一个特殊的属性 `_entry` 也将被用来替换原来的日志行。

例如，使用 `| unpack` 解析器，可以得到如下所示的标签：

```
{
  "container": "myapp",
  "pod": "pod-3223f",
  "_entry": "original log message"
}
```

允许提取 `container` 和 `pod` 标签以及原始日志信息作为新的日志行。

> 如果原始嵌入的日志行是特定的格式，你可以将 unpack 与 json 解析器（或其他解析器）相结合使用。

#### 标签过滤表达式

标签过滤表达式允许使用其原始和提取的标签来过滤日志行，它可以包含多个谓词。

一个谓词包含一个标签标识符、操作符和用于比较标签的值。

例如 `cluster="namespace"` 其中的 `cluster` 是标签标识符，操作符是 `=`，值是`"namespace"`。

LogQL 支持从查询输入中自动推断出的多种值类型：

- `String（字符串）`用双引号或反引号引起来，例如`"200"`或`us-central1`。
- `Duration（时间）`是一串十进制数字，每个数字都有可选的数和单位后缀，如 `"300ms"`、`"1.5h"` 或 `"2h45m"`，有效的时间单位是 `"ns"`、`"us"`（或 `"µs"`）、`"ms"`、`"s"`、`"m"`、`"h"`。
- `Number（数字）`是浮点数（64 位），如 250、89.923。
- `Bytes（字节）`是一串十进制数字，每个数字都有可选的数和单位后缀，如 `"42MB"`、`"1.5Kib"` 或 `"20b"`，有效的字节单位是 `"b"`、`"kib"`、`"kb"`、`"mib"`、`"mb"`、`"gib"`、`"gb"`、`"tib"`、`"tb"`、`"pib"`、`"bb"`、`"eb"`。

字符串类型的工作方式与 Prometheus 标签匹配器在日志流选择器中使用的方式完全一样，这意味着你可以使用同样的操作符（`=`、`!=`、`=~`、`!~`）。

使用 Duration、Number 和 Bytes 将在比较前转换标签值，并支持以下比较器。

- `==` 或 `=` 相等比较
- `!=` 不等于比较
- `>` 和 `>=` 用于大于或大于等于比较
- `<` 和 `<=` 用于小于或小于等于比较

例如 `logfmt | duration > 1m and bytes_consumed > 20MB` 过滤表达式。

如果标签值的转换失败，日志行就不会被过滤，而会添加一个 `__error__` 标签，要过滤这些错误，请看管道错误部分。

你可以使用 `and`和 `or` 来连接多个谓词，它们分别表示**且**和**或**的二进制操作，`and` 可以用逗号、空格或其他管道来表示，标签过滤器可以放在日志管道的任何地方。

以下所有的表达式都是等价的:

```
| duration >= 20ms or size == 20kb and method!~"2.."
| duration >= 20ms or size == 20kb | method!~"2.."
| duration >= 20ms or size == 20kb,method!~"2.."
| duration >= 20ms or size == 20kb method!~"2.."
```

默认情况下，多个谓词的优先级是从右到左，你可以用圆括号包装谓词，强制使用从左到右的不同优先级。

例如，以下内容是等价的：

```
| duration >= 20ms or method="GET" and size <= 20KB
| ((duration >= 20ms or method="GET") and size <= 20KB)
```

它将首先评估 `duration>=20ms or method="GET"`，要首先评估 `method="GET" and size<=20KB`，请确保使用适当的括号，如下所示。

```
| duration >= 20ms or (method="GET" and size <= 20KB)
```

#### 日志行格式表达式

日志行格式化表达式可以通过使用 Golang 的 `text/template` 模板格式重写日志行的内容，它需要一个字符串参数 `| line_format "{{.label_name}}"` 作为模板格式，所有的标签都是注入模板的变量，可以用 `{{.label_name}}` 的符号来使用。

例如，下面的表达式：

```
{container="frontend"} | logfmt | line_format "{{.query}} {{.duration}}"
```

将提取并重写日志行，只包含 `query` 和请求的 `duration`。你可以为模板使用双引号字符串或反引号 `{{.label_name}}` 来避免转义特殊字符。

此外 `line_format` 也支持数学函数，例如：

如果我们有以下标签 `ip=1.1.1.1`, `status=200` 和 `duration=3000(ms)`, 我们可以用 `duration` 除以 1000 得到以秒为单位的值：

```
{container="frontend"} | logfmt | line_format "{{.ip}} {{.status}} {{div .duration 1000}}"
```

上面的查询将得到的日志行内容为`1.1.1.1 200 3`。

#### 标签格式表达式

`| label_format`表达式可以重命名、修改或添加标签，它以逗号分隔的操作列表作为参数，可以同时进行多个操作。

当两边都是标签标识符时，例如 `dst=src`，该操作将把 `src` 标签重命名为 `dst`。

左边也可以是一个模板字符串，例如 `dst="{{.status}} {{.query}}"`，在这种情况下，`dst` 标签值会被 Golang 模板执行结果所取代，这与 `| line_format` 表达式是同一个模板引擎，这意味着标签可以作为变量使用，也可以使用同样的函数列表。

在上面两种情况下，如果目标标签不存在，那么就会创建一个新的标签。

重命名形式 `dst=src` 会在将 `src` 标签重新映射到 `dst` 标签后将其删除，然而，模板形式将保留引用的标签，例如 `dst="{{.src}}"` 的结果是 `dst` 和 `src` 都有相同的值。

> 一个标签名称在每个表达式中只能出现一次，这意味着 `| label_format foo=bar,foo="new"` 是不允许的，但你可以使用两个表达式来达到预期效果，比如 `| label_format foo=bar | label_format foo="new"`。

### 查询示例

**多重过滤**

过滤应该首先使用标签匹配器，然后是行过滤器，最后使用标签过滤器：

```
{cluster="ops-tools1", namespace="loki-dev", job="loki-dev/query-frontend"} |= "metrics.go" !="out of order" | logfmt | duration > 30s or status_code!="200"
```

**多解析器**

比如要提取以下格式日志行的方法和路径：

```
level=debug ts=2020-10-02T10:10:42.092268913Z caller=logging.go:66 traceID=a9d4d8a928d8db1 msg="POST /api/prom/api/v1/query_range (200) 1.5s"
```

你可以像下面这样使用多个解析器：

```
{job="cortex-ops/query-frontend"} | logfmt | line_format "{{.msg}}" | regexp "(?P<method>\\w+) (?P<path>[\\w|/]+) \\((?P<status>\\d+?)\\) (?P<duration>.*)"`
```

首先通过 `logfmt` 解析器提取日志中的数据，然后使用 `| line_format` 重新将日志格式化为 `POST /api/prom/api/v1/query_range (200) 1.5s`，然后紧接着就是用 `regexp` 解析器通过正则表达式来匹配提前标签了。

**格式化**

下面的查询显示了如何重新格式化日志行，使其更容易阅读。

```
{cluster="ops-tools1", name="querier", namespace="loki-dev"}
  |= "metrics.go" != "loki-canary"
  | logfmt
  | query != ""
  | label_format query="{{ Replace .query \"\\n\" \"\" -1 }}"
  | line_format "{{ .ts}}\t{{.duration}}\ttraceID = {{.traceID}}\t{{ printf \"%-100.100s\" .query }} "
```

其中的 `label_format` 用于格式化查询，而 `line_format` 则用于减少信息量并创建一个表格化的输出。比如对于下面的日志行数据：

```
level=info ts=2020-10-23T20:32:18.094668233Z caller=metrics.go:81 org_id=29 traceID=1980d41501b57b68 latency=fast query="{cluster=\"ops-tools1\", job=\"cortex-ops/query-frontend\"} |= \"query_range\"" query_type=filter range_type=range length=15m0s step=7s duration=650.22401ms status=200 throughput_mb=1.529717 total_bytes_mb=0.994659
level=info ts=2020-10-23T20:32:18.068866235Z caller=metrics.go:81 org_id=29 traceID=1980d41501b57b68 latency=fast query="{cluster=\"ops-tools1\", job=\"cortex-ops/query-frontend\"} |= \"query_range\"" query_type=filter range_type=range length=15m0s step=7s duration=624.008132ms status=200 throughput_mb=0.693449 total_bytes_mb=0.432718
```

经过上面的查询过后可以得到如下所示的结果：

```
2020-10-23T20:32:18.094668233Z  650.22401ms     traceID = 1980d41501b57b68  {cluster="ops-tools1", job="cortex-ops/query-frontend"} |= "query_range"
2020-10-23T20:32:18.068866235Z  624.008132ms    traceID = 1980d41501b57b68  {cluster="ops-tools1", job="cortex-ops/query-frontend"} |= "query_range"
```

### 日志度量

LogQL 同样支持通过函数方式将日志流进行度量，通常我们可以用它来计算消息的错误率或者排序一段时间内的应用日志输出 Top N。

#### 区间向量

LogQL 同样也支持有限的区间向量度量语句，使用方式和 PromQL 类似，常用函数主要是如下 4 个：

- `rate`: 计算每秒的日志条目
- `count_over_time`: 对指定范围内的每个日志流的条目进行计数
- `bytes_rate`: 计算日志流每秒的字节数
- `bytes_over_time`: 对指定范围内的每个日志流的使用的字节数

比如计算 nginx 的 qps：

```
rate({filename="/var/log/nginx/access.log"}[5m]))
```

计算 kernel 过去 5 分钟发生 oom 的次数：

```
count_over_time({filename="/var/log/message"} |~ "oom_kill_process" [5m]))
```

#### 聚合函数

LogQL 也支持聚合运算，我们可用它来聚合单个向量内的元素，从而产生一个具有较少元素的新向量，当前支持的聚合函数如下：

- `sum`：求和
- `min`：最小值
- `max`：最大值
- `avg`：平均值
- `stddev`：标准差
- `stdvar`：标准方差
- `count`：计数
- `bottomk`：最小的 k 个元素
- `topk`：最大的 k 个元素

聚合函数我们可以用如下表达式描述：

```
<aggr-op>([parameter,] <vector expression>) [without|by (<label list>)]
```

对于需要对标签进行分组时，我们可以用 `without` 或者 `by` 来区分。比如计算 nginx 的 qps，并按照 pod 来分组：

```
sum(rate({filename="/var/log/nginx/access.log"}[5m])) by (pod)
```

只有在使用 `bottomk` 和 `topk` 函数时，我们可以对函数输入相关的参数。比如计算 nginx 的 qps 最大的前 5 个，并按照 pod 来分组：

```
topk(5,sum(rate({filename="/var/log/nginx/access.log"}[5m])) by (pod)))
```

#### 二元运算

##### 数学计算

Loki 存的是日志，都是文本，怎么计算呢？显然 LogQL 中的数学运算是面向区间向量操作的，LogQL 中的支持的二进制运算符如下：

- `+`：加法
- `-`：减法
- `*`：乘法
- `/`：除法
- `%`：求模
- `^`：求幂

比如我们要找到某个业务日志里面的错误率，就可以按照如下方式计算：

```
sum(rate({app="foo", level="error"}[1m])) / sum(rate({app="foo"}[1m]))
```

##### 逻辑运算

集合运算仅在区间向量范围内有效，当前支持

- `and`：并且
- `or`：或者
- `unless`：排除

比如：

```
rate({app=~"foo|bar"}[1m]) and rate({app="bar"}[1m])
```

##### 比较运算

LogQL 支持的比较运算符和 PromQL 一样，包括：

- `==`：等于
- `!=`：不等于
- `>`：大于
- `>=`: 大于或等于
- `<`：小于
- `<=`: 小于或等于

通常我们使用区间向量计算后会做一个阈值的比较，这对应告警是非常有用的，比如统计 5 分钟内 error 级别日志条目大于 10 的情况：

```
count_over_time({app="foo", level="error"}[5m]) > 10
```

我们也可以通过布尔计算来表达，比如统计 5 分钟内 error 级别日志条目大于 10 为真，反正则为假：

```
count_over_time({app="foo", level="error"}[5m]) > bool 10
```

#### 注释

LogQL 查询可以使用 `#` 字符进行注释，例如：

```
{app="foo"} # anything that comes after will not be interpreted in your query
```

对于多行 LogQL 查询，可以使用 `#` 排除整个或部分行：

```
{app="foo"}
    | json
    # this line will be ignored
    | bar="baz" # this checks if bar = "baz"
```






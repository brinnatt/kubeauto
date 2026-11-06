# Elasticsearch 用户与运维手册

## 1. 适用范围与任务入口

本手册面向 Kubernetes 平台管理员、搜索业务开发者和数据库运维人员，版本为 ECK 3.5.0、Elasticsearch 9.5.6、可选 Kibana 9.5.6、Kubernetes 1.33.6。Basic 为默认许可；业务集群与 EFK 数据面独立，共用一个经过身份校验的 ECK。当前交付资格以[专项矩阵](../../../tests/elasticsearch-test-matrix.yaml)及其绑定证据为准。

首次部署从[规划与前置检查](#2-规划与上线前检查)连续执行至[上线验收](#5-控制面数据面与上线验收)；应用开发使用[业务建模与客户端](#6-业务建模与客户端)；常用巡检、扩容和维护见[日常运维](#7-日常运维容量与性能)；安全变更见[证书与凭据轮换](#8-证书与凭据轮换)；数据保护见[快照恢复与灾备](#9-快照恢复与灾备)；生命周期见[升级与卸载](#10-升级回旧版与卸载)；[故障入口与参数速查](#11-故障入口与参数速查)供日常检索。原理与选择依据见[技术白皮书](technical-whitepaper.md)，变量和制品的维护边界见[开发手册](development-manual.md)。

所有部署命令在已配置 kubeauto、固定二进制和密钥认证的控制机上，由有权管理目标集群的管理员在 root 会话中执行；采用 sudo 的客户先按自己的授权流程进入该会话。工作目录为 `/usr/local/kubeauto`，客户先选择实际集群名并核验 kubeconfig。这样才能写入产品部署目录、读取受控凭据和创建 `/run` 下的私有诊断目录；应用客户端示例另在应用身份和运行环境中执行。不要使用开发机或不属于该客户环境的地址代替产品节点。

## 2. 规划与上线前检查

推荐 `standard`：三个专用 master 和至少三个 data/ingest，分配到分开的宿主节点集合；`compact`：三个混合角色成员，适合经过容量评估的小规模业务。两种形态都要求三个独立节点故障域，不提供单节点或双节点生产形态。control-plane 节点默认不作为 ES 调度目标；实验室显式容忍 control-plane 污点的结果不能作为生产隔离建议。

| 项目 | 起步配置与准入条件 | 失败时处理 |
| --- | --- | --- |
| master | 每 Pod 2 CPU/4Gi，20Gi PVC | 配足选主与元数据余量，资源不足停止安装 |
| data | 每 Pod 4 CPU/16Gi、heap 8g，200Gi PVC | 按数据膨胀、副本、merge、恢复及 N−1 预算评估 |
| 存储 | 显式 RWO 文件系统 CSI，兑现 fsync、重挂及扩容 | 不使用 emptyDir、local-path 或未经验证的共享目录替代 |
| 内核 | 选定节点 `vm.max_map_count >= 1048576` | 可由产品写入专用 sysctl 文件；无主机权限时先由平台管理员配置 |
| 网络 | 节点间 transport、API、DNS、时间与 Registry 正常 | 先修平台；不要绕过 TLS 或关闭数据保护 |
| 对象存储 | 独立 HTTPS S3，明确 bucket、region、base_path、CA 和凭据 | 先核验后端语义，不因 bucket 能列出就启用自动备份 |
| 监控 | 已交付 Prometheus/Grafana；默认 namespace `monitor`、release `prometheus` | 改为客户真实值，检查 CRD 与采集选择器 |

这些资源数值是规划起点，不是上游最低要求、生产容量上限或 SLA。客户主机 16 CPU/32Gi 的规划基线与实验室缩小参数分开使用。一个 ES replica 与存储层副本形成两层容量消耗，合并、迁移和重建还需要额外空间；不能仅以原始业务 JSON 大小估算磁盘。

在控制机设置上下文。下列输入中的 `CLUSTER` 取自客户已创建的 kubeauto 集群目录；预期目录、配置和 kubeconfig 均存在。任一步失败即停止后续安装。

```bash
set -euo pipefail
cd /usr/local/kubeauto
CLUSTER=search-production
CFG="$PWD/clusters/$CLUSTER/config.yml"
export KUBECONFIG="$PWD/clusters/$CLUSTER/kubectl.kubeconfig"
test -s "$CFG"
test -s "$KUBECONFIG"
kubectl config current-context
kubectl version
kubectl get nodes -o wide
kubectl get storageclass
kubectl get crd prometheuses.monitoring.coreos.com servicemonitors.monitoring.coreos.com
kubectl -n monitor get prometheus,pod
```

节点必须 Ready、可调度且具有足够实际 allocatable。检查 CSI 现有文件系统写入、fsync、跨节点重挂和扩容证据是否对应本次 StorageClass/版本。检查已有 ECK 的位置、版本和管理范围；本产品只接受固定共享实例，不自动接管外来控制器。

本分支已在 Kubernetes 1.33.6、Longhorn 1.12.1 V1、ext4、三个存储副本下验证这条存储路径：向 1 GiB RWO 卷写入并 fsync 1 MiB 数据，删除首个使用者后把原卷挂到另一节点，再在线扩到 2 GiB。PVC、PV 和原生卷 UID 保持，数据 SHA256 不变，实际文件系统从 1,020,702,720 增至 2,077,073,408 字节，探针及其 PV 已回收，见[当前 CSI 验收证据](../../../tests/evidence/elasticsearch-csi-probe-20261104-151821-3ac2b904a51f4d4ab0c42321c8bf22e5.json)。这证明上述固定路径的集成语义；底层是实验室专用 loop 文件系统，不代表客户物理盘性能、断电持久性或其他 CSI 的资格。客户采用不同驱动、版本或 StorageClass 时，由平台管理员先取得对应验证结果，再安装 ES。

> **异常处理：** 版本、资源、StorageClass 或 ECK 身份不匹配时停在前置阶段。保留配置、节点状态、事件与首个错误，不反复尝试安装，不借用其他业务的现存 PVC。

## 3. 准备命名空间与凭据引用

由平台管理员在同一 kubeconfig 下准备独立 namespace 和客户管理的 Secret。默认名称为 `elasticsearch` 与 `search`。若同名 namespace 已存在，先确认它是为本业务创建的空边界；不得给外来 namespace 加所有权标签来绕过拒绝检查。

首次从空边界创建 namespace：

```bash
set -euo pipefail
kubectl create namespace elasticsearch
kubectl label namespace elasticsearch app.kubernetes.io/managed-by=kubeauto kubeauto.io/component=elasticsearch
```

指标账户使用预先生成的随机密码文件，文件来源是客户凭据管理系统，格式为无换行的原始密码，权限 0600。示例固定输入路径 `/run/secrets/search-metrics-password`；文件不存在或来源不明时停止，不把密码写入 config.yml、命令行或日志。

```bash
set -euo pipefail
test -s /run/secrets/search-metrics-password
kubectl -n elasticsearch create secret generic search-metrics \
  --from-literal=username=search-metrics \
  --from-file=password=/run/secrets/search-metrics-password
```

产品在 ES 就绪后为该身份收敛最小监控角色；应用不能复用此账户。客户已有 HTTP 证书时创建独立 Secret，含 `tls.crt`、`tls.key`、`ca.crt`，SAN 必须覆盖真实访问名，并同时包含 `search-client.elasticsearch.svc` 与 `search-es-http.elasticsearch.svc`：前者供业务入口使用，后者供 ECK 关联和内部管理连接使用。产品会校验证书链、有效期、私钥及两个名称。不配置自定义证书时由 ECK 生成，产品把业务 Service DNS 加入 SAN。Secret 只提供引用，不由卸载流程删除。

S3 需要时预先创建 `search-s3`，key 为 `s3.client.default.access_key` 和 `s3.client.default.secret_key`；文件由对象存储管理员提供。私有 CA ConfigMap 的 key 固定为 `ca.crt`。ES JVM truststore 与访问 S3 的客户端 CA 分别校验，不能只给测试客户端装 CA。

对象存储管理员先创建专用 bucket、为本业务分配独立 base_path，并提供该范围内所需的读、写、列举、删除及 multipart 权限；灾备读取身份只授予读取需要的权限。确切 IAM actions 按实际 AWS/S3 后端审核，桶策略不能只允许上传而阻断保留清理或并发校验。以下输入文件由客户凭据系统交付，两个 key 文件内容无换行、权限 0600；CA 文件是对象存储证书链对应的受信 PEM，先与管理员通过独立渠道提供的指纹核对。公网可信 CA 无须创建该 ConfigMap，也不设置 `elasticsearch_s3_ca_configmap`。

```bash
set -euo pipefail
test -s /run/secrets/search-s3-access-key
test -s /run/secrets/search-s3-secret-key
test -s /run/secrets/search-s3-ca.pem
openssl x509 -in /run/secrets/search-s3-ca.pem -noout -fingerprint -sha256
kubectl -n elasticsearch create secret generic search-s3 \
  --from-file=s3.client.default.access_key=/run/secrets/search-s3-access-key \
  --from-file=s3.client.default.secret_key=/run/secrets/search-s3-secret-key
kubectl -n elasticsearch create configmap search-s3-ca \
  --from-file=ca.crt=/run/secrets/search-s3-ca.pem
```

这些是首次创建命令。名称已存在时停止并核对来源，不覆盖外来 Secret 或把其内容输出到终端；轮换走第 8 节。凭据系统、对象存储、Kubernetes Secret/CA 和产品配置均需进入客户的加密备份与恢复清单，普通索引 snapshot 不会代替这些输入。

## 4. 配置、下载与安装

在 `$CFG` 中修改已有字段，避免重复 YAML key。下例节点名来自第 2 节的 `kubectl get nodes`，StorageClass 来自客户已验收 CSI。以下 `master-a` 等是配置结构示例，必须整体替换为实际节点名，不能直接用于生产执行。

```yaml
elasticsearch_install: "yes"
elasticsearch_state: present
elasticsearch_namespace: elasticsearch
elasticsearch_name: search
elasticsearch_ver: "9.5.6"
elasticsearch_eck_ver: "3.5.0"
elasticsearch_topology: standard
elasticsearch_master_nodes: [master-a, master-b, master-c]
elasticsearch_data_nodes: [data-a, data-b, data-c]
elasticsearch_data_count: 3
elasticsearch_storage_class: production-rwo
elasticsearch_master_cpu: "2"
elasticsearch_master_memory: 4Gi
elasticsearch_master_pvc_size: 20Gi
elasticsearch_data_cpu: "4"
elasticsearch_data_memory: 16Gi
elasticsearch_data_heap: 8g
elasticsearch_data_pvc_size: 200Gi
elasticsearch_exporter_enabled: true
elasticsearch_exporter_secret: search-metrics
elasticsearch_monitoring_namespace: monitor
elasticsearch_monitoring_release: prometheus
elasticsearch_network_policy_enabled: true
elasticsearch_kibana_enabled: false
```

选择 compact 时将 topology 改为 `compact`，master_nodes 设为 `[]`，data_count 保持 `3`，data_nodes 至少包含三个不同的候选节点。这三个成员同时承担 master/data/ingest；可保留额外候选节点作为维护迁移位置，增加候选不增加成员数，备用节点仍须满足相同容量和存储条件。不要把 standard 已有 CR 原地改成 compact。拓扑转换通过独立目标迁移，切换前必须对账。

控制机按顺序执行固定产品入口。下载优先 TalkEdu，保留固定回退；任一必需镜像失败即停止。安装会先校验所有权和共享 ECK，再配置 mmap、收敛声明并等待当前代次的数据面，默认最长 1800 秒。日志中的等待心跳说明当前节点数和健康状态，不等于业务验收通过。

```bash
set -euo pipefail
cd /usr/local/kubeauto
kubecli download -E elasticsearch
kubecli setup "$CLUSTER" 07 --tags elasticsearch
```

可选 SmartCN 须先下载 `kubecli download -E elasticsearch-smartcn`，再设置 `elasticsearch_smartcn_enabled: true` 并重新执行同一 setup 入口。切换镜像会引起受控滚动；先验证 analyzer 与迁移方案，不给旧字段假定自动重分词。Kibana 设置 `elasticsearch_kibana_enabled: true` 后以两副本运行，业务 REST API 不依赖 Kibana。

> **失败停止与恢复：** 记录首次错误、渲染目录、ES/ECK 状态及事件。镜像问题修制品，节点/CSI 问题修平台，身份冲突不得强制接管。原输入重试应复用已收敛资源；数据格式升级、失败恢复及缩容不属于可以盲目重复的安装动作。

## 5. 控制面、数据面与上线验收

以下检查在同一控制机与 kubeconfig 下执行，只读取状态。预期唯一 ECK、正确 ES 当前版本和节点数、PVC Bound、分片绿色，随后还须完成业务检查。

```bash
set -euo pipefail
kubectl -n elastic-system get statefulset elastic-operator
kubectl -n elasticsearch get elasticsearch search -o yaml
kubectl -n elasticsearch get pod,pvc,service -o wide
kubectl -n elasticsearch get events --sort-by=.lastTimestamp
kubectl -n monitor get servicemonitor elasticsearch-search-metrics
```

standard 应为六个 ES 成员起步，compact 为三个；Service `search-client` 的 EndpointSlice 只应包含 data 成员。ECK `observedGeneration` 应对应当前 CR generation，并且 `phase=Ready`、`ReconciliationComplete=True`。观察到当前代次仅表示控制器已处理输入；滚动中的集群也可能暂时绿色且成员齐全，不能据此认为所有成员已采用新配置。

当前 standard 集成验证已通过上述产品安装路径：三个专用 master、三个 data/ingest、六个 Bound PVC，以及两个可用 Kibana 实例；业务 Service 的就绪端点与 data 成员集合一致。向三个 primary、一个 replica 的确认索引写入 32 条固定 ID 文档后，再次执行相同 setup，CR、Pod（含 Operator）、PVC、凭据 UID 与凭据摘要均保持，文档数量、内容 SHA256 和索引 UUID 不变。该结果说明本次相同配置重入没有替换数据或触发意外滚动，见[标准拓扑证据](../../../tests/evidence/elasticsearch-standard-20261104-151049-9a935e0dbcd84156be09a2e27a4ded3a.json)；它不替代后续故障、变更及容量验收，也不保证客户环境没有外部控制器或人工操作引起变化。

业务客户端从获准的 namespace 接入：由平台管理员给该客户端 namespace 添加 `kubeauto.io/elasticsearch-client=true`，应用使用独立最小权限角色或 API key，信任 ECK 导出的 CA。不要向全网开放管理账户，也不要使用 `curl -k`。标准内网端点为 `https://search-client.elasticsearch.svc:9200`；跨集群访问先配置稳定域名、LB 和相应 SAN。

业务验收至少创建专用索引、写入带固定 ID 的文档、检查响应 item 与分片结果、刷新后搜索、核对数量/hash，并验证错误 CA、错误凭据和跨索引越权被拒绝。然后重复同一 setup，比较 CR、Pod、PVC 与 Secret UID、数据及滚动事件。允许的变化必须能由输入差异或控制器正常动作解释。

### 5.1 建立管理诊断连接

管理员在第 2 节的控制机、同一 shell 与 kubeconfig 中执行。本路径针对 ECK 生成的默认 HTTP 证书和 `elastic` 管理身份；用于管理诊断及后续显式授权的维护，应用接入见第 6 节。本节实际请求只读；`eschange` 供后续变更任务复用，默认从标准输入读取 JSON，第三参数为 `no-body` 时不发送请求体。需要 `kubectl`、Python 3 和 curl ≥ 7.84.0（支持带引号的 netrc 密码、`--fail-with-body` 和 `--connect-to`），先用 `curl --version` 核对。凭据仅写入本次创建的私有临时目录，结束此 shell 时回收文件和 port-forward；不要把目录打包到工单或日志中。使用自定义证书/管理员的客户应由凭据系统提供对应 CA 和 netrc 文件，不读取默认 Secret 代替自己的身份。

```bash
set -euo pipefail
umask 077
export OPS_DIR
OPS_DIR=$(mktemp -d /run/kubeauto-search.XXXXXX)
PF_PID=
trap 'if test -n "$PF_PID"; then kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; fi; rm -rf -- "$OPS_DIR"' EXIT
kubectl -n elasticsearch get secret search-es-http-certs-public -o json |
  python3 -c 'import base64,json,os,pathlib,sys; pathlib.Path(os.environ["OPS_DIR"],"ca.crt").write_bytes(base64.b64decode(json.load(sys.stdin)["data"]["tls.crt"]))'
kubectl -n elasticsearch get secret search-es-elastic-user -o json |
  python3 -c 'import base64,json,os,pathlib,sys; password=base64.b64decode(json.load(sys.stdin)["data"]["elastic"]).decode(); pathlib.Path(os.environ["OPS_DIR"],"netrc").write_text("machine search-client.elasticsearch.svc login elastic password "+json.dumps(password)+"\n")'
kubectl -n elasticsearch port-forward service/search-client 19200:9200 \
  > "$OPS_DIR/port-forward.log" 2>&1 &
PF_PID=$!
esread() {
  curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time "${2:-40}" \
    --noproxy '*' --cacert "$OPS_DIR/ca.crt" --netrc-file "$OPS_DIR/netrc" \
    --connect-to search-client.elasticsearch.svc:9200:127.0.0.1:19200 \
    "https://search-client.elasticsearch.svc:9200$1"
}
eschange() {
  local body_args=(--header 'Content-Type: application/json' --data-binary @-)
  if test "${3:-}" = no-body; then body_args=(); fi
  curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 40 \
    --noproxy '*' --cacert "$OPS_DIR/ca.crt" --netrc-file "$OPS_DIR/netrc" \
    --connect-to search-client.elasticsearch.svc:9200:127.0.0.1:19200 \
    --request "$1" "${body_args[@]}" \
    "https://search-client.elasticsearch.svc:9200$2"
}
ready=no
for attempt in $(seq 1 15); do
  if esread / 2 > "$OPS_DIR/identity.json"; then ready=yes; break; fi
  kill -0 "$PF_PID"
  sleep 2
done
test "$ready" = yes
cat "$OPS_DIR/identity.json"
esread '/_cluster/health?wait_for_status=green&timeout=30s'
```

本地端口只承载 TCP 转发；URL 与 TLS 主机名仍为 Service DNS，CA 和 SAN 校验均保留。准备循环最多 15 次，每次 HTTP 最长 2 秒、失败后间隔 2 秒，等待约 60 秒；后续普通诊断请求最长 40 秒。版本必须为 `9.5.6`，记录非空 `cluster_uuid`，health 应为 `green`、`timed_out=false`，节点数对应所选拓扑。连接失败时检查私有 port-forward 日志、Service EndpointSlice、CA 和 Secret 身份，停止后续变更。退出 shell 后临时文件被删除；新 shell 先重新完成第 2 节上下文和本节连接。

## 6. 业务建模与客户端

先由平台管理员在控制机准备应用身份。在第 2 节的上下文中，从客户凭据系统取得 `/run/secrets/search-app-password`，内容为无换行随机密码，权限 0600；它与指标账户的密码分别管理。下列操作只创建专用于 `catalog-*` 的身份，不开放其他业务索引。

```bash
set -euo pipefail
test -s /run/secrets/search-app-password
kubectl -n elasticsearch create secret generic search-app-password \
  --from-file=password=/run/secrets/search-app-password
```

在 `$CFG` 的现有 `elasticsearch_requests` 列表中增加以下两个对象，不覆盖已有请求。角色允许显式创建索引、只新增文档及查询；不含更新、删除、集群管理或其他索引权限。索引创建权限只在应用负责建索引时保留，生产预建索引后可用新的变更 ID 收紧角色。

```yaml
elasticsearch_requests:
  - id: catalog-role-v1
    type: role
    name: catalog-app
    definition:
      cluster: []
      indices:
        - names: ["catalog-*"]
          privileges: [create_index, create_doc, read, view_index_metadata]
  - id: catalog-user-v1
    type: user
    name: catalog-app
    password_secret: search-app-password
    roles: [catalog-app]
```

执行 `kubecli setup "$CLUSTER" 07 --tags elasticsearch`，预期依次出现两个 `ELASTICSEARCH_REQUEST_PASS`，随后使用应用身份完成下面的读写，向其他业务索引的请求应返回 403。任一请求失败即停止应用切换，保留已工作的身份；修正角色定义使用新变更 ID，同一 ID 不允许改为不同输入。Secret 已存在时通过客户 Secret 管理流程轮换，不删除后重建来避开冲突。

下面是应用运行环境中的 Python 示例，依赖固定客户端 `elasticsearch==9.5.1`。平台管理员把应用 namespace 加上 `kubeauto.io/elasticsearch-client=true`，将同一应用密码和已核验 CA 以只读文件挂载给应用：`ES_PASSWORD_FILE`、`ES_CA_FILE` 为其挂载路径，`ES_USERNAME=catalog-app`，`ES_URL=https://search-client.elasticsearch.svc:9200`。这些环境变量只存路径和用户名，不存密码。示例创建新的演示索引和文档，重复运行会明确返回已存在或冲突；不得指向已有生产索引。示例索引的删除由管理员在核对名称和用途后另行执行，应用角色没有删除权限。

```python
import json
import os
from pathlib import Path
from elasticsearch import Elasticsearch

client = Elasticsearch(
    os.environ["ES_URL"],
    ca_certs=os.environ["ES_CA_FILE"],
    basic_auth=(os.environ["ES_USERNAME"], Path(os.environ["ES_PASSWORD_FILE"]).read_text()),
    request_timeout=30,
    max_retries=3,
    retry_on_status=(429, 502, 503, 504),
    sniff_on_start=False,
    sniff_before_requests=False,
)
created = client.indices.create(index="catalog-demo-v1", settings={"number_of_shards": 3, "number_of_replicas": 1},
    mappings={"dynamic": "strict", "properties": {
        "product_id": {"type": "keyword"}, "title": {"type": "text"},
        "category": {"type": "keyword"}, "price": {"type": "double"}}})
assert created["acknowledged"] and created["shards_acknowledged"]
written = client.create(index="catalog-demo-v1", id="item-1001", refresh="wait_for",
    document={"product_id": "item-1001", "title": "可靠的全文检索", "category": "books", "price": 79.0})
assert written["result"] == "created"
assert written["_shards"]["failed"] == 0
result = client.search(index="catalog-demo-v1", allow_partial_search_results=False, query={"bool": {
    "must": [{"match": {"title": "检索"}}], "filter": [{"term": {"category": "books"}}]}},
    sort=[{"price": "asc"}, {"product_id": "asc"}],
    aggs={"categories": {"terms": {"field": "category"}}})
assert not result["timed_out"] and result["_shards"]["failed"] == 0
assert result["hits"]["total"]["value"] == 1
print(json.dumps({"hits": result["hits"]["total"]["value"]}))
client.close()
```

示例期待一个命中，分片失败为零。`refresh=wait_for` 等待可搜索性，与写入持久化不是同一条件。Bulk 必须遍历每个 item，分别统计成功、429、mapping 错误和认证错误；仅重试可重试失败项，保留 ID。连接超时可能结果未知，先查询或按 create/条件更新语义处理，不直接生成新 ID 重发。

本分支在 standard 与 compact 上各用 100 条固定 ID 文档验证了这条客户端路径：Bulk item 与分片确认全部符合预期，全文/过滤/排序结果为 50 条，两个类别各 50 条、价格总和 4950；reindex 与 alias 切换后仍为 100 条且内容摘要一致。旧并发条件和重复 create 均得到 409。该[业务实测](../../../tests/evidence/elasticsearch-business-20261104-153037-96ea5c9af1e4430c8644573de51f83e1.json)使用独立测试索引，结束后已删除；它验证接口与数据语义，不是生产负载容量结论。

上述 `create_doc` 角色要求 Bulk 使用 `create` action；普通 `index` action 可覆盖已有文档，因此会被拒绝。需要更新、删除或迁移的服务应使用独立、明确授权的角色，而不是给只新增的应用补 `all`。API key 适合需要独立到期与撤销的应用实例，可由安全管理员通过原生安全 API 签发权限受限的 key，再从 Secret 文件读取给客户端的 `api_key` 参数；不与本例的 basic authentication 同时配置。权限含义依据官方[安全权限参考](https://www.elastic.co/docs/reference/elasticsearch/security-privileges)。

更新先读取 `_seq_no` 和 `_primary_term`，再携带两者提交条件更新；409 表示并发状态已变化，应重新读取并按业务规则合并。reindex 前创建明确 mapping 的目标索引，核对数量、关键字段 hash 和检索结果，再在一次 `_aliases` 请求中 remove/add；必要的 remove 设置 `must_exist: true`，响应同时检查 `acknowledged`、`errors` 和逐项结果，并读回 alias 只指向目标索引。迁移中的新增写入必须另有捕获或停写窗口；reindex 不持续同步这些变更。切换后有新写入时，回指旧索引前必须先保护并重放这些数据，不能只把 alias 指回去。alias filter 不限制按 ID 读取，不能替代角色权限。

data stream 适合带 `@timestamp` 的追加数据。先声明匹配的 index template、data stream 标记和 ingest pipeline，再以 create action 写入；rollover 新建 backing index，ILM 按状态推进生命周期。进入 delete 阶段前必须确认保留要求与可恢复快照；rollover 后阶段的 `min_age` 从 rollover 时间计算，不按每条文档的业务时间精确删除。通过 `_ilm/explain` 定位停滞步骤，不能用“策略已创建”代替生命周期实际推进。发现错误删除策略时先暂停本业务写入和变更，保存当前 policy、backing indices 与 explain，按实际已进入的阶段处理；删除模板或修改模板不会撤回旧索引已采用的策略，已删除数据只能从兼容恢复点恢复。

### 6.1 用新索引迁移字段模型并切换 alias

此任务由维护管理员在第 5.1 节同一控制机和管理连接中执行，针对第 6 节创建的单文档演示索引。应用角色没有迁移和 alias 管理权限，不提升应用权限来执行维护。先停止该演示索引的所有写入，保留源索引，并确认业务没有绕过 alias 的写入者；真实业务迁移先按第 9 节建立可恢复点和增量处理方案，不能把这里的静态单文档条件外推到持续写入业务。

下面读取源身份与已有索引、alias，明确拒绝已有目标或同名 alias。`eschange` 仅复用已核验的 TLS 和身份，所有变更方法及对象仍在调用处显式给出；默认从标准输入读取 JSON，第三参数为 `no-body` 时不发送请求体；命令失败即停止。

```bash
set -euo pipefail
esread '/_cat/indices?format=json&h=index&expand_wildcards=all' > "$OPS_DIR/indices.json"
esread '/_alias' > "$OPS_DIR/aliases.json"
esread '/catalog-demo-v1/_doc/item-1001' > "$OPS_DIR/source-document.json"
python3 - <<'PYREAD'
import json, os
from pathlib import Path
p = Path(os.environ["OPS_DIR"])
indices = {r["index"] for r in json.loads((p / "indices.json").read_text())}
assert "catalog-demo-v1" in indices and "catalog-demo-v2" not in indices
aliases = json.loads((p / "aliases.json").read_text())
assert all("catalog-live" not in r.get("aliases", {}) for r in aliases.values())
assert json.loads((p / "source-document.json").read_text())["found"]
PYREAD
eschange PUT /catalog-demo-v2 <<'JSON'
{"settings":{"number_of_shards":3,"number_of_replicas":1},"mappings":{"dynamic":"strict","properties":{"product_id":{"type":"keyword"},"title":{"type":"text"},"category":{"type":"keyword"},"price":{"type":"double"}}}}
JSON
esread '/_cluster/health/catalog-demo-v2?wait_for_status=green&timeout=30s'
eschange POST /_aliases <<'JSON'
{"actions":[{"add":{"index":"catalog-demo-v1","alias":"catalog-live","is_write_index":true}}]}
JSON
eschange POST '/_reindex?wait_for_completion=false' <<'JSON' > "$OPS_DIR/reindex-start.json"
{"source":{"index":"catalog-demo-v1"},"dest":{"index":"catalog-demo-v2","op_type":"create"}}
JSON
TASK_ID=$(python3 -c 'import json,os,pathlib; print(json.loads(pathlib.Path(os.environ["OPS_DIR"],"reindex-start.json").read_text())["task"])')
printf 'reindex_task=%s\n' "$TASK_ID"
deadline=$((SECONDS + 300))
completed=no
while (( SECONDS < deadline )); do
  esread "/_tasks/$TASK_ID" 5 > "$OPS_DIR/reindex-status.json"
  if python3 -c 'import json,os,pathlib,sys; r=json.loads(pathlib.Path(os.environ["OPS_DIR"],"reindex-status.json").read_text()); sys.exit(0 if r.get("completed") else 1)'; then
    completed=yes
    break
  fi
  sleep 2
done
test "$completed" = yes
python3 - <<'PYRESULT'
import json, os
from pathlib import Path
r = json.loads(Path(os.environ["OPS_DIR"], "reindex-status.json").read_text())
assert not r.get("error")
r = r["response"]
assert not r.get("failures") and not r.get("timed_out")
assert r["created"] == 1 and r["total"] == 1 and r["version_conflicts"] == 0
PYRESULT
eschange POST /catalog-demo-v2/_refresh no-body
esread '/catalog-demo-v2/_doc/item-1001' > "$OPS_DIR/target-document.json"
esread '/catalog-demo-v2/_count' > "$OPS_DIR/target-count.json"
python3 - <<'PYCOMPARE'
import json, os
from pathlib import Path
p = Path(os.environ["OPS_DIR"])
source = json.loads((p / "source-document.json").read_text())
target = json.loads((p / "target-document.json").read_text())
assert source["_source"] == target["_source"] and target["found"]
assert json.loads((p / "target-count.json").read_text())["count"] == 1
PYCOMPARE
eschange POST /_aliases <<'JSON' > "$OPS_DIR/alias-switch.json"
{"actions":[{"remove":{"index":"catalog-demo-v1","alias":"catalog-live","must_exist":true}},{"add":{"index":"catalog-demo-v2","alias":"catalog-live","is_write_index":true}}]}
JSON
esread '/_alias/catalog-live' > "$OPS_DIR/alias-after.json"
python3 - <<'PYALIAS'
import json, os
from pathlib import Path
p = Path(os.environ["OPS_DIR"])
r = json.loads((p / "alias-switch.json").read_text())
assert r["acknowledged"] and not r.get("errors", False)
assert all(a.get("status", 200) < 300 for a in r.get("action_results", []))
r = json.loads((p / "alias-after.json").read_text())
assert set(r) == {"catalog-demo-v2"}
assert r["catalog-demo-v2"]["aliases"]["catalog-live"]["is_write_index"]
PYALIAS
esread '/catalog-live/_doc/item-1001'
```

此例保留相同 mapping，用于先验证迁移流程；真正改变字段类型或 analyzer 时，应先用新模型验证样本和代表性查询，再执行同样的复制与对账。把输出的非敏感 task ID 记入客户变更记录；私有临时目录会随第 5.1 节的 shell 退出回收。任务等待最多约 307 秒，任务错误、冲突、数量不符或目标内容不符均停止切换；超时不代表任务已经取消，先用该 ID 在重新建立的管理连接中查询实际状态，不重复创建任务。alias 切换后，把应用的读写索引名从 `catalog-demo-v1` 改为 `catalog-live`，核对所有写入者均已采用该名称，保持停写并先验证第 6 节的查询/聚合和越权拒绝，再开放新写入；第 6 节示例中的首次建索引命令不再执行。真实业务应按完整 ID 集合、内容摘要及查询验收，不使用单文档对账代替。

若切换前失败，保留源与目标以诊断，源数据不被 reindex 删除。切换成功但尚无新写入时，可在核对当前 alias 精确目标后，用一次带 `must_exist: true` 的 remove-v2/add-v1 请求回切，再读回业务。已有目标新写入时先保护、重放并对账，禁止直接回指旧索引。源索引保留到恢复点、回退窗口和数据核对均满足客户要求；本步骤不自动删除任何源索引。

### 6.2 为追加事件建立 pipeline、data stream 与 ILM

维护管理员在第 5.1 节同一控制机和连接中执行，复用该节定义的 `esread` 与 `eschange`；本任务不要求先执行第 6.1 节的索引迁移。以下是独立演示数据，不用于现有商品索引。对象名均带 `catalog-events-demo-v1`，只匹配这一条 data stream；应用继续使用独立的 `catalog-*` 角色，不能获得模板、pipeline 或 ILM 管理权限。

此例对新 backing index 使用三个 primary、一个 replica，经默认 pipeline 把 `category` 设为 `normalized`；mapping 拒绝未声明字段和错误类型。ILM 在 primary 达到 50 GB 或索引年龄达到 7 天时触发 rollover，rollover 后至少 30 天进入删除阶段。数值只表达示例保留策略，采用前须审批业务保留期限、容量和独立恢复路径；ILM 不等待备份成功才删除。第 9 章固定产品恢复入口当前针对显式普通索引，不覆盖 data stream 名称、backing index 与模板/ILM 的完整恢复，本例不能代替追加数据业务的生产数据保护资格。

先逐项核验五个名称均不存在；`esabsent` 只接受明确 HTTP 404，网络失败、401/403 或已有对象均停止。先核验全部名称，再创建任何对象；不要删除同名外来资源来重试。

```bash
set -euo pipefail
esabsent() {
  local status
  status=$(curl --silent --show-error --connect-timeout 5 --max-time 40 \
    --noproxy '*' --cacert "$OPS_DIR/ca.crt" --netrc-file "$OPS_DIR/netrc" \
    --connect-to search-client.elasticsearch.svc:9200:127.0.0.1:19200 \
    --output "$OPS_DIR/absent.json" --write-out '%{http_code}' \
    "https://search-client.elasticsearch.svc:9200$1")
  test "$status" = 404
}
for path in /catalog-events-demo-v1 /_data_stream/catalog-events-demo-v1 \
  /_ingest/pipeline/catalog-events-demo-v1 /_ilm/policy/catalog-events-demo-v1 \
  /_index_template/catalog-events-demo-v1; do
  esabsent "$path"
done
eschange PUT /_ingest/pipeline/catalog-events-demo-v1 <<'JSON'
{"processors":[{"set":{"field":"category","value":"normalized"}}]}
JSON
eschange POST /_ingest/pipeline/catalog-events-demo-v1/_simulate <<'JSON'
{"docs":[{"_source":{"business_id":"event-0001"}}]}
JSON
eschange PUT /_ilm/policy/catalog-events-demo-v1 <<'JSON'
{"policy":{"phases":{"hot":{"actions":{"rollover":{"max_primary_shard_size":"50gb","max_age":"7d"}}},"delete":{"min_age":"30d","actions":{"delete":{}}}}}}
JSON
eschange PUT /_index_template/catalog-events-demo-v1 <<'JSON'
{"index_patterns":["catalog-events-demo-v1"],"priority":501,"data_stream":{},"template":{"settings":{"number_of_shards":3,"number_of_replicas":1,"index.default_pipeline":"catalog-events-demo-v1","index.lifecycle.name":"catalog-events-demo-v1"},"mappings":{"dynamic":"strict","properties":{"@timestamp":{"type":"date"},"business_id":{"type":"keyword"},"category":{"type":"keyword"},"price":{"type":"integer"}}}}}
JSON
eschange POST /_index_template/_simulate_index/catalog-events-demo-v1 <<<'{}'
eschange PUT /_data_stream/catalog-events-demo-v1 no-body
esread '/_cluster/health/catalog-events-demo-v1?wait_for_status=green&timeout=30s'
eschange PUT '/catalog-events-demo-v1/_create/event-0001?wait_for_active_shards=all&refresh=wait_for' <<'JSON'
{"@timestamp":"2026-11-04T00:00:00Z","business_id":"event-0001","price":79}
JSON
esread '/catalog-events-demo-v1/_search?allow_partial_search_results=false'
esread '/catalog-events-demo-v1/_ilm/explain'
```

逐步检查：声明响应 `acknowledged=true`，simulate 的 `_source.category` 为 `normalized`；模板模拟结果采用本例 mapping、pipeline 与 ILM，没有更高优先级模板覆盖；健康为绿色且未超时；写入为 `created`、分片失败为零；搜索恰有 `event-0001` 且内容正确，explain 中每个 backing index 的 `managed=true`、policy 为本例名称。每个 HTTP 请求最多 40 秒；健康等待最多 30 秒，任一结果不符都停止，不继续写入。模板在集群全局生效，不能把匹配范围扩大为 `*`。

确认上述出口后，管理员可以显式 rollover 验证写入索引切换。它会额外创建三个 primary 及其 replica，先核对容量；此处没有等待自动 ILM 调度，不能据此声称已实测七天/三十天生命周期：

```bash
set -euo pipefail
esread '/_data_stream/catalog-events-demo-v1' > "$OPS_DIR/stream-before.json"
eschange POST /catalog-events-demo-v1/_rollover <<<'{}' > "$OPS_DIR/rollover.json"
esread '/_data_stream/catalog-events-demo-v1' > "$OPS_DIR/stream-after.json"
esread '/catalog-events-demo-v1/_count' > "$OPS_DIR/stream-count.json"
python3 - <<'PYSTREAM'
import json, os
from pathlib import Path
p = Path(os.environ["OPS_DIR"])
read = lambda name: json.loads((p / name).read_text())
r = read("rollover.json")
assert r["rolled_over"] and r["old_index"] != r["new_index"]
before = read("stream-before.json")["data_streams"][0]["indices"]
after = read("stream-after.json")["data_streams"][0]["indices"]
assert len(after) == len(before) + 1 and after[-1]["index_name"] == r["new_index"]
assert read("stream-count.json")["count"] == 1
PYSTREAM
esread '/catalog-events-demo-v1/_ilm/explain'
```

预期 backing index 增加一个、旧数据仍可查询，新 index 继续由同一 policy 管理。正常应用始终向 data stream 名称执行 `create`，不要按 backing index 名写入；同一 ID 在 rollover 后的新 backing index 中可能再次创建成功，不能把固定 ID 的去重边界外推为跨 backing index 的全局唯一约束。

> **异常与退役：** 写入前失败时保留本次已创建对象的定义和响应，不覆盖其他对象。写入后发生 policy/模板错误时先停该流的写入，保存 `GET /_ilm/policy/catalog-events-demo-v1`、data stream 与 explain；模板修改只影响后续索引，不能撤回已进入删除阶段的动作，也不要用集群级 ILM stop 影响其他业务。仅退役本例且审批确认无须保留数据后，管理员通过 `eschange DELETE 路径 no-body` 依次删除 `/_data_stream/catalog-events-demo-v1`、`/_index_template/catalog-events-demo-v1`、`/_ingest/pipeline/catalog-events-demo-v1`、`/_ilm/policy/catalog-events-demo-v1`，每步检查 acknowledged，再用 `esabsent` 核验精确名称消失。删除 data stream 会删除其全部 backing indices，不使用通配符；已经删除的数据须从已验证的兼容恢复路径恢复。

## 7. 日常运维、容量与性能

每日巡检结合业务探针、集群 health、未分配分片、recovery、任务队列、JVM/GC、磁盘水位和 Prometheus target。看到 yellow 先判断是否缺 replica、是否处于恢复，不盲目减少 replica 数；red 表示 primary 不可用，应停止依赖完整数据的变更并保留 shard allocation 证据。

`elasticsearch_data_nodes` 是允许调度数据成员的 Kubernetes 节点名列表，`elasticsearch_data_count` 是实际成员数。默认 `0` 表示每个候选节点一个成员；显式设置 `3` 并提供四个候选节点，可为三个成员保留一个备用故障域。所有候选节点都须通过容量、Ready、污点和 mmap 前置检查。候选列表不是预先锁定某个 Pod 的宿主，调度器决定落点；专用 master 与数据候选节点仍须分开。compact 可增加候选节点，实际混合成员始终是三个。

扩容前确认快照可恢复、集群绿色、N−1 与迁移容量充足。在 `$CFG` 增加经过准入的数据候选节点；若使用显式成员数，同时增加 `elasticsearch_data_count`，再执行 setup，等待分片恢复与绿色，验证确认账本和业务延迟。只增加候选节点而保持成员数会增加调度余量，不增加 ES 容量。PVC 扩容同时修改对应 `*_pvc_size`，产品拒绝缩小或更换 StorageClass；完成后检查 PVC 请求、实际 capacity、Pod 文件系统可用空间和原有数据。不得只以 PVC spec 变化判断成功。

缩容会触及 ECK 的 PVC 删除语义，产品入口默认拒绝。计划退役时先给出分片撤离、N−1 容量、当前快照和恢复目标，按独立变更计划操作，完成后对账。不能直接减少 nodeSet 数量，也不能手工删除 PVC 迫使协调完成。

节点维护由平台管理员逐个故障域进行。先确认集群绿色、副本齐全、PDB、备用候选节点和恢复预算，再执行客户既有 drain 流程；PDB 拒绝时停止并诊断，不能使用强制驱逐绕过数据保护。严格主机反亲和意味着三个候选节点已承载三个成员时，驱逐的成员不能在另两个节点上重复放置；CSI 支持跨节点重挂也不能创造调度空间。需要持续恢复全部成员时，先通过产品入口增加合格候选节点并保持成员数，再维护。恢复节点后等待存储重挂、分片恢复与业务探针，再维护下一节点。

当前[故障实践](../../../tests/evidence/elasticsearch-faults-20261104-163016-e9549ddac3484fc7b7f652189831b752.json)在四个数据候选宿主上运行三个成员：旧宿主经 guest runtime 隔离、证明相关进程停止后，两拓扑各一个成员使用原 PVC/PV/原生卷迁移到其他候选节点；接管后业务与各 32 条确认数据一致，原节点随后恢复 Ready 和原调度状态。它没有实施物理断电，不提供客户环境的迁移时限保证。现场即使节点 NotReady 或 SSH 不通，也不能据此强制退役旧写入实例；应由平台按其存储与隔离流程证明旧实例停止，再继续接管，失败时停在该边界保留证据。

压测使用固定 Rally 2.13.0 离线镜像和 track，运行 `benchmark-only`，授权范围限定压测索引。先保存数据规模、硬件、分片、并发和缓存状态，再记录吞吐、分位延迟、错误、重试与资源。Rally 2.13.0 默认报告包含 P50/P99，未输出 P95；本项目的 P95 来自独立稳态客户端的原始延迟样本，同时保留该客户端的 P50/P99。两组负载、并发和统计序列不同，须分别阅读，不能把客户端 P95 拼成 Rally 的结果，也不能用 P90/P99 插值冒充实测 P95。短基准不能代替至少 1800 秒含故障的稳定性演练；实验室缩小资源的数据不能承诺生产容量。压测后删除自有压测索引、Job 与临时凭据，保留脱敏结果。

当前固定 10000 条语料的短基准中，standard/compact 的 bulk 吞吐中位数分别为 4315.57/2505.31 docs/s，search 吞吐中位数为 192.56/137.82 ops/s，search P99 为 39.57/63.91 ms，两类任务错误率均为零。两拓扑总资源不同，且使用共享实验室与 loop 存储；完整负载、资源条件、数据核验及不可外推边界见[白皮书第 8 节](technical-whitepaper.md#8-生命周期与性能判断)。这些是短基准的实测结果，不是容量推荐值或长时故障负载结果。

独立故障负载在两拓扑各运行 1800 秒，每秒一条 create 和一次搜索，各注入一次 primary Pod 故障；最终各 1714 条文档全部对账，原业务索引保持。standard/compact 写入 P95 为 40.77/53.45 ms，搜索 P95 为 22.38/29.07 ms，各发生 11 次写入重试、无最终失败。少量长等待可能落在 P99 之外，不能用较低分位推断故障期间始终可写；该限速小文档负载也不衡量最大容量。窗口、完整分位、确认边界、重试和测量限制同见白皮书第 8 节。

> **磁盘保护：** low/high/flood 水位是故障控制，不是调优障碍。先查实际空间、merge、分片分布和底层卷；恢复容量后再核验写保护解除与新写入，不永久关闭水位或清除 translog。

固定 9.5.6 的 flood-stage 写保护返回 HTTP `429` 与 `cluster_block_exception`，并对应索引设置 `index.blocks.read_only_allow_delete=true`；它不同于线程池过载的 `es_rejected_execution_exception`，也不同于权限拒绝 403。看到 429 时先核对具体错误类型和实际水位，不把所有 429 都交给客户端重试。恢复到 high 水位以下后等待原生 block 自动解除，再复验原确认数据与新写入。固定定义见 [`INDEX_READ_ONLY_ALLOW_DELETE_BLOCK`](https://github.com/elastic/elasticsearch/blob/fd1e8011342a0988e432b999cea4bf884ca120ed/server/src/main/java/org/elasticsearch/cluster/metadata/IndexMetadata.java)。

### 7.1 从日常巡检进入故障诊断

运维管理员重新建立第 2 节上下文与第 5.1 节管理连接后，执行下面的只读检查；每次请求最长 40 秒，不改变分片放置或水位。将非敏感结果保存到客户巡检系统，与相同负载时段的基线比较。

```bash
set -euo pipefail
esread '/_cluster/health?wait_for_status=green&timeout=30s'
esread '/_cat/shards?format=json&h=index,shard,prirep,state,store,node,unassigned.reason'
esread '/_cat/allocation?format=json&bytes=gb'
esread '/_cat/recovery?format=json&active_only=true'
esread '/_cluster/pending_tasks'
esread '/_nodes/stats/jvm,fs,thread_pool,indices'
kubectl -n elasticsearch get pods,pvc -o wide
kubectl -n monitor get servicemonitor elasticsearch-search-metrics
kubectl -n monitor get prometheusrule elasticsearch-search-metrics
```

健康检查应为 `green`、`timed_out=false`，成员数符合当前配置；非维护时段不应持续存在未分配分片、恢复任务或积压的集群状态任务。`jvm` 中 GC 累计时间、`thread_pool` 中 rejected 累计数要按同一节点身份计算窗口增量；重启后计数重置，不能把累计数直接当作当前错误率。将节点 `fs` 可用空间与 PVC 实际容量、CSI 后端余量一起判断，尤其核对最满数据节点，而非仅看全群平均。

出现未分配分片时，读取一条分配解释并关联该分片的节点、磁盘、反亲和与恢复状态：

```bash
set -euo pipefail
eschange POST /_cluster/allocation/explain <<<'{}'
```

该 API 的空请求体只选择一个未分配分片，并不覆盖所有故障；没有未分配分片时可能返回 400，这个诊断结果不能作为安装失败。`can_allocate=no` 应沿各 decider 原因修真实前置，`throttled` 应结合恢复进度与资源观测；不要直接强制空 primary 或降低副本来消除告警。修复后重复 health、shards、recovery 和业务读写，并核对故障前的确认记录。若仍 red、确认记录缺失或原因不明，停止扩容、升级和下一节点维护，保留当前状态进入第 11 节相应故障分支。接口按固定 9.5.6 的 [allocation explain](https://github.com/elastic/elasticsearch/blob/fd1e8011342a0988e432b999cea4bf884ca120ed/rest-api-spec/src/main/resources/rest-api-spec/api/cluster.allocation_explain.json)、[nodes stats](https://github.com/elastic/elasticsearch/blob/fd1e8011342a0988e432b999cea4bf884ca120ed/rest-api-spec/src/main/resources/rest-api-spec/api/nodes.stats.json) 和 [cat allocation](https://github.com/elastic/elasticsearch/blob/fd1e8011342a0988e432b999cea4bf884ca120ed/rest-api-spec/src/main/resources/rest-api-spec/api/cat.allocation.json) 定义核对。

### 7.2 判断监控链与告警

平台监控管理员在客户已有 Prometheus 的查询/Targets/Rules 页面检查下表。默认标签为 `namespace="elasticsearch",service="search-metrics"`；改过 namespace 或资源名前，先从实际 target 读取标签，不能因查错标签而把空结果当成健康。双副本 Prometheus 分别核验，不把一个副本可用当作另一副本也已加载规则。

| 信号 | 单位、窗口与判断 | 下一步 |
| --- | --- | --- |
| `up{namespace="elasticsearch",service="search-metrics"}` | Prometheus 抓取成功为 1；只证明 HTTP 采集链 | 为 0 查 target 错误、Service/EndpointSlice/网络；空结果查 ServiceMonitor 与 Prometheus 选择器 |
| `elasticsearch_cluster_health_status{namespace="elasticsearch",service="search-metrics",color="green"}` | exporter 读回的集群绿色状态为 1 | 空结果查 ES 可达性、TLS、指标身份和权限；为 0 同时检查 yellow/red，不能只看 exporter Pod Ready |
| `elasticsearch_cluster_health_status{namespace="elasticsearch",service="search-metrics",color="red"}` | 状态量为 1 持续 1 分钟，触发 critical | primary 不可用；按 7.1 和第 11 节诊断，停止依赖完整数据的变更 |
| `elasticsearch_cluster_health_unassigned_shards{namespace="elasticsearch",service="search-metrics"}` | 分片个数大于 0 持续 5 分钟，触发 warning | 区分副本恢复、容量或分配拒绝，核验 allocation explain |

产品每 60 秒采集，单次 scrape 最长 30 秒，ES 请求最长 20 秒。`ElasticsearchMetricsUnavailable` 将抓取失败、target 缺失或健康采集失败合并为持续 2 分钟的 critical；恢复时应同时看到 target up、健康状态样本恢复且 green 为 1、业务探针通过及告警恢复。规则的持续时间是抑制短暂波动的项目默认，并非业务 SLO；通知到达还受 Prometheus 评估周期、Alertmanager 分组和客户通知链影响。采样负载随节点与索引数增长，依据官方 [exporter 采集说明](https://github.com/prometheus-community/elasticsearch_exporter/tree/v1.11.0)先测实际抓取耗时，再调整频率；不要用缩短间隔掩盖采集超时。

本项目当前固定版本已完成[双副本真实采样与告警恢复](../../../tests/evidence/elasticsearch-observability-20261105-030611-e3383c1ebf3749e4905c7b23fb7b810a.json)：指标身份读取业务数据被 403 拒绝，实际不可分配索引触发 red/未分配分片告警，移除 target 触发缺失采样告警，恢复后两份 Prometheus 的采样与告警均正常，原 32 条确认记录保持。这不包含外部通知到达验证；客户仍须对实际接收人和通知通道执行一次可追踪的通知演练。

### 7.3 增加 standard 数据成员

平台管理员在第 2 节的控制机、同一 `$CFG` 和第 5.1 节管理连接中操作。本例将三个 data 成员扩为四个，三个专用 master 保持不变；compact 不支持增加混合成员数。先完成 7.1 的健康检查，核验第 9 节的恢复点，并记录 CR/PVC UID、确认数据及业务延迟。新候选节点的实际名称取自 `kubectl get nodes -o wide`，须满足第 2 节相同的资源、内核、存储及故障域条件；容量预算同时包含分片迁移与一个宿主故障，不只计算新增 Pod。

在 `$CFG` 的原 `elasticsearch_data_nodes` 列表中加入已准入的第四个节点，将 `elasticsearch_data_count` 明确设为 `4`，再执行：

```bash
set -euo pipefail
kubecli setup "$CLUSTER" 07 --tags elasticsearch
kubectl -n elasticsearch get elasticsearch search
kubectl -n elasticsearch get pods,pvc -o wide
esread '/_cluster/health?wait_for_status=green&timeout=30s'
esread '/_cat/shards?format=json&h=index,shard,prirep,state,store,node'
esread '/_cat/recovery?format=json&active_only=true'
```

产品默认最多等待 1800 秒。出口是当前代次 Ready、三个 master 与四个 data、绿色且未超时、新成员及其 PVC 就绪，原 CR/PVC 身份与确认数据保持。结合真实分片表判断新增容量是否已承接分片，不用新 Pod Ready 代替迁移和业务复验；对照原账本复查数量、内容和代表性查询，再检查延迟及采集。迁移仍在进行时留在本窗口观察，不开始下一项维护。

> **失败停止与恢复：** 新成员 Pending、存储不足或分片分配被拒绝时保留新增对象和首个原因，按第 11 节修复对应前置，再以同一配置重入。已经创建第四个成员后，不能把 count 改回 `3` 当作无损回退：产品会拒绝缩容。若决定退出扩容，按经过审核的分片撤离及数据保留方案另行处置，不删除新 PVC 逼迫协调。

### 7.4 在线扩大数据卷

平台与存储管理员在同一控制机、配置及管理连接中执行。先检查 `kubectl get storageclass` 对应类支持扩容，驱动版本与在线文件系统扩容路径已准入；保存每个 data PVC/PV UID、当前请求/实际容量和 `esread '/_nodes/stats/fs'` 的文件系统总容量。确认后端空间、存储副本和恢复余量足以承载全部数据卷的新尺寸，集群绿色且最近恢复点已验证。主节点卷与数据卷是不同字段，不为扩大 data 卷同时修改 master 卷。

例如将生产默认的 data 卷从 `200Gi` 扩至 `300Gi`，只在 `$CFG` 修改 `elasticsearch_data_pvc_size: 300Gi`，保留原 StorageClass 和其他输入，再执行：

```bash
set -euo pipefail
kubecli setup "$CLUSTER" 07 --tags elasticsearch
kubectl -n elasticsearch get pvc
kubectl -n elasticsearch describe pvc
esread '/_nodes/stats/fs'
esread '/_cluster/health?wait_for_status=green&timeout=30s'
```

`200Gi→300Gi` 是生产配置示例，不是容量推荐值；客户当前卷不同或已经更大时，先确定严格大于现值的新尺寸。产品收敛默认等待 1800 秒，驱动后续扩容还须遵守客户存储变更窗口。逐卷核对 PVC 请求与 status.capacity、PV capacity、CSI 后端原生卷及成员实际文件系统均已扩大，原 UID 与数据保持；文件系统元数据会占用少量空间，因此比较总容量及驱动可解释的开销，不要求可用字节数等于声明字节数。全部成员绿色、确认数据及业务复验通过后才结束窗口。

当前[维护验收](../../../tests/evidence/elasticsearch-maintenance-20261104-220030-5a694a47444c45998faa196c20342b44.json)在 Longhorn 1.12.1 V1/ext4 上，将 standard 四个与 compact 三个 data 卷从 4 GiB 扩至 6 GiB，四层容量均通过，原 CR/PVC 与确认数据保持；缩回 4 GiB 被明确拒绝。它证明本分支的配置到实际容量闭环，不把实验室 loop 的结果外推为客户任意 CSI 已合格。

> **失败停止与恢复：** 请求已扩大但后端或文件系统未扩大时，保留原卷并检查 PVC 条件、CSI resizer/节点插件及存储事件；修复驱动或真实容量后继续原请求。扩容不能用缩小字段撤销，产品拒绝缩卷和换 StorageClass。不创建替代空卷，也不同时重启多个成员；驱动明确要求重启时，按经过该驱动验证的逐成员维护路径处理。

### 7.5 维护一个数据宿主

本任务由平台管理员执行，业务管理员负责停写窗口、确认账本与业务复验，存储管理员负责驱动及卷状态。使用第 2 节控制机和第 5.1 节管理连接。维护前确认绿色、三个投票成员健康、分片副本齐全、PDB 允许一次驱逐，并记录宿主原调度状态、其承载的全部 Pod 和 ES 卷身份。数据候选列表须有足够的独立备用宿主，且均已通过产品前置；standard/compact 或其他业务共用该宿主时，必须一起计入资源和恢复预算。专用 master 宿主维护不套用这个数据宿主示例。

先按 7.3 的配置方式补充备用候选，但保持 `elasticsearch_data_count` 不变，经同一 setup 收敛后再次检查健康。`drain` 会驱逐节点上所有符合条件的工作负载，不仅是 ES；节点上其他业务的 PDB、迁移容量及 emptyDir 临时数据须由各自负责人确认。下例 `data-a` 是维护计划中的节点名模板，执行前必须替换为实际数据宿主，不按 Pod 名或主机 IP 猜测：

```bash
set -euo pipefail
MAINT_NODE=data-a
kubectl get node "$MAINT_NODE" -o wide
kubectl get pods -A --field-selector "spec.nodeName=$MAINT_NODE" -o wide
kubectl -n elasticsearch get pdb
kubectl drain "$MAINT_NODE" --ignore-daemonsets --delete-emptydir-data --timeout=900s
kubectl -n elasticsearch get pods,pvc -o wide
esread '/_cluster/health?wait_for_status=green&timeout=30s'
esread '/_cat/recovery?format=json&active_only=true'
```

`--delete-emptydir-data` 允许丢弃已批准的临时卷内容，不删除 PVC；没有完成其他工作负载审查时不得执行该命令。驱逐最多等待 900 秒，不能增加 `--force`、绕过 eviction/PDB 或降低副本来通过。原 ES 成员须在不同宿主上重新就绪，原 PVC/PV/CSI 卷 UID 保持，绿色、确认数据与业务复验通过后，才能开始本宿主的系统维护。驱逐成功但迁移未完成时停在数据恢复阶段，不继续维护其他节点。

系统维护完成后，先确认宿主、运行时、网络和存储恢复正常，再恢复原来可调度的节点：

```bash
set -euo pipefail
kubectl wait --for=condition=Ready "node/$MAINT_NODE" --timeout=300s
kubectl uncordon "$MAINT_NODE"
kubectl get node "$MAINT_NODE" -o wide
kubectl -n elasticsearch get pods,pvc -o wide
esread '/_cluster/health?wait_for_status=green&timeout=30s'
```

节点 Ready 等待最多 300 秒。成员不一定迁回原宿主，这不等于恢复失败；判据是节点原调度状态、预期成员、原卷身份、绿色、无持续恢复和业务对账均恢复，再进入下一个故障域。仅解除 cordon 不会强制迁移现存成员。

> **失败停止与恢复：** drain 超时或 PDB 拒绝时保存未完成的 eviction、Pod、事件及 CSI 原因，停止系统维护和下一节点操作。若放弃本次维护，须先证明宿主仍健康、未进行系统破坏性操作且不存在卷双挂载风险，再恢复原调度状态并等待产品健康和业务复验；宿主是否安全不明时保留 cordon，由平台和存储管理员处理。整个过程保留原 PVC，不用删除卷、强拆附件或 finalizer 代替恢复。

## 8. 证书与凭据轮换

管理员先建立变更窗口、当前健康和可恢复点，保存 Secret 名称与版本摘要，不导出明文到证据。轮换遵循“增加新信任/身份、验证、切换、撤销旧值”的顺序。HTTP CA 变更先把新旧 CA 放入客户端信任集合，再更新服务端 Secret 并等待 ECK 协调，验证真实 Service/LB 主机名的新连接，最后撤销旧 CA。错误 CA/SAN 仍必须被拒绝。

应用 API key 可并行保留新旧 key，完成客户端切换并确认新写入/读取后撤销旧 key，复验旧 key 失败。密码账户先通过独立新身份切换或明确停机窗口处理同名密码替换，不能假定现存连接证明新密码有效。exporter 环境变量来自 Secret，仅修改 Secret 不会刷新运行进程的环境变量；更新客户管理的指标 Secret 后必须执行同一产品 setup，它会收敛指标账户，并根据 Secret UID/resourceVersion 更新 Pod 模板、等待 Deployment 滚动就绪，预期 `ELASTICSEARCH_MONITORING_READY`。随后按 7.2 核验真实采样和告警恢复；Deployment 就绪本身不能证明新密码已成功访问 ES。

S3 凭据更新后等待 ECK keystore 协调，再执行显式 `reload-secure-settings` 请求并检查所有节点无 reload 异常；随后做 repository verify 和小快照/读取验证。固定 ECK 3.5.0 将 secureSettings 内容摘要纳入 Pod 配置 hash，因此凭据内容变化会由控制器触发逐成员滚动；不需要人工删除 Pod，但必须预留滚动维护窗口。显式 reload 不能取消这次控制器滚动，也不能替代全部成员就绪。CA 更新需保证 ES JVM 使用新的 truststore，不能仅验证 mc 或 curl。任何一步失败先恢复旧的有效引用/信任，验证业务与 repository，再继续轮换。

### 8.1 更换客户 HTTP 证书或 CA

此任务由平台与安全管理员在第 2 节控制机和第 5.1 节管理连接中执行，适用于客户提供的 HTTP 证书；transport 证书继续由 ECK 管理。来源为客户 PKI 交付的 `/run/secrets/search-http-next/{ca.crt,tls.crt,tls.key}`，其中证书链完整、私钥权限 0600，并包含第 3 节要求的两个 Service DNS 和客户实际外部域名。这里不创建自签生产 CA，也不更改 ECK 内部 CA Secret。正式更换前保存 `$CFG` 中原 `elasticsearch_http_tls_secret` 的值和原 Secret UID，确认绿色、业务对账基线与维护窗口。

先把旧、新 CA 的重叠信任分发到全部应用客户端，并完成应用侧受控重载；管理连接也采用重叠信任。只替换服务端证书而没有完成客户端信任分发时停止。

```bash
set -euo pipefail
NEXT_CERT_DIR=/run/secrets/search-http-next
test -s "$NEXT_CERT_DIR/ca.crt"
test -s "$NEXT_CERT_DIR/tls.crt"
test -s "$NEXT_CERT_DIR/tls.key"
openssl x509 -in "$NEXT_CERT_DIR/tls.crt" -noout -checkend 172800
openssl verify -CAfile "$NEXT_CERT_DIR/ca.crt" \
  -verify_hostname search-client.elasticsearch.svc "$NEXT_CERT_DIR/tls.crt"
openssl verify -CAfile "$NEXT_CERT_DIR/ca.crt" \
  -verify_hostname search-es-http.elasticsearch.svc "$NEXT_CERT_DIR/tls.crt"
cat "$OPS_DIR/ca.crt" "$NEXT_CERT_DIR/ca.crt" > "$OPS_DIR/ca-overlap.crt"
mv "$OPS_DIR/ca-overlap.crt" "$OPS_DIR/ca.crt"
kubectl -n elasticsearch create secret generic search-http-next \
  --from-file=ca.crt="$NEXT_CERT_DIR/ca.crt" \
  --from-file=tls.crt="$NEXT_CERT_DIR/tls.crt" \
  --from-file=tls.key="$NEXT_CERT_DIR/tls.key"
```

证书至少还有两天有效期是本次操作的前置下限，不是生产推荐生命周期。上述检查不输出私钥；私钥与证书是否匹配由后续产品前置检查验证。`search-http-next` 已存在时停止核对，不覆盖。随后在 `$CFG` 把 `elasticsearch_http_tls_secret` 改为 `search-http-next`，执行 `kubecli setup "$CLUSTER" 07 --tags elasticsearch`。产品检查证书链、有效期、私钥和 SAN，ECK 协调新证书，数据面等待最长 1800 秒；产品同时刷新 exporter 的信任引用。

完成后用新 CA 单独验证管理连接，并在应用侧创建新连接完成原查询、写入对账及错误 CA/SAN 拒绝测试：

```bash
set -euo pipefail
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 40 \
  --noproxy '*' --cacert "$NEXT_CERT_DIR/ca.crt" --netrc-file "$OPS_DIR/netrc" \
  --connect-to search-client.elasticsearch.svc:9200:127.0.0.1:19200 \
  https://search-client.elasticsearch.svc:9200/
esread '/_cluster/health?wait_for_status=green&timeout=30s'
```

核对 ES cluster UUID、版本和原业务数据保持，再按 7.2 核验采集。真正更换了 CA 且各成员已加载新证书后，才从客户端撤销旧 CA，并证明仅信任旧 CA 的新连接失败；同 CA 的证书续期不能要求旧 CA 验证失败。保留旧 Secret 到客户回退窗口结束，由凭据管理流程退役。

> **失败恢复：** 不撤销仍工作的旧信任。将 `$CFG` 的证书引用恢复为已记录的原值，再执行同一 setup；若原来由 ECK 生成证书，原值为空字符串。客户端暂保留新旧信任，等待原证书连接、绿色、业务和采集恢复，再回收本次未使用的 Secret；恢复未完成或身份变化时保留现场，不删除原 Secret、业务 CR 或 PVC。依据为官方 [ECK HTTP 证书管理](https://www.elastic.co/docs/deploy-manage/security/k8s-https-settings)，本产品要求的 Secret 三个 key 比上游可选 CA 配置更严格。

### 8.2 更换 S3 访问身份

由对象存储管理员先创建相同 bucket/base_path 权限范围的新身份，保留旧身份有效；平台管理员在同一控制机取得权限 0600、无换行的新 key 文件 `/run/secrets/search-s3-next-access-key` 与 `/run/secrets/search-s3-next-secret-key`。记录当前 `elasticsearch_s3_secret` 引用及原 Secret UID。此任务只换访问身份，endpoint、bucket、base_path、CA 和 repository 的 writer/reader 角色保持原输入；更换后端或 CA 应拆为独立变更，不能合并验证结论。

```bash
set -euo pipefail
test -s /run/secrets/search-s3-next-access-key
test -s /run/secrets/search-s3-next-secret-key
kubectl -n elasticsearch create secret generic search-s3-next \
  --from-file=s3.client.default.access_key=/run/secrets/search-s3-next-access-key \
  --from-file=s3.client.default.secret_key=/run/secrets/search-s3-next-secret-key
```

在 `$CFG` 把 `elasticsearch_s3_secret` 改为 `search-s3-next`，保留第 9 节原 repository 请求，并将下列对象放在该 repository 请求之前；产品按列表顺序执行，先 reload 再验证仓库。已完成的一次性 snapshot/restore 请求可按开发手册的账本规则从当前列表移除，避免把已经退役的恢复点当成本次前置。

```yaml
  - id: s3-credentials-next-reload
    type: reload-secure-settings
```

执行同一 setup；ECK 按 secureSettings 协调，显式 reload 要求全部预期成员成功且没有 `reload_exception`。后续 repository 请求的 verify，以及 writer 的有界 analysis，均须成功。若首次运行早于 ECK 的异步 Secret 协调而失败，保留首个错误，先观察 ECK 事件与 keystore 协调，再在原 1800 秒维护等待窗口内以相同输入重入；不删除账本、不无限重试、不把 key 放进 Pod 命令行。

随后按第 9 节以新的请求 ID 和快照名创建小范围快照，并在独立目标恢复、核对内容。此时旧身份仍有效，这些结果只证明当前访问路径仍可工作；最后由对象存储管理员停用旧身份，再重复 repository verify 与一次新的小快照/独立读取，才证明不依赖旧身份缓存。reader 不执行快照写入，改为在旧读取身份停用后重新读取已核验的快照并完成独立恢复。整个过程不向外部系统输出 Secret。

> **失败恢复：** 旧身份尚有效时，把配置引用恢复为原 Secret，使用新的明确 reload 请求 ID 执行 setup，再核验 repository、原恢复点与业务。旧身份已停用时，由对象存储管理员按既有授权恢复旧身份或修复新身份，确认其范围和有效性后再收敛；没有有效身份时暂停快照任务并保留失败状态，不删 repository 文件或默认绕过 checksum。新旧身份与 Secret 的最终退役分别由客户凭据流程负责，不把创建新 Secret 当作已撤销旧身份。机制依据见官方 [ECK 快照与 secureSettings](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/cloud-on-k8s)和 [S3 secure settings](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/s3-repository)。

## 9. 快照、恢复与灾备

源集群通过 `elasticsearch_s3_secret` 和可选 `elasticsearch_s3_ca_configmap` 引用客户输入。EFK 与业务 ES 的 S3 access key 必须不同，产品会读取双方实际 Secret 内容进行双向检查，不能仅通过改 Secret 名称绕过。业务 repository 不允许使用 EFK 保留目录 `elasticsearch/logging`、其子目录或祖先目录 `elasticsearch`。以下是放入 `$CFG` 的请求结构示例，endpoint/bucket/region/base_path 取自客户对象存储配置；示例域名不是可执行的生产端点。

本章示例保护第 6 节尚未迁移的 `catalog-demo-v1`。若已执行第 6.1 节，先读回 `catalog-live` 当前指向，将 snapshot、SLM 和 restore 的显式索引列表一致改为实际物理索引 `catalog-demo-v2`，并重新保存数量与内容对账基线；不要继续保护已经停止接收业务写入的旧索引而遗漏当前数据。

```yaml
elasticsearch_s3_secret: search-s3
elasticsearch_s3_ca_configmap: search-s3-ca
elasticsearch_repository_role: writer
elasticsearch_requests:
  - id: repository-20261103
    type: repository
    name: business-backup
    endpoint: https://s3.example.com
    bucket: search-backups
    region: region-1
    base_path: business-search/production
    path_style_access: false
  - id: snapshot-before-change-20261103
    type: snapshot
    repository: business-backup
    snapshot: before-change-20261103
    indices: [catalog-demo-v1]
    feature_states: [none]
    include_global_state: false
```

管理员在同一控制机执行 `kubecli setup "$CLUSTER" 07 --tags elasticsearch`。repository 请求先注册并 verify，writer 还执行有界 analysis；snapshot 请求按稳定 ID 等待完成，默认最长 1800 秒。预期无失败 shard、快照含目标索引，并须在独立空目标验证内容/hash/query。不要把完成标志当作独立恢复成功。

**MinIO 的复制争用边界：** ES 9.5.6 的 analysis 会随机在对象上传尚未结束时发起复制；部分 MinIO 实现等待该上传释放锁，而上传又等待复制结束，最终出现 `Repository analysis timed out`。先保留 ES 堆栈，只有明确出现 `failed to copy blob before write` 且后端符合[官方 MinIO 测试已记录的限制](https://github.com/elastic/elasticsearch/pull/135565)时，才在同一集群配置中显式增加 `elasticsearch_repository_analysis_copy_during_write_contention: false`，移除已完成的一次性 snapshot/restore 请求，保留原 repository 请求及其 ID，再执行同一 setup。该可选项省略时为 `true`，不自动根据后端名称关闭检查；变更会触发所有 ES 成员受控滚动，须先确认集群绿色、副本完整、故障域和维护窗口允许。它仅采用官方 MinIO 测试的“关闭上传未结束时复制争用”条件，其余 analysis、verify、快照和独立恢复仍须成功，不能据此声称后端通过完整 S3 并发语义验证。任一步失败停止，不增大超时反复碰运气，也不跳过 analysis。复验三成员或专用角色的全部成员健康、原业务账本和第 9.2 节独立恢复；回退为 `true` 后再次执行 setup 并复验健康，但后端限制仍在时原探针可能再次失败。此设置为固定 ES 版本的上游弃用接口，升级前必须重新核对；它不修改 EFK 集群。

### 9.1 判断单次快照与启用周期备份

沿用第 5.1 节同一管理连接。先读取本次快照和产品请求账本：

```bash
set -euo pipefail
esread '/_snapshot/business-backup/before-change-20261103'
kubectl -n elasticsearch get configmap search-request-snapshot-before-change-20261103 \
  -o jsonpath='{.data.phase}{"\n"}{.data.receipt}{"\n"}'
```

快照必须为 `SUCCESS`、包含 `catalog-demo-v1`、失败分片为零，账本为 `complete` 且包含对应 snapshot UUID；`IN_PROGRESS` 继续在原 1800 秒窗口内观察，`PARTIAL`/`FAILED` 或 UUID 不符即停止。应保存文档数量、确定性内容摘要和代表性查询结果，供恢复对账。源端快照完成后仍需第 9.2 节独立恢复；只有该出口通过才接受此备份路径。

SLM 使用 `type: slm` 请求。以下对象增加到现有请求列表，不覆盖 repository 请求；例子每天 UTC 01:30 执行，只保护演示索引，保留 30 天、至少 5 个、最多 50 个本策略的成功快照。客户先根据允许 RPO、数据量和恢复测量选择实际周期及保留量；每天一次不能支持分钟级 RPO。

```yaml
  - id: catalog-slm-v1
    type: slm
    name: catalog-nightly
    policy:
      schedule: "0 30 1 * * ?"
      name: "<catalog-{now/d}>"
      repository: business-backup
      config:
        indices: [catalog-demo-v1]
        include_global_state: false
        feature_states: [none]
      retention:
        expire_after: 30d
        min_count: 5
        max_count: 50
```

执行同一产品 setup，预期 `ELASTICSEARCH_REQUEST_PASS id=catalog-slm-v1 type=slm`。管理员随后使用同一受信连接触发一次原生 SLM 执行并读取状态；此处的 PUT 只触发已由产品收敛的策略，不替代配置入口：

```bash
set -euo pipefail
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 40 \
  --noproxy '*' --cacert "$OPS_DIR/ca.crt" --netrc-file "$OPS_DIR/netrc" \
  --connect-to search-client.elasticsearch.svc:9200:127.0.0.1:19200 \
  --request PUT https://search-client.elasticsearch.svc:9200/_slm/policy/catalog-nightly/_execute
esread '/_slm/status'
esread '/_slm/policy/catalog-nightly'
esread '/_slm/stats'
```

PUT 返回 `snapshot_name` 只说明已调度；用该真实名称读取 `/_snapshot/business-backup/实际名称`，按上面的成功判据等待最长 1800 秒。策略状态应有本次成功记录，失败计数和原因需要进入告警；检查下次执行时间和控制机/节点时钟。SLM retention 是独立的集群周期任务，只处理该策略创建的快照，不会替用户清理手工快照。修改策略使用新请求 ID；不直接删除对象存储中的段文件。机制依据见官方[快照创建与 SLM](https://www.elastic.co/docs/deploy-manage/tools/snapshot-and-restore/create-snapshots)和固定版本的 [SLM 执行 API](https://github.com/elastic/elasticsearch/blob/fd1e8011342a0988e432b999cea4bf884ca120ed/rest-api-spec/src/main/resources/rest-api-spec/api/slm.execute_lifecycle.json)。

> **撤回错误策略：** SLM 的 stop API 会停止整个集群的 SLM，不能当作单策略暂停。若本例策略配置错误，先核对 GET 返回的定义确实属于本业务，把对应 `type: slm` 请求从 `$CFG` 移除，保留客户版本化配置中的旧定义，再使用下列原生删除命令取消这个策略的后续计划。删除策略不删除它已经产生的快照；正在运行的快照仍须读取状态。修正后以新请求 ID 经产品入口重新创建，并重复单次执行和独立恢复验证。

```bash
set -euo pipefail
curl --silent --show-error --fail-with-body --connect-timeout 5 --max-time 40 \
  --noproxy '*' --cacert "$OPS_DIR/ca.crt" --netrc-file "$OPS_DIR/netrc" \
  --connect-to search-client.elasticsearch.svc:9200:127.0.0.1:19200 \
  --request DELETE https://search-client.elasticsearch.svc:9200/_slm/policy/catalog-nightly
```

当前[源端验证](../../../tests/evidence/elasticsearch-backup-source-20261104-220408-6f48daf57cb945dcaceafbc554021d85.json)在两种拓扑各完成三次显式 SLM 快照，并在 `max_count: 2` 下触发原生 retention，核验最旧快照消失、最新两个仍为 SUCCESS；各 32 条原确认数据保持。测试使用 `expire_after: 1d`、`min_count: 1`，没有等待一天，也没有验证上述生产示例的 30 天过期周期。该结果说明应检查实际保留集合，不能仅凭策略创建成功推断清理生效；独立恢复结果仍按下一节另行判定。

平台在 `elastic-system/kubeauto-eck-repository-writers` ConfigMap 中保留业务仓库写入归属。相同 bucket 下不同写入者的路径相同或互为父子时会拒绝；即使 endpoint 使用不同 DNS 名称也按可能指向同一后端处理。并发注册使用 Kubernetes resourceVersion 条件更新，冲突发生在写入对象存储之前，应保留原请求 ID 和输入后重试。仓库名一旦绑定 bucket/path 与数据集群身份，不允许原地改指向；迁移时创建新仓库名和新的独立路径。卸载不删除归属记录，也不自动释放旧备份目录。首次使用时自动创建账本；既有账本缺少 claims 字段、被外来对象占用或内容损坏时停止，不通过删除账本解除冲突。平台管理员须先核对原 namespace UID、数据集群 UUID、恢复点和存储权限，再评估单独的数据退役。

这些约束保护 kubeauto 产品入口。Kubernetes 集群管理员或对象存储管理员仍能直接修改 CRD、资源、权限和数据；应用账户不应拥有这些权限。独立 access key 也不代表已经配置了最小权限，对象存储的桶/前缀授权仍须在其 IAM 或等效策略中限制。

### 9.2 在独立空目标恢复

恢复目标必须有独立集群身份、namespace/PVC、资源与网络边界，并设 `elasticsearch_repository_role: reader`。以相同 bucket/base_path 注册 readonly repository，然后提交 `type: restore` 请求，明确 repository、snapshot、indices 与唯一 id。产品拒绝覆盖已有目标索引。先恢复并核对业务数据，再开放写入或切换入口。security feature state 需要预先配置独立 file realm 应急管理员及其认证 Secret；普通索引恢复不自动覆盖安全状态。

平台管理员按第 2–5 节在独立客户 Kubernetes 集群建立空目标；目标版本、CSI、CA 和只读对象存储身份分别准入，不复用源 PVC、管理 Secret 或 kubeconfig。此时不要部署会写入 `catalog-demo-v1` 的应用。下面是目标 config 的请求列表结构，repository 的 endpoint/bucket/region/base_path 必须替换为源端同一组实际输入；使用第 3 节在目标自行创建的 Secret/CA 引用。这里的 `before-change-20261103` 是已经核验的源快照名，而非目标现场生成的新快照。

```yaml
elasticsearch_repository_role: reader
elasticsearch_s3_secret: search-s3
elasticsearch_s3_ca_configmap: search-s3-ca
elasticsearch_requests:
  - id: repository-reader-v1
    type: repository
    name: business-backup
    endpoint: https://s3.example.com
    bucket: search-backups
    region: region-1
    base_path: business-search/production
    path_style_access: false
  - id: restore-before-change-v1
    type: restore
    repository: business-backup
    snapshot: before-change-20261103
    indices: [catalog-demo-v1]
    feature_states: [none]
```

先保存源端的非敏感身份、快照 UUID 与业务对账基线，退出第 5.1 节的源管理 shell，令其 EXIT trap 回收源端 port-forward 和私有临时文件；保留客户凭据系统中的原始输入。再在控制机开启新的 root 会话，建立下面的目标上下文并执行产品入口。不要在源连接仍占用本地 19200 端口时重复建立目标转发。示例 `search-recovery` 是客户按前述章节实际创建的目标目录名；命令执行前把它改为真实名称：

```bash
set -euo pipefail
cd /usr/local/kubeauto
TARGET_CLUSTER=search-recovery
TARGET_CFG="$PWD/clusters/$TARGET_CLUSTER/config.yml"
CLUSTER="$TARGET_CLUSTER"
CFG="$TARGET_CFG"
export KUBECONFIG="$PWD/clusters/$TARGET_CLUSTER/kubectl.kubeconfig"
test -s "$TARGET_CFG"
test -s "$KUBECONFIG"
kubectl config current-context
kubectl get namespace kube-system -o jsonpath='{.metadata.uid}{"\n"}'
kubecli setup "$TARGET_CLUSTER" 07 --tags elasticsearch
kubectl -n elasticsearch get configmap search-request-restore-before-change-v1 \
  -o jsonpath='{.data.phase}{"\n"}{.data.receipt}{"\n"}'
```

目标 Kubernetes UID 和 ES `cluster_uuid` 必须分别不同于源；产品等待目标索引绿色及 primary 从指定 repository/snapshot 完成恢复，最长 1800 秒。成功账本包含 snapshot UUID、目标索引 UUID。随后在目标重建第 5.1 节管理连接。普通索引快照不恢复原生用户域（native realm）中的用户/角色；按第 6 节的身份准备步骤，仅创建目标独立应用密码 Secret、追加 role/user 请求，并在目标执行同一 setup。不要运行该节演示程序的首次建索引和写入动作，也不要为恢复普通数据而启用 security feature state。

给目标应用挂载目标 CA/凭据，使用恢复后的现存索引核对数量、逐条内容摘要、查询/聚合及越权拒绝；源端演示数据若未修改，恢复后仍应命中 `item-1001`，价格为 79.0。此时只运行读取验证，暂不启动目标业务写入者，待第 9.3 节确认单一写入入口后再启用新写入。仅索引 UUID、节点健康或快照状态正确，均不能代替这些业务结果。

恢复 `security` 会覆盖 native realm 用户/API key 等安全状态；恢复 `kibana` 还要匹配 Kibana 版本并等待 saved object migration。二者不是普通索引恢复的默认步骤。应由安全管理员预先在目标创建独立 file realm 应急管理员，配置 `elasticsearch_file_realm_secret` 与 `elasticsearch_admin_secret`，并在覆盖前用新连接确认 `authentication_realm.type=file`、具备 superuser 权限。ECK 的 file realm Secret 采用官方 `users`/`users_roles` 格式；密码哈希文件、原密码与管理 Secret 在凭据系统中统一维护。应急身份缺失、版本不兼容或目标仍有业务写入时停止；普通业务只需 `feature_states: [none]`，不为省事恢复全部系统状态。

### 9.3 灾备切换与回切判据

灾备切换先隔离源写入，记录最后确认业务账本与快照恢复点，在目标恢复并对账，测量从宣布故障到业务可用的 RTO 及恢复点差距 RPO，再切换客户稳定入口并验证目标新写入。回切必须保护目标新增数据并设计重放/冲突处置，不能同时开放旧源。该路径不提供任意时间点 PITR，snapshot 也不保证跨 shard 严格同一瞬时业务视图。

`elasticsearch_repository_role: reader` 只将目标的快照仓库注册为只读，不会禁止恢复后的业务索引接收写入。源、目标也不会因为使用同一快照自动建立复制关系。切换负责人必须先通过客户已有的应用入口及基础设施隔离机制证明旧写入者不能继续写，再开放目标；源不可达本身不能证明源已停止。回切同样先停止目标写入并保存其确认账本，将目标新增数据按稳定业务 ID 重放到源，遇到同 ID 不同内容时停在冲突处置，不直接覆盖。只有数量、内容摘要、代表性查询和新增写入全部复验通过后，才恢复源入口并保持目标写入关闭。若隔离证明、增量记录或冲突处置缺失，保持当前唯一已确认的写入端，暂停切换。

当前固定版本的[受控灾备演练](../../../tests/evidence/elasticsearch-dr-20261105-021409-b752d811ae0f459c81b8d58b92f0ae3c.json)在已准备的独立 Kubernetes/CSI/空目标上实测切换 RTO 163.04 秒；从旧快照到确认探针的间隔为 14864.99 秒，恢复点之外有 1 条源写入。回切后原 32 条加源、目标各 1 条新增记录，共 34 条完整对账，目标保持写阻断。此测量不含平台建设、客户 DNS/LB 传播或回切耗时；受控停止源成员不能替代生产网络分区时的外部隔离证明。机制、测量起止和外推边界见[白皮书第 7 节](technical-whitepaper.md#7-快照恢复与灾备)。

> **失败恢复：** 同一请求 ID 不得更换输入。中断 restore 先查恢复状态、目标索引和账本，禁止自动删除目标重试。错误 S3 CA、凭据或后端语义先修所属层，再验证 verify/analysis/小快照和独立读取；不默认绕过 checksum。

### 9.4 恢复原生用户与 Kibana 保存对象

此任务由安全与平台管理员共同执行，针对 ES/Kibana 均为 9.5.6 的独立空目标，替代 9.2 节的普通索引恢复步骤，不能在已经恢复业务索引的目标上追加执行。它会覆盖目标的 security/Kibana 系统状态；目标应用与 Kibana 必须停止，源业务不因本任务停写。源端先保存用户/角色名称、代表性授权查询以及一个客户已知保存对象的 ID、类型、属性，作为恢复基线。只需普通索引时继续使用 `feature_states: [none]`，不要扩大恢复范围。

在源 `$CFG` 的既有 repository 请求后增加新的 snapshot 请求，显式使用 `feature_states: [security, kibana]`、`include_global_state: false`，其余索引列表沿用本章已核验的实际物理索引；使用新的请求 ID 与快照名，例如 `catalog-feature-backup-v1`。通过同一产品 setup 创建，再读取快照：须为 SUCCESS、零失败分片，并且返回的两个 feature state 均有非空索引清单。没有启用源 Kibana、没有代表性保存对象或缺少任一 state 时停止，不用一个普通快照代替。源端 config.yml、Secret/CA、对象存储输入及 Kibana 加密密钥分别由客户凭据与配置备份流程保护；快照不会保存这些文件。包含加密连接器等对象时还须恢复原加密密钥，本节的普通保存对象路径不能证明这些对象的外部凭据也已恢复。

恢复后的 native 用户采用快照时的安全状态。源端后来轮换密码，不会改变已经完成的旧快照；恢复旧快照时，应从凭据系统取回对应恢复点的原凭据，不能把刚生成的新密码 Secret 当作旧快照中的密码。若要保护轮换后的状态，使用新的请求 ID 与快照名建立并验证新恢复点。独立 file realm 管理员仍用于恢复期间的管理，不依赖被覆盖的 native 用户状态。

随后按 9.2 节切换到独立目标上下文，但暂不提交 restore 请求。先以 `reader` 注册源 repository，设置 `elasticsearch_kibana_enabled: false`，执行 setup，确认目标没有业务索引、Kibana CR/Pod 已消失、没有目标写入者，再建立第 5.1 节目标管理连接。安全管理员提供 `/run/secrets/search-recovery-password`：独立随机 ASCII 密码、无换行、权限 0600，不能复用源 native 用户或依赖待恢复的 security 索引。下面在该目标控制机会话中生成本地 file realm 文件，内容只写入现有私有 `$OPS_DIR`，不输出密码或哈希：

```bash
set -euo pipefail
test -s /run/secrets/search-recovery-password
python3 - <<'PY'
import base64, hashlib, os, pathlib, secrets
root = pathlib.Path(os.environ["OPS_DIR"])
password = pathlib.Path("/run/secrets/search-recovery-password").read_bytes()
assert password.isascii() and len(password) >= 24 and not any(c in password for c in (b"\n", b"\r", b"\x00"))
salt = secrets.token_bytes(32)
digest = hashlib.pbkdf2_hmac("sha512", password, salt, 100000, dklen=32)
encoded = "{PBKDF2}100000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()
(root / "users").write_text("search_recovery:" + encoded + "\n")
(root / "users_roles").write_text("superuser:search_recovery\n")
PY
kubectl -n elasticsearch create secret generic search-recovery-realm \
  --from-file=users="$OPS_DIR/users" --from-file=users_roles="$OPS_DIR/users_roles"
kubectl -n elasticsearch create secret generic search-recovery-admin \
  --from-literal=username=search_recovery \
  --from-file=password=/run/secrets/search-recovery-password
```

Secret 同名已存在时停止核对，不覆盖。`users` 使用固定 9.5.6 支持的 PBKDF2-HMAC-SHA512 格式；`users_roles` 将此独立身份映射到产品要求的 `superuser`。ECK 向全部成员分发 file realm，不能只在一个 Pod 手工加用户。上述权限用于本次原生快照恢复和状态读取，不授权直接修改 restricted 系统索引；官方的专家级系统索引修复可能还需临时自定义权限，不属于本节入口。来源见官方[file realm](https://www.elastic.co/docs/deploy-manage/users-roles/cluster-or-deployment-auth/file-based)与固定版本的 [Hasher](https://github.com/elastic/elasticsearch/blob/fd1e8011342a0988e432b999cea4bf884ca120ed/x-pack/plugin/core/src/main/java/org/elasticsearch/xpack/core/security/authc/support/Hasher.java)。

在目标 `$CFG` 设置以下引用，保留 reader repository 请求，仍不添加 restore：

```yaml
elasticsearch_kibana_enabled: false
elasticsearch_file_realm_secret: search-recovery-realm
elasticsearch_admin_secret: search-recovery-admin
```

执行同一 setup，最长等待 1800 秒，先验证 file realm 已生效。为当前目标管理连接切换 netrc，不更改目标 CA 或 Service 主机名：

```bash
set -euo pipefail
python3 - <<'PY'
import json, os, pathlib
password = pathlib.Path("/run/secrets/search-recovery-password").read_text()
pathlib.Path(os.environ["OPS_DIR"], "netrc").write_text(
    "machine search-client.elasticsearch.svc login search_recovery password " + json.dumps(password) + "\n")
PY
esread '/_security/_authenticate'
```

认证响应必须是 `username=search_recovery`、`authentication_realm.type=file` 且 roles 包含 `superuser`。只有此出口通过，才把 9.2 节 restore 请求改为新的 ID、已核验的 feature 快照名及 `feature_states: [security, kibana]`，索引列表与快照一致，经产品 setup 执行；保持 Kibana 关闭。产品等待各业务/feature primary 来自指定 snapshot 的恢复完成、绿色和完成账本，最长 1800 秒。随后先核对业务数据，再用源备份中已知的 native 用户新建连接，验证认证域、原角色、允许查询及越权拒绝。

恢复通过后，从配置中移除已完成的一次性 restore 请求，保留其完成账本，再设 `elasticsearch_kibana_enabled: true` 并执行 setup。预期两个 Kibana 实例就绪、状态绿色，再用恢复后的业务身份进入同一 space，读取此前保存的精确对象 ID，核对类型与属性；出现迁移失败时停止开放业务，保存 Kibana 状态和首个迁移错误，不手工删除系统索引。单纯登录成功不能证明保存对象恢复，普通 dashboard 成功也不代表加密连接器已恢复。

> **失败停止与恢复：** 保留独立 file realm、原快照和产品请求账本，停止目标写入与 Kibana；不反复覆盖部分恢复的目标，不删除系统索引或改回快照名来重试。先检查实际恢复状态与首次错误；需要重建时，只在完成该目标的数据保留和身份核验后按第 10 节退役，另建干净独立目标重新恢复。源集群保持原入口。应急身份退役前，客户必须另有已验证的独立管理入口和最新恢复点，再通过配置撤销两个引用、执行 setup、验证正常管理及业务，最后按凭据流程退役对应 Secret 与密码文件；退出当前 shell 时第 5.1 节 trap 回收本地私有文件。

> **系统索引诊断：** 系统 data stream 的 backing index 不允许直接通过普通 `_settings` 请求读取，返回“access is reserved for system operations”并不表示快照恢复失败或管理员缺少角色。产品通过仅含 UUID 的集群元数据核验这些 feature 索引；恢复是否完成仍以指定快照的 primary recovery、绿色、完成账本及业务/原生用户/保存对象复验共同判断。若恢复已完成但取证失败，保留 `running` 账本及首次错误，不手工改成 `complete`，也不通过特殊内部请求头或扩大权限绕过限制。

## 10. 升级、回旧版与卸载

升级前记录版本与镜像摘要、所有 ECK 消费者、健康、容量、业务账本和已独立验证的升级前快照。固定升级来源是 ES/Kibana 9.5.5→9.5.6；修改 `elasticsearch_ver` 后运行同一 setup，等待滚动与业务复验。ECK 3.4.1→3.5.0 属于平台维护，只能使用本节的 `eck-platform` 入口；普通安装和业务配置均不能触发共享控制器升级。

版本降级不是镜像回滚。产品拒绝把已升级 CR 的版本改小；如需回旧版，准备旧版空集群，恢复升级前兼容快照，处置升级后的新增写入并重新对账，或按官方支持路径前滚。不可恢复的数据格式和缺少新增写入记录是停止条件。

### 10.1 分开完成共享 ECK 与业务版本升级

由平台管理员与业务负责人在第 2 节控制机、原 `$CFG` 和第 5.1 节管理连接中执行。本路径只接受已由本产品管理的 ECK 3.4.1 和 ES/Kibana 9.5.5；已经是目标版本时跳过相应升级阶段，不伪造来源、不重新安装控制器。先按 7.1 检查健康、成员与恢复余量，按第 9 节完成升级前快照及独立恢复，保存客户版本化配置、业务确认账本、CR/PVC/Secret UID 和当前镜像摘要。使用插件时，先确认目标同版插件镜像和索引迁移边界；来源不属于固定升级对时停止，另行评估官方升级路径。

共享 ECK 影响所有 namespace 中的消费者。先只读盘点，逐项与各业务负责人确认版本、恢复点、变更窗口及业务复验负责人：

```bash
set -euo pipefail
kubectl -n elastic-system get statefulset elastic-operator -o yaml
kubectl get elasticsearch,kibana -A
kubectl get crd elasticsearches.elasticsearch.k8s.elastic.co kibanas.kibana.k8s.elastic.co
```

ECK 官方说明升级可能更新全部受管资源并触发滚动，因此只看业务 ES 健康不足以签署共享控制器变更。确认范围后，保持 `elasticsearch_ver: "9.5.5"`，仅把 `$CFG` 中的 ECK 字段设为以下值；`search-eck-350-v1` 是本次变更的稳定 ID，同一 ID 不更换消费者或输入：

```yaml
eck_platform_upgrade_from: "3.4.1"
eck_platform_change_id: search-eck-350-v1
```

保持旧字段 `elasticsearch_eck_upgrade_from`、`elasticsearch_eck_upgrade_change_id` 为空，非空会在业务变更前被拒绝。平台管理员在原控制机、工作目录和 `$CLUSTER` 执行：

```bash
set -euo pipefail
kubecli download -E elasticsearch
kubecli setup "$CLUSTER" 07 --tags eck-platform
```

普通无 tags 的 setup 不运行平台任务。平台入口当前准入 EFK 9.5.1 和业务 ES/Kibana 9.5.5、9.5.6 消费者；未知类型、版本、所有权或退役中的消费者阻断升级。首次启动维护还要求全部消费者已观测当前代次且绿色健康，ES 全部声明节点可用。EFK 普通入口也会在变更前拒绝其它 Stack 版本；未来版本须完成兼容性准入后才能扩大允许范围。产品先更新配套 CRD，等待 Established（各最长 120 秒），再等待 Operator 滚动（最长 10 分钟），随后等待所有消费者当前代次恢复绿色健康（最长 1800 秒），并核对消费者 UID 和配置摘要。超时或身份变化会保留 `running` 账本，不能作为成功继续业务升级。CR 健康不替代业务数据对账。出口须同时包括 `ECK_SHARED_UPGRADE_COMPLETE change_id=search-eck-350-v1`、变更 ConfigMap 的 `phase=complete`、原 Operator StatefulSet/消费者 CR/PVC/凭据身份保持，以及包括 EFK 在内的各消费者业务复验。不要手工只换 Operator 镜像而保留旧 CRD。

```bash
set -euo pipefail
kubectl -n elastic-system get configmap kubeauto-eck-change-search-eck-350-v1 \
  -o jsonpath='{.data.phase}{"\n"}{.data.from}{"\n"}{.data.to}{"\n"}'
kubectl -n elastic-system get statefulset elastic-operator -o wide
kubectl get elasticsearch,kibana -A
```

ECK 阶段通过后，把配置中的 `eck_platform_upgrade_from`、`eck_platform_change_id` 恢复为空字符串，保留完成账本；将 `elasticsearch_ver` 改为 `"9.5.6"`，再执行 `kubecli setup "$CLUSTER" 07 --tags elasticsearch`，不同时改变拓扑、容量或凭据。ECK 负责成员顺序与安全预算，不对其 StatefulSet 直接执行手工版本替换。用以下只读检查观察结果：

```bash
set -euo pipefail
kubectl -n elasticsearch get elasticsearch search -o yaml
kubectl -n elasticsearch get kibana,pods,pvc -o wide
esread '/_cat/nodes?format=json&h=name,version,node.role'
esread '/_cluster/health?wait_for_status=green&timeout=30s'
esread '/_cat/recovery?format=json&active_only=true'
```

业务等待默认最长 1800 秒，普通 API 请求最长 40 秒。全部预期成员须为 9.5.6、当前代次 Ready、绿色、无未完成恢复，启用的 Kibana 也须完成同版协调；原 CR/PVC 与业务确认数据保持，应用读写、越权拒绝及监控复验通过后才结束窗口。升级期间先按已审批的停写或增量记录方案处理业务，不把控制器的单成员预算解释为零请求错误保证。

当前[固定升级对的实际验证](../../../tests/evidence/elasticsearch-upgrade-20261105-055359-527858c2428943c9b49ed1a9fff0ed1d.json)已完成 ECK、ES/Kibana 顺序升级、Operator Pod 丢失后的恢复和相同配置重入，原 PVC、凭据及 32 条确认数据保持；原地降级被拒绝，独立空 9.5.5 目标从既有兼容恢复点恢复后逐条对账一致。恢复点及边界见[白皮书第 8 节](technical-whitepaper.md#8-生命周期与性能判断)。客户变更仍须为自己的最新确认数据建立并独立验证恢复点，不能以实验室的既有快照代替生产备份。

> **失败停止与恢复：** 首个前置或升级阶段失败即停止后续版本变更，保存产品日志、当前镜像/CRD、变更账本、成员事件和原确认账本。相同输入且身份未变时可从同一产品入口继续；账本冲突或身份变化时保留现场，不删账本或强制接管。已经升级的数据成员不能改回旧 tag。需要回旧版时，以独立旧版空目标恢复升级前兼容快照，按 9.3 的单写入口和新增数据重放规则切换；没有可恢复点或增量处置方案时不回切。官方依据为 [ECK 升级](https://www.elastic.co/docs/deploy-manage/upgrade/orchestrator/upgrade-cloud-on-k8s)与 [Elastic Stack 升级](https://www.elastic.co/docs/deploy-manage/upgrade/deployment-or-cluster)，产品的固定来源、所有权和稳定变更 ID 限制更严格。

### 10.2 卸载与最终数据退役

卸载业务前先停客户端、保护最新恢复点并记录 PVC/Secret/快照清单。在客户版本化配置中保留原运维请求定义，再在 `$CFG` 保持 `elasticsearch_install: "yes"`，设置 `elasticsearch_state: absent` 和 `elasticsearch_requests: []`，执行同一 setup；携带运维请求的卸载会在前置检查拒绝。预期 `ELASTICSEARCH_ABSENT_PASS pvc=retained secrets=retained eck=retained`，仅删除业务 CR 与自有入口，保留 namespace、PVC、客户 Secret、外部快照与共享 ECK。业务 CR 删除等待最长 10 分钟；超时先检查 ECK 事件、finalizer 和挂载状态，不强拆 finalizer 或删除 PVC。成功后分别查 CR/业务 Service 已消失，原 PVC/Secret UID 保持、共享 Operator 仍就绪，并从仍存活的其他 ECK 消费者验证业务。只把 install 改为 no 表示不再收敛，不会自动卸载。

共存实践分别卸载、重入两种业务拓扑后，standard 的 32 条、compact 的 34 条原确认数据内容保持，EFK 9.5.1 的哨兵与独立快照保持；反向退役、重入 EFK 后，两套业务仍健康。实践条件及分段证据边界见[白皮书第 1 节](technical-whitepaper.md#1-产品边界与技术体系)。客户执行时仍须为自己的每个消费者分别记录卷、凭据、数据与快照身份；业务卸载成功不能替代其他消费者的复验。

最终数据销毁是独立授权动作，需要验证所有权、备份保留要求和所有消费者；不得删除全局 CRD 或共享 Operator 来卸载单个业务，也不得使用 namespace 全删代替客户数据清单审批。

## 11. 故障入口与参数速查

| 现象 | 最小检查与分叉 | 处置与复验 |
| --- | --- | --- |
| setup 在前置拒绝 | 首个 `ELASTICSEARCH_REFUSED`/`ECK_SHARED_REFUSED`；资源所有权、版本、watch、SC | 修明确输入或平台条件；外来资源不接管；重试后核对业务与身份 |
| Pod Pending | describe、events、节点 requests/taints、PVC/CSI | 先区分调度与卷，不手工删数据卷；就绪后查分片与业务 |
| ES 已 green，但重建成员不 Ready | 成员日志与就绪探针、`GET /_nodes/shutdown`、共享 Operator 副本及协调日志 | 按下文区分重启记录未清理与数据面异常；恢复控制器协调后复验节点、分片及确认账本 |
| TLS 失败 | 真实访问名、SAN、CA 链、客户端时间 | 恢复正确证书/信任；错误 CA/SAN 必须仍失败 |
| 401/403 | 身份、Secret 版本、角色索引范围 | 修凭据或权限，验证新身份允许及越权拒绝；不无限重试 |
| yellow/red | health、allocation explain、recovery、磁盘/节点状态 | 恢复缺失副本或真实容量；禁止强制空 primary；核对确认账本 |
| 写入 429/超时 | 错误类型、Bulk item、磁盘水位、节点压力、客户端重试账本 | flood block 先恢复真实空间；过载才限流与有界退避；超时先查结果，固定 ID 防止重复 |
| snapshot 失败 | repository verify/analysis、JVM CA、凭据、后端错误 | 修 S3 所属层；小快照和独立恢复通过后再恢复计划 |
| Prometheus 无数据 | ServiceMonitor selector、target、exporter、ES 权限/CA | 分别恢复采集链与 ES；验证规则、真实样本及 absent 告警 |

对“成员已归队但不 Ready”，管理员先建立[5.1 节的管理诊断连接](#51-建立管理诊断连接)，以 `esread '/_nodes/shutdown'` 只读检查退出记录，并将记录的 node ID 与 `esread '/_nodes'` 对应。若是 `RESTART / COMPLETE`、节点已经归队且 Pod 不再终止，同时共享 Operator 被维护操作暂停，应先按原维护记录恢复该控制器的期望状态和协调；暂停原因、所有权或原副本数不明确时停止并交由平台维护人员处理。Operator 正常时继续检查其首个协调错误及节点日志，不直接删除退出记录。按安装入口的健康超时等待节点 Ready（默认最长 1800 秒），随后核验 green、预期成员、业务读写和确认账本；超时保留控制器及节点诊断，不能靠删除卷或替换就绪探针消除现象。机制与固定版本来源见白皮书[仲裁、拓扑与故障域](technical-whitepaper.md#4-仲裁拓扑与故障域)。

下表字段均位于 `$CFG`，统一前缀为 `elasticsearch_`，表中省略此前缀；值来自项目默认配置，区别于 Elasticsearch/ECK 上游默认。修改后通过同一 setup 收敛；涉及 Pod 配置的变更可能滚动重启，按维护窗口处理。

| 配置 | 默认与影响 | 生效/边界 |
| --- | --- | --- |
| install/state | no / present；只有 install=yes 才执行 | absent 保留数据；no 不等于卸载 |
| namespace/name | elasticsearch/search，字符串 | 首次安装确定资源名；改名会指向新资源，不能当作迁移 |
| ver/eck_ver | 业务 9.5.6 / 3.5.0 | 只接受固定升级对；EFK 的 9.5.1 不随此字段升级 |
| eck_upgrade_from/eck_upgrade_change_id | 空/空 | 旧业务字段仅允许为空；共享升级改用无此前缀的 eck_platform_upgrade_from/eck_platform_change_id，见 10.1 节 |
| topology/master_nodes/data_nodes | standard / 空列表 | 节点由客户填写；不原地转换拓扑，缩容默认拒绝 |
| data_count | 整数 0，表示候选节点数 | standard 至少 3，compact 必须恰好 3，不超过候选数；增加候选不等于增加成员 |
| `*_cpu` / `*_memory` / data_heap | master 2/4Gi，data 4/16Gi/8g | Pod 资源/堆；heap 不超过内存一半，变更可滚动 |
| `*_pvc_size` / storage_class | master 20Gi、data 200Gi / 空 | 显式 CSI；允许扩容，拒绝缩小/换类 |
| manage_sysctl/node_ssh_user | true/root | 密钥认证；写专用文件，实际阈值不得降低 |
| tolerate_control_plane | false，布尔值 | 生产默认不在控制节点调度；true 只允许已批准的资源隔离方案 |
| http_tls_secret | 空，ECK 生成 | 自定义时提供 CA/证书/私钥和正确 SAN |
| file_realm_secret/admin_secret | 空/空 | 前者 key 为 users、users_roles；后者为 username、password；security 恢复前验证独立应急身份 |
| s3_secret/s3_ca_configmap | 空/空 | 前者注入两项 s3.client.default secure settings；后者 ca.crt 进入 JVM truststore，CA 变化触发滚动 |
| kibana_enabled | false，布尔值 | 启用时同版两副本，另计 CPU/内存与反亲和容量 |
| smartcn_enabled | false，布尔值 | 只接受 9.5.6 固定插件镜像；先下载，旧字段需重新索引 |
| exporter_enabled/exporter_secret | true/空 | 必须提供独立最小权限身份；环境变量变更需滚动 |
| monitoring_namespace/release | monitor/prometheus | 必须匹配实际 Prometheus 选择器 |
| network_policy_enabled | true | 客户端 namespace 标签授权；不是防火墙与权限的替代品 |
| repository_role | writer | DR 目标 reader；同一 base_path 一个 writer |
| wait_seconds | 1800，允许 60–3600 | 超时保存首失败，不把继续等待当作成功 |
| requests | 空列表 | 稳定 ID、不可变输入指纹；Secret 引用，不含明文凭据 |

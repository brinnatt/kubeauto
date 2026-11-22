# Kubernetes 1.36 升级与 Harbor 2.16 运维手册

适用范围为 `v1.35.10 → v1.36.6`，以及本项目独立 Compose Harbor 向官方
`v2.16.0` 的迁移。面向拥有集群配置、主机管理权限及维护窗口的实施人员。
使用前先核对[交付组合](release.yaml)和[验收矩阵](test-matrix.yaml)：待验收路径
不得用于客户生产，本文中的操作定义不代表这些路径已经验收或客户已经签收。

当前产品发行 [v0.1.10](https://github.com/brinnatt/kubeauto/releases/tag/v0.1.10)
包含已验收的 ARM 2.14.4 桥接实现。运行镜像为
`hub.talkedu.cn/kubeauto/kubeauto:v0.1.10`，保留 `brinnatt/kubeauto:v0.1.10`
和 `ghcr.io/brinnatt/kubeauto:v0.1.10` 回退。选择与控制机 CPU 对应的二进制，
按发行文件校验 SHA256；精确源码、构建和三端双架构摘要见
[当前发布记录](evidence/product-arm-publication.json)。原
[v0.1.9 发布记录](evidence/product-publication.json)保留历史范围，该旧发行不含新增 ARM 桥接。
本次原生 ARM 已完成迁移、原点恢复、干净新建与幂等验收；镜像和二进制发布成功
仍不能替代客户现场的源状态、恢复点及维护窗口检查。

任务导航：[准备Kubernetes升级](#准备kubernetes升级) · [执行与续跑](#执行与续跑)
· [Harbor](#harbor) · [私有制品使用](#私有制品使用) · [日常运维](#日常运维)
· [异常诊断与恢复](#异常诊断与恢复)。原理见[技术白皮书](technical-whitepaper.md)。

## 准备Kubernetes升级

本跳只接受实际 `1.35.10` 源，不从 `1.33` 或 `1.34` 直接跳入。使用目标分支
构建的 `kubecli`，保留原集群目录、CA、kubeconfig、库存和业务恢复点。
在控制机原数据根目录执行，集群名必须与既有 `clusters/` 目录一致：

```bash
export KUBEAUTO_BASE_PATH=/usr/local/kubeauto
export CLUSTER=实际源集群名
test -f "$KUBEAUTO_BASE_PATH/clusters/$CLUSTER/config.yml"
test -f "$KUBEAUTO_BASE_PATH/clusters/$CLUSTER/kubectl.kubeconfig"
kubecli version
```

`version` 中的 `1.36.6` 是执行文件的发行默认值，不是现场版本；源配置和实际API、
kubelet仍应是 `1.35.10`。不要预先把源配置版本改成目标，也不要覆盖成新建模板。
下载的 Kubernetes 六文件、扩展包与镜像必须匹配[制品固定记录](artifact-pins.json)。
本跳保留 etcd `3.6.16`、containerd `2.3.7`、nerdctl `2.4.2` 和 Calico `3.32.4`。

执行前确认所有 API 后端、etcd 成员和节点健康，主机时间正确；Calico主机内核至少
5.10、使用已评审的cgroup配置。当前原地自动入口支持Calico直接etcd组合，其它CNI
不会被自动切换。含gitRepo卷、非空DRA资源、未知feature gate或未迁移API的工作负载
须先由其负责人解决，不能删除数据以通过预检。原有get-only streaming RBAC也需先
核对create授权；产品不自动拓权。

按原数据库、消息和存储手册保存业务一致恢复点。etcd快照只保护控制面，不包含PVC
内容。维护窗口需要考虑逐节点驱逐、原PDB、容量与卷节点亲和性；单副本及local-path
业务可能短暂停顿，这条路径不承诺零停机。

准备已有HTTP业务账本的JSON：namespace中的Service、Bound PVC与客户端标签必须
对应真实对象。客户端唯一Ready Pod中需有wget，能读取
`http://SERVICE:8080/ledger`。账本SHA256包含响应末尾换行。

```json
{
  "namespace": "upgrade-ledger",
  "service": "ledger",
  "pvc": "ledger",
  "client_selector": "app=upgrade-client",
  "ledger_sha256": "替换为已确认原HTTP响应的64位SHA256"
}
```

将其以0600保存为控制机的 `/root/upgrade-continuity.json`。此文件只描述已有业务；
产品不会自动创建测试应用。先独立核对原数据，再执行准备：

```bash
kubecli upgrade "$CLUSTER" --prepare --continuity /root/upgrade-continuity.json
```

成功出口是 `UPGRADE_PREPARE_PASS`，并产生与库存、源对象、恢复快照绑定的receipt。
它包括快照完整性及离线恢复校验，仍不能替代应用灾备演练。任一源健康、权限、
版本或恢复前置失败时停止，保留原始错误；不要先调用旧 `93.upgrade.yml` 绕过检查。

## 执行与续跑

准备通过后使用同一源目录和同一目标执行文件：

```bash
kubecli upgrade "$CLUSTER"
```

第一次成功停在控制面交接，输出 `UPGRADE_CONTROL_PLANE_STAGE_PASS`：三类API后端
必须全到目标后才推进控制器，kubelet仍可处于源版。核验业务及该边界后再次调用同一
命令，逐节点执行drain、节点更新、调度恢复及已安装DNS/metrics检查。完整成功出口是
`UPGRADE_NEXT_HOP_STAGE_PASS`，最后才原子发布目标配置。

独立诊断可使用原kubeconfig：

```bash
export KUBECONFIG="$KUBEAUTO_BASE_PATH/clusters/$CLUSTER/kubectl.kubeconfig"
kubectl get nodes -o wide
kubectl get --raw /readyz
kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes
kubectl -n upgrade-ledger get pvc,service,pods
```

以上是只读诊断，不替代产品的逐后端检查和业务账本。验收还需原namespace/PVC/PV/
Service UID、卷绑定及确认数据保持；节点Ready本身不证明这些结果。未安装metrics
时不以metrics路径作为其安装授权。

失败时保留 `/var/tmp/kubeauto-upgrade-$CLUSTER/` 的journal、receipt和首失败输出。
先按[诊断表](#异常诊断与恢复)处理首个因果层。PDB拒绝时恢复业务可迁移条件，
不得删除客户PDB、强删Pod或加入清空本地数据参数。恢复条件通过后重用原命令续跑，
不要删除journal重头开始，不要将目标二进制回盖源版。成功后再调用同一命令应只
完成幂等核对，不能重装业务或改写无关组件。

### 已验证的升级边界

本跳已在六台原生AMD64、Rocky Linux 9.8节点上，使用containerd 2.3.7、etcd
3.6.16、Calico 3.32.4及local-path原卷，完成以下产品行为验证。结果适用于相同
接口与前置条件，不构成客户任意拓扑的停机时间或性能保证。

| 检查点 | 实际结果与客户判断 |
| --- | --- |
| 控制面交接 | 三台API均为1.36.6且就绪后推进控制器，六台kubelet在交接点仍为1.35.10；控制面成功不等于节点升级已完成 |
| 驱逐保护 | 测试PDB真实拒绝节点驱逐；产品保留cordon和原journal。解除同一PDB的阻断条件后从原边界续跑，不跳过保护 |
| 节点终态 | 六节点均为1.36.6且Ready，原节点UID、三台master的cordon及三台worker的可调度状态保持 |
| 业务及接口 | 原PVC/PV绑定、Service和HTTP确认账本保持，DNS与metrics实际读回成功；再次调用完整升级入口通过幂等核对 |

这些结果分别由[控制面证据](evidence/UPG-06.json)和
[节点证据](evidence/UPG-07.json)绑定原始记录。Harbor与入口新增资格另按所属任务
验收，不从这份Kubernetes结果推定通过。原卷使用local-path，不能据此声称本次
重新测量了Ceph、Longhorn、所有数据库或ARM64性能。

入口控制器另在同一目标平台上，经配置和`kubecli setup 07`安装
ingress-nginx `1.15.1`，以独立业务后端验证了以下接口。此结果是本项目集成验证，
不表示上游提供了Kubernetes 1.36逐版认证，也不改变既有业务的入口配置。

| 入口检查 | 实际结果与判断 |
| --- | --- |
| HTTP与HTTPS | 两种协议均返回预先确定的后端内容；HTTPS使用匹配域名的证书并验证CA |
| 错误CA | 客户端以证书验证错误退出，curl返回60；没有关闭TLS验证 |
| 冲突路由 | 重复host/path被真实admission webhook拒绝，返回`already defined` |
| 验证后回收 | 自有业务与入口资源已删除，节点标签按原UID恢复，临时配置逐字节恢复 |

原始结果由[入口集成证据](evidence/ingress-target-qualification.json)绑定。
入口业务可用、拒绝路径和清理各自成立，仍不替代本跳全部前置及最终验收。

## Harbor

### 选择路径与准备恢复空间

Harbor独立维护，不调用Kubernetes升级或重放全部中间件。项目使用单主机Compose，
目标必须使用官方对应CPU的完整包：

| 源 | 路径 | 限制 |
| --- | --- | --- |
| AMD64 2.13.0 | 原点完整备份 → 官方AMD64 2.14.6 → 官方AMD64 2.16.0 | 不允许2.13直接进入2.16 |
| 第三方ARM64 2.13.0 | 原点完整备份 → IabSDocker ARM64 2.14.4 → 官方ARM64 2.16.0 | 同CPU原地迁移；第三方仅作过渡，原生补充验收状态见本跳矩阵；不能使用AMD64 2.14.6包 |
| 干净AMD64或ARM64 | 官方对应架构2.16.0新建 | 目录必须无旧数据库、密钥、Registry及停止的安装 |

ARM过渡包来自 [IabSDocker v2.14.4发行](https://github.com/IabSDocker/harbor/releases/tag/v2.14.4)，
固定文件 `harbor-offline-installer-v2.14.4_arm64.tgz`，SHA256：

```text
524ab0439cdb421f3226c689c5d5799f9e4dda6dd3a26dbdf0cc2311690e0626
```

该包是第三方构建，不具有官方发行签名或上游逐架构认证。公开GitHub仓只提供分发
资料，其指向的构建源码需要认证；当前核查不能证明全部源码与官方无差异。已核对
包内12个镜像均为ARM64、固定版本及关键迁移脚本；这些结论不等于安全认证。
过渡结束后运行官方2.16.0，不长期维护第三方2.14.4。若组织要求全部构建源码可审计，
应停在选型阶段，不能将本路线写成满足该要求。

项目分发镜像为 `hub.talkedu.cn/kubeauto/harbor-upgrade-bridge-arm64:v2.14.4-iabs.1`，
Docker Hub回退为 `brinnatt/harbor-upgrade-bridge-arm64:v2.14.4-iabs.1`。双架构包装内
都是同一个ARM安装包，不能在AMD64上运行它。在Harbor主机准备独立包目录
`/srv/packages`，先确认目录可写、目标文件不存在且容器名`harbor-arm-bridge-package`
未占用；每条命令非零立即停止，不继续复制或删除。然后提取：

```bash
docker pull hub.talkedu.cn/kubeauto/harbor-upgrade-bridge-arm64:v2.14.4-iabs.1
docker create --name harbor-arm-bridge-package \
  hub.talkedu.cn/kubeauto/harbor-upgrade-bridge-arm64:v2.14.4-iabs.1
docker cp harbor-arm-bridge-package:/harbor/harbor-offline-installer-v2.14.4.tgz \
  /srv/packages/harbor-offline-installer-v2.14.4.tgz
docker rm harbor-arm-bridge-package
printf '%s  %s\n' \
  524ab0439cdb421f3226c689c5d5799f9e4dda6dd3a26dbdf0cc2311690e0626 \
  /srv/packages/harbor-offline-installer-v2.14.4.tgz | sha256sum -c -
```

提取只创建未运行的包装容器，最后仅删除该容器，不覆盖既有包或容器。SHA核对失败保留首因并
重新核查来源，不继续迁移。AMD64保持官方2.14.6路线，不下载此ARM过渡包。

官方最低资源2CPU/4GB/40GB，推荐4CPU/8GB/160GB。按实际数据再预留完整数据副本、
原镜像archive、目标解包与失败隔离空间，不能把“40GB磁盘”理解成迁移总空间足够。
主机需Docker Engine >20.10、Compose >2.3；项目固定Docker28.5.2及已交付Compose。
不使用Helm升级流程。外置数据库/Redis、对象存储、internal TLS或未知定制源会被
当前本地恢复入口拒绝，不能去掉拒绝检查硬套该路径。

在Harbor主机保存官方离线包，按[制品记录](artifact-pins.json)核对SHA256。
2.16还提供Cosign keyless签名bundle，可用固定版本cosign检查官方发行身份：

```bash
cosign verify-blob \
  --bundle harbor-offline-installer-v2.16.0-amd64.tgz.sigstore.json \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/goharbor/harbor/.github/workflows/publish_release.yml@refs/tags/v2\.16\.0$' \
  harbor-offline-installer-v2.16.0-amd64.tgz
```

ARM64使用`arm64`文件名。预期`Verified OK`，签名验证失败或结果未知时不得放宽身份
匹配或关闭验证继续；核对官方bundle、包和可信根来源。此处定义官方校验方式，不
把未取得的签名执行结果写成已通过。

本组合的AMD64、ARM64完整包已使用Sigstore官方Python验证器`4.5.1`核对上述发行
身份与OIDC签发者，均通过；把发行tag改为错误身份时被拒绝。该结果证明包的签名
来源，不代表ARM64迁移已经运行。固定包SHA256与原始验证记录见
[签名证据](evidence/harbor-official-signatures.json)。上面的Cosign命令是官方提供的
同类验证入口，本记录没有将Python验证结果表述为Cosign执行结果。

### 原点备份、同CPU迁移与恢复

在控制机使用原集群配置，`[harbor]`只允许一个明确管理主机。原`harbor.yml`是密码、
端口、存储与TLS的权威，备份入口读取它；不能用新模板生成随机密码覆盖原配置。
确认其中管理员凭据仍可认证：版本接口的匿名HTTP 200会隐藏版本字段，不能用作
版本验收。若管理员密码已轮换，应按现有凭据管理流程同步受保护的原配置；不要重置
线上密码或把密码写入命令行、日志和工单。认证或原CA校验失败时，入口在停止服务前拒绝。
业务先停止新推送、复制、GC、扫描等写入任务，通知维护窗口。以下示例保留原
`HARBOR_PATH`，恢复目录位于该数据树之外，变量路径均指Harbor主机而非控制机：

```bash
export HARBOR_BACKUP=/srv/harbor-recovery/pre-216
export HARBOR_CA=/etc/kubernetes/ssl/ca.pem
export HARBOR_BRIDGE=/srv/packages/harbor-offline-installer-v2.14.6.tgz
export HARBOR_TARGET=/srv/packages/harbor-offline-installer-v2.16.0.tgz
# ARM64主机使用下一行替代上面的AMD64桥接包路径：
# export HARBOR_BRIDGE=/srv/packages/harbor-offline-installer-v2.14.4.tgz
kubecli upgrade "$CLUSTER" --harbor backup \
  --harbor-backup-dir "$HARBOR_BACKUP" --harbor-ca-file "$HARBOR_CA"
```

备份会停止Harbor，绑定原版本、主机、配置、密钥、镜像和完整数据，输出
`HARBOR_STOPPED_BACKUP_PASS`。首次备份目录必须不存在，禁止覆盖旧恢复点。只有
`receipt.json`处于`stopped`、尚无数据/镜像归档或逻辑导出输出，且原主机、配置、
容器身份及镜像仍一致时，才允许在诊断首因后用同一`backup`命令继续归档；该续跑
不启动业务，也不再次保存队列。已有部分归档或身份变化时停止，保留检查点，
不可删除文件强行重试。Harbor 2.16的Valkey 9.0.3使用`VALKEY080`格式，
与旧Redis文件头不同，入口按实际引擎校验后完整保留队列快照。将恢复目录
以所有者私有权限复制到独立存储，并核对每文件SHA256后再推进；包含数据库和密钥，
不得提交Git或公开到工单。

```bash
kubecli upgrade "$CLUSTER" --harbor upgrade \
  --harbor-backup-dir "$HARBOR_BACKUP" \
  --harbor-bridge-package "$HARBOR_BRIDGE" \
  --harbor-target-package "$HARBOR_TARGET"
```

产品以受管Harbor主机的真实CPU选择桥接：AMD64为2.14.6，ARM64为2.14.4，控制机CPU不参与该选择。
包路径必须位于受管主机；错误CPU或包SHA在迁移前拒绝。产品按检查点完成桥接、schema/数据迁移、目标启动与健康确认。迁移失败先停止，
保留恢复目录中的`receipt.json`、`commands.log`及`first-failure.json`。不要另做一次
备份取代原点，也不要在已迁移数据库上启动旧服务。成功后仍在维护窗口内用原项目、
原用户/机器人、原制品digest验证内容、允许拉取及拒绝越权推送，核对队列状态和
所有组件健康，再决定是否恢复写入。仅`/api/v2.0/health`通过不够。

需回原点时，明确目标新增写入如何处理，停止目标写入，使用同一原始恢复点：

```bash
kubecli upgrade "$CLUSTER" --harbor restore --harbor-backup-dir "$HARBOR_BACKUP"
```

此操作是恢复整套旧状态，不是数据库降级。恢复后再次核验原版本、项目/成员/机器人
身份、原digest的实际层内容、权限及健康，然后才恢复客户流量。目标新增写入不会
自动回流原库，不能无说明地丢弃。失败目标数据的隔离目录保留到原点验证通过，
按恢复保留期定向回收，禁止笼统`rm -rf /var/data`。

本跳已在原生AMD64、Rocky Linux 9.8单机Compose环境验证下列边界，使用Docker
28.5.2、HTTPS原CA及内部数据库/本地文件存储，未启用Trivy、外置服务或internal TLS。
时间是该实验室的小型业务结果，不能据此估算客户大仓库的维护窗口。

| 操作 | 实际验证结果与判断 |
| --- | --- |
| 原版迁移 | 完整停写备份后经官方2.14.6到2.16.0，约10分53秒；原项目/制品ID、digest及层文件保持，原用户和只拉机器人允许拉取、拒绝越权推送 |
| 再次升级 | 同一恢复点再次调用`upgrade`通过幂等核对，原身份与内容再次保持；未重新创建用户或机器人 |
| 原点恢复 | 从同一备份完整恢复2.13.0，约2分24秒；原身份、权限、digest和实际文件内容均保持，目标隔离目录保留 |

对应[AMD64迁移证据](evidence/harbor-amd64-migration-qualification.json)与
[原点恢复证据](evidence/harbor-amd64-restore-qualification.json)仅覆盖这些操作。
本次原生ARM补充使用openEuler 22.03 LTS-SP4、4 KiB页、约14 GiB内存，
Docker26.1.3及Compose2.27.0，保持同CPU、HTTPS原CA、内部数据库与本地文件存储，
未启用Trivy、外置服务或internal TLS。实际结果如下：

| ARM操作 | 已验证的业务结果 |
| --- | --- |
| 2.13经IabSDocker 2.14.4到官方2.16 | 九个运行容器为审核包内的ARM镜像；原项目ID 2、制品ID 1、manifest digest与实际层文件保持，原用户及只拉机器人仍可拉取、拒绝越权推送 |
| 再次upgrade与原点restore | 重复升级不重建身份；恢复原2.13后直接读取同一原账本，项目/成员/机器人、原secret、digest和层内容保持；每次拉取前移除本地夹具缓存 |

原账本的manifest digest为
`sha256:c66d7e9e6c569189eeba110a0bac63b3ebc260da8ca9af8d8e874190af75b90d`，
层文件SHA256为`517aef10289ebf4fbc9de74573596fc84f03d8d668d0d7075b8a5086dcfcd1a0`。
这些是小型实验室账本的身份与内容判据，不是性能测试或客户数据保证。
原生补充的完整状态见[矩阵UPG-12](test-matrix.yaml)；只在该项及最终项通过后交付此路线。

上述原验收只包含x86运行与ARM来源、签名和静态检查。本次追加的同CPU ARM迁移、恢复与新建由独立原生补充范围验收；跨CPU逻辑迁移仍没有项目实测。完整父项以本跳矩阵为准。

### 逻辑迁移与目标撤回

逻辑迁移用于更换主机或CPU，沿用上面的停写备份。先明确源、目标两份集群配置：
先保存源集群名，再创建独立目标配置，不能将源库存改成目标地址：

```bash
export HARBOR_SOURCE_CLUSTER="$CLUSTER"
export HARBOR_TARGET_CLUSTER=harbor216-restore
kubecli new "$HARBOR_TARGET_CLUSTER"
```

两端保持相同的Harbor域名、HTTPS端口、数据路径及原TLS身份。按
[干净新建](#干净新建)中的Docker库存要求编辑**目标**配置和库存，但不执行该节的
整段安装命令，也不覆盖源集群变量。目标仅执行`setup 01`和`setup 03`，
**此时不执行`setup 11`创建新数据库**。目标不得有旧数据、停止容器或独立生成的
同名证书。若选择本节的逻辑迁移接口，原第三方ARM64 2.13必须先导入AMD64桥接主机；同CPU原地升级应使用前节ARM 2.14.4路线，不需要逻辑中转。源为2.16时才可直接导入
官方2.16的对应CPU目标。本次原生实测CPU为x86，跨CPU没有项目实测。

源完整备份成功后，在控制机导出：

```bash
kubecli setup "$HARBOR_TARGET_CLUSTER" 01
kubecli setup "$HARBOR_TARGET_CLUSTER" 03
kubecli upgrade "$HARBOR_SOURCE_CLUSTER" --harbor export --harbor-backup-dir "$HARBOR_BACKUP"
```

预期`HARBOR_PORTABLE_EXPORT_PASS`。入口只启动日志服务和数据库以读取逻辑库，
结束后全部停回；Core、Registry与Jobservice等业务写服务不能启动。原完整备份仍
是源主机的恢复点，不因导出而删除。若导出启动失败且尚无输出，修复首因后可重用
同一命令；若已有`registry.dump`或`portable.json`，入口拒绝覆盖，应保留资料并
检查结果或恢复原点，不删除输出强行重试。

在源Harbor主机以root传输私有导出文件。先将`TARGET_HOST`换为经过授权的目标地址，
目标目录`/srv/harbor-transfer/pre-216`须由root新建为0700、位于数据树和恢复目录之外；
SSH需已核对主机密钥。以下清单包含完整数据、逻辑库和TLS材料，不包含原CPU镜像包：

```bash
(
set -eu
umask 077
export TARGET_HOST=目标Harbor地址
python3 - "$HARBOR_BACKUP" <<'PY' > "$HARBOR_BACKUP/transfer-files.txt"
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
manifest = json.loads((root / 'portable.json').read_text())
names = ['portable.json', *manifest['files']]
if any(Path(name).name != name or name in ('.', '..') for name in names):
    raise SystemExit('导出文件清单越界，停止传输')
print('\n'.join(names))
PY
rsync -a --chmod=D700,F600 --files-from="$HARBOR_BACKUP/transfer-files.txt" \
  "$HARBOR_BACKUP/" "root@$TARGET_HOST:/srv/harbor-transfer/pre-216/"
)
```

清单命令失败时停止，不运行rsync。源恢复点和传输目录均包含秘密，禁止公开。
传输完成后在控制机指定目标配置、目标主机上的新checkpoint和已核验官方包：

```bash
export HARBOR_TARGET_BACKUP=/srv/harbor-recovery/import-pre-216
kubecli upgrade "$HARBOR_TARGET_CLUSTER" --harbor import \
  --harbor-backup-dir "$HARBOR_TARGET_BACKUP" \
  --harbor-transfer-dir /srv/harbor-transfer/pre-216 \
  --harbor-bridge-package "$HARBOR_BRIDGE" \
  --harbor-target-package "$HARBOR_TARGET"
```

包路径位于目标主机，2.13导入使用AMD64的2.14.6和2.16包；2.16导入只消费目标CPU的
2.16包。入口先核对文件SHA256、私有权限、原配置、队列和TLS身份，排除物理数据库，
在启动Core迁移schema前完成逻辑恢复。成功标志为`HARBOR_PORTABLE_IMPORT_PASS`。
保持源停止，以隔离的客户端解析将原域名指向目标，使用原CA、原用户和原机器人验证
原项目/制品ID、digest、实际层内容及只拉身份禁止推送；不得重建用户或制品掩盖丢失。
这些检查通过后，才安排DNS/客户端配置切换并唯一开放目标写入。

导入失败或决定回原点时，先在控制机撤回本次目标，再恢复源：

```bash
kubecli upgrade "$HARBOR_TARGET_CLUSTER" --harbor abort-import \
  --harbor-backup-dir "$HARBOR_TARGET_BACKUP"
kubecli upgrade "$HARBOR_SOURCE_CLUSTER" --harbor restore --harbor-backup-dir "$HARBOR_BACKUP"
```

目标撤回会停止并隔离本次数据，输出`HARBOR_PORTABLE_TARGET_STOPPED`，再次调用
应幂等；它不重启源。确认目标已停后才恢复源并复验原账本，防止双写。若目标已经
接收新写入，先解决这些数据的保留与回迁，不能直接用旧恢复点丢弃。失败checkpoint、
隔离目录和原完整备份继续按恢复保留期保存，不能当作普通临时文件清理。

本跳在上述原生AMD64、Rocky 9.8单机Compose条件下，另以独立源/目标验证逻辑路径：

| 路径 | 实际结果与判断 |
| --- | --- |
| 2.13逻辑导入 | 源保持停止；在独立目标逻辑恢复数据库，经官方2.14.6到2.16，原项目/制品ID、digest、层文件及原用户/只拉机器人权限保持 |
| 2.16同版逻辑导入 | 停写并完整保存Valkey 9.0.3队列后，在干净目标逻辑恢复；同一原业务账本保持，未重建用户、机器人或制品 |
| 撤回与原点恢复 | 目标撤回及再次撤回幂等通过；确认目标停止后恢复源2.13，原身份、允许/拒绝权限与层内容再次保持 |

对应[逻辑迁移证据](evidence/harbor-logical-qualification.json)只支持以上x86路径；
不将其改称跨CPU运行认证，也不据此推导外置数据库、额外SQL身份或目标新增写入回迁已验证。

### 干净新建

本节从独立的新配置开始；不用于覆盖已停止的旧Harbor。控制机已安装本跳产品运行
目录与执行文件，有管理员权限、可用Docker及已配置的制品源。控制机和Harbor主机
使用同一CPU架构时，先下载本跳固定依赖；命令在控制机执行：

```bash
export KUBEAUTO_BASE_PATH=/usr/local/kubeauto
export CLUSTER=harbor216
kubecli version
kubecli download -d 28.5.2
kubecli download -e 1.17.1
kubecli download -k v1.36.6
kubecli download -R v2.16.0
kubecli new "$CLUSTER"
```

下载Kubernetes二进制供既有基础准备生成客户端配置使用，本节不安装Kubernetes
控制面或工作节点。不使用`download -D`拉取无关组件。不同架构的控制端与Harbor
主机不能直接混用本机自动选择的包和二进制；必须另行准备目标架构的固定完整包、
Docker及扩展制品，并按各自摘要核对后再进入部署，不能只改文件名。

使用新配置中的单机库存模板，将其中示例地址替换为已完成资源和SSH检查的Harbor
主机，保留模板的变量及分组。在`[harbor]`中明确同一主机及`NEW_INSTALL=true`，将
`[all:vars]`的`CONTAINER_RUNTIME`设为`"docker"`。本节只调用基础准备、运行时与
Harbor步骤；不要执行`setup 90`。在对应`config.yml`明确域名、路径、端口、证书
模式与扫描器选择：

```yaml
HARBOR_VER: v2.16.0
HARBOR_DOMAIN: harbor.example.com
HARBOR_PATH: /var/data
HARBOR_TLS_PORT: 443
HARBOR_SELF_SIGNED_CERT: true
HARBOR_WITH_TRIVY: false
```

官方Compose安装前置为Docker大于20.10、Compose大于2.3；本跳已验证的Docker为
28.5.2，不据最低要求保证其它任意运行时组合。主机至少2CPU、4GB内存及40GB磁盘，
推荐4CPU、8GB及160GB；客户容量还须计入实际镜像增长、数据库、日志及完整备份。
完整包、解包目录和恢复目录的空间分别核对，不能只看数据卷还有空闲。

将域名解析到Harbor主机，客户端信任选定CA；自签模式下CA位于本集群目录的
`ssl/ca.pem`，只分发公有CA，不分发`ca-key.pem`。确认数据目录无旧状态后执行：

```bash
kubecli setup "$CLUSTER" 01
kubecli setup "$CLUSTER" 03
kubecli setup "$CLUSTER" 11
```

任一步非零先停止，按[异常诊断与恢复](#异常诊断与恢复)定位；不要以重建配置覆盖
旧目录来重试。2.16安装步骤会核对主机架构与官方包SHA256，等待TLS端口，并要求
`/api/v2.0/systeminfo`精确为2.16.0、`/api/v2.0/health`为`healthy`。之后仍须完成
[私有制品使用](#私有制品使用)的允许/拒绝和原内容读回。已运行Harbor会进入
检查分支；停止或残缺的原数据会拒绝重新初始化，必须走恢复/迁移流程。新建会生成
密码，使用权限受控的Harbor主机`harbor.yml`取出管理员初始凭据，登录后按客户密码
管理规范维护；不要打印整份配置到公开终端日志。

本跳在另一台原生AMD64、Rocky Linux 9.8主机以独立配置验证了官方2.16.0新建：
私有项目、原创建用户及只拉机器人完成实际内容拉取与越权推送拒绝；第二次`setup 11`
保持同一业务账本及身份，原迁移源未受影响。见
[新建与幂等证据](evidence/harbor-amd64-clean-install-qualification.json)。此历史结果只覆盖AMD64；原生ARM64新建由本次补充范围独立验收，双架构交付状态以矩阵为准。

本次另在原生ARM的干净Harbor数据边界通过`setup 11`安装官方2.16.0，创建独立
私有项目、用户和只拉机器人；第二次`setup 11`为`changed=0`，原新建账本保持。
其manifest digest为`sha256:a186b2a6f62ba493dbeec8dd5bb87d00f4ed43feff30d80ead9b1d633a5f3e6f`，
层文件摘要与上表相同，原用户和机器人权限均通过实际允许/拒绝请求确认。
该补充使用上一节列出的ARM运行时，不重演整机prepare、CRI或Kubernetes安装；
完整新建与清理结论见矩阵UPG-13，不把它扩大为其它组件ARM验收。

上述资格结束后，已按归属清理源、目标Harbor及运行时，复验六节点并释放共享租约；
完整恢复档案仍在受控目录保存，未作为临时文件删除。见
[限定清理证据](evidence/harbor-qualification-cleanup.json)。原资格链存在失败，
修复后的独立子资格及清理通过不等于最终干净完整回归已经完成。

## 私有制品使用

浏览器访问配置的HTTPS域名，在项目中创建私有项目，例如`school`。为业务用户分配
项目角色，为CI创建只覆盖需要操作的机器人；避免把管理员密码写进流水线。

以下在安装nerdctl 2.4.2的root客户端执行，使用前节显式选择的443端口；若实际端口
为8443，`HARBOR_AUTHORITY`必须包含`:8443`，后续所有命令保持同一名称。先通过受信
渠道将该Harbor的公有CA保存为`/root/harbor-ca.pem`，与控制机`ssl/ca.pem`的SHA256
独立核对。自签模式不是关闭证书校验的理由。

为CLI使用独立信任目录，避免继承旧`hosts.toml`中的跳过验证或静态Authorization。
目录已存在时先停步核对归属和配置，不覆盖它；此操作不修改节点CRI配置。

```bash
export HARBOR_AUTHORITY=harbor.example.com
export HARBOR_CLIENT_TRUST=/etc/nerdctl/harbor-trust
sha256sum /root/harbor-ca.pem
test ! -e "$HARBOR_CLIENT_TRUST"
install -d -m 0755 "$HARBOR_CLIENT_TRUST/$HARBOR_AUTHORITY"
install -m 0644 /root/harbor-ca.pem "$HARBOR_CLIENT_TRUST/$HARBOR_AUTHORITY/ca.crt"
cat > "$HARBOR_CLIENT_TRUST/$HARBOR_AUTHORITY/hosts.toml" <<EOF
server = "https://$HARBOR_AUTHORITY"
[host."https://$HARBOR_AUTHORITY"]
  capabilities = ["pull", "resolve", "push"]
  ca = "$HARBOR_CLIENT_TRUST/$HARBOR_AUTHORITY/ca.crt"
  skip_verify = false
EOF
```

域名必须解析到真实Harbor主机，证书SAN包含该域名。TLS失败时按
[诊断表](#异常诊断与恢复)定位CA、SAN、端口和时间，不加`--insecure-registry`。
按固定版本的[证书与认证说明](https://github.com/containerd/nerdctl/blob/v2.4.2/docs/registry.md)，
使用交互登录，不将密码写在命令行：

```bash
NERDCTL=(nerdctl --hosts-dir "$HARBOR_CLIENT_TRUST" --insecure-registry=false -n k8s.io)
"${NERDCTL[@]}" login "$HARBOR_AUTHORITY"
"${NERDCTL[@]}" tag busybox:1.30 "$HARBOR_AUTHORITY/school/busybox:1.30"
"${NERDCTL[@]}" push "$HARBOR_AUTHORITY/school/busybox:1.30"
"${NERDCTL[@]}" pull "$HARBOR_AUTHORITY/school/busybox:1.30"
```

示例前提是本机已有明确来源的`busybox:1.30`，项目存在且当前身份有push权限。
推送后核对UI/API中的repository、tag、digest与客户端结果；只拉取身份应能pull而
不能push。Kubernetes使用其自己的imagePullSecret或节点凭据路径，不因控制机登录
就自动给所有Pod授权。机器人secret只在创建/刷新时交付，需安全保存，不靠重新创建
来证明迁移成功。nerdctl将认证材料写入当前用户的`$DOCKER_CONFIG/config.json`
（默认`~/.docker/config.json`）；按凭据文件管理，不公开或提交。独立信任目录只服务
显式使用它的CLI，Pod拉取还需各节点的受信CA及最小权限认证分别配置和验证。

Harbor可管理OCI Helm Chart等制品、扫描、签名、配额、不可变tag、保留策略、复制、
代理缓存、Webhook和P2P等能力。常规客户先使用私有项目、最小权限与固定tag；
策略按项目逐项启用并验证。上游可用不等于本项目已验收其全部选项，本次不自动启用
这些新增能力，也不迁移到Helm部署或外部服务。

私有项目必须先创建再推送，用户存在并不等于具有项目权限；默认的公开`library`
项目不能用于验证匿名拒绝。项目存储配额耗尽会拒绝新推送，配额计量、tag引用和
实际blob回收分属不同机制。遇到配额拒绝先确认项目用量及预期增长，再调整容量或
规则，不通过改为公开项目、扩权或删除未知数据排除错误。

## 日常运维

日常同时检查HTTPS及业务拉取、系统组件健康、宿主机空间、数据库/队列、异步任务、
复制失败与扫描库更新。项目配额控制项目增长，不能代替宿主机磁盘阈值。审计保留期
关系追溯能力；GC、保留策略和删除任务关系恢复窗口，变更前先确认保留范围与备份。

删除tag不保证立即释放blob空间；GC按未引用blob回收。不可变tag规则与保留策略
分别审阅，先在自有测试项目确认匹配范围，禁止以全局删除消除容量告警。Webhook
按可能重试、重复及延迟投递设计接收方，消费端应幂等；复制失败按任务记录重试，
不能将复制成功当成数据库、身份及配置的完整备份。

变更保留策略或运行GC前，先做dry run，核对候选repository、artifact、tag与附件，
确认不可变规则、业务保留期和完整恢复点。候选与预期不符时停止，不执行正式删除。
任务结束后分别核验保留制品可按原digest实际拉取、已删除对象的范围及真实磁盘
变化；空间未立即下降时先检查共享引用和GC状态，不能直接删除Registry的blob目录。
迁移的停写和快照窗口内不执行GC、上传清理或策略变更。

维护证书时核对SAN、有效期、完整链及所有客户端CA，保留原证书和回退路径；更新
配置后按官方同版本重配置流程处理，不能把`setup`当成旧数据覆盖器。更换认证模式、
外置存储、internal TLS或部署拓扑属于独立变更，不夹入补丁升级。

## 异常诊断与恢复

| 客户现象 | 最小检查与分叉 | 处置及复验 |
| --- | --- | --- |
| 节点升级停在drain | 看首失败中PDB、未控制Pod、本地数据或超时；核对journal当前node | 业务负责人恢复可驱逐条件，保留cordon与恢复点，同入口续跑；读回原PVC账本 |
| 所有节点Ready但业务失败 | 核对Service endpoints、DNS、卷绑定、原数据及客户端错误 | 按网络/存储/业务因果层处理，不能将Ready当成业务成功 |
| Harbor登录401/403 | 区分正常Bearer challenge与持续拒绝；检查项目/用户/机器人scope及外部身份服务 | 修复实际身份或scope，重新验证最小允许/拒绝；不换随机密码或关闭TLS |
| x509错误 | 核对访问域名/SAN、客户端CA、链与主机时间 | 修复对应信任输入，用原域名验证；不启用insecure绕过 |
| Portal有镜像但pull失败 | 用原digest核对manifest、blob与Registry/Token服务；排除本地缓存 | 保留故障证据，按同一完整恢复点处理；恢复后真正下载原层并读文件 |
| 迁移或PG启动失败 | 读取私有first-failure与commands、检查点、包CPU与空闲空间 | 停止依赖步骤，修已证明原因或执行完整restore；不把旧容器接新PG目录 |
| Docker成功但Harbor不健康 | 核对每组件状态、Core/DB/队列与入口日志 | 分层处理，原业务和权限读回后才恢复流量 |
| 磁盘不足/任务积压 | 看实际文件系统、blob增长、队列及扫描/复制/GC任务 | 先停止增长或扩容，按策略与备份回收；不删数据库或密钥 |

所有诊断资料按其中是否包含令牌、数据库、配置和密钥分级保存。清理只触及明确
归属本次运行的资源；已有正式证据、旧GA分支与客户恢复点继续保留。

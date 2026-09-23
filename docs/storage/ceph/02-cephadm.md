# Cephadm：从裸机到可持续运维的生产编排手册

> 适用版本：Ceph Tentacle。Cephadm 生产生命周期必须形成可执行、可验收、可停止、可恢复的闭环。

## 1. Cephadm 的工作模型

Cephadm 不是“用容器启动 Ceph”的脚本。它由三个相互约束的平面组成：

| 平面 | 权威状态 | 职责 | 边界 |
|---|---|---|---|
| 编排控制面 | active MGR 的 cephadm module | inventory、Service Spec、placement、证书、SSH 和 reconcile | 不替代 MON 一致性与业务协议 |
| 主机执行面 | SSH、主机 cephadm、systemd、Podman/Docker | 拉镜像、生成 unit/config、控制容器、扫描设备 | 不决定期望副本数 |
| Ceph 数据面 | MON/MGR/OSD/MDS/RGW 等 daemon | quorum、对象、元数据、协议和业务 I/O | 不保存完整部署意图 |

Cephadm 从 bootstrap 的单 MON/单 MGR 种子开始，通过 orchestrator interface 扩展并管理完整生命周期，管理员可使用 Ceph CLI 或 Dashboard GUI 操作。它不依赖 Ansible、Rook 或 Salt；这些工具可以自动化 cephadm 未覆盖的外围任务，但不得与 cephadm 同时管理同一 daemon、systemd unit 或持久配置，否则 reconcile 会反复覆盖对方的结果。

```mermaid
flowchart LR
  OP[管理员提交 Service Spec] --> MGR[active MGR cephadm module]
  MGR --> INV[主机与设备 inventory]
  MGR --> PLAN[placement 与 reconcile]
  PLAN --> SSH[SSH 到目标主机]
  SSH --> ADM[主机 cephadm]
  ADM --> SD[systemd unit]
  SD --> CTR[Ceph 容器]
  CTR --> DATA[Ceph 数据面]
  DATA --> OBS[状态与事件]
  OBS --> MGR
```

生产验收必须分别回答：

1. `ceph orch ls --export` 是否等于期望声明；
2. `ceph orch ls --refresh` 是否完成调度收敛；
3. `ceph orch ps --refresh`、events、systemd/container 是否健康；
4. RADOS、CephFS、RBD、S3、NFS 或 iSCSI 的真实读写是否成功。

容器 `running` 只覆盖第三项的一部分，绝不等于生产就绪。

## 2. 统一的生产变更闭环

Bootstrap、扩容、换盘、证书、升级和迁移使用同一控制循环：

```mermaid
flowchart TD
  B[保存健康、spec、版本和恢复点] --> P[生成目标 spec]
  P --> D[dry-run 或 check]
  D --> G{容量与故障域正确?}
  G -->|否| R[修正规划]
  R --> P
  G -->|是| A[apply]
  A --> W[观察 events 与 reconcile]
  W --> H{Ceph health 正常?}
  H -->|否| S[停止后续批次并保留 first failure]
  H -->|是| I[业务 I/O 验收]
  I --> E[导出新 spec 与证据]
```

变更前至少保存：

```bash
ceph -s
ceph health detail
ceph versions
ceph orch host ls --detail --format yaml
ceph orch ls --export > cluster-before.yaml
ceph orch ps --refresh --format yaml > daemons-before.yaml
ceph osd tree
ceph df detail
```

以下任一情况必须停批：quorum 逼近安全线、PG 不可用或降级继续扩大、镜像无法验证、设备身份不确定、证书 SAN 不匹配、业务探针失败。不要继续执行后续节点来等待“自行恢复”。

## 3. 裸机准入

### 3.1 官方依赖与生产验收

官方基础要求是 Python 3、systemd、Podman 或 Docker、时间同步和 LVM2。生产还要证明名称、网络、制品、权限和磁盘身份一致。

| 项目 | 准入证据 | 不满足的后果 |
|---|---|---|
| Python | `python3 --version`；目标 cephadm 能启动 | cephadm 无法执行 |
| systemd | unit 可创建、启动和持久化 | daemon 失去主机生命周期管理 |
| runtime | Podman/Docker 能拉取固定镜像 | bootstrap/reconcile 卡在 pull |
| 时间 | 所有节点已同步且偏差受控 | MON lease、CephX、TLS 异常 |
| LVM2 | `lvm version` 与设备扫描正常 | ceph-volume 不能准备 OSD |
| hostname | 远端 `hostname` 与 inventory 名完全相同 | SSH、daemon、CRUSH、TLS 身份分裂 |
| 网络 | public/cluster CIDR、MTU、路由、端口双向通过 | quorum 或 OSD heartbeat 不稳定 |
| 制品 | image tag/digest、架构、registry CA 已验证 | 节点间版本或镜像漂移 |
| 磁盘 | OS/OSD 盘按 WWN/serial 对账 | 误擦系统盘或旧数据盘 |
| 权限 | root 或 passwordless sudo 专用用户 | 远端动作执行一半失败 |

Docker 场景可评估启用 Docker Live Restore，使 Docker Engine 重启期间已有容器继续运行；它不保证 daemon 能被管理、不替代 systemd、MON quorum 或业务 HA，也不能覆盖主机重启、容器自身退出和不兼容 daemon 配置变更。启用前后必须演练 runtime reload/restart、容器存活、systemd 状态和 cephadm 后续 reconcile。Podman 版本必须与当前 Ceph 兼容矩阵匹配，不能仅以“能启动一个容器”判定兼容。

```mermaid
flowchart TD
  H[候选主机] --> N{hostname 精确一致?}
  N -->|否| X[拒绝加入]
  N -->|是| T{时间同步?}
  T -->|否| X
  T -->|是| C{runtime systemd Python LVM2 合格?}
  C -->|否| X
  C -->|是| W{网络与镜像可达?}
  W -->|否| X
  W -->|是| A[允许 check-host 与 host add]
```

### 3.2 cephadm 制品

新版 cephadm 是由源码编译的 executable，不能把仓库中的 Python 源脚本当作正式发布物直接复制。发行版包与官方 release 下载是两条取得路径，选定一种并记录来源、版本、SHA256；不要混装导致 CLI 与容器 release 分裂。

```bash
CEPH_RELEASE=tentacle
curl --fail --location --remote-name \
  "https://download.ceph.com/rpm-${CEPH_RELEASE}/el9/noarch/cephadm"
chmod +x cephadm
./cephadm version
./cephadm list-images
```

`cephadm list-images` 是 air-gap 镜像清单的起点，包含 Ceph 和辅助服务；它不是只同步主 Ceph image 的理由。生产下载应由企业制品库承接，固定完整 reference 并记录 manifest digest。

发行版路径与 curl 路径互斥，同一主机不要混用。官方示例分别为 Ubuntu `apt install -y cephadm`、CentOS Stream/Fedora `dnf install cephadm`、SUSE `zypper install -y cephadm`；必须先确认发行版仓库确实提供目标 Tentacle 版本。curl 取得的 standalone executable 足以 bootstrap，但长期运维应安装到系统 PATH：

```bash
./cephadm add-repo --release tentacle
./cephadm install
which cephadm
cephadm version
```

官方最低运行条件是 Python 3.6；出现 `bad interpreter` 时先验证实际解释器，可用 `python3.8 ./cephadm <args>` 诊断。生产支持范围仍以目标 OS、Ceph 构建和安全维护中的 Python 组合为准，不能因为达到 3.6 就忽略已 EOL 的解释器。

## 4. Bootstrap 控制面

### 4.1 实际产物

`cephadm bootstrap --mon-ip <IP>` 会：

- 创建 FSID、首个 MON 与 MGR；
- 建立初始 monmap、CephX keyring 和 quorum；
- 生成或导入 cephadm SSH 身份；
- 把首主机纳入 inventory 并赋予 `_admin`；
- 输出 `ceph.conf` 和 `client.admin` keyring；
- 启用 cephadm orchestrator；
- 未跳过时部署基础 monitoring stack。

```mermaid
sequenceDiagram
  participant Admin as 管理员
  participant Boot as cephadm bootstrap
  participant Mon as 首个 MON
  participant Mgr as 首个 MGR
  participant Host as 首主机 systemd
  Admin->>Boot: mon-ip image SSH 初始配置
  Boot->>Host: 创建 FSID 目录与 unit
  Boot->>Mon: 创建 monmap keyring quorum
  Boot->>Mgr: 启用 cephadm module
  Mgr->>Host: 纳管主机与 SSH 身份
  Boot-->>Admin: 输出 config 与 client.admin
```

它只建立控制面种子。生产仍需完成主机、MON/MGR 故障域、OSD、业务服务、入口、安全、监控和恢复演练。

### 4.2 参数职责

| 目的 | 参数 | 生产含义 |
|---|---|---|
| 首 MON | `--mon-ip` | 多网卡主机必须选正确 public 地址 |
| 复制网络 | `--cluster-network <CIDR>` | OSD replication/recovery 网络 |
| Ceph 镜像 | 全局 `--image <ref>` | 固定 bootstrap 和初始 daemon image |
| registry | `--registry-json <file>` | 导入 secret，不进入仓库或普通日志 |
| 初始配置 | `--config <file>` | daemon 创建前导入审核过的设置 |
| 输出隔离 | `--output-dir <dir>` | 避免覆盖已有 `/etc/ceph` |
| SSH 用户 | `--ssh-user <user>` | 非 root 用户须 passwordless sudo |
| 自有 key | `--ssh-private-key` + `--ssh-public-key` | 对应普通公钥已分发 |
| CA key | `--ssh-private-key` + `--ssh-signed-cert` | 与 `--ssh-public-key` 互斥 |
| 初始声明 | `--apply-spec <file>` | SSH 信任就绪后一次应用 host/service spec |
| 日志 | `--log-to-file`、全局 `--log-dest` | 前者管 daemon，后者管 cephadm 自身 |
| 监控 | `--skip-monitoring-stack` | 明确改用外部或延后监控 |
| 单机 | `--single-host-defaults` | 改变 CRUSH、副本和 MGR 默认值 |

### 4.3 可审计执行与验收

```bash
sudo ./cephadm \
  --image registry.example.com/ceph/ceph:<tentacle-pin> \
  bootstrap \
  --mon-ip 10.20.0.11 \
  --cluster-network 10.30.0.0/24 \
  --config initial-ceph.conf \
  --registry-json registry.json \
  --output-dir /root/ceph-bootstrap-output \
  --log-to-file
```

```bash
cephadm shell -- ceph -s
cephadm shell -- ceph fsid
cephadm ls
cephadm shell -- ceph orch host ls --detail
cephadm shell -- ceph orch ps --refresh
```

验收 FSID、MON quorum、MGR active、主机名、输出文件权限和 image。Bootstrap 中断先识别已经创建的 FSID 和 unit，不能盲目重跑；决定废弃时按精确 FSID 清理。

## 5. 特殊部署

### 5.1 单主机

`--single-host-defaults` 设置：

```text
osd_crush_chooseleaf_type = 0
osd_pool_default_size = 2
mgr_standby_modules = false
```

它允许副本落到同一 host 的不同 OSD，不提供主机级容灾。扩成多主机时要显式重做 CRUSH failure domain、pool size 和 MGR 冗余；增加节点不会撤销这些值。

### 5.2 隔离环境

```mermaid
flowchart TD
  L[cephadm list-images] --> C[Ceph 主镜像]
  L --> M[监控镜像]
  L --> G[HAProxy Keepalived nginx OAuth2]
  L --> O[SNMP Jaeger Elasticsearch Loki Promtail]
  C & M & G & O --> R[内部 registry]
  R --> D[逐架构核验 digest]
  D --> B[registry-json + 固定 image bootstrap]
```

只同步 Ceph 主镜像会让 bootstrap 表面成功、监控或入口在 reconcile 时失败。内部 registry 应使用受信 CA；insecure registry 是需要审批和退出计划的例外，不是默认设计。

`--registry-json` 不只是让 bootstrap 主机完成一次 pull：cephadm 会登录该 registry，并把登录信息保存到集群配置数据库，后续加入的受管主机也可使用它。凭据因此属于集群级 secret，JSON 文件必须 `0600`、置于受控临时目录并在导入后销毁；配置数据库和 MON 备份也必须按含密材料保护。

Registry 密码、token 或 CA 轮换不能等到升级窗口才发现。官方运行期入口是：

```bash
ceph cephadm registry-login <registry> <username> <password>
```

该命令会让 orchestrator 在受管主机登录；主机一旦被判断为 online，cephadm 还可能立即补建此前缺失的 `crash`、`node-exporter` 或其他 daemon。因此轮换前先保证 service spec、host 状态和目标 image 都正确，轮换时观察 `orch ps --refresh` 与 events，不能把它当成无副作用的“只写密码”命令。密码作为参数可能进入 shell history、审计或进程观测面，应从受控运维入口注入、禁止命令回显并在操作后按企业 secret 流程清理；需要只登录单台主机时，可在该主机使用受权限保护的 JSON 执行 `cephadm registry-login --registry-json <file> --fsid <fsid>`，但它不能冒充全体受管主机已完成轮换。

轮换验收必须同时证明：所有目标主机 runtime 登录有效；Ceph 主 image 和辅助 image 均能按固定 digest 拉取；新加或重装主机能完成 reconcile；旧凭据已撤销；没有 `UPGRADE_FAILED_PULL` 或 daemon image 漂移。任一主机认证失败就停止升级、扩容和 redeploy，不通过改用浮动 tag 或临时公共镜像绕过。

### 5.3 SSH 三种模式

| 模式 | 主机信任 | Bootstrap 输入 | 轮换 |
|---|---|---|---|
| 自动生成 | `authorized_keys` 接受 cephadm key | 默认 | 逐主机分发/撤销 |
| 自有 key pair | 已分发普通公钥 | private + public | 接入现有 key 管理 |
| CA-signed | sshd 信任 CA 公钥 | private + signed cert | 重新签发，无需普通公钥 |

CA-signed 模式官方拒绝同时传 `--ssh-public-key`，因为不需要它。所有模式都要从 active/standby MGR 的实际上下文验证到每台主机，而非只从运维机测试。

### 5.4 Podman 兼容与功能成熟度

Ceph 与 Podman 的 EOL 节奏不同。官方保留的历史配对矩阵如下；它主要用于 legacy adoption 和跨旧版本升级判断，Tentacle 新部署仍应以目标 OS 与当前 release 的认证组合为准。

| Ceph | Podman 1.9 | 2.0 | 2.1 | 2.2 | 3.0 | > 3.0 |
|---|---:|---:|---:|---:|---:|---:|
| `<= 15.2.5` | 支持 | 不支持 | 不支持 | 不支持 | 不支持 | 不支持 |
| `>= 15.2.6` | 支持 | 支持 | 支持 | 不支持 | 不支持 | 不支持 |
| `>= 16.2.1` | 不支持 | 支持 | 支持 | 不支持 | 支持 | 支持 |
| `>= 17.2.0` | 不支持 | 支持 | 支持 | 不支持 | 支持 | 支持 |

Quincy 及以后未发现 Podman 3.0+ 的已知问题；Pacific 要求至少 2.0，但 2.2.1 明确不可用，Kubic stable 还要求较新 kernel。不要从这个历史表推导“任意最新 Podman 一定兼容”，仍需按目标 release/发行版实测。

Cephadm 自 Octopus 15.2.0 引入，不支持更旧 Ceph。官方把 ceph-exporter、stretch integration、监控服务发现/TLS、RGW multisite 自动化和 cephadm agent 列为持续演进领域；其中 RGW multisite 仍包含大量手工控制面步骤。遇到编排行为异常，应先 pause，而不是让 reconcile 继续放大变化。

## 6. CLI 与多集群隔离

```bash
cephadm shell
cephadm shell -- ceph -s
cephadm add-repo --release tentacle
cephadm install ceph-common
```

`cephadm shell` 可从本地 MON 容器推断配置，也可显式挂载 config/keyring。多集群主机始终显式指定 FSID 和材料，防止命令落到错误集群。`cephadm enter --name <daemon>` 进入 daemon 容器，仅用于诊断；容器内手改会在 redeploy 后消失。

## 7. 主机生命周期

### 7.1 加入

```bash
ceph cephadm get-pub-key > ceph.pub
ssh-copy-id -f -i ceph.pub root@host02
ssh root@host02 hostname
ceph orch host add host02 10.20.0.12
ceph orch host ls --detail
ceph orch host ls --host-pattern 'osd[0-9]+' --format yaml
ceph orch host ls --label osd --host-status offline --detail
```

`host add` 名必须与远端 `hostname` 完全相等。FQDN 或短名都可，但集群内统一；最好显式传 IP，避免 DNS 漂移改变 SSH 目标。

`--host-pattern` 按正则匹配 inventory hostname，`--label` 与 `--host-status` 可叠加过滤；当前状态过滤主要使用 `offline`、`maintenance`。容量审计和维护窗口不要只看无过滤的总数，应保存带 `--detail --format yaml` 的目标集合，逐台确认地址、标签、状态和 daemon 数。

```yaml
service_type: host
hostname: host02
addr: 10.20.0.12
labels: [osd]
location:
  root: default
  datacenter: dc-a
  rack: rack-01
```

`location` 仅首次加入生效，之后重应用 host spec 修改 location 会被忽略。现有 CRUSH 位置应通过 CRUSH 操作调整并评估数据迁移。

```mermaid
stateDiagram-v2
  [*] --> Candidate
  Candidate --> Checked: SSH 和 check-host 通过
  Checked --> Managed: host add
  Managed --> Maintenance: maintenance enter
  Maintenance --> Managed: maintenance exit
  Managed --> Draining: host drain
  Draining --> Empty: daemon 与 OSD 清空
  Empty --> Removed: host rm
  Removed --> [*]
```

### 7.2 特殊标签

| 标签 | 精确效果 | 风险 |
|---|---|---|
| `_admin` | 分发 `ceph.conf` 和 `client.admin` | 扩大超级用户凭据暴露面 |
| `_no_schedule` | 不再调度并迁移可迁移的非 OSD daemon | OSD 不自动搬走或删除 |
| `_no_conf_keyring` | 停止分发受管 config/keyring | 应用可能失去本地凭据 |
| `_no_autotune_memory` | 不自动调整该主机 OSD memory | 必须人工配置容量 |

`host drain` 默认添加 `_no_schedule` 和 `_no_conf_keyring`；`--keep-conf-keyring` 只添加 `_no_schedule`。

`_admin` 不是另一套复制机制：bootstrap 会给首主机加该标签，并通过 cephadm 的 client-keyring 管理把 `client.admin` 与 `ceph.conf` 持续分发到匹配主机。新增 `_admin` 会扩大可完全控制集群的凭据落盘范围；移除标签后必须验证受管文件已按 placement 收敛，并确认仍保留至少一个经过授权的管理入口。

### 7.3 Maintenance

```bash
ceph orch host maintenance enter host02
ceph orch host maintenance exit host02
```

- `--force` 只绕 warning，不绕 alert；
- `--yes-i-really-mean-it` 绕过所有检查，可能丢 quorum/可用性；
- `maintenance exit --force --offline` 只清除离线主机的 maintenance 标记；主机重新上线后 daemon 仍保持 stopped。

```mermaid
flowchart TD
  P[计划维护] --> S{安全检查}
  S -->|通过| E[maintenance enter]
  S -->|warning| F{风险已接受?}
  F -->|是| FW[使用 force]
  F -->|否| X[停止]
  S -->|alert| A{接受中断风险?}
  A -->|否| X
  A -->|是且获审批| Y[yes-i-really-mean-it]
  E & FW & Y --> M[维护并验证其他故障域]
  M --> O[maintenance exit]
```

### 7.4 Drain、删除与离线强删

```bash
ceph orch host drain host02
ceph orch osd rm status
ceph orch ps --hostname host02 --refresh
ceph orch host rm host02 --rm-crush-entry
```

Drain 会迁出非 OSD daemon并调度 OSD removal。`--zap-osd-devices` 会擦除 OSD 设备，必须核验序列号、备份和恢复状态。`--rm-crush-entry` 只有 host bucket 已空才成功；失败时主机仍受管。

```bash
ceph orch host rm host02 --offline --force
```

离线强删会走 OSD purge-actual 语义，可能丢数据；它不是 SSH 故障的方便替代。还必须从所有显式 placement/spec 中删除该主机。

## 8. 设备重扫与 OS tuning

```bash
ceph orch host rescan host02 --with-summary
ceph orch device ls --hostname host02 --wide --refresh
```

Rescan 会触发 SCSI host scan。在 SAN、多路径和大型盘柜先保存 HBA/multipath 状态，以 WWN/serial 对账后再允许 spec 消费。

```yaml
profile_name: 90-ceph-osd
placement:
  label: osd
settings:
  vm.swappiness: 1
  vm.zone_reclaim_mode: 0
```

```bash
ceph orch tuned-profile apply -i osd-profile.yaml
ceph orch tuned-profile ls --format yaml
ceph orch tuned-profile add-setting 90-ceph-osd vm.dirty_ratio 10
ceph orch tuned-profile rm-setting 90-ceph-osd vm.dirty_ratio
ceph orch tuned-profile rm 90-ceph-osd
```

Cephadm 写 `/etc/sysctl.d/<profile>-cephadm-tuned-profile.conf` 并执行 `sysctl --system`。文件名字典序决定覆盖关系；`--no-overwrite` 避免覆盖已有 profile。删除会移除文件但不保证恢复应用前历史值，必须预存 baseline 与回滚值。

## 9. SSH 运行期管理

```bash
ceph cephadm generate-key
ceph cephadm get-pub-key
ceph cephadm get-ssh-config
ceph cephadm set-user cephadmin
ceph cephadm set-ssh-config -i ssh_config
ceph cephadm set-priv-key -i id_cephadm
ceph cephadm set-pub-key -i id_cephadm.pub
ceph cephadm clear-key
ceph cephadm clear-ssh-config
```

私钥和 SSH config 存在 MON config-key 中，对所有 MGR 可见。变更 key 后重启或 failover MGR 重新加载。还可直接导入身份材料：

```bash
ceph config-key set mgr/cephadm/ssh_identity_key -i <private-key-file>
ceph config-key set mgr/cephadm/ssh_identity_pub -i <public-key-file>
# CA-signed 模式使用 signed certificate，不再同时提供普通 public key
ceph config-key set mgr/cephadm/ssh_identity_cert -i <signed-cert-file>
```

官方默认 SSH config 使用 `StrictHostKeyChecking no` 和 `UserKnownHostsFile /dev/null`，便于自动纳管，但不提供主机身份验证。生产建议通过 `ceph cephadm set-ssh-config -i` 注入企业 host CA/known-hosts 策略并演练 MGR failover。另一种 `mgr/cephadm/ssh_config_file` 路径方式不推荐：该路径必须同时存在于所有 MGR 容器；宿主机路径为 `/var/lib/ceph/<fsid>/mgr.<id>`，容器内为 `/var/lib/ceph/mgr/ceph-<id>`。不能引用只存在于某个管理员 shell 或单台 active MGR 的临时路径。

```mermaid
flowchart LR
  KEY[MON config-key SSH 身份] --> AM[active MGR]
  KEY --> SM[standby MGR]
  AM --> H1[host01 trust]
  AM --> H2[host02 trust]
  SM -.failover 后.-> H1
  SM -.failover 后.-> H2
```

轮换要验证 active MGR、主动 failover 后的新 active、所有主机和旧 key 撤销。

## 10. Service Spec：声明而非安装参数

### 10.1 通用结构

```yaml
service_type: rgw
service_id: site-a
placement:
  label: rgw
  count: 3
networks:
  - 10.40.0.0/24
unmanaged: false
extra_container_args:
  - argument: "--cpus=4"
    split: false
extra_entrypoint_args: []
custom_configs: []
spec:
  rgw_frontend_port: 8080
```

| 字段 | 契约 |
|---|---|
| `service_type` | 必填，选择 daemon 类型与专属 schema |
| `service_id` | 多实例组服务需要；与 type 组成 service name |
| `placement` | 候选主机与副本，不负责创建标签 |
| `networks` | 限制 bind 地址，目标主机须真实拥有匹配接口 |
| `unmanaged` | 暂停自动增删，保留 spec |
| `spec` | 服务专属字段 |
| `config` | 转换为 `ceph config set` |
| `custom_configs` | 额外文件；已有实例要 redeploy 才挂载 |

无效 `config` 产生 `CEPHADM_INVALID_CONFIG_OPTION`；设置失败产生 `CEPHADM_FAILED_SET_OPTION`。Daemon 仍运行不代表声明已落实。

```bash
ceph orch ls --service_name rgw.site-a --export > rgw.site-a.yaml
ceph orch apply -i rgw.site-a.yaml --dry-run
ceph orch apply -i rgw.site-a.yaml
```

同一 service 的后一次 apply 覆盖前一次。连续执行 `apply mon host1`、`host2`、`host3` 不会累加，最后只剩 host3 的期望；必须一次声明完整 placement。

主机与多个 service 可以放进同一个以 `---` 分隔的多文档 YAML，由一次 `ceph orch apply -i cluster.yaml` 提交；bootstrap 也可通过 `--apply-spec` 消费整份集群 spec。SSH key 必须在 host 文档被接纳前已部署到目标主机。一次提交便于版本化完整意图，但不是跨 service 的数据库事务：任一对象失败时，要用 `orch ls --export`、events 和逐项状态确认哪些对象已经收敛，修正原文件后重应用，不能假定整体自动回滚。

### 10.2 Placement 算法

```mermaid
flowchart TD
  S[读取 placement] --> E{explicit hosts?}
  E -->|是| CE[候选=explicit]
  E -->|否| L{label?}
  L -->|是| CL[候选=label 匹配]
  L -->|否| P{host_pattern?}
  P -->|是| CP[候选=fnmatch 或 regex]
  P -->|否| CA[候选=全部 inventory]
  CE & CL & CP & CA --> NS[排除 _no_schedule]
  NS --> K[尽量保留现有 daemon]
  K --> C{与 count 比较}
  C -->|不足| ADD[从候选补足]
  C -->|超出| DEL[删除多余]
  C -->|相等| KEEP[保持]
  ADD & DEL & KEEP --> OUT[删除候选外实例]
```

`host_pattern` 默认 fnmatch，`regex:` 前缀才是正则。`count` 大于主机数仍默认一主机一实例；同机多实例必须 `count_per_host`。标签集合变化会导致真实迁移/删除，应先 dry-run。

### 10.3 额外参数与文件

字符串参数中空格默认拆分；需保持单参数时使用 `split: false`：

```yaml
extra_container_args:
  - "--cpus 4"
  - argument: "--annotation=com.example.note=production ceph"
    split: false
extra_entrypoint_args:
  - argument: "--title=Primary Storage"
    split: false
```

命令行可被进程列表看到，不传 secret。`custom_configs` 应在 apply 后执行：

```bash
ceph orch redeploy <service-name>
```

Managed daemon 手工删除会被自动重建；先设 unmanaged 才能保持手工拓扑。完整的例外操作闭环是：

```bash
ceph orch ls --service_name <service> --export > <service>.yaml
ceph orch set-unmanaged <service>
ceph orch daemon add <daemon-type> --placement=<placement>
ceph orch daemon rm <daemon-name>... [--force]
ceph orch set-managed <service>
ceph orch apply -i <service>.yaml --dry-run
```

手工增加前必须先设 unmanaged；恢复 managed 后，reconcile 会按原 placement 判断，多出的手工 daemon 可能被删除。对 managed service 手工 `daemon rm`，cephadm 会在数秒内补回实例。没有对应 spec、仅用于跟踪孤立 OSD 的特殊 `osd` service 永远是 unmanaged，对它执行 `set-managed`/`set-unmanaged` 会报找不到 service。

## 11. 状态缓存与 daemon 动作

```bash
ceph orch ls --refresh
ceph orch ls --export
ceph orch ps --refresh
ceph orch daemon stop <daemon>
ceph orch daemon start <daemon>
ceph orch daemon restart <daemon>
ceph orch daemon reconfig <daemon>
ceph orch daemon redeploy <daemon> [--image <image>]
ceph orch daemon rotate-key <daemon>
```

`orch ps` 默认可能缓存约 10 分钟；看 `REFRESHED` 或使用 `--refresh`，缓存周期由 `daemon_cache_timeout` 控制。事故期间可临时缩短，但会增加主机查询负载：

```bash
ceph config set mgr mgr/cephadm/daemon_cache_timeout 60
```

调查结束后恢复原值。MDS/OSD/MGR rotate-key 无需重启，其他 daemon 需要适当重启。

```mermaid
flowchart LR
  SPEC[service spec] --> SV[service 状态]
  SV --> D1[daemon 1]
  SV --> D2[daemon 2]
  D1 & D2 --> CACHE[daemon cache]
  CACHE --> PS[orch ps]
  REF[--refresh] --> LIVE[重新查询主机]
  LIVE --> PS
```

`ceph orch restart osd.<service>` 不考虑 CRUSH failure domain，可能同时重启持有同一 PG 副本的 OSD。OSD 必须按 host/rack 与 PG 可用性小批操作。

## 12. OSD inventory

### 12.1 Available 的严格条件

设备必须同时：无 partition、无 LVM、未挂载、无 filesystem、无 BlueStore OSD、容量大于 5 GB。

```bash
ceph orch device ls --wide --refresh
ceph orch device ls --hostname host02 --wide
cephadm shell -- ceph-volume inventory /dev/sdb --format json
```

```mermaid
flowchart TD
  D[块设备] --> P{有分区?}
  P -->|是| N[Not available]
  P -->|否| L{有 LVM?}
  L -->|是| N
  L -->|否| M{挂载或 filesystem?}
  M -->|是| N
  M -->|否| B{BlueStore OSD?}
  B -->|是| N
  B -->|否| Z{容量 > 5GB?}
  Z -->|否| N
  Z -->|是| A[Available]
```

以 `--wide` 的 reject reasons 为入口，再用 serial/WWN、`lsblk -f`、LVM、multipath 和 ceph-volume 交叉识别，不能见盘符就 zap。

### 12.2 Enhanced scan

```bash
cephadm shell -- lsmcli ldl
ceph config set mgr mgr/cephadm/device_enhanced_scan true
```

Enhanced scan 用 libstoragemgmt 提供 Health/Ident/Fault。旧硬件可能因 SCSI inquiry bus reset；先在同型号非生产设备执行 `lsmcli ldl`。libstoragemgmt 1.8.8 正式支持本地 SCSI/SAS/SATA，不把 NVMe、SAN、复杂 meta-device 视为等同支持。

### 12.3 精确容量

```bash
cephadm shell -- ceph-volume inventory /dev/sdc --format json \
  | jq .sys_api.human_readable_size
```

DriveGroup 按 ceph-volume 值过滤；官方例中 3.64 TB 换算为 3727.36 GB。标称容量不精确，应使用范围 filter。

## 13. OSD DriveGroup

### 13.1 创建入口与持续语义

```bash
ceph orch apply osd --all-available-devices
ceph orch daemon add osd host02:/dev/sdb
ceph orch daemon add osd \
  host02:data_devices=/dev/sda,/dev/sdb,db_devices=/dev/nvme0n1,osds_per_device=2
```

目标主机不应安装宿主机 `ceph-osd` 包，以免与容器管理冲突。一次性 `daemon add` 创建归入 `osd.default` 的受管 spec；已有 OSD 可用：

```bash
ceph orch osd set-spec-affinity <service-name> <osd-id> [<osd-id>...]
```

`apply osd --all-available-devices` 是持久声明：未来新增盘、或被 zap 后重新 available 的盘都会再次被消费。

```mermaid
stateDiagram-v2
  [*] --> Unavailable
  Unavailable --> Available: 清除签名
  Available --> Selected: 匹配 managed spec
  Selected --> OSD: reconcile 创建
  OSD --> Removing: orch osd rm
  Removing --> Available: rm --zap
  Available --> Selected: spec 仍存在
```

要保留人工换盘窗口，先把 spec 设 `unmanaged: true`；它会停止创建新 OSD，即使重新 apply 也不创建，直到恢复 managed。

### 13.2 生产 spec

```yaml
service_type: osd
service_id: hdd-with-nvme-db
placement:
  label: osd-hdd
spec:
  data_devices:
    rotational: 1
    size: "4T:24T"
  db_devices:
    rotational: 0
    size: "800G:4T"
    limit: 2
  db_slots: 6
  block_db_size: 400G
  block_wal_size: 4G
  osds_per_device: 1
  data_allocate_fraction: 1.0
  method: lvm
  encrypted: true
  crush_device_class: hdd
```

```bash
ceph orch apply -i osd-hdd.yaml --dry-run
ceph orch apply -i osd-hdd.yaml
ceph orch ls --service_name osd.hdd-with-nvme-db --export
```

高级 spec 必须有唯一 `service_id`。复用 id 覆盖旧 spec，但现有 OSD 不变，只影响以后出现的 available 盘。

### 13.3 过滤器

| 过滤器 | 语义 | 风险控制 |
|---|---|---|
| `model` | 型号匹配 | 替换型号变化会失配 |
| `vendor` | 厂商匹配 | 同厂商不等于同介质/耐久 |
| `size` | `LOW:HIGH`、`:HIGH`、`LOW:`、`EXACT`；M/G/T 或 MB/GB/TB | 推荐范围 |
| `rotational` | `1` HDD，`0` 非旋转 | SAN/复合设备 kernel 属性可能失真 |
| `paths` | 明确路径 | Linux/HBA 重枚举会漂移 |
| `all: true` | 全部 available | 仅可用于 `data_devices` |
| `limit` | 每主机最大匹配数 | 不固定具体序列号，须 dry-run |

同一 selector 默认 AND；`filter_logic: OR` 改为任一条件。优先 vendor/model/size 等稳定属性，不依赖 `/dev/sdX`。

### 13.4 DB/WAL、slots、加密与 class

```mermaid
flowchart LR
  H1[HDD data 1] --> S1[NVMe DB slot 1]
  H2[HDD data 2] --> S2[NVMe DB slot 2]
  H3[HDD data 3] --> S3[NVMe DB slot 3]
  S1 & S2 & S3 --> NV[NVMe db_device]
  H1 & H2 & H3 --> WAL[可选独立 WAL]
```

`db_slots`/`wal_slots` 把高速盘切给多个 OSD。过小 DB 会 spill 到慢盘，过度共享会扩大 NVMe 故障半径。大多数场景 WAL 与 DB 共置；独立 WAL 需基准与故障论证。

`encrypted: true` 使用 LUKS；Tentacle 支持 `tpm2: true` 为 LUKS2 enrollment 使用 TPM2。必须验证固件升级、主板更换和灾难恢复。`crush_device_class` 可在 service 或 `paths` 单盘粒度指定；创建后核对 `ceph osd tree` 和 pool rule。

DriveGroup 的部署控制字段必须在变更评审中逐项解释：

| 字段 | Tentacle 语义 | 生产约束 |
|---|---|---|
| `block_db_size` | 覆盖每个 OSD 的 BlueStore DB 大小，整数或容量字符串 | 先按 RocksDB 增长和高速盘总容量验算，不能让 `db_slots * block_db_size` 超卖 |
| `block_wal_size` | 覆盖 BlueStore WAL 大小 | 大多数负载与 DB 共置即可；独立 WAL 必须有延迟基准 |
| `osds_per_device` | 每个 data device 创建的 OSD 数 | NVMe 或双执行器盘可大于 1；HDD 通常保持 1，并计入内存、CPU、PG 和故障半径 |
| `data_allocate_fraction` | 使用 data device 的比例，范围 `(0, 1.0]` | 留白不是备份；必须验证后续分区/LVM 操作不会破坏 OSD |
| `method` | `lvm` 或 `raw` | 默认优先成熟的 LVM 路径；raw 仅支持 BlueStore，迁移和恢复工具链要先验证 |
| `objectstore` | 当前源码只接受 `bluestore` | `filestore` 仅是历史字段语义，不得用于 Tentacle 新建 OSD |
| `preview_only` | 将 spec 作为预览声明处理 | 不能替代命令行 `--dry-run` 的变更前证据 |
| `osd_id_claims` | host 到待复用 OSD ID 列表 | 仅用于已确认 replacement/destroyed 身份，不能人工猜测 ID |

`data_devices` 必填，placement 不能为空；只有 `data_devices` 允许 `all: true`，DB、WAL 和 legacy journal selector 使用 `all` 会被 schema 拒绝。`block_db_size`、`block_wal_size` 接受整数或字符串，但仍应使用明确容量字符串避免人工换算错误。`journal_devices`、`journal_size`、`data_directories` 是历史兼容字段；Tentacle 新部署只设计 BlueStore data/DB/WAL。

多个磁盘布局必须使用多个唯一 service ID，按主机标签隔离，不能用一个宽泛 selector 抢盘：

```yaml
service_type: osd
service_id: capacity-hdd
placement:
  label: osd-capacity
spec:
  data_devices:
    rotational: 1
  db_devices:
    rotational: 0
    limit: 2
  db_slots: 5
---
service_type: osd
service_id: performance-nvme
placement:
  label: osd-performance
spec:
  data_devices:
    rotational: 0
    size: "3T:8T"
  osds_per_device: 2
  crush_device_class: nvme
```

```mermaid
flowchart TD
  I[device inventory] --> L{主机磁盘布局标签}
  L -->|capacity| H[HDD data + SSD DB slots]
  L -->|performance| N[NVMe data + 多 OSD]
  H & N --> D[dry-run 保存 device/serial/role/size]
  D --> Q{DB/WAL 容量和故障半径合格?}
  Q -->|否| R[修改 selector 与容量]
  Q -->|是| A[apply]
  A --> V[核验 OSD ID、设备、LV、class、CRUSH]
```

### 13.5 OSD memory autotune

Bootstrap 默认设置 `osd_memory_target_autotune=true`，并把 `mgr/cephadm/autotune_memory_target_ratio` 设为主机总内存的 `.7`。Cephadm 先从该预算扣除不参与 autotune 的 daemon 和手工固定 OSD，再把余量分给参与的 OSD；最终值写入 config database，并在 `ceph orch ps` 的 `MEM LIMIT` 展示。

```mermaid
flowchart LR
  RAM[主机总内存] --> R[乘 autotune_memory_target_ratio]
  R --> N[扣除非 autotune daemon]
  N --> F[扣除固定 memory OSD]
  F --> D[余量除以 autotune OSD 数]
  D --> T[各 OSD osd_memory_target]
```

`.7` 适合 Ceph 专用节点，不适合超融合计算/存储混部。官方示例先降到 `.2` 再启用：

```bash
ceph config set mgr mgr/cephadm/autotune_memory_target_ratio 0.2
ceph config set osd osd_memory_target_autotune true
```

可对单个 OSD 退出并固定值：

```bash
ceph config set osd.123 osd_memory_target_autotune false
ceph config set osd.123 osd_memory_target 16G
```

主机 `_no_autotune_memory` 标签停止该主机自动调节。任何 ratio 必须给 kernel、runtime、MON/MGR、网络和业务进程保留实测余量；不能把 `.7` 或 `.2` 当成跨硬件固定容量答案。

## 14. OSD 删除、换盘与激活

### 14.1 安全删除

```bash
ceph orch osd rm 12
ceph orch osd rm status
ceph orch osd rm stop 12
```

```mermaid
stateDiagram-v2
  [*] --> Active
  Active --> Queued: orch osd rm
  Queued --> Draining: PG 迁出
  Draining --> Active: rm stop
  Draining --> SafeToDestroy: PG 清空
  SafeToDestroy --> Purged: 删除 OSD
  Purged --> Zapped: 指定 --zap
  Zapped --> [*]
```

默认不 safe-to-destroy 就拒绝删除；`--force` 绕过安全约束。`--zap` 再删除 LVM/partition。持续观察 degraded/misplaced、恢复带宽、业务延迟和 backfillfull；超过阈值就 stop removal。

### 14.2 替换与复用 ID

```bash
ceph orch osd rm 12 --replace
ceph orch osd rm status
ceph orch apply -i osd-hdd.yaml --dry-run
```

`--replace` 排空数据，但在 CRUSH 中保留 OSD 并标记 `destroyed`。替代盘必须在同一 host 且匹配原 OSDSpec，下一次部署才复用 ID。

```mermaid
flowchart TD
  F[核对故障盘序列号与 OSD ID] --> R[rm --replace]
  R --> D[等待 drain 与 destroyed]
  D --> P[物理换盘]
  P --> I[refresh inventory]
  I --> Y[dry-run 匹配原 spec]
  Y --> A[apply]
  A --> V[验证 ID up+in 与 PG]
```

```bash
ceph orch device zap host02 /dev/disk/by-id/<verified-id>
```

Managed `all-available-devices` 存在时，zap 后会被自动重建。

### 14.3 设备级自动替换

Tentacle 的 `ceph orch device replace` 自动完成底层 LVM OSD 设备替换准备：

```bash
ceph orch device replace <host> <device-path>
```

它仅支持 LVM 部署的 OSD。设备是多个 OSD 共享的 DB/WAL 时，命令会列出将被销毁的所有 OSD 并拒绝继续；只有逐一核对这些 OSD 的 data device、容量和故障域后，才可：

```bash
ceph orch device replace host02 /dev/disk/by-id/<verified-id> \
  --yes-i-really-mean-it
```

Cephadm/ceph-volume 会 zap 关联设备、把相应 OSD 标记 `destroyed` 以保留 ID，并给旧设备写入 replacement header。Inventory 显示 `Is being replaced`，阻止 reconcile 过快重新消费。若取消/修正操作：

```bash
ceph orch device replace host02 /dev/disk/by-id/<verified-id> --clear
```

清除 header 后，managed spec 会在数分钟内重新部署，除非 service 为 unmanaged。设备级命令适合共享 DB/WAL 的完整影响分析；单 OSD 换盘仍可按 `osd rm --replace` 流程。

### 14.4 OS 重装激活

保留 OSD 盘重装系统后，恢复 cephadm、runtime、SSH 和 registry 登录，不重新 prepare 数据盘：

```bash
ceph cephadm osd activate host02
```

它扫描并部署缺失 OSD daemon。私有 registry 场景先执行集群级 `ceph cephadm registry-login`，并确认该主机实际能拉取固定 image；登录动作也可能触发该主机其他缺失 daemon 的 reconcile，因此先确保 service spec 正确，并明确是否要让 host 退出 offline/maintenance。若为测试取出 cephadm private key，验证后立即删除且不写日志。

## 15. MON：quorum、网络与 CRUSH location

### 15.1 自动规模与显式 placement

典型集群部署 3 或 5 个 MON；官方建议节点达到 5 台时部署 5 个。Cephadm 会随集群扩缩，在 bootstrap 得到的默认 subnet 上自动放置最多 5 个 MON；前提是 subnet 配置正确且候选主机拥有该网络地址。

```bash
ceph config set mon public_network 10.20.0.0/24
ceph config set mon public_network 10.20.0.0/24,10.21.0.0/24
```

显式 placement 后，管理员负责奇数规模、主机/机架故障域和网络可达性：

```yaml
service_type: mon
placement:
  hosts:
    - host01
    - host02
    - host03
```

```bash
ceph orch apply -i mon.yaml --dry-run
ceph orch apply -i mon.yaml
ceph quorum_status --format json-pretty
ceph mon dump
```

### 15.2 指定地址与迁移网络

需要逐个指定地址时，先暂停自动 placement：

```bash
ceph orch apply mon --unmanaged
ceph orch daemon add mon host04:10.21.0.14
ceph orch daemon add mon host05:10.21.0.0/24
```

MON 网络迁移不能手工注入 monmap 来“整体切换”。正确顺序是先在新网络增加 MON，逐个确认入 quorum，形成新网络多数派，再移除旧 MON，最后更新 `public_network` 并恢复 managed spec。

```mermaid
flowchart LR
  Q0[旧网络 3 MON quorum] --> U[MON service unmanaged]
  U --> N1[新网络增加 MON 1]
  N1 --> V1[验证 quorum/mon dump]
  V1 --> N2[新网络增加 MON 2]
  N2 --> V2[新网络已有多数派]
  V2 --> R[逐个删除旧 MON]
  R --> P[更新 public_network]
  P --> S[apply 完整 managed spec]
```

任何时刻都不得让可用 MON 少于多数派。每删除一个 MON 前保存 `ceph quorum_status`；新 MON 未稳定入 quorum，不进入下一步。

### 15.3 MON CRUSH location

```yaml
service_type: mon
placement:
  count: 5
spec:
  crush_locations:
    host01:
      - datacenter=dc-a
    host02:
      - datacenter=dc-b
      - rack=rack-02
```

Cephadm 在部署 MON 时把第一条 location 作为 `--set-crush-location`，额外条目通过 `ceph mon set_location` 设置。它特别适合替换 stretch cluster 的 tiebreaker MON。已存在 MON 只有在 redeploy 后才获得启动参数；多 location 可能因加入 quorum 的时序未全部设置，此时重应用同一 spec 触发服务动作并核验 `mon dump`。

## 16. MGR 与 MDS

### 16.1 MGR

MGR 承载 Dashboard、cephadm、Prometheus 等模块。可限制绑定网络：

```yaml
service_type: mgr
placement:
  count: 2
networks:
  - 10.20.0.0/24
```

即使单主机，为自动升级也需要至少两个 MGR；允许 co-location 不等于有主机级 HA。生产把 active 与 standby 放在不同故障域，并验证：

```bash
ceph mgr dump
ceph mgr services
ceph mgr fail <active-name>
ceph -s
```

```mermaid
stateDiagram-v2
  [*] --> Active
  [*] --> Standby
  Active --> Failed: mgr fail 或主机故障
  Standby --> Active: MON 选举接管
  Failed --> Standby: 恢复并重新加入
```

`ceph orch daemon redeploy mgr.x` 重建实例；`ceph mgr fail` 触发 active 切换，两者不能混称“重启 MGR”。

### 16.2 MDS

使用 volume 接口创建 CephFS 时可自动创建 MDS：

```bash
ceph fs volume create fs-prod --placement="label:mds"
```

也可独立声明：

```yaml
service_type: mds
service_id: fs-prod
placement:
  count: 3
  label: mds
```

```bash
ceph orch apply -i mds.yaml
ceph fs status fs-prod
ceph mds stat
```

MDS daemon count 应覆盖 active rank 和 standby，但 `count` 与 `max_mds` 是两个控制面：提高 count 不会自动增加 active rank，提高 `max_mds` 也不会自动生成足够 daemon。删除 MDS service 前先把文件系统降到安全 rank 或停止；删容器不等于可以删除 metadata/data pool。

```mermaid
flowchart LR
  SPEC[MDS placement count] --> DAEMONS[MDS daemon 总数]
  FS[max_mds] --> ACTIVE[active ranks]
  DAEMONS --> ACTIVE
  DAEMONS --> STANDBY[standby 数量]
  ACTIVE --> IO[CephFS 元数据服务]
  STANDBY --> FAIL[rank 故障接管]
```

## 17. RGW 与 Ingress

### 17.1 单站点与 multisite

Cephadm 只部署 RGW daemon；RGW 配置来自 MON config database，而非依赖本地 `ceph.conf`。若未准备相应 `client.rgw.*` 配置，daemon 会按默认值启动，例如默认绑定 80，可能与安全/端口规划不一致。

```bash
# 单集群，默认部署两个 daemon
ceph orch apply rgw site-a

# 标记网关主机，每主机两个实例，从 8000 起使用连续端口
ceph orch host label add rgw01 rgw
ceph orch host label add rgw02 rgw
ceph orch apply rgw site-a \
  --placement="label:rgw count-per-host:2" --port=8000
```

```yaml
service_type: rgw
service_id: site-a
placement:
  label: rgw
  count_per_host: 2
networks:
  - 10.40.0.0/24
spec:
  rgw_realm: corp
  rgw_zonegroup: east-zg
  rgw_zone: east-1
  rgw_frontend_type: beast
  rgw_frontend_port: 8080
  rgw_frontend_extra_args:
    - tcp_nodelay=1
    - max_header_size=65536
```

Cephadm 把 frontend 基础字段与 `rgw_frontend_extra_args` 合并成空格分隔的 `rgw_frontends`。优先使用专用字段，extra args 仅补充无字段表达的 Beast 参数。

Multisite 下 cephadm 不创建或更新 realm/zonegroup/zone/period。部署 daemon 前先完成：

```bash
radosgw-admin realm create --rgw-realm=corp
radosgw-admin zonegroup create --rgw-zonegroup=east-zg --master
radosgw-admin zone create \
  --rgw-zonegroup=east-zg --rgw-zone=east-1 --master
radosgw-admin period update --rgw-realm=corp --commit
ceph orch apply rgw east \
  --realm=corp --zonegroup=east-zg --zone=east-1 \
  --placement="2 rgw01 rgw02"
```

```mermaid
flowchart TD
  R[realm] --> ZG[zonegroup]
  ZG --> Z[zone]
  Z --> P[period commit]
  P --> S[RGW Service Spec]
  S --> D1[RGW daemon 1]
  S --> D2[RGW daemon 2]
```

### 17.2 HTTPS、wildcard 与同步职责

RGW 企业证书必须通过真实 schema 字段 `rgw_frontend_ssl_certificate` 提供，值是按顺序拼接的 PEM private key 和 certificate chain，并设置 `ssl: true`：

```yaml
service_type: rgw
service_id: site-a
spec:
  ssl: true
  rgw_frontend_port: 8443
  rgw_frontend_ssl_certificate: |
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
    -----BEGIN CERTIFICATE-----
    ...leaf certificate...
    -----END CERTIFICATE-----
    -----BEGIN CERTIFICATE-----
    ...intermediate CA...
    -----END CERTIFICATE-----
```

`|` 必须保留换行。应用前验证 private key 匹配 leaf certificate、SAN、用途、完整 chain 和过期时间；应用后从客户端完成 TLS hostname verification，不能只检查 8443 端口。也可让 CertMgr 生成证书：

```yaml
spec:
  ssl: true
  generate_cert: true
  rgw_frontend_port: 8443
  wildcard_enabled: true
  zonegroup_hostnames:
    - s3.example.com
```

`generate_cert: true` 必须同时设置 `ssl: true`，并与 `rgw_frontend_ssl_certificate` 互斥。`wildcard_enabled` 默认 false；开启后自签证书加入 `*.s3.example.com`，只覆盖一个 DNS label 层级。使用企业证书时核验完整链、私钥匹配、SAN 和 virtual-hosted-style bucket DNS。

RGW ServiceSpec 的其他控制面不能混为普通 `rgw_frontend_extra_args`：

| 字段 | 用途 | 生产要求 |
|---|---|---|
| `rgw_realm_token` | 将 RGW service 接入已有 realm 的 bootstrap token | 按 secret 保存，不进入 Git、shell history 或普通工单 |
| `update_endpoints` | 允许编排更新 zone endpoints | 先确认 period 和多站点变更责任，避免覆盖外部管理结果 |
| `zone_endpoints` | 逗号分隔的明确 endpoint 列表 | 所有站点可解析、TLS 名称匹配且健康检查可达 |
| `only_bind_port_on_networks` | 只在顶层 `networks` 匹配地址监听 | 每台 placement host 必须真实拥有匹配地址 |
| `rgw_user_counters_cache` / `_size` | 启用并设置用户操作计数缓存 | 高基数会消耗内存，按观测目标和用户规模压测 |
| `rgw_bucket_counters_cache` / `_size` | 启用并设置桶操作计数缓存 | 桶数量大时同样需要容量评估 |
| `data_pool_attributes` | realm bootstrap 创建 data pool 时定义 replicated/EC 属性 | 默认按 EC 解析；EC 必须同时提供 `k`、`m`，不接受手工 `erasure_code_profile` |

指定 `rgw_realm` 时必须同时指定 `rgw_zone`，反之亦然；frontend type 只接受 `beast` 或兼容保留的 `civetweb`。这些校验应在 `apply --dry-run` 阶段通过，而不是等 daemon 反复退出。

```yaml
spec:
  disable_multisite_sync_traffic: true
```

它令该 service 的 `rgw_run_sync_thread=false`，只停止发送同步数据；只要该 endpoint 仍在 zone/zonegroup 中，它仍可能接收复制。它是 I/O 与 sync 角色分离工具，不是网络隔离。

### 17.3 停机排空

Cephadm 部署的 RGW 默认启用 120 秒退出排空：停止接收新请求，并等待在途请求完成。显式设置：

```yaml
spec:
  rgw_exit_timeout_secs: 120
```

设置 `0` 才关闭。修改后必须 `ceph orch redeploy rgw.site-a`，仅 apply 不会让现有 daemon 拾取。容器 stop timeout、负载均衡摘流时间与该值要协调，否则外层先杀容器，内部排空无效。

### 17.4 Ingress HA

Ingress 在每个 placement host 部署 HAProxy + Keepalived；同一时刻一个 host 持有 VIP，active HAProxy 向所有 RGW backend 分流。RGW backend 启用 SSL 时，HAProxy 用 SSL 连接但因按 IP 访问而 `verify none`，不能把这段称为后端身份验证。

```mermaid
flowchart LR
  C[S3 Swift 客户端] --> VIP[Virtual IP]
  VIP --> K1[Keepalived master]
  VIP -.故障漂移.-> K2[Keepalived backup]
  K1 --> H1[HAProxy 1]
  K2 --> H2[HAProxy 2]
  H1 & H2 --> R1[RGW 1]
  H1 & H2 --> R2[RGW 2]
  H1 & H2 --> R3[RGW 3]
```

```yaml
service_type: ingress
service_id: rgw.site-a
placement:
  hosts: [gw01, gw02, gw03]
spec:
  backend_service: rgw.site-a
  virtual_ip: 10.40.0.100/24
  frontend_port: 443
  monitor_port: 1967
  ssl: true
  ssl_cert: |
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
  ssl_key: |
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
  ssl_ciphers: [<approved-cipher>]
  ssl_options: [no-sslv3]
  enable_stats: true
  monitor_user: admin
  monitor_password: <secret>
  keepalived_password: <secret>
  virtual_interface_networks:
    - 10.40.0.0/24
  use_keepalived_multicast: false
  vrrp_interface_network: 10.41.0.0/24
  first_virtual_router_id: 50
  health_check_interval: 2s
```

也可用 `virtual_ips_list` 配多个 VIP，每个 IP 建立一个 virtual router；VIP 数量不得超过 ingress 节点数。`first_virtual_router_id` 默认 50，有效 1-255，多 ingress 服务要避免 ID 冲突。Keepalived 默认 unicast，设置 multicast 后使用 `224.0.0.18`。

Ingress schema 的硬校验如下：

- `backend_service` 必填；非 `keepalive_only` 模式同时要求 `frontend_port` 和 `monitor_port`；
- `virtual_ip` 与 `virtual_ips_list` 二选一，不能同时存在，也不能都为空；
- `health_check_interval` 只接受整数加 `s`、`m` 或 `h`，例如 `2s`、`1m`；
- `ssl_dh_param` 可提供审核过的 DH 参数；`ssl_ciphers`、`ssl_options` 必须与企业 TLS 基线一致；
- monitor 和 keepalived password 是 secret，不能使用示例默认值，也不能写入公开 spec 仓库。

Cephadm 依据目标 subnet 上已有 IP 选择 VIP 接口，而不是接受接口名。若 VIP subnet 没有已有地址，可在正确接口配置不可路由 dummy IP，并让 `virtual_interface_networks` 匹配该 dummy network。官方建议至少 3 个 RGW 和 3 个 ingress host。

验收必须包含 VIP 漂移、HAProxy monitor、S3 签名请求、bucket PUT/GET/DELETE；TCP 端口可达不证明 RGW 业务。

## 18. NFS-Ganesha

### 18.1 服务与协议边界

官方 cephadm NFS 章节以 NFSv4 为支持基线；spec 同时提供 `enable_nfsv3: true` 显式开启 NFSv3。常规管理优先 `ceph nfs cluster/export`，直接 Service Spec 用于特殊拓扑。

```bash
ceph orch apply nfs prod --port 2049 --placement="label:nfs"
```

```yaml
service_type: nfs
service_id: prod
placement:
  hosts: [nfs01, nfs02]
spec:
  port: 12049
  monitoring_port: 19000
  enable_nfsv3: false
  enable_nlm: false
  idmap_conf:
    General:
      Domain: example.com
```

配置保存在 `.nfs` pool，export 由 `ceph nfs export ...` 或 Dashboard 管理。`idmap_conf` 按节和键生成 NFS idmapping 配置，域必须与客户端一致，否则文件可访问但 UID/GID 映射错误。`enable_nlm` 默认 false，仅在确实需要 Network Lock Manager 的 NFSv3 兼容场景开启；同时验证锁恢复、grace period 和 failover。服务 running 后还需从客户端 mount、创建、读回、锁和 failover 验收。

### 18.2 HAProxy + Keepalived 模式

```yaml
service_type: ingress
service_id: nfs.prod
placement:
  count: 2
spec:
  backend_service: nfs.prod
  frontend_port: 2049
  monitor_port: 9000
  virtual_ip: 10.40.0.110/24
```

Backend NFS 应使用非 2049 端口，避免 ingress 与 NFS 共置时冲突。Monitor 页面默认用户 `admin`；未设置 password 时读取自动生成值：

```bash
ceph config-key get mgr/cephadm/ingress.nfs.prod/monitor_password
```

该输出按 secret 处理。

### 18.3 Keepalived-only 模式

若不需要 HAProxy，可让 NFS daemon 直接绑定 VIP：

```yaml
service_type: ingress
service_id: nfs.prod
placement:
  hosts: [nfs01, nfs02, nfs03]
spec:
  backend_service: nfs.prod
  monitor_port: 9049
  virtual_ip: 10.40.0.110/24
  keepalive_only: true
---
service_type: nfs
service_id: prod
placement:
  count: 1
  hosts: [nfs01, nfs02, nfs03]
spec:
  port: 2049
  virtual_ip: 10.40.0.110
```

此模式必须 `count: 1`，因为同一时刻只能一个 NFS daemon 绑定 VIP。可先建 ingress 让 VIP 存在，再建 NFS；或通过 NFS module 创建以管理顺序。

```mermaid
flowchart TD
  N[选择 NFS 入口] --> H{需要 HAProxy 分流?}
  H -->|是| HP[HAProxy + Keepalived]
  HP --> B[backend NFS 使用非 2049]
  H -->|否| KO[keepalive_only]
  KO --> O[单个 NFS daemon 直接绑定 VIP]
  B & O --> V[mount/I/O/锁/failover 验收]
```

### 18.4 HAProxy Protocol

NFS-Ganesha 5.0+ 才支持。Ingress 与 NFS spec 的 `enable_haproxy_protocol` 必须同时为 true 或同时为 false；单边启用会把正常流量解释成损坏协议。该模式用于把真实 client IP 传给 export 级策略。

## 19. iSCSI

```yaml
service_type: iscsi
service_id: iscsi
placement:
  hosts: [gw01, gw02]
spec:
  pool: iscsi_pool
  trusted_ip_list: "10.50.0.11,10.50.0.12"
  api_port: 5000
  api_user: <secret-user>
  api_password: <secret-password>
  api_secure: true
  ssl_cert: |
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
  ssl_key: |
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
```

```bash
ceph orch apply -i iscsi.yaml
ceph orch ps --service_name iscsi.iscsi --refresh
```

`pool` 存放 ceph-iscsi 配置状态；trusted IP、API 凭据和 TLS key 是控制面安全边界。至少两个 gateway 加客户端 multipath 才构成路径 HA；同一主机两个容器没有主机容错。

```mermaid
flowchart LR
  I[Initiator multipath] --> G1[iSCSI gateway 1]
  I --> G2[iSCSI gateway 2]
  G1 & G2 --> CFG[配置 pool]
  G1 & G2 --> RBD[RBD images]
  RBD --> OSD[RADOS]
```

删除 service 前先在 initiator 停止业务写入、卸载 filesystem、登出 session、确认 multipath 无使用者，再删除 target/gateway。验收包含 discovery、CHAP/TLS（如启用）、多路径、读写和单 gateway 故障。

## 20. SMB

SMB 支持仍处于活跃开发，官方优先推荐使用 `smb` MGR module；直接 service spec 只在 module 不适用时使用。

```yaml
service_type: smb
service_id: tango
placement:
  hosts: [smb01]
spec:
  cluster_id: tango
  features: [domain]
  config_uri: rados://.smb/tango/scc.toml
  custom_dns: [192.168.76.204]
  join_sources:
    - rados:mon-config-key:smb/config/tango/join1.json
  include_ceph_users:
    - client.smb.fs.cluster.tango
```

字段语义：

| 字段 | 作用 |
|---|---|
| `cluster_id` | 一组共享配置的 Samba 管理单元，不自动代表 HA |
| `features` | `domain` 域成员；`clustered` Samba/CTDB 集群 |
| `config_uri` | `http:`、`https:`、`rados:`、`rados:mon-config-key:` 主配置源 |
| `user_sources` | 本地用户凭据 URI 列表 |
| `join_sources` | 域加入凭据 URI，按顺序尝试直到成功 |
| `custom_dns` | 容器访问 AD DNS 的服务器 |
| `custom_ports` | 覆盖 `smb`、`smbmetrics`、`ctdb` 端口 |
| `bind_addrs` | 以单 address 或 network 限制绑定，两字段互斥 |
| `include_ceph_users` | 自动把指定 CephX key 放入容器 keyring |
| `cluster_meta_uri` | `clustered` 必需，RADOS pseudo-URI |
| `cluster_lock_uri` | `clustered` 必需，CTDB cluster lock 的 RADOS pseudo-URI |
| `cluster_public_addrs` | clustered 模式由 CTDB 管理的浮动地址与 destination network |
| `remote_control_ssl_cert` | SMB remote-control 服务证书 |
| `remote_control_ssl_key` | remote-control 对应私钥 |
| `remote_control_ca_cert` | 校验 remote-control 对端的 CA |

未设置 `clustered` 时，多 Samba 实例没有透明状态迁移，不能宣称 HA。配置可放 `.smb` pool 的 cluster namespace：

```bash
rados --pool=.smb --namespace=tango put config.json /tmp/config.json
```

也可放 MON KV：

```bash
ceph config-key set smb/config/tango/config.json -i /tmp/config.json
```

使用推荐 URI 命名时 cephadm 自动生成最小 CephX 访问。HTTP(S) 配置的可用性、TLS 与鉴权由管理员负责。域模式高度依赖 DNS；宿主机或 `custom_dns` 必须可解析且可达 AD。

CTDB public address 必须映射到实际 destination interface/network。应用前执行 `cephadm list-networks`，以 cephadm 看到的接口到 CIDR 映射核对 `cluster_public_addrs`；错误映射会让 VIP 不漂移或漂到不可达接口。remote-control TLS 材料按 secret 管理，应用后分别验证控制面完整链、双向信任、证书轮换和 SMB 445 数据面。

```mermaid
flowchart TD
  CFG[SMB 配置] --> R[RADOS .smb namespace]
  CFG --> K[MON config-key]
  CFG --> H[HTTP HTTPS]
  R & K & H --> SC[Samba container]
  J[join_sources] --> SC
  U[user_sources] --> SC
  SC --> C[SMB 客户端]
  SC --> F[CephFS]
```

CephFS-backed share 使用 proxied provider 时，SMB module 会在 features 加 `cephfs-proxy`，cephadm 为每个 Samba 实例部署 sidecar。要分别诊断 Samba、proxy socket 和 CephFS。当前同一主机不支持多个 SMB service，因为必须绑定 TCP 445；端口冲突是明确限制。

## 21. Monitoring 与集中日志

### 21.1 三种责任模式

1. cephadm 部署并配置，bootstrap 默认采用；
2. 企业已有 Prometheus/Grafana 时外部管理，推荐复用；
3. 完全跳过，此时 Dashboard 部分图表不可用。

```bash
ceph orch apply node-exporter
ceph orch apply alertmanager
ceph orch apply prometheus --placement 'count:2'
ceph orch apply grafana
```

```mermaid
flowchart LR
  CM[ceph-mgr prometheus module] --> P[Prometheus]
  CE[ceph-exporter] --> P
  NE[node-exporter] --> P
  P --> AM[Alertmanager]
  P --> G[Grafana]
  AM --> W[webhook/SNMP]
  D[daemon 与主机日志] --> PT[Promtail]
  PT --> L[Loki]
  L --> G
```

Loki/Promtail 不默认部署。集中日志提供统一时间线、实时查询、灵活保留和集中保护，但日志含敏感信息，仍需访问控制、容量、HA 和备份。

Prometheus 安全模型假设 HTTP endpoint 和日志可被不受信用户访问时，用户能读到全部指标元数据和调试信息；API 只读并不等于可以公开到业务网。

### 21.2 Secure monitoring stack

默认 cephadm monitoring 没有启用这些安全措施。开启：

```bash
ceph config set mgr mgr/cephadm/secure_monitoring_stack true
```

Cephadm 会在数分钟内重配置：Prometheus/Alertmanager 启用 TLS + basic auth，node-exporter 启用 TLS，Grafana datasource 需要认证并使用 TLS。Prometheus/Alertmanager 默认凭据是 `admin/admin`，必须立即轮换：

```bash
ceph orch prometheus set-credentials
ceph orch alertmanager set-credentials
ceph orch prometheus get-credentials
ceph orch alertmanager get-credentials
```

优先通过 JSON/安全输入传 secret，不把口令留在 shell history。

### 21.3 网络、端口、镜像与模板

所有 monitoring service 可由 YAML 设置 network/port；Grafana 默认用 HTTPS，只有显式 `protocol: http` 才降级：

```yaml
service_type: grafana
placement:
  count: 1
networks: [10.60.0.0/24]
spec:
  port: 4200
  protocol: https
```

没有显式覆盖时，ServiceSpec 使用以下监听端口：

| 服务 | 默认端口 | 专属控制字段 |
|---|---:|---|
| Prometheus | 9095 | `retention_time`、`retention_size`、`targets`、`only_bind_port_on_networks` |
| node-exporter | 9100 | 通用 network/port/placement |
| Alertmanager | 9093；集群监听另占 9094 | `user_data.webhook_urls`、`secure`、`only_bind_port_on_networks`；业务端口不得设为 9094 |
| Grafana | 3000 | `protocol`、`anonymous_access`、`initial_admin_password`、`only_bind_port_on_networks` |
| Loki | 3100 | 通用 network/port/placement |
| Promtail | 9080 | 通用 network/port/placement |

`only_bind_port_on_networks: true` 只有在顶层 `networks` 与每台目标主机接口确实匹配时才能启用。`targets` 用于显式外部 scrape/通知目标时，应验证 DNS、TLS、认证和失败超时；不能把“配置已保存”当作 target 已被采集。

可覆盖的 image 配置键为：`container_image_prometheus`、`container_image_grafana`、`container_image_alertmanager`、`container_image_node_exporter`、`container_image_loki`、`container_image_promtail`、`container_image_haproxy`、`container_image_keepalived`、`container_image_snmp_gateway`、`container_image_elasticsearch`、`container_image_jaeger_agent`、`container_image_jaeger_collector`、`container_image_jaeger_query`。默认 image 的运行时权威清单由 `cephadm list-images` 输出；源码定义位于 `src/python-common/ceph/cephadm/images.py`。设置自定义 image 后必须 redeploy 对应服务，并由管理员持续更新，否则 cephadm 不会自动替换该辅助组件。恢复默认：

```bash
ceph config rm mgr mgr/cephadm/container_image_prometheus
ceph orch redeploy prometheus
```

Cephadm 的 Jinja2 模板通过 `mgr/cephadm/services/...` config-key 覆盖。Tentacle 支持的完整键名是：

```text
services/alertmanager/alertmanager.yml
services/alertmanager/web.yml
services/grafana/ceph-dashboard.yml
services/grafana/grafana.ini
services/ingress/haproxy.cfg
services/ingress/keepalived.conf
services/iscsi/iscsi-gateway.cfg
services/mgmt-gateway/external_server.conf
services/mgmt-gateway/internal_server.conf
services/mgmt-gateway/nginx.conf
services/nfs/ganesha.conf
services/node-exporter/web.yml
services/nvmeof/ceph-nvmeof.conf
services/oauth2-proxy/oauth2-proxy.conf
services/prometheus/prometheus.yml
services/prometheus/web.yml
services/loki.yml
services/promtail.yml
```

对应官方模板在 `src/pybind/mgr/cephadm/templates` 下使用相同路径并追加 `.j2`。例：

```bash
ceph config-key set mgr/cephadm/services/prometheus/prometheus.yml \
  -i "$PWD/prometheus.yml.j2"
ceph orch reconfig prometheus

ceph config-key set \
  mgr/cephadm/services/prometheus/alerting/custom_alerts.yml \
  -i "$PWD/custom_alerts.yml"
```

`-i` 必须用绝对路径。自定义模板跨 Ceph 升级保留，也因此不会自动得到官方模板新字段；每次升级都要人工 diff/migrate。

### 21.4 外部 Prometheus 与 service discovery

```bash
ceph mgr module enable prometheus
ceph orch sd dump cert
```

MGR metrics 默认在每个 MGR 主机的 9283。Cephadm HTTP service discovery 在 `https://<mgr-ip>:8765/sd/`，端口由 `service_discovery_port` 控制，返回 Prometheus `http_sd_config`。外部 Prometheus 可按服务查询 `/sd/prometheus/sd-config?service=ceph-exporter`；使用 `sd dump cert` 的根证书验证服务。

### 21.5 Retention、Grafana 与 Alertmanager

```yaml
service_type: prometheus
placement:
  count: 2
spec:
  retention_time: 1y
  retention_size: 1TB
```

时间默认 15d，支持 y/w/d/h/m/s；size 默认 0 即不限制，支持 B 到 EB。两者同时设置时先触达者生效。更新已有 spec 后执行 `ceph orch redeploy prometheus`。

浏览器与集群 DNS 域不同可固定 Grafana frontend URL：

```bash
ceph dashboard set-grafana-frontend-api-url https://grafana.example.com
```

该值 cephadm 不再自动修改。Grafana 无自定义证书时为每主机生成自签证书；传统 config-key 路径为：

```bash
ceph config-key set mgr/cephadm/<hostname>/grafana_key -i "$PWD/key.pem"
ceph config-key set mgr/cephadm/<hostname>/grafana_crt -i "$PWD/cert.pem"
ceph orch reconfig grafana
```

默认不会创建初始 admin，但允许匿名 viewer。关闭匿名必须同时设置初始密码，否则 cephadm 拒绝不可登录配置：

```yaml
service_type: grafana
spec:
  anonymous_access: false
  initial_admin_password: <secret>
```

Alertmanager 可添加 webhook；`secure: true` 才验证证书，默认 false。变更后 `ceph orch reconfig alertmanager`。

### 21.6 禁用与数据边界

```bash
ceph orch rm grafana
ceph orch rm prometheus --force
ceph orch rm node-exporter
ceph orch rm alertmanager
ceph mgr module disable prometheus
```

Prometheus 的 `--force` 会删除已采集指标。RBD image 监控因性能开销默认关闭；未启用时 Grafana 对应面板空白不是采集故障。

## 22. Management Gateway 与 OAuth2 Proxy

### 22.1 Mgmt-gateway

Mgmt-gateway 自 Squid 起以 nginx 为 Dashboard、Prometheus、Grafana、Alertmanager 等提供统一 HTTPS 入口。部署后 cephadm 会重配置后端，监控服务不再允许直接外部访问；入口能跟随 active MGR 并在多个监控实例间选健康后端。

```yaml
service_type: mgmt-gateway
placement:
  label: mgmt
spec:
  port: 5000
  ssl: true
  enable_auth: true
  virtual_ip: 10.60.0.100
  ssl_protocols: [TLSv1.2, TLSv1.3]
  ssl_ciphers: [<approved-cipher>]
  ssl_prefer_server_ciphers: "on"
  ssl_session_tickets: "off"
  ssl_session_timeout: 10m
  ssl_session_cache: "shared:SSL:10m"
  server_tokens: "off"
  ssl_stapling: "on"
  ssl_stapling_verify: "on"
  enable_health_check_endpoint: true
  ssl_cert: |
    -----BEGIN CERTIFICATE-----
    ...
  ssl_key: |
    -----BEGIN PRIVATE KEY-----
    ...
```

修改 TLS 1.2 cipher 需要按当前安全基线审核；TLS 1.3 自带安全 cipher 集，盲目覆盖可能失去前向保密或重新启用弱算法。

端口只接受 1-65535，`ssl_protocols` 只接受 `TLSv1.2`、`TLSv1.3`。`ssl_prefer_server_ciphers`、`ssl_session_tickets`、`ssl_stapling`、`ssl_stapling_verify` 是字符串开关，只接受 `on|off`，不是 YAML boolean；`server_tokens` 接受 `on|off|build|string`。session timeout 是数字加 `s/m/h/d`。`ssl_session_cache` 接受 `off`、`none`、`builtin[:size]` 或 `shared:name:size`。启用 stapling 前必须验证 issuer chain、OCSP 可达性和故障策略，否则会把证书增强项变成入口故障点。

Mgmt-gateway 自身 HA 要部署多个实例并配置 keepalive-only ingress，二者 `virtual_ip` 必须完全相同：

```yaml
service_type: ingress
service_id: ingress-mgmt-gw
placement:
  label: mgmt
spec:
  virtual_ip: 10.60.0.100
  backend_service: mgmt-gateway
  keepalive_only: true
```

```mermaid
flowchart LR
  U[管理员浏览器] --> VIP[管理 VIP]
  VIP --> K[Keepalived]
  K --> MG1[mgmt-gateway 1]
  K -.failover.-> MG2[mgmt-gateway 2]
  MG1 & MG2 --> MGR[active MGR Dashboard]
  MG1 & MG2 --> P[Prometheus replicas]
  MG1 & MG2 --> A[Alertmanager replicas]
  MG1 & MG2 --> G[Grafana replicas]
```

默认 nginx image 为 `quay.io/ceph/nginx:sclorg-nginx-126`，由 `mgr/cephadm/container_image_nginx` 覆盖；已有实例必须 redeploy。限制是所有被代理应用端口不得冲突。

### 22.2 OAuth2 Proxy

先启用 mgmt-gateway 的 auth，再部署：

```yaml
service_type: oauth2-proxy
service_id: auth-proxy
placement:
  label: mgmt
spec:
  https_address: 0.0.0.0:4180
  provider_display_name: Corporate OIDC
  client_id: <client-id>
  oidc_issuer_url: https://idp.example.com/realms/ceph
  redirect_url: https://ceph-mgmt.example.com/oauth2/callback
  allowlist_domains:
    - .example.com
  client_secret: <secret>
  cookie_secret: <16-24-or-32-byte-secret>
  ssl_cert: |
    -----BEGIN CERTIFICATE-----
    ...
  ssl_key: |
    -----BEGIN PRIVATE KEY-----
    ...
```

> **版本差异**：Tentacle 的 `oauth2-proxy.rst` 示例仍写 `ssl_certificate`、`ssl_certificate_key`，但同版本 `OAuth2ProxySpec` 的可执行 schema 是 `ssl_cert`、`ssl_key`。有效配置必须使用 `ssl_cert` 与 `ssl_key`。

`provider_display_name`、`client_id`、`client_secret` 必须是非空字符串；issuer 和显式 redirect 必须是同时具有 scheme 与 authority 的 URL。`https_address` 格式为 `host:port`。`cookie_secret` 可以是 URL-safe base64 或普通字符串，但解码后的真实长度必须为 16、24 或 32 bytes，以满足 AES key 长度；不要把占位字符串 `<secret>` 原样投入生产。`allowlist_domains` 限制登录或退出后的安全重定向域，必须使用最小集合，避免 open redirect。

OAuth2 service 可作为无状态实例由 mgmt-gateway round-robin；IDP 自身 HA 属于外部责任。官方同一页的 HA 描述称可多实例，而 Limitations 又称 oauth2-proxy 自身 HA 不受支持。生产按保守边界处理：多实例可做进程冗余，但不宣称端到端 HA，必须实测 session/cookie、redirect 和 IDP 故障。

```mermaid
sequenceDiagram
  participant User as 用户
  participant Gateway as mgmt-gateway
  participant Proxy as oauth2-proxy
  participant IdP as 外部 IdP
  participant App as Ceph 应用
  User->>Gateway: 请求应用
  Gateway->>Proxy: auth_request
  Proxy-->>User: 重定向 IdP
  User->>IdP: 登录
  IdP-->>User: OIDC callback
  User->>Proxy: code 与 state
  Proxy-->>Gateway: 认证通过
  Gateway->>App: 代理受信请求
  App-->>User: 应用响应
```

验证 issuer discovery、client secret、redirect URI、cookie secret 长度/轮换、TLS chain、allowlist 和 claims。部署成功后 cephadm 自动 redeploy mgmt-gateway 接入认证。Image 由 `container_image_oauth2_proxy` 控制，修改后执行 `ceph orch redeploy oauth2-proxy`。

## 23. SNMP Gateway、Tracing 与自定义容器

### 23.1 SNMP Gateway

SNMP gateway 把带 OID label 的 Alertmanager alert 转为 trap/notification：V1 不支持；V2c 支持；V3 支持 authNoPriv 和 authPriv。

| 模式 | 凭据 | 隐私 |
|---|---|---|
| V2c | community | 无认证加密 |
| V3 authNoPriv | username/password，auth MD5 或 SHA，默认 SHA | 认证，无加密 |
| V3 authPriv | 再加 priv password，DES 或 AES | 认证 + 加密 |

默认部署一个实例。多个实例会让 NMS 对同一事件收到重复通知，除非接收端明确去重。

```yaml
service_type: snmp-gateway
placement:
  count: 1
spec:
  credentials:
    snmp_v3_auth_username: ceph
    snmp_v3_auth_password: <secret>
    snmp_v3_priv_password: <secret>
  engine_id: 8000C53F<fsid-without-dashes>
  port: 9464
  snmp_destination: nms.example.com:162
  snmp_version: V3
  auth_protocol: SHA
  privacy_protocol: AES
```

CLI 部署必须 `-i` 传 credentials 文件，secret 不接受普通参数。V3 engine ID 必须唯一且为 hex；建议 `8000C53F` 加无横线 FSID。凭据在主机以 root-only env file 交给 snmp_notifier。Alertmanager 自动把有 OID label 的告警路由到 gateway；NMS 还需导入官方 `CEPH-MIB.txt`。验收以 NMS 收到测试 trap 且 OID/health code 正确为准。

### 23.2 Jaeger tracing

```mermaid
flowchart LR
  CD[Ceph daemons] --> JA[Jaeger agents]
  JA --> JC[Jaeger collectors]
  JC --> ES[Elasticsearch 6 默认]
  JQ[Jaeger query] --> ES
  U[运维查询] --> JQ
```

```bash
# 部署 agent/collector/query 和新 Elasticsearch
ceph orch apply jaeger

# 使用既有 Elasticsearch/既有 query，只部署 agent 和 collector
ceph orch apply jaeger --without-query --es_nodes=ip:port,ip:port
```

规划 trace 采样率、Elasticsearch 容量、保留和访问控制。长期开高采样会增加 daemon、网络和后端开销；tracing running 不证明 trace 已完整串联，需按 trace ID 验证产生、收集、索引和查询。

### 23.3 Custom container

```yaml
service_type: container
service_id: app
placement:
  label: app
spec:
  image: registry.example.com/app/app:<digest-pin>
  entrypoint: /usr/bin/app
  uid: 1000
  gid: 1000
  privileged: false
  args: ["--net=host", "--cpus=2"]
  ports: [8080, 8443]
  envs: ["PORT=8080"]
  dirs: [CONFIG_DIR, DATA_DIR]
  volume_mounts:
    CONFIG_DIR: /etc/app
    DATA_DIR: /var/lib/app
  bind_mounts:
    - [type=bind, source=lib/modules, destination=/lib/modules, ro=true]
  files:
    CONFIG_DIR/app.conf:
      - mode=production
  init_containers:
    - image: registry.example.com/app/init:<digest-pin>
      entrypoint: /usr/bin/prepare
      entrypoint_args: ["/var/lib/app"]
      volume_mounts:
        DATA_DIR: /var/lib/app
      envs: ["MODE=verify"]
      privileged: false
```

相对 mount source、dirs 和 files 都位于 `/var/lib/ceph/<fsid>/<daemon-name>`。文件父目录须由 `dirs` 创建；字符串内容要双引号并用 `\n`，多行可用字符串列表。

Init container 可独立指定 image、entrypoint、entrypoint_args、volume_mounts、envs、privileged；省略 image/mount/privileged 时继承主容器。它们按顺序在主进程前执行，总运行时间不能超过 200 秒，否则 service 启动失败。

`service_id` 与 `image` 必填。顶层 `privileged` 默认 false，只有经过主机安全审批且无法用 capability/设备映射满足时才开启。`args` 是容器 runtime 参数，与通用 `extra_container_args` 互斥；`files` 与 `custom_configs` 同样互斥，因为两组字段承担相同文件注入职责。Secret 不得放进 `args` 或 `envs`；优先用权限受控的挂载文件和外部 secret 生命周期。

```mermaid
flowchart LR
  I1[init container 1] --> I2[init container 2]
  I2 --> M[主容器]
  V[共享 volume mounts] --> I1
  V --> I2
  V --> M
  T[总计 200 秒上限] --> I1
```

Cephadm 只保证容器期望状态，不理解该应用的 quorum、schema migration、业务健康、备份或滚动顺序。Secret 不应明文写 `envs`/spec；自定义容器必须另有产品 owner 和验收 runbook。

## 24. Certificate Manager

### 24.1 所有权决定续期责任

CertMgr 是 cephadm 自签证书的 root CA 和生命周期控制器：

- cephadm 自签证书会在阈值内自动生成新证书，并触发服务 reload/redeploy；
- 用户提供证书只被监测，不会自动续签；临期、过期或无效产生 `CEPHADM_CERT_ERROR`，管理员负责替换；
- 私钥读取和导出属于高敏感操作，不能进入普通终端录屏、日志或工单。

```mermaid
stateDiagram-v2
  [*] --> Valid
  Valid --> RenewalWindow: 距过期进入阈值
  RenewalWindow --> Rotated: cephadm 自签且自动轮换开启
  Rotated --> Valid: 服务 reload/redeploy 成功
  RenewalWindow --> UserAction: 用户提供证书
  UserAction --> Valid: 管理员上传新 pair
  RenewalWindow --> Error: 未及时处理
  Error --> UserAction: CEPHADM_CERT_ERROR
```

### 24.2 参数、默认值和范围

| 配置 | 默认值 | 官方范围/语义 |
|---|---:|---|
| `mgr/cephadm/certificate_automated_rotation_enabled` | `true` | 是否自动轮换 cephadm 自签证书 |
| `mgr/cephadm/certificate_duration_days` | `3*365` | 最少 90，最多 `10*365` 天 |
| `mgr/cephadm/certificate_renewal_threshold_days` | 30 | 10-90 天 |
| `mgr/cephadm/certificate_check_period` | 1 天 | 0-30；0 禁用检查 |

禁用检查不会延长证书，只会失去提前告警。把 check period 设 0 必须有外部证书监控覆盖。

### 24.3 Scope

| Scope | 唯一键 | 示例 |
|---|---|---|
| global | certificate/key 名 | 所有实例共享的 mgmt-gateway 材料 |
| host | 名 + `--hostname` | 每台 Grafana/节点证书 |
| service | 名 + `--service_name` | 某个 RGW service 的证书 |

Host/service scope 的 get/set/rm 都必须携带 selector；否则可能操作错误实体或被拒绝。

CertMgr 初始全局对象至少包含 `cephadm_root_ca_cert` 和 `cephadm_root_ca_key`；其余证书、私钥和 entity 由已加载的 service handler 按 Tentacle 版本动态注册。不要从旧版本静态清单猜名称，运行时先建立以下三方对账：

```bash
ceph orch certmgr entity ls
ceph orch certmgr cert ls --show-details
ceph orch certmgr key ls
```

对账表必须记录 entity、对象名、scope、service/hostname selector、签发者、SAN、到期日、是否 user-provided 和消费它的 daemon。`cert ls`/`key ls` 是当前集群可操作名称的权威集合；源码 `known_certs`/`known_keys` 是版本能力集合，二者差异意味着服务尚未注册、配置尚未加载或升级迁移未完成。

### 24.4 运维命令

```bash
ceph orch certmgr reload
ceph orch certmgr entity ls
ceph orch certmgr cert ls --show-details
ceph orch certmgr key ls
ceph orch certmgr cert check

ceph orch certmgr cert get <certificate_name> \
  --service_name <service>
ceph orch certmgr key get <key_name> \
  --service_name <service>

ceph orch certmgr cert-key set <entity> \
  --service_name <service> -i <pair.pem> [--force]
ceph orch certmgr cert set <certificate_name> \
  --hostname <host> -i <cert.pem>
ceph orch certmgr key set <key_name> \
  --hostname <host> -i <key.pem>

ceph orch certmgr cert rm <certificate_name> --service_name <service>
ceph orch certmgr key rm <key_name> --service_name <service>
ceph orch certmgr generate-certificates <module>
```

官方 RST 的生成命令处存在 `cehp` 拼写错误；有效 CLI 为 `ceph`。上传 pair 前在离线环境验证 PEM、私钥匹配、issuer、chain、SAN、KeyUsage 和有效期。替换流程：

```mermaid
flowchart TD
  N[生成/取得新证书] --> O[离线验证 key/cert/chain/SAN]
  O --> B[备份当前 entity 与 scope]
  B --> S[cert-key set]
  S --> C[cert check]
  C --> R[reload 或对应服务 redeploy]
  R --> E[TLS 客户端握手与业务验收]
  E --> K[确认后销毁临时私钥副本]
```

删除用户材料可能回落自签，也可能让服务无法启动，取决于 entity。先在同类型非关键实例演练，不把 `cert rm` 当作回滚手段。

## 25. 客户端配置与 keyring 分发

客户端通常只需 `ceph-common`，它提供 `ceph`、`rados`、`mount.ceph`、`rbd` 等。最小配置：

```bash
ceph config generate-minimal-conf > /etc/ceph/ceph.conf
ceph auth get-or-create client.fs > /etc/ceph/ceph.client.fs.keyring
```

不要把 `client.admin` 分给应用。为每个应用创建最小 caps 的 entity，再让 cephadm 按 placement 管理文件：

```bash
ceph auth get-or-create-key client.rbd \
  mon 'profile rbd' mgr 'profile rbd' \
  osd 'profile rbd pool=my_rbd_pool'
ceph orch client-keyring set client.app 'label:app' \
  --mode 0600 \
  --owner 1000:1000 \
  --path /etc/ceph/ceph.client.app.keyring
```

默认 mode `0600`、owner `root:root`。Placement 不再匹配时，cephadm 会从旧主机移除受管 keyring。路径或 owner 变化必须与消费进程一致，防止轮换后应用读不到。

```mermaid
flowchart LR
  AUTH[CephX entity 与最小 caps] --> CK[client-keyring spec]
  CK --> PL[placement]
  PL --> H1[应用主机 1 keyring]
  PL --> H2[应用主机 2 keyring]
  CFG[minimal ceph.conf] --> H1
  CFG --> H2
  PL -.不再匹配.-> RM[从旧主机删除受管文件]
```

Cephadm 通常也向 keyring 主机分发 `/etc/ceph/ceph.conf`。`bare_config` 标签用于只分发基础 config 的场景。验收不能止于文件存在：使用目标 UID、目标 keyring 和最小 config 发起真实客户端连接并证明 caps 既足够又不能越权。

Cephadm 还在 `/var/lib/ceph/<fsid>/config/` 保存自身使用的 config/keyring 副本；Ceph daemon 仍使用 `/etc/ceph/`。管理命令完整闭环为：

```bash
ceph orch client-keyring ls
ceph orch client-keyring set client.rbd label:rbd-client \
  --owner 107:107 --mode 640
ceph orch client-keyring set client.foo label:foo \
  --owner 0:0 --no-ceph-conf
ceph orch client-keyring rm client.foo
```

`client-keyring rm` 会删除此前写到集群主机的该 entity 文件，不删除 CephX entity 本身。要向无 keyring 主机分发 config：

```bash
ceph config set mgr mgr/cephadm/manage_etc_ceph_ceph_conf true
ceph config set mgr \
  mgr/cephadm/manage_etc_ceph_ceph_conf_hosts label:bare_config
```

默认 keyring path 是 `/etc/ceph/client.{entity}.keyring`，自定义路径可能覆盖已有文件；变更 placement 后旧主机副本会被删除。

## 26. 日常 daemon 操作与日志

### 26.1 Stop、restart、reconfig、redeploy

| 动作 | 做什么 | 适用 |
|---|---|---|
| `daemon stop/start/restart` | 控制现有 unit/container | 短期实例操作 |
| `orch stop/start/restart <service>` | 对 service 全部实例操作 | 必须先评估 quorum/failure domain |
| `daemon reconfig` | 重生成配置并让实例使用 | 配置变化，无需换 image/container 布局 |
| `daemon redeploy` | 停止并重建容器，可指定 image | image、挂载、启动参数变化 |
| `daemon rotate-key` | 更新 daemon CephX key | MDS/OSD/MGR 无需重启，其他类型需重启 |

不要对 OSD service 整体 restart。对 MON/MGR/MDS/RGW 的全 service 动作同样需要明确 HA 和客户端重试模型。

### 26.2 Ceph daemon 日志

Quincy 起 daemon 默认向 journald 输出；查看：

```bash
journalctl -u ceph-<fsid>@<daemon>.service
cephadm logs --name <daemon>
```

可切换文件日志：

```bash
ceph config set global log_to_file true
ceph config set global mon_cluster_log_to_file true
ceph config set global log_to_stderr false
ceph config set global mon_cluster_log_to_stderr false
ceph config set global log_to_journald false
ceph config set global mon_cluster_log_to_journald false
```

文件通常位于 `/var/log/ceph/<fsid>/`，cephadm 在各主机管理 logrotate。若选择文件日志，官方建议关闭 journald 避免双写容量。日志级别升高前评估磁盘和性能，并设置恢复时间。

文件日志的轮换入口是 `/etc/logrotate.d/ceph.<fsid>`。修改前保存原文件，按日志增长率、事故保留期和磁盘告警设计 rotation，升级后复核 cephadm 是否重新生成配置。cephadm 自身在既有集群的持久日志目的地由下列配置控制，可取单值或逗号组合：

```bash
ceph config set mgr mgr/cephadm/cephadm_log_destination syslog
ceph config set mgr mgr/cephadm/cephadm_log_destination file,syslog
ceph config set mgr mgr/cephadm/cephadm_log_destination file
```

### 26.3 cephadm 自身日志与集群事件

Cephadm 可向 stderr、syslog、journald 或 file 输出。全局 `cephadm --log-dest=file|syslog` 控制当前执行；bootstrap 的 `--log-to-file` 是集群 daemon 行为，二者不能混淆。

```bash
ceph -W cephadm
ceph -W cephadm --watch-debug
ceph log last cephadm
```

Debug stream 可能含主机命令、路径和敏感上下文，只在受控窗口使用并按敏感日志处理。

### 26.4 数据目录、磁盘容量与受限 sudo

| 路径 | 内容 |
|---|---|
| `/var/log/ceph/<fsid>` | 仅启用文件日志时存在的集群日志 |
| `/var/lib/ceph/<fsid>` | 除日志外的集群 daemon 数据 |
| `/var/lib/ceph/<fsid>/<daemon-name>` | 单 daemon 数据与生成文件 |
| `/var/lib/ceph/<fsid>/crash` | crash reports |
| `/var/lib/ceph/<fsid>/removed` | cephadm 删除的 MON、Prometheus 等有状态 daemon 旧目录 |

MON 与 Prometheus 可能大量使用 `/var/lib/ceph`，官方建议把它放到独立磁盘、分区或 LV，避免填满 root filesystem。`removed` 不是永久备份区，也不能未经确认自动清空；先证明对应 daemon 已恢复且目录不再是唯一副本。

非 root cephadm 用户可限制 passwordless sudo，但官方命令集合会跨版本变化。当前至少涉及 `chmod`、`chown`、`ls`、`mkdir`、`mv`、`rm`、`sysctl`、`touch`、`true`、`which`，以及 `/usr/bin/cephadm` 或 `which python3` 得到的 Python。升级前先扩展目标版本所需 allowlist，否则升级中途会失败；用 `visudo` 管理，不手改损坏 sudoers。

```mermaid
flowchart TD
  E[Service/daemon events] --> T[第一失败时间点]
  C[cephadm cluster log] --> T
  J[target host journald] --> T
  R[container runtime log] --> T
  T --> D[按同一 timestamp 重建因果链]
```

## 27. Cephadm 健康检查

| Health code | 含义 | 首要动作 |
|---|---|---|
| `CEPHADM_PAUSED` | reconcile 被暂停 | 确认暂停原因后 `resume`，不要只 mute |
| `CEPHADM_STRAY_HOST` | daemon 所在 host 不在 inventory | 纳管主机或移除其 daemon |
| `CEPHADM_STRAY_DAEMON` | daemon 不属于已知受管 service | 建立等价 spec/adopt 或安全移除 |
| `CEPHADM_HOST_CHECK_FAILED` | SSH、runtime、时间同步等失败 | `check-host` 与目标主机日志 |
| `CEPHADM_CHECK_KERNEL_LSM` | SELinux/AppArmor 不一致 | 对齐策略，不能直接全禁用 |
| `CEPHADM_CHECK_SUBSCRIPTION` | OS subscription 不一致 | 修复软件源/订阅状态 |
| `CEPHADM_CHECK_PUBLIC_MEMBERSHIP` | 缺少 public network 接口 | 核验地址、CIDR 与网络声明 |
| `CEPHADM_CHECK_MTU` | OSD 网络 MTU 不一致 | 全路径 MTU 探测 |
| `CEPHADM_CHECK_LINKSPEED` | OSD 网络速率不一致 | 检查 NIC/交换机协商与故障 |
| `CEPHADM_CHECK_NETWORK_MISSING` | 定义网络在主机不存在 | 修复网络或 placement |
| `CEPHADM_CHECK_CEPH_RELEASE` | 非升级期间 release 不一致 | 找出漂移 daemon 并 redeploy |
| `CEPHADM_CHECK_KERNEL_VERSION` | kernel major.minor 不一致 | 评估兼容后分批统一 |
| `CEPHADM_INVALID_CONFIG_OPTION` | spec 中配置键无效 | 修正 spec 并重应用 |
| `CEPHADM_FAILED_SET_OPTION` | `ceph config set` 失败 | 查具体 option、权限和作用域 |
| `CEPHADM_CERT_ERROR` | 证书无效/临期/过期 | 按 owner 更换或修复自动轮换 |

检查属于早期风险信号，不是所有异常都要求立刻把所有主机改成完全一致。例如计划内 kernel 滚动期间会短暂不同；应记录窗口和完成条件，而非永久 mute。

官方提供 `mgr/cephadm/warn_on_stray_hosts`、`warn_on_stray_daemons` 和 `warn_on_failed_host_check` 开关，但关闭它们只隐藏告警，不修复主机、daemon 或 SSH/runtime 故障。只有在外部监控已经覆盖、例外有到期时间和 owner 时才能临时关闭；窗口结束必须恢复 true 并确认 `ceph health detail` 无残留。

### 27.1 配置检查控制面

Cephadm 在每次 host scan 后比较 OS、磁盘与网络事实。Operations 类 health check 在 module 启用时始终运行；cluster configuration checks 是可选的：

```bash
ceph config set mgr mgr/cephadm/config_checks_enabled true
ceph cephadm config-check status
ceph cephadm config-check ls
ceph cephadm config-check disable kernel_security
ceph cephadm config-check enable kernel_security
```

检查以多数主机状态作为 MTU、link speed、kernel/LSM 等异常基准；“多数”不自动代表正确。先与设计基线对账，再决定修复异常节点还是修正全体。`CEPHADM_CHECK_CEPH_RELEASE` 在正式 upgrade 进行时会跳过，非升级窗口发现混合 release 才告警。

## 28. 升级：控制面顺序与业务批次

### 28.1 升级前门禁

1. 阅读起点到目标 release 的 release notes 与兼容矩阵；不跨越未支持版本。
2. 集群健康、MON quorum、MGR standby、容量、PG 和业务探针均达标。
3. 固定目标 image tag/digest，所有主机预检 registry、认证和架构。
4. 保存 spec、config dump、versions、CRUSH、auth、证书和恢复材料。
5. 暂停其他容量、换盘、CRUSH、pool 和网络变更。
6. 明确 MDS 策略和可接受中断。

PG autoscaler 在版本混合期可能触发无关 PG 变化，官方建议升级期间临时设置所有 pool `noautoscale`，完成后恢复每个 pool 原模式，而不是一律开启：

```bash
ceph osd pool set noautoscale
# 完成并验证后
ceph osd pool unset noautoscale
```

### 28.2 检查、启动和观察

```bash
ceph orch upgrade check <target-image>
# 官方 release 版本入口：由 container_image_base 与 v<version> 组成目标 image
ceph orch upgrade start --ceph-version <version>
# 私有仓库、digest pin 或非标准构建使用完整 image
ceph orch upgrade start --image <target-image>
ceph orch upgrade status
ceph -W cephadm
ceph progress
ceph versions
```

`--ceph-version X.Y.Z` 默认把 `mgr/cephadm/container_image_base`（默认 `docker.io/ceph/ceph`）与 `vX.Y.Z` 组合。企业私库、开发构建或 digest pin 不应伪装成 version，直接使用 `--image <complete-reference>`。启动前保存 `container_image_base` 当前值并在每台主机验证完全相同的 manifest digest。

升级顺序固定为：

```text
mgr -> mon -> crash -> osd -> mds -> rgw -> rbd-mirror
    -> cephfs-mirror -> iscsi -> nfs
```

```mermaid
flowchart LR
  MGR[mgr] --> MON[mon]
  MON --> CR[crash]
  CR --> OSD[osd]
  OSD --> MDS[mds]
  MDS --> RGW[rgw]
  RGW --> RM[rbd-mirror]
  RM --> FM[cephfs-mirror]
  FM --> IS[iSCSI]
  IS --> NFS[nfs]
```

这是依赖顺序，不应通过 stagger 参数打乱。Host offline 会暂停升级，不会被自动跳过；修复主机或显式调整计划后再继续。

### 28.3 CephFS 策略

默认升级 MDS 时 cephadm 会把 `max_mds` 降为 1，影响多 active rank 文件系统的吞吐和 namespace 并发。`mgr/orchestrator/fail_fs=true` 则先 fail filesystem、升级 MDS、再恢复，是明确的业务中断策略。两者都要在变更前选定并压测：

```mermaid
flowchart TD
  U[MDS 升级] --> F{fail_fs=true?}
  F -->|是| OFF[Fail FS，业务中断]
  OFF --> ALL[升级全部 MDS]
  ALL --> ON[恢复 FS]
  F -->|否| ONE[临时 max_mds=1]
  ONE --> ROLL[滚动升级]
  ROLL --> RESTORE[恢复原 max_mds]
```

### 28.4 Staggered upgrade

可按 `daemon_types`、`services`、`hosts` 与 `limit` 缩小批次。限制：

- `services` 与 `daemon_types` 互斥；
- 一次指定的 services 必须属于同一 daemon type；
- 仍受固定类型顺序约束；
- 旧 release 不支持 stagger 时，先普通升级 standby MGR，failover，再完成 MGR，使支持新参数的 MGR 接管。

带限制参数的 `upgrade start` 会先校验选项，期间可能拉取目标镜像，因此命令返回较慢不等于卡死；同时观察 cephadm event、registry 和目标主机 runtime。MGR 批次完成后，Prometheus、node-exporter 等 monitoring daemon 会被刷新；即使其组件版本不变，也可能发生 redeploy，必须把监控短暂抖动纳入窗口。

从不支持 `redeploy --image` 的早期 cephadm 进入 stagger 能力时，先确认至少两个 MGR，再逐个处理 standby：

```bash
# 新一些的旧版本
ceph orch daemon redeploy mgr.<standby-id> --image <target-image>

# 极早期版本没有 --image 时
ceph config set mgr container_image <target-image>
ceph orch daemon redeploy mgr.<standby-id>

ceph mgr fail
ceph orch upgrade start --image <target-image> --daemon-types mgr
```

每一步都要验证新 active MGR 已运行目标版本，禁止在没有 standby 时强制 failover。

每批验收：目标实例版本、daemon health、PG、quorum、业务延迟和读写，全部达标才进入下一批。

### 28.5 停止不是回滚

```bash
ceph orch upgrade stop
```

它只停止调度新的升级，不会降级已经更新的实例。真正 downgrade 必须确认官方支持混合版本/降级、固定旧 image、按 daemon 依赖计划，并考虑数据格式不可逆变化。没有官方支持就选择修复前进，而非手工强降。

### 28.6 典型错误

| 错误 | 根因方向 | 处置 |
|---|---|---|
| `UPGRADE_NO_STANDBY_MGR` | 无可接管 standby | 先恢复第二 MGR 并验证 failover |
| `UPGRADE_FAILED_PULL` | registry、认证、DNS、架构、digest | 在失败主机验证同一 image，修复制品链 |
| `ENOENT Module not found` | MGR config-key 可能含非法 JSON | 查相关 config-key，备份后修正 |
| 版本长时间混合 | daemon 离线、placement/host 阻塞 | `upgrade status` + orch events 定位首个阻塞 |

非 Ceph 辅助服务不会必然随核心版本一起更新，使用：

```bash
ceph orch update service <service-type> <image>
```

分别核验 Prometheus、Grafana、NFS、iSCSI 等 image digest 和兼容性。禁止用 `latest` 绕过失败检查。

核心 daemon 全部完成并不代表宿主机工具已完成。升级验收后，把各管理主机的 `cephadm` 包更新到与新 release 兼容的版本；不使用 `cephadm shell`、依赖宿主机 CLI 的环境同时更新 `ceph-common`。最后重新执行 `cephadm version`、`ceph -v`、`ceph versions`，确认本地 CLI、编排器和全部 daemon 没有意外版本漂移。

```mermaid
stateDiagram-v2
  [*] --> Checked
  Checked --> Running: upgrade start
  Running --> Paused: host offline 或前置失败
  Paused --> Running: 修复并继续
  Running --> Stopped: upgrade stop
  Stopped --> Running: 重新 start 同目标
  Running --> Completed: 全部 daemon 达目标
  Completed --> [*]
```

## 29. 故障诊断：先找控制层，再找实例层

### 29.1 通用顺序

```mermaid
flowchart TD
  A[服务不收敛] --> B{orchestrator paused 或 service unmanaged?}
  B -->|是| C[确认原因后 resume/set-managed]
  B -->|否| D[service/daemon events]
  D --> E[cephadm MGR 日志]
  E --> F[SSH authorized_keys sudo check-host]
  F --> G[systemd unit 与 container inspect/logs]
  G --> H[image 端口 证书 配置 数据目录]
  H --> I[admin socket 与业务健康]
```

```bash
ceph orch status
ceph orch ls --refresh --format yaml
ceph orch ps --refresh --format yaml
ceph -W cephadm --watch-debug
ceph health detail
```

`ceph orch pause` 停止大多数后台 reconcile，但仍周期性刷新 host、daemon 和 device inventory；现有 daemon 继续运行，`resume` 恢复。完全禁用编排器的精确命令是：

```bash
ceph orch set backend ''
ceph mgr module disable cephadm
```

它会令全部 `ceph orch ...` 命令不可用，但既有容器和 systemd unit 继续运行和随主机启动。恢复时先 `ceph mgr module enable cephadm`，再 `ceph orch set backend cephadm`，导出/审查 spec 后才恢复变更。完全 disable 的影响远大于 pause，不能作为普通暂停替代；暂停期间所有漂移都会积累。

### 29.2 主机与 SSH

```bash
ceph cephadm check-host host02
ceph orch host ls --detail
ceph cephadm get-ssh-config
ceph cephadm get-pub-key
```

从 active MGR 所在主机/容器语境复现 SSH，检查 hostname、known hosts、key、signed cert、sudo、Python、runtime、时间。管理员工作站能登录不能证明 MGR 能登录。

下面的闭环导出的是 cephadm 实际使用的身份和配置，不用管理员自己的 key 替代。调试目录必须在受控管理节点创建，默认拒绝其他用户读取：

```bash
umask 077
ssh_debug_dir="$(mktemp -d /tmp/cephadm-ssh.XXXXXX)"

cephadm shell -- ceph config-key get mgr/cephadm/ssh_identity_key \
  > "${ssh_debug_dir}/cephadm_private_key"
chmod 0600 "${ssh_debug_dir}/cephadm_private_key"
cephadm shell -- ceph cephadm get-ssh-config \
  > "${ssh_debug_dir}/ssh_config"
cephadm shell -- ceph cephadm get-pub-key \
  > "${ssh_debug_dir}/ceph.pub"

ssh -F "${ssh_debug_dir}/ssh_config" \
  -i "${ssh_debug_dir}/cephadm_private_key" \
  <cephadm-ssh-user>@<target-host> hostname
```

在目标主机用 cephadm 实际登录用户核验公钥是 `authorized_keys` 中的一整行，并核对目录/文件 owner、mode 和 sshd 策略；默认 root 模式对应 `/root/.ssh/authorized_keys`。若使用 SSH CA-signed certificate，普通 `get-pub-key`/`authorized_keys` 检查不是完整证据，还要验证 `ssh_identity_cert`、目标 sshd 的 `TrustedUserCAKeys`、principal 和有效期。若 config-key 中没有身份，先证明是 key 丢失而非读取了错误 FSID，再选择 `ceph cephadm generate-ssh-key` 或经审批的 `ceph cephadm set-ssh-key -i -`；两者会改变整个编排面的登录身份，必须把新公钥部署到所有目标主机并逐台 `check-host`，不能只修一台。

复现完成立即安全销毁导出的私钥和临时材料，例如对明确文件逐个执行 `shred -u`，再 `rmdir "${ssh_debug_dir}"`；不把私钥放进工单、聊天、普通日志或版本库。若底层文件系统/快照不保证覆盖擦除，应从一开始就在加密临时卷中操作，并按密钥泄露流程轮换集群 SSH 身份。

### 29.3 systemd 与容器

在目标主机：

```bash
cephadm ls
systemctl status ceph-<fsid>@<daemon>.service
journalctl -u ceph-<fsid>@<daemon>.service --since -1h
cephadm logs --name <daemon>
cephadm unit --name <daemon> status
cephadm shell --name <daemon>
cephadm enter --name <daemon>
podman ps -a --format json | jq -r '.[] | .Image // .ImageID'
```

数据目录通常为 `/var/lib/ceph/<fsid>/<daemon-name>`。`/var/lib/ceph/<fsid>/<daemon-name>/unit.run` 是 cephadm 生成的真实容器启动包装，可用于核对 image、mount、network、entrypoint 和参数；只读保存后再分析，不直接把它改成长期配置。先以 `cephadm ls` 证明 FSID/daemon ownership，再触碰目录。手工运行容器只用于重现启动错误，不能长期绕开 systemd 和 orchestrator。

单主机全量取证应固定 FSID，并同时保存 `cephadm ls`、每个 daemon 的 cephadm 日志与 systemd 状态。以下脚本只读采集，不因某个 inactive/failed unit 返回非零而丢弃其他证据：

```bash
umask 077
evidence_dir="$(mktemp -d /tmp/cephadm-evidence.XXXXXX)"
fsid="$(cephadm shell -- ceph fsid)"
cephadm ls > "${evidence_dir}/cephadm-ls.json"

jq -r '.[].name' "${evidence_dir}/cephadm-ls.json" |
while IFS= read -r name; do
  cephadm logs --fsid "${fsid}" --name "${name}" \
    > "${evidence_dir}/${name}.log" 2>&1
  printf '%s\n' "$?" > "${evidence_dir}/${name}.log.rc"

  systemctl status --no-pager "ceph-${fsid}@${name}.service" \
    > "${evidence_dir}/${name}.systemd" 2>&1
  printf '%s\n' "$?" > "${evidence_dir}/${name}.systemd.rc"
done
```

在每台相关主机分别执行，不能从一台主机的 `cephadm ls` 推断全局 daemon。`cephadm logs --fsid` 可避免多集群主机命中错误 FSID；还应按事故时间窗补充对应 unit 的 `journalctl`、runtime inspect 和 image digest。日志可能包含主机地址、bucket/client 名、命令参数和路径；归档或外发前保留原始受限副本、制作脱敏副本并记录 SHA256。不要在采集脚本中用 `|| true` 抹掉每条命令的返回码。

### 29.4 配置、端口与证书

对“进程反复退出”按顺序确认：实际 container command、挂载文件、配置解析、端口占用、SELinux/AppArmor、文件 owner、证书/key、registry image digest。Spec 里的值、生成文件和进程参数必须三方一致。

进入目标 daemon 容器后，使用 admin socket 读取有效配置和支持动作：

```bash
cephadm enter --name <daemon-name>
ceph --admin-daemon /var/run/ceph/ceph-<daemon-name>.asok config show
ceph --admin-daemon /var/run/ceph/ceph-<daemon-name>.asok help
```

需要操作 MON store 或 OSD object store 时，让 cephadm 按 daemon 身份挂载正确数据目录和工具环境：

```bash
cephadm unit --name mon.<host> stop
cephadm shell --name mon.<host>
ceph-monstore-tool /var/lib/ceph/mon/ceph-<host> get monmap > monmap
monmaptool --print monmap

# OSD 必须先确认停止、设备和 FSID，再选择 ceph-objectstore-tool 的只读或修复动作
cephadm shell --name osd.<id>
ceph-objectstore-tool --help
```

`ceph-monstore-tool`、`ceph-objectstore-tool` 可以直接改变持久状态。生产默认先做只读检查和块级/目录级副本，只有官方恢复步骤明确要求且变更获批时才写入。

Bootstrap 或新增 MON 出现下列任一错误：

```text
ERROR: Failed to infer CIDR network for mon ip ...; pass --skip-mon-network to configure it later
Must set public_network config option or specify a CIDR network, ceph addrvec, or plain IP
```

先证明目标 MON 地址所属 CIDR、路由和每台 MON 接口一致，再设置：

```bash
ceph config set mon public_network <mon-cidr>[,<additional-mon-cidr>...]
```

不要用过宽 CIDR 消除报错；它会让后续 MON placement 选择错误接口。

```mermaid
flowchart LR
  S[Service Spec] --> G[生成配置/unit]
  G --> C[容器实际命令与挂载]
  C --> P[进程监听/证书/身份]
  P --> B[业务请求]
  S -.逐层对账.-> B
```

## 30. MON quorum 丢失与临时 MGR

### 30.1 恢复 MON quorum

先区分“少数 MON 离线”“网络分区”与“所有 MON 确实无法形成多数派”。只要原 monmap 的多数派能够恢复，就修复网络、时钟、磁盘或单个 MON，不执行手工 monmap 注入。两个分区都仍有可读取 store 时，必须先隔离并证明不会形成双边写入；不能凭主机启动时间选择一边。

只有原多数派无法恢复且事故指挥人批准收缩 monmap 时，才执行下面的破坏性恢复。开始前冻结配置、auth、pool、CRUSH 和编排变更，记录所有 MON 的主机、daemon ID、FSID、image、unit 状态、数据目录、磁盘/文件系统状态和最后日志。停止条件包括：候选 store 的 FSID 不一致、无法证明哪份 store 最新完整、monmap 无法读取、备份空间不足，或故障实为网络分区。

```mermaid
flowchart TD
  Q[MON 无 quorum] --> A{原多数派可恢复?}
  A -->|是| S[修复网络/时钟/磁盘/进程]
  A -->|否| P{已排除网络分区并识别权威 store?}
  P -->|否| X[停止并升级事故决策]
  P -->|是| D[停止全部 MON 并备份每个 store]
  D --> M[从权威 store 提取 monmap]
  M --> R[删除非存活 MON 并注入]
  R --> O[只启动权威 MON]
  O --> V{形成 quorum 且 maps 一致?}
  V -->|否| X
  V -->|是| E[隔离旧 store并逐个扩回 MON]
```

**第一步：停止全部 MON 并取得一致备份。** 通过 SSH 登录每台 MON 主机，逐台执行；多集群主机必须带正确 FSID。为避免 quorum 恢复瞬间 active MGR 按旧 MON spec 自动 reconcile，同时记录并停止全部 MGR unit；这不会修改 MON store，且后面只恢复一个已确认的 MGR。确认 unit 已停止后，把 `/var/lib/ceph/<fsid>/mon.<id>` 按 owner、ACL、xattr 和硬链接不变地复制到独立受保护介质。备份目录不能与故障盘共用故障域，也不能把正在变化的 RocksDB 目录当一致备份。

```bash
# 在每一台 MON 主机执行，并替换占位符
cephadm ls | jq '.[] | select(.name == "mon.<id>") |
  {fsid, name, state, container_image_id, data_dir}'
cephadm unit --fsid <fsid> --name mon.<id> stop
systemctl is-active "ceph-<fsid>@mon.<id>.service"

# 在每一台 MGR 主机记录身份后停止；不要停止 OSD
cephadm ls | jq '.[] | select(.name | startswith("mgr.")) |
  {fsid, name, state, container_image_id, data_dir}'
cephadm unit --fsid <fsid> --name mgr.<id> stop
```

`systemctl is-active` 应返回 `inactive`；任何 MON 仍在运行都禁止进入下一步。对每份停止后的 store 保存校验清单、容量和备份位置，原目录只读保留，禁止先删除所谓“坏 MON”。

**第二步：选择唯一权威 MON。** 结合最后成功形成 quorum 的时间、MON 日志、store 可读性和 map epoch 选择一份最新完整 store。以该 MON 身份启动一次性 cephadm shell，挂载它的离线 store，提取而不是凭记忆重建 monmap：

```bash
cephadm shell --fsid <fsid> --name mon.<survivor-id>

# 以下命令在 cephadm shell 打开的维护容器中执行
ceph-mon -i <survivor-id> --extract-monmap /tmp/monmap
cp --preserve=all /tmp/monmap /tmp/monmap.before
monmaptool --print /tmp/monmap.before
```

这里不能使用 `cephadm enter`：Tentacle 同提交源码中它通过 runtime `exec` 进入正在运行的 daemon 容器，而本流程已经停止所有 MON。`cephadm shell --name` 才是官方同一故障章节给出的离线 daemon 工具入口，会按 daemon 身份挂载对应数据目录。

打印结果中的 FSID 必须等于事故集群 FSID；保存 `epoch`、`min_mon_release`、election strategy、每个 MON ID 和 v1/v2 地址。若 FSID 不同、地址属于另一集群、文件为空或命令报 store 损坏，立即停止，回到备份评估，不能继续 `--inject-monmap`。

**第三步：从副本中删除所有不准备启动的 MON。** `monmaptool --rm` 使用的是 daemon ID，不带 `mon.` 前缀。每删一个都重新打印；最终 monmap 只保留本次确认要启动的权威 MON，FSID 不得变化：

```bash
# 仍在权威 MON 的 cephadm shell 维护容器中
monmaptool /tmp/monmap --rm <failed-id-1>
monmaptool /tmp/monmap --rm <failed-id-2>
monmaptool --print /tmp/monmap

# 最后一次人工复核通过后才写入权威 store
ceph-mon -i <survivor-id> --inject-monmap /tmp/monmap
exit
```

删除对象必须与“最终打印结果”和变更单逐项一致。不要删除仍计划作为初始 quorum 成员的 ID；不要把新 IP 顺手塞进本次灾难恢复。`monmap.before`、修改后 monmap、命令输出和 SHA256 都要导出到受保护证据目录，容器 `/tmp` 不是持久备份。

**第四步：只启动权威 MON 并验证最小 quorum。** 其余 MON unit 保持停止，避免带旧 monmap 的 store 回来干扰恢复：

```bash
cephadm unit --fsid <fsid> --name mon.<survivor-id> start
ceph -s
ceph quorum_status --format json-pretty
ceph mon dump

# 在任何 MGR 回来之前冻结 cephadm scheduler，再只启动一个已核对的 MGR
ceph config-key set mgr/cephadm/pause true
cephadm unit --fsid <fsid> --name mgr.<mgr-id> start
ceph mgr dump
```

验收标准是 `quorum_names` 只包含预期存活成员、`mon dump` 的 FSID/epoch/地址与注入后 monmap 一致，并且命令连续多次稳定返回。`mgr.<mgr-id>` 必须使用第一步记录的真实 MGR daemon ID；若没有任何可启动 MGR，则保持 pause config-key 并执行 30.2。单 MON quorum 只用于恢复，不满足生产容错。若不能形成 quorum，停止反复 inject；保存 MON 日志和当前 store，回到注入前副本定位首个失败。

**第五步：隔离旧 store，核对集群 maps，再恢复编排。** quorum 恢复后立即暂停 cephadm 调度，防止旧 placement 把已从 monmap 删除的实例自动拉起；如 MGR 尚未恢复，先完成 30.2，再执行本步骤。

```bash
ceph orch pause
ceph osd dump
ceph osd crush dump
ceph auth list
ceph config dump
ceph mgr dump
ceph orch ls --service_name mon --export
```

`auth list` 含敏感能力与身份信息，证据必须限制访问。逐项验证 OSD map、CRUSH map、auth database、config database 和 MGR map 与事故前基线一致；任何关键 map 回退或未知变更都阻止业务恢复。将被删除 MON 的原数据目录归档到安全位置，确保 live MON DB 扩展、compaction 和归档副本均有足够空间；在新 quorum 达到目标冗余并完成恢复演练前不得销毁归档。

把 MON service spec 的期望 placement 先改为当前权威主机，再恢复调度；随后一次只增加一个跨故障域 MON，每次等待其进入 quorum、同步完成并稳定观察后再增加下一个：

```bash
ceph orch apply mon --placement="<survivor-host>"
ceph orch resume
ceph orch apply mon --placement="<survivor-host>,<new-host-1>"
ceph quorum_status --format json-pretty
ceph orch apply mon --placement="<survivor-host>,<new-host-1>,<new-host-2>"
ceph quorum_status --format json-pretty
```

最终恢复奇数个、跨故障域的 3 或 5 MON，验证连续选举、单 MON 故障、主机重启和客户端 I/O。旧 MON 数据目录只有在新 quorum 充分冗余、归档可恢复且变更获得批准后才能删除。手工注入 monmap 改变了一致性根；恢复记录必须包含原 store 备份、前后 monmap、FSID、epoch、quorum 演进、各 map 对账和业务验收。

### 30.2 没有可用 MGR

MON quorum 正常但所有 MGR 不可用时，cephadm orchestrator 命令无法工作。使用仍可访问 MON 的 admin 环境，按以下顺序手工部署临时 MGR：

```bash
# 1. 暂停 cephadm scheduler，防止恢复后立即删除临时实例
ceph config-key set mgr/cephadm/pause true

# 2. 创建目标 MGR 身份并保存完整 keyring
ceph auth get-or-create mgr.<host>.<id> \
  mon "profile mgr" osd "allow *" mds "allow *" > mgr.keyring

# 3. 生成最小配置并取得该 MGR 应使用的 image
ceph config generate-minimal-conf > ceph.conf
ceph config get "mgr.<host>.<id>" container_image

# 4. 生成 cephadm deploy 输入；输出文件按 secret 保护
jq -n --rawfile config ceph.conf --rawfile keyring mgr.keyring \
  '{config: $config, keyring: $keyring}' > config-json.json
chmod 0600 mgr.keyring ceph.conf config-json.json

# 5. 在目标主机以现有 FSID 部署
cephadm --image <container-image> deploy \
  --fsid <fsid> --name mgr.<host>.<id> --config-json config-json.json
```

`container_image` 为空时，从仍存活 daemon 的 `cephadm ls`/runtime inspect 取得已验证的同 release image，不能猜 tag。新 MGR active 后恢复/应用正式 MGR service spec、部署 standby、确认临时实例已纳入或安全移除，再执行 `ceph orch resume` 清除 scheduler pause，并销毁临时 keyring/config 文件。

临时 MGR 是恢复控制面的桥，不得成为无 spec 的长期实例。验收 `ceph -s`、`ceph mgr dump`、cephadm module 和 `ceph orch status`。

## 31. Core dump、重复调试与 live process

Systemd-coredump 通常把文件保存在主机 `/var/lib/systemd/coredump`。调试 image 必须匹配崩溃二进制版本和 debuginfo：

```bash
cephadm shell --mount /var/lib/systemd/coredump
```

临时 shell 内安装 gdb/debuginfo 会随容器退出丢失。重复分析应构建固定 debug image，例如以目标 Ceph image 为 base 安装匹配 `ceph-debuginfo`、gdb、zstd、strace 和 Python debuginfo，再用：

```bash
cephadm --image ceph:debugging shell --mount /var/lib/systemd/coredump
```

Live attach 需要与目标容器共享 PID namespace。可先：

```bash
cephadm --image ceph:debugging shell --dry-run > /tmp/debug.sh
```

审核生成命令，去掉会妨碍 attach 的 `--init`，使用类似 `--pid=container:<target-container>`。GDB attach 会暂停/影响业务进程，只在隔离窗口、冗余实例和明确回滚下执行。

```mermaid
flowchart TD
  C[daemon crash] --> P[保存 core、版本、image digest]
  P --> M[取得完全匹配的 debuginfo]
  M --> D[离线 core 分析]
  D --> R{需要复现/attach?}
  R -->|否| F[形成堆栈与根因]
  R -->|是| W[安排隔离窗口与冗余]
  W --> L[live attach]
  L --> F
```

## 32. Legacy 集群 Adoption

### 32.1 边界与准备

Adoption 把 legacy systemd/ceph-ansible 等 daemon 转成 cephadm 容器管理，不重新设计 CRUSH、网络、caps 或 pool。仅支持 BlueStore OSD。准备：

```bash
cephadm prepare-host
```

固定 Octopus 15.2 或更高兼容 image；每个 daemon 记录 FSID、name、version、unit、data path、keyring、config、端口和主机。`cephadm ls` 在迁移前显示 style `legacy`，成功后为 `cephadm:v1`。

先将本地配置同化到 MON config database：

```bash
ceph config assimilate-conf -i /etc/ceph/ceph.conf
```

不同主机同一键值冲突时，最后执行的值覆盖之前值。必须先 diff 和决定权威值，不能并行无序执行。

### 32.2 官方迁移顺序

```mermaid
flowchart LR
  I[盘点/健康/备份] --> MON[adopt MON]
  MON --> MGR[adopt MGR]
  MGR --> BE[启用 cephadm backend]
  BE --> SSH[建立 SSH 身份]
  SSH --> HOST[添加 hosts]
  HOST --> OSD[分批 adopt BlueStore OSD]
  OSD --> MDS[重建 MDS service]
  MDS --> RGW[重建 RGW service]
  RGW --> CLEAN[清 stray 与旧控制器]
```

```bash
cephadm adopt --style legacy --name mon.<hostname>
cephadm adopt --style legacy --name mgr.<hostname>
ceph orch set backend cephadm

ceph cephadm generate-key
ceph cephadm get-pub-key > ceph.pub
ssh-copy-id -f -i ceph.pub root@<host>
ceph orch host add <hostname> <ip>

cephadm adopt --style legacy --name osd.1
cephadm adopt --style legacy --name osd.2
```

每批 OSD 后确认原 ID、data、auth、CRUSH location、up/in、PG 与业务 I/O；不要在 recovery 活跃时进入下一批。

### 32.3 MDS 和 RGW 不是直接 adopt

MDS/RGW 通过新 service spec 部署等价 daemon，验证新实例后再停旧 unit、删除旧 data dir：

```bash
ceph orch apply mds <fs-name> --placement=<placement>
ceph orch ps --daemon-type mds --refresh

ceph orch apply rgw <service-id> \
  --realm=<realm> --zone=<zone> --port=<port> \
  --placement=<placement>
```

新实例必须沿用正确 realm/zone、CephX、端口、证书和负载均衡摘流。先业务验收，后停止 legacy；顺序反过来会制造中断。

### 32.4 管理权交割

Adoption 完成后：

- 所有期望 daemon 在 `ceph orch ps` 中为 `cephadm:v1`；
- `ceph health detail` 无 stray host/daemon；
- 导出完整 service specs；
- 禁用 ceph-ansible、Salt、cron 或自研脚本等旧控制器；
- 只删除明确不再使用的旧 unit/config/data，不碰 cephadm 新目录；
- 完成一次升级、重启、主机故障和业务恢复演练后，再销毁旧恢复材料。

双控制器同时管理 unit/config 是 adoption 后最危险的隐性状态。

## 33. Service 删除与整集群销毁

### 33.1 删除单个 service

```bash
ceph orch rm <service-name>
```

删除 service 会删除其受管 daemon，但后端数据是否删除取决于服务：删 RGW daemon 不删 bucket 数据；删 ingress 会立即移除稳定入口；删 Prometheus `--force` 删除指标；删 MDS 不代表可删 CephFS pool；删 iSCSI/SMB 前需客户端 drain。每种 service 都必须单独写数据所有权和恢复动作。

### 33.2 整集群销毁

```bash
cephadm rm-cluster --force --zap-osds --fsid <exact-fsid>
```

它停止并删除该 FSID 的 daemon；`--zap-osds` 再擦 OSD 设备，不可恢复。执行前做四重确认：

1. 在线 `ceph fsid`；
2. 每台主机 `cephadm ls` 的 FSID 和 daemon；
3. OSD ID、设备 path、WWN/serial 与计划销毁资产；
4. 独立备份恢复演练和业务下线审批。

```mermaid
flowchart TD
  R[收到销毁请求] --> F[在线 FSID]
  F --> L[逐主机 cephadm ls]
  L --> D[逐盘 OSD/WWN/serial 对账]
  D --> B[备份可恢复证明]
  B --> A{四项完全一致且审批?}
  A -->|否| X[禁止执行]
  A -->|是| C[rm-cluster 精确 FSID]
  C --> V[验证无 unit/container/data/OSD signature]
```

不使用通配 FSID、模糊主机名或未经核验的 `/dev/sdX`。多集群主机必须逐 FSID 隔离清理。

## 34. 生产 Runbook：扩容、维护、换盘、升级

### 34.1 新增主机与容量

1. 完成第 3 节准入和镜像预热。
2. 分发 cephadm SSH 信任，确认远端 `hostname`。
3. `host add <name> <ip>`，添加普通 placement 标签。
4. `host ls --detail`、`check-host`、网络检查均通过。
5. `device ls --wide --refresh`，按 WWN/serial 审核。
6. 用现有 OSD spec `--dry-run`，确认不会消费 OS/保留盘。
7. Apply 后观察 OSD 创建、up/in、backfill 和容量均衡。
8. 执行业务读写并导出更新后的 inventory/spec。

### 34.2 主机维护

1. 检查该 host 承载的 MON/MGR/MDS/OSD/RGW 和故障域。
2. 确认 quorum、standby 和 PG 副本可承受离线。
3. `maintenance enter`，不默认使用 force。
4. 观察业务、PG 和其他实例接管。
5. 完成硬件/OS 维护，验证 runtime、时间和网络。
6. `maintenance exit`，显式恢复仍 stopped daemon。
7. 等 health 和业务回到基线再关闭窗口。

### 34.3 故障盘替换

1. 用 OSD metadata、`ceph osd tree`、BMC、WWN/serial 四方定位。
2. 暂停会自动消费盘的 spec，或明确接受自动重建。
3. `osd rm <id> --replace`，等待排空和 destroyed。
4. 物理换盘，rescan，确认新 serial 和 available。
5. 原 spec dry-run，验证 host、data/db/wal 和 OSD ID。
6. 恢复 managed/apply，等待 up/in 和 recovery 完成。
7. 业务 I/O、scrub 策略和故障灯复位验收。

### 34.4 版本升级

1. 完成 release note、健康、空间、备份、镜像和 MGR standby 门禁。
2. 记录 pool autoscale 原状态并临时 noautoscale。
3. `upgrade check` 后按固定顺序启动。
4. 每个 stagger 批次验证版本、quorum、PG、延迟和业务 I/O。
5. 首个错误停批，保留 events/logs，不清洗现场。
6. 全部完成后执行 release-specific post actions。
7. 恢复 autoscale 原值，核对辅助服务 image，完成故障切换验证。

### 34.5 Service Spec 变更与可证明回退

端口、placement、network、image、TLS、custom config 和 daemon 参数都走同一主线。以下“旧声明”只能恢复编排意图，不能自动撤销期间已经发生的数据迁移、客户端请求或外部系统变更：

```bash
ceph orch ls --service_name <service> --export > <service>.before.yaml
ceph orch ps --service_name <service> --refresh --format yaml \
  > <service>.daemons.before.yaml
ceph orch apply -i <service>.candidate.yaml --dry-run
ceph orch apply -i <service>.candidate.yaml
ceph orch ls --service_name <service> --refresh --format yaml
ceph orch ps --service_name <service> --refresh --format yaml
```

```mermaid
flowchart TD
  B[导出旧 spec 与 daemon/image 事实] --> D[candidate dry-run]
  D --> P{placement/network/端口/证书可满足?}
  P -->|否| X[不 apply，修正候选]
  P -->|是| A[apply]
  A --> R[观察 service/daemon events 与 reconcile]
  R --> H{daemon、Ceph health、业务探针均合格?}
  H -->|是| S[导出收敛后的 spec 并归档]
  H -->|否| T[停止下一批并保存 first failure]
  T --> C{旧 spec 仍与当前数据/外部依赖兼容?}
  C -->|是| O[重应用旧 spec，必要时 redeploy]
  C -->|否| F[按服务恢复方案修复前进或恢复数据]
```

停止条件包括：placement 意外删除实例、端口冲突、证书/SAN 不匹配、image digest 不一致、PG/业务错误扩大、入口 failover 失败。回退后必须再次等待 reconcile，并用真实协议探针验证；仅看到旧 YAML 已接受不算恢复。`custom_configs`、mount、image 或启动参数变化通常需要 reconfig/redeploy，不能假设 apply 已更新正在运行的容器。

不同服务还要处理自身不可逆副作用：OSD/CRUSH 变化会迁移数据；RGW multisite period 和 zone 不是由 RGW daemon spec 自动回退；删除 Prometheus `--force` 已丢失历史指标；客户端在入口切换期间的成功写入不能由旧 ingress spec 撤销；MDS、iSCSI、NFS、SMB 的客户端状态需要各自 drain/reconnect。变更单必须把“恢复声明”和“恢复业务状态”分开验收。

### 34.6 控制面恢复资料包与恢复演练

Service Spec 不是 Ceph 备份。恢复材料必须按对象分层保存：

| 恢复对象 | 必须保存的材料 | 明确不包含什么 |
|---|---|---|
| 编排意图 | `ceph orch ls --export`、host inventory/labels/location、tuned profiles、image digest、版本 | 不包含 RADOS 数据，也不保证 MON/auth/config-key 可恢复 |
| 集群配置与放置 | `ceph config dump`、OSD/CRUSH map、pool 与 filesystem/RGW/NFS/SMB/iSCSI 配置清单 | 文本导出不能代替一致 MON store |
| 身份与 secret | CephX entity/caps/keyring、cephadm SSH 身份、registry 凭据、cert/key 与外部 IDP/NMS secret | 普通工单和日志不得保存明文副本 |
| MON 控制状态 | 按官方一致性方法取得并验证的 MON store 备份，记录 FSID、epoch、release、owner/ACL/xattr | 运行中直接复制 RocksDB 目录不构成一致备份 |
| 服务后端状态 | `.nfs`、`.smb`、iSCSI config pool、RGW realm/period/zone、业务 pool 和应用备份 | 重建 daemon 容器不会重建这些数据或外部 DNS/LB/IDP |
| 外部依赖 | registry、CA、DNS、NTP、VIP、firewall、OIDC、NMS、客户端配置与恢复联系人 | cephadm 不拥有这些系统的可用性 |

资料包生成前记录 `ceph -s`、FSID、quorum、版本、当前 epoch 和时间；生成后按文件分类做 SHA256、加密、访问控制、异地副本和恢复负责人登记。`ceph config-key dump`、auth/keyring、registry 和 cert key 含直接接管集群的秘密，只能进入加密的受限 secret 包，不能与可广泛分发的 spec/config 清单混放。

恢复演练必须在隔离环境证明以下顺序，而不是只证明归档可解压：确认 FSID 与目标资产；恢复 MON quorum 和可用 MGR；保持 cephadm paused；核对 maps、auth、config-key 与 service specs；恢复主机 SSH/registry；分批恢复编排；最后验证 RADOS 与每种对外协议的真实 I/O。任何 FSID、MON store 时代、设备身份或 secret 来源无法证明时立即停止，不能把另一个集群的“更新文件”混入事故集群。

```mermaid
flowchart LR
  PKG[加密恢复资料包] --> ID[FSID/epoch/资产核验]
  ID --> MON[恢复 MON quorum]
  MON --> MGR[恢复可用 MGR，编排保持 paused]
  MGR --> MAP[核对 maps/auth/config-key/spec]
  MAP --> HOST[恢复 SSH/registry/hosts]
  HOST --> REC[分批 resume/reconcile]
  REC --> IO[RADOS 与各协议 I/O 验收]
```

## 35. 生产验收矩阵

| 域 | 必须证明的结果 | 主要命令/探针 |
|---|---|---|
| 控制面 | MON quorum、MGR active+standby、cephadm available | `ceph -s`、`quorum_status`、`mgr dump`、`orch status` |
| 主机 | 名称、SSH、runtime、时间、网络、标签正确 | `host ls --detail`、`check-host` |
| 声明 | 所有 service spec 可导出并与批准配置一致 | `orch ls --export` |
| Daemon | running/desired、版本、image digest、events 正常 | `orch ps --refresh`、`ceph versions` |
| OSD | 设备身份、布局、加密、class、up/in 正确 | `device ls --wide`、`osd tree`、`ceph-volume` |
| 数据 | PG clean，容量与恢复余量满足设计 | `ceph -s`、`ceph df detail`、`pg stat` |
| CephFS | MDS rank/standby 与挂载 I/O | `fs status` + 客户端读写/锁 |
| RGW | realm/zone、TLS、VIP、S3 I/O 与 failover | RGW/ingress 状态 + S3 PUT/GET/DELETE |
| NFS | export、mount、锁、VIP 漂移 | NFS client I/O/failover |
| iSCSI | discovery、session、multipath 与块 I/O | initiator/multipath 业务探针 |
| SMB | 域加入、ACL、锁、CTDB（如启用） | SMB 客户端业务探针 |
| 监控 | targets/rules/notifications/dashboard 与安全入口 | Prometheus API、Alertmanager、Grafana |
| 证书 | chain、SAN、有效期、轮换责任 | `cert check` + TLS handshake |
| 恢复 | 主机、MGR、MON、OSD 激活和备份路径已演练 | 对应 runbook 当前证据 |

```mermaid
flowchart TD
  C[控制面通过] --> H[主机与声明通过]
  H --> D[daemon 与设备通过]
  D --> P[PG/容量通过]
  P --> S[各协议真实 I/O]
  S --> F[单故障切换]
  F --> R[恢复演练]
  R --> A[生产验收]
```

## 36. 快速判定表：看到现象先去哪里

| 现象 | 第一证据 | 不应先做 |
|---|---|---|
| Spec apply 后没动作 | unmanaged/paused、service events、placement dry-run | 手工起容器 |
| 某主机全失败 | `check-host`、SSH、sudo、runtime、时间 | 重装整个集群 |
| OSD 新盘没创建 | reject reasons、spec filter、unmanaged | 直接 zap 未知盘 |
| Zap 后又自动建 OSD | managed persistent OSD spec | 反复 zap |
| MON 不增加 | public_network 与主机地址、placement | 注入 monmap |
| RGW 改退出时间不生效 | 是否 redeploy | 重启所有 RGW |
| NFS HA 连接损坏 | HAProxy Protocol 两端值 | 只改客户端 |
| Grafana 不可访问 | anonymous/password、frontend URL、TLS | 暴露内部端口 |
| 升级停住 | `upgrade status`、offline host、首个 event | 重复 start 多个目标 |
| MGR 无法编排 | MGR quorum/active 与 module | 删除 service specs |
| Cert 告警 | owner、scope、SAN、expiry | 直接 rm certificate |
| Adoption 后 unit 被改回 | 旧 Ansible/Salt/cron 控制器 | 再次 adopt 全部 daemon |

## 37. 实现、扩展与规模化边界

这一章覆盖 Cephadm 官方索引直接纳入的 developer/design 文档。它们同时包含现有实现、开发方法和未来设计讨论；三者必须严格区分，不能把 proposal 当作已经可用的生产命令。

### 37.1 对象命名与 reconcile 执行模型

| 名称 | 精确定义 | 示例 |
|---|---|---|
| `service_type` | ServiceSpec 定义的服务类型 | `mon`、`mgr`、`rgw`、`ingress` |
| `service_id` | 同一类型下的服务标识；部分类型不需要 | `site-a` |
| `service_name` | `<service_type>.<service_id>` | `rgw.site-a` |
| `daemon_type` | daemon 类型；通常等于 service type，但 ingress 产生 `haproxy`、`keepalived` | `haproxy` |
| `daemon_id` | 通常为 `<service_id>.<hostname>.<random>`；OSD 固定为数字 ID | `site-a.gw01.abc123` |
| `daemon_name` | `<daemon_type>.<daemon_id>` | `rgw.site-a.gw01.abc123` |

管理员 CLI、orchestrator module、MON config-key 和目标主机之间不是一次调用完成全部工作：CLI 写入意图，active MGR 的 cephadm module 在 `serve()` 循环中读取缓存、调度远端动作、更新状态并持续 reconcile。

```mermaid
sequenceDiagram
  participant CLI as ceph orch CLI
  participant MON as MON command/config-key
  participant MGR as active MGR cephadm
  participant Cache as host/daemon/device cache
  participant Host as target host
  CLI->>MON: 提交或查询
  MON->>MGR: command handler
  MGR->>Cache: 写入期望/读取事实
  MGR-->>CLI: 返回已接受或缓存状态
  loop serve reconcile
    MGR->>Host: SSH/agent 查询或执行
    Host-->>MGR: inventory/daemon/result
    MGR->>Cache: 更新事实与 event
  end
```

MGR command handler 会阻塞同一 MON command 处理线程；客户端按 `Ctrl-C` 只终止本地等待，不会取消 MGR 内正在执行的调用。在 cephadm 扩展中，同步 CLI handler 最多执行 `O(1)` 次网络调用，其余远端工作必须异步交给 `serve()` 等后台线程。CLI 超时时先查 cephadm event 和 MGR 日志，不能立即重复提交同一个破坏性动作。

### 37.2 主机抓取、缓存与规模化

官方 scalability notes 描述的基线是：cephadm 管理全部注册主机，周期性获取 disk、daemon、network/firewall 等事实；历史实现最多并行抓取 10 台主机、约每 6 分钟一轮，磁盘和 daemon 等检查在单主机上顺序执行。`--refresh` 会绕过等待但增加瞬时负载。

cephadm-exporter 通过 HTTP 提供 host metadata，缩短扫描时间，但并未自动消除全部规模问题：

- SSH 与 HTTP 两种传输并存，认证、超时和故障模式不同；
- standalone `cephadm` 不便随意引入外部 HTTP server 依赖；
- 主机元数据进入 MON config-key、MGR memory 或 RADOS pool 的耐久性需求不同；
- 单纯扩大 worker pool 只降低总墙钟时间，远端单机耗时不变，复杂度下界仍接近 `O(hosts) + O(daemons)`；
- 所有改进都必须保持升级兼容或与旧路径完全隔离。

这些数字来自官方“Notes and Thoughts”，不是 Tentacle 的容量 SLA。生产容量验证要测量 P50/P95 host refresh、完整 reconcile 周期、MGR CPU/内存、MON config-key 增长、SSH/HTTP 失败率和 `orch ps --refresh` 延迟，并以目标主机/daemon 数量压测。

```mermaid
flowchart TD
  H[主机与 daemon 数增长] --> P[周期扫描队列增长]
  P --> C[状态缓存变旧]
  C --> O[运维决策使用旧事实]
  P --> M[MGR/SSH/HTTP 压力]
  E[cephadm-exporter] --> F[缩短单次事实采集]
  F --> P
  T[规模压测与告警] --> O
  T --> M
```

### 37.3 Compliance 设计与已实现配置检查

官方 compliance-check 文档是设计讨论，不是已承诺 CLI。文档提出利用 `HostFacts`/`gather-facts` 缓存，按默认 12 小时间隔检查 OS vendor/major、SELinux/AppArmor、systemd daemon、订阅状态、MTU、link speed 和 public/cluster 网络一致性，并聚合为 WARN；还设想 `ceph cephadm compliance ...` 命令族。

Tentacle 的有效操作入口是 `ceph cephadm config-check ...` 和 `CEPHADM_CHECK_*` 健康码。下列 proposal 尚未形成可用 CLI，不能写入自动化或运行手册：

```text
ceph cephadm compliance enable|disable|status
ceph cephadm compliance enable-check|disable-check <name>
ceph cephadm compliance set-check-interval|get-check-interval
```

设计价值在于建立检查原则：host status 必须进入上下文，一次呈现全部平台问题，允许受控 opt-out，并把 OS、硬件和网络视为 Ceph 稳定性的一部分。实际命令面以 `ceph --help` 和当前 module schema 为准。

### 37.4 Host maintenance 的实现与设计来源

官方设计把 maintenance 定义为以下状态机：先检查移除主机是否影响数据可用性；对 host CRUSH subtree 设置 `noout`；停止并 disable 该主机 Ceph target；退出时逆序恢复。当前 `ceph orch host maintenance enter/exit` 已实现这一生产工作流，并增加 `--force`、`--yes-i-really-mean-it`、离线退出等现实分支。

```mermaid
flowchart TD
  R[maintenance request] --> D{data availability safe?}
  D -->|否| X[拒绝]
  D -->|是| S{secondary service impact?}
  S --> W[列出 MGR/RGW/monitoring/gateway 影响]
  W --> N[host subtree noout]
  N --> T[stop + disable ceph target]
  T --> M[host status maintenance]
  M --> E[exit: enable/start target]
  E --> O[清除 noout 并验证服务]
```

设计文档提出的 `--check` 二阶段交互、维护窗口超时、第三方告警静默插件和 SLA 记录属于未来方向，不能假定存在。当前变更单必须自行列出非原生服务影响，包括 MGR、Prometheus、Grafana、Alertmanager、RGW、HAProxy、iSCSI 和 NFS-Ganesha。

### 37.5 设备与 OSD 工作流：当前能力和设计提案

官方设计文档把设备管理分成 inventory、add、remove、replace 四条链。当前可执行事实是：

1. `ceph orch device ls --wide --refresh` 返回 host、path、type、serial、size、health、LED 和 available/reject reason；大规模环境要关注返回延迟和缓存年龄。
2. `daemon add osd` 是明确设备的一次性入口；DriveGroup 是持续声明，未来匹配盘和 zap 后重新 available 的盘会自动被消费。
3. `orch osd rm` 负责迁移 PG 后删除，`rm status` 显示进度，`rm stop` 取消仍可取消的队列，`device zap` 清理签名。
4. `--replace` 保留 destroyed OSD ID，后续以设备 replacement 或 `osd_id_claims` 复用。

设计提案指出声明式 DriveGroup 可能在新增盘、清盘和高流量窗口自动触发 OSD 创建及 rebalance，并提出 device/host 两种 UI、确认页、预计时间，以及 immediate、按 OSD 分阶段、按 host 分阶段引入容量。它不是当前 CLI 已自动实现的策略。生产用下面的人工门禁获得同等安全性：

```mermaid
flowchart LR
  I[盘点 serial/size/health/available] --> P[dry-run 与原始容量汇总]
  P --> F[按 rack/host 检查容量和故障域平衡]
  F --> C{空集群初始化?}
  C -->|是| B[审批后批量创建]
  C -->|否| W[小批创建并限制 recovery/backfill]
  W --> V[每批验证 latency/PG/business I/O]
  V --> W
  B --> E[最终 OSD/CRUSH/class 验收]
  W --> E
```

### 37.6 Cephadm 开发与验证路径

扩展或定位 cephadm 本身时，官方提供多条开发环境路径，适用边界不同：

| 路径 | 特征 | 适用与限制 |
|---|---|---|
| `vstart --cephadm` | MON/MGR 等可由 vstart 启动，额外 daemon 交给 cephadm | 适合快速改 MGR/cephadm；vstart daemon 会显示 stray，不等价于真实生产集群 |
| `cstart.sh` + `cpatch` | 建立正常 cephadm 集群，把本地构建 patch 进稳定 FSID 对应 image | 更接近生产集群；变更 image 后需重启目标 daemon，结束用 `ckill.sh` 清理 |
| `bootstrap --shared_ceph_folder` | 把源码目录共享给容器，无需完整编译 Ceph | 适合 Python MGR 模块；源码变化后重启 MGR |
| kcli VM plan | 多 VM、可选择 OS/CPU/磁盘，接近 QE/生产 | 固定 kcli image tag，避免 rolling release 破坏复现；完整删除 VM/磁盘 |
| cephadm box | Podman-in-Podman 或 Docker 的快速实验环境 | 官方标记 experimental；loop device 不是生产盘，Podman OSD 支持有限，Docker privileged 有宿主机风险 |

最小开发命令示意：

```bash
# vstart
MON=1 MGR=1 OSD=0 MDS=0 ../src/vstart.sh -d -n -x --cephadm

# 更接近生产的容器集群
sudo ../src/cstart.sh
sudo ../src/script/cpatch -t <generated-image> --py
sudo ../src/ckill.sh

# shared source
sudo ./cephadm bootstrap --mon-ip 127.0.0.1 \
  --skip-mon-network --skip-monitoring-stack --single-host-defaults \
  --skip-dashboard --shared_ceph_folder <ceph-source>

# kcli：固定经过验证的容器 tag 后创建并检查三节点 plan
kcli create plan -u \
  https://github.com/karmab/kcli-plans/blob/master/ceph/ceph_cluster.yml
kcli list vms
kcli ssh ceph-node-00
# 开发 MGR/Dashboard 时把宿主机源码共享给 plan
kcli create plan -u \
  https://github.com/karmab/kcli-plans/blob/master/ceph/ceph_cluster.yml \
  -P ceph_dev_folder=<absolute-ceph-source>

# Dashboard 前端必须先生成 bundle，watch 完成后再刷新浏览器
cd <ceph-source>/src/pybind/mgr/dashboard/frontend
NG_CLI_ANALYTICS=false npm ci
npm run build -- --deleteOutputPath=false --watch

# experimental box 生命周期
cd src/cephadm/box
./box.py -v cluster setup
./box.py -v cluster start
sudo ./box.py -v cluster start --extended --osds 5 --hosts 5
./box.py -v cluster bash
./box.py cluster list
./box.py cluster cleanup
./box.py cluster down
```

`vstart --cephadm` 默认使用本地 `~/.ssh/id_dsa[.pub]` 作为无口令 root SSH 的实验 key，且 vstart 启动的 daemon 没有 service spec，因此出现 stray warning 是预期开发现象，不应通过生产告警豁免掩盖。`cstart.sh` 在 build 目录保存稳定 FSID，以其前 8 位生成 `quay.io/ceph-ci/ceph:<tag>`；`cpatch --py` 不包含 Dashboard，改完 image 后要重启目标 daemon 或 `ceph-$(cat fsid).target` 才加载新代码。shared-source 模式中 `pybind/mgr/` 变更同样要重启 MGR。

Dashboard watch build 出现 `Localized bundle generation complete.` 才表示前端 bundle 已重新生成；`npm ci`、目录 owner 和 Node 依赖失败属于开发环境问题，不能通过把宿主目录放宽为全局可写来规避。kcli 使用 rolling release，企业复现实验必须把验证过的容器 tag、plan URL/commit、VM image、CPU、内存和磁盘参数一并固定，结束后删除 plan 创建的 VM 与磁盘。

Cephadm box 的 `--extended` 才会按参数增加 host/OSD；未带它时单独给 `--hosts`/`--osds` 不改变集群。每个 loop OSD 消耗 5 GiB 空间；Podman-in-Podman 的 OSD 支持仍未完成，Docker engine 会启用 privileged 容器，官方记录过导致图形会话退出的风险，SELinux 与源码目录权限也可能引发非生产性失败。它是 experimental 开发设施，不是兼容性或数据耐久性证据。

这些入口只属于隔离开发环境，不能在生产节点使用 `--shared_ceph_folder`、loop OSD、development image 或 privileged nested runtime。任何实验结束都要验证 VM、container、loop device、VG/LV、network 和 registry image 已清理；`cluster cleanup/down` 的输出不能替代宿主机实际残留检查。

### 37.7 构建 cephadm 与制品溯源

新版 cephadm 是 Python Zip Application，不再是复制单个源码脚本。官方构建入口：

```bash
./src/cephadm/build.py \
  -SCEPH_GIT_VER=<full-commit> \
  -SCEPH_GIT_NICE_VER=<describe> \
  -SCEPH_RELEASE=tentacle \
  -B rpm \
  ./cephadm
./cephadm version --verbose
sha256sum ./cephadm
```

版本 metadata 支持 `CEPH_GIT_VER`、`CEPH_GIT_NICE_VER`、`CEPH_RELEASE`、`CEPH_RELEASE_NAME`、`CEPH_RELEASE_TYPE`；bundled dependencies 模式为 `pip`、`rpm` 或 `none`。`version --verbose` 会显示构建 metadata、bundled packages 和 zip root entries。制品溯源必须保存源码提交、构建参数、依赖模式、构建日志、SHA256 和签名，并证明运行制品与审核源码一致。

## 38. 参考资料与许可

Ceph Tentacle cephadm 参考资料包括：

- `index.rst`、`compatibility.rst`、`install.rst`；
- `host-management.rst`、`operations.rst`、`troubleshooting.rst`；
- `services/index.rst`、`services/osd.rst`；
- `services/mon.rst`、`mgr.rst`、`mds.rst`、`rgw.rst`、`nfs.rst`；
- `iscsi.rst`、`smb.rst`、`monitoring.rst`；
- `mgmt-gateway.rst`、`oauth2-proxy.rst`；
- `snmp-gateway.rst`、`tracing.rst`、`custom-container.rst`；
- `certmgr.rst`、`client-setup.rst`、`upgrade.rst`、`adoption.rst`。

开发与设计参考资料包括 `doc/dev/cephadm/` 下的 `index.rst`、`compliance-check.rst`、`host-maintenance.rst`、`scalability-notes.rst`、`developing-cephadm.rst`、`design/storage_devices_and_osds.rst`，以及 RST `autoclass` 动态展开的 `ServiceSpec`、`DriveGroupSpec` 和相关校验源码。设计提案不构成现有功能承诺。

文档版本：Ceph Tentacle。RST 示例与同版本可执行 schema 冲突时，以 schema 为准。Ceph Authors and Contributors，文档许可 CC BY-SA 3.0。

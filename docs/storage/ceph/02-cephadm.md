# Cephadm：从裸机到可持续运维的生产编排手册

> 适用版本：Ceph Tentacle。本文以官方 `doc/cephadm/` 全模块为事实基线，核验提交 `76fba24cef67d9219f97eeaa68cd1a848da3f2b2`。目标不是复述目录，而是给出一条可执行、可验收、可停止、可恢复的生产主线。

## 1. Cephadm 的工作模型

Cephadm 不是“用容器启动 Ceph”的脚本。它由三个相互约束的平面组成：

| 平面 | 权威状态 | 职责 | 边界 |
|---|---|---|---|
| 编排控制面 | active MGR 的 cephadm module | inventory、Service Spec、placement、证书、SSH 和 reconcile | 不替代 MON 一致性与业务协议 |
| 主机执行面 | SSH、主机 cephadm、systemd、Podman/Docker | 拉镜像、生成 unit/config、控制容器、扫描设备 | 不决定期望副本数 |
| Ceph 数据面 | MON/MGR/OSD/MDS/RGW 等 daemon | quorum、对象、元数据、协议和业务 I/O | 不保存完整部署意图 |

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

容器 `running` 只覆盖第三项的一部分，绝不等于交付成功。

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
```

`host add` 名必须与远端 `hostname` 完全相等。FQDN 或短名都可，但集群内统一；最好显式传 IP，避免 DNS 漂移改变 SSH 目标。

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

私钥和 SSH config 存在 MON config-key 中，对所有 MGR 可见。变更 key 后重启或 failover MGR 重新加载。自定义 config 不能引用只存在于某个管理员 shell 的临时路径。

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

Managed daemon 手工删除会被自动重建；先设 unmanaged 才能保持手工拓扑。特殊无 spec 的 OSD service 始终 unmanaged。

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

`orch ps` 默认可能缓存约 10 分钟；看 `REFRESHED` 或使用 `--refresh`，缓存周期由 `daemon_cache_timeout` 控制。MDS/OSD/MGR rotate-key 无需重启，其他 daemon 需要适当重启。

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

它扫描并部署缺失 OSD daemon。Registry 登录也可能触发该主机其他缺失 daemon 的 reconcile；先确保 host 不在 offline/maintenance。若为测试取出 cephadm private key，验证后立即删除且不写日志。

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

RGW spec 可内嵌 PEM private key + certificate 并设置 `ssl: true`。也可：

```yaml
spec:
  ssl: true
  generate_cert: true
  rgw_frontend_port: 8443
  wildcard_enabled: true
  zonegroup_hostnames:
    - s3.example.com
```

`wildcard_enabled` 默认 false；开启后自签证书加入 `*.s3.example.com`，只覆盖一个 DNS label 层级。使用企业证书时核验完整链、私钥匹配、SAN 和 virtual-hosted-style bucket DNS。

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
  virtual_interface_networks:
    - 10.40.0.0/24
  use_keepalived_multicast: false
  vrrp_interface_network: 10.41.0.0/24
  first_virtual_router_id: 50
  health_check_interval: 2s
```

也可用 `virtual_ips_list` 配多个 VIP，每个 IP 建立一个 virtual router；VIP 数量不得超过 ingress 节点数。`first_virtual_router_id` 默认 50，有效 1-255，多 ingress 服务要避免 ID 冲突。Keepalived 默认 unicast，设置 multicast 后使用 `224.0.0.18`。

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
```

配置保存在 `.nfs` pool，export 由 `ceph nfs export ...` 或 Dashboard 管理。服务 running 后还需从客户端 mount、创建、读回、锁和 failover 验收。

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

未设置 `clustered` 时，多 Samba 实例没有透明状态迁移，不能宣称 HA。配置可放 `.smb` pool 的 cluster namespace：

```bash
rados --pool=.smb --namespace=tango put config.json /tmp/config.json
```

也可放 MON KV：

```bash
ceph config-key set smb/config/tango/config.json -i /tmp/config.json
```

使用推荐 URI 命名时 cephadm 自动生成最小 CephX 访问。HTTP(S) 配置的可用性、TLS 与鉴权由管理员负责。域模式高度依赖 DNS；宿主机或 `custom_dns` 必须可解析且可达 AD。

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

可覆盖的 image 选项包括 Prometheus、Grafana、Alertmanager、node-exporter、Loki、Promtail、HAProxy、Keepalived、SNMP gateway、Elasticsearch 和 Jaeger 三组件。设置后必须 redeploy 对应服务；自定义 image 会阻断 cephadm 的自动辅助组件升级，需管理员持续更新。恢复默认：

```bash
ceph config rm mgr mgr/cephadm/container_image_prometheus
ceph orch redeploy prometheus
```

Cephadm 的 Jinja2 模板可通过 `mgr/cephadm/services/...` config-key 覆盖，包括 Alertmanager、Grafana、ingress、iSCSI、mgmt-gateway、NFS、node-exporter、NVMe-oF、OAuth2、Prometheus、Loki 和 Promtail。例：

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
  ssl_cert: |
    -----BEGIN CERTIFICATE-----
    ...
  ssl_key: |
    -----BEGIN PRIVATE KEY-----
    ...
```

修改 TLS 1.2 cipher 需要按当前安全基线审核；TLS 1.3 自带安全 cipher 集，盲目覆盖可能失去前向保密或重新启用弱算法。

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
  client_secret: <secret>
  cookie_secret: <secret>
  ssl_certificate: |
    -----BEGIN CERTIFICATE-----
    ...
  ssl_certificate_key: |
    -----BEGIN PRIVATE KEY-----
    ...
```

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

验证 issuer discovery、client secret、redirect URI、cookie secret 长度/轮换、TLS chain 和 claims。部署成功后 cephadm 自动 redeploy mgmt-gateway 接入认证。Image 由 `container_image_oauth2_proxy` 控制，修改后 redeploy。

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
```

相对 mount source、dirs 和 files 都位于 `/var/lib/ceph/<fsid>/<daemon-name>`。文件父目录须由 `dirs` 创建；字符串内容要双引号并用 `\n`，多行可用字符串列表。

Init container 可独立指定 image、entrypoint、entrypoint_args、volume_mounts、envs、privileged；省略 image/mount/privileged 时继承主容器。它们按顺序在主进程前执行，总运行时间不能超过 200 秒，否则 service 启动失败。

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

官方 RST 的生成命令处存在 `cehp` 拼写错误，本文按真实 CLI `ceph` 给出。上传 pair 前在离线环境验证 PEM、私钥匹配、issuer、chain、SAN、KeyUsage 和有效期。替换流程：

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
ceph orch upgrade start --image <target-image>
ceph orch upgrade status
ceph -W cephadm
ceph progress
ceph versions
```

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

`ceph orch pause` 停止后台 reconcile，但现有 daemon 继续运行；`resume` 恢复。Disable cephadm module 的影响更大，不能作为普通暂停替代。暂停期间所有漂移都会积累，恢复前先 dry-run/导出 spec 评估将发生的批量动作。

### 29.2 主机与 SSH

```bash
ceph cephadm check-host host02
ceph orch host ls --detail
ceph cephadm get-ssh-config
ceph cephadm get-pub-key
```

从 active MGR 所在主机/容器语境复现 SSH，检查 hostname、known hosts、key、signed cert、sudo、Python、runtime、时间。管理员工作站能登录不能证明 MGR 能登录。

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
```

数据目录通常为 `/var/lib/ceph/<fsid>/<daemon-name>`。先以 `cephadm ls` 证明 FSID/daemon ownership，再触碰目录。手工运行容器只用于重现启动错误，不能长期绕开 systemd 和 orchestrator。

### 29.4 配置、端口与证书

对“进程反复退出”按顺序确认：实际 container command、挂载文件、配置解析、端口占用、SELinux/AppArmor、文件 owner、证书/key、registry image digest。Spec 里的值、生成文件和进程参数必须三方一致。

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

先区分“少数 MON 离线”与“全部 MON 丢失 quorum”。只要多数派仍在，修复/重建单个 MON；不要提取 monmap 全量重构。全部 quorum 丢失时，从确认最新且完整的存活 MON 数据目录/容器提取 monmap，按官方 restore-quorum 流程重建最小多数派。

```mermaid
flowchart TD
  Q[MON 无 quorum] --> A{仍有多数派可恢复?}
  A -->|是| S[修复故障 MON 网络/磁盘/进程]
  A -->|否| D[冻结写变更并盘点 MON 数据]
  D --> N[选最新完整 MON store]
  N --> M[提取/修复 monmap]
  M --> R[重建最小 quorum]
  R --> V[验证 epoch/auth/maps]
  V --> E[逐个扩回目标 MON 数]
```

手工注入 monmap 会改变一致性根，必须保留原数据副本、FSID、epoch 和每一步输出。恢复 quorum 后先核验 OSD/CRUSH/auth/config maps，再恢复编排。

### 30.2 没有可用 MGR

MON quorum 正常但所有 MGR 不可用时，cephadm orchestrator 命令无法工作。可通过 cephadm 手工部署临时 MGR，使用现有 FSID、mon config 和新建的正确 MGR auth；待 MGR active 后立即由 service spec 纳管，部署 standby，并移除临时/stray 状态。

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

## 35. 交付验收矩阵

| 域 | 必须证明的结果 | 主要命令/探针 |
|---|---|---|
| 控制面 | MON quorum、MGR active+standby、cephadm available | `ceph -s`、`quorum_status`、`mgr dump`、`orch status` |
| 主机 | 名称、SSH、runtime、时间、网络、标签正确 | `host ls --detail`、`check-host` |
| 声明 | 所有 service spec 可导出并与交付配置一致 | `orch ls --export` |
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
  R --> A[客户验收]
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

## 37. 官方事实基线与许可

本文覆盖 Tentacle `doc/cephadm/` 的直属模块：

- `index.rst`、`compatibility.rst`、`install.rst`；
- `host-management.rst`、`operations.rst`、`troubleshooting.rst`；
- `services/index.rst`、`services/osd.rst`；
- `services/mon.rst`、`mgr.rst`、`mds.rst`、`rgw.rst`、`nfs.rst`；
- `iscsi.rst`、`smb.rst`、`monitoring.rst`；
- `mgmt-gateway.rst`、`oauth2-proxy.rst`；
- `snmp-gateway.rst`、`tracing.rst`、`custom-container.rst`；
- `certmgr.rst`、`client-setup.rst`、`upgrade.rst`、`adoption.rst`。

事实版本：Ceph 官方 Tentacle 快照提交 `76fba24cef67d9219f97eeaa68cd1a848da3f2b2`。正文对官方重复内容按生产生命周期重新编排，保留默认值、限制、警告、破坏性边界和失败恢复语义。Ceph Authors and Contributors，文档许可 CC BY-SA 3.0。

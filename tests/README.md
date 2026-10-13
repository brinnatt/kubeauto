# Enterprise regression handbook

## Redis 独立交付分路

Redis 的验收权威为 `redis-test-matrix.yaml`，产品入口是目标部署目录的
`config.yml` 与 `kubecli setup redis-gate 07 --tags redis`。Cluster 使用独立部署
目录 `redis-cluster-gate`，保留源 Sentinel 数据边界。不得借用其它中间件或核心
矩阵的 PASS，也不得用安装 smoke 签收 24 项完整验收。

先执行 `--redis-static-only`，再执行只读 `--redis-artifacts`。镜像必须已经具备
TalkEdu/Docker Hub 双架构发布证据，现场不能临时发现新的公共镜像源。
状态、日志、退出码和失败诊断由总运行器的 durable wrapper 管理；聚焦模块
只复用身份、服务、数据和无故障残留均已核验的 runner 基线。

| 固定入口 | 证据边界 |
| --- | --- |
| `--redis-full-clean-verify` | 完整门禁父租约、durable 子阶段与只读清理验证的最窄资格；不安装环境、不执行业务模块、不签收完整回归 |
| `--redis-cleanup-qualification` | 实际清理 CLI 的正常、外部资源拒绝和真实中断后重入；限定 UUID 命名空间，保留外部 canary，随后回收全部测试材料 |
| `--redis-platform` | 六节点身份、三个可调度故障域、时钟偏差、Pod 内 DNS，以及独立 CSI PVC 读写/扩容/保留与回收 |
| `--redis-install-only` | 专属核心/CSI/Redis 安装；保留健康基线，不是完整交付 |
| `--redis-sentinel-data` / `--redis-sentinel-security` | TLS 业务发现、读写、TTL，以及错误凭据/CA/ACL 拒绝 |
| `--redis-sentinel-failure` / `--redis-sentinel-quorum` | 单主切换、仲裁拒绝与恢复 |
| `--redis-maintenance-rolling` / `--redis-maintenance-drain` | 两组 StatefulSet 顺序滚动、PDB 与 Eviction API drain；临时借用已租约 master 134 补齐故障域，恢复 taint/调度、CSI、主机服务和限定数据目录 |
| `--redis-tls-rotation` / `--redis-password-rotation` / `--redis-idempotence` | 维护项的部分证据，不单独签收整个维护项 |
| `--redis-persistence` / `--redis-backup` | AOF 部分证据；产品快照与独立 RDB 恢复分别核验 |
| `--redis-capacity-lag` / `--redis-capacity` | 先最窄复制滞后，再完整内存、磁盘和 PVC 扩容 |
| `--redis-observability` / `--redis-performance` | 实际抓取/告警与固定负载的业务、延迟和资源证据 |
| `--redis-stability-faults` / `--redis-stability` | 先限定 Pod、两个 Operator 进程和白名单 worker 真重启；再至少 30 分钟 Sentinel/Cluster 持续业务、持久化与数据/PVC 保留，不以故障准备项代替 soak |
| `--redis-upgrade-prepare` / `--redis-upgrade-sentinel` | Sentinel-only 固定旧夹具准备，以及 config+kubecli 的 0.26→0.27 CRD/控制器与 8.10.1→8.10.2 滚动升级和限定版本对回退；结束保留当前固定版本 |
| `--redis-upgrade-cluster` | 固定 3 主 6 副本、受保护控制器下的 8.10.1↔8.10.2 修订镜像滚动迁移；逐成员版本、绝对 TTL、槽位、持久化和 PVC 保留，结束恢复当前镜像 |
| `--redis-crd-upgrade` | 显式产品请求更新固定官方 Chart 的四项 CRD，拒绝共享消费者并保存 schema 备份，核验 Sentinel/Cluster 数据与 PVC；这是最窄更新路径，不替代实际版本升级/回退验收 |
| `--redis-cluster-install` / `--redis-cluster-data` | 独立 Cluster、真实复制图、完整槽位、MOVED/ASK/事务边界 |
| `--redis-cluster-snapshot` | 独立一次性产品请求目录，三主 RDB 的 CRC/SHA256、完整无重叠槽范围及源 PVC 保留；不替代 Cluster 全拓扑灾备恢复 |
| `--redis-cluster-primary-fault` / `--redis-cluster-failure` | 先主故障夹具，再副本重建、多数派拒写和完整恢复 |
| `--redis-cluster-rebalance-seed` / `--redis-cluster-rebalance` | 先种子、确认、TTL 与清理，再产品驱动的 3→4→3 |
| `--redis-cluster-repair` | 限定旧故障测试键恢复，不签收任何故障或扩缩容项 |
| `--redis-cluster-clean` | 核验身份与故障 journal 后，回收失败的独立 Cluster；可限定恢复尚未注入故障的 stability seed 阶段，保留源 Sentinel 业务、PVC 和共享依赖，不签收安装或完整清理项 |
| `--redis-cluster-config` | 通过产品入口验证两角色启动配置、真实挂载和有效参数，以及变更后的数据/PVC 保留；不替代完整故障、扩缩与交付验收 |
| `--redis-cluster-probe` / `--redis-dns-probe` | 只读资源、真实成员与 DNS 诊断，不是业务 PASS |

上述入口都通过 `bash tests/run_enterprise_regression.sh MODE </dev/null` 调用。
不得直接启动远端 helper，或以临时 SSH/Helm 序列绕过租约、预检和证据采集。
Sentinel 密码轮换须先于共享认证 Secret 的独立 Cluster 安装；轮换门禁在修改前
拒绝其它 Redis 声明共享待轮换 Secret。Cluster 使用独立 CA，不能据此假定密码和 ACL 也独立。
Cluster 进程故障必须从所属白名单节点的宿主 PID namespace 发信号，核验精确
容器与进程身份以及实际 `T` 状态；容器内 PID 1 信号返回成功不足以证明暂停。

每个新断言先过对应确定性契约，再运行最窄分支；该分支通过后才能执行完整
模块。模块 PASS 需要业务标志、scoped clean、durable `rc=0` 和零失败标志。
最终 `--redis-only` 还必须重新建立干净现场、覆盖所有矩阵项、完成限定回收并
取得 `REDIS_CLEAN_VERIFY_PASS` 与 `LAB_CLEAN_VERIFY_PASS`。若完整场景实现不齐，
运行器必须明确拒绝交付，不将已完成模块的合集包装为完整回归。

`tests/` is the executable delivery knowledge base for kubeauto. It captures the reusable lessons from the Cursor-era regression work and the 2026-07-25 through 2026-07-29 Codex full-chain run. The source of truth is executable evidence, not a narrative claim or an old log.

## Acceptance contract

A delivery run is complete only when all of the following are true:

- the affected unit and contract tests pass;
- the current live scenario emits its documented success marker;
- the durable wrapper records `rc=0`;
- the supervisor reports zero failure markers;
- `enterprise-test-matrix.yaml` has no affected pending or failed item;
- the six-repository compatibility tests pass;
- final cleanup emits `LAB_CLEAN_VERIFY_PASS`.

Before any enterprise gate starts, the runner executes
`helpers/validate-test-matrix.py`. This parses the YAML and recalculates every
Tier total/status count and the overall `N/N` declaration from the item
details. It rejects stale summaries, duplicate IDs, missing/unknown statuses,
or any non-pass item; a delivery gate cannot emit PASS while this check fails.

Do not mark a matrix item pass from historical evidence. A Pod merely existing or showing `Running` is insufficient when the component has multiple containers, asynchronous custom resources, readiness conditions, data-path checks, or a required business read/write operation.

For a newly created Kubernetes cluster, Node/Pod `Ready` is also insufficient.
The fixed live gate must deploy an application, satisfy its readiness probe,
resolve a Service through cluster DNS, complete an HTTP write/read through the
ClusterIP data path, and emit `KUBERNETES_PRODUCTION_SMOKE_PASS`.  The reusable
implementation is `helpers/kubernetes-production-smoke.sh`.

## Laboratory topology

| Role | Address | Account | Contract |
| --- | --- | --- | --- |
| Development only | 192.168.47.129 | brinnatt | Edit and sync only; never a test node |
| Jumper | 192.168.47.130 | root | Rocky 8.10, Ansible 2.16 control path |
| Workers | 192.168.47.131-133 | root | Rocky 8.10, small memory, reserved disabled |
| Masters | 192.168.47.134-136 | root | Rocky 8.10, small memory, reserved disabled |
| Debian node | 192.168.47.128 | brinnatt + sudo | Debian compatibility |
| Reserved/runtime | 192.168.47.137 | root | Rocky 8.10, 8 CPU / about 16 GiB currently provisioned; live 2 CPU + 4 GiB reservation gate |
| AIO/control/reserved | 192.168.47.138 | ubuntu + sudo | Ubuntu AIO and primary regression control |
| Ceph OS qualification | 192.168.47.145 | root | Rocky Linux 10.2; Ceph qualification only |
| Ceph OS qualification | 192.168.47.146 | ly + sudo | Ubuntu 24.04; Ceph qualification only |
| Ceph OS qualification | 192.168.47.147 | ly + sudo | Ubuntu 26.04; Ceph qualification only |
| Ansible compatibility | 192.168.47.141 | root | Anolis OS 23.3 clean-snapshot control path |
| Ansible compatibility | 192.168.47.142 | root | openEuler 22.03 LTS-SP4 clean-snapshot control path |
| Ansible compatibility | 192.168.47.143 | root | openSUSE Leap 16.0 clean-snapshot control path |
| Ceph-priority shared | 192.168.122.135, .40, .72 | root | ceph-01 through ceph-03; three additional test disks per host |
| Ceph-priority shared | 192.168.122.212, .165, .238 | root | mceph-01 through mceph-03; three additional test disks per host |

The current external load-balancer VIP is `192.168.47.250:8443`. Address 140
belongs to a retired kubeauto lab layout and must not appear in active
regression configuration. Address 147 is reserved exclusively for the Ceph
Ubuntu 26.04 qualification profile and must not be reused by the core or
other middleware topology. Addresses 141-143 are dedicated compatibility
controls and must not be silently reused as ordinary Kubernetes nodes. A
negative unit-test fixture may contain a retired address only to prove that it
is removed.

The six Ceph-priority hosts are shared, lease-controlled capacity rather than a
permanent exclusive allocation. Ceph tests get first use because each host has
three additional disks intended for destructive storage scenarios. Every
runner must resolve and record the exact disk serial/WWN allowlist before any
write, and must release the host lease only after scoped cleanup verification.

The production reserved-resource sizing baseline remains 16 CPU / 32 GiB. The
smaller 137 lab host validates the effective 2 CPU + 4 GiB Allocatable delta and
cgroup placement; it must not be reported as proof that the lab itself has 32 GiB.

SSH keys are the primary authentication path. The scripts use `BatchMode=yes` for SSH/SCP/rsync. A snapshot restore may remove the installed key on 141-143; the fixed runner invokes `tests/helpers/lab-ssh-bootstrap.sh`, which accepts the already-authorized credential only through the runtime `LAB_SSH_PASSWORD` environment variable and then returns to `BatchMode=yes`. Do not copy that credential into files, command output or retained logs.

## One top-level runner

Run all lab work from the development host through:

```bash
bash tests/run_enterprise_regression.sh --all-delivery
```

`--all-delivery` is the unattended final-signoff sequence. It composes the
already focused and cleanup-safe modes in this order: jumper, nerdctl, Docker,
Kubernetes patch upgrade, the delivery-gaps full chain (which includes the
authoritative core topology regression), the Rocky 8 customer-binary build,
cross-distribution Ansible gates, and the nine Tier3 frozen-tool gates. A failed
sub-mode stops the sequence after that mode's mandatory cleanup.

Useful fixed modes:

```bash
bash tests/run_enterprise_regression.sh --status
bash tests/run_enterprise_regression.sh --follow
bash tests/run_enterprise_regression.sh
bash tests/run_enterprise_regression.sh --aio-only
bash tests/run_enterprise_regression.sh --jumper-only
bash tests/run_enterprise_regression.sh --nerdctl-only
bash tests/run_enterprise_regression.sh --docker-only
bash tests/run_enterprise_regression.sh --registry-reboot-only
bash tests/run_enterprise_regression.sh --upgrade-only
bash tests/run_enterprise_regression.sh --gaps-only
bash tests/run_enterprise_regression.sh --diagnose-gaps-last
bash tests/run_enterprise_regression.sh --ansible-os-probe
bash tests/run_enterprise_regression.sh --ansible-anolis-probe
bash tests/run_enterprise_regression.sh --ansible-anolis-container-probe
bash tests/run_enterprise_regression.sh --ansible-os-only
bash tests/run_enterprise_regression.sh --build-rocky8-kubecli
bash tests/run_enterprise_regression.sh --tier3-tools-only
bash tests/run_enterprise_regression.sh --mysql-only
bash tests/run_enterprise_regression.sh --mysql-status
bash tests/run_enterprise_regression.sh --mysql-follow
bash tests/run_enterprise_regression.sh --kafka-only
bash tests/run_enterprise_regression.sh --kafka-status
bash tests/run_enterprise_regression.sh --kafka-follow
bash tests/run_enterprise_regression.sh --kafka-clean-only
bash tests/run_enterprise_regression.sh --ceph-probe
bash tests/run_enterprise_regression.sh --ceph-os-probe
bash tests/run_enterprise_regression.sh --ceph-qualification-network-only
bash tests/run_enterprise_regression.sh --ceph-prepare-hosts-only
bash tests/run_enterprise_regression.sh --ceph-os-qualification-legacy
bash tests/run_enterprise_regression.sh --ceph-os-qualification-modern
bash tests/run_enterprise_regression.sh --ceph-os-qualification-rocky9
bash tests/run_enterprise_regression.sh --ceph-lab-bootstrap
bash tests/run_enterprise_regression.sh --ceph-lab-python-bootstrap
bash tests/run_enterprise_regression.sh --ceph-lab-runtime-bootstrap
bash tests/run_enterprise_regression.sh --ceph-supply-chain-only
bash tests/run_enterprise_regression.sh --ceph-slow-osd-fixed
bash tests/run_enterprise_regression.sh --ceph-observability
bash tests/run_enterprise_regression.sh --ceph-security
bash tests/run_enterprise_regression.sh --ceph-only
bash tests/run_enterprise_regression.sh --ceph-status
bash tests/run_enterprise_regression.sh --ceph-follow
bash tests/run_enterprise_regression.sh --ceph-clean-only
```

`--mysql-only` owns the independent Percona PXC matrix in
`mysql-test-matrix.yaml`. It uses the dedicated six-node MySQL laboratory,
does not enter the delivered core topology or `--all-delivery`, and always
performs scoped PXC cleanup and verification before and after the run.

`--kafka-only` owns the independent Strimzi/Kafka matrix in
`kafka-test-matrix.yaml`. It uses the approved six-node Kafka laboratory,
does not enter the delivered core topology, PXC branch, or `--all-delivery`,
and always performs Kafka-owned cleanup and verification before and after the
run. Use `--kafka-status` and `--kafka-follow` for durable state and live logs;
use `--kafka-cancel` only to stop the Kafka-owned durable job.

`--ceph-only` owns the independent Cephadm/Ceph-CSI matrix in
`ceph-test-matrix.yaml`. The six Ceph-priority hosts provide standalone storage;
the default control is `root@192.168.47.130`, which can reach both lab subnets.
Kubernetes uses masters/etcd `192.168.47.134-136` and workers
`192.168.47.131`, `192.168.47.132`, `192.168.47.137`, with reserved resources
disabled for this branch. The two pools are
leased separately and must pass read-only clean checks before installation.
The existing `k8s-dev` cluster in the supplemental Rocky 9 pool is not touched.
Every destructive device is bound to a current serial/WWN allowlist on storage
hosts only. Product destroy validates its inventory against the leased compute
hosts; Ceph FSID purge and disk checks never target compute hosts.
Default host-network ports prevent unmodified co-location: RGW and RBD CSI
metrics both use `8080`, and Ceph Alertmanager and Calico controllers use `9094`.
This branch validates standalone storage plus external CSI, not hyperconvergence.
`--ceph-probe` is read-only. `--ceph-os-probe` separately checks the six real OS
profiles and their selected kernel client capabilities without marking OS
qualification PASS. Ubuntu 26.04 requires the pinned Ansible 2.20.9 execution
image for Python 3.14; dual-published manifests are a live qualification
prerequisite. If a snapshot reset leaves one of the six
authoritative Ceph hosts named `localhost`, `--ceph-lab-bootstrap` may restore
only its registered hostname; it refuses to overwrite any other identity and
does not touch storage devices. The focused modes, beginning with
`--ceph-slow-osd-fixed`, run one owning scenario and verify its cleanup scope.
Prefer a healthy runner-owned baseline for focused repairs: verify its FSID,
product dependencies, node/resource identities, credentials, business hashes
and absence of injected faults before reuse. Restore the module's own faults
and fixtures without rebuilding unrelated prerequisites; if isolation cannot
be proved, rebuild the smallest affected environment. They remain blocked until the
ext-bin 1.16.0 and image catalogs have matching TalkEdu/Docker Hub digests
by default; the ext-bin image contains the checksum-pinned official cephadm.
The runner writes `ceph_csi_install: "no"` into its generated `config.yml` before
the product `08` entry converges standalone Ceph, then restores `"yes"` before
the normal `90` entry deploys Kubernetes and reconciles Ceph/CSI. This exposes
independent Ceph prerequisite failures before the more
expensive platform setup without omitting the combined product path.
Before enabling Ceph, the same product `08` entry must succeed with the default
disabled configuration, without gathering facts or creating any FSID/owner marker.
Publish each completed module as `pass` after its marker, durable `rc=0`, zero
failure markers and declared cleanup scope are verified. Bind evidence to the
product and owning test dependencies; a related change returns only affected
items to `pending`. Matrix progress, documentation and unrelated test changes
do not invalidate completed business evidence. Keep the aggregate delivery
result `PENDING` until one clean complete `--ceph-only` run passes all 28 cases,
the full case ledger, durable exit and final `LAB_CLEAN_VERIFY_PASS`.
`--ceph-only` also requires current slow-OSD and six-OS qualification evidence.
`--ceph-security` exercises the same product `08`/`90` path and business fixtures,
then verifies wrong CephX/S3 credentials and cross-pool denial before owned cleanup.
The pinned mc JSON error code proves S3 authentication rejection; a connection,
client-argument or unrelated service error cannot satisfy the negative test.
The upgrade gate requires both exact target daemon versions and the official
orchestrator's `in_progress=false`; version convergence can precede daemon-key
rotation and monitoring redeployments.
The pinned [orchestrator CLI](https://github.com/ceph/ceph/blob/v20.2.4/src/pybind/mgr/orchestrator/module.py)
returns `There are no upgrades in progress currently.`
at completion even with `--format json`; the gate accepts that exact terminal
response as well as an explicit completed status object, and rejects other text.
Its bounded 90-minute wall-time budget
includes CLI time, and a budget expiry records the last upgrade status for
classification rather than certifying a product failure from the timeout alone.
`--ceph-probe` and the focused/full prerequisites require the actual compute
`libceph` module to support secure msgr2 before creating fault devices or
deploying storage. The default `aes256k` credential additionally requires the
new key support (Linux 7.0 or a verified vendor backport). The explicit legacy
`aes` compatibility profile still uses GA kernel RBD/CephFS and secure msgr2;
the runner records its CVE-2025-30156 warning and must verify real mount,
read/write, recovery and performance behavior. Ceph-CSI is pinned to v3.17.1.
A module preflight is not business evidence. The runner never selects Alpha
RBD-NBD or silently downgrades the selected credential policy.
The monitoring focused gate preserves explicitly accepted legacy-key warnings.
It matches the aggregate health gauge and every active per-check gauge against
Ceph health JSON, rejects unrelated warnings, and verifies the controlled
`OSDMAP_FLAGS` gauge changes from inactive to active and back to zero through
one runner-owned `noout` maintenance flag, which must be absent at baseline.
The aggregate returns to its accepted baseline; security warnings are not muted.
After an upgrade, the gate waits for the official
`AUTH_INSECURE_ROTATING_SERVICE_KEY_TYPE` transition to expire naturally before
sampling that baseline. Waiting requires exact target daemon versions, completed
orchestration, an active `aes256k` service cipher and no unrelated health checks.
The fixed lab permits at most three service-ticket TTL intervals plus two minutes
(default TTL: one hour). It neither shortens TTL nor wipes rotating keys; the final
baseline still rejects the transition warning. See the official
[health-check procedure](https://docs.ceph.com/en/tentacle/rados/operations/health-checks/#auth-insecure-rotating-service-key-type).
The final Ceph gate additionally requires current real-host qualification for
Rocky 8/9/10 and Ubuntu 22/24/26 in `os_qualification`; Ubuntu 20 is outside
the supported qualification set because it is end-of-life. Its SHA256-bound
evidence records the OS, kernel, Python, runtime, actual product command,
orchestrator remote convergence, secure RBD/CephFS read-back, idempotence,
durable exit and cleanup. A container package-profile test or the fixed
Rocky 9 storage/Rocky 8 compute lab cannot certify another OS version. Missing
profiles block final sign-off before a new full-chain launch; focused work
remains available. Matrix status/evidence changes do not invalidate the OS
source binding (`ceph_gate_fingerprint --without-matrix`); product/gate changes do.
The OS qualification modes own three exact inventories. The legacy round uses
Rocky 8 and Ubuntu 22 with explicitly accepted `aes`; the modern round uses
Rocky 10 and Ubuntu 24/26 with `aes256k`, and the rocky9 round qualifies Rocky 9
with `aes256k` on the Ceph-priority pool. Each Kubernetes topology remains
within one reachable lab subnet; modern hosts use the product's supported
overlapping master/worker groups. Rocky 9 also overlaps master/worker groups
on the last three storage hosts, keeping the RGW hosts outside Kubernetes:
the official host-network RBD metrics and default RGW both bind TCP/8080,
and cordon does not exclude DaemonSets. The existing `.122.2` Kubernetes
cluster is not used. Qualification deliberately co-locates selected MGR or
non-RGW OSD fixtures with CSI clients on the Ceph-priority pool and authorized
OS controls, disables unconstrained monitoring placement, and preserves
hostnames (restoring the temporary Rocky 10 localhost fixture). This fixture
is not a customer hyperconvergence qualification. Each selected host must run
an orchestrator-deployed MGR and a node-pinned CSI Pod, verify both secure
kernel mounts, persistent hashes after detach/remount, 16 fsync samples with
zero errors, and an idempotent second product execution preserving PVC/Pod
and CSI credential identity. Draft JSON is published to ignored
`logs/ceph-os-qualification/` and bound to the matrix only after the supervisor
verifies current source, durable `rc=0`, zero failure markers and scoped cleanup.
The qualification runner probes storage-node-to-OS-host TCP/22 separately from
control SSH. For this NAT-separated lab only, failed management paths use
runner-owned loopback SSH reverse forwards from the authorized control and
exact `/32`, TCP/22 OUTPUT redirects. Ceph data traffic is not redirected.
The journal precedes mutation; cleanup removes the exact tagged rules, SSH
control processes and listeners before the clean marker. Customer configuration
and production SSH policy do not inherit this fixture.
The runner maintains journal-owned SSH masters throughout qualification. A lost
master is reconnected only while its exact NAT rules remain; foreign listeners
or missing rules block recovery. The network focused gate deliberately stops an
owned master, verifies listener removal, restores transport, and then cleans it.
The watcher is stopped before scoped cleanup, including handled failures.
`--ceph-qualification-network-only` proves this fixture and its cleanup under
both host leases and durable supervision before a qualification cluster launch.
`--ceph-prepare-hosts-only` exercises the exact production delegated hosts task
with eight local inventory fixtures and a private temporary file. It checks all
entries and an unrelated line after insertion and an idempotent second run.
Ansible delegation retains parallel forks, so the shared-file update uses
`throttle: 1` as prescribed by the official delegation guide. A missing node
entry can prevent API Server kubelet log/exec access even after a Job completes.
Its runtime-only `CEPH_QUALIFICATION_NETWORK_PROFILE=rocky9` selection also
verifies Registry HTTP `/v2/` from the last three Ceph hosts. If TCP/5000 to the
authorized control is unreachable, exact `/32` OUTPUT redirects use a separate
loopback reverse forward to that control's local Registry. Directly reachable
paths are untouched. Journaled rules, listeners and SSH masters are removed
before the clean marker. This lab fixture does not change customer image paths.
Qualification also rejects a cgroup-v1 host with a transient hybrid `0::/`
entry before storage mutation. Podman 4.9's official split-cgroup path prefers
that root entry and refuses it, including after a Calico cgroup2 mount is removed.
After scoped cleanup, `--ceph-lab-runtime-bootstrap` holds both host leases,
refuses live services/containers/Ceph data, reboots only affected legacy
qualification hosts, and verifies a changed boot ID and restored cgroup state.
It does not change kernel boot options, customer configuration or OCI runtimes.
An installed Ubuntu lab image may retain an enabled `file:/cdrom` source after
its installation media is removed. An `apt-get update` failure naming that
missing Release file is a package-source environment failure. Preserve the
original source and disable only the verified stale stanza using the official
APT [`Enabled: no` field](https://manpages.ubuntu.com/manpages/resolute/en/man5/sources.list.5.html);
require a successful `apt-get -o APT::Update::Error-Mode=any update` before
rerunning qualification. Keep this lab repair out of the production role.
`--ceph-lab-python-bootstrap` is an explicit lab environment recovery mode for
the six clean Rocky 8.10 compute hosts, not a production Python policy change.
It leases the pool, installs distribution-owned Python 3.12 from AppStream,
verifies RPM integrity and system-interpreter package ownership, then runs
100 standard-library import probes per host and 30 native Ansible ping rounds.
A failed prerequisite stops the mode without a success marker. The native RPM
may update an automatic `python3` alternative; the runner does not override
the distribution's links or remove the original interpreter. These probes
are environment evidence, not Ceph business or cross-OS delivery evidence.
Both Ceph-CSI charts are rendered with the product defaults before node access
bootstrap or live installation. The preflight rejects cross-release resource
ownership collisions, wrong namespaces and unowned ConfigMap mounts.
Every string in the Ceph task files also passes Jinja syntax parsing; Ansible
`--syntax-check` alone does not catch embedded shell/template delimiter conflicts.
When publication is explicitly authorized outside CI, the runtime-only
`CEPH_ARTIFACT_MODE=manual-talkedu` mode verifies every TalkEdu image manifest and
the official cephadm SHA256/version without claiming Docker Hub publication.
`CEPHADM_FILE` may supply a locally downloaded executable; the fixed runner
verifies its pinned checksum before transfer and atomically stages it after a
second remote check. Temporary download accelerators are not persisted. Both
modes keep the full business, fault, recovery and cleanup acceptance coverage.
Ceph cleanup verifies stopped Kubernetes runtimes, orphan `k8s.io` shim process
trees and known CNI links as well as Ceph and disk state. It refuses active
Kubernetes/runtime or foreign containerd namespaces. Stopping containerd alone
is not cleanup evidence: the official service uses `KillMode=process` and its
shims may keep node-cache and other host-network fixtures alive.
Before purging the storage FSID, cleanup verifies its ownership marker and
releases kernel RBD mappings whose sysfs `cluster_fsid` matches that FSID;
foreign or unbound mappings block cleanup. Only allowlisted OSD hosts use
`rm-cluster --zap-osds`; MGR-only qualification hosts do not scan or zap disks.
An unmap blocked in kernel D-state fails after a bounded wait, without waiting
again for the uninterruptible child. Preserve that failure, then verify whether
the mapping has disappeared before considering host recovery. A normal reboot
may itself wait on the blocked client; an accepted reboot request is not
recovery evidence. Require a changed boot ID, restored BatchMode SSH and zero
owned kernel mappings, then rerun `--ceph-clean-only` and require its final
`LAB_CLEAN_VERIFY_PASS` before starting another qualification round.

`run_tools_regression.sh` owns the separate `tools-test-matrix.yaml` branch.
The branch is independent from the enterprise and middleware runners: every
script under `tools/` is built and tested as a standalone CLI, with its own
import-boundary, security, functional, recovery and cleanup evidence. Before
review approval, only the non-mutating entry points are allowed:

```bash
bash tests/run_tools_regression.sh --preflight
bash tests/run_tools_regression.sh --status
```

The tools matrix contains 68 executable scenarios plus a 101-item
`functional_inventory`. The inventory is the public-function checklist for all
ten CLIs: every command/role/data path/configuration mode/remote path and
failure-recovery behavior must point to a CLI, functional, security or recovery
case. Build success and `--help` are never sufficient evidence for an inventory
item. A functional case is PASS only after its isolated fixture reaches the
documented product result, verifies read-back/state/exit markers, and completes
scoped cleanup; partial execution remains pending.

New or changed matrix cases remain `pending` until review approves fixtures,
credentials, external-service versions and live cleanup scope. The current
matrix is approved, and `--full` still requires the explicit
`TOOLS_MATRIX_APPROVED=yes` gate; Tier3 `--help` results in the enterprise
matrix are not tools delivery evidence.

### Tools artifact gate

The independent MySQL tool branch has an additional supply-chain gate. New
fixtures must normally be registered and published by
`kubeauto-ext-images-dockerfile`'s GitHub Actions dual-push workflow before a
live run. A fixture already published in TalkEdu Hub may be consumed directly
when the matrix pins its current manifest digest, owner and cleanup scope and
the delivery does not modify or republish that artifact.
Record the official source, exact version, manifest digest or file SHA256,
owner and cleanup scope in the tools matrix. The runner pulls the TalkEdu
copy first and verifies the digest before handing a fixture to a test host;
dynamic mirror discovery and repeated public downloads are prohibited.

For `MigrationCli`, the live matrix requires fixed MySQL `8.0.46`, one pinned
`8.4.x` and one pinned `9.x` logical-migration fixture. For `MyBackupCli`, use
only a MySQL/XtraBackup combination explicitly supported by Percona's
compatibility documentation. A MySQL 9.x physical-backup combination without
that official support is an explicit boundary (`na`), not a pass. GitHub
Actions may fetch upstream artifacts, but the lab must remain runnable when
public network access is unavailable.

The runner is authoritative because it centralizes SSH, source synchronization, remote launch, durable PID/exit state, foreground log streaming, heartbeat, silent-stall diagnostics, final markers, and cleanup. Do not replace it with a sequence of manually approved SSH commands.

## Required lifecycle

For every stateful run:

1. Refuse an active durable job that owns the same hosts; stop only a verified stale owned job.
2. For a focused repair, restore only its owned faults, fixtures and processes.
   Retain a healthy runner-owned baseline after verifying ownership, dependencies,
   service health, business hashes and absence of fault residue. If isolation
   cannot be proved, clean the smallest affected environment through its runner.
   Full regression uses its normal complete cleanup.
3. Require the declared scoped-clean marker before a focused run, or
   `LAB_CLEAN_VERIFY_PASS` before a full run.
4. Sync current source to the relevant control host.
5. Launch through `run-durable-gate.sh` and the top-level supervisor.
6. Stream every remote log line to the foreground console.
7. Emit a heartbeat every 30 seconds. After 60 seconds with no new log bytes, print durable state and a process-tree diagnostic.
8. On success or failure, verify the module's declared cleanup scope before reuse.
   A monitoring disconnect alone does not invalidate a durable remote workload;
   reconnect and inspect its state before recovery. A completed full regression
   always requires final complete cleanup and `LAB_CLEAN_VERIFY_PASS`.

The remote workload must survive a local SSH/tail disconnect. The `.pid` and `.exit` state files are authoritative; a missing exit record is a failed/lost run, not a pass.

## Failure and root-cause protocol

When a gate fails:

1. Preserve the exact command, current log tail, PID/exit state and cluster events.
2. Determine whether the failure is in the gate, environment, registry/artifact path, runtime, Kubernetes, controller reconciliation, or product code.
3. Consult the official source and documentation for the pinned version. Prefer vendored official charts/CRDs/source when external access is limited.
4. Reproduce from a clean baseline and change one causal layer at a time.
5. Add a focused unit/contract test for the proven root cause.
6. Run the focused live gate, then the required full chain.
7. If the attempted fix does not pass, remove the speculative change and clean its data before continuing.

Examples captured by the current gates include registry HTTP readiness instead of container state, SHA256 validation and atomic restaging of Docker runtime artifacts, asynchronous RocketMQ Broker reconciliation, real LVM/NFS read/write, official Nacos schema import with external MySQL, MinIO Tenant health, and 9/9 executed network-check jobs.

## Test-gate change protocol

Test helpers, runners, fixtures, diagnostics, templates and matrix validators
are delivery code.  They must be validated before they are allowed to consume a
clean lab.  Preserve every acceptance scenario, but execute the following
evidence ladder in order:

1. Classify the failure and change as product, test gate, environment,
   supply-chain, runtime, Kubernetes/controller, or a proven combination.
   Name the owning matrix scenario, product entry point, terminal marker and
   smallest disproof command.
2. Pass the changed artifact's lowest-cost checks first: syntax/import checks,
   focused unit and contract tests, structured-data parsing, render checks, and
   resource contracts for names, namespaces, selectors, ports, images,
   ownership, markers and durable exit state.
3. Add a deterministic regression test for a proven test-gate defect, then use
   the narrowest fixed-runner branch that executes the affected product path.
   A static or focused failure blocks the full middleware gate.
4. Complete the affected focused module gates, then run one final clean full
   middleware regression. The runner may reuse healthy owned prerequisites
   between focused modules; stale faults, foreign resources or unverifiable
   identities require cleanup of the smallest affected dependency boundary.
   The final full run requires normal complete cleanup and `LAB_CLEAN_VERIFY_PASS`.
5. On a live failure, capture the first failed command, durable exit state,
   rendered inputs, resource state, Pod descriptions/events and controller logs
   before changing code.  Abort after a failed prerequisite rather than running
   unrelated later stages to obtain secondary failures.

For each live launch, the log must identify the completed ladder level, the
evidence that unlocked it and the next permitted level.  Direct Helm, kubectl
or SSH remains diagnostic-only unless the test has already exercised the same
behavior through the documented `config.yml` and `kubecli` product entry point.
This protocol is an efficiency control, not a coverage waiver: matrix status,
durable `rc=0`, zero failure markers and final cleanup remain mandatory.

## China image-source contract

For a Docker Hub-origin fixture in an already-delivered path, preserve its explicit, reviewable fallback list. The existing gates try the pinned `docker.sparkcr.cn/<upstream-image>` accelerator before direct Docker Hub, retain the applicable TalkEdu and Huawei candidates, and keep the upstream reference last. A mirror-discovery page such as `https://status.anye.xyz/` is for operator review only; never consume its current recommendations dynamically in a delivery run.

An accelerator response is not evidence that the fixture is usable. The gate must complete pull, inspect the exact image, tag and push it into the local registry, then verify the expected registry tag/manifest before deploying a workload. Pin the real upstream tag throughout the source list, local-registry tag and Pod manifest; never retag a different patch release to impersonate the requested version.

New middleware branches do not persist dynamically discovered public accelerators. When an isolated middleware test requires temporary acceleration, inject the reviewed source through a non-persisted runtime parameter, verify the checksum or manifest digest, and remove the override after the run.

## Script map

Authoritative orchestration:

- `run_enterprise_regression.sh`: development-host owner of the full workflow and all supported modes; `--all-delivery` is the single final-signoff command.
- `run_unit_tests.sh`: isolated unit suite; writes logs to a temporary directory.
- `helpers/run-durable-gate.sh`: remote PID/exit wrapper.
- `helpers/lab-wipe-nodes.sh`: destructive lab cleanup plus independent verification.
- `helpers/sync-kubeauto.sh`: controlled source synchronization.
- `helpers/lab-control-ssh-bootstrap.sh`: key-only control-to-node access bootstrap; it transfers only the control public key and never persists a fallback password or copies a private key.

Current live gates:

- `helpers/regression-full.sh`: core topology, lifecycle, CNI, addon and reserved baseline.
- `helpers/regression-jumper.sh`: jumper/Ansible 2.16 path.
- `helpers/nerdctl-gate.sh`: containerd/nerdctl contract.
- `helpers/delivery-docker-gate.sh`: Docker, buildx, Compose, cri-dockerd and artifact recovery.
- `helpers/registry-reboot-gate.sh`: local Registry restart policy, real control-host reboot recovery and persisted-manifest integrity.
- `helpers/delivery-upgrade-smoke.sh`: real Kubernetes patch transition.
- `helpers/delivery-gaps-fullchain.sh` and `helpers/delivery-gap-retest.sh`: remaining storage, messaging, registry, observability and network workload gates.
- `helpers/build-tools-rocky8.sh` and `helpers/tier3-tools-gate.sh`: build every frozen tool on glibc 2.28, execute the exact outputs on real Rocky 8, and remove scoped build/runtime residue.

Recovery and historical phase scripts are retained because they encode useful focused diagnostics and safe resume points. They are not alternate top-level sign-off commands. Before reusing one, verify that its preconditions still match the matrix and run it through the same durable supervision and cleanup contract.

Retained focused/recovery assets:

- registry and image preparation: `bootstrap-brinnatt-mirrors.sh`, `registry-ready-gate.sh`, `registry_blob_integrity.py`;
- storage fixtures: `prep-node-lvm-loop.sh`, `prep-nfs-server.sh`, `mk-cluster-133.sh`;
- reserved-resource focus: `reserved-4g-gate.sh`, `rocky-reserved-gate.sh`, `run-reserved-fullchain.sh`, `verify-node-reserved.sh`;
- phase/resume diagnosis: `regression-phase3.sh`, `regression-remainder.sh`, `regression-resume.sh`, `regression-resume-g2.sh`, `regression-resume-g3.sh`, `resume-from-g8.sh`, `resume-g8b-only.sh`;
- superseded launch/smoke paths retained for logic reference: `regression.sh`, `start-full-regression.sh`, `delivery-gap-smoke.sh`.

The last group must not be used for final sign-off: it predates parts of the current durable supervisor and full cleanup contract. Reuse useful checks by migrating them into the authoritative runner/gates with unit coverage.

## Matrix state transitions

Before implementation, set only affected entries to `pending` and record the
changed dependency. Publish a module as `pass` as soon as its complete current
product evidence, durable `rc=0`, zero failure markers and declared scoped cleanup
are verified; do not wait for unrelated modules or the final full run. Unknown
dependencies require impact analysis before reuse. After a completed gate, update:

- the item status and evidence;
- its execution-group status;
- coverage totals;
- `meta.updated`, `meta.regression_log` and `meta.regression_result`;
- the overall PASS/PENDING/FAIL assessment.

Validate YAML parsing and count every `status: pass`, `status: pending`, and `status: fail`. Tier3 scope-defined skips remain explicit and are not silently counted as pass.

## Logs and retained knowledge

Runtime logs belong under `logs/` and are ignored by Git. They are transient diagnostic evidence and may include host-specific or obsolete data. Do not commit them.

When a log teaches a reusable lesson, convert that lesson into one or more of:

- a unit or contract test;
- a bounded helper/gate;
- an enterprise-matrix evidence entry;
- this handbook or another maintained document.

Then remove the raw log when it is no longer needed for the active investigation.

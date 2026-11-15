# kubeauto 首跳升级操作手册：v1.33.6 到 v1.34.13

## 适用范围

本文适用于已经通过 GA 验收的 kubeauto `v1.33.6` 集群，目标是同一大版本
连续的下一 minor `v1.34.13`。执行人需要具备目标集群管理员权限，使用目标分支
内的 `kubecli`，并在升级目录保存本次 run 的输入、日志和证据。本文描述的是
项目升级入口和本次已冻结的组合；它不把上游的最低版本声明写成逐版认证，也不
代表客户签收。

目录：先读本节和“前置检查”，再执行“推荐主线”；异常时进入“停止与恢复”，
完成后按“验收与交付”判断结果。组件自身的用户、运维、开发手册仍是组件能力的
权威来源，本手册只说明它们在本次 Kubernetes 首跳中的边界。

## 版本组合和证据边界

| 类别 | 源 | 目标或处理 | 依据和边界 |
| --- | --- | --- | --- |
| Kubernetes 六项二进制 | `v1.33.6` | `v1.34.13` | Kubernetes stable-1.34、发布校验和及版本偏差资料；只允许相邻 minor |
| Calico | 已 GA 组合 | `v3.28.4` 保持 | 官方 manifest/API 资料与项目制品证据；本手册不替代 Calico 自身升级说明 |
| etcd | `3.6.4` | `3.6.16` | 官方同 minor patch 迁移资料；逐成员串行，保持 member、revision、TLS 和数据目录 |
| containerd（迁移路径） | `2.1.4` | `2.2.10` 后 `2.3.7` | 官方相邻 minor/CRI 配置资料和项目冻结包；只在 drain 后、kubelet 前执行 |
| containerd（干净新建） | 当前 GA 安装包 | `2.1.4` | 新建安装复用当前 GA 包；不将迁移桥接版本冒称为新建安装版本 |
| CoreDNS、node-local-dns、metrics-server | 已 GA 组合 | 保持项目冻结版本 | 目标 API、manifest 和 metrics API 可用性核验；不因 Kubernetes minor 自动升级全部附件 |
| Dashboard、Harbor | 已 GA 组合 | 首跳保留 | 官方仓库归档或当前版本已停止维护；这是有明确限制的过渡，不表示长期受支持 |
| kube-router、Kube-OVN、NFS、RocketMQ | 已 GA 组合 | 首跳保留 | 上游缺少逐版 1.34 测试表时，依据官方稳定 API/最低版本/源码契约，加项目最小集成验证；不冒称上游逐版认证 |

完整的版本、制品 digest、官方来源 ID 和组件决策见
[`release.yaml`](release.yaml)、[`research.yaml`](research.yaml) 和
[`test-matrix.yaml`](test-matrix.yaml)。目标证据只能引用
[`evidence/`](evidence/) 内的副本；历史 `v1.33.6` 证据可以复用，但复用不等于
本轮重新实测。

## 前置检查

1. 在 `v1.34.13` 分支工作，确认源集群确实为 `v1.33.6`，所有 API Server 和
   kubelet Ready，控制面和业务账本健康。不得从正在运行的外来集群接管 journal。
2. 建立并校验 etcd 快照和业务恢复点。保存快照路径、字节数和 SHA256；仓库不
   保存证书、密钥或完整 Secret。恢复点缺失、不可读或未确认时停止。
3. 核对固定的六仓源码、目标二进制和实际消费镜像身份。禁止使用 `latest`、未
   记录 digest 的临时镜像或未经核验的代理地址。
4. 核对容量、PDB、卷/本地数据约束、时间同步和独占资源租约。小内存实验主机
   不得代替客户 sizing 基线；没有租约不得变更共享主机。
5. 使用只读状态命令确认矩阵前置：

   ```bash
   .venv/bin/python -B upgrade/validate.py upgrade/v1.34.13/test-matrix.yaml --status
   .venv/bin/python -B upgrade/validate.py upgrade/v1.34.13/test-matrix.yaml --final-prerequisites
   ```

   `--final-prerequisites` 只做变更前拒绝检查，不执行升级。任何 pending、fail、
   stale 或组合身份不一致都必须先处理，不能用上一次成功标志代替当前输入。

## 推荐主线

以下命令必须从产品入口执行。`CLUSTER` 是 kubeauto 已管理的源集群名，不能把
下面的顺序改成直接运行 Helm、kubectl 或 SSH 脚本。

```bash
export CLUSTER=<已通过前置检查的集群名>
kubecli checkout "$CLUSTER"
kubecli backup "$CLUSTER"
kubecli upgrade "$CLUSTER"
```

产品入口按以下边界推进，并在每个边界保存 journal 和读回结果：

1. 先观察全部实际 API 后端、集群 UID、节点 UID、控制组件和运行时身份；输入与
   journal 不匹配时在任何变更前拒绝。
2. 依官方偏差约束逐个更新 API Server。每个后端必须通过目标版本和 `readyz`，
   全部后端达标前不得更新 controller-manager、scheduler 或 kubelet。
3. 全部 API 后端达标后更新控制组件，再逐节点执行 `cordon → policy/v1
   eviction drain → 运行时桥接（如适用）→ kubelet/proxy → Ready → 恢复原调度状态`。
   PDB、emptyDir、本地卷或业务账本不允许用强删绕过。
4. 每个节点失败都停止当前依赖路径，保留 cordon、首命令、事件、Pod 描述、控制器
   日志和 durable journal；不扩大批次、不自动降级、不盲目 uncordon。
5. 所有节点完成后核对 API、controller、scheduler、kubelet、proxy、runtime、
   CNI、DNS、metrics API 和业务账本。第二次相同入口执行只能读回并保持身份，不能
   重建证书、etcd member 或业务数据。

## 停止、续跑和恢复

失败先分类为产品、测试门禁、环境/供应链、运行时或 Kubernetes/controller 问题。
保留首失败和 durable 状态后只修复已证明的因果层；修复测试门禁时先补最窄合同，
不能直接重启完整链。续跑前必须再次核对 cluster UID、source/target、输入 fingerprint、
节点 UID、已完成步骤和无故障残留。journal 不匹配时拒绝接管。

Kubernetes 和运行时二进制升级存在不可逆边界。项目不承诺通过简单降级恢复已经
写入的新 API 对象或数据格式；需要恢复时使用已验证的完整恢复点，并重新确认外部
业务一致性。etcd 快照只证明控制面数据恢复点，不自动证明数据库、消息、对象存储
或卷内业务数据的一致性。恢复后重新执行健康、身份、数据账本和安全检查。

## 完成判断和交付

目标版本的验收主线是 `test-matrix.yaml` 的 `UPG-01` 至 `UPG-15`。矩阵 PASS 只
表示对应证据已满足本项目边界；官方资料、项目集成实测和客户签收分别记录。最终
干净回归必须在所有前置父项通过后由固定 launcher 执行一次，取得 durable `rc=0`、
零失败标志、完整成功标志和 `LAB_CLEAN_VERIFY_PASS`。不能把 UPG-13 的新建安装、
历史 GA 证据或某个子模块 PASS 拼成最终 PASS。

交付前保留：目标分支和六仓 SHA、版本/镜像 digest、journal、首失败、恢复点摘要、
原始日志 SHA256、矩阵状态快照和清理验证。`product_acceptance` 与 `customer_signoff`
在评审完成前保持空值；本手册不代替客户签收。

## 官方资料

- [Kubernetes releases and downloads](https://kubernetes.io/releases/)、
  [Version skew policy](https://kubernetes.io/releases/version-skew-policy/)
- [Kubernetes upgrade tasks](https://kubernetes.io/docs/tasks/administer-cluster/cluster-upgrade/)
- [containerd releases](https://github.com/containerd/containerd/releases) 与
  [containerd configuration](https://github.com/containerd/containerd/blob/main/docs/cri/configuration.md)
- 本次已读取并固定的官方页面副本见
  [`official-snapshots/2026-11-14-first-hop/`](../official-snapshots/2026-11-14-first-hop/)，
  来源身份和访问日期见 [`official-sources.yaml`](../official-sources.yaml)。

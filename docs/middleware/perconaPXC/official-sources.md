# 官方依据与版本基线

> **文档版本：** v1.1（独立 MySQL/PXC 分路交付版）
> **最后核验：** 2026-08-20
> **适用范围：** Percona Operator for MySQL/PXC v1.20.0 文档集的版本、源码和官方功能证据
> **维护要求：** 版本升级时必须重新核对 Release、Chart tag、CR/CRD 和 supported software，不能只修改版本字符串。

本页是 PXC 文档的证据索引。文档结论以锁定版本的官方源码和官方发布说明为准，不以 `main` 分支或动态镜像标签推断生产行为。

## 目录

1. 版本锁定
2. 必读官方资料
3. 证据使用规则

## 版本锁定

| 层级 | 版本 | 官方证据 | 说明 |
|---|---|---|---|
| Operator | `1.20.0` | [GitHub release](https://github.com/percona/percona-xtradb-cluster-operator/releases/tag/v1.20.0) | `draft=false`、`prerelease=false`；GA 发布 |
| Operator Helm chart | `1.20.0` | [`pxc-operator/Chart.yaml`](https://raw.githubusercontent.com/percona/percona-helm-charts/pxc-operator-1.20.0/charts/pxc-operator/Chart.yaml) | 独立 tag `pxc-operator-1.20.0`，`appVersion=1.20.0` |
| PXC | `8.4.8-8.1` | [官方 v1.20.0 CR](https://raw.githubusercontent.com/percona/percona-xtradb-cluster-operator/v1.20.0/deploy/cr.yaml) | 官方示例和支持矩阵版本 |
| XtraBackup | `8.4.0-5.1` | [官方 release supported software](https://github.com/percona/percona-xtradb-cluster-operator/releases/tag/v1.20.0) | 与 PXC 8.4 匹配 |
| HAProxy | `2.8.18-1` | [官方 CR 默认镜像](https://raw.githubusercontent.com/percona/percona-xtradb-cluster-operator/v1.20.0/deploy/cr.yaml) | 默认代理路径 |
| Fluent Bit | `5.0.6-1` | [官方 release supported software](https://github.com/percona/percona-xtradb-cluster-operator/releases/tag/v1.20.0) | 可选日志收集器 |
| Kubernetes | `1.33.6` | kubeauto 当前基线 | Percona 1.20.0 官方重点测试范围为 Kubernetes 1.33–1.36 |

> 2026-08-19 核对结果：Percona Operator v1.20.0 的 GitHub release 为正式发布；官方系统要求文档写明至少 3 节点、每节点 2 CPU、2 GiB RAM，至少 60 GiB 可用于 PV。官方架构文档建议生产集群 3–5 个节点，避免偶数节点和 7 节点以上拓扑。

## 必读官方资料

| 主题 | 官方资料 |
|---|---|
| 产品总览 | [Percona Operator for MySQL/PXC](https://docs.percona.com/percona-operator-for-mysql/pxc/index.html) |
| 系统要求 | [System requirements](https://docs.percona.com/percona-operator-for-mysql/pxc/System-Requirements.html) |
| 架构与 Galera | [Architecture](https://docs.percona.com/percona-operator-for-mysql/pxc/architecture.html) |
| 暴露服务 | [Exposing cluster](https://docs.percona.com/percona-operator-for-mysql/pxc/expose.html) |
| HAProxy | [HAProxy configuration](https://docs.percona.com/percona-operator-for-mysql/pxc/haproxy-conf.html) |
| 备份存储 | [Backup storage](https://docs.percona.com/percona-operator-for-mysql/pxc/backups-storage.html) |
| 定时/手工备份 | [Backups](https://docs.percona.com/percona-operator-for-mysql/pxc/backups.html) |
| PITR | [Point-in-time recovery](https://docs.percona.com/percona-operator-for-mysql/pxc/backups-pitr.html) |
| 恢复 | [Backups and restore](https://docs.percona.com/percona-operator-for-mysql/pxc/backups-restore.html) |
| 升级 | [Update](https://docs.percona.com/percona-operator-for-mysql/pxc/update.html) |
| TLS | [TLS](https://docs.percona.com/percona-operator-for-mysql/pxc/TLS.html) |
| 用户与密码轮换 | [Users](https://docs.percona.com/percona-operator-for-mysql/pxc/users.html) |
| 监控 | [Monitoring](https://docs.percona.com/percona-operator-for-mysql/pxc/monitoring.html) |
| 官方源码 | [Operator v1.20.0 source](https://github.com/percona/percona-xtradb-cluster-operator/tree/v1.20.0) |
| 官方 CR 源码 | [`deploy/cr.yaml`](https://raw.githubusercontent.com/percona/percona-xtradb-cluster-operator/v1.20.0/deploy/cr.yaml) |
| 官方 CRD/bundle | [`deploy/bundle.yaml`](https://raw.githubusercontent.com/percona/percona-xtradb-cluster-operator/v1.20.0/deploy/bundle.yaml) |

## 证据使用规则

1. 先看本目录的项目边界和锁定版本，再查看对应 tag 的官方源码。
2. 官方文档中的 `LoadBalancer`、S3、cert-manager、PMM 等能力不是 kubeauto 默认交付，必须以本项目后续实现和独立门禁为准。
3. 官方示例中的 `perconalab/*:main-*` 是开发示例，不能用于生产镜像。生产必须使用正式版本 tag，并在镜像进入本地 Registry 前记录 digest。
4. 官方支持平台列表不等于 kubeauto 所有发行版均已通过 PXC 门禁；Anolis/openEuler/openSUSE 需另行验证，不能借用核心集群测试结果。

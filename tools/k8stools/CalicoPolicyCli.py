#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
限制哪些网段能访问某个 TCP 端口（节点上的端口或普通 Pod 里的端口）。
详细说明在本文件里的 MAINTAINER_DOC 常量（不会出现在 --help 里）。
需要 Python 3.8+，只用标准库。
"""

from __future__ import annotations

__version__ = "2.8.1"

# 改 Calico 相关逻辑时，对照 Tigera 当前稳定文档（与 Calico 发布列车一致）
CALICO_DOCS_LATEST = "https://docs.tigera.io/calico/latest/"
CALICO_COMPONENT_VERSIONS = "https://docs.tigera.io/calico/latest/reference/component-versions"

# ---------------------------------------------------------------------------
# 给维护者看的说明（不会出现在 --help 里；改脚本前建议先扫一遍）
# ---------------------------------------------------------------------------
MAINTAINER_DOC = """
===============================================================================
CalicoPolicyCli.py（原 calico_host_port_lockdown 逻辑）- 维护者说明（用语尽量贴近 K8s / Calico 官方概念）
===============================================================================

读完可知：脚本做什么、两类策略各自解决哪条数据路径、会生成哪些 API 对象、各参数含义与生产风险、代码入口。

-------------------------------------------------------------------------------
0. 生产集群读前须知（请先读这段再执行 apply / delete）
-------------------------------------------------------------------------------

  本工具在授权后会按你显式指定的 --executor：kubectl（Kubernetes API）或 calicoctl（Calico 数据存储，如 etcd），
  提交或删除对应策略对象；两者不会在主机层被脚本自动代选。
  配置不当可能造成：监控与告警采集中断、跳板/堡垒机无法访问、控制台或 CI 被误拦、
  或在删除策略后端口暴露面与变更前不一致（变宽松或与遗留策略叠加产生意外组合）。

  host 层 HostEndpoint 的 interfaceName：必须由人工显式指定——CLI 传 --interface <网卡名> 或 \"*\"，和/或
  在每个 Node 上设置注解 kubeauto.calico/host-interface（缺省节点的网卡须由 --interface 兜底）。禁止 auto/自动探测。
  任何访问集群的子命令须显式 --context；plan/validate/apply/delete 还须显式 --traffic-layer；
  禁止用 CALICO_HOST_FW_CONTEXT / CALICO_HOST_FW_TRAFFIC_LAYER / CALICO_HOST_FW_NAMESPACE 代替手写参数。
  会改集群的操作：
    apply：在通过「写集群授权」后，创建或更新 GlobalNetworkPolicy、HostEndpoint、
           networking.k8s.io/v1 NetworkPolicy（取决于 --traffic-layer）。
    delete：按名称或标签删除上述对象的一部分或全部。

  只读、不修改 API 对象的操作：
    preflight、nodes、validate（仅查询与校验）、plan（只向标准输出打印 YAML）。

  validate 的 CLI 刻意「窄」于 plan/apply：只接受会改变校验结论的参数（含 --hep-profile、
  --no-default-allow-profile 等与 Profile 校验相关的项），避免接受一堆脚本根本不检查的开关
  （可靠、可审计）。语义一致靠共用内部校验函数（如 HEP 网卡/注解规则），不是靠强行同款命令行外观。
  apply --post-verify 在进程内直接调用 validate 逻辑，不依赖再解析一长串 CLI。

  无法承诺「绝对零风险」：现网还受 Felix 配置、其它 GNP/Profile/Tier、节点路由与 kube-proxy 模式等影响。
  本版本按 Tigera 文档为 GNP 增加 ingress 末尾 Pass、并在 calicoctl 场景用 calicoctl 做主机层备份，以降低误伤与回滚信息丢失。

  【生产影响】授权写集群的方式（任选其一即视为自愿承担数据面变更风险）：
    - 命令行显式加 --confirm（apply 子命令）
    - 环境变量 CALICO_HOST_FW_CONFIRM 设为 1、true、yes（效果等同 --confirm，
      日志会单独提醒：避免在共享会话、全局 shell profile、未审查的 CI 中误设）

  执行 apply 前建议至少做到：
    1) 核对命令行 --context 与 kubectl config get-contexts / 变更工单一致，避免误连生产。
    2) preflight 与 validate：核对 -a 是否覆盖 Prometheus、跳板、管控网等真实源地址段。
    3) plan 审阅 YAML；可用 apply --dry-run=server 让 apiserver 校验对象（不落库）。
    4) 正式变更建议加 --backup，并在业务低峰做小范围连通性验证。

  apply 成功后（企业级变更闭环）：
    - 日志会打印「成功」与同参 validate 的可复制命令行；应对齐执行 validate，确认节点 IP、
      Pod 命中数等仍无告警。
    - 须在数据面做抽测：从 -a 内与 -a 外各选一源，验证目标 TCP 端口放行/拒绝符合预期。
    - 可加 apply --post-verify，在本进程内自动接跑 validate（只读），便于 CI/人工少跳一步；
      不可替代数据面探测与 kubectl get 核对对象。

  「入站只允许列表内源」是脚本生成的规则语义；最终在节点/Pod 上的效果还与集群内
  已有 Calico Profile、其它 GlobalNetworkPolicy/NetworkPolicy、Felix 配置共同作用，
  与 Tigera 文档中关于 hosts 与 Policy 优先级的说明一致；变更后请以现网表现为准。

-------------------------------------------------------------------------------
1. 用途（本工具解决什么问题）
-------------------------------------------------------------------------------

  针对一个 TCP 监听端口，生成「仅允许若干源 IP 段（CIDR）访问该端口，其它源被拒绝」
  的入站策略。典型场景：节点上 9100（node-exporter）、hostNetwork 工作负载暴露的端口、
  绑定节点 IP 的服务端口。

-------------------------------------------------------------------------------
2. 为什么仅靠 Kubernetes NetworkPolicy 往往不够
-------------------------------------------------------------------------------

  networking.k8s.io/v1 NetworkPolicy 由 CNI 在 Pod 网络接口上执行，解决的是「进入 Pod
  网络命名空间」的入口流量（见 Kubernetes 官方 NetworkPolicy 概念文档）。

  当访问目标是「节点的 IP:端口」或 hostNetwork Pod 在宿主机网络命名空间监听的端口时，
  该流量通常不经过你以为的那条 Pod NetworkPolicy 路径；需要用 Calico 的
  HostEndpoint（描述主机端点）配合 GlobalNetworkPolicy（可匹配主机端点）来约束这类入站。

  对工作负载在普通 Pod 网卡上、非 hostNetwork 的场景，使用标准 NetworkPolicy 更合适，
  且有利于与具体 CNI 解耦。--traffic-layer 即在「仅主机侧 Calico」「仅命名空间内 NetPol」
  或「两者同时」之间选择。

-------------------------------------------------------------------------------
3. host / pod / both 含义
-------------------------------------------------------------------------------

  host：
    生成 projectcalico.org/v3 GlobalNetworkPolicy，并默认生成本工具管理的 HostEndpoint
   （可用 --no-hostendpoint 关闭后者）。GNP.spec.selector 仅匹配带约定标签的 HEP。
    Calico v3 API 中策略匹配主机端点用 spec.selector，请勿混用已废弃或无效的 nodeSelector
    写法，否则存在策略匹配范围扩大到 workload 的风险（见官方 GlobalNetworkPolicy 参考）。

  pod：
    在指定 namespace 生成一条标准 NetworkPolicy：spec.policyTypes 含 Ingress，
    仅允许来自 -a 中 CIDR 到目标 TCP 端口的入站。受影响的 Pod 由 podSelector 决定
   （--pod-label 或 --pod-selector-all）。hostNetwork Pod 是否受该策略约束以官方文档为准。

  both：
    同一次输出多份 YAML 文档（--- 分隔）：主机 Calico 资源 + 命名空间内 NetworkPolicy。

-------------------------------------------------------------------------------
4. 主机侧 Calico 对象（读 YAML 时对照字段含义）
-------------------------------------------------------------------------------

  HostEndpoint：
    spec.node（节点名）、spec.interfaceName（须 CLI --interface 或 Node 注解人工指定；通配 \"*\" 见官方 HEP）、
    spec.expectedIPs（通常由 Node 地址推导，可选含 ExternalIP）、spec.profiles。
    默认 profiles 含 projectcalico-default-allow，与「自动 HostEndpoint」基线一致：
    在收紧目标端口的同时降低误伤 SSH、kubelet 等主机关键入站的风险。

  GlobalNetworkPolicy：
    spec.types 含 Ingress；spec.selector 与上方 HEP 标签对齐。
    ingress：对目标 TCP 端口先按 -a 放行，再拒绝同端口其它源；IPv4 与 IPv6 分条填写
   （符合 EntityRule 不混编地址族的要求）。清单末尾追加一条 action: Pass：使未命中上述 Allow/Deny 的入站
    按官方语义跳转到 HostEndpoint 上的 Profile（默认 projectcalico-default-allow）。
    若无此 Pass，在「GNP 已选中该 HEP」时其余入站（含 kubelet 10250；该端口不在 Calico 默认 failsafe 列表中）
    往往不会落到 Profile，表现为 apiserver→kubelet exec/日志流超时、telnet 10250 挂死等；删除 GNP/HEP 后恢复。

-------------------------------------------------------------------------------
5. Pod 侧 NetworkPolicy
-------------------------------------------------------------------------------

  仅生成 kubernetes.io networking.k8s.io/v1 NetworkPolicy；不生成 projectcalico.org/v3
  的 namespaced NetworkPolicy CR。若需 Calico 专有 NetPol 字段，请手写清单另行下发。

-------------------------------------------------------------------------------
6. 代码阅读顺序
-------------------------------------------------------------------------------

  main() -> build_parser().parse_args() -> cmd_preflight | cmd_nodes | cmd_validate |
  cmd_plan | cmd_apply | cmd_delete

  生成 YAML：cmd_plan 及 build_combined_manifest、build_k8s_network_policy_yaml 等。
  下发与防护：cmd_apply（--confirm、--backup、--apply-staged、dry-run、executor）。
  检查：cmd_validate、cmd_preflight。

  plan 与 validate 不向 API 写入策略对象；apply 仅在实际写集群路径（非纯 dry-run）
  且具备授权时才会 apply。

-------------------------------------------------------------------------------
7. kubectl 与 calicoctl（kubeauto 默认 Calico 必读本节）
-------------------------------------------------------------------------------

  清单中含标准 NetworkPolicy 时必须使用 kubectl。仅含 Calico CR（GNP、HEP 等）时，用 kubectl 还是
  calicoctl 取决于集群如何安装 Calico。

  【kubeauto 仓库 roles/calico 的默认装法】
    - manifest 来自 etcd 数据存储的 Calico 发行包（如 calico-etcd.yaml），见各版本模板顶注释。
    - calico-node、kube-controllers 通过 Secret 中的证书连接 etcd，策略与节点状态主要在 etcd。
    - 不会在 Kubernetes API 里注册 projectcalico.org/v3 的 GlobalNetworkPolicy、HostEndpoint 等 CRD，
      因此 kubectl get crd 筛选 calico 往往为空，kubectl apply 对这类 YAML 会报
      no matches for kind ... projectcalico.org/v3。这与「calico-node Pod 在跑」可以同时成立。
    - 管理这些对象应使用 calicoctl；kubeauto 的 roles/calico 会配置 /etc/calico/calicoctl.cfg
      （指向 etcdEndpoints 与证书），并把 calicoctl 安装到 ansible 的 bin_dir。

  【本工具在 kubeauto 集群上的用法（与 Tigera 文档一致：CR 经何种 API 写入须由你显式选择）。
    - traffic-layer 含 host 或 both：plan/apply/delete 必须显式 --executor kubectl 或 calicoctl（禁止省略）。
      etcd 数据存储场景与 kubeauto 默认 Calico 装法下选 calicoctl + /etc/calico/calicoctl.cfg。
    - traffic-layer pod：不得依赖 --executor；清单仅含 networking.k8s.io NetworkPolicy，固定 kubectl。
    - traffic-layer both：主机 Calico 段与 Pod NetPol 段分两次下发（先 host executor，再 kubectl），
      与「Calico CR 与 NetPol 可能走不同 API」的官方现实一致；禁止将两类清单混在一起用错误工具一次 apply。
    - 预演：calicoctl 无 kubectl 的 --dry-run=server；主机层用 plan 审 YAML，apply --dry-run=none；NetPol 仍可用 kubectl --dry-run=server。

  【若希望全部用 kubectl 管理 Calico CR】
    - 需将 Calico 改为 Kubernetes 数据存储并安装相应 CRD（及可选 API Server），属集群改造，
      见 Tigera 文档；不在本工具默认假设内。

-------------------------------------------------------------------------------
8. --backup 与 --apply-staged（降低操作窗口风险，不替代人工核对）
-------------------------------------------------------------------------------

  --backup：在覆盖同名/同标签资源前尝试 get 并写入本地文件；备份失败不阻止 apply，
  【生产影响】不应视为唯一回滚手段，重大变更仍应用 GitOps 或集群备份。
    apply 且 --executor calicoctl 时：主机层 GNP/HEP 备份走 calicoctl get（与 etcd 数据存储一致）；
    HEP 多对象为合并 JSON（hostendpoints-kubeauto.json），非 kubectl -l YAML。
    --executor kubectl 时：主机层仍走 kubectl get projectcalico 资源名（与 KDD/CRD 一致）。

  --apply-staged：主机相关时先发 GNP 再发 HEP；traffic-layer=both 且 staged 时为三步——
  GNP → HEP（均走 --executor）→ NetPol（固定 kubectl），避免 etcd 模式下误用 kubectl 混用 Calico 清单。
  【生产影响】中间态仍可能影响现网，仅作缓和而非消除风险。

-------------------------------------------------------------------------------
9. 参数与环境变量（带【生产影响】的项：变更前请读完并评估是否继续）
-------------------------------------------------------------------------------

【9.1 全局选项】写在子命令前，例：脚本 --context prod apply …

  --verbose / -v：更详细的运行日志，便于排障。不改集群。
  --no-log-file：不写本地滚动日志。【生产影响】出问题后可追溯信息变少，仅建议短期会话。
  --kubectl：kubectl 可执行文件；默认环境变量 KUBECTL，否则在 PATH 中查找 kubectl。
  --context：kubeconfig 中的 context 名；preflight/plan/validate/apply/delete/nodes 均必填（禁止省略、
      禁止 CALICO_HOST_FW_CONTEXT 注入默认值）。【生产影响】指错集群则所有操作对准错误环境。
  --executor kubectl | calicoctl：traffic-layer 为 host 或 both 时 plan/apply/delete 必填（禁止 auto/省略）。
      kubectl=资源经 Kubernetes API（须已注册 projectcalico 等 CRD）；calicoctl=经 Calico 数据存储（如 etcd）。
      仅 traffic-layer=pod 时不要求本参数（实现固定 kubectl）。
  --calicoctl：calicoctl 路径；默认 CALICOCTL 环境变量。

【9.2 流量层与 Pod 策略命名】（plan / apply / validate / delete；preflight、nodes 无）

  --traffic-layer host | pod | both：控制生成/删除哪些资源；须每次手写，禁止省略、禁止用环境变量替代。
      【生产影响】both 同一次变更动主机与某 namespace，故障面更大；排查需同时看 Calico 与 NetPol。
  -n / --namespace：Pod 策略所在命名空间；traffic-layer 含 pod/both 时 plan/apply/validate 与 delete（删 NP）必填，
      须手写，禁止依赖环境变量默认。
      【生产影响】namespace 错误会把策略套到错误工作负载集合或删错对象。
  --pod-label KEY=VALUE（可重复）：写入 podSelector.matchLabels；多对之间为 AND。
      【生产影响】标签与线上 Deployment 不一致会导致策略不生效（等于未防护或部分未防护）。
  --pod-selector-all：空选择器，匹配该 namespace 内全部 Pod。
      【生产影响】极高：若 -a 过窄或误改，可能导致整 namespace 入站大面积拒绝；须书面审批后再用。
  若与 --pod-label 同时写：当前实现以 --pod-selector-all 为准并忽略 pod-label，运行时会打警告；
      请勿依赖该组合，应只保留其一以免误解。
  --k8s-np-name：NetworkPolicy metadata.name；不配则用 kubeauto-restrict-pod-tcp-<port>。
      【生产影响】与集群已有同名冲突会覆盖原对象；delete 名字错误会删除他人策略。

【9.3 plan 与 apply 共用】（delete 无；validate 仅镜像其中与校验相关的子集，见 9.4）

  -a / --allow-net CIDR（可重复）：允许访问「本工具所设 TCP 端口」的源网段；IPv4/IPv6 可混开，
      脚本拆成多条规则（Calico EntityRule / NetPol ipBlock）。至少须有一个有效 CIDR。
      【生产影响】漏掉监控、跳板、堡垒出口、管控集群访问源会导致合法流量被 deny；
      网段过宽则收口失去意义；请以现网真实源地址规划为准。
  --port：目标 TCP 端口；默认 9100；参与默认 GNP/NetPol 名称（见 9.9）。
      【生产影响】端口错误时规则作用在错误的监听上，表现为「改了策略但业务仍暴露/仍不通」。
  --policy-name：GlobalNetworkPolicy 名称；不配则用 kubeauto-restrict-host-tcp-<port>。
      【生产影响】改名会新建一条 GNP，旧 GNP 若未手动删除可能并存导致评估顺序非预期。
  --hep-prefix：HostEndpoint 名前缀；Per 节点名为「前缀-节点名(合法化)」；默认 kubeauto-hep。
      【生产影响】改名会产生新 HEP，旧对象残留可能使 selector 命中多套端点。
  --interface：创建 HostEndpoint 时须显式给出网卡名或 \"*\"（Calico 通配）；可与注解 kubeauto.calico/host-interface
      联用（注解优先，未注解节点必须用本参数兜底）。禁止 auto、禁止脚本内探测或环境变量隐式推断。
      【生产影响】漏传且有个别节点无注解时 plan/apply 会直接拒绝，避免静默错绑网卡。
  --include-external-ip：expectedIPs 增加 Node ExternalIP。
      【生产影响】公网 IP 纳入端点后，若边界安全策略以为仅靠 NetPol 即可隔离，可能产生认知偏差。
  --no-hostendpoint：仅下发 GNP，不创建本工具通常创建的 HEP。
      【生产影响】须配置精确 --policy-selector，仅命中你确认安全的已有 HostEndpoint；selector
      过宽可能对错误端点集收紧入站，造成多节点业务中断。
  --policy-selector：GNP.spec.selector 字符串。自建 HEP 时默认对齐标签 kubeauto.calico/host-
      port-lockdown=true；与 --no-hostendpoint 联用时必填合法 selector。
      【生产影响】这是主机侧作用范围的核心；生产错误可直接导致大规模拒收或误放行。
  --policy-order：GNP.spec.order；默认 500；同 tier 内数值越小越先匹配（以 Calico 文档为准）。
      【生产影响】顺序与现有策略冲突时，最终结果可能与「仅读 YAML」的直觉不一致。
  --hep-profile：HostEndpoint profiles 之一；默认 projectcalico-default-allow。
      【生产影响】改错将失去与「自动 HEP」对齐的基线放行，可能误伤 SSH/kubelet 等。
  --no-default-allow-profile：不挂载上述默认 profile。
      【生产影响】高：若无其它 Profile/策略明确放行主机基线入站，存在整节点管理面失联风险。
  --gnp-tier：GNP.spec.tier；留空用集群默认 tier。
      【生产影响】tier 名与集群定义不一致时对象可能被 API 拒绝或落入错误评估桶。
  --gnp-apply-on-forward：GNP.spec.applyOnForward=true。
      【生产影响】影响转发路径上的包；误用可导致经节点转发的服务与直连行为不一致（见官方
      hosts / applyOnForward 说明）。
  --gnp-performance-hint（可重复）：写入 spec.performanceHints；值由 Felix 校验。
      【生产影响】与版本不兼容的 hint 可能导致策略生效延迟或节点负载异常；应先在非生产验证。

【9.4 validate】
  参数集合：9.1 + 9.2 + -a + --port + 与本次 apply 对齐所需的主机开关（--interface、--include-external-ip、
  --no-hostendpoint、--no-default-allow-profile、--hep-profile）+ Pod 层定位项（同 9.2）。
  不含 9.3 中仅影响「生成 YAML 长什么样」、但 validate 并不解析的项（如 --policy-order、--gnp-tier）；
  那些请以 plan 人工审阅与数据面抽测补位。
  host/both 时须 --executor；未 --no-hostendpoint 时须 --interface 或每节点注解。
  行为：校验 CIDR；host/both 时对照 InternalIP（及 --include-external-ip 时的 ExternalIP）与 -a；
  并与 plan/apply 共用同一套主机 HEP 网卡/注解校验（实现上即同一函数）；pod/both 时统计匹配 Pod 数，
  并记录 --k8s-np-name（或默认名）供工单核对，不对 NetworkPolicy 对象做 get。
  host/both 且 --executor calicoctl、未 --no-hostendpoint、未 --no-default-allow-profile 时：只读
  calicoctl get profile <hep-profile>，确认将写入 HEP 的 Profile 在数据存储中存在（降低 apply 失败或 Pass 无 Profile 风险）。
  【生产影响】不向 API 写入对象；仍执行只读 List/Get，需具备相应 RBAC；大集群注意超时配置。

【9.5 仅 apply】

  --confirm：显式允许 kubectl/calicoctl 提交可改变集群的策略对象。
  未给 --confirm 且未设 CALICO_HOST_FW_CONFIRM 时：apply 只生成清单逻辑，不向 API 写入（防误操作）。
  CALICO_HOST_FW_CONFIRM=1/true/yes：等同全程 --confirm；运行时会打【生产影响】专用日志条。
      【生产影响】若写在 shell 全局配置、共享 CI 凭据或未审查脚本中，可能在无意识下持续写生产。
  --backup：覆盖前尝试拉取将触及的 GNP、带工具标签的 HEP、同名 NetPol 存盘；主机层 calicoctl 与 kubectl
      路径差异见上文第 8 节（etcd 场景下 HEP 备份为 JSON 清单）。
      【生产影响】备份失败不会中止 apply；不能代替 GitOps/变更工单/集群级备份。
  --backup-dir：备份根目录；不配则在 CALICO_HOST_FW_LOG_DIR 下按时间分子目录。
  --apply-staged：主机侧分次 apply（先 GNP 后 HEP）；both 时分三步，NetPol 在 HEP 之后单独 kubectl 提交。
      【生产影响】仍存在中间态窗口；不能视为零风险切换。
  --dry-run none | client | server（仅走 kubectl apply 时有效；与 kubectl 官方语义一致）：
      none
        正常 apply，会把对象写入集群（在你已授权写 API 的前提下）。这就是「真变更」。
      client
        客户端 dry-run：不会在集群里持久化对象；只在 kubectl 侧做拼装/基本校验。
        优点快；缺点是不会完整经过 apiserver 准入/校验，有些问题只有 server 模式能提前暴露。
      server
        服务端 dry-run：请求会发到 apiserver，走准入等链路，仍不把对象写入 etcd。
        最适合正式下发前「让集群验一遍YAML」；比 client 更准，对 apiserver 多一点点负载。
      三个取值的意义：none = 真下发；client = 快速本地演练；server = 集群侧预演不落库。
      【生产影响】仅有 none 在授权后会产生持久化变更；client/server 都不会落库，但也不是「没发请求」
      （server 会对 API 有只读类校验流量）。走 calicoctl apply 时仅支持 none，若设 client/server 会报错
      （须改用 kubectl 或先用 plan 看 YAML）。
      traffic-layer=both 且 --executor calicoctl：整条 apply 不能使用 --dry-run=server（主机清单先经 calicoctl）；
      若仅预演 Pod 段，请对 plan 输出的 NetworkPolicy 文档单独执行 kubectl apply --dry-run=server。
  --post-verify：apply 全部下发成功后，在同一进程内直接调用 cmd_validate（只读），与 validate 子命令
      同源逻辑且共用 apply 已解析的参数对象，无需把全套 plan 参数再塞回 CLI。
      进程退出码随后续 validate 结果传递（validate 当前对告警仍返回 0，以日志为准）。
      日志中的可手动 validate 行仅含 validate 子命令实际接受的开关（窄 CLI），便于与抽测、kubectl get 闭环。

【9.6 仅 delete】

  --port：与创建时一致，用于默认 GNP/NetPol 名称。
  --policy-name：要删的 GlobalNetworkPolicy；不配则用默认命名。
  --delete-hostendpoints：为真时删除本工具创建的 HostEndpoint（kubeauto.calico/host-
      port-lockdown=true）。无 projectcalico CRD 时走 calicoctl，按清单名批量 delete（勿依赖 kubectl -l）。
  --confirm：可选，与 apply 写法兼容；delete 无「未确认则不删」门禁，传不传行为相同。
  若误粘贴 apply 的 -a / --interface：忽略并警告（delete 不按网段或网卡删，只按资源名与工具标签）。
  另含 9.2（定位 NetPol）。不接受完整 9.3（无改 GNP spec 类删参）。
  kubeauto 默认 etcd 存储：主机层 delete 须与 apply 使用相同且显式的 --executor（通常为 calicoctl）。
  【生产影响】删策略后端口不再受本工具规则约束：可能变宽松，或与其它遗留 Calico/K8s 策略
      叠加产生非直觉结果。未删 HEP 时主机端点仍存在，可与其它 GNP 继续作用。删前逐项核对
      context、资源名、namespace。

【9.7 子命令与参数速查】

  preflight、nodes：9.1（须含显式 --context）。
  validate：9.1 + 9.2 + 9.4（窄 CLI）；host/both 时 9.1 须含 --executor。
  plan：9.1 + 9.2 + 9.3；host/both 时 9.1 须含 --executor。
  apply：9.1 + 9.2 + 9.3 + 9.5；host/both 时 9.1 须含 --executor。
  delete：9.1 + 9.2 + 9.6（含 NetPol 定位用的 -n、--k8s-np-name）；误传 -a 或 --interface 会忽略并警告
      （便于与 apply 命令行对行粘贴）。

【9.8 环境变量】可用于运维便利的仅限下列项；--context、--traffic-layer、-n 不得以环境变量代填（须 CLI 手写）。

  KUBECTL：kubectl 可执行路径默认来源之一。
  CALICOCTL：calicoctl 路径默认来源之一。
  CALICO_HOST_FW_LOG_DIR：日志与默认备份根路径的上级目录；默认 <cwd>/logs/。
  CALICO_HOST_FW_KUBECTL_TIMEOUT：kubectl/calicoctl 超时秒数；默认 120；大 manifest 的 apply 会使用数倍超时。
  CALICO_HOST_FW_CONFIRM：等同 apply --confirm。【生产影响】见 9.5；勿长期留在共享环境中。

【9.9 默认资源名与标签】

  GlobalNetworkPolicy：kubeauto-restrict-host-tcp-<port>
  NetworkPolicy：kubeauto-restrict-pod-tcp-<port>
  HostEndpoint：<hep-prefix>-<节点名合法化>
  HostEndpoint 标签：kubeauto.calico/host-port-lockdown=true（与 GNP 默认 selector 对齐）。
  NetworkPolicy 标签：kubeauto.calico/traffic-lockdown=pod。
  【生产影响】与现有同名 metadata.name 冲突时 apply 将覆盖；生产请先 kubectl get 确认归属。

【9.10 生产变更自检清单（建议打印或贴到变更单）】

  1) 命令行 --context 是否与工单环境一致（与 kubectl config get-contexts 中名称一致）。
  2) host/both 是否已手写 --executor kubectl 或 calicoctl（与集群 Calico 数据存储方式一致，禁止依赖 auto）。
  3) 创建 HEP 是否已手写 --interface 或为每节点配置注解 kubeauto.calico/host-interface；
      validate / --post-verify 与 apply 的主机侧参数须一致（含 --executor、--interface、--include-external-ip、
      --hep-profile / --no-default-allow-profile）。
  4) -a 是否包含监控、告警、跳板、管控面、必要 CI 出口等全部真实源网段（含 IPv6 若在用）。
  5) Pod 选择是否故意为之；是否避免误用 --pod-selector-all。
  6) 主机侧 --policy-selector / --no-hostendpoint 是否在测试集群验证过命中范围。
  7) 是否已 plan；主机层经 calicoctl 时禁止对整单 apply 使用 --dry-run=server；traffic-layer=both 时请分段预演
      （plan 审 GNP/HEP，NetPol 单独 kubectl apply --dry-run=server）。
  8) 是否理解删除策略后暴露面可能变化，并已评估与其它防火墙、安全组、遗留策略的叠加效应。

-------------------------------------------------------------------------------
10. 官方文档
-------------------------------------------------------------------------------
  Calico 总览：
""" + CALICO_DOCS_LATEST + """
  组件版本：
""" + CALICO_COMPONENT_VERSIONS + """
  HostEndpoint:
    https://docs.tigera.io/calico/latest/reference/resources/hostendpoint
  GlobalNetworkPolicy:
    https://docs.tigera.io/calico/latest/reference/resources/globalnetworkpolicy
  保护 Kubernetes 节点:
    https://docs.tigera.io/calico/latest/network-policy/hosts/kubernetes-nodes
  Kubernetes NetworkPolicy:
    https://kubernetes.io/docs/concepts/services-networking/network-policies/

-------------------------------------------------------------------------------
11. 本 CLI 不生成的 Calico 字段（须手写 YAML 并在非生产先验证）
-------------------------------------------------------------------------------

  GlobalNetworkPolicy 的 doNotTrack、preDNAT 等与连接跟踪、DNAT 及转发路径强相关，
  错误组合可导致会话半开、合法流量被 silent drop 或管理面失联。
  【生产影响】本工具不写这些字段；若业务需要，由具备 Calico hosts 策略经验的人员按
  Tigera「Protect Kubernetes nodes / Policy for hosts」等文档单独评审与下发。
===============================================================================
"""

import argparse
import datetime as _dt
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_POLICY_NAME_TEMPLATE = "kubeauto-restrict-host-tcp-{port}"
DEFAULT_K8S_NP_NAME_TEMPLATE = "kubeauto-restrict-pod-tcp-{port}"
DEFAULT_HEP_PREFIX = "kubeauto-hep"
DEFAULT_HEP_PROFILE = "projectcalico-default-allow"
# 与 HostEndpoint 上的标签一致；使用 == 形式利于 Felix 优化（见官方 selector 性能说明）
DEFAULT_POLICY_SELECTOR_FOR_MANAGED_HEP = 'kubeauto.calico/host-port-lockdown == "true"'
MANAGED_LABEL_KEY = "kubeauto.calico/host-port-lockdown"
MANAGED_LABEL_VAL = "true"
# Node.metadata.annotations：按节点覆盖 HostEndpoint.interfaceName（优先于 CLI --interface）
NODE_HOST_INTERFACE_ANNOTATION = "kubeauto.calico/host-interface"
K8S_NP_LABEL_KEY = "kubeauto.calico/traffic-lockdown"
K8S_NP_LABEL_VAL = "pod"

MIN_PORT = 1
MAX_PORT = 65535

LOG_DIR = os.environ.get("CALICO_HOST_FW_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_BASENAME = "calico_host_port_lockdown.log"
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

CONFIRM_ENV = "CALICO_HOST_FW_CONFIRM"

# 根命令 --help 结尾的短文；想搞懂设计看源码里的 MAINTAINER_DOC
HELP_EPILOG = """
干啥: 某个 TCP 端口只允许 -a 里的网段访问；管节点上的端口用 Calico（GNP+HEP），管 Pod 用标准 NetPol。

--traffic-layer  host | pod | both
  host  写 Calico 全局策略 + 默认再写 HostEndpoint
  pod   在某个 namespace 写一条标准 NetworkPolicy
  both  上面两套一次打出来（多段 YAML）

子命令（注意：--context 这类要写在子命令前面，例如 %(prog)s --context 生产 plan ...）
  preflight / nodes  只读，不改集群
  validate  只读；仅含校验所需参数（见 MAINTAINER_DOC 9.4）；apply --post-verify 在进程内直调校验逻辑
  plan      只在屏幕上打印 YAML
  apply     真往集群里写要加 --confirm；可加 --backup、--apply-staged、--dry-run
  delete    按层删 GNP、HEP、NetPol（名字要和当初创建时一致）

--executor  kubectl | calicoctl（host/both 必填；仅 Calico 段与 delete 主机层）
  清单含标准 NetPol 时由实现固定第二次使用 kubectl，与 --executor 无关。
  kubeauto 默认 Calico 为 etcd、常无 projectcalico CRD：主机层选 calicoctl，并配置 PATH 与 /etc/calico/calicoctl.cfg；
  详见 MAINTAINER_DOC 第 7、9.1 节。

环境变量和命令行重复时，以命令行为准。具体名字看每个参数的 -h。

链接: Calico %(calico_docs)s  组件版本 %(calico_ver)s
退出: 0 正常  1 出错  2 子命令不认识

建议顺序: preflight -> validate（窄参数，须与即将 apply 的主机开关对齐）/ plan ->（Pod 层可 kubectl apply --dry-run=server）-> apply --confirm [--backup]；成功后 apply --post-verify 或按日志中的窄 validate 行复测
  host 且 --executor calicoctl 时：预演=plan，勿用 apply --dry-run=server（calicoctl 不支持）。
  traffic-layer=both 且主机段走 calicoctl 时：整单 apply 不可用 --dry-run=server（会先经 calicoctl）；请 plan 审主机 YAML，再仅对 NetPol 段单独 kubectl apply --dry-run=server。

生产注意: apply/delete 会修改 API 对象；执行前核对命令行 --context 与工单环境、kubectl config get-contexts 一致。
traffic-layer 含 host/both 须显式 --executor；创建 HostEndpoint 须 --interface（或每节点注解 kubeauto.calico/host-interface）。
validate 在 host/both 时也必须带齐上述项，否则子命令会直接报错或与 apply 校验范围不一致。
漏配 -a、--policy-selector 过宽、误用 --pod-selector-all 可能导致拒收或误伤；详见源码 MAINTAINER_DOC 第 0、9 节。

--------------------------------------------------------------------
示例 A：主机层（host / 节点 IP 或 hostNetwork 监听端口，如 node-exporter :9100）
  plan/apply/delete 须 --traffic-layer host；主机对象经 Calico 时须 --executor kubectl|calicoctl。
  下表「单条命令」刻意列全主机侧 plan/apply 可选参数，便于对照 plan -h / apply -h；实践中请删不需要的项。
  validate 为窄 CLI（MAINTAINER_DOC 9.4）：不含 --policy-order、--gnp-tier、--hep-prefix、--policy-name 等，
  但须与 apply 对齐 --executor、--interface（或节点注解）、--include-external-ip、--no-hostendpoint、
  --hep-profile、--no-default-allow-profile。
  根级通用：可加 -v / --verbose、--no-log-file、--kubectl /usr/bin/kubectl、--calicoctl /usr/bin/calicoctl。
  慎用：--gnp-apply-on-forward、--gnp-performance-hint、--gnp-tier 仅在有明确数据面/集群设计依据时使用；
  --gnp-tier 须与现网已存在的 Tier 资源名一致；无 Tier CR 的集群请删掉该行，否则 apply 可能被拒。
--------------------------------------------------------------------
  %(prog)s -v --context prod preflight
  %(prog)s --context prod nodes
  %(prog)s --context prod --executor calicoctl validate \\
      --traffic-layer host \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 10.234.0.0/16 -a 2001:db8:nodes::/64 \\
      --port 9100 \\
      --interface eth0 \\
      --include-external-ip \\
      --hep-profile projectcalico-default-allow
  %(prog)s --context prod --executor calicoctl plan \\
      --traffic-layer host \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 10.234.0.0/16 -a 2001:db8:nodes::/64 \\
      --port 9100 \\
      --policy-name myorg-host-tcp-9100 \\
      --hep-prefix myorg-hep \\
      --interface eth0 \\
      --include-external-ip \\
      --policy-order 500 \\
      --hep-profile projectcalico-default-allow \\
      --gnp-apply-on-forward \\
      --gnp-performance-hint AssumeNeededOnEveryNode
  # 若集群已配置 Calico/Enterprise Tier，在上一行 plan 末尾追加： --gnp-tier <现网Tier名>
  %(prog)s --context prod --executor calicoctl apply --confirm --backup --apply-staged --post-verify \\
      --traffic-layer host \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 10.234.0.0/16 -a 2001:db8:nodes::/64 \\
      --port 9100 \\
      --policy-name myorg-host-tcp-9100 \\
      --hep-prefix myorg-hep \\
      --interface eth0 \\
      --include-external-ip \\
      --policy-order 500 \\
      --hep-profile projectcalico-default-allow \\
      --gnp-apply-on-forward \\
      --gnp-performance-hint AssumeNeededOnEveryNode \\
      --backup-dir /tmp/calico-fw-backup
  %(prog)s --context prod delete --executor calicoctl --traffic-layer host \\
      --delete-hostendpoints \\
      --port 9100 \\
      --policy-name myorg-host-tcp-9100
  # 可选授权（等同 apply --confirm）： export CALICO_HOST_FW_CONFIRM=1
  # 仅下发 GNP、不创建本工具 HEP（须 --policy-selector 只命中你确认安全的已有 HEP；与上面「默认建 HEP」二选一）：
  # %(prog)s --context prod --executor calicoctl plan --traffic-layer host \\
  #   -a 172.20.0.0/16 -a 192.168.125.0/24 --port 9100 \\
  #   --no-hostendpoint \\
  #   --policy-selector 'has(kubernetes-host)' \\
  #   --policy-name myorg-host-tcp-9100 --policy-order 500
  # 对应 validate 须加 --no-hostendpoint，且不必 --interface（除非仍想校验注解逻辑）。
  # 【高危】不挂默认 Profile（须另有全局策略放行管理面；GNP 仍有末尾 Pass）：
  # %(prog)s ... apply ... --no-default-allow-profile
  # 对应 validate 须加 --no-default-allow-profile；若仍引用自定义 Profile 名可再传 --hep-profile <name>
  # Calico 为 Kubernetes 数据存储且已注册 projectcalico CRD 时：将上面 calicoctl 改为 kubectl，参数保持同名。

--------------------------------------------------------------------
示例 B：Pod 层（标准 Pod 网卡，非 hostNetwork；networking.k8s.io NetworkPolicy）
  须 --traffic-layer pod；不要传 --executor（实现固定 kubectl）。须 -n；目标 Pod 用多对 --pod-label（AND）
  或 --pod-selector-all（与 --pod-label 同时写时以后者为准并告警，勿依赖）。
  下列列全 Pod 侧 plan/apply 参数及根级 apply 选项；validate 窄 CLI 含 -n、--pod-label/--pod-selector-all、-a、--port、
  --k8s-np-name（与 apply 名称对齐；实现仅日志核对，不对 NetPol 做 kubectl get）。
--------------------------------------------------------------------
  %(prog)s -v --context prod preflight
  %(prog)s --context prod validate \\
      --traffic-layer pod \\
      -n monitoring \\
      --pod-label app.kubernetes.io/name=prometheus-node-exporter \\
      --pod-label app.kubernetes.io/component=metrics \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 2001:db8:prom::/64 \\
      --port 9100 \\
      --k8s-np-name myorg-pod-tcp-9100
  %(prog)s --context prod plan \\
      --traffic-layer pod \\
      -n monitoring \\
      --pod-label app.kubernetes.io/name=prometheus-node-exporter \\
      --pod-label app.kubernetes.io/component=metrics \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 2001:db8:prom::/64 \\
      --port 9100 \\
      --k8s-np-name myorg-pod-tcp-9100
  %(prog)s --context prod apply --dry-run=server \\
      --traffic-layer pod \\
      -n monitoring \\
      --pod-label app.kubernetes.io/name=prometheus-node-exporter \\
      --pod-label app.kubernetes.io/component=metrics \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 2001:db8:prom::/64 \\
      --port 9100 \\
      --k8s-np-name myorg-pod-tcp-9100
  %(prog)s --context prod apply --confirm --backup --post-verify \\
      --traffic-layer pod \\
      -n monitoring \\
      --pod-label app.kubernetes.io/name=prometheus-node-exporter \\
      --pod-label app.kubernetes.io/component=metrics \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 -a 2001:db8:prom::/64 \\
      --port 9100 \\
      --k8s-np-name myorg-pod-tcp-9100 \\
      --backup-dir /tmp/calico-fw-backup
  %(prog)s --context prod delete --traffic-layer pod \\
      -n monitoring \\
      --port 9100 \\
      --k8s-np-name myorg-pod-tcp-9100
  # 与「多标签选 Pod」二选一：整 namespace 全部 Pod（极高危，须书面审批）— 勿与上面两条 --pod-label 混用同一意图
  # %(prog)s --context prod validate --traffic-layer pod -n monitoring --pod-selector-all \\
  #   -a 172.20.0.0/16 --port 9100 --k8s-np-name myorg-pod-tcp-9100

某个子命令有哪些参数: %(prog)s plan -h、apply -h 等。
每条选项、环境变量、默认资源名: 见本文件 MAINTAINER_DOC 第 9 节。
原理和代码从哪读: 同上文件前半部分。
"""


def _build_help_epilog() -> str:
    """塞进文档链接；%(prog)s 留给 argparse 换成脚本名。"""
    return HELP_EPILOG % {
        "prog": "%(prog)s",
        "calico_docs": CALICO_DOCS_LATEST,
        "calico_ver": CALICO_COMPONENT_VERSIONS,
    }


class CalicoHostFwError(Exception):
    """业务错误"""


def setup_logging(verbose: bool = False, no_file: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    log = logging.getLogger("calico_host_port_lockdown")
    log.handlers.clear()
    log.setLevel(level)
    log.propagate = False
    fmt = logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if not no_file:
        try:
            from logging.handlers import RotatingFileHandler

            if not os.path.isdir(LOG_DIR):
                os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, DEFAULT_LOG_BASENAME)
            fh = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as e:
            log.warning("无法写入日志文件: %s", e)
    return log


logger = logging.getLogger("calico_host_port_lockdown")


def run_cmd(
    argv: Sequence[str],
    *,
    timeout: Optional[int] = None,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    kw: Dict[str, Any] = {"text": True}
    if input_text is not None:
        kw["input"] = input_text
    if timeout is not None:
        kw["timeout"] = timeout
    try:
        return subprocess.run(list(argv), capture_output=True, **kw)
    except subprocess.TimeoutExpired as e:
        cmd_s = " ".join(str(x) for x in (e.cmd if isinstance(e.cmd, (list, tuple)) else [e.cmd]))
        raise CalicoHostFwError(f"命令超时（{e.timeout}s）: {cmd_s}") from e
    except FileNotFoundError as e:
        exe = list(argv)[0] if argv else "?"
        raise CalicoHostFwError(f"找不到可执行文件: {exe}（请安装或指定 --kubectl / PATH）") from e


def _timeout_sec() -> int:
    raw = os.environ.get("CALICO_HOST_FW_KUBECTL_TIMEOUT", "120").strip()
    try:
        v = int(raw, 10)
    except ValueError:
        v = 120
    return max(10, min(3600, v))


def validate_cidrs(nets: List[str]) -> Tuple[List[str], List[str]]:
    """返回 (规范化 IPv4 CIDR 列表, 规范化 IPv6 CIDR 列表)。IPv4/IPv6 不可混在同一 Calico Rule 的 nets 中。"""
    v4: List[str] = []
    v6: List[str] = []
    for raw in nets:
        s = raw.strip()
        if not s:
            continue
        try:
            n = ipaddress.ip_network(s, strict=False)
        except ValueError as e:
            raise CalicoHostFwError(f"非法 CIDR: {raw!r} ({e})") from e
        if n.version == 4:
            v4.append(str(n))
        else:
            v6.append(str(n))
    if not v4 and not v6:
        raise CalicoHostFwError("至少指定一个有效 --allow-net")
    return v4, v6


def validate_calico_metadata_name(name: str, what: str) -> None:
    """Calico metadata.name：官方允许字母数字及 ._-，长度合理；过严会误伤合法名称，仅做基本校验。"""
    if not name or len(name) > 253:
        raise CalicoHostFwError(f"{what} 名称无效或过长（≤253）: {name!r}")
    if not re.match(r"^[a-zA-Z0-9._-]+$", name):
        raise CalicoHostFwError(f"{what} 名称含非法字符: {name!r}")


def validate_k8s_rfc1035_name(name: str, what: str) -> None:
    """Kubernetes 常见资源名（如 networking.k8s.io NetworkPolicy）DNS 子域风格校验。"""
    if not name or len(name) > 253:
        raise CalicoHostFwError(f"{what} 名称无效或过长: {name!r}")
    for part in name.split("."):
        if not part or len(part) > 63:
            raise CalicoHostFwError(f"{what} 名称段非法: {name!r}")
        if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", part):
            raise CalicoHostFwError(f"{what} 名称不符合 DNS 标签规范: {name!r}")


def sanitize_k8s_name(name: str, max_len: int = 63) -> str:
    n = name.lower().strip()
    n = re.sub(r"[^a-z0-9.-]+", "-", n)
    n = re.sub(r"-+", "-", n).strip("-")
    if not n:
        n = "unknown"
    return n[:max_len].rstrip("-.") or "x"


def kubectl_base(kubectl: str, context: Optional[str]) -> List[str]:
    cmd = [kubectl]
    if context:
        cmd.extend(["--context", context])
    return cmd


def kube_get_nodes_json(kubectl: str, context: Optional[str]) -> List[Dict[str, Any]]:
    base = kubectl_base(kubectl, context)
    r = run_cmd([*base, "get", "nodes", "-o", "json"], timeout=_timeout_sec())
    if r.returncode != 0:
        raise CalicoHostFwError(
            "获取 Node 列表失败:\n" + (r.stderr or r.stdout or "").strip()
        )
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise CalicoHostFwError("无法解析 kubectl get nodes 的 JSON") from e
    items = data.get("items") or []
    return [x for x in items if isinstance(x, dict)]


def node_internal_ips(node: Dict[str, Any]) -> Tuple[str, List[str]]:
    meta = node.get("metadata") or {}
    name = (meta.get("name") or "").strip()
    status = node.get("status") or {}
    addrs = status.get("addresses") or []
    ips: List[str] = []
    for a in addrs:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "InternalIP":
            continue
        addr = (a.get("address") or "").strip()
        if addr and addr not in ips:
            ips.append(addr)
    return name, ips


def _validate_explicit_host_interfaces(
    nodes: List[Dict[str, Any]],
    cli_interface: Optional[str],
) -> None:
    """创建 HostEndpoint 前校验：禁止 auto；每个节点须有注解或有效的 --interface 兜底。"""
    base = (cli_interface or "").strip()
    if base.lower() == "auto":
        raise CalicoHostFwError(
            "主机层创建 HostEndpoint 时禁止使用 --interface auto；"
            "请显式传入网卡名（如 ens192、eth0）或 Calico 通配 \"*\"，见官方 HostEndpoint。"
        )
    missing: List[str] = []
    for node in nodes:
        name, _ = node_internal_ips(node)
        if not name:
            continue
        ann = (node.get("metadata") or {}).get("annotations") or {}
        ov = ""
        if isinstance(ann, dict):
            ov = (ann.get(NODE_HOST_INTERFACE_ANNOTATION) or "").strip()
        if not ov and not base:
            missing.append(name)
    if missing:
        tail = ", ".join(missing[:24])
        if len(missing) > 24:
            tail += f", …(+{len(missing) - 24})"
        raise CalicoHostFwError(
            "主机层 HostEndpoint 的 interfaceName 必须由人工显式给出："
            "请使用 --interface <网卡名> 或 \"*\" 作为未注解节点的默认值，"
            f"或为每个节点设置注解 {NODE_HOST_INTERFACE_ANNOTATION!r}。"
            f"当前既无 --interface 又无注解的节点: {tail}"
        )


def build_host_iface_by_node(
    nodes: List[Dict[str, Any]],
    cli_interface: Optional[str],
) -> Dict[str, str]:
    """注解 kubeauto.calico/host-interface 优先，否则用 CLI --interface。"""
    base = (cli_interface or "").strip()
    out: Dict[str, str] = {}
    for node in nodes:
        name, _ = node_internal_ips(node)
        if not name:
            continue
        ann = (node.get("metadata") or {}).get("annotations") or {}
        override = ""
        if isinstance(ann, dict):
            override = (ann.get(NODE_HOST_INTERFACE_ANNOTATION) or "").strip()
        out[name] = override if override else base
    return out


def node_external_ips(node: Dict[str, Any]) -> List[str]:
    status = node.get("status") or {}
    addrs = status.get("addresses") or []
    ips: List[str] = []
    for a in addrs:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "ExternalIP":
            continue
        addr = (a.get("address") or "").strip()
        if addr and addr not in ips:
            ips.append(addr)
    return ips


def _ingress_allow_deny_for_family(nets: List[str], port: int, ip_version: int) -> str:
    """单一 IP 版本的一组 Allow + Deny 规则（Calico 要求 nets 不混用 v4/v6）。"""
    nets_lines = "\n".join(f"            - {n}" for n in nets)
    return f"""    - action: Allow
      protocol: TCP
      ipVersion: {ip_version}
      source:
        nets:
{nets_lines}
      destination:
        ports:
          - {port}
    - action: Deny
      protocol: TCP
      ipVersion: {ip_version}
      destination:
        ports:
          - {port}"""


def build_global_network_policy_yaml(
    name: str,
    allow_nets_v4: List[str],
    allow_nets_v6: List[str],
    port: int,
    policy_order: float,
    policy_selector: str,
    *,
    tier: Optional[str] = None,
    apply_on_forward: bool = False,
    performance_hints: Optional[List[str]] = None,
) -> str:
    validate_calico_metadata_name(name, "GlobalNetworkPolicy")
    ingress_parts: List[str] = []
    if allow_nets_v4:
        ingress_parts.append(_ingress_allow_deny_for_family(allow_nets_v4, port, 4))
    if allow_nets_v6:
        ingress_parts.append(_ingress_allow_deny_for_family(allow_nets_v6, port, 6))
    ingress_block = "\n".join(ingress_parts)
    # Calico：GNP 优先于 Profile；未命中本策略 Allow/Deny 的包不会自动套用 projectcalico-default-allow。
    # 官方 Pass 语义：跳过后续 GNP、进入端点 Profile。缺此条时常见误伤 kubelet 10250（非 failsafe 端口）。
    if ingress_block.strip():
        ingress_block += "\n    - action: Pass"
    sel_quoted = json.dumps(policy_selector)
    extra_lines: List[str] = []
    if tier and tier.strip():
        t = tier.strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", t):
            raise CalicoHostFwError(f"gnp-tier 仅允许字母数字、下划线、连字符: {t!r}")
        extra_lines.append(f"  tier: {t}")
    if apply_on_forward:
        extra_lines.append("  applyOnForward: true")
    hints = [h.strip() for h in (performance_hints or []) if h and str(h).strip()]
    for h in hints:
        if re.search(r"[\n\r:{}\[\]]", h):
            raise CalicoHostFwError(f"非法的 performanceHint（禁止换行及部分符号）: {h!r}")
    if hints:
        extra_lines.append("  performanceHints:")
        for h in hints:
            extra_lines.append(f"    - {h}")
    extra_yaml = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
    order_out = int(policy_order) if float(policy_order).is_integer() else policy_order
    return f"""apiVersion: projectcalico.org/v3
kind: GlobalNetworkPolicy
metadata:
  name: {name}
  labels:
    "{MANAGED_LABEL_KEY}": "{MANAGED_LABEL_VAL}"
  annotations:
    kubeauto.calico/tool: calico_host_port_lockdown
    kubeauto.calico/docs: {json.dumps(CALICO_DOCS_LATEST)}
    kubeauto.calico/gnp-ingress-tail: {json.dumps("Pass: Tigera GlobalNetworkPolicy — jump to endpoint Profile after port rules")}
spec:
  order: {order_out}
  selector: {sel_quoted}
  types:
    - Ingress{extra_yaml}
  ingress:
{ingress_block}
"""


def build_host_endpoint_yaml(
    hep_name: str,
    node_name: str,
    expected_ips: List[str],
    iface: str,
    profiles: List[str],
) -> str:
    validate_calico_metadata_name(hep_name, "HostEndpoint")
    if not expected_ips:
        raise CalicoHostFwError(f"节点 {node_name!r} 无可用 IP，跳过 HostEndpoint")
    ips_lines = "\n".join(f"    - {ip}" for ip in expected_ips)
    prof_lines = "\n".join(f"    - {p}" for p in profiles) if profiles else ""
    prof_block = f"  profiles:\n{prof_lines}\n" if profiles else ""
    return f"""apiVersion: projectcalico.org/v3
kind: HostEndpoint
metadata:
  name: {hep_name}
  labels:
    "{MANAGED_LABEL_KEY}": "{MANAGED_LABEL_VAL}"
spec:
  node: {node_name}
  interfaceName: "{iface}"
{prof_block}  expectedIPs:
{ips_lines}
"""


def _validate_k8s_namespace(ns: str) -> None:
    s = ns.strip()
    if not s or len(s) > 253:
        raise CalicoHostFwError(f"非法 namespace: {ns!r}")
    for part in s.split("."):
        if len(part) > 63:
            raise CalicoHostFwError(f"namespace 段过长: {ns!r}")
        if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", part):
            raise CalicoHostFwError(f"非法 namespace: {ns!r}")


def parse_pod_label_list(pod_labels: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in pod_labels:
        item = raw.strip()
        if not item:
            continue
        if "=" not in item:
            raise CalicoHostFwError(f"无效的 --pod-label: {raw!r}，应为 KEY=VALUE")
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            raise CalicoHostFwError(f"无效的 --pod-label: {raw!r}")
        out[k] = v
    return out


def require_namespace_for_netpol(namespace: Optional[str]) -> str:
    if not namespace or not namespace.strip():
        raise CalicoHostFwError("请指定 --namespace")
    _validate_k8s_namespace(namespace)
    return namespace.strip()


def require_pod_layer_args(
    namespace: Optional[str],
    pod_labels: Dict[str, str],
    pod_selector_all: bool,
) -> Tuple[str, Dict[str, str]]:
    ns = require_namespace_for_netpol(namespace)
    if pod_selector_all:
        if pod_labels:
            logger.warning(
                "[生产影响] 同时传入了 --pod-selector-all 与 --pod-label：策略将作用于命名空间 "
                "`%s` 内全部 Pod，--pod-label 会被忽略。若不符合预期请去掉其一后重试。",
                ns,
            )
        return ns, {}
    if not pod_labels:
        raise CalicoHostFwError(
            "请使用一个或多个 --pod-label KEY=VALUE 指定目标 Pod，"
            "或显式 --pod-selector-all（将匹配命名空间内全部 Pod，风险极高）"
        )
    return ns, pod_labels


def build_k8s_network_policy_yaml(
    name: str,
    namespace: str,
    pod_match_labels: Dict[str, str],
    allow_nets_v4: List[str],
    allow_nets_v6: List[str],
    port: int,
) -> str:
    """kubernetes.io NetworkPolicy：ingress 仅允许来自指定 CIDR 的 TCP 端口（含 Allow+隐式拒绝其它入站）。"""
    validate_k8s_rfc1035_name(name, "NetworkPolicy.metadata.name")
    cidrs = list(allow_nets_v4) + list(allow_nets_v6)
    if not cidrs:
        raise CalicoHostFwError("内部错误：无 CIDR")
    from_yaml = "\n".join(f"        - ipBlock:\n            cidr: {c}" for c in cidrs)
    if pod_match_labels:
        lbl_lines = "\n".join(
            f"      {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(pod_match_labels.items())
        )
        sel = f"    matchLabels:\n{lbl_lines}"
    else:
        sel = "    matchLabels: {}"
    return f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    "{K8S_NP_LABEL_KEY}": "{K8S_NP_LABEL_VAL}"
  annotations:
    kubeauto.calico/tool: calico_host_port_lockdown
spec:
  podSelector:
{sel}
  policyTypes:
    - Ingress
  ingress:
    - from:
{from_yaml}
      ports:
        - protocol: TCP
          port: {port}
"""


def _manifest_gnp_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    """从 CLI 解析 GlobalNetworkPolicy 扩展字段（validate 等无此字段时使用默认值）。"""
    tier_raw = getattr(args, "gnp_tier", None)
    tier = tier_raw.strip() if isinstance(tier_raw, str) and tier_raw.strip() else None
    raw_hints = getattr(args, "gnp_performance_hints", None) or []
    hints = [str(h).strip() for h in raw_hints if h and str(h).strip()]
    return {
        "gnp_tier": tier,
        "gnp_apply_on_forward": bool(getattr(args, "gnp_apply_on_forward", False)),
        "gnp_performance_hints": hints if hints else None,
    }


def effective_policy_selector(no_hostendpoint: bool, policy_selector: Optional[str]) -> str:
    if no_hostendpoint:
        if not (policy_selector and policy_selector.strip()):
            raise CalicoHostFwError(
                "使用 --no-hostendpoint 时必须指定 --policy-selector，"
                "以仅匹配您现有的 HostEndpoint（例如官方自动 HEP 同步的节点标签），"
                "避免 selector 使用 all() 误伤 Pod。"
            )
        return policy_selector.strip()
    return (
        policy_selector.strip()
        if policy_selector and policy_selector.strip()
        else DEFAULT_POLICY_SELECTOR_FOR_MANAGED_HEP
    )


def build_combined_manifest(
    *,
    policy_name: str,
    allow_nets_v4: List[str],
    allow_nets_v6: List[str],
    port: int,
    hep_prefix: str,
    nodes: List[Dict[str, Any]],
    include_external_ip: bool,
    iface_by_node: Dict[str, str],
    skip_hep: bool,
    skip_policy: bool = False,
    policy_order: float,
    policy_selector: str,
    hep_profiles: List[str],
    gnp_tier: Optional[str] = None,
    gnp_apply_on_forward: bool = False,
    gnp_performance_hints: Optional[List[str]] = None,
) -> str:
    parts: List[str] = []
    if not skip_policy:
        parts.append(
            build_global_network_policy_yaml(
                policy_name,
                allow_nets_v4,
                allow_nets_v6,
                port,
                policy_order,
                policy_selector,
                tier=gnp_tier,
                apply_on_forward=gnp_apply_on_forward,
                performance_hints=gnp_performance_hints,
            ).rstrip()
        )
    if not skip_hep:
        for node in nodes:
            n, ips = node_internal_ips(node)
            if not n:
                continue
            want = list(ips)
            if include_external_ip:
                for e in node_external_ips(node):
                    if e not in want:
                        want.append(e)
            hep_name = sanitize_k8s_name(f"{hep_prefix}-{n}")
            try:
                eff_iface = iface_by_node.get(n)
                if not eff_iface:
                    raise CalicoHostFwError(f"节点 {n!r} 缺少已解析的 interfaceName（内部错误）")
                parts.append(
                    build_host_endpoint_yaml(
                        hep_name, n, want, eff_iface, hep_profiles
                    ).rstrip()
                )
            except CalicoHostFwError as e:
                logger.warning("%s", e)
    return "\n---\n".join(parts) + "\n"


def _require_executor_for_host_traffic(args: argparse.Namespace, subcmd: str) -> None:
    """traffic-layer 含 host/both 时：必须由人工指定 kubectl 或 calicoctl，禁止脚本代选。"""
    tl = (getattr(args, "traffic_layer", None) or "").strip()
    if tl not in ("host", "both"):
        return
    ex = getattr(args, "executor", None)
    if ex not in ("kubectl", "calicoctl"):
        raise CalicoHostFwError(
            f"{subcmd}: --traffic-layer 为 host 或 both 时必须显式指定 "
            f"--executor kubectl（Calico CR 走 Kubernetes API，且集群已注册对应 CRD）或 "
            f"--executor calicoctl（Calico CR 走 calico 数据存储，如 kubeauto 默认 etcd）。禁止省略。"
        )


def _require_explicit_kube_context(args: argparse.Namespace, subcmd: str) -> None:
    if not (getattr(args, "context", None) or "").strip():
        raise CalicoHostFwError(
            f"{subcmd}: 须显式指定 --context（与 kubectl config get-contexts 中名称一致），"
            "禁止依赖当前默认 context 或已从环境变量注入的旧行为。"
        )


def _require_explicit_traffic_layer(args: argparse.Namespace, subcmd: str) -> None:
    tl = getattr(args, "traffic_layer", None)
    if tl not in ("host", "pod", "both"):
        raise CalicoHostFwError(
            f"{subcmd}: 须显式指定 --traffic-layer host|pod|both，禁止省略或依赖 CALICO_HOST_FW_TRAFFIC_LAYER。"
        )


def apply_manifest(
    manifest: str,
    *,
    executor: str,
    kubectl: str,
    calicoctl: str,
    context: Optional[str],
    dry_run: str,
) -> None:
    if executor == "calicoctl" and "kind: NetworkPolicy" in manifest:
        raise CalicoHostFwError(
            "清单中含 kubernetes.io NetworkPolicy，必须使用 kubectl apply（请使用 --executor kubectl）"
        )
    if executor == "kubectl":
        base = kubectl_base(kubectl, context)
        argv = [*base, "apply", "-f", "-"]
        if dry_run == "client":
            argv.append("--dry-run=client")
        elif dry_run == "server":
            argv.append("--dry-run=server")
        r = run_cmd(argv, input_text=manifest, timeout=_timeout_sec() * 3)
        if r.returncode != 0:
            raise CalicoHostFwError(
                "kubectl apply 失败:\n" + (r.stderr or r.stdout or "").strip()
            )
        logger.info("%s", (r.stdout or "").strip() or "(kubectl apply 成功，无输出)")
        return

    if executor == "calicoctl":
        if dry_run != "none":
            raise CalicoHostFwError("calicoctl 不支持 --dry-run，请使用 plan 预览或改用 --executor kubectl")
        argv = [*_calicoctl_argv(calicoctl, context), "apply", "-f", "-"]
        r = run_cmd(argv, input_text=manifest, timeout=_timeout_sec() * 3)
        if r.returncode != 0:
            raise CalicoHostFwError(
                "calicoctl apply 失败:\n" + (r.stderr or r.stdout or "").strip()
            )
        logger.info("%s", (r.stdout or "").strip() or "(calicoctl apply 成功，无输出)")
        return

    raise CalicoHostFwError(f"未知 executor: {executor!r}")


def _calicoctl_argv(calicoctl: str, context: Optional[str]) -> List[str]:
    out = [calicoctl]
    if context and str(context).strip():
        out.extend(["--context", str(context).strip()])
    return out


def _kubectl_projectcalico_resource(kubectl: str, context: Optional[str], needle: str) -> str:
    base = kubectl_base(kubectl, context)
    r = run_cmd([*base, "api-resources", "-o", "name"], timeout=_timeout_sec())
    if r.returncode != 0:
        return ""
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        if not s or "projectcalico" not in s.lower():
            continue
        if needle.lower() in s.lower():
            return s
    return ""


def _kubectl_delete_label(
    kubectl: str,
    context: Optional[str],
    resource_api_name: str,
    label_selector: str,
) -> subprocess.CompletedProcess:
    base = kubectl_base(kubectl, context)
    return run_cmd(
        [*base, "delete", resource_api_name, "-l", label_selector, "--ignore-not-found"],
        timeout=_timeout_sec(),
    )


def _calicoctl_get_profile_yaml(
    calicoctl: str, context: Optional[str], profile_name: str
) -> subprocess.CompletedProcess:
    return run_cmd(
        [*_calicoctl_argv(calicoctl, context), "get", "profile", profile_name, "-o", "yaml"],
        timeout=_timeout_sec(),
    )


def _backup_host_resources_calicoctl(
    calicoctl: str,
    context: Optional[str],
    policy_name: str,
    backup_dir: str,
) -> None:
    """etcd 等经 calicoctl 管理的主机层：备份与 kubectl CRD 路径分离，避免空备份。"""
    r = run_cmd(
        [*_calicoctl_argv(calicoctl, context), "get", "globalnetworkpolicy", policy_name, "-o", "yaml"],
        timeout=_timeout_sec(),
    )
    if r.returncode == 0 and (r.stdout or "").strip() and not (r.stdout or "").strip().startswith("null"):
        path = os.path.join(backup_dir, f"gnp-{sanitize_k8s_name(policy_name, 200)}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.stdout or "")
        logger.info("已备份现有 GlobalNetworkPolicy（calicoctl）-> %s", path)
    elif r.returncode != 0:
        logger.info(
            "无现有 GlobalNetworkPolicy 可备份或 calicoctl get 失败（可忽略）: %s",
            (r.stderr or r.stdout or "").strip()[:300],
        )
    else:
        logger.info("无现有 GlobalNetworkPolicy（calicoctl 返回空）；跳过 GNP 备份文件")

    rj = run_cmd(
        [*_calicoctl_argv(calicoctl, context), "get", "hostendpoints", "-o", "json"],
        timeout=_timeout_sec(),
    )
    if rj.returncode != 0:
        rj = run_cmd(
            [*_calicoctl_argv(calicoctl, context), "get", "hep", "-o", "json"],
            timeout=_timeout_sec(),
        )
    if rj.returncode != 0 or not (rj.stdout or "").strip():
        logger.info(
            "备份 HostEndpoint（calicoctl JSON）失败或无输出（可忽略）: %s",
            (rj.stderr or "").strip()[:300],
        )
        return
    try:
        data = json.loads(rj.stdout or "{}")
    except json.JSONDecodeError:
        logger.info("calicoctl get hep -o json 无法解析，跳过 HEP 备份")
        return
    items = data.get("items")
    if not isinstance(items, list):
        return
    kept: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        labels = meta.get("labels") or {}
        if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VAL:
            continue
        kept.append(item)
    if not kept:
        logger.info("当前无带本工具标签的 HostEndpoint，跳过 HEP 备份 JSON")
        return
    out_doc = {"apiVersion": data.get("apiVersion") or "projectcalico.org/v3", "kind": "List", "items": kept}
    path2 = os.path.join(backup_dir, "hostendpoints-kubeauto.json")
    with open(path2, "w", encoding="utf-8") as f:
        json.dump(out_doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("已备份带工具标签的 HostEndpoint（calicoctl JSON，共 %d 条）-> %s", len(kept), path2)


def _calicoctl_managed_hostendpoint_names(
    calicoctl: str, context: Optional[str] = None
) -> List[str]:
    """从 calicoctl JSON 列出带本工具标签的 HostEndpoint 名（不依赖 kubectl -l）。"""
    for kind in ("hostendpoints", "hep"):
        r = run_cmd(
            [*_calicoctl_argv(calicoctl, context), "get", kind, "-o", "json"],
            timeout=_timeout_sec(),
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            continue
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        items = data.get("items")
        if not isinstance(items, list):
            continue
        out: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            labels = meta.get("labels") or {}
            if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VAL:
                continue
            name = (meta.get("name") or "").strip()
            if name:
                out.append(name)
        return sorted(out)
    logger.warning(
        "calicoctl get hostendpoints -o json 未得到可用清单，无法自动枚举本工具 HostEndpoint；"
        "请手工执行 calicoctl get hep 后逐个 calicoctl delete hep <NAME>。"
    )
    return []


def _backup_dir_path(base: Optional[str]) -> str:
    if base and base.strip():
        return os.path.abspath(base.strip())
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = os.path.join(LOG_DIR, "calico_host_fw_backup", ts)
    return d


def backup_managed_resources(
    kubectl: str,
    context: Optional[str],
    policy_name: str,
    backup_dir: str,
    *,
    backup_host_resources: bool = True,
    backup_pod_resources: bool = False,
    host_executor: Optional[str] = None,
    calicoctl: str = "calicoctl",
    k8s_np_name: Optional[str] = None,
    k8s_namespace: Optional[str] = None,
) -> None:
    os.makedirs(backup_dir, exist_ok=True)
    base = kubectl_base(kubectl, context)

    if backup_host_resources:
        if host_executor == "calicoctl":
            _backup_host_resources_calicoctl(calicoctl, context, policy_name, backup_dir)
        else:
            gnp_res = (
                _kubectl_projectcalico_resource(kubectl, context, "globalnetworkpolic")
                or "globalnetworkpolicies.projectcalico.org"
            )
            r = run_cmd(
                [*base, "get", gnp_res, policy_name, "-o", "yaml"],
                timeout=_timeout_sec(),
            )
            if r.returncode == 0 and (r.stdout or "").strip():
                path = os.path.join(backup_dir, f"gnp-{sanitize_k8s_name(policy_name, 200)}.yaml")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(r.stdout or "")
                logger.info("已备份现有 GlobalNetworkPolicy -> %s", path)
            elif r.returncode != 0:
                logger.info(
                    "无现有 GlobalNetworkPolicy 可备份或 get 失败（可忽略）: %s",
                    (r.stderr or "").strip()[:300],
                )
            hep_res = (
                _kubectl_projectcalico_resource(kubectl, context, "hostendpoint")
                or "hostendpoints.projectcalico.org"
            )
            r2 = run_cmd(
                [
                    *base,
                    "get",
                    hep_res,
                    "-l",
                    f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VAL}",
                    "-o",
                    "yaml",
                ],
                timeout=_timeout_sec(),
            )
            if r2.returncode == 0 and (r2.stdout or "").strip():
                path2 = os.path.join(backup_dir, "hostendpoints-kubeauto.yaml")
                with open(path2, "w", encoding="utf-8") as f:
                    f.write(r2.stdout or "")
                logger.info("已备份现有 HostEndpoint（工具标签） -> %s", path2)

    if backup_pod_resources and k8s_np_name and k8s_namespace:
        r3 = run_cmd(
            [*base, "get", "networkpolicy", k8s_np_name, "-n", k8s_namespace, "-o", "yaml"],
            timeout=_timeout_sec(),
        )
        if r3.returncode == 0 and (r3.stdout or "").strip():
            path3 = os.path.join(
                backup_dir,
                f"netpol-{sanitize_k8s_name(k8s_namespace, 40)}-{sanitize_k8s_name(k8s_np_name, 120)}.yaml",
            )
            with open(path3, "w", encoding="utf-8") as f:
                f.write(r3.stdout or "")
            logger.info("已备份现有 NetworkPolicy -> %s", path3)
        elif r3.returncode != 0:
            logger.info(
                "无现有 NetworkPolicy 可备份或 get 失败（可忽略）: %s",
                (r3.stderr or "").strip()[:300],
            )


def cmd_preflight(args: argparse.Namespace) -> int:
    """集群与 Calico API 可读性检查（不修改集群）。"""
    _require_explicit_kube_context(args, "preflight")
    to = _timeout_sec()
    base = kubectl_base(args.kubectl, args.context)
    logger.info("Calico 文档基线: %s", CALICO_DOCS_LATEST)
    logger.info("组件版本对照: %s", CALICO_COMPONENT_VERSIONS)

    ver = run_cmd([*base, "version", "--client=true", "-o", "json"], timeout=30)
    if ver.returncode == 0 and ver.stdout:
        try:
            j = json.loads(ver.stdout)
            c = j.get("clientVersion") or {}
            logger.info("kubectl client: gitVersion=%s", c.get("gitVersion", "?"))
        except json.JSONDecodeError:
            logger.info("kubectl version 输出非 JSON，略过解析")

    co = run_cmd([*base, "cluster-info"], timeout=to)
    if co.returncode != 0:
        raise CalicoHostFwError("kubectl cluster-info 失败，请检查 kubeconfig 与网络:\n" + (co.stderr or co.stdout or "").strip())
    logger.info("cluster-info: ok")

    calico_ns = run_cmd(
        [*base, "get", "pods", "-A", "-l", "k8s-app=calico-node", "-o", "json"], timeout=to
    )
    if calico_ns.returncode == 0 and calico_ns.stdout:
        try:
            pods = json.loads(calico_ns.stdout).get("items") or []
            logger.info("发现 calico-node Pod（按 k8s-app=calico-node）数量: %d", len(pods))
            if not pods:
                logger.warning(
                    "[生产影响] 未找到 k8s-app=calico-node 的 Pod。若当前集群 CNI 不是 Calico，"
                    "对本脚本生成的主机层 GlobalNetworkPolicy/HostEndpoint 请勿在生产执行 apply，"
                    "否则对象可能被拒或策略行为不可预期。"
                )
        except json.JSONDecodeError:
            pass

    ar = run_cmd([*base, "api-resources", "-o", "name"], timeout=to)
    if ar.returncode == 0:
        names = ar.stdout or ""
        if "globalnetworkpolicies" in names.lower() and "projectcalico" in names.lower():
            logger.info("api-resources: 发现 projectcalico GlobalNetworkPolicy 资源名")
        else:
            logger.warning("api-resources 中未明确发现 globalnetworkpolicies.projectcalico.org；仍可能使用 calicoctl")
        if "hostendpoints" in names.lower() and "projectcalico" in names.lower():
            logger.info("api-resources: 发现 projectcalico HostEndpoint 资源名")
        else:
            logger.warning("api-resources 中未明确发现 hostendpoints；请确认已装 Calico API / CRD")
        if "networkpolicies" in names.lower() and "networking.k8s.io" in names.lower():
            logger.info("api-resources: 发现 networking.k8s.io NetworkPolicy（Pod 层策略依赖 CNI 执行）")

    kc = shutil.which(args.calicoctl)
    if kc:
        cv = run_cmd([args.calicoctl, "version"], timeout=30)
        if cv.returncode == 0:
            logger.info("calicoctl: %s", (cv.stdout or cv.stderr or "").strip()[:500])
        pr = _calicoctl_get_profile_yaml(args.calicoctl, args.context, DEFAULT_HEP_PROFILE)
        if pr.returncode != 0:
            logger.warning(
                "[生产影响] preflight: calicoctl 无法读取默认 Profile %r（本工具 HostEndpoint 默认挂载）。"
                " 若直接 apply 默认配置，GNP 中 Pass 可能无 Profile 可交接，非目标端口入站易为 Deny。"
                " 请核对 Calico 数据存储或改用 --hep-profile / --no-default-allow-profile 并评估风险。",
                DEFAULT_HEP_PROFILE,
            )
        else:
            logger.info("preflight: Calico Profile %r 存在且可读（calicoctl）", DEFAULT_HEP_PROFILE)

    logger.info(
        "预检完成。下一步建议: 各子命令须显式 --context；plan/validate/apply/delete 另须 --traffic-layer；"
        "plan 审 YAML；apply 时使用 --confirm 或环境变量 %s=1。",
        CONFIRM_ENV,
    )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    _require_explicit_kube_context(args, "plan")
    _require_explicit_traffic_layer(args, "plan")
    allow_v4, allow_v6 = validate_cidrs(list(args.allow_net))
    tl = args.traffic_layer
    parts: List[str] = []
    if tl in ("host", "both"):
        _require_executor_for_host_traffic(args, "plan")
        nodes = kube_get_nodes_json(args.kubectl, args.context)
        policy_name = args.policy_name or DEFAULT_POLICY_NAME_TEMPLATE.format(port=args.port)
        validate_calico_metadata_name(policy_name, "GlobalNetworkPolicy")
        sel = effective_policy_selector(args.no_hostendpoint, args.policy_selector)
        hep_prof = [] if args.no_default_allow_profile else [args.hep_profile]
        gk = _manifest_gnp_kwargs(args)
        iface_by_node: Dict[str, str] = {}
        if not args.no_hostendpoint:
            _validate_explicit_host_interfaces(nodes, args.interface)
            iface_by_node = build_host_iface_by_node(nodes, args.interface)
        parts.append(
            build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface_by_node=iface_by_node,
                skip_hep=args.no_hostendpoint,
                skip_policy=False,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            ).rstrip()
        )
        if not args.no_hostendpoint:
            logger.info(
                "plan: 主机层 YAML 为「单 TCP 端口收紧 + GNP ingress 末尾 Pass」；Pass 将未命中规则交 HEP Profile（默认 %s）。"
                " 全节点硬收口请另写 GNP 或调 tier/order，参见 %snetwork-policy/hosts/kubernetes-nodes",
                DEFAULT_HEP_PROFILE,
                CALICO_DOCS_LATEST,
            )
    if tl in ("pod", "both"):
        pod_labels = parse_pod_label_list(list(args.pod_labels))
        ns, pl = require_pod_layer_args(args.namespace, pod_labels, args.pod_selector_all)
        np_name = args.k8s_np_name or DEFAULT_K8S_NP_NAME_TEMPLATE.format(port=args.port)
        validate_k8s_rfc1035_name(np_name, "NetworkPolicy")
        parts.append(
            build_k8s_network_policy_yaml(
                np_name, ns, pl, allow_v4, allow_v6, args.port
            ).rstrip()
        )
    sys.stdout.write("\n---\n".join(parts) + "\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """校验 CIDR；host 层启发式核对 Node IP；pod 层统计匹配 Pod 数量。"""
    _require_explicit_kube_context(args, "validate")
    _require_explicit_traffic_layer(args, "validate")
    allow_v4, allow_v6 = validate_cidrs(list(args.allow_net))
    tl = args.traffic_layer
    warnings = 0

    def _warn_node_ip_not_in_allow(node_name: str, role: str, ip_text: str) -> int:
        try:
            addr = ipaddress.ip_address(ip_text)
        except ValueError:
            return 0
        pool = nets_v4 if addr.version == 4 else nets_v6
        if not pool:
            return 0
        if any(addr in n for n in pool):
            return 0
        logger.warning(
            "[生产影响] 节点 %s 的 %s %s 不在任一 --allow-net 内。"
            " apply 后从该地址集的采集/SSH 等访问目标端口可能被 GNP deny；"
            " 请确认 Prometheus、跳板、集群内客户端真实源网段已写入 -a。",
            node_name,
            role,
            ip_text,
        )
        return 1

    if tl in ("host", "both"):
        _require_executor_for_host_traffic(args, "validate")
        nets_v4 = [ipaddress.ip_network(c, strict=False) for c in allow_v4]
        nets_v6 = [ipaddress.ip_network(c, strict=False) for c in allow_v6]
        nodes = kube_get_nodes_json(args.kubectl, args.context)
        if not args.no_hostendpoint:
            _validate_explicit_host_interfaces(nodes, args.interface)
        for node in nodes:
            name, ips_int = node_internal_ips(node)
            for ip_s in ips_int:
                warnings += _warn_node_ip_not_in_allow(name, "InternalIP", ip_s)
            if args.include_external_ip:
                for ip_s in node_external_ips(node):
                    warnings += _warn_node_ip_not_in_allow(name, "ExternalIP", ip_s)

        ex = str(getattr(args, "executor", "") or "")
        if not args.no_hostendpoint:
            prof_name = (getattr(args, "hep_profile", None) or DEFAULT_HEP_PROFILE).strip()
            if getattr(args, "no_default_allow_profile", False):
                logger.warning(
                    "[生产影响] validate: --no-default-allow-profile：HEP 无 Profile；GNP 末尾 Pass 依官方语义"
                    " 在无 Profile 时对未命中规则入站为 Deny，仅本策略内对该 TCP 端口的 Allow/Deny 生效。"
                    " 须有其它全局策略或按 Tigera「Protect Kubernetes nodes」显式放开 10250/6443 等管理面端口。",
                )
                warnings += 1
            elif ex == "calicoctl" and prof_name:
                pr = _calicoctl_get_profile_yaml(args.calicoctl, args.context, prof_name)
                if pr.returncode != 0:
                    logger.warning(
                        "[生产影响] validate: calicoctl 无法读取 Profile %r（将写入 HEP.spec.profiles）。"
                        " apply 可能被拒或 Pass 后无 Profile；详情: %s",
                        prof_name,
                        (pr.stderr or pr.stdout or "").strip()[:400],
                    )
                    warnings += 1
                else:
                    logger.info("validate: Calico Profile %r 可读（calicoctl）", prof_name)
            elif ex == "kubectl" and prof_name:
                prof_res = _kubectl_projectcalico_resource(
                    args.kubectl, args.context, "profiles.projectcalico"
                )
                if prof_res:
                    kb = kubectl_base(args.kubectl, args.context)
                    prk = run_cmd(
                        [*kb, "get", prof_res, prof_name, "-o", "yaml"],
                        timeout=_timeout_sec(),
                    )
                    if prk.returncode != 0:
                        logger.warning(
                            "[生产影响] validate: kubectl 无法读取 %s/%s；HEP 若引用该 Profile 可能异常。%s",
                            prof_res,
                            prof_name,
                            (prk.stderr or "").strip()[:300],
                        )
                        warnings += 1
                    else:
                        logger.info("validate: Profile %s/%s 可读（kubectl）", prof_res, prof_name)
                else:
                    logger.info(
                        "validate: kubectl api-resources 未发现 profiles.projectcalico CRD，跳过 Profile 存在性检查"
                        "（主机层若走 calicoctl，请用 --executor calicoctl 做 Profile 校验）。"
                    )

    if tl in ("pod", "both"):
        pod_labels = parse_pod_label_list(list(args.pod_labels))
        ns, pl = require_pod_layer_args(args.namespace, pod_labels, args.pod_selector_all)
        base = kubectl_base(args.kubectl, args.context)
        argv = [*base, "get", "pods", "-n", ns, "-o", "json"]
        if pl:
            lab = ",".join(f"{k}={v}" for k, v in sorted(pl.items()))
            argv.extend(["-l", lab])
        r = run_cmd(argv, timeout=_timeout_sec())
        if r.returncode != 0:
            raise CalicoHostFwError(
                "validate: 无法列出 Pod %s/%s: %s" % (ns, pl or "(all)", (r.stderr or "").strip())
            )
        try:
            items = json.loads(r.stdout or "{}").get("items") or []
        except json.JSONDecodeError as e:
            raise CalicoHostFwError("validate: 解析 Pod 列表 JSON 失败") from e
        n_pods = len([x for x in items if isinstance(x, dict)])
        logger.info("validate: namespace=%s 匹配 Pod 数=%d（仅供核对）", ns, n_pods)
        np_name = args.k8s_np_name or DEFAULT_K8S_NP_NAME_TEMPLATE.format(port=args.port)
        logger.info(
            "validate: 与 plan/apply 对齐的 NetworkPolicy.metadata.name=%s（本命令不对该对象做 kubectl get，仅名称核对）",
            np_name,
        )
        if n_pods == 0:
            logger.warning(
                "[生产影响] validate: 当前选择下 namespace=%s 无匹配 Pod；"
                " apply 后该 NetworkPolicy 实际不保护任何工作负载（等于对目标端口策略空转）。"
                " 请核对 --pod-label、-n。",
                ns,
            )
            warnings += 1

    if warnings:
        logger.info("validate: 完成，有 %d 条提示（请结合拓扑核对）", warnings)
    else:
        logger.info("validate: 未触发告警项（CIDR/Node/Pod 核对通过；数据面请自行抽测）。")
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    _require_explicit_kube_context(args, "nodes")
    nodes = kube_get_nodes_json(args.kubectl, args.context)
    for node in nodes:
        name, ips = node_internal_ips(node)
        ext = node_external_ips(node)
        logger.info(
            "node=%s internal_ip=%s external_ip=%s",
            name,
            ",".join(ips) or "-",
            ",".join(ext) or "-",
        )
    return 0


def _require_confirm(args: argparse.Namespace) -> None:
    if args.dry_run and args.dry_run != "none":
        logger.info(
            "[仅此校验] dry-run=%s：不向集群持久化对象，无需 --confirm。",
            args.dry_run,
        )
        return
    if args.confirm:
        return
    env_raw = os.environ.get(CONFIRM_ENV, "").strip().lower()
    if env_raw in ("1", "true", "yes"):
        logger.warning(
            "[生产影响] 环境变量 %s 已授权「写集群」（等同 apply --confirm）。"
            " 请勿在共享 shell profile、未审核 CI 或错误 kubeconfig 会话中保留该变量；"
            " 用毕请 unset %s。",
            CONFIRM_ENV,
            CONFIRM_ENV,
        )
        return
    raise CalicoHostFwError(
        "生产级保护：真正写集群前请显式添加 --confirm，或设置环境变量 "
        f"{CONFIRM_ENV}=1（用毕请 unset）。仍建议先 plan，并在需要时用 --dry-run=server。"
    )


def _log_apply_production_impact(
    args: argparse.Namespace,
    policy_name: str,
    np_name: str,
) -> None:
    if args.dry_run and args.dry_run != "none":
        return
    ctx = str(args.context)
    logger.warning(
        "[生产影响] 即将向 API 提交策略对象（context=%s）。"
        " -a 漏配或 GNP selector 过宽可能导致监控/跳板被拒或误伤多节点；"
        " 完整清单见本文件 MAINTAINER_DOC 第 0、9 节。",
        ctx,
    )
    desc: List[str] = []
    tl = args.traffic_layer
    if tl in ("host", "both"):
        desc.append(
            "Calico GlobalNetworkPolicy `%s` + 本工具 HostEndpoint（除非已 --no-hostendpoint）"
            % policy_name
        )
    if tl in ("pod", "both"):
        desc.append(
            "命名空间 `%s` 的 NetworkPolicy `%s`"
            % (args.namespace or "(未指定，异常)", np_name)
        )
    logger.warning("[生产影响] 将创建或更新的主要对象: %s", "；".join(desc))


def _cli_invocation_hint() -> str:
    """日志里可复制整条命令时的脚本前缀（与当前解释器一致，避免仅有 python3 的环境）。"""
    return "%s %s" % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))


def _argv_suggested_validate(args: argparse.Namespace) -> List[str]:
    """与本次 apply 对齐的 validate 子命令参数（不含脚本名）。"""
    out: List[str] = []
    if getattr(args, "verbose", False):
        out.append("-v")
    if getattr(args, "no_log_file", False):
        out.append("--no-log-file")
    kc = getattr(args, "kubectl", None) or "kubectl"
    if kc != "kubectl":
        out.extend(["--kubectl", kc])
    if getattr(args, "context", None):
        out.extend(["--context", args.context])
    if getattr(args, "executor", None):
        out.extend(["--executor", args.executor])
    cal = getattr(args, "calicoctl", None) or "calicoctl"
    if cal != "calicoctl":
        out.extend(["--calicoctl", cal])
    out.append("validate")
    for c in list(args.allow_net):
        s = (c or "").strip()
        if s:
            out.extend(["-a", s])
    out.extend(["--port", str(args.port)])
    out.extend(["--traffic-layer", args.traffic_layer])
    if getattr(args, "namespace", None):
        out.extend(["-n", args.namespace])
    if getattr(args, "pod_selector_all", False):
        out.append("--pod-selector-all")
    for raw in list(args.pod_labels):
        if raw and str(raw).strip():
            out.extend(["--pod-label", str(raw).strip()])
    if getattr(args, "k8s_np_name", None):
        out.extend(["--k8s-np-name", args.k8s_np_name])
    if getattr(args, "traffic_layer", None) in ("host", "both"):
        iface = getattr(args, "interface", None)
        if iface is not None and str(iface).strip():
            out.extend(["--interface", str(iface).strip()])
    if getattr(args, "include_external_ip", False):
        out.append("--include-external-ip")
    if getattr(args, "no_hostendpoint", False):
        out.append("--no-hostendpoint")
    if getattr(args, "no_default_allow_profile", False):
        out.append("--no-default-allow-profile")
    hp = getattr(args, "hep_profile", None)
    if hp and str(hp).strip():
        hp_s = str(hp).strip()
        if hp_s != DEFAULT_HEP_PROFILE or getattr(args, "no_default_allow_profile", False):
            out.extend(["--hep-profile", hp_s])
    return out


def _suggested_validate_cli_line(args: argparse.Namespace) -> str:
    return _cli_invocation_hint() + " " + " ".join(shlex.quote(x) for x in _argv_suggested_validate(args))


def _log_apply_success_followup(args: argparse.Namespace) -> None:
    """apply 成功后：成功提示 + 与官方变更流程对齐的后期校验指引（闭环）。"""
    vline = _suggested_validate_cli_line(args)
    dr = getattr(args, "dry_run", "none") or "none"
    if dr != "none":
        logger.info("[后续] 本次 apply 为 dry-run=%s：未向集群持久化策略对象。", dr)
        logger.info(
            "[后续·闭环] apiserver 校验通过且 YAML 符合预期后，再使用 --dry-run=none 与 --confirm "
            "正式下发；落地后必须再跑同参 validate，并对目标端口做允许源/拒绝源抽测。"
        )
        logger.info("[后续] 与本次参数对齐的 validate（可随时先跑，只读）:\n  %s", vline)
        return
    logger.info("[成功] apply 已提交（清单写入 Calico/Kubernetes 数据面；以 calicoctl/kubectl 退出码为准）。")
    logger.info(
        "[后续·闭环][1] 立即执行下方 validate：仅核对 CIDR、节点 IP、Pod 命中数；不能检测 Felix/BIRD 是否故障。"
    )
    logger.info(
        "[后续·闭环][2] 数据面抽测：从 -a 内与 -a 外各选一源，探测目标 TCP %s；并观察 calico-node / BGP。",
        args.port,
    )
    logger.info("[后续] validate 命令行（建议原样复制执行）:\n  %s", vline)
    tl = getattr(args, "traffic_layer", "host") or "host"
    res_ex = getattr(args, "executor", None) if tl in ("host", "both") else None
    cal = getattr(args, "calicoctl", "calicoctl") or "calicoctl"
    if tl in ("host", "both") and res_ex == "calicoctl":
        pn = getattr(args, "policy_name", None) or DEFAULT_POLICY_NAME_TEMPLATE.format(port=args.port)
        logger.info(
            "[后续] 主机层对象在 Calico 数据存储（非 K8s CRD）时，请用 calicoctl 核对，例如:\n"
            "  %s get globalnetworkpolicy %s\n"
            "  %s get hep",
            shlex.quote(cal),
            shlex.quote(pn),
            shlex.quote(cal),
        )
    if tl in ("host", "both") and res_ex == "kubectl":
        logger.info(
            "[后续] 主机层走 K8s API 时可用 kubectl get globalnetworkpolicies.projectcalico.org、"
            "hostendpoints.projectcalico.io（以 kubectl api-resources 实际名为准）。"
        )
    if tl in ("pod", "both"):
        logger.info(
            "[后续] Pod 层请 kubectl get networkpolicy -n <namespace> 核对命名与标签是否与预期一致。"
        )
    if getattr(args, "post_verify", False):
        logger.info("[闭环] 已启用 --post-verify，本进程将接着运行上述 validate。")


def _finish_apply_success(args: argparse.Namespace) -> int:
    _log_apply_success_followup(args)
    if getattr(args, "post_verify", False):
        return cmd_validate(args)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    _require_explicit_kube_context(args, "apply")
    _require_explicit_traffic_layer(args, "apply")
    allow_v4, allow_v6 = validate_cidrs(list(args.allow_net))
    if not (MIN_PORT <= args.port <= MAX_PORT):
        raise CalicoHostFwError(f"端口范围非法: {args.port}")
    _require_confirm(args)

    tl = args.traffic_layer
    host_ex: Optional[str] = None
    if tl in ("host", "both"):
        _require_executor_for_host_traffic(args, "apply")
        host_ex = str(args.executor)

    policy_name = args.policy_name or DEFAULT_POLICY_NAME_TEMPLATE.format(port=args.port)
    np_name = args.k8s_np_name or DEFAULT_K8S_NP_NAME_TEMPLATE.format(port=args.port)
    if tl in ("host", "both"):
        validate_calico_metadata_name(policy_name, "GlobalNetworkPolicy")
    if tl in ("pod", "both"):
        validate_k8s_rfc1035_name(np_name, "NetworkPolicy")

    nodes: Optional[List[Dict[str, Any]]] = None
    if tl in ("host", "both"):
        nodes = kube_get_nodes_json(args.kubectl, args.context)

    sel = (
        effective_policy_selector(args.no_hostendpoint, args.policy_selector)
        if tl in ("host", "both")
        else ""
    )
    hep_prof = [] if args.no_default_allow_profile else [args.hep_profile]
    gk = _manifest_gnp_kwargs(args)

    iface_by_node: Dict[str, str] = {}
    if tl in ("host", "both") and nodes is not None and not args.no_hostendpoint:
        _validate_explicit_host_interfaces(nodes, args.interface)
        iface_by_node = build_host_iface_by_node(nodes, args.interface)

    if tl in ("host", "both"):
        if args.no_default_allow_profile:
            logger.warning(
                "[生产影响] 已启用 --no-default-allow-profile：HostEndpoint 不挂载 "
                "projectcalico-default-allow。GNP 仍含末尾 Pass；无 Profile 时官方语义为 Deny（仅本 GNP 内"
                " 对该 TCP 端口的规则生效）。若无其它全局放行，kubelet 10250、apiserver 6443 等可能中断；"
                "参见 Tigera「Protect Kubernetes nodes」按端口显式 Allow。"
            )
        if args.no_hostendpoint:
            logger.warning(
                "[生产影响] 已启用 --no-hostendpoint：仅下发 GlobalNetworkPolicy，不创建本工具 HEP；"
                " 请务必确认 --policy-selector 仅命中预期主机端点，否则可能大范围拒收。"
            )

    if args.dry_run == "none":
        _log_apply_production_impact(args, policy_name, np_name)

    if args.backup:
        bdir = _backup_dir_path(args.backup_dir)
        logger.info("备份目录: %s", bdir)
        ns_bak: Optional[str] = None
        if tl in ("pod", "both"):
            ns_bak, _ = require_pod_layer_args(
                args.namespace,
                parse_pod_label_list(list(args.pod_labels)),
                args.pod_selector_all,
            )
        backup_managed_resources(
            args.kubectl,
            args.context,
            policy_name,
            bdir,
            backup_host_resources=tl in ("host", "both"),
            backup_pod_resources=tl in ("pod", "both"),
            host_executor=host_ex if tl in ("host", "both") else None,
            calicoctl=args.calicoctl,
            k8s_np_name=np_name if tl in ("pod", "both") else None,
            k8s_namespace=ns_bak,
        )

    def _emit(manifest: str, executor: str) -> None:
        apply_manifest(
            manifest,
            executor=executor,
            kubectl=args.kubectl,
            calicoctl=args.calicoctl,
            context=args.context,
            dry_run=args.dry_run or "none",
        )

    if tl == "pod":
        pod_labels = parse_pod_label_list(list(args.pod_labels))
        ns, pl = require_pod_layer_args(args.namespace, pod_labels, args.pod_selector_all)
        if args.apply_staged:
            logger.warning(
                "[生产影响] traffic-layer=pod 时不存在主机 HEP 分阶段下发，已忽略 --apply-staged。"
            )
        _emit(
            build_k8s_network_policy_yaml(
                np_name, ns, pl, allow_v4, allow_v6, args.port
            ),
            "kubectl",
        )
        return _finish_apply_success(args)

    if tl == "host":
        assert nodes is not None
        if args.apply_staged:
            gnp_only = build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface_by_node=iface_by_node,
                skip_hep=True,
                skip_policy=False,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            )
            _emit(gnp_only, host_ex)
            hep_only = build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface_by_node=iface_by_node,
                skip_hep=False,
                skip_policy=True,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            )
            _emit(hep_only, host_ex)
            return _finish_apply_success(args)
        _emit(
            build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface_by_node=iface_by_node,
                skip_hep=args.no_hostendpoint,
                skip_policy=False,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            ),
            host_ex,
        )
        return _finish_apply_success(args)

    # both
    assert nodes is not None
    pod_labels = parse_pod_label_list(list(args.pod_labels))
    ns, pl = require_pod_layer_args(args.namespace, pod_labels, args.pod_selector_all)
    np_body = build_k8s_network_policy_yaml(
        np_name, ns, pl, allow_v4, allow_v6, args.port
    ).rstrip()

    if args.apply_staged:
        gnp_only = build_combined_manifest(
            policy_name=policy_name,
            allow_nets_v4=allow_v4,
            allow_nets_v6=allow_v6,
            port=args.port,
            hep_prefix=args.hep_prefix,
            nodes=nodes,
            include_external_ip=args.include_external_ip,
            iface_by_node=iface_by_node,
            skip_hep=True,
            skip_policy=False,
            policy_order=args.policy_order,
            policy_selector=sel,
            hep_profiles=hep_prof,
            **gk,
        )
        _emit(gnp_only, host_ex)
        hep_part = build_combined_manifest(
            policy_name=policy_name,
            allow_nets_v4=allow_v4,
            allow_nets_v6=allow_v6,
            port=args.port,
            hep_prefix=args.hep_prefix,
            nodes=nodes,
            include_external_ip=args.include_external_ip,
            iface_by_node=iface_by_node,
            skip_hep=False,
            skip_policy=True,
            policy_order=args.policy_order,
            policy_selector=sel,
            hep_profiles=hep_prof,
            **gk,
        ).rstrip()
        _emit(hep_part, host_ex)
        _emit(np_body, "kubectl")
        return _finish_apply_success(args)

    host_part = build_combined_manifest(
        policy_name=policy_name,
        allow_nets_v4=allow_v4,
        allow_nets_v6=allow_v6,
        port=args.port,
        hep_prefix=args.hep_prefix,
        nodes=nodes,
        include_external_ip=args.include_external_ip,
        iface_by_node=iface_by_node,
        skip_hep=args.no_hostendpoint,
        skip_policy=False,
        policy_order=args.policy_order,
        policy_selector=sel,
        hep_profiles=hep_prof,
        **gk,
    ).rstrip()
    assert host_ex is not None
    _emit(host_part, host_ex)
    _emit(np_body, "kubectl")
    return _finish_apply_success(args)


def cmd_delete(args: argparse.Namespace) -> int:
    _require_explicit_kube_context(args, "delete")
    _require_explicit_traffic_layer(args, "delete")
    ignored_allow = getattr(args, "allow_net_ignored", None) or []
    if ignored_allow:
        logger.warning(
            "delete 已忽略 --allow-net / -a（共 %d 条）：删除只认资源名与本工具 HostEndpoint 标签，"
            "与 apply 的网段列表无关。正确定法见 delete --help。",
            len(ignored_allow),
        )
    if (getattr(args, "interface", None) or "").strip():
        logger.warning(
            "delete 已忽略 --interface：删除主机对象只按 GlobalNetworkPolicy 名与本工具 HostEndpoint 标签/名；"
            "与 apply 时 HostEndpoint 网卡字段无关。"
        )
    ctx = str(args.context)
    logger.warning(
        "[生产影响] delete 将从集群 API 移除策略对象（context=%s）。"
        " 目标 TCP 端口将不再受本工具规则约束，与其它 Calico/K8s 策略叠加后的现网行为可能变化；"
        " 请先核对 --traffic-layer、--policy-name、-n、--k8s-np-name。详见 MAINTAINER_DOC 9.6。",
        ctx,
    )
    base = kubectl_base(args.kubectl, args.context)
    tl = args.traffic_layer
    if tl in ("host", "both"):
        policy_name = args.policy_name or DEFAULT_POLICY_NAME_TEMPLATE.format(port=args.port)
        validate_calico_metadata_name(policy_name, "GlobalNetworkPolicy")
        _require_executor_for_host_traffic(args, "delete")
        executor = str(args.executor)
        if executor == "calicoctl":
            r1 = run_cmd(
                [
                    *_calicoctl_argv(args.calicoctl, args.context),
                    "delete",
                    "globalnetworkpolicy",
                    policy_name,
                    "--skip-not-exists",
                ],
                timeout=_timeout_sec(),
            )
            if r1.returncode != 0:
                logger.warning("calicoctl 删除 GlobalNetworkPolicy: %s", (r1.stderr or r1.stdout or "").strip())
            else:
                logger.info("%s", (r1.stdout or r1.stderr or "").strip() or "GlobalNetworkPolicy 已处理")

            if args.delete_hostendpoints:
                names = _calicoctl_managed_hostendpoint_names(args.calicoctl, args.context)
                if not names:
                    logger.warning("未找到待删除的本工具 HostEndpoint（或枚举失败）。")
                else:
                    r2 = run_cmd(
                        [
                            *_calicoctl_argv(args.calicoctl, args.context),
                            "delete",
                            "hostendpoint",
                            *names,
                            "--skip-not-exists",
                        ],
                        timeout=_timeout_sec(),
                    )
                    if r2.returncode != 0:
                        logger.warning(
                            "calicoctl 删除 HostEndpoint: %s",
                            (r2.stderr or r2.stdout or "").strip(),
                        )
                    else:
                        logger.info("%s", (r2.stdout or r2.stderr or "").strip() or "HostEndpoint 已处理")
        else:
            gnp_res = _kubectl_projectcalico_resource(
                args.kubectl, args.context, "globalnetworkpolic"
            ) or "globalnetworkpolicies.projectcalico.org"
            r1 = run_cmd(
                [*base, "delete", gnp_res, policy_name, "--ignore-not-found"],
                timeout=_timeout_sec(),
            )
            if r1.returncode != 0:
                logger.warning("删除 GlobalNetworkPolicy: %s", (r1.stderr or r1.stdout or "").strip())
            else:
                logger.info("%s", (r1.stdout or r1.stderr or "").strip() or "GlobalNetworkPolicy 已处理")

            if args.delete_hostendpoints:
                hep_res = (
                    _kubectl_projectcalico_resource(args.kubectl, args.context, "hostendpoint")
                    or "hostendpoints.projectcalico.org"
                )
                r2 = _kubectl_delete_label(
                    args.kubectl,
                    args.context,
                    hep_res,
                    f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VAL}",
                )
                if r2.returncode != 0:
                    logger.warning(
                        "按标签删除 HostEndpoint 失败: %s",
                        (r2.stderr or r2.stdout or "").strip(),
                    )
                else:
                    logger.info("%s", (r2.stdout or r2.stderr or "").strip() or "HostEndpoint 已处理")

    if tl in ("pod", "both"):
        np_name = args.k8s_np_name or DEFAULT_K8S_NP_NAME_TEMPLATE.format(port=args.port)
        validate_k8s_rfc1035_name(np_name, "NetworkPolicy")
        ns = require_namespace_for_netpol(args.namespace)
        r3 = run_cmd(
            [*base, "delete", "networkpolicy", np_name, "-n", ns, "--ignore-not-found"],
            timeout=_timeout_sec(),
        )
        if r3.returncode != 0:
            logger.warning("删除 NetworkPolicy: %s", (r3.stderr or r3.stdout or "").strip())
        else:
            logger.info("%s", (r3.stdout or r3.stderr or "").strip() or "NetworkPolicy 已处理")
    return 0


def _traffic_layer_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--traffic-layer",
        choices=("host", "pod", "both"),
        default=None,
        metavar="LAYER",
        help=(
            "plan/validate/apply/delete 必填：host=主机侧 Calico（GNP+默认 HEP）；"
            "pod=某 namespace 标准 NetworkPolicy；both=同时。"
            "【生产】both 一次改两套；须手写 -n 与 --context（见 MAINTAINER_DOC）。"
        ),
    )
    p.add_argument(
        "-n",
        "--namespace",
        default=None,
        help="Pod 层所在命名空间；traffic-layer 含 pod/both 时必填（手写，禁止环境变量默认）。",
    )
    p.add_argument(
        "--pod-label",
        action="append",
        dest="pod_labels",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "NetworkPolicy podSelector.matchLabels（可重复，AND）。"
            "与 --pod-selector-all 同时出现时以后者为准并打警告（见 MAINTAINER_DOC 9.2）。"
        ),
    )
    p.add_argument(
        "--pod-selector-all",
        action="store_true",
        help=(
            "podSelector 匹配该命名空间内全部 Pod。【生产】极高危：-a 过窄可导致整 namespace "
            "入站大面积失败；须变更审批并与 MAINTAINER_DOC 9.2 对照。"
        ),
    )
    p.add_argument(
        "--k8s-np-name",
        default=None,
        help=f"NetworkPolicy metadata.name（默认 {DEFAULT_K8S_NP_NAME_TEMPLATE}）",
    )


def _interface_flag(p: argparse.ArgumentParser) -> None:
    """HostEndpoint 网卡参数（plan / apply / validate 在 host 或 both 时共用）。"""
    p.add_argument(
        "--interface",
        default=None,
        metavar="IFACE",
        help=(
            "HostEndpoint.interfaceName，须人工指定：真实网卡名或 \"*\"（Calico 通配）。"
            f"可与 Node 注解 {NODE_HOST_INTERFACE_ANNOTATION!r} 联用（注解优先，其它节点用本值兜底）。"
            "禁止 auto、禁止省略（除非每个节点均已打上述注解）。traffic-layer 仅 pod 时无需填写。"
        ),
    )


def _validate_flags(p: argparse.ArgumentParser) -> None:
    """validate 子命令：仅注册 cmd_validate 会消费的参数（可靠优先；不照搬 plan 整树）。"""
    p.add_argument(
        "-a",
        "--allow-net",
        action="append",
        dest="allow_net",
        default=[],
        metavar="CIDR",
        help="允许访问该 TCP 端口的源 CIDR（与 plan/apply 语义一致）",
    )
    p.add_argument("--port", type=int, default=9100, help="目标 TCP 端口（与 plan/apply 一致）")
    _interface_flag(p)
    p.add_argument(
        "--include-external-ip",
        action="store_true",
        help="若 apply 使用本开关，validate 须一致，才会核对 ExternalIP 是否在 -a 内",
    )
    p.add_argument(
        "--no-hostendpoint",
        action="store_true",
        help="若 apply 仅下发 GNP，validate 须一致，否则会误要求 HEP 网卡",
    )
    p.add_argument(
        "--hep-profile",
        default=DEFAULT_HEP_PROFILE,
        help=f"与 apply 一致；默认 {DEFAULT_HEP_PROFILE}，用于 calicoctl/kubectl 侧 Profile 存在性校验",
    )
    p.add_argument(
        "--no-default-allow-profile",
        action="store_true",
        help="与 apply 一致；为真时不校验 Profile，并提示 Pass 无 Profile 时的官方语义",
    )
    _traffic_layer_flags(p)


def _plan_apply_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-a",
        "--allow-net",
        action="append",
        dest="allow_net",
        default=[],
        metavar="CIDR",
        help=(
            "允许访问该 TCP 端口的源 CIDR（可重复；v4/v6 拆条）。"
            "【生产】漏配监控/跳板/管控源会导致合法流量被拒。"
        ),
    )
    p.add_argument("--port", type=int, default=9100, help="要限制的 TCP 端口（默认 9100）")
    p.add_argument(
        "--policy-name",
        default=None,
        help=f"主机层 GlobalNetworkPolicy 名称（默认 {DEFAULT_POLICY_NAME_TEMPLATE}；仅 traffic-layer 含 host 时使用）",
    )
    p.add_argument("--hep-prefix", default=DEFAULT_HEP_PREFIX, help="HostEndpoint 名前缀")
    _interface_flag(p)
    p.add_argument(
        "--include-external-ip",
        action="store_true",
        help="HostEndpoint expectedIPs 附加 Node ExternalIP",
    )
    p.add_argument(
        "--no-hostendpoint",
        action="store_true",
        help=(
            "只下发 GNP，不创建本工具 HEP，须配合 --policy-selector。"
            "【生产】selector 过宽可能误伤多节点主机入站。"
        ),
    )
    p.add_argument(
        "--policy-selector",
        default=None,
        help='GNP spec.selector 表达式；创建本工具 HEP 时默认匹配其标签；'
        "--no-hostendpoint 时必填",
    )
    p.add_argument(
        "--policy-order",
        type=float,
        default=500.0,
        help="GlobalNetworkPolicy spec.order（同 tier 内越小越优先）",
    )
    p.add_argument(
        "--hep-profile",
        default=DEFAULT_HEP_PROFILE,
        help="HostEndpoint spec.profiles（默认 projectcalico-default-allow，对齐官方自动 HEP）",
    )
    p.add_argument(
        "--no-default-allow-profile",
        action="store_true",
        help=(
            "不为 HEP 挂载 projectcalico-default-allow。【生产】若无等效主机基线放行，"
            "存在 SSH/kubelet 等不可达风险；仅在有明确兜底时启用。"
        ),
    )
    p.add_argument(
        "--gnp-tier",
        default=None,
        help="GlobalNetworkPolicy spec.tier（省略=默认 tier；值须与集群/企业版 Tier CR 一致）",
    )
    p.add_argument(
        "--gnp-apply-on-forward",
        action="store_true",
        help="GlobalNetworkPolicy spec.applyOnForward（主机作转发路径时见官方 hosts / applyOnForward 说明）",
    )
    p.add_argument(
        "--gnp-performance-hint",
        action="append",
        dest="gnp_performance_hints",
        default=[],
        metavar="HINT",
        help="spec.performanceHints 条目，可重复（如 AssumeNeededOnEveryNode）；未知值原样下发由 Felix 校验",
    )
    _traffic_layer_flags(p)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", "-v", action="store_true", help="调试日志")
    common.add_argument("--no-log-file", action="store_true", help="不写本地滚动日志")
    common.add_argument(
        "--kubectl",
        default=os.environ.get("KUBECTL", "kubectl"),
        help="kubectl 可执行文件（建议写在子命令前）",
    )
    common.add_argument(
        "--context",
        default=None,
        help="kube context 名（须显式填写；禁止用 CALICO_HOST_FW_CONTEXT 注入默认）",
    )
    common.add_argument(
        "--executor",
        choices=("kubectl", "calicoctl"),
        default=None,
        metavar="EXEC",
        help=(
            "traffic-layer 为 host/both 时必填：kubectl=Calico CR 经 Kubernetes API；"
            "calicoctl=经 Calico 数据存储。仅 pod 时不要传。"
        ),
    )
    common.add_argument(
        "--calicoctl",
        default=os.environ.get("CALICOCTL", "calicoctl"),
        help="calicoctl 路径（$CALICOCTL）",
    )

    _sub_doc = (
        "下面是这个子命令自己的参数。总用法看：python …/CalicoPolicyCli.py --help。"
        "背景和实现思路看源码里的 MAINTAINER_DOC。"
    )

    p = argparse.ArgumentParser(
        parents=[common],
        description=(
            "限制谁能连某个 TCP 端口：打在节点上的用 Calico（GNP+HEP），打在普通 Pod 上的用标准 NetworkPolicy。"
            "\n\n下面有一小段用法摘要；搞懂为啥这么写请看源码里的 MAINTAINER_DOC。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_build_help_epilog(),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "preflight",
        help="预检：kubectl / Calico API / calico-node（不修改集群）",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    com_plan = sub.add_parser(
        "plan",
        help="输出合并 YAML，不执行 apply",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _plan_apply_flags(com_plan)

    com_val = sub.add_parser(
        "validate",
        help="校验 allow-net；host 层核对 Node IP；pod 层统计 Pod 数（仅含校验所需参数，见 MAINTAINER_DOC 9.4）",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _validate_flags(com_val)

    com_apply = sub.add_parser(
        "apply",
        help="生成并 apply（主机层 Calico / Pod 层 NetworkPolicy）",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _plan_apply_flags(com_apply)
    com_apply.add_argument(
        "--confirm",
        action="store_true",
        help=(
            f"授权向集群写入/更新策略对象；缺少则 apply 不写 API。"
            f" 也可用 {CONFIRM_ENV}=1（【生产】用毕请 unset，勿长期 export）。"
        ),
    )
    com_apply.add_argument(
        "--backup",
        action="store_true",
        help=(
            f"应用前备份：按 traffic-layer 仅备份将变更的项——"
            f"主机层：--executor calicoctl 时用 calicoctl get（HEP 为 JSON 清单）；"
            f"否则 kubectl get projectcalico 资源；"
            f"Pod 层备份同名 NetworkPolicy（目录默认 {LOG_DIR}/calico_host_fw_backup/<UTC>）。"
            f"【生产】备份失败不中止 apply，不可替代变更评审与 GitOps。"
        ),
    )
    com_apply.add_argument(
        "--backup-dir",
        default=None,
        help="指定备份目录（与 --backup 连用）",
    )
    com_apply.add_argument(
        "--apply-staged",
        action="store_true",
        help=(
            "主机相关时分次下发：先 GNP 再 HEP（同一 --executor）；"
            "traffic-layer=both 时为三步——GNP → HEP → NetPol（NetPol 固定 kubectl，与主机层分离）。"
            "【生产】仍有中间态窗口，参见 MAINTAINER_DOC 第 8 节。"
        ),
    )
    com_apply.add_argument(
        "--dry-run",
        choices=("none", "client", "server"),
        default="none",
        help=(
            "kubectl apply 专用。none=正常下发（真变更）；client=仅客户端 dry-run、不落库；"
            "server=发到 apiserver 校验但不落库（推荐正式前预演）。calicoctl 路径仅支持 none。"
        ),
    )
    com_apply.add_argument(
        "--post-verify",
        action="store_true",
        help=(
            "apply 成功后在本进程内直接运行与 validate 子命令同源的校验逻辑（只读），共用已解析参数；"
            "退出码含 validate 结果。日志中的可手动 validate 行仅含 validate 实际接受的开关。"
        ),
    )

    com_delete = sub.add_parser(
        "delete",
        help="删除本工具创建的主机层 GNP/HEP 及/或命名空间内 NetworkPolicy",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    com_delete.add_argument(
        "--confirm",
        action="store_true",
        help="与 apply 参数习惯兼容；delete 无额外鉴权，传不传结果相同。",
    )
    com_delete.add_argument(
        "-a",
        "--allow-net",
        action="append",
        dest="allow_net_ignored",
        default=[],
        metavar="CIDR",
        help="delete 不使用此参数；若误从 apply 粘贴会忽略并警告。",
    )
    com_delete.add_argument("--port", type=int, default=9100, help="与创建时一致，用于默认资源名")
    com_delete.add_argument(
        "--policy-name",
        default=None,
        help="要删除的 GlobalNetworkPolicy 名称（traffic-layer 含 host 时；默认随 --port）",
    )
    com_delete.add_argument(
        "--delete-hostendpoints",
        action="store_true",
        help=(
            f"traffic-layer 含 host 时：删除本工具 HostEndpoint（{MANAGED_LABEL_KEY}={MANAGED_LABEL_VAL}）。"
            "etcd 模式由 calicoctl 按名批量删除；KDD 时等价 kubectl delete ... -l。"
            "【生产】不指定则 HEP 残留，可能与其它 GNP 继续交互；指定前确认无其它依赖方。"
        ),
    )
    _traffic_layer_flags(com_delete)
    _interface_flag(com_delete)

    sub.add_parser(
        "nodes",
        help="列出节点 InternalIP/ExternalIP",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose, no_file=args.no_log_file)
    try:
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "apply":
            return cmd_apply(args)
        if args.command == "delete":
            return cmd_delete(args)
        if args.command == "nodes":
            return cmd_nodes(args)
    except CalicoHostFwError as e:
        logger.error("%s", e)
        return 1
    logger.error("未知子命令: %s", args.command)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

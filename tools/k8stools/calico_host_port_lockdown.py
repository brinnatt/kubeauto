#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按「流量落点」收敛入站 TCP：主机接口（hostNetwork / 节点监听）与 Pod 网卡（标准 NetworkPolicy）。

**host**：Calico HostEndpoint + GlobalNetworkPolicy（宿主机命名空间 / hostNetwork 暴露端口，K8s NetworkPolicy 够不着）。

**pod**：`networking.k8s.io/v1` NetworkPolicy（仅作用于 **未使用 hostNetwork**、且被 podSelector 选中的 Pod；
依赖 CNI 对 NetworkPolicy 的支持；Calico 等常见插件会执行）。

**both**：同一 allow-net / 端口下同时生成上述两类清单（例如 hostNetwork 抓节点 IP + 普通 Pod metrics 双路径防护）。

【与 Calico 文档对齐】（Open Source 以 docs.tigera.io/calico/latest 为准，当前主线为 Calico 3.31.x；
生产集群请运行受支持的 Calico 版本并参阅官方 Release / Component 版本说明。）

- HostEndpoint 参考：https://docs.tigera.io/calico/latest/reference/resources/hostendpoint
- GlobalNetworkPolicy 参考：https://docs.tigera.io/calico/latest/reference/resources/globalnetworkpolicy
- 保护 Kubernetes 节点：https://docs.tigera.io/calico/latest/network-policy/hosts/kubernetes-nodes

【关键生产语义】
1. GNP 的匹配字段为 **spec.selector**（按 Endpoint 标签选择），Calico 3.31 API **无** nodeSelector 字段；
   错误使用 nodeSelector 可能被 API 丢弃，导致 selector 退化为 all()，**策略可能套到所有 Pod**。
2. 官方说明：wildcard HostEndpoint 在未附加策略时默认拒绝（除 failsafe）；自动创建的 HEP 会带
   **projectcalico-default-allow** Profile。本工具创建的 HEP **默认同样挂载该 Profile**，
   仅额外收紧目标 TCP 端口，避免只写端口级规则却误伤 SSH/kubelet 等其它入站。
3. apply 前请用「preflight → plan →（可选 server dry-run）→ apply + confirm」；生产务必包含 Prometheus 源、跳板、管控网段。

【与官方资源模型对齐（本工具范围说明）】
- **GlobalNetworkPolicy**（主机层）：生成 **spec.order、spec.selector、spec.types: [Ingress]、ingress 规则列表**；
  规则动作为 **Allow/Deny**，按 **protocol TCP、source.nets、destination.ports、ipVersion** 匹配（IPv4/IPv6 分条，符合 EntityRule 不混用 v4/v6 的要求）。
  可选：**spec.tier**（`--gnp-tier`，省略则用集群默认 tier）、**spec.applyOnForward**（`--gnp-apply-on-forward`，适用于官方所述转发路径策略）、
  **spec.performanceHints**（`--gnp-performance-hint`，可重复，如 AssumeNeededOnEveryNode）。
  **未在 CLI 暴露**：**doNotTrack**、**preDNAT**（与 applyOnForward 强相关，误用易导致连接跟踪/DNAT 顺序问题；需按
  「Policy for hosts」自行编写 YAML）、HTTP 应用层策略、基于 Service/SA 的复杂 selector 等——应用 calicoctl/Helm/GitOps 扩展。
- **HostEndpoint**：**spec.node、interfaceName、expectedIPs、profiles**；不生成 **spec.ports**（命名端口），因策略中直接使用数字端口。
- **Pod 层**：生成 **networking.k8s.io/v1 NetworkPolicy**（可移植），**不**生成 namespaced 的 **projectcalico.org/v3 NetworkPolicy**（避免与 Calico CR 模型双轨混淆；高级 Calico 特性请手写）。

标准 NetworkPolicy 语义见：https://kubernetes.io/docs/concepts/services-networking/network-policies/

**用户帮助**：运行 `python calico_host_port_lockdown.py --help` 会输出与 `tools/netcheck.py`
同级详度的 epilog（实现框架、子命令、参数分组、环境变量、退出码、可复制示例）；
子命令 `plan -h` / `apply -h` 等会列出该子命令专有 flag，完整总表以根 `help` 为准。

Python 3.8+，仅标准库。
"""

from __future__ import annotations

__version__ = "2.2.0"

# 文档与版本指引（请在变更 Calico 行为时同步核对）
CALICO_DOCS_LATEST = "https://docs.tigera.io/calico/latest/"
CALICO_COMPONENT_VERSIONS = "https://docs.tigera.io/calico/latest/reference/component-versions"

import argparse
import datetime as _dt
import ipaddress
import json
import logging
import os
import re
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
K8S_NP_LABEL_KEY = "kubeauto.calico/traffic-lockdown"
K8S_NP_LABEL_VAL = "pod"

MIN_PORT = 1
MAX_PORT = 65535

LOG_DIR = os.environ.get("CALICO_HOST_FW_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_BASENAME = "calico_host_port_lockdown.log"
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

CONFIRM_ENV = "CALICO_HOST_FW_CONFIRM"

# 根命令 --help 的 epilog（风格对齐 tools/netcheck.py：读帮助即可操作、理解框架）
HELP_EPILOG = """
================================================================================
一、本脚本做什么（实现框架 = 读帮助即可运维）
================================================================================
  为指定 TCP 端口生成「仅允许若干源 CIDR、其余来源访问该端口拒绝」的入站策略。
  hostNetwork/节点监听 与 普通 Pod 网卡 在 Linux 与 K8s 里走不同路径 → 用 --traffic-layer
  选用 Calico 主机资源 或 标准 NetworkPolicy（或 both）。

  数据流（与代码一致）：
    1) 只读 kubectl：get nodes / get pods（用于生成 expectedIPs 或 validate）。
    2) 内存拼接 YAML：projectcalico.org/v3（GNP、HEP）+ 可选 networking.k8s.io/v1（NetPol）。
    3) apply：kubectl apply -f - 或 calicoctl apply -f -；真写需 --confirm（或环境变量）。

  原理（与 Tigera 官方语义一致，便于排障）：
    - 主机层：包到「主机端点」→ HostEndpoint + GlobalNetworkPolicy；GNP 必须用 spec.selector
      命中 HEP，禁止误用已无对应 API 的 nodeSelector 以免策略套到全部 Pod。
      ingress 内先 Allow 名单源网段 + 目标端口，再 Deny 同端口其他源；IPv4/IPv6 分条 +
      ipVersion（EntityRule 不混地址族）。
    - Pod 层：包到 Pod 接口 → namespaced NetworkPolicy；podSelector + ipBlock + ports。
    - HEP 默认 profiles: projectcalico-default-allow，对齐「自动 HEP」基线，再叠端口收紧。

================================================================================
二、子命令一览（惯例：全局选项在子命令前，如 %(prog)s --context prod plan ...）
================================================================================
  preflight   只读预检 cluster-info、calico-node、projectcalico / networkpolicies 资源名等。
  nodes       只读列出 Node InternalIP、ExternalIP（填 -a 网段时对照用）。
  validate    只读：校验 CIDR；host/both 时对照 Node IP 是否在 allow-net；pod/both 时在 -n
              下统计匹配 --pod-label 的 Pod 数。
  plan        输出完整 YAML 到 stdout（--- 分隔多文档）；不写 etcd（除只读 API）。
  apply       生成并下发；--dry-run=server 不要 --confirm；真写要 --confirm。
  delete      按 traffic-layer 删 GNP /（可选）HEP / NetPol；名称须与创建时一致。

================================================================================
三、--traffic-layer
================================================================================
  host — 仅 Calico GNP +（默认）本工具创建的 HEP
  pod  — 仅某 namespace 的 kubernetes.io NetworkPolicy
  both — 同一 -a / --port 同时生成 host + pod 两类对象（一次 apply 多文档）

================================================================================
四、--executor（kubectl / calicoctl / auto）
================================================================================
  auto：能发现 projectcalico HostEndpoint 资源则用 kubectl，否则尝试 calicoctl。
  traffic-layer 含 pod/both 时清单内有 NetworkPolicy，仅 kubectl；calicoctl 无 --dry-run。

================================================================================
五、参数分组速查（详细语义见各 flag 的 help 文本）
================================================================================
  共有        -a/--allow-net  -n/--namespace  --port  --traffic-layer  --pod-label
              --pod-selector-all  --k8s-np-name
  主机层      --policy-name  --hep-prefix  --interface  --include-external-ip
              --no-hostendpoint  --policy-selector  --policy-order  --hep-profile
              --no-default-allow-profile  --gnp-tier  --gnp-apply-on-forward
              --gnp-performance-hint（可重复）
  apply 专有  --confirm  --backup  --backup-dir  --apply-staged
              --dry-run=none|client|server
  delete 专有 --delete-hostendpoints（仅 host/both 且需删本工具 HEP 时）
  全局        --kubectl  --context  --executor  --calicoctl  -v  --no-log-file

================================================================================
六、环境变量（与命令行冲突时以命令行为准）
================================================================================
  KUBECTL, CALICOCTL
  CALICO_HOST_FW_CONTEXT, CALICO_HOST_FW_TRAFFIC_LAYER, CALICO_HOST_FW_NAMESPACE
  CALICO_HOST_FW_LOG_DIR, CALICO_HOST_FW_KUBECTL_TIMEOUT, CALICO_HOST_FW_CONFIRM

  Calico 文档：%(calico_docs)s
  组件版本：%(calico_ver)s
  K8s NetworkPolicy：https://kubernetes.io/docs/concepts/services-networking/network-policies/

================================================================================
七、退出码：0 成功  1 业务/命令错误  2 未知子命令
================================================================================

================================================================================
八、示例（复制改 CIDR / 命名空间 / context）
================================================================================
  %(prog)s --context prod preflight
  %(prog)s --context prod nodes
  %(prog)s --context prod validate -a 172.20.0.0/16 -a 192.168.125.0/24
  %(prog)s --context prod plan -a 172.20.0.0/16 -a 192.168.125.0/24 --port 9100
  %(prog)s --context prod apply --dry-run=server -a 172.20.0.0/16 -a 192.168.125.0/24 --port 9100
  %(prog)s --context prod apply --confirm --backup --apply-staged \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 --port 9100

  %(prog)s plan --no-hostendpoint --policy-selector 'has(your-label)' -a 10.0.0.0/8 --port 9100

  %(prog)s plan --traffic-layer pod -n monitor \\
      --pod-label app.kubernetes.io/name=prometheus-node-exporter \\
      -a 172.20.0.0/16 --port 9100
  %(prog)s apply --confirm -n monitor --traffic-layer pod \\
      --pod-label app.kubernetes.io/name=prometheus-node-exporter \\
      -a 172.20.0.0/16 --port 9100

  %(prog)s plan --traffic-layer both -n monitor --pod-label app=prometheus \\
      -a 172.20.0.0/16 -a 192.168.125.0/24 --port 9100

  %(prog)s delete --traffic-layer host --delete-hostendpoints --context prod --port 9100
  %(prog)s delete --traffic-layer pod -n monitor --context prod --port 9100
  %(prog)s delete --traffic-layer both -n monitor --delete-hostendpoints --context prod --port 9100

子命令详细 flag 请执行：%(prog)s plan -h   %(prog)s apply -h   %(prog)s validate -h 等
"""


def _build_help_epilog() -> str:
    """argparse 只认 %(prog)s；文档 URL 用命名占位符避免与 %% 格式化冲突。"""
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
    iface: str,
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
                parts.append(
                    build_host_endpoint_yaml(
                        hep_name, n, want, iface, hep_profiles
                    ).rstrip()
                )
            except CalicoHostFwError as e:
                logger.warning("%s", e)
    return "\n---\n".join(parts) + "\n"


def detect_executor(kubectl: str, context: Optional[str], prefer: str) -> str:
    if prefer != "auto":
        return prefer
    base = kubectl_base(kubectl, context)
    r = run_cmd([*base, "api-resources", "-o", "name"], timeout=_timeout_sec())
    out = (r.stdout or "").lower()
    if r.returncode == 0 and "hostendpoints" in out and "projectcalico" in out:
        return "kubectl"
    if shutil.which("calicoctl"):
        logger.info("未在 kubectl api-resources 中发现 projectcalico HostEndpoint，改用 calicoctl")
        return "calicoctl"
    logger.warning("将尝试 kubectl；若失败请安装 Calico API Server 或改用 --executor calicoctl")
    return "kubectl"


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
        argv = [calicoctl, "apply", "-f", "-"]
        if context:
            argv.extend(["--context", context])
        r = run_cmd(argv, input_text=manifest, timeout=_timeout_sec() * 3)
        if r.returncode != 0:
            raise CalicoHostFwError(
                "calicoctl apply 失败:\n" + (r.stderr or r.stdout or "").strip()
            )
        logger.info("%s", (r.stdout or "").strip() or "(calicoctl apply 成功，无输出)")
        return

    raise CalicoHostFwError(f"未知 executor: {executor!r}")


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
    k8s_np_name: Optional[str] = None,
    k8s_namespace: Optional[str] = None,
) -> None:
    os.makedirs(backup_dir, exist_ok=True)
    base = kubectl_base(kubectl, context)

    if backup_host_resources:
        gnp_res = _kubectl_projectcalico_resource(kubectl, context, "globalnetworkpolic") or "globalnetworkpolicies.projectcalico.org"
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
        hep_res = _kubectl_projectcalico_resource(kubectl, context, "hostendpoint") or "hostendpoints.projectcalico.org"
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
                logger.warning("未找到 k8s-app=calico-node 的 Pod；若 CNI 非 Calico 请勿使用本工具")
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

    logger.info(
        "预检完成。下一步建议: plan 生成 YAML；apply 时使用 --confirm "
        "或设置环境变量 %s=1。",
        CONFIRM_ENV,
    )
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    allow_v4, allow_v6 = validate_cidrs(list(args.allow_net))
    tl = args.traffic_layer
    parts: List[str] = []
    if tl in ("host", "both"):
        nodes = kube_get_nodes_json(args.kubectl, args.context)
        policy_name = args.policy_name or DEFAULT_POLICY_NAME_TEMPLATE.format(port=args.port)
        validate_calico_metadata_name(policy_name, "GlobalNetworkPolicy")
        sel = effective_policy_selector(args.no_hostendpoint, args.policy_selector)
        hep_prof = [] if args.no_default_allow_profile else [args.hep_profile]
        gk = _manifest_gnp_kwargs(args)
        parts.append(
            build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface=args.interface,
                skip_hep=args.no_hostendpoint,
                skip_policy=False,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            ).rstrip()
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
    allow_v4, allow_v6 = validate_cidrs(list(args.allow_net))
    tl = args.traffic_layer
    warnings = 0

    if tl in ("host", "both"):
        nets_v4 = [ipaddress.ip_network(c, strict=False) for c in allow_v4]
        nets_v6 = [ipaddress.ip_network(c, strict=False) for c in allow_v6]
        nodes = kube_get_nodes_json(args.kubectl, args.context)
        for node in nodes:
            name, ips = node_internal_ips(node)
            for ip_s in ips:
                try:
                    addr = ipaddress.ip_address(ip_s)
                except ValueError:
                    continue
                pool = nets_v4 if addr.version == 4 else nets_v6
                if not pool:
                    continue
                if not any(addr in n for n in pool):
                    logger.warning(
                        "节点 %s 的 InternalIP %s 不在任一 --allow-net 内；"
                        "若 Prometheus 从 Pod 网段访问抓取地址，请确认已包含 Pod CIDR / 相关网段",
                        name,
                        ip_s,
                    )
                    warnings += 1

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
        if n_pods == 0:
            logger.warning(
                "validate: 当前选择器下无 Pod，应用 NetworkPolicy 后不会影响任何工作负载；请检查 --pod-label / 命名空间"
            )
            warnings += 1

    if warnings:
        logger.info("validate: 完成，有 %d 条提示（请结合拓扑核对）", warnings)
    else:
        logger.info("validate: 未触发告警项（仍请人工复核）")
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
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
        return
    if args.confirm:
        return
    if os.environ.get(CONFIRM_ENV, "").strip() in ("1", "true", "yes"):
        return
    raise CalicoHostFwError(
        "生产级保护：真正写集群前请显式添加 --confirm，或设置环境变量 "
        f"{CONFIRM_ENV}=1 （仍建议先 plan + server dry-run）。"
    )


def cmd_apply(args: argparse.Namespace) -> int:
    allow_v4, allow_v6 = validate_cidrs(list(args.allow_net))
    if not (MIN_PORT <= args.port <= MAX_PORT):
        raise CalicoHostFwError(f"端口范围非法: {args.port}")
    _require_confirm(args)

    tl = args.traffic_layer
    ex = detect_executor(args.kubectl, args.context, args.executor)
    if tl in ("pod", "both") and ex == "calicoctl":
        logger.info("清单含 Kubernetes NetworkPolicy，改用 kubectl apply")
        ex = "kubectl"

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
            logger.info("traffic-layer=pod 时忽略 --apply-staged")
        _emit(
            build_k8s_network_policy_yaml(
                np_name, ns, pl, allow_v4, allow_v6, args.port
            ),
            "kubectl",
        )
        return 0

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
                iface=args.interface,
                skip_hep=True,
                skip_policy=False,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            )
            _emit(gnp_only, ex)
            hep_only = build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface=args.interface,
                skip_hep=False,
                skip_policy=True,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            )
            _emit(hep_only, ex)
            return 0
        _emit(
            build_combined_manifest(
                policy_name=policy_name,
                allow_nets_v4=allow_v4,
                allow_nets_v6=allow_v6,
                port=args.port,
                hep_prefix=args.hep_prefix,
                nodes=nodes,
                include_external_ip=args.include_external_ip,
                iface=args.interface,
                skip_hep=args.no_hostendpoint,
                skip_policy=False,
                policy_order=args.policy_order,
                policy_selector=sel,
                hep_profiles=hep_prof,
                **gk,
            ),
            ex,
        )
        return 0

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
            iface=args.interface,
            skip_hep=True,
            skip_policy=False,
            policy_order=args.policy_order,
            policy_selector=sel,
            hep_profiles=hep_prof,
            **gk,
        )
        _emit(gnp_only, ex)
        hep_part = build_combined_manifest(
            policy_name=policy_name,
            allow_nets_v4=allow_v4,
            allow_nets_v6=allow_v6,
            port=args.port,
            hep_prefix=args.hep_prefix,
            nodes=nodes,
            include_external_ip=args.include_external_ip,
            iface=args.interface,
            skip_hep=False,
            skip_policy=True,
            policy_order=args.policy_order,
            policy_selector=sel,
            hep_profiles=hep_prof,
            **gk,
        ).rstrip()
        _emit(hep_part + "\n---\n" + np_body + "\n", "kubectl")
        return 0

    host_part = build_combined_manifest(
        policy_name=policy_name,
        allow_nets_v4=allow_v4,
        allow_nets_v6=allow_v6,
        port=args.port,
        hep_prefix=args.hep_prefix,
        nodes=nodes,
        include_external_ip=args.include_external_ip,
        iface=args.interface,
        skip_hep=args.no_hostendpoint,
        skip_policy=False,
        policy_order=args.policy_order,
        policy_selector=sel,
        hep_profiles=hep_prof,
        **gk,
    ).rstrip()
    _emit(host_part + "\n---\n" + np_body + "\n", "kubectl")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    base = kubectl_base(args.kubectl, args.context)
    tl = args.traffic_layer
    if tl in ("host", "both"):
        policy_name = args.policy_name or DEFAULT_POLICY_NAME_TEMPLATE.format(port=args.port)
        validate_calico_metadata_name(policy_name, "GlobalNetworkPolicy")
        gnp_res = _kubectl_projectcalico_resource(args.kubectl, args.context, "globalnetworkpolic") or "globalnetworkpolicies.projectcalico.org"
        r1 = run_cmd(
            [*base, "delete", gnp_res, policy_name, "--ignore-not-found"],
            timeout=_timeout_sec(),
        )
        if r1.returncode != 0:
            logger.warning("删除 GlobalNetworkPolicy: %s", (r1.stderr or r1.stdout or "").strip())
        else:
            logger.info("%s", (r1.stdout or r1.stderr or "").strip() or "GlobalNetworkPolicy 已处理")

        if args.delete_hostendpoints:
            hep_res = _kubectl_projectcalico_resource(args.kubectl, args.context, "hostendpoint") or "hostendpoints.projectcalico.org"
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
        default=(os.environ.get("CALICO_HOST_FW_TRAFFIC_LAYER") or "host").strip() or "host",
        metavar="LAYER",
        help="host=节点/hostNetwork（Calico HEP+GNP）；pod=Kubernetes NetworkPolicy；both=同时下发",
    )
    p.add_argument(
        "-n",
        "--namespace",
        default=os.environ.get("CALICO_HOST_FW_NAMESPACE"),
        help="Pod 层策略所在命名空间（traffic-layer 含 pod 时 plan/apply/validate 必填；delete 删 NP 时必填）",
    )
    p.add_argument(
        "--pod-label",
        action="append",
        dest="pod_labels",
        default=[],
        metavar="KEY=VALUE",
        help="NetworkPolicy podSelector.matchLabels（可重复；与 --pod-selector-all 二选一）",
    )
    p.add_argument(
        "--pod-selector-all",
        action="store_true",
        help="podSelector 为空，匹配该命名空间下全部 Pod（高危，务必确认）",
    )
    p.add_argument(
        "--k8s-np-name",
        default=None,
        help=f"NetworkPolicy metadata.name（默认 {DEFAULT_K8S_NP_NAME_TEMPLATE}）",
    )


def _plan_apply_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-a",
        "--allow-net",
        action="append",
        dest="allow_net",
        default=[],
        metavar="CIDR",
        help="允许访问该端口的源 CIDR（可重复；IPv4/IPv6 可混开，将拆成多条规则）",
    )
    p.add_argument("--port", type=int, default=9100, help="要限制的 TCP 端口（默认 9100）")
    p.add_argument(
        "--policy-name",
        default=None,
        help=f"主机层 GlobalNetworkPolicy 名称（默认 {DEFAULT_POLICY_NAME_TEMPLATE}；仅 traffic-layer 含 host 时使用）",
    )
    p.add_argument("--hep-prefix", default=DEFAULT_HEP_PREFIX, help="HostEndpoint 名前缀")
    p.add_argument(
        "--interface",
        default="*",
        help='HostEndpoint interfaceName，默认 "*" 表示所有宿主机网卡（见官方 HEP 说明）',
    )
    p.add_argument(
        "--include-external-ip",
        action="store_true",
        help="HostEndpoint expectedIPs 附加 Node ExternalIP",
    )
    p.add_argument(
        "--no-hostendpoint",
        action="store_true",
        help="只生成 GlobalNetworkPolicy，不创建本工具的 HostEndpoint（须配合 --policy-selector）",
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
        help="不为 HostEndpoint 挂载 projectcalico-default-allow（仅资深用户：需确认其它策略/Profile 已覆盖主机基线）",
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
        default=os.environ.get("CALICO_HOST_FW_CONTEXT"),
        help="kube context（$CALICO_HOST_FW_CONTEXT）",
    )
    common.add_argument(
        "--executor",
        choices=("auto", "kubectl", "calicoctl"),
        default="auto",
        help="apply 时选用 kubectl 或 calicoctl",
    )
    common.add_argument(
        "--calicoctl",
        default=os.environ.get("CALICOCTL", "calicoctl"),
        help="calicoctl 路径（$CALICOCTL）",
    )

    _sub_doc = (
        "下列为本子命令专有选项。脚本整体协议（主机/Pod 双路径、数据流、子命令表、"
        "参数分组、环境变量、退出码、可复制示例）在用户执行**主命令** "
        "`python …/calico_host_port_lockdown.py --help`（不带子命令）时输出。"
    )

    p = argparse.ArgumentParser(
        parents=[common],
        description=(
            "统一收敛「谁可以从哪些 CIDR 访问哪个 TCP 端口」："
            "主机/hostNetwork 走 Calico HostEndpoint + GlobalNetworkPolicy；"
            "普通 Pod 走标准 networking.k8s.io NetworkPolicy（CNI 需支持）。\n\n"
            "读下方「帮助续篇」即可独立运维并理解实现框架（对齐 tools/netcheck.py 的 epilog 深度）。"
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
        help="校验 allow-net；host 层核对 Node IP；pod 层统计匹配 Pod 数",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    com_val.add_argument(
        "-a",
        "--allow-net",
        action="append",
        dest="allow_net",
        default=[],
        metavar="CIDR",
        help="允许访问该端口的源 CIDR（与 apply 一致）",
    )
    com_val.add_argument("--port", type=int, default=9100, help="与 apply 一致（用于默认资源名提示）")
    _traffic_layer_flags(com_val)

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
        help=f"确认写入生产集群（或设环境变量 {CONFIRM_ENV}=1）",
    )
    com_apply.add_argument(
        "--backup",
        action="store_true",
        help=(
            f"应用前备份：按 traffic-layer 仅备份将变更的项——"
            f"主机层尝试备份同名 GNP 与带工具标签的 HEP；"
            f"Pod 层尝试备份同名 NetworkPolicy（目录默认 {LOG_DIR}/calico_host_fw_backup/<UTC>）"
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
        help="主机层分两次 apply：先仅 GNP 再 HEP；traffic-layer=both 时第二步为 HEP+NetworkPolicy（同一 kubectl apply）",
    )
    com_apply.add_argument(
        "--dry-run",
        choices=("none", "client", "server"),
        default="none",
        help="kubectl 专用：client/server dry-run（calicoctl 路径不可用）",
    )

    com_delete = sub.add_parser(
        "delete",
        help="删除本工具创建的主机层 GNP/HEP 及/或命名空间内 NetworkPolicy",
        description=_sub_doc,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help=f"traffic-layer 含 host 时：删除带 {MANAGED_LABEL_KEY}={MANAGED_LABEL_VAL} 的 HostEndpoint",
    )
    _traffic_layer_flags(com_delete)

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

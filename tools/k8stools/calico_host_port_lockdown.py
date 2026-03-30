#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
限制哪些网段能访问某个 TCP 端口（节点上的端口或普通 Pod 里的端口）。
详细说明在本文件里的 MAINTAINER_DOC 常量（不会出现在 --help 里）。
需要 Python 3.8+，只用标准库。
"""

from __future__ import annotations

__version__ = "2.2.0"

# 改 Calico 相关逻辑时，对照下面链接里的官方说明
CALICO_DOCS_LATEST = "https://docs.tigera.io/calico/latest/"
CALICO_COMPONENT_VERSIONS = "https://docs.tigera.io/calico/latest/reference/component-versions"

# ---------------------------------------------------------------------------
# 给维护者看的说明（不会出现在 --help 里；改脚本前建议先扫一遍）
# ---------------------------------------------------------------------------
MAINTAINER_DOC = """
===============================================================================
calico_host_port_lockdown.py - 维护者说明
===============================================================================

读这个能知道：脚本干啥、为啥分两套策略、生成啥 YAML、代码从哪进从哪改。

-------------------------------------------------------------------------------
1. 干啥用
-------------------------------------------------------------------------------
  某一个 TCP 端口：只允许你写在 -a 里的那些网段来连，别的来源一律挡掉。
  常见例子：9100 上的 node-exporter、hostNetwork 的采集、直接绑在节点 IP 上的服务。

-------------------------------------------------------------------------------
2. 为啥有时候不能只用 K8s 自带的 NetworkPolicy
-------------------------------------------------------------------------------
  平时说的 NetworkPolicy 管的是 Pod 里的网卡那条路。
  流量如果是打到「节点 IP + 端口」，或者是 hostNetwork 在宿主机上听的端口，
  往往走不到你以为的那条 Pod 策略上。
  所以要靠 Calico 的 HostEndpoint + GlobalNetworkPolicy，才能管住「到机器上的」这条入站。

  如果是普通 Pod（不是 hostNetwork），用标准 NetworkPolicy 就行，也方便换 CNI。
  --traffic-layer 就是在选：只管机器 (host)、只管 Pod (pod)、两个都要 (both)。

-------------------------------------------------------------------------------
3. host / pod / both 各是啥
-------------------------------------------------------------------------------
  host:
    会写出 Calico 的 GlobalNetworkPolicy，并默认再写出 HostEndpoint（除非你关掉）。
    策略用 spec.selector 对上本工具给 HEP 打的标签，别给 GNP 写 nodeSelector：
    老字段/写错了可能变成「整集群都套上」，非常危险。

  pod:
    在某个 namespace 里写一条标准 NetworkPolicy（ingress + 网段 + 端口）。
    选哪些 Pod 靠 --pod-label 或 --pod-selector-all。
    hostNetwork 的 Pod 算不算管得到，以 K8s 官方 NetworkPolicy 文档为准。

  both:
    一次打出两段 YAML（中间用 --- 隔开）：主机上一套 Calico，命名空间里一条 NetPol。

-------------------------------------------------------------------------------
4. 主机那套 YAML 里大概有啥
-------------------------------------------------------------------------------
  HostEndpoint:
    节点名、网卡名（常用 *）、expectedIPs（一般从 Node 地址推出来，可加外网 IP）。
    默认带着 projectcalico-default-allow 这个 profile：和 Calico 自动 HEP 一样，
    先别误伤 SSH、kubelet 之类，再在你指定的端口上加收紧规则。

  GlobalNetworkPolicy:
    只要入站 (Ingress)；selector 和上面 HEP 的标签对齐。
    对你设的 TCP 端口：先放行 -a 里的网段，再拒绝别的来源；IPv4 和 IPv6 分成两条写。

-------------------------------------------------------------------------------
5. Pod 那条 NetworkPolicy
-------------------------------------------------------------------------------
  就是标准的 networking.k8s.io/v1，不写 Calico 自己那种 namespaced NetworkPolicy CR。
  真要 Calico 特有字段，自己写 YAML 下发。

-------------------------------------------------------------------------------
6. 代码从哪读
-------------------------------------------------------------------------------
  main() 里 parse_args，然后按子命令进 cmd_preflight / nodes / validate / plan / apply / delete。

  想改生成规则：跟 cmd_plan 和它下面拼 YAML 的函数。
  想改怎么下发：看 cmd_apply（确认、备份、分步 apply、kubectl/calicoctl、dry-run）。
  想改检查逻辑：cmd_validate、cmd_preflight。

  只有 apply 并且加了 --confirm（或设置了 CALICO_HOST_FW_CONFIRM=1）才会真的改集群；
  plan 永远只是打印。

-------------------------------------------------------------------------------
7. kubectl 还是 calicoctl
-------------------------------------------------------------------------------
  YAML 里只要有标准 NetworkPolicy，就必须用 kubectl。
  只有 Calico 那几种 CR 时，两种工具常有集群都能用；auto 会帮你试。
  服务端 dry-run 只有 kubectl 有。

-------------------------------------------------------------------------------
8. --backup 和 --apply-staged
-------------------------------------------------------------------------------
  --backup：改动前尽量把要被覆盖的 GNP、带本工具标签的 HEP、同名 NetPol 先拉下来存盘（具体目录看代码）。
  --apply-staged：可以先只 apply GNP，再 apply HEP（Pod 层若有，跟第二步一起），减少中间空窗期。

-------------------------------------------------------------------------------
9. 参数和环境变量（对照着用；可重复的选项多写几次）
-------------------------------------------------------------------------------

【9.1 全局选项】写在子命令前面，例如：脚本 --context 生产 plan ...

  --verbose / -v
      控制台多打调试信息。
  --no-log-file
      不写滚动日志文件；默认会写到目录 CALICO_HOST_FW_LOG_DIR（见 9.8）下的
      calico_host_port_lockdown.log。
  --kubectl
      kubectl 命令路径。默认先读环境变量 KUBECTL，没有再当命令名叫 kubectl。
  --context
      用 kubeconfig 里哪一个 context。默认读 CALICO_HOST_FW_CONTEXT。
  --executor auto | kubectl | calicoctl
      真正 apply 时用谁把 YAML 塞给集群。auto 会按集群情况试探。
      只要清单里带了标准 NetworkPolicy，实际只能走 kubectl。
  --calicoctl
      calicoctl 路径。默认读 CALICOCTL 环境变量，没有则当命令 calicoctl。

【9.2 流量层、命名空间、Pod 怎么选、NetPol 叫啥名】
（出现在 plan / apply / validate / delete 里，preflight、nodes 没有）

  --traffic-layer host | pod | both
      host：只做节点侧 Calico（GNP + 默认还带 HEP）。
      pod：只在某个 namespace 写标准 NetworkPolicy。
      both：两套一起出。默认用环境变量 CALICO_HOST_FW_TRAFFIC_LAYER，没有再默认 host。
  -n / --namespace
      Pod 那条策略写在哪个命名空间里。traffic-layer 带 pod 或 both 时，plan/apply/validate
      必须给；delete 要删 NetPol 时也必须给。默认可读 CALICO_HOST_FW_NAMESPACE。
  --pod-label KEY=VALUE
      可以写很多遍，每一对变成 NetworkPolicy 里 podSelector.matchLabels 里的一项（一起生效）。
      和 --pod-selector-all 不要同时用（逻辑互斥）。
  --pod-selector-all
      不配具体 label，相当于选中该命名空间下所有 Pod，风险大，确认清楚再用。
  --k8s-np-name
      生成的 NetworkPolicy 在 K8s 里的资源名。不写就用「kubeauto-restrict-pod-tcp-」加端口，
      例如端口 9100 就是 kubeauto-restrict-pod-tcp-9100。

【9.3 plan 和 apply 共用】（validate、delete 都不带这一组）

  -a / --allow-net CIDR
      允许访问目标端口的来源网段。可以写很多遍，每一遍一段 CIDR（IPv4、IPv6 可以混着写，
      脚本会拆成多条 Calico/K8s 规则）。
  --port
      要限制的 TCP 端口号。默认 9100。会影响默认的 GNP 名、NetPol 名（见 9.9）。
  --policy-name
      主机层 GlobalNetworkPolicy 的名字。不写默认「kubeauto-restrict-host-tcp-」加端口。
      只有 traffic-layer 含 host 时才会去创建/删除这条 GNP。
  --hep-prefix
      每个节点上 HostEndpoint 名字的前半截。最终名字是「前缀 + "-" + 节点名（会做合法化）」。
      默认 kubeauto-hep。
  --interface
      写进 HostEndpoint 的 interfaceName。默认 *，表示 Calico 文档里那种匹配所有宿主机接口的写法。
  --include-external-ip
      打开后，把节点的 ExternalIP 也塞进 HostEndpoint 的 expectedIPs（否则主要靠 InternalIP 等）。
  --no-hostendpoint
      只生成 GNP，不创建本脚本通常会顺带创建的 HEP。这时你必须用 --policy-selector 写清楚
      GNP 要套在哪些已有 HostEndpoint 上。
  --policy-selector
      Calico 的 selector 表达式，进 GNP 的 spec.selector。若本脚本自己建 HEP，默认会去对齐
      带 kubeauto.calico/host-port-lockdown=true 标签的那批端点；你关掉 HEP 时必须自己填这条。
  --policy-order
      GNP 的 spec.order，数字越小在同 tier 里越早评估。默认 500。
  --hep-profile
      HostEndpoint 的 profiles 里带的 profile 名，默认 projectcalico-default-allow，
      和 Calico 自动 HEP 行为对齐，避免误伤 SSH 等。
  --no-default-allow-profile
      不给 HEP 挂上面的默认放行 profile，只有你有把握其它策略已经兜住主机流量时才开。
  --gnp-tier
      GNP 的 spec.tier；不配就用集群默认 tier，具体以你集群里 Tier 定义为准。
  --gnp-apply-on-forward
      打开 GNP 的 spec.applyOnForward，给节点做转发路径时用，细节看 Calico 官方「hosts」文档。
  --gnp-performance-hint
      可以写多遍，每一遍变成 spec.performanceHints 里的一条，例如 AssumeNeededOnEveryNode。
      不认得的值也会原样下发，由集群里的 Felix 决定收不收。

【9.4 validate 实际认的参数】
  只有 9.1 全局 + 9.2（traffic-layer、namespace、pod 选择、NetPol 名）+ -a/--allow-net + --port。
  不会用到 9.3 里那些 HEP/GNP 细调（那些只出现在 plan、apply）。
  做的事：校验 CIDR；host/both 时扫一遍节点 IP 是不是落在 -a 里；pod/both 时在命名空间里数
  匹配你选中的 Pod 有几个，方便发现「一条策略根本套不到 Pod」这种乌龙。

【9.5 只有 apply 多出来的】

  --confirm
      不加的话 apply 只会在内存里算完 YAML，不会真的 kubectl apply（防手滑）。
      或者设环境变量 CALICO_HOST_FW_CONFIRM 为 1、true、yes。

  --backup
      在覆盖前，尝试把即将动到的同名 GNP、带本工具标签的 HEP、同名 NetworkPolicy 先 kubectl get
      出来存盘；目录规则见 9.8。
  --backup-dir
      备份落盘目录；不配就用默认备份根目录（仍在 CALICO_HOST_FW_LOG_DIR 底下那一套）。
  --apply-staged
      主机相关时分两次下发：第一次只 apply GNP，第二次再 apply HEP（若还有 Pod 层，第二次里
      会和 NetPol 一起打）。缓解「HEP 挂上瞬间规则还没按你想的来」的短窗口。
  --dry-run none | client | server
      走 kubectl 时的 dry-run；none 表示正常；server 让 apiserver 验对象但不落库（不要配 confirm）。
      calicoctl 路径下没有等价 server dry-run。

【9.6 只有 delete 多出来的】

  --port
      和创建时一致，用来拼默认的 GNP / NetPol 名字（若你没自定义名字）。
  --policy-name
      要删的 GlobalNetworkPolicy 叫什么；不配就用默认命名规则。
  --delete-hostendpoints
      若有 host/both：为 true 时按标签删掉「本脚本打过 kubeauto.calico/host-port-lockdown=true」
      的所有 HostEndpoint；不删留着的话节点上还挂着端点对象。

  delete 还会带上 9.2 整组（含 --k8s-np-name、-n），用来找要删的 NetworkPolicy。
  注意：delete 不认 -a/--allow-net，也不认 9.3 里任何「生成规则」参数，只按名字/标签删对象。

【9.7 每个子命令到底认哪些参数（速查）】

  preflight、nodes
      只有 9.1 全局选项。

  validate
      9.1 + 9.2 + -a + --port（见 9.4）。

  plan
      9.1 + 9.2 + 9.3 全部。

  apply
      9.1 + 9.2 + 9.3 + 9.5。

  delete
      9.1 + 9.2 + 9.6（没有放行网段 -a，也没有 9.3）。

【9.8 环境变量一览（和命令行冲突时以命令行为准）】

  KUBECTL               默认 kubectl 可执行文件路径
  CALICOCTL             默认 calicoctl 可执行文件路径
  CALICO_HOST_FW_CONTEXT          默认 --context
  CALICO_HOST_FW_TRAFFIC_LAYER    默认 --traffic-layer（host/pod/both）
  CALICO_HOST_FW_NAMESPACE        默认 -n/--namespace
  CALICO_HOST_FW_LOG_DIR          日志目录；默认当前工作目录下的 logs/
  CALICO_HOST_FW_KUBECTL_TIMEOUT  kubectl 子进程超时秒数，默认 120（大 manifest 时 apply 会用更长时间）
  CALICO_HOST_FW_CONFIRM          等价于全程带上 --confirm（apply 真写集群）

【9.9 默认会起什么资源名、打什么标签】

  GlobalNetworkPolicy 默认名：kubeauto-restrict-host-tcp-<port>
  NetworkPolicy 默认名：kubeauto-restrict-pod-tcp-<port>
  HostEndpoint 默认名：<hep-prefix>-<节点名>
  本脚本创建的 HostEndpoint 会带标签：kubeauto.calico/host-port-lockdown=true；
  GNP 默认用这个标签去 selector（除非你换成自己的 HEP + policy-selector）。
  本脚本创建的 NetworkPolicy 会带标签：kubeauto.calico/traffic-lockdown=pod（方便认了删）。

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
11. 命令行里不做的 Calico 高级项
-------------------------------------------------------------------------------
  像 doNotTrack、preDNAT 这种会牵动连接跟踪和 DNAT 顺序，弄不好就断网，所以 CLI 不代你写；
  真要上，按官方「主机策略」自己手写 YAML。
===============================================================================
"""

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

# 根命令 --help 结尾的短文；想搞懂设计看源码里的 MAINTAINER_DOC
HELP_EPILOG = """
干啥: 某个 TCP 端口只允许 -a 里的网段访问；管节点上的端口用 Calico（GNP+HEP），管 Pod 用标准 NetPol。

--traffic-layer  host | pod | both
  host  写 Calico 全局策略 + 默认再写 HostEndpoint
  pod   在某个 namespace 写一条标准 NetworkPolicy
  both  上面两套一次打出来（多段 YAML）

子命令（注意：--context 这类要写在子命令前面，例如 %(prog)s --context 生产 plan ...）
  preflight / validate / nodes  只读，不改集群
  plan      只在屏幕上打印 YAML
  apply     真往集群里写要加 --confirm；可加 --backup、--apply-staged、--dry-run
  delete    按层删 GNP、HEP、NetPol（名字要和当初创建时一致）

--executor  auto | kubectl | calicoctl
  只要清单里带了标准 NetPol，就只能 kubectl；服务端 dry-run 也只有 kubectl 能用。

环境变量和命令行重复时，以命令行为准。具体名字看每个参数的 -h。

链接: Calico %(calico_docs)s  组件版本 %(calico_ver)s
退出: 0 正常  1 出错  2 子命令不认识

建议顺序: preflight -> 看看 nodes/validate -> plan -> apply --dry-run=server -> apply --confirm [--backup]

例子:
  %(prog)s preflight
  %(prog)s plan -a 10.0.0.0/8 --port 9100
  %(prog)s apply --dry-run=server -a 10.0.0.0/8 --port 9100
  %(prog)s apply --confirm --backup -a 172.20.0.0/16 -a 192.168.0.0/16 --port 9100
  %(prog)s apply --confirm -n mon --traffic-layer pod --pod-label app=x -a 10.0.0.0/8 --port 9100
  %(prog)s delete --traffic-layer host --delete-hostendpoints --port 9100

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
        "下面是这个子命令自己的参数。总用法看：python …/calico_host_port_lockdown.py --help。"
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

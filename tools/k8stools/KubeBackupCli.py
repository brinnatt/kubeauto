#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
k8s_backup.py - 企业级 Kubernetes 配置备份和恢复工具

支持功能：
- 备份 Kubernetes 集群资源（Deployments、Services、ConfigMaps、Secrets 等）
- 恢复备份到目标集群（server-side apply、单文件多文档清单、依赖顺序串行）
- 支持命名空间映射、镜像映射、环境变量映射（统一 KEY=值，多项用逗号或空格分隔）
- 恢复时可选：--merge-patch 与 kubectl patch -p 同形状（YAML/JSON 文件或「{」开头内联 JSON），须配 --merge-patch-kind；内联仅适合小片段（脚本有 UTF-8 大小与嵌套深度上限），复杂结构请用文件以便评审与稳定落地（字典递归合并，列表整段覆盖）
- 恢复时可选：Downward API（KEY=@k8s:fieldPath）；另支持仅对 env.value 内联字符串按命名空间映射做受控替换（--env-namespace-substitute，与 valueFrom 区分见 -h）
- 典型：restore --namespace-mapping 旧命名空间=新命名空间 时，清单里 metadata.namespace 与符合条件的 env.value 会随策略改写；具体顺序与示例见 -h 专节
- 备份侧可选 --include-names：按 metadata.name 精确过滤（与对象名一致；省略则不过滤，行为与旧版相同）
- 备份侧拒绝缺少 apiVersion/kind 的对象；元数据 JSON 损坏时降级而非崩溃
- 自动处理资源依赖关系和恢复顺序（含 HPA、PDB、NetworkPolicy 等扩展优先级）

设计与约束对齐 Kubernetes 官方文档（声明式配置、SSA、Service、NetworkPolicy 等）。

Python 3.7+ 兼容
"""

import argparse
import logging
import os
import re
import sys
import tarfile
import time
import base64
import json
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import ResourceNotFoundError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
    retry_if_exception,
)

# -------------------------
# Configuration / Constants
# -------------------------
DEFAULT_RESOURCES = [
    "deployments",
    "daemonsets",
    "statefulsets",
    "jobs",
    "cronjobs",
    "persistentvolumes",
    "persistentvolumeclaims",
    "services",
    "configmaps",
    "secrets",
    "ingresses",
    "roles",
    "rolebindings",
    "clusterroles",
    "clusterrolebindings",
]

CLUSTER_SCOPED_RESOURCES = {
    "persistentvolumes",
    "storageclasses",
    "customresourcedefinitions",
    "clusterroles",
    "clusterrolebindings",
}

RESOURCE_API_MAPPING = {
    "deployments": [("apps/v1", "Deployment")],
    "daemonsets": [("apps/v1", "DaemonSet")],
    "statefulsets": [("apps/v1", "StatefulSet")],
    "jobs": [("batch/v1", "Job")],
    "cronjobs": [("batch/v1", "CronJob")],
    "persistentvolumes": [("v1", "PersistentVolume")],
    "persistentvolumeclaims": [("v1", "PersistentVolumeClaim")],
    "services": [("v1", "Service")],
    "configmaps": [("v1", "ConfigMap")],
    "secrets": [("v1", "Secret")],
    "ingresses": [("networking.k8s.io/v1", "Ingress")],
    "customresourcedefinitions": [("apiextensions.k8s.io/v1", "CustomResourceDefinition")],
    "roles": [("rbac.authorization.k8s.io/v1", "Role")],
    "rolebindings": [("rbac.authorization.k8s.io/v1", "RoleBinding")],
    "clusterroles": [("rbac.authorization.k8s.io/v1", "ClusterRole")],
    "clusterrolebindings": [("rbac.authorization.k8s.io/v1", "ClusterRoleBinding")],
    "storageclasses": [("storage.k8s.io/v1", "StorageClass")],
}

METADATA_FIELDS_TO_REMOVE = {
    "uid",
    "resourceVersion",
    "selfLink",
    "generation",
    "managedFields",
    "creationTimestamp",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
    # Finalizers should be removed during backup as they reference cluster-specific controllers
    # They will be re-added by controllers in the target cluster if needed
    "finalizers",
    # ownerReferences should be removed as they reference cluster-specific parent resources
    # These relationships will be re-established in the target cluster if needed
    "ownerReferences",
}

ANNOTATIONS_TO_REMOVE = {
    "kubectl.kubernetes.io/",  # kubectl 自动生成的注解
    "deployment.kubernetes.io/revision",  # Deployment 控制器自动生成的版本号
    "kubernetes.io/",  # Kubernetes 系统自动生成的注解
    "control-plane.alpha.kubernetes.io/",  # 控制平面自动生成的注解
    "pv.kubernetes.io/",  # PV 相关自动生成的注解
    "pv.beta.kubernetes.io/",  # PV beta 相关自动生成的注解
    "volume.beta.kubernetes.io/",  # Volume 相关自动生成的注解
}

# 系统自动生成的 labels 前缀（这些应该被移除，因为它们由控制器自动管理）
LABELS_TO_REMOVE = {
    "pod-template-hash",  # Deployment/StatefulSet 自动生成的 Pod 模板哈希
    "controller-uid",  # Job 控制器自动生成的 UID
    "job-name",  # Job 自动生成的名称标签（对于 Job 创建的 Pod）
    "statefulset.kubernetes.io/pod-name",  # StatefulSet 自动生成的 Pod 名称
}

# --env-mapping 中 Downward API 占位前缀（值以此前缀开头时写入 valueFrom.fieldRef，不使用 env.value 内联）
# 参考: https://kubernetes.io/docs/tasks/inject-data-application/environment-variable-expose-pod-information/
ENV_MAPPING_FIELDREF_PREFIX = "@k8s:"
# 允许的 fieldPath（过宽易导致无效清单；标签需用 metadata.labels['key'] 形式）
ALLOWED_DOWNWARD_ENV_FIELDPATHS = frozenset({
    "metadata.namespace",
    "metadata.name",
    "spec.nodeName",
    "spec.serviceAccountName",
    "status.podIP",
    "status.hostIP",
})
_ENV_FIELDPATH_LABEL_RE = re.compile(r"^metadata\.labels\['([^'\\]+)']$")
_ENV_FIELDPATH_ANN_RE = re.compile(r"^metadata\.annotations\['([^'\\]+)']$")


# -------------------------
# Enhanced Configuration Classes
# -------------------------
@dataclass
class BackupConfig:
    """Configuration for backup operations"""
    kubeconfig: Optional[str] = None
    context: Optional[str] = None
    namespace: Optional[str] = None
    resources: List[str] = None
    output_dir: str = "/opt/k8s-backup"
    include_crds: bool = False
    clean_metadata: bool = True
    create_tarball: bool = False
    max_workers: int = 5
    timeout: int = 30
    retry_attempts: int = 3
    dry_run: bool = False
    label_selector: Optional[str] = None
    field_selector: Optional[str] = None
    include_names: Optional[frozenset] = None
    backup_name: Optional[str] = None

    def __post_init__(self):
        if self.resources is None:
            self.resources = DEFAULT_RESOURCES.copy()
        if self.backup_name is None:
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            ns_suffix = self.namespace or 'all'
            self.backup_name = f"backup-{timestamp}-{ns_suffix}"


@dataclass
class RestoreConfig:
    """restore 子命令配置。env_namespace_substitute 仅在存在 namespace_mapping 时改写仍为 env.value 内联字符串的项；merge_patch 与 kubectl patch -p 一致，详见 -h 专节。"""
    kubeconfig: Optional[str] = None
    context: Optional[str] = None
    backup_dir: str = ""
    namespace_mapping: Dict[str, str] = field(default_factory=dict)
    image_mapping: Dict[str, str] = field(default_factory=dict)
    env_mapping: Dict[str, str] = field(default_factory=dict)
    max_workers: int = 5
    dry_run: bool = False
    skip_crds: bool = False
    skip_cluster_scoped: bool = False
    create_namespaces: bool = True
    backup_name: Optional[str] = None
    env_namespace_substitute: str = "auto"
    merge_patch: Optional[Dict[str, Any]] = None
    merge_patch_kinds: Optional[Set[str]] = None


@dataclass
class TransformationRule:
    """恢复阶段变换规则：命名空间、镜像、env_mapping（含 @k8s）、env_namespace_substitute（仅 env.value 内联字符串）、merge_patch（与 kubectl patch -p 一致的部分对象合并）。"""
    namespace_mapping: Dict[str, str] = field(default_factory=dict)
    image_mapping: Dict[str, str] = field(default_factory=dict)
    env_mapping: Dict[str, str] = field(default_factory=dict)
    env_namespace_substitute: str = "auto"
    merge_patch: Optional[Dict[str, Any]] = None
    merge_patch_kinds: Optional[Set[str]] = None

    def transform_namespace(self, original_ns: str) -> str:
        """Transform namespace according to mapping rules"""
        return self.namespace_mapping.get(original_ns, original_ns)

    def transform_image(self, original_image: str) -> str:
        """
        按前缀规则改写容器镜像引用字符串（对应 Pod/Container 的 image 字段）。

        Kubernetes 中镜像名为单一字符串（见官方文档 Container Images）：
        可含仓库/路径、冒号标签、@sha256: digest。标签与 digest 均属于该字符串后缀，
        无单独 API 字段。

        - 仅替换与 old_prefix 匹配的前缀；未匹配则保持原样。
        - 若需改标签或 digest，将旧引用中含标签或 digest 的前缀写在映射左侧，例如
          registry.io/app:v1.0=registry.io/app:v2.0
        - 多规则时按最长前缀优先匹配，避免短规则抢在带仓库路径的规则之前命中。

        Args:
            original_image: container.image 的完整值

        Returns:
            替换后的镜像字符串
        """
        if not original_image or not self.image_mapping:
            return original_image
        for old_prefix, new_prefix in sorted(
            self.image_mapping.items(),
            key=lambda kv: len(kv[0]),
            reverse=True,
        ):
            if original_image.startswith(old_prefix):
                return original_image.replace(old_prefix, new_prefix, 1)
        return original_image

    def transform_env_value(self, env_name: str, original_value: str) -> Optional[str]:
        """
        转换环境变量值（按 KEY 的映射；@k8s: 目标由 ResourceTransformer 写成 valueFrom，而非内联 value）。
        
        基于环境变量 key 的精确映射，符合 Kubernetes 与声明式配置习惯：
        - CLI 格式: "ENV_KEY=new_value"（值中可含冒号、URL 等；键值只用第一个 = 分隔）
        - 映射值为空字符串 "" 表示删除该环境变量（返回 None）
        - 若原资源中不存在该 key，映射会新增该环境变量
        
        Args:
            env_name: 环境变量名称（key）
            original_value: 原始值
            
        Returns:
            Optional[str]: 转换后的值，如果返回 None 表示应删除该环境变量
        """
        if not env_name:
            return original_value
        
        # 检查是否有针对此环境变量 key 的映射
        if env_name in self.env_mapping:
            new_value = self.env_mapping[env_name]
            # 如果映射值为空字符串，返回 None 表示删除
            if new_value == "":
                return None
            # 否则返回新值
            return new_value
        
        # 如果没有映射，返回原始值
        return original_value

    @staticmethod
    def substitute_namespace_in_plain_string(
        text: str,
        namespace_mapping: Dict[str, str],
        mode: str,
    ) -> str:
        """
        按 namespace_mapping（源命名空间到目标命名空间）改写一段纯文本，供 env.value 内联字符串使用。

        说明：Kubernetes 中 env 条目均在 YAML 中；本函数处理的是「字符串内容」层面的替换，
        与是否使用 valueFrom 无关（valueFrom 在恢复流程中由其它步骤处理）。

        - off：不替换。
        - all：对每个源命名空间字符串做全文 replace（多规则时源名从长到短）。
        - auto：保守规则（整值等于源名、后缀 .源名、中间 .源名.、前缀 源名.）。

        须在 --namespace-mapping 非空且调用方（如恢复）选择非 off 时使用。
        与 Downward API（@k8s:）互补；详见运行本脚本加 -h 时「恢复阶段：环境变量」专节。
        """
        if mode == "off" or not text or not namespace_mapping:
            return text
        pairs = sorted(namespace_mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
        out = text
        if mode == "all":
            for old_ns, new_ns in pairs:
                if old_ns:
                    out = out.replace(old_ns, new_ns)
            return out
        for old_ns, new_ns in pairs:
            if not old_ns:
                continue
            if out == old_ns:
                return new_ns
            suf = "." + old_ns
            if out.endswith(suf):
                out = out[: -len(suf)] + "." + new_ns
                continue
            mid = "." + old_ns + "."
            if mid in out:
                out = out.replace(mid, "." + new_ns + ".")
            pref = old_ns + "."
            if out.startswith(pref):
                out = new_ns + out[len(old_ns) :]
        return out


# -------------------------
# Logging Configuration
# -------------------------
class StructuredLogger:
    """结构化日志记录器，支持标准输出和文件输出"""
    
    def __init__(self, name: str, level: int = logging.INFO):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)

        if not self.logger.handlers:
            # 标准输出处理器
            stdout_handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            stdout_handler.setFormatter(formatter)
            # 使用 extra={'to_stdout': True} 才输出到标准输出
            stdout_handler.addFilter(lambda record: getattr(record, 'to_stdout', False))
            self.logger.addHandler(stdout_handler)

    def info(self, message: str, **kwargs):
        """记录信息日志"""
        if kwargs:
            self.logger.info(f"{message} | {kwargs}", extra={'to_stdout': True})
        else:
            self.logger.info(message, extra={'to_stdout': True})

    def error(self, message: str, **kwargs):
        """记录错误日志"""
        if kwargs:
            self.logger.error(f"{message} | {kwargs}", extra={'to_stdout': True})
        else:
            self.logger.error(message, extra={'to_stdout': True})

    def warning(self, message: str, **kwargs):
        """记录警告日志"""
        if kwargs:
            self.logger.warning(f"{message} | {kwargs}", extra={'to_stdout': True})
        else:
            self.logger.warning(message, extra={'to_stdout': True})

    def debug(self, message: str, **kwargs):
        """记录调试日志"""
        if kwargs:
            self.logger.debug(f"{message} | {kwargs}", extra={'to_stdout': True})
        else:
            self.logger.debug(message, extra={'to_stdout': True})


logger = StructuredLogger("k8s_backup")


# -------------------------
# Retry Configuration (保持原样)
# -------------------------
def retry_on_api_error(exception):
    """
    判断是否应该重试特定的 API 错误。
    
    Args:
        exception: 异常对象
        
    Returns:
        bool: 如果应该重试返回 True，否则返回 False
    """
    if isinstance(exception, ResourceNotFoundError):
        return False
    if isinstance(exception, ApiException):
        status = getattr(exception, "status", None)
        if status is None:
            return False
        # 5xx、429；409 在并发 SSA / 资源更新竞态下可能出现，短重试可提高生产成功率
        # 参考: https://kubernetes.io/docs/reference/using-api/server-side-apply/#conflicts
        return (500 <= status < 600) or status == 429 or status == 409
    return isinstance(exception, (ConnectionError, TimeoutError))


retry_decorator = retry(
    retry=retry_if_exception(retry_on_api_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    before_sleep=before_sleep_log(logger.logger, logging.WARNING),
)


# -------------------------
# Utility Functions (增强)
# -------------------------
def ensure_directory(path: str) -> None:
    """
    确保目录存在，并设置适当的权限。
    
    Args:
        path: 目录路径
    """
    path = path or "."
    Path(path).mkdir(parents=True, exist_ok=True, mode=0o755)


def sanitize_filename(name: str) -> str:
    """
    清理字符串以便用于文件名。
    
    Args:
        name: 要清理的字符串
        
    Returns:
        str: 清理后的文件名安全字符串
    """
    return "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in name)


def _encode_bytes_in_obj(obj: Any) -> Any:
    """
    递归遍历对象（字典/列表/基本类型）并将字节转换为 base64 字符串。
    
    Args:
        obj: 要编码的对象
        
    Returns:
        Any: 编码后的对象
    """
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode('ascii')
    if isinstance(obj, dict):
        return {k: _encode_bytes_in_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_encode_bytes_in_obj(i) for i in obj]
    return obj


def write_yaml_safely(data: Dict, filepath: str, dry_run: bool = False) -> None:
    """
    安全地将 YAML 数据写入文件，包含错误处理。
    
    Args:
        data: 要写入的字典数据
        filepath: 文件路径
        dry_run: 是否为模拟运行（为 True 时不创建目录、不写文件，仅打日志）
        
    Raises:
        IOError: 文件写入失败
        yaml.YAMLError: YAML 序列化失败
    """
    try:
        if dry_run:
            logger.info(f"[模拟运行] 将写入 YAML 文件 {filepath}")
            return
        dirpath = os.path.dirname(filepath) or "."
        ensure_directory(dirpath)

        safe_data = _encode_bytes_in_obj(data)

        with open(filepath, 'w', encoding='utf-8') as f:
            yaml.safe_dump(
                safe_data,
                f,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False
            )
    except (IOError, yaml.YAMLError) as e:
        logger.error(f"写入 YAML 文件 {filepath} 失败: {e}")
        raise


class MappingParseError(ValueError):
    """映射字符串解析或校验失败。"""


class IncludeNamesParseError(ValueError):
    """--include-names 解析或校验失败。"""


# Kubernetes 命名空间名称（DNS 标签）
_K8S_DNS_LABEL_RE = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
# 对象 metadata.name（DNS 子域名，RFC 1123）— 与 API 常见约束一致
# 参考: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-subdomain-names
# 对齐 apimachinery/pkg/util/validation DNS1123Subdomain 形态
_K8S_DNS_SUBDOMAIN_RE = re.compile(
    r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$'
)
# 容器环境变量名（C_IDENTIFIER）
_K8S_ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_namespace_mapping_token(key: str, value: str, token: str) -> None:
    if not value:
        raise MappingParseError(f"命名空间映射的目标不能为空，项: {token!r}")
    for label, role in ((key, "源"), (value, "目标")):
        if len(label) > 63 or not _K8S_DNS_LABEL_RE.match(label):
            raise MappingParseError(
                f"命名空间映射{role}名称无效（须为 DNS 标签、最长 63）: {label!r}，项: {token!r}"
            )


def _validate_env_mapping_token(key: str, token: str) -> None:
    if not _K8S_ENV_NAME_RE.match(key):
        raise MappingParseError(
            f"环境变量名无效（须匹配 [A-Za-z_][A-Za-z0-9_]*）: {key!r}，项: {token!r}"
        )


def _validate_image_mapping_token(key: str, token: str) -> None:
    if not key:
        raise MappingParseError(f"镜像映射源前缀不能为空，项: {token!r}")


def parse_env_fieldref_from_mapping_value(value: str) -> Optional[str]:
    """
    解析 --env-mapping 中的 Downward API 占位值。

    若以 @k8s: 开头则返回 fieldPath（如 metadata.namespace），否则返回 None。
    参考: https://kubernetes.io/docs/tasks/inject-data-application/environment-variable-expose-pod-information/
    """
    if not value.startswith(ENV_MAPPING_FIELDREF_PREFIX):
        return None
    path = value[len(ENV_MAPPING_FIELDREF_PREFIX) :].strip()
    return path if path else None


def _validate_downward_field_path(path: str, token: str) -> None:
    if path in ALLOWED_DOWNWARD_ENV_FIELDPATHS:
        return
    if _ENV_FIELDPATH_LABEL_RE.match(path) or _ENV_FIELDPATH_ANN_RE.match(path):
        return
    raise MappingParseError(
        f"env 映射 @k8s: 的 fieldPath 无效或未支持: {path!r}（项: {token!r}）"
    )


def _parse_mapping_pair(token: str) -> Tuple[str, str]:
    """从单个映射项解析 key/value，仅支持第一个 '=' 作为分隔符。"""
    t = token.strip()
    if not t:
        raise MappingParseError("存在空的映射项")
    if "=" not in t:
        raise MappingParseError(f"映射项须为 KEY=value，无效项: {token!r}")
    key, value = t.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key:
        raise MappingParseError(f"映射键不能为空，项: {token!r}")
    return key, value


def _tokenize_mapping_string(mapping_str: str) -> List[str]:
    """
    将整段映射字符串拆成若干项（调用方保证非空且含 '='）。
    按「下一个 KEY=」前瞻分割，支持逗号/空白分隔；值中可含空格。
    """
    s = mapping_str.strip()
    if not s:
        return []
    parts = re.split(r"(?:\s*,\s*|\s+)(?=[^\s=]+=)", s)
    return [p.strip() for p in parts if p.strip()]


def parse_mapping(mapping_str: Optional[str], mapping_kind: str) -> Dict[str, str]:
    """
    解析命名空间 / 镜像 / 环境变量映射字符串。

    唯一支持的写法（各类映射通用）：
        KEY1=value1 KEY2=value2, KEY3=value3
    多项之间可用逗号、空白或「逗号+空白」分隔。键与值之间只用第一个 = 分割，故值中可含 ':'、'=' 等。

    环境变量：
        - KEY= 或 KEY="" 表示删除该环境变量（空值）
        - 增：原清单无该 KEY 时于恢复阶段新增
        - 改：有则替换值（含从 valueFrom 改为 value）
        - 删：见上
        - Downward API（恢复时写入 valueFrom.fieldRef）: KEY=@k8s:metadata.namespace 等，
          fieldPath 须为白名单内字段或 metadata.labels['app'] / metadata.annotations['k'] 形式
        - env.value 内联字符串与 --env-namespace-substitute：与 env.valueFrom 的区分、依赖关系与处理顺序以
          运行 python KubeBackupCli.py -h 时打印的「恢复阶段：环境变量」整节为准

    示例（与 mapping_kind 对应，仅供理解写法）：
        namespace：old-team-ns=new-team-ns
        image：harbor.old.com/library/=harbor.new.com/library/
        env：API_URL=https://api.prod:443（删除某键用 KEY= 空值，见上文「环境变量」列表）

    Args:
        mapping_str: 映射字符串；None 或空白视为无映射
        mapping_kind: "namespace" | "image" | "env"

    Returns:
        有序映射字典（重复键且值冲突时报错）

    Raises:
        MappingParseError: 格式或校验不通过
    """
    if not mapping_str or not str(mapping_str).strip():
        return {}
    s = str(mapping_str).strip()
    if "=" not in s:
        raise MappingParseError("映射须使用 KEY=value 形式，且至少包含一个 '='")
    if mapping_kind not in ("namespace", "image", "env"):
        raise MappingParseError(f"未知 mapping_kind: {mapping_kind!r}")

    result: Dict[str, str] = {}
    tokens = _tokenize_mapping_string(s)

    for token in tokens:
        key, value = _parse_mapping_pair(token)
        if mapping_kind == "namespace":
            _validate_namespace_mapping_token(key, value, token)
        elif mapping_kind == "env":
            _validate_env_mapping_token(key, token)
            if value:
                fp = parse_env_fieldref_from_mapping_value(value)
                if fp is not None:
                    _validate_downward_field_path(fp, token)
        else:
            _validate_image_mapping_token(key, token)

        if key in result and result[key] != value:
            raise MappingParseError(
                f"映射键 {key!r} 重复且值不一致: {result[key]!r} 与 {value!r}"
            )
        result[key] = value
    return result


# -------------------------
# CLI 参数校验（入口统一校验，避免非法参数进入业务逻辑后抛未处理异常）
# -------------------------
KNOWN_RESOURCE_TYPES = frozenset(RESOURCE_API_MAPPING.keys())
MAX_WORKERS_CAP = 128
MAX_CLI_PATH_LEN = 4096
# 内联 --merge-patch（JSON）：控制 shell/转义与可读性风险；超限须改用文件（与 kubectl 形状一致，无额外语义）
MAX_MERGE_PATCH_INLINE_UTF8_BYTES = 32768
MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH = 20
MAX_BACKUP_NAME_LEN = 200
MAX_CONTEXT_LEN = 256
MAX_SELECTOR_LEN = 8192
# metadata.name 最长 253（DNS1123Subdomain）；单次 --include-names 条目上限防滥用
MAX_K8S_OBJECT_NAME_LEN = 253
MAX_INCLUDE_NAMES_COUNT = 512

_K8S_RESOURCE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_BACKUP_NAME_BAD = re.compile(r'[/\\\x00\r\n]|[<>:"|?*]')


def _cli_strip_opt(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    t = str(s).strip()
    return t if t else None


def _cli_reject_control_chars(s: Optional[str], option_label: str) -> bool:
    if s is None:
        return True
    if "\x00" in s:
        logger.error(f"{option_label} 不能包含空字符")
        return False
    return True


def validate_cli_max_workers(n: int) -> bool:
    if n < 1:
        logger.error("--max-workers 须为 >= 1 的整数")
        return False
    if n > MAX_WORKERS_CAP:
        logger.error(f"--max-workers 过大（建议 1–{MAX_WORKERS_CAP}）: {n}")
        return False
    return True


def validate_cli_kubeconfig(path: Optional[str]) -> bool:
    p = _cli_strip_opt(path)
    if not p:
        return True
    exp = os.path.abspath(os.path.expanduser(p))
    if len(exp) > MAX_CLI_PATH_LEN:
        logger.error("--kubeconfig 路径过长")
        return False
    if not os.path.isfile(exp):
        logger.error(f"--kubeconfig 不是有效文件: {exp}")
        return False
    if not os.access(exp, os.R_OK):
        logger.error(f"--kubeconfig 文件不可读: {exp}")
        return False
    return True


def validate_cli_context(ctx: Optional[str]) -> bool:
    c = _cli_strip_opt(ctx)
    if c is None:
        return True
    if len(c) > MAX_CONTEXT_LEN:
        logger.error(f"--context 过长（最大 {MAX_CONTEXT_LEN}）")
        return False
    return True


def validate_cli_selector(sel: Optional[str], label: str) -> bool:
    s = _cli_strip_opt(sel)
    if s is None:
        return True
    if len(s) > MAX_SELECTOR_LEN:
        logger.error(f"{label} 过长（最大 {MAX_SELECTOR_LEN}）")
        return False
    if "\x00" in s:
        logger.error(f"{label} 不能包含空字符")
        return False
    return True


def validate_cli_backup_namespace(name: str) -> bool:
    if name == "all":
        return True
    if len(name) > 63 or not _K8S_DNS_LABEL_RE.match(name):
        logger.error(
            f"命名空间名称无效（须为 DNS 标签、最长 63，或与 --all-namespaces 联用）: {name!r}"
        )
        return False
    return True


def is_valid_k8s_object_name(name: str) -> bool:
    """校验 Kubernetes 对象 metadata.name（DNS 子域名）。"""
    if not name or len(name) > MAX_K8S_OBJECT_NAME_LEN:
        return False
    return _K8S_DNS_SUBDOMAIN_RE.match(name) is not None


def parse_include_names(names_str: Optional[str]) -> Optional[frozenset]:
    """
    解析 backup --include-names。

    未指定或空白时返回 None（不过滤，与历史行为一致）。
    多项逗号分隔；按 metadata.name 精确匹配（大小写敏感，与 API 一致）。
    """
    s = _cli_strip_opt(names_str)
    if s is None:
        return None
    if len(s) > MAX_SELECTOR_LEN:
        raise IncludeNamesParseError(
            f"--include-names 过长（最大 {MAX_SELECTOR_LEN} 字符）"
        )
    if "\x00" in s:
        raise IncludeNamesParseError("--include-names 不能包含空字符")
    tokens = [t.strip() for t in s.split(",") if t.strip()]
    if not tokens:
        raise IncludeNamesParseError("--include-names 须至少包含一个对象名")
    if len(tokens) > MAX_INCLUDE_NAMES_COUNT:
        raise IncludeNamesParseError(
            f"--include-names 条目过多（最多 {MAX_INCLUDE_NAMES_COUNT} 个）"
        )
    unique: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        if token in seen:
            logger.debug(f"--include-names 重复项已忽略: {token!r}")
            continue
        if not is_valid_k8s_object_name(token):
            raise IncludeNamesParseError(
                f"对象名无效（须为 DNS 子域名、最长 {MAX_K8S_OBJECT_NAME_LEN}）: {token!r}"
            )
        seen.add(token)
        unique.append(token)
    return frozenset(unique)


def filter_resources_by_include_names(
    items: List[Dict], include_names: frozenset
) -> List[Dict]:
    """
    在 API List 结果上按 metadata.name 保留指定对象。

    与 label/field selector 为 AND 关系：先由 API 过滤，再按名称精确匹配。
    fieldSelector 对多名称的 in 语义因资源类型与版本而异，故在客户端做稳定过滤。
    """
    if not include_names:
        return items
    filtered: List[Dict] = []
    for itm in items:
        if not isinstance(itm, dict):
            continue
        meta = itm.get("metadata")
        if not isinstance(meta, dict):
            continue
        name = meta.get("name")
        if isinstance(name, str) and name in include_names:
            filtered.append(itm)
    return filtered


def validate_cli_resource_types(resources: List[str]) -> bool:
    for r in resources:
        if not _K8S_RESOURCE_TOKEN_RE.match(r):
            logger.error(
                f"非法资源类型: {r!r}（须小写字母开头，仅含小写、数字、连字符，最长 63）"
            )
            return False
        if r not in KNOWN_RESOURCE_TYPES:
            logger.warning(
                f"资源类型 {r!r} 不在内置列表，将尝试 API 动态发现；无对应资源时备份结果为空"
            )
    return True


def validate_cli_backup_name(name: Optional[str]) -> bool:
    n = _cli_strip_opt(name)
    if n is None:
        return True
    if len(n) > MAX_BACKUP_NAME_LEN:
        logger.error(f"--backup-name 过长（最大 {MAX_BACKUP_NAME_LEN}）")
        return False
    if n in (".", ".."):
        logger.error("--backup-name 不能为 '.' 或 '..'")
        return False
    if _BACKUP_NAME_BAD.search(n):
        logger.error("--backup-name 不能含路径分隔符或 <>:\"|?* 等非法字符")
        return False
    return True


def validate_cli_output_dir_for_backup(output_dir: str, dry_run: bool) -> bool:
    if not _cli_reject_control_chars(output_dir, "输出目录"):
        return False
    raw = str(output_dir).strip()
    if not raw:
        logger.error("--output-dir 不能为空")
        return False
    base = os.path.abspath(os.path.expanduser(raw))
    if len(base) > MAX_CLI_PATH_LEN:
        logger.error("--output-dir 路径过长")
        return False
    if dry_run:
        return True
    try:
        Path(base).mkdir(parents=True, exist_ok=True, mode=0o755)
    except OSError as e:
        logger.error(f"无法创建或访问输出目录 {base}: {e}")
        return False
    if not os.path.isdir(base):
        logger.error(f"--output-dir 不是目录: {base}")
        return False
    if not os.access(base, os.W_OK):
        logger.error(f"输出目录不可写: {base}")
        return False
    return True


def validate_cli_restore_backup_dir(path: str) -> Optional[str]:
    if not _cli_reject_control_chars(path, "--backup-dir"):
        return None
    raw = str(path).strip()
    if not raw:
        logger.error("--backup-dir 不能为空")
        return None
    exp = os.path.abspath(os.path.expanduser(raw))
    if len(exp) > MAX_CLI_PATH_LEN:
        logger.error("--backup-dir 路径过长")
        return None
    if not os.path.exists(exp):
        logger.error(f"备份目录不存在: {exp}")
        return None
    if not os.path.isdir(exp):
        logger.error(f"--backup-dir 不是目录: {exp}")
        return None
    if not os.access(exp, os.R_OK):
        logger.error(f"备份目录不可读: {exp}")
        return None
    return exp


def load_kubernetes_yaml_documents(filepath: str) -> List[Dict[str, Any]]:
    """
    读取单个 YAML 文件中的全部文档（与多对象清单中 --- 分隔行为一致）。

    若文件含多段对象而只解析第一段，会在无告警情况下丢失后续资源；恢复须与 kubectl apply 清单语义一致。
    例：同一文件内先写 Deployment、下一行 --- 再写 Service，本函数返回两个对象，恢复时逐个 apply。
    参考: https://kubernetes.io/docs/concepts/cluster-administration/manage-deployment/
    """
    out: List[Dict[str, Any]] = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for idx, doc in enumerate(yaml.safe_load_all(f), start=1):
            if doc is None:
                continue
            if not isinstance(doc, dict):
                logger.warning(
                    f"{filepath}: 第 {idx} 段 YAML 不是映射，已跳过（应为 Kubernetes 对象）"
                )
                continue
            out.append(doc)
    return out


def deep_merge_k8s_dict(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    """
    将 overlay 深度合并进 base（原地修改 base）。

    - 双方同一键均为 dict 时递归合并子键；
    - 否则以 overlay 的值覆盖 base 中该键（含 list、str、int 等；列表为整段替换，非数组合并）。
    用于恢复前并入与 kubectl patch -p 相同形状的部分对象；与 apiserver strategic merge 对「列表按主键合并」
    的差异见 Kubernetes 文档，复杂列表字段请按官方语义自行核对清单。
    """
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            deep_merge_k8s_dict(base[k], v)
        else:
            base[k] = v


def validate_cli_merge_patch_path(path: str) -> Optional[str]:
    if not _cli_reject_control_chars(path, "--merge-patch"):
        return None
    raw = str(path).strip()
    if not raw:
        logger.error("--merge-patch 不能为空")
        return None
    exp = os.path.abspath(os.path.expanduser(raw))
    if len(exp) > MAX_CLI_PATH_LEN:
        logger.error("--merge-patch 路径过长")
        return None
    if not os.path.exists(exp):
        logger.error(f"--merge-patch 文件不存在: {exp}")
        return None
    if not os.path.isfile(exp):
        logger.error(f"--merge-patch 不是文件: {exp}")
        return None
    if not os.access(exp, os.R_OK):
        logger.error(f"--merge-patch 文件不可读: {exp}")
        return None
    return exp


# 与 kubectl patch -p 惯例一致：补丁为「部分对象」，不应含根级 apiVersion/kind/status
_MERGE_PATCH_FORBIDDEN_ROOT_KEYS = frozenset({"apiVersion", "kind", "status"})


def _merge_patch_inline_structure_depth(obj: Any) -> int:
    """
    估算内联 JSON 补丁的结构深度（dict 向下 +1；list 取元素最大深度不额外加层，避免 tolerations 等数组误伤）。
    仅用于拒绝过深内联；文件补丁不做此限制。
    """
    if isinstance(obj, dict):
        if not obj:
            return 1
        return 1 + max(_merge_patch_inline_structure_depth(v) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return 0
        return max(_merge_patch_inline_structure_depth(v) for v in obj)
    return 0


def _sanitize_merge_patch_fragment_inplace(
    frag: Dict[str, Any],
    source_hint: str,
    warned_flag: List[bool],
) -> None:
    """从补丁片段根级移除 apiVersion/kind/status；首次移除时打一条 warning。"""
    bad = _MERGE_PATCH_FORBIDDEN_ROOT_KEYS & frag.keys()
    if not bad:
        return
    if not warned_flag[0]:
        logger.warning(
            f"{source_hint}: 补丁根级含 {sorted(bad)}，已忽略（与 kubectl patch -p 相同，勿含 apiVersion/kind/status）"
        )
        warned_flag[0] = True
    for k in bad:
        frag.pop(k, None)


def load_merge_patch_file(filepath: str) -> Optional[Dict[str, Any]]:
    """
    加载 --merge-patch：形状与 kubectl patch … -p '<json>' 相同的部分 Kubernetes 对象（YAML 或 JSON）。

    例（Deployment，与官方 API 一致）：
        {"spec": {"template": {"spec": {"nodeSelector": {"release": "production"}}}}}

    支持多文档 YAML：各段按顺序先合并为单一补丁。根级若含 apiVersion/kind/status 将剔除并告警。
    文件模式不设内联 JSON 的结构深度上限；复杂 patch、多段 YAML、需入库评审的场景应优先用本路径而非命令行内联。
    """
    merged: Dict[str, Any] = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            docs = list(yaml.safe_load_all(f))
    except (IOError, OSError) as e:
        logger.error(f"读取 merge-patch 失败: {e}")
        return None
    except yaml.YAMLError as e:
        logger.error(f"解析 merge-patch YAML 失败: {e}")
        return None

    any_doc = False
    warned_forbidden: List[bool] = [False]
    for idx, doc in enumerate(docs, start=1):
        if doc is None:
            continue
        any_doc = True
        if not isinstance(doc, dict):
            logger.error(
                f"{filepath}: 第 {idx} 段不是映射（应为与 kubectl patch -p 一致的部分对象）"
            )
            return None
        frag = dict(doc)
        _sanitize_merge_patch_fragment_inplace(frag, filepath, warned_forbidden)
        deep_merge_k8s_dict(merged, frag)
    if not any_doc:
        logger.error(f"{filepath}: 未解析到任何 YAML 文档（文件为空？）")
        return None
    return merged


def parse_merge_patch_json_string(json_text: str, source_hint: str) -> Optional[Dict[str, Any]]:
    """
    解析与 kubectl patch -p 相同的 JSON 对象字符串（如脚本内 dict 的 JSON 表示）。
    须为 JSON object，不得为数组；根级 apiVersion/kind/status 将剔除。
    内联场景另受结构深度约束（见 load_merge_patch_from_cli_arg）；复杂补丁请用文件。
    """
    if json_text.startswith("\ufeff"):
        json_text = json_text[1:]
    try:
        val = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error(f"{source_hint} 不是合法 JSON: {e}")
        return None
    if not isinstance(val, dict):
        logger.error(f"{source_hint} 须为 JSON 对象（映射），当前为 {type(val).__name__}")
        return None
    frag = dict(val)
    _sanitize_merge_patch_fragment_inplace(frag, source_hint, [False])
    if not frag:
        logger.warning(f"{source_hint}: 剔除禁止根字段后为空，合并不产生任何字段变更")
        return frag
    depth = _merge_patch_inline_structure_depth(frag)
    if depth > MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH:
        logger.error(
            f"{source_hint}: 嵌套过深（结构深度约 {depth}，内联上限 {MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH}）；"
            "企业环境请将复杂补丁写入 YAML/JSON 文件并通过路径传入，与简单 kubectl -p 片段分工一致"
        )
        return None
    return frag


def load_merge_patch_from_cli_arg(arg_value: str) -> Optional[Dict[str, Any]]:
    """
    解析 --merge-patch：文件路径，或以「{」开头的内联 JSON（与 kubectl -p 一致）。

    生产约束：内联仅适合短、浅结构（UTF-8 字节与嵌套深度有上限）；多文档、大段 affinity/volumes 等请用文件，
    降低 shell 转义错误并便于 Code Review 与配置库管理。
    """
    raw = str(arg_value).strip()
    if not raw:
        logger.error("--merge-patch 不能为空")
        return None
    if raw.startswith("{"):
        ub = len(raw.encode("utf-8"))
        if ub > MAX_MERGE_PATCH_INLINE_UTF8_BYTES:
            logger.error(
                f"内联 merge-patch 过大（UTF-8 约 {ub} 字节，上限 {MAX_MERGE_PATCH_INLINE_UTF8_BYTES}）；"
                "请改用 YAML/JSON 文件路径传入，便于稳定评审与避免命令行长度/转义问题"
            )
            return None
        return parse_merge_patch_json_string(raw, "--merge-patch 内联 JSON")
    if len(raw) > MAX_CLI_PATH_LEN:
        logger.error("--merge-patch 文件路径过长")
        return None
    patch_path = validate_cli_merge_patch_path(raw)
    if patch_path is None:
        return None
    return load_merge_patch_file(patch_path)


def load_backup_metadata(backup_dir: str) -> Dict[str, Any]:
    """
    加载备份元数据文件。
    
    Args:
        backup_dir: 备份目录路径
        
    Returns:
        Dict[str, Any]: 元数据字典，如果文件不存在则返回空字典
    """
    metadata_file = os.path.join(backup_dir, "backup-metadata.json")
    if os.path.exists(metadata_file):
        try:
            with open(metadata_file, 'r', encoding='utf-8') as f:
                raw = f.read()
            if len(raw) > 4 * 1024 * 1024:
                logger.warning(
                    "backup-metadata.json 超过 4MB，已继续解析；若非正常备份请检查目录是否被篡改"
                )
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as e:
            logger.warning(f"backup-metadata.json 非合法 JSON，按无元数据处理: {e}")
            return {}
        except (IOError, OSError) as e:
            logger.warning(f"读取 backup-metadata.json 失败: {e}")
            return {}
    return {}


def save_backup_metadata(backup_dir: str, metadata: Dict[str, Any]) -> None:
    """
    保存备份元数据文件。
    
    Args:
        backup_dir: 备份目录路径
        metadata: 要保存的元数据字典
        
    Raises:
        IOError: 文件写入失败
    """
    metadata_file = os.path.join(backup_dir, "backup-metadata.json")
    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


# -------------------------
# Resource Cleaners (增强)
# -------------------------
class ResourceCleaner:
    """清理 Kubernetes 资源定义以便备份"""

    @staticmethod
    def clean_metadata(metadata: Dict, kind: str = '') -> Dict:
        """
        清理资源的 metadata 部分，只保留核心字段。
        
        根据 Kubernetes 最佳实践，移除所有非核心和自动生成的字段：
        - 系统生成的字段：uid, resourceVersion, selfLink, generation, managedFields
        - 时间戳字段：creationTimestamp, deletionTimestamp, deletionGracePeriodSeconds
        - 关系字段：finalizers, ownerReferences（由系统自动管理）
        - 系统生成的注解：kubectl.kubernetes.io/*, kubernetes.io/* 等
        - 系统生成的标签：pod-template-hash, controller-* 等
        - ServiceAccount 的自动生成的 secrets（如果存在）
        
        保留的核心字段：
        - name, namespace（如果存在）
        - 用户定义的 labels 和 annotations
        - 其他用户定义的 metadata 字段
        
        Args:
            metadata: 资源的 metadata 字典
            kind: 资源类型（用于特殊处理，如 ServiceAccount）
            
        Returns:
            Dict: 清理后的 metadata 字典（只包含核心字段）
        """
        if not metadata:
            return metadata

        # 移除所有标准的集群特定字段
        for field_name in METADATA_FIELDS_TO_REMOVE:
            metadata.pop(field_name, None)

        # ServiceAccount 特殊处理：移除自动生成的 secrets
        # 注意：ServiceAccount 的 secrets 字段在较新版本的 Kubernetes 中已移除
        # 但为了兼容性，我们仍然检查并移除（如果存在）
        if kind.lower() == 'serviceaccount':
            # ServiceAccount 的 secrets 字段（如果存在）是集群特定的
            # 这些 secrets 由 API server 自动管理，不应备份
            if 'secrets' in metadata:
                metadata.pop('secrets', None)

        # 清理 annotations：移除集群特定的注解和自动生成的注解
        annotations = metadata.get('annotations', {}) or {}
        if annotations:
            cleaned_annotations = {}
            for key, value in annotations.items():
                # 移除以指定前缀开头的注解（系统自动生成的）
                if any(key.startswith(prefix) for prefix in ANNOTATIONS_TO_REMOVE):
                    continue
                # 保留用户定义的注解
                cleaned_annotations[key] = value
            if cleaned_annotations:
                metadata['annotations'] = cleaned_annotations
            else:
                metadata.pop('annotations', None)

        # 清理 labels：移除系统自动生成的标签
        # 只保留用户定义的标签，删除控制器自动生成的标签
        labels = metadata.get('labels', {}) or {}
        if labels:
            cleaned_labels = {}
            for key, value in labels.items():
                # 移除系统自动生成的标签
                if key in LABELS_TO_REMOVE:
                    continue
                # 移除以系统前缀开头的标签（如 controller-*, pod-template-*）
                if key.startswith('controller-') or key.startswith('pod-template-'):
                    continue
                # 保留用户定义的标签
                cleaned_labels[key] = value
            if cleaned_labels:
                metadata['labels'] = cleaned_labels
            else:
                metadata.pop('labels', None)

        return metadata

    @staticmethod
    def _dedupe_container_ports_in_container(container: Dict, log_ctx: str) -> None:
        """
        按 (containerPort, protocol) 去重 ports 列表。

        Server-Side Apply 将 ports 视为按 (containerPort, protocol) 合并的列表；重复键会导致 apiserver
        返回 500（typed patch: duplicate entries）。部分集群曾允许同端口多 name，恢复时须规范化。
        """
        ports = container.get("ports")
        if not ports or not isinstance(ports, list):
            return
        seen = set()
        kept: List[Dict] = []
        dropped = 0
        for p in ports:
            if not isinstance(p, dict):
                kept.append(p)
                continue
            cp = p.get("containerPort")
            if cp is None:
                kept.append(p)
                continue
            proto = p.get("protocol") or "TCP"
            if isinstance(proto, str):
                proto = proto.upper()
            key = (cp, proto)
            if key in seen:
                dropped += 1
                cname = container.get("name", "?")
                pname = p.get("name", "")
                logger.warning(
                    f"{log_ctx} 容器 {cname}: 忽略重复 ports 项 containerPort={cp} protocol={proto}"
                    + (f" name={pname}" if pname else "")
                    + "（SSA 与同键冲突；保留首次出现的项）"
                )
                continue
            seen.add(key)
            kept.append(p)
        if dropped:
            if kept:
                container["ports"] = kept
            else:
                container.pop("ports", None)

    @staticmethod
    def dedupe_container_ports_in_pod_spec(pod_spec: Dict, log_ctx: str) -> None:
        """对 PodSpec 内所有容器（含 init/ephemeral）执行 ports 去重。"""
        if not isinstance(pod_spec, dict):
            return
        for ckey in ("initContainers", "containers", "ephemeralContainers"):
            for ctr in pod_spec.get(ckey) or []:
                if isinstance(ctr, dict):
                    ResourceCleaner._dedupe_container_ports_in_container(ctr, log_ctx)

    @staticmethod
    def _clean_pod_spec(pod_spec: Dict) -> None:
        """
        清理 Pod spec 中的集群特定字段
        
        Args:
            pod_spec: Pod spec 字典（会被原地修改）
        """
        # 移除节点特定字段（将由调度器分配）
        pod_spec.pop('nodeName', None)
        pod_spec.pop('hostname', None)
        pod_spec.pop('subdomain', None)
        
        # 移除自动分配的服务账户 token volume
        # 这些由 API server 自动注入
        if 'volumes' in pod_spec:
            pod_spec['volumes'] = [
                v for v in pod_spec['volumes']
                if not (isinstance(v, dict) and v.get('name', '').startswith('default-token-'))
            ]
            if not pod_spec['volumes']:
                pod_spec.pop('volumes', None)

        ResourceCleaner.dedupe_container_ports_in_pod_spec(pod_spec, log_ctx="备份清理")

    @staticmethod
    def _clean_pod_template(template: Dict) -> None:
        """
        清理 Pod template 中的集群特定字段
        
        Args:
            template: Pod template 字典（会被原地修改）
        """
        # 清理 Pod template metadata
        if 'metadata' in template:
            template['metadata'] = ResourceCleaner.clean_metadata(
                template['metadata'],
                'Pod'
            )
        
        # 清理 Pod template spec
        if 'spec' in template:
            ResourceCleaner._clean_pod_spec(template['spec'])

    @staticmethod
    def _clean_service_spec(spec: Dict) -> None:
        """
        清理 Service spec 中的集群特定字段
        
        Args:
            spec: Service spec 字典（会被原地修改）
        """
        # 移除集群特定的 IP 分配（将由 API server 分配）
        # clusterIP 对于 ClusterIP 和 NodePort 服务应移除
        # 但对于 ExternalName 服务应保留（符合 Kubernetes 最佳实践）
        service_type = spec.get('type', 'ClusterIP')
        if service_type != 'ExternalName':
            spec.pop('clusterIP', None)
            spec.pop('clusterIPs', None)
        
        # loadBalancerIP 已弃用（迁移到 LB 控制器特定注解/实现）
        spec.pop('loadBalancerIP', None)
        # loadBalancerClass 为用户显式选择 LoadBalancer 实现类，属意向配置，应保留
        # 参考: https://kubernetes.io/docs/concepts/services-networking/service/#load-balancer-class

    @staticmethod
    def _clean_persistentvolume_spec(spec: Dict) -> None:
        """
        清理 PersistentVolume spec 中的集群特定字段
        
        Args:
            spec: PersistentVolume spec 字典（会被原地修改）
        """
        # 移除集群特定的 claim 引用（将在目标集群中重新建立）
        spec.pop('claimRef', None)
        # 移除节点亲和性（集群特定，节点可能不同）
        spec.pop('nodeAffinity', None)
        # 注意：volumeMode 是用户定义的，应保留
        # 它不是集群特定的，所以保留它

    @staticmethod
    def _clean_persistentvolumeclaim_spec(spec: Dict) -> None:
        """
        清理 PersistentVolumeClaim spec 中的集群特定字段
        
        Args:
            spec: PersistentVolumeClaim spec 字典（会被原地修改）
        """
        # 移除集群特定的 volume 名称（将由存储供应器分配）
        spec.pop('volumeName', None)
        # 注意：volumeMode 是用户定义的，应保留
        # 它是 PVC 规范的一部分，不是集群特定的

    @staticmethod
    def _clean_workload_spec(spec: Dict) -> None:
        """
        清理工作负载资源（Deployment/StatefulSet/DaemonSet/ReplicaSet）的 spec
        
        Args:
            spec: 工作负载资源的 spec 字典（会被原地修改）
        """
        # 清理 Pod template
        if 'template' in spec:
            ResourceCleaner._clean_pod_template(spec['template'])
        
        # 注意：selector 对于 StatefulSet/Deployment 是不可变的，但我们保留它以用于恢复
        # 恢复过程将处理 selector 不匹配的冲突
        # Server-side apply with force=True 将解决冲突

    @staticmethod
    def _clean_job_spec(spec: Dict) -> None:
        """
        清理 Job/CronJob spec 中的集群特定字段
        
        Args:
            spec: Job/CronJob spec 字典（会被原地修改）
        """
        # 清理 jobTemplate 中的 Pod template
        if 'jobTemplate' in spec:
            job_template = spec['jobTemplate']
            if 'spec' in job_template and 'template' in job_template['spec']:
                ResourceCleaner._clean_pod_template(job_template['spec']['template'])
        
        # 清理直接 template（用于 Job）
        elif 'template' in spec:
            ResourceCleaner._clean_pod_template(spec['template'])

    @staticmethod
    def clean_spec(spec: Dict, kind: str) -> Dict:
        """
        根据资源类型清理 spec 部分，只保留核心配置字段。
        
        移除的字段：
        - 集群特定的字段（如 clusterIP, volumeName, claimRef）
        - 节点特定的字段（如 nodeName, nodeAffinity）
        - 自动生成的字段（如默认 token volumes）
        
        保留的字段：
        - 用户定义的配置（如镜像、环境变量、资源限制等）
        - 用户定义的标签选择器
        - 用户定义的存储配置
        
        Args:
            spec: 资源的 spec 字典
            kind: 资源类型（如 'Service', 'Deployment'）
            
        Returns:
            Dict: 清理后的 spec 字典（只包含核心配置字段）
        """
        if not spec:
            return spec

        kind_lower = kind.lower() if isinstance(kind, str) else ''

        # 使用策略模式路由到不同的清理方法
        if kind_lower == 'service':
            ResourceCleaner._clean_service_spec(spec)
        elif kind_lower == 'persistentvolume':
            ResourceCleaner._clean_persistentvolume_spec(spec)
        elif kind_lower == 'persistentvolumeclaim':
            ResourceCleaner._clean_persistentvolumeclaim_spec(spec)
        elif kind_lower in ['deployment', 'statefulset', 'daemonset', 'replicaset']:
            ResourceCleaner._clean_workload_spec(spec)
        elif kind_lower in ['job', 'cronjob']:
            ResourceCleaner._clean_job_spec(spec)
        elif kind_lower == 'pod':
            # 独立 Pod（含静态 Pod 清单等）：spec 即 PodSpec，须与工作负载模板一致地清节点绑定字段
            ResourceCleaner._clean_pod_spec(spec)
        # ServiceAccount 和未知资源类型不需要特殊处理
        # ServiceAccount secrets 在 metadata 中处理
        # 未知资源类型保持原样

        return spec

    @staticmethod
    def clean_resource(resource: Dict) -> Dict:
        """
        主要的资源清理方法，确保备份文件只包含核心字段。
        
        清理策略：
        1. 清理 metadata：移除所有系统生成的字段，只保留用户定义的核心字段
        2. 清理 spec：移除集群特定和自动生成的字段，只保留用户配置
        3. 移除 status：状态字段完全移除（运行时生成，不应备份）
        
        这样备份的文件干净整洁，便于：
        - 版本控制和代码审查
        - 跨集群迁移
        - 恢复时修改、新增、删除字段
        - 符合 Kubernetes 声明式配置最佳实践
        
        Args:
            resource: 要清理的 Kubernetes 资源字典
            
        Returns:
            Dict: 清理后的资源字典（只包含核心字段，干净整洁）
        """
        if not isinstance(resource, dict):
            return resource

        kind = resource.get('kind', '')

        if 'metadata' in resource:
            resource['metadata'] = ResourceCleaner.clean_metadata(resource['metadata'], kind)

        if 'spec' in resource:
            resource['spec'] = ResourceCleaner.clean_spec(resource['spec'], kind)

        resource.pop('status', None)

        # 绝不虚构 apiVersion/kind；无效对象应在备份写入前被拒绝，避免生成无法 apply 的清单
        return resource


# -------------------------
# Resource Transformer (新增)
# -------------------------
class ResourceTransformer:
    """恢复阶段将备份清单改写为目标环境：命名空间、镜像、env 映射、env.value 内联命名空间替换、可选与 kubectl patch -p 一致的部分对象合并。"""

    def __init__(self, transformation_rule: TransformationRule):
        self.rule = transformation_rule

    def transform_resource(self, resource: Dict) -> Dict:
        """对单个资源依次执行本类定义的变换（含①～⑥步，见 -h 专节）。"""
        if not isinstance(resource, dict):
            return resource

        # ① namespace_mapping（metadata 等）
        resource = self._transform_namespace(resource)

        # ② image_mapping
        resource = self._transform_container_images(resource)

        # ③ env_mapping（含 @k8s:，写入 valueFrom）
        resource = self._transform_env_variables(resource)
        # ④ env_namespace_substitute：仅仍为 env.value 内联的条目（见 -h 专节）
        resource = self._substitute_namespace_in_env_plain_values(resource)

        # ⑤ merge_patch：与 kubectl patch -p 相同形状的部分对象，深度合并进完整资源（见 --merge-patch 与 -h 专节）
        resource = self._apply_merge_patch(resource)

        # ⑥ container.ports：SSA 按 (containerPort, protocol) 合并，重复项会导致 apiserver 报错（如 HTTP 500）
        meta = resource.get("metadata") or {}
        log_ctx = "恢复 {} / {}（{}）".format(
            resource.get("kind") or "?",
            meta.get("name") or "?",
            meta.get("namespace") or "cluster-scoped",
        )
        for pod_spec in self._iter_pod_specs(resource):
            ResourceCleaner.dedupe_container_ports_in_pod_spec(pod_spec, log_ctx=log_ctx)

        return resource

    def _apply_merge_patch(self, resource: Dict) -> Dict:
        """
        将 rule.merge_patch 按官方对象嵌套深度合并进当前资源根对象（与 kubectl patch -p 片段一致）。

        经 CLI 校验时 merge_patch 与 merge_patch_kinds 同时存在；若 kinds 为空则跳过合并（防御性）。
        """
        patch = self.rule.merge_patch
        if not patch:
            return resource
        kinds = self.rule.merge_patch_kinds
        if not kinds:
            logger.warning(
                "merge_patch 已配置但 merge_patch_kinds 为空，已跳过合并；请使用 --merge-patch-kind 指定资源类型"
            )
            return resource
        k = resource.get("kind") or ""
        if k not in kinds:
            return resource
        deep_merge_k8s_dict(resource, patch)
        return resource

    def _transform_namespace(self, resource: Dict) -> Dict:
        """
        命名空间映射：改写命名空间名字段及各类引用。

        Namespace 对象为集群作用域，名称在 metadata.name（非 metadata.namespace）。
        若不改写，跨集群恢复时仍会得到旧名 Namespace，与其它已映射资源不一致。
        参考: https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/
        """
        metadata = resource.get('metadata', {})
        kind = (resource.get('kind') or '').lower()

        if kind == 'namespace':
            res_name = metadata.get('name')
            if res_name and res_name in self.rule.namespace_mapping:
                metadata['name'] = self.rule.transform_namespace(res_name)
                resource['metadata'] = metadata
        else:
            current_ns = metadata.get('namespace')
            if current_ns and current_ns in self.rule.namespace_mapping:
                new_ns = self.rule.transform_namespace(current_ns)
                metadata['namespace'] = new_ns
                resource['metadata'] = metadata

        self._transform_namespace_references(resource)

        return resource

    def _apply_namespace_to_metadata(self, meta: Optional[Dict]) -> None:
        """
        若 metadata 中存在 namespace 且在映射表中，则原地替换为映射后的命名空间。
        用于 Pod template、Ingress backend 等嵌套的 namespace 字段，保证与顶层一致。
        """
        if not meta or not self.rule.namespace_mapping:
            return
        ns = meta.get('namespace')
        if ns and ns in self.rule.namespace_mapping:
            meta['namespace'] = self.rule.transform_namespace(ns)

    def _transform_pod_template_namespaces(self, resource: Dict) -> None:
        """
        转换工作负载资源中 Pod 模板的 spec.template.metadata.namespace。
        Kubernetes 官方：若设置了 Pod template 的 namespace，必须与资源所属 namespace 一致。
        参考: PodTemplateSpec (ObjectMeta.namespace), Deployment/StatefulSet/DaemonSet/Job/CronJob API。
        """
        kind = resource.get('kind', '').lower()
        spec = resource.get('spec', {}) or {}

        if kind in ('deployment', 'statefulset', 'daemonset', 'replicaset'):
            template = spec.get('template', {})
            if isinstance(template, dict) and 'metadata' in template:
                self._apply_namespace_to_metadata(template['metadata'])
        elif kind == 'job':
            template = spec.get('template', {})
            if isinstance(template, dict) and 'metadata' in template:
                self._apply_namespace_to_metadata(template['metadata'])
        elif kind == 'cronjob':
            job_template = spec.get('jobTemplate', {}) or {}
            jspec = job_template.get('spec', {}) or {}
            template = jspec.get('template', {})
            if isinstance(template, dict) and 'metadata' in template:
                self._apply_namespace_to_metadata(template['metadata'])

    def _transform_ingress_backend_namespaces(self, resource: Dict) -> None:
        """
        转换 Ingress 中 backend.service.namespace（networking.k8s.io/v1）。
        跨命名空间引用时需与命名空间映射保持一致。
        """
        spec = resource.get('spec', {}) or {}
        default_backend = spec.get('defaultBackend', {}) or {}
        if isinstance(default_backend.get('service'), dict):
            self._apply_namespace_to_metadata(default_backend['service'])
        for rule in spec.get('rules', []) or []:
            http = rule.get('http', {}) or {}
            for path in http.get('paths', []) or []:
                backend = path.get('backend', {}) or {}
                if isinstance(backend.get('service'), dict):
                    self._apply_namespace_to_metadata(backend['service'])

    def _transform_namespace_references(self, resource: Dict):
        """
        按资源类型转换所有嵌套的 namespace 引用，保证与 metadata.namespace 映射一致。
        审计范围（符合 Kubernetes 官方 API）：
        - 顶层 metadata.namespace：在 _transform_namespace 中已处理
        - Deployment/StatefulSet/DaemonSet/ReplicaSet：spec.template.metadata.namespace
        - Job：spec.template.metadata.namespace
        - CronJob：spec.jobTemplate.spec.template.metadata.namespace
        - RoleBinding/ClusterRoleBinding：subjects[].namespace（ServiceAccount 等）
        - NetworkPolicy：ingress/egress 中 namespaceSelector.matchExpressions 的 values
        - Ingress (networking.k8s.io/v1)：spec.defaultBackend.service.namespace、rules[].http.paths[].backend.service.namespace
        """
        kind = resource.get('kind', '').lower()

        # 工作负载：Pod 模板中的 namespace 必须与资源 namespace 一致（Kubernetes 要求）
        self._transform_pod_template_namespaces(resource)

        if kind in ('rolebinding', 'clusterrolebinding'):
            subjects = resource.get('subjects', [])
            for subject in subjects:
                if subject.get('kind') in ['ServiceAccount', 'User', 'Group']:
                    ns = subject.get('namespace')
                    if ns and ns in self.rule.namespace_mapping:
                        subject['namespace'] = self.rule.transform_namespace(ns)

        elif kind == 'networkpolicy':
            # Handle ingress/egress namespace selectors
            self._transform_network_policy_namespaces(resource)

        elif kind == 'ingress':
            self._transform_ingress_backend_namespaces(resource)

    def _transform_network_policy_namespaces(self, resource: Dict):
        """Transform namespace selectors in network policies"""
        spec = resource.get('spec', {})

        # Ingress rules
        for ingress in spec.get('ingress', []):
            for from_rule in ingress.get('from', []):
                if 'namespaceSelector' in from_rule:
                    self._transform_namespace_selector(from_rule['namespaceSelector'])

        # Egress rules
        for egress in spec.get('egress', []):
            for to_rule in egress.get('to', []):
                if 'namespaceSelector' in to_rule:
                    self._transform_namespace_selector(to_rule['namespaceSelector'])

    def _transform_namespace_selector(self, selector: Dict):
        """
        转换 NetworkPolicy 等资源中的 namespaceSelector。

        官方推荐用标签选择命名空间；按名称筛选时常用标签键
        kubernetes.io/metadata.name（值为命名空间名称），与 matchExpressions 等价形式均须改写，
        否则命名空间映射后 NetworkPolicy 仍指向旧名，生产上会直接导致流量错误。
        参考: https://kubernetes.io/docs/concepts/services-networking/network-policies/
        """
        if not self.rule.namespace_mapping:
            return
        if 'matchLabels' in selector and isinstance(selector.get('matchLabels'), dict):
            labels = selector['matchLabels']
            for key in ('kubernetes.io/metadata.name', 'name'):
                val = labels.get(key)
                if isinstance(val, str) and val in self.rule.namespace_mapping:
                    labels[key] = self.rule.transform_namespace(val)
        if 'matchExpressions' in selector:
            for expr in selector['matchExpressions']:
                if expr.get('key') == 'kubernetes.io/metadata.name':
                    values = expr.get('values', [])
                    if not isinstance(values, list):
                        continue
                    new_values = [
                        self.rule.transform_namespace(v) if isinstance(v, str) else v
                        for v in values
                    ]
                    expr['values'] = new_values

    @staticmethod
    def _iter_pod_specs(resource: Dict) -> List[Dict]:
        """
        收集资源中参与容器镜像/环境变量转换的 PodSpec 字典（原地修改）。

        覆盖与 Kubernetes API 一致的路径：
        - Pod: spec 即为 PodSpec
        - CronJob: spec.jobTemplate.spec.template.spec
        - Deployment/ReplicaSet/StatefulSet/DaemonSet/Job/ReplicationController 等: spec.template.spec
        """
        out: List[Dict] = []
        if not isinstance(resource, dict):
            return out
        kind = (resource.get('kind') or '').lower()
        rspec = resource.get('spec')
        if not isinstance(rspec, dict):
            return out

        if kind == 'pod':
            out.append(rspec)
            return out

        if kind == 'cronjob':
            jt = rspec.get('jobTemplate') or {}
            jspec = jt.get('spec') or {}
            tmpl = jspec.get('template') or {}
            ps = tmpl.get('spec')
            if isinstance(ps, dict):
                out.append(ps)
            return out

        tmpl = rspec.get('template') or {}
        ps = tmpl.get('spec')
        if isinstance(ps, dict):
            out.append(ps)
        return out

    def _transform_container_images(self, resource: Dict) -> Dict:
        """按 image_mapping 转换 PodSpec 内所有容器的 image 字段。"""
        if not self.rule.image_mapping:
            return resource

        for pod_spec in self._iter_pod_specs(resource):
            for ckey in ('initContainers', 'containers', 'ephemeralContainers'):
                for container in pod_spec.get(ckey) or []:
                    if isinstance(container, dict) and container.get('image') is not None:
                        container['image'] = self.rule.transform_image(container['image'])

        return resource

    @staticmethod
    def _apply_env_mapping_value_to_entry(env_var: Dict, new_value: str) -> None:
        """将 env_mapping 的目标值写入 env 项：普通字符串写入 env.value；@k8s: 前缀则写入 valueFrom.fieldRef。"""
        env_var.pop("value", None)
        env_var.pop("valueFrom", None)
        fp = parse_env_fieldref_from_mapping_value(new_value)
        if fp is not None:
            env_var["valueFrom"] = {"fieldRef": {"fieldPath": fp}}
        else:
            env_var["value"] = new_value

    def _substitute_namespace_in_env_plain_values(self, resource: Dict) -> Dict:
        """
        在 namespace_mapping 非空且 env_namespace_substitute 不为 off 时执行；
        且须在 _transform_env_variables 之后调用（先应用 --env-mapping 与 @k8s:，再处理剩余项）。

        仅处理仍使用 env.value 字段、且值为内联字符串的条目；已使用 valueFrom 的条目跳过不改。
        典型：将 DOMAIN_NAME 的 value 从 应用名.源命名空间 改为 应用名.目标命名空间（auto 模式）。
        """
        mode = self.rule.env_namespace_substitute
        if mode == "off" or not self.rule.namespace_mapping:
            return resource

        for pod_spec in self._iter_pod_specs(resource):
            for ckey in ("initContainers", "containers", "ephemeralContainers"):
                for container in pod_spec.get(ckey) or []:
                    if not isinstance(container, dict):
                        continue
                    for env_var in container.get("env") or []:
                        if not isinstance(env_var, dict):
                            continue
                        if "valueFrom" in env_var or "value" not in env_var:
                            continue
                        val = env_var.get("value")
                        if not isinstance(val, str):
                            continue
                        new_val = TransformationRule.substitute_namespace_in_plain_string(
                            val,
                            self.rule.namespace_mapping,
                            mode,
                        )
                        if new_val != val:
                            env_var["value"] = new_val
        return resource

    def _transform_env_variables(self, resource: Dict) -> Dict:
        """
        转换容器中的环境变量。
        
        根据 env_mapping 规则（CLI 统一 KEY=值）：
        1. 映射中存在该 key：替换其值（改）；值为 @k8s:fieldPath 时改为 Downward API（valueFrom.fieldRef）
        2. 映射值为空字符串：删除该环境变量（删）
        3. 原资源中不存在该 key：新增该环境变量（增）
        
        Args:
            resource: Kubernetes 资源字典
            
        Returns:
            Dict: 转换后的资源字典
        """
        if not self.rule.env_mapping:
            return resource

        for pod_spec in self._iter_pod_specs(resource):
            containers = (
                list(pod_spec.get('containers') or [])
                + list(pod_spec.get('initContainers') or [])
                + list(pod_spec.get('ephemeralContainers') or [])
            )

            for container in containers:
                if not isinstance(container, dict):
                    continue
                env_vars = container.get('env', [])
                if not env_vars:
                    env_vars = []
                    container['env'] = env_vars

                env_vars_to_remove = []
                for env_var in env_vars:
                    env_name = env_var.get('name', '')
                    if not env_name:
                        continue
                    if env_name in self.rule.env_mapping:
                        new_value = self.rule.env_mapping[env_name]
                        if new_value == "":
                            env_vars_to_remove.append(env_var)
                            continue
                        self._apply_env_mapping_value_to_entry(env_var, new_value)

                for env_var in env_vars_to_remove:
                    env_vars.remove(env_var)

                existing_env_names = {env_var.get('name', '') for env_var in env_vars}
                for env_key, env_value in self.rule.env_mapping.items():
                    if env_value == "":
                        continue
                    if env_key not in existing_env_names:
                        entry: Dict[str, Any] = {"name": env_key}
                        self._apply_env_mapping_value_to_entry(entry, env_value)
                        env_vars.append(entry)

        return resource


# -------------------------
# Kubernetes Client Wrapper (保持原样)
# -------------------------
class KubernetesClient:
    """Wrapper for Kubernetes client with enhanced error handling"""

    def __init__(self, config_path: Optional[str] = None, context: Optional[str] = None):
        try:
            if config_path:
                config.load_kube_config(config_file=config_path, context=context)
            else:
                try:
                    config.load_incluster_config()
                except (IOError, FileNotFoundError, config.ConfigException):
                    config.load_kube_config()

            self.api_client = client.ApiClient()
            self.dynamic_client = DynamicClient(self.api_client)
            self.core_v1 = client.CoreV1Api(self.api_client)
            self.apps_v1 = client.AppsV1Api(self.api_client)
            self.batch_v1 = client.BatchV1Api(self.api_client)
            self.version_api = client.VersionApi(self.api_client)

        except (IOError, FileNotFoundError, config.ConfigException, AttributeError) as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            raise

    @retry_decorator
    def list_namespaces(self) -> List[str]:
        """List all namespaces in the cluster"""
        try:
            namespaces = self.core_v1.list_namespace()
            return [ns.metadata.name for ns in namespaces.items]
        except ApiException as e:
            logger.error(f"Failed to list namespaces: {e}")
            raise

    @retry_decorator
    def get_resource(self, api_version: str, kind: str):
        """Get dynamic resource object"""
        try:
            return self.dynamic_client.resources.get(api_version=api_version, kind=kind)
        except ResourceNotFoundError:
            logger.warning(f"Resource not found: {api_version}/{kind}")
            raise

    @retry_decorator
    def list_resources(self, resource_type: str, namespace: Optional[str] = None,
                       label_selector: Optional[str] = None, field_selector: Optional[str] = None) -> List[Dict]:
        """
        列出资源，使用改进的发现机制。
        根据 Kubernetes 最佳实践，使用完全限定的 API 版本。
        
        Args:
            resource_type: 资源类型（如 'deployments', 'services'）
            namespace: 命名空间（None 表示集群级资源）
            label_selector: 标签选择器
            field_selector: 字段选择器
            
        Returns:
            List[Dict]: 资源列表
        """
        candidates = RESOURCE_API_MAPPING.get(resource_type, [])

        for api_version, kind in candidates:
            try:
                resource = self.get_resource(api_version, kind)
                if namespace and getattr(resource, "namespaced", False):
                    items = resource.get(
                        namespace=namespace,
                        label_selector=label_selector,
                        field_selector=field_selector
                    )
                else:
                    items = resource.get(
                        label_selector=label_selector,
                        field_selector=field_selector
                    )
                if hasattr(items, "to_dict"):
                    items_dict = items.to_dict()
                elif isinstance(items, dict):
                    items_dict = items
                else:
                    try:
                        items_dict = dict(items)
                    except (TypeError, ValueError, AttributeError):
                        items_dict = {}
                return items_dict.get('items', [])
            except (ResourceNotFoundError, ApiException):
                continue

        try:
            for res in self.dynamic_client.resources:
                try:
                    plural = getattr(res, "plural", None)
                    resource_kind = getattr(res, "kind", None)
                    namespaced = getattr(res, "namespaced", False)
                    if plural == resource_type or (
                            resource_kind and resource_kind.lower() == resource_type.rstrip("s").lower()):
                        if namespace and namespaced:
                            items = res.get(
                                namespace=namespace,
                                label_selector=label_selector,
                                field_selector=field_selector
                            )
                        else:
                            items = res.get(
                                label_selector=label_selector,
                                field_selector=field_selector
                            )
                        if hasattr(items, "to_dict"):
                            return items.to_dict().get('items', [])
                        elif isinstance(items, dict):
                            return items.get('items', [])
                except (ApiException, ResourceNotFoundError, AttributeError, KeyError) as e:
                    logger.error(f"Failed to get resource: {e}")
                    continue
        except (AttributeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to discover resource {resource_type}: {e}")

        return []

    def is_cluster_scoped(self, resource_type: str) -> bool:
        """
        检查资源是否为集群级资源。
        
        Args:
            resource_type: 资源类型名称
            
        Returns:
            bool: 如果是集群级资源返回 True，否则返回 False
        """
        return resource_type in CLUSTER_SCOPED_RESOURCES

    @retry_decorator
    def apply_resource(self, resource: Dict, field_manager: str = "k8s-backup") -> bool:
        """
        使用 server-side apply 应用资源（符合 Kubernetes 最佳实践）。
        这是幂等的，可以优雅地处理创建和更新场景。
        
        参考: https://kubernetes.io/docs/reference/using-api/server-side-apply/
        
        Args:
            resource: 要应用的 Kubernetes 资源字典
            field_manager: 字段管理器名称（用于标识资源的所有者）
            
        Returns:
            bool: 操作是否成功
            
        Raises:
            ValueError: 资源结构无效
            ResourceNotFoundError: 资源类型在集群中不存在
            ApiException: Kubernetes API 调用失败
        """
        api_version = resource.get('apiVersion', 'v1')
        kind = resource.get('kind', '')
        metadata = resource.get('metadata', {})
        namespace = metadata.get('namespace') if metadata else None
        name = metadata.get('name', '') if metadata else ''

        try:
            if not kind:
                raise ValueError("资源缺少 'kind' 字段")
            if not metadata:
                raise ValueError("资源缺少 'metadata' 字段")
            
            if not name:
                raise ValueError("资源缺少 'metadata.name' 字段")
            
            resource_obj = self.get_resource(api_version, kind)
            
            # 确保集群特定字段被移除（恢复最佳实践）
            # 这些字段引用集群特定的资源，不应被恢复
            if 'finalizers' in metadata and metadata.get('finalizers'):
                metadata.pop('finalizers', None)
                logger.debug(f"已移除 {kind}/{name} 的 finalizers")
            
            # ownerReferences 引用集群特定的父资源，不应被恢复
            if 'ownerReferences' in metadata and metadata.get('ownerReferences'):
                metadata.pop('ownerReferences', None)
                logger.debug(f"已移除 {kind}/{name} 的 ownerReferences")
            
            # 使用 server-side apply 进行幂等资源管理
            # 这可以优雅地处理创建和更新场景
            # 根据 Kubernetes 最佳实践，使用 server-side apply 而不是 create/update
            try:
                if namespace and resource_obj.namespaced:
                    # 命名空间资源
                    resource_obj.server_side_apply(
                        body=resource,
                        namespace=namespace,
                        field_manager=field_manager,
                        force=True  # 强制解决冲突（恢复场景必需）
                    )
                else:
                    # 集群级资源
                    resource_obj.server_side_apply(
                        body=resource,
                        field_manager=field_manager,
                        force=True
                    )
                return True
            except AttributeError:
                # 如果 server_side_apply 方法不存在，回退到 patch 方法
                # 某些旧版本的 kubernetes-client 可能不支持 server_side_apply
                logger.warning(f"server_side_apply 不可用，使用 patch 方法作为回退")
                if namespace and resource_obj.namespaced:
                    resource_obj.patch(
                        body=resource,
                        namespace=namespace,
                        content_type='application/apply-patch+yaml',
                        field_manager=field_manager,
                        force=True
                    )
                else:
                    resource_obj.patch(
                        body=resource,
                        content_type='application/apply-patch+yaml',
                        field_manager=field_manager,
                        force=True
                    )
                return True
                
        except ResourceNotFoundError as e:
            logger.error(f"资源类型 {api_version}/{kind} 在集群中不存在: {e}")
            raise
        except ApiException as e:
            logger.error(f"应用资源 {kind}/{name} 失败: HTTP {e.status} - {e}")
            raise
        except (ValueError, AttributeError, KeyError) as e:
            logger.error(f"资源结构无效: {e}")
            raise
        except Exception as e:
            logger.error(f"应用资源 {kind} 时发生意外错误: {e}")
            raise

    @retry_decorator
    def apply_namespace(self, namespace_name: str) -> bool:
        """
        使用 server-side apply 应用命名空间，实现幂等创建。
        这比 create 更安全，因为它可以优雅地处理已存在的命名空间。
        
        参考: Kubernetes 幂等资源创建最佳实践
        
        Args:
            namespace_name: 命名空间名称
            
        Returns:
            bool: 操作是否成功
            
        Raises:
            ApiException: Kubernetes API 调用失败
        """
        try:
            namespace_manifest = {
                'apiVersion': 'v1',
                'kind': 'Namespace',
                'metadata': {
                    'name': namespace_name
                }
            }
            
            # 使用 server-side apply 创建命名空间（幂等）
            resource_obj = self.get_resource('v1', 'Namespace')
            try:
                resource_obj.server_side_apply(
                    body=namespace_manifest,
                    field_manager='k8s-backup',
                    force=True
                )
            except AttributeError:
                # 回退到 patch 方法
                resource_obj.patch(
                    body=namespace_manifest,
                    content_type='application/apply-patch+yaml',
                    field_manager='k8s-backup',
                    force=True
                )
            return True
        except ApiException as e:
            if e.status == 409:
                # 已存在，这对于 apply 语义来说是正常的
                logger.debug(f"命名空间 {namespace_name} 已存在（apply 语义预期行为）")
                return True
            logger.error(f"应用命名空间 {namespace_name} 失败: {e}")
            raise
        except Exception as e:
            logger.error(f"应用命名空间时发生意外错误: {e}")
            raise


# -------------------------
# Backup Manager (增强)
# -------------------------
class KubernetesBackupManager:
    """Main backup management class"""

    def __init__(self, backup_config: BackupConfig):
        self.config = backup_config
        self.k8s_client = KubernetesClient(backup_config.kubeconfig, backup_config.context)
        self.cleaner = ResourceCleaner()
        self.backup_stats = {
            'total_resources': 0,
            'successful_backups': 0,
            'failed_backups': 0,
            'start_time': time.time(),
            'end_time': None
        }

    def get_output_path(self, resource: Dict, base_dir: str) -> str:
        """Generate output path for resource"""
        kind = resource.get('kind', 'Unknown')
        namespace = resource.get('metadata', {}).get('namespace', 'cluster-scoped')
        name = resource.get('metadata', {}).get('name', 'unnamed')

        safe_kind = sanitize_filename(kind)
        safe_namespace = sanitize_filename(namespace)
        safe_name = sanitize_filename(name)

        filename = f"{safe_kind}-{safe_name}.yaml"
        return os.path.join(base_dir, safe_namespace, safe_kind, filename)

    def backup_resource(self, resource: Dict, output_dir: str) -> bool:
        """
        备份单个资源。
        
        Args:
            resource: 要备份的 Kubernetes 资源字典
            output_dir: 输出目录路径
            
        Returns:
            bool: 备份是否成功
        """
        try:
            cleaned_resource = self.cleaner.clean_resource(resource)
            kind = cleaned_resource.get('kind')
            api_version = cleaned_resource.get('apiVersion')
            if not kind or not isinstance(kind, str):
                logger.error(
                    "拒绝备份：对象缺少合法 metadata/kind（清理后仍无效，可能来自损坏的 Informer 数据）"
                )
                return False
            if not api_version or not isinstance(api_version, str):
                logger.error(f"拒绝备份 {kind}：缺少 apiVersion，不符合 Kubernetes 对象约定")
                return False
            output_path = self.get_output_path(cleaned_resource, output_dir)
            write_yaml_safely(cleaned_resource, output_path, dry_run=self.config.dry_run)
            if not self.config.dry_run:
                name = cleaned_resource.get('metadata', {}).get('name', 'unknown')
                logger.info(f"已备份资源 {kind}/{name} 到 {output_path}")
            return True
        except Exception as e:
            resource_name = resource.get('metadata', {}).get('name', 'unknown')
            kind = resource.get('kind', 'Unknown')
            logger.error(f"备份资源 {kind}/{resource_name} 失败: {e}")
            return False

    def _get_resource_info(self, resource: Dict) -> Tuple[str, str, str]:
        """
        获取资源的标识信息，用于日志输出。
        
        Args:
            resource: Kubernetes 资源字典
            
        Returns:
            Tuple[str, str, str]: (kind, name, namespace_info)
        """
        if not isinstance(resource, dict):
            return 'Unknown', 'unknown', '未知'
        
        kind = resource.get('kind', 'Unknown')
        metadata = resource.get('metadata', {})
        name = metadata.get('name', 'unknown')
        namespace = metadata.get('namespace', 'cluster-scoped')
        ns_info = f"命名空间 {namespace}" if namespace != 'cluster-scoped' else "集群级"
        
        return kind, name, ns_info

    def _normalize_listed_resources(self, resources: List) -> List[Dict]:
        """将 API List 条目统一为 dict。"""
        normalized: List[Dict] = []
        for itm in resources:
            if hasattr(itm, 'to_dict'):
                normalized.append(itm.to_dict())
            elif isinstance(itm, dict):
                normalized.append(itm)
            else:
                try:
                    normalized.append(dict(itm))
                except (TypeError, ValueError, AttributeError):
                    continue
        return normalized

    def _apply_include_names_filter(
        self, items: List[Dict], resource_type: str, namespace: Optional[str]
    ) -> List[Dict]:
        """按 --include-names 过滤；未配置时原样返回。"""
        if not self.config.include_names:
            return items
        before = len(items)
        filtered = filter_resources_by_include_names(items, self.config.include_names)
        scope = namespace or 'cluster'
        if before != len(filtered):
            logger.info(
                f"{resource_type} ({scope}): --include-names 过滤，列举 {before} 个，保留 {len(filtered)} 个"
            )
        elif before > 0 and len(filtered) == 0:
            logger.info(
                f"{resource_type} ({scope}): 列举 {before} 个，无对象名匹配 --include-names，跳过写盘"
            )
        return filtered

    def backup_resources_parallel(self, resources: List[Dict], output_dir: str) -> Tuple[int, int]:
        """
        并行备份资源。
        
        Args:
            resources: 要备份的资源列表
            output_dir: 输出目录路径
            
        Returns:
            Tuple[int, int]: (成功数量, 失败数量)
        """
        successful = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            future_to_resource = {
                executor.submit(self.backup_resource, resource, output_dir): resource
                for resource in resources
            }

            for future in as_completed(future_to_resource):
                resource = future_to_resource[future]
                try:
                    if future.result():
                        successful += 1
                    else:
                        # 备份失败，记录资源信息
                        kind, name, ns_info = self._get_resource_info(resource)
                        logger.warning(f"备份资源 {kind}/{name} ({ns_info}) 失败")
                        failed += 1
                except (IOError, yaml.YAMLError, KeyError, AttributeError) as e:
                    # 异常处理时记录详细的资源信息
                    kind, name, ns_info = self._get_resource_info(resource)
                    logger.error(f"备份资源 {kind}/{name} ({ns_info}) 时发生异常: {e}")
                    failed += 1

        return successful, failed

    def execute_backup(self) -> bool:
        """
        执行备份过程。
        
        Returns:
            bool: 备份是否完全成功（无失败）
        """
        try:
            output_dir = os.path.join(self.config.output_dir, self.config.backup_name)
            logger.info(f"开始备份到目录: {output_dir}")
            if self.config.include_names:
                names_preview = ", ".join(sorted(self.config.include_names))
                logger.info(
                    f"已启用 --include-names（metadata.name 精确匹配，共 "
                    f"{len(self.config.include_names)} 个）: {names_preview}"
                )
            if not self.config.dry_run:
                ensure_directory(output_dir)
            else:
                logger.info("[模拟运行] 不创建备份目录、不写 YAML / 元数据 / tar")

            # 保存备份元数据
            metadata = {
                'backup_name': self.config.backup_name,
                'timestamp': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                'namespace': self.config.namespace,
                'resources': self.config.resources,
                'include_crds': self.config.include_crds,
                'cluster_info': self._get_cluster_info()
            }
            if self.config.label_selector:
                metadata['label_selector'] = self.config.label_selector
            if self.config.field_selector:
                metadata['field_selector'] = self.config.field_selector
            if self.config.include_names:
                metadata['include_names'] = sorted(self.config.include_names)

            if not self.config.dry_run:
                save_backup_metadata(output_dir, metadata)
                logger.info(f"备份元数据已保存: {self.config.backup_name}")

            # 构建命名空间列表
            if self.config.namespace == 'all':
                try:
                    namespaces = self.k8s_client.list_namespaces()
                    logger.info(f"发现 {len(namespaces)} 个命名空间")
                except (ApiException, ConnectionError, TimeoutError) as e:
                    logger.error(f"列出命名空间失败: {e}")
                    return False
            else:
                namespaces = [self.config.namespace] if self.config.namespace else []
                logger.info(f"备份命名空间: {self.config.namespace}")

            # 先备份集群级资源
            cluster_resources = [
                r for r in self.config.resources
                if self.k8s_client.is_cluster_scoped(r)
            ]

            if cluster_resources:
                logger.info(f"开始备份集群级资源: {', '.join(cluster_resources)}")
            for resource_type in cluster_resources:
                try:
                    self.backup_resource_type(resource_type, None, output_dir)
                except (ApiException, ResourceNotFoundError, IOError, yaml.YAMLError) as e:
                    logger.error(f"备份集群资源 {resource_type} 时出错: {e}")
                    self.backup_stats['failed_backups'] += 1

            # 备份命名空间资源
            namespaced_resources = [
                r for r in self.config.resources
                if not self.k8s_client.is_cluster_scoped(r)
            ]
            
            if namespaced_resources:
                logger.info(f"开始备份命名空间资源: {', '.join(namespaced_resources)}")
            for namespace in namespaces:
                    logger.info(f"备份命名空间 {namespace} 中的资源...")
                    for resource_type in namespaced_resources:
                        try:
                            self.backup_resource_type(resource_type, namespace, output_dir)
                        except (ApiException, ResourceNotFoundError, IOError, yaml.YAMLError) as e:
                            logger.error(f"备份命名空间 {namespace} 中的 {resource_type} 时出错: {e}")
                            self.backup_stats['failed_backups'] += 1

            # 如果请求，备份 CRD
            if self.config.include_crds:
                logger.info("开始备份自定义资源定义（CRD）...")
                self.backup_crds(output_dir)

            # 如果请求，创建压缩包（非 dry-run）
            if self.config.create_tarball and not self.config.dry_run:
                logger.info("创建备份压缩包...")
                self.create_backup_archive(output_dir)

            # 验证备份（非 dry-run）
            if not self.config.dry_run:
                logger.info("验证备份文件...")
                self.validate_backup(output_dir)

            self.backup_stats['end_time'] = time.time()
            self.log_backup_summary()

            return self.backup_stats['failed_backups'] == 0

        except (IOError, yaml.YAMLError, ApiException, KeyError, AttributeError) as e:
            logger.error(f"备份过程失败: {e}")
            return False

    def backup_resource_type(self, resource_type: str, namespace: Optional[str], output_dir: str):
        """Backup all resources of specific type"""
        try:
            resources = self.k8s_client.list_resources(
                resource_type,
                namespace,
                self.config.label_selector,
                self.config.field_selector
            )
            if not resources:
                logger.debug(f"No resources found for {resource_type} in {namespace or 'cluster'}")
                return

            normalized = self._normalize_listed_resources(resources)
            normalized = self._apply_include_names_filter(
                normalized, resource_type, namespace
            )
            if not normalized:
                return

            successful, failed = self.backup_resources_parallel(normalized, output_dir)
            self.backup_stats['total_resources'] += len(normalized)
            self.backup_stats['successful_backups'] += successful
            self.backup_stats['failed_backups'] += failed

            logger.info(
                f"Backed up {resource_type}: {successful} successful, {failed} failed"
            )

        except (ApiException, ResourceNotFoundError, IOError, yaml.YAMLError, AttributeError) as e:
            logger.error(f"Failed to backup {resource_type}: {e}")
            self.backup_stats['failed_backups'] += 1

    def backup_crds(self, output_dir: str):
        """Backup Custom Resource Definitions and their instances"""
        try:
            crds = self.k8s_client.list_resources('customresourcedefinitions', None)
            crd_resources = []
            for crd in crds:
                if hasattr(crd, 'to_dict'):
                    crd = crd.to_dict()
                crd_resources.append(crd)

            crd_resources = self._apply_include_names_filter(
                crd_resources, 'customresourcedefinitions', None
            )
            if crd_resources:
                successful, failed = self.backup_resources_parallel(crd_resources, output_dir)
                self.backup_stats['total_resources'] += len(crd_resources)
                self.backup_stats['successful_backups'] += successful
                self.backup_stats['failed_backups'] += failed
                logger.info(f"Backed up CRD definitions: {successful} successful, {failed} failed")

            for crd in crd_resources:
                crd_kind = None
                try:
                    spec = crd.get("spec", {}) or {}
                    names = spec.get("names", {}) or {}
                    plural = names.get("plural")
                    group = spec.get("group")
                    versions = spec.get("versions") or []
                    crd_kind = names.get("kind")

                    if not (plural and group and versions and crd_kind):
                        continue

                    # 根据 Kubernetes 最佳实践，优先使用 storageVersion
                    # 如果没有 storageVersion，则使用第一个 served 版本
                    storage_version = None
                    served_versions = []
                    
                    for v in versions:
                        if v.get("storage", False):
                            storage_version = v
                            break
                        if v.get("served", False):
                            served_versions.append(v)
                    
                    # 优先使用 storageVersion，否则使用第一个 served 版本
                    if storage_version:
                        api_version = f"{group}/{storage_version.get('name')}"
                    elif served_versions:
                        api_version = f"{group}/{served_versions[0].get('name')}"
                    else:
                        # 如果没有 served 版本，跳过此 CRD
                        logger.warning(f"CRD {crd_kind} 没有可用的 served 版本")
                        continue

                    resource = self.k8s_client.dynamic_client.resources.get(
                        api_version=api_version,
                        kind=crd_kind
                    )

                    scope = spec.get("scope", "Namespaced")
                    if scope == "Cluster":
                        items = resource.get().to_dict().get('items', [])
                    else:
                        items = []
                        for namespace in self.k8s_client.list_namespaces():
                            try:
                                ns_items = resource.get(namespace=namespace).to_dict().get('items', [])
                                items.extend(ns_items)
                            except (ApiException, ResourceNotFoundError, AttributeError, KeyError) as e:
                                logger.warning(f"Failed to get {crd_kind} in namespace {namespace}: {e}")
                                continue

                    if items:
                        normalized_items = self._normalize_listed_resources(items)
                        # Namespaced CR 实例可能来自多个命名空间，勿用 for 循环末次的 namespace
                        cr_filter_scope = None if scope == "Cluster" else "all-namespaces"
                        normalized_items = self._apply_include_names_filter(
                            normalized_items,
                            crd_kind or 'customresource',
                            cr_filter_scope,
                        )
                        if not normalized_items:
                            continue

                        successful, failed = self.backup_resources_parallel(normalized_items, output_dir)
                        self.backup_stats['total_resources'] += len(normalized_items)
                        self.backup_stats['successful_backups'] += successful
                        self.backup_stats['failed_backups'] += failed
                        logger.info(f"Backed up {crd_kind} instances: {successful} successful, {failed} failed")

                except (ApiException, ResourceNotFoundError, KeyError, AttributeError, TypeError) as e:
                    logger.warning(f"Failed to backup CR instances for {crd_kind or 'unknown'}: {e}")
                    continue

        except (ApiException, ResourceNotFoundError, IOError, yaml.YAMLError) as e:
            logger.error(f"Failed to backup CRDs: {e}")

    def create_backup_archive(self, output_dir: str):
        """Create compressed archive of backup directory"""
        try:
            archive_path = f"{output_dir}.tar.gz"
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(output_dir, arcname=os.path.basename(output_dir))

            logger.info(f"Created backup archive: {archive_path}")

        except (IOError, OSError, tarfile.TarError) as e:
            logger.error(f"Failed to create backup archive: {e}")

    def validate_backup(self, output_dir: str) -> bool:
        """Validate backup files for correctness"""
        success = True
        try:
            yaml_files = list(Path(output_dir).rglob("*.yaml"))
            if not yaml_files:
                logger.warning("No YAML files found in backup directory")
                return False

            for yaml_file in yaml_files:
                try:
                    docs = load_kubernetes_yaml_documents(str(yaml_file))
                    if not docs:
                        logger.warning(f"{yaml_file} 中无有效 Kubernetes 文档")
                        success = False
                        continue
                    for content in docs:
                        if not content or not all(
                            key in content for key in ('apiVersion', 'kind', 'metadata')
                        ):
                            logger.warning(f"{yaml_file} 中存在无效或空的资源对象")
                            success = False
                            continue
                        meta = content.get('metadata') or {}
                        if 'name' not in meta:
                            logger.warning(f"{yaml_file} 中资源缺少 metadata.name")
                            success = False
                            continue
                except yaml.YAMLError as e:
                    logger.error(f"Invalid YAML in {yaml_file}: {e}")
                    success = False
                except (IOError, KeyError, AttributeError, TypeError) as e:
                    logger.error(f"Failed to validate {yaml_file}: {e}")
                    success = False

            if success:
                logger.info("Backup validation completed successfully")
            else:
                logger.warning("Backup validation completed with warnings")

        except (IOError, OSError, yaml.YAMLError) as e:
            logger.error(f"Backup validation failed: {e}")
            success = False

        return success

    def _get_cluster_info(self) -> Dict[str, Any]:
        """
        Get cluster information for metadata using VersionApi.
        
        Reference: https://kubernetes.io/docs/reference/using-api/api-concepts/#versioning
        """
        cluster_info = {'platform': 'unknown', 'version': 'unknown'}
        
        try:
            # Use VersionApi to get cluster version information
            # The method name might be get_code() or get_version() depending on client version
            version_info = None
            
            # Try get_code() first (common in kubernetes-client/python)
            if hasattr(self.k8s_client.version_api, 'get_code'):
                version_info = self.k8s_client.version_api.get_code()
            # Fallback to direct API call if method doesn't exist
            elif hasattr(self.k8s_client.api_client, 'call_api'):
                # Direct API call to /version endpoint
                response = self.k8s_client.api_client.call_api(
                    '/version', 'GET',
                    response_type='object',
                    _preload_content=True
                )
                if response and isinstance(response, tuple) and len(response) > 0:
                    version_info = response[0]
            
            if not version_info:
                logger.warning("Could not retrieve version info from cluster")
                return cluster_info
            
            # Get Kubernetes version (gitVersion)
            # Handle both snake_case and camelCase attribute names
            version = None
            if hasattr(version_info, 'git_version'):
                version = version_info.git_version
            elif hasattr(version_info, 'gitVersion'):
                version = version_info.gitVersion
            elif isinstance(version_info, dict):
                version = version_info.get('gitVersion') or version_info.get('git_version')
            
            if version:
                cluster_info['version'] = version
            else:
                logger.debug("gitVersion not found in version info")
            
            # Get platform information
            # Platform typically refers to the Kubernetes version (major.minor)
            # or can be the build platform
            platform = None
            
            # Try to get major.minor version as platform identifier
            major = None
            minor = None
            
            if hasattr(version_info, 'major'):
                major = version_info.major
            elif isinstance(version_info, dict):
                major = version_info.get('major')
            
            if hasattr(version_info, 'minor'):
                minor = version_info.minor
            elif isinstance(version_info, dict):
                minor = version_info.get('minor')
            
            if major and minor:
                platform = f"{major}.{minor}"
            
            # Fallback to platform attribute if available
            if not platform:
                if hasattr(version_info, 'platform') and version_info.platform:
                    platform = version_info.platform
                elif isinstance(version_info, dict):
                    platform = version_info.get('platform')
            
            if platform:
                cluster_info['platform'] = platform
            else:
                logger.debug("Platform info not found in version info")
            
            # Add additional useful info if available
            if major:
                cluster_info['major'] = str(major)
            if minor:
                cluster_info['minor'] = str(minor)
            
            return cluster_info
            
        except ApiException as e:
            logger.warning(f"Failed to get cluster info via API (HTTP {e.status}): {e}")
            return cluster_info
        except (AttributeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse cluster version info: {e}")
            return cluster_info
        except Exception as e:
            logger.warning(f"Failed to get cluster info: {e}")
            return cluster_info

    def log_backup_summary(self):
        """记录备份摘要统计信息"""
        duration = self.backup_stats['end_time'] - self.backup_stats['start_time'] if self.backup_stats[
            'end_time'] else 0
        total = self.backup_stats['total_resources']
        successful = self.backup_stats['successful_backups']
        failed = self.backup_stats['failed_backups']

        summary = {
            'total_resources': total,
            'successful': successful,
            'failed': failed,
            'duration_seconds': round(duration, 2),
            'success_rate': round((successful / total * 100) if total > 0 else 100, 2)
        }

        logger.info("=== 备份摘要 ===")
        logger.info(f"总资源数: {total}")
        logger.info(f"成功: {successful}")
        logger.info(f"失败: {failed}")
        logger.info(f"耗时: {round(duration, 2)} 秒")
        logger.info(f"成功率: {summary['success_rate']}%")
        logger.info("备份操作完成", **summary)


# -------------------------
# Restore Manager (新增)
# -------------------------
class KubernetesRestoreManager:
    """Manage restoration of Kubernetes resources from backup"""

    def __init__(self, restore_config: RestoreConfig):
        self.config = restore_config
        self.k8s_client = KubernetesClient(restore_config.kubeconfig, restore_config.context)
        self.transformer = ResourceTransformer(
            TransformationRule(
                namespace_mapping=restore_config.namespace_mapping,
                image_mapping=restore_config.image_mapping,
                env_mapping=restore_config.env_mapping,
                env_namespace_substitute=restore_config.env_namespace_substitute,
                merge_patch=restore_config.merge_patch,
                merge_patch_kinds=restore_config.merge_patch_kinds,
            )
        )
        self.restore_stats = {
            'total_resources': 0,
            'successful_restores': 0,
            'failed_restores': 0,
            'start_time': time.time(),
            'end_time': None
        }

    def execute_restore(self) -> bool:
        """
        执行恢复过程。
        
        Returns:
            bool: 恢复是否完全成功（无失败）
        """
        try:
            if not os.path.exists(self.config.backup_dir):
                logger.error(f"备份目录不存在: {self.config.backup_dir}")
                return False

            # 加载备份元数据
            metadata = load_backup_metadata(self.config.backup_dir)
            backup_name = metadata.get('backup_name', 'unknown')
            logger.info(f"从备份恢复: {backup_name}")
            if metadata.get('timestamp'):
                logger.info(f"备份时间: {metadata.get('timestamp')}")

            # 与 Kubernetes 依赖顺序一致：恢复必须按优先级串行 apply；并行会破坏 ConfigMap→Workload 等顺序
            logger.info(
                "恢复阶段按清单依赖顺序串行执行 server-side apply；"
                "--max-workers 仅作用于 backup 子命令，restore 传入将被忽略"
            )

            # 预创建命名空间失败则中止：继续恢复会导致大量资源 apply 到不存在的 namespace
            if self.config.create_namespaces and not self.config.dry_run:
                logger.info("检查并创建缺失的命名空间...")
                if not self._create_missing_namespaces():
                    logger.error("预创建命名空间失败，已中止恢复（避免写入错误拓扑）")
                    return False

            # 恢复集群级资源（先恢复 CRD，然后其他资源）
            # CRD 必须在任何 CustomResource 之前恢复（Kubernetes 要求）
            if not self.config.skip_cluster_scoped:
                if self.config.skip_crds:
                    # 跳过 CRD 但恢复其他集群级资源
                    logger.info("跳过 CRD（按请求），恢复其他集群级资源...")
                    cluster_scoped_dir = os.path.join(self.config.backup_dir, "cluster-scoped")
                    if os.path.exists(cluster_scoped_dir):
                        for resource_dir in sorted(os.listdir(cluster_scoped_dir)):
                            if resource_dir == "customresourcedefinition":
                                continue  # 跳过 CRD
                            resource_path = os.path.join(cluster_scoped_dir, resource_dir)
                            if os.path.isdir(resource_path):
                                logger.info(f"恢复集群级资源: {resource_dir}")
                                for yaml_file in sorted(Path(resource_path).glob("*.yaml")):
                                    self._restore_single_resource(str(yaml_file), cluster_scoped=True)
                else:
                    # 恢复所有集群级资源（CRD 优先）
                    logger.info("恢复集群级资源（CRD 优先）...")
                    self._restore_cluster_scoped_resources()

            # 恢复命名空间资源
            # 命名空间已创建，可以恢复命名空间资源
            logger.info("恢复命名空间资源...")
            self._restore_namespaced_resources()

            self.restore_stats['end_time'] = time.time()
            self._log_restore_summary()

            return self.restore_stats['failed_restores'] == 0

        except (IOError, yaml.YAMLError, ApiException, KeyError, AttributeError) as e:
            logger.error(f"恢复过程失败: {e}")
            return False

    def _create_missing_namespaces(self) -> bool:
        """
        为备份中出现的（经映射后的）命名空间执行 server-side apply 幂等创建。

        Returns:
            全部成功为 True；任一失败为 False（调用方应中止恢复）。
        """
        ok = True
        try:
            existing_namespaces = set(self.k8s_client.list_namespaces())
        except ApiException as e:
            logger.error(f"无法列举集群命名空间，跳过预创建: {e}")
            return False

        backup_namespaces = self._discover_backup_namespaces()

        for ns in backup_namespaces:
            target_ns = self.transformer.rule.transform_namespace(ns)
            if target_ns == 'cluster-scoped':
                continue
            if target_ns in existing_namespaces:
                continue
            logger.info(f"预创建命名空间: {target_ns}")
            try:
                self.k8s_client.apply_namespace(target_ns)
                existing_namespaces.add(target_ns)
            except (ApiException, ResourceNotFoundError, IOError) as e:
                logger.error(f"命名空间 {target_ns} 创建/apply 失败: {e}")
                ok = False
        return ok

    def _discover_backup_namespaces(self) -> Set[str]:
        """
        发现备份文件中引用的所有命名空间。
        
        Returns:
            Set[str]: 命名空间集合
        """
        namespaces = set()
        backup_path = Path(self.config.backup_dir)

        for yaml_file in backup_path.rglob("*.yaml"):
            try:
                for resource in load_kubernetes_yaml_documents(str(yaml_file)):
                    if resource and 'metadata' in resource:
                        ns = resource['metadata'].get('namespace')
                        if ns:
                            namespaces.add(ns)
            except (IOError, yaml.YAMLError, KeyError) as e:
                logger.warning(f"读取备份文件失败 {yaml_file}: {e}")

        return namespaces

    def _restore_cluster_scoped_resources(self):
        """
        恢复集群级资源。
        确保 CRD 在 CustomResource 之前恢复（Kubernetes 最佳实践）。
        
        参考: CRD 必须在 CR 创建之前注册。
        """
        cluster_scoped_dir = os.path.join(self.config.backup_dir, "cluster-scoped")
        if not os.path.exists(cluster_scoped_dir):
            return

        # First, restore CRDs to ensure they're registered before any CRs
        crd_dir = os.path.join(cluster_scoped_dir, "customresourcedefinition")
        if os.path.exists(crd_dir):
            logger.info("Restoring CustomResourceDefinitions first (required before CRs)")
            for yaml_file in sorted(Path(crd_dir).glob("*.yaml")):
                self._restore_single_resource(str(yaml_file), cluster_scoped=True)

        # Then restore other cluster-scoped resources
        for resource_dir in sorted(os.listdir(cluster_scoped_dir)):
            if resource_dir == "customresourcedefinition":
                continue  # Already handled above
            resource_path = os.path.join(cluster_scoped_dir, resource_dir)
            if os.path.isdir(resource_path):
                for yaml_file in sorted(Path(resource_path).glob("*.yaml")):
                    self._restore_single_resource(str(yaml_file), cluster_scoped=True)

    def _restore_namespaced_resources(self):
        """
        Restore namespaced resources in dependency order.
        Best practice: Restore base resources (ConfigMap, Secret, ServiceAccount) before
        resources that depend on them (Deployments, StatefulSets, etc.)
        """
        backup_path = Path(self.config.backup_dir)
        
        # Define restore order: base resources first, then workload resources
        # This ensures dependencies are available before dependent resources
        resource_priority = {
            'namespace': 0,
            'configmap': 1,
            'secret': 1,
            'serviceaccount': 1,
            'persistentvolumeclaim': 2,
            'service': 2,
            'role': 2,
            'networkpolicy': 3,
            'rolebinding': 3,
            'ingress': 3,
            'deployment': 4,
            'replicaset': 4,
            'daemonset': 4,
            'statefulset': 4,
            'job': 4,
            'cronjob': 4,
            # 引用 scaleTargetRef / 工作负载就绪后再应用（ autoscaling/v2 HorizontalPodAutoscaler ）
            'horizontalpodautoscaler': 5,
            # PodDisruptionBudget 依赖 Pod 标签与目标工作负载
            'poddisruptionbudget': 5,
        }

        # Find all namespace directories (excluding cluster-scoped)
        for ns_dir in sorted(backup_path.iterdir()):
            if ns_dir.is_dir() and ns_dir.name != "cluster-scoped":
                # Collect all resources with their priorities
                resources_to_restore = []
                
                for resource_dir in ns_dir.iterdir():
                    if resource_dir.is_dir():
                        resource_type = resource_dir.name.lower()
                        priority = resource_priority.get(resource_type, 5)  # Default priority for unknown types
                        
                        for yaml_file in sorted(resource_dir.glob("*.yaml")):
                            resources_to_restore.append((priority, str(yaml_file)))
                
                # Sort by priority and restore
                resources_to_restore.sort(key=lambda x: x[0])
                for _, yaml_file in resources_to_restore:
                    self._restore_single_resource(yaml_file, cluster_scoped=False)

    def _restore_crds(self):
        """
        恢复自定义资源定义（CRD）。
        
        注意: 此方法现在由 _restore_cluster_scoped_resources 处理，以确保
        CRD 在 CR 之前恢复。此方法保留用于向后兼容，但不应该单独调用。
        """
        # CRD 已在 _restore_cluster_scoped_resources 中恢复
        # 此方法保留用于兼容性，但不执行任何操作
        pass

    def _restore_single_resource(self, yaml_file: str, cluster_scoped: bool = False):
        """
        从 YAML 文件恢复对象（支持单文件多文档清单），统一使用 server-side apply。

        参考: https://kubernetes.io/docs/reference/using-api/server-side-apply/
        """
        try:
            documents = load_kubernetes_yaml_documents(yaml_file)
        except yaml.YAMLError as e:
            logger.error(f"解析 YAML 失败 {yaml_file}: {e}")
            self.restore_stats['total_resources'] += 1
            self.restore_stats['failed_restores'] += 1
            return
        except IOError as e:
            logger.error(f"读取文件失败 {yaml_file}: {e}")
            self.restore_stats['total_resources'] += 1
            self.restore_stats['failed_restores'] += 1
            return

        if not documents:
            logger.warning(f"{yaml_file} 中无有效的 Kubernetes 对象文档")
            self.restore_stats['total_resources'] += 1
            self.restore_stats['failed_restores'] += 1
            return

        n_doc = len(documents)
        for idx, resource in enumerate(documents):
            self.restore_stats['total_resources'] += 1
            doc_tag = f" [#{idx + 1}/{n_doc}]" if n_doc > 1 else ""
            self._restore_one_manifest(resource, yaml_file, cluster_scoped, doc_tag)

    def _restore_one_manifest(
        self,
        resource: Dict,
        yaml_file: str,
        cluster_scoped: bool,
        doc_tag: str,
    ) -> None:
        """对单个已解析的对象字典执行变换与 apply。"""
        try:
            transformed_resource = self.transformer.transform_resource(resource)
            api_version = transformed_resource.get('apiVersion', 'v1')
            kind = transformed_resource.get('kind', '')

            if not kind:
                logger.warning(f"{yaml_file}{doc_tag} 缺少 kind")
                self.restore_stats['failed_restores'] += 1
                return

            metadata = transformed_resource.get('metadata', {})
            if 'finalizers' in metadata:
                metadata.pop('finalizers', None)
                logger.debug(f"已移除 {kind} {metadata.get('name', 'unknown')} 的 finalizers")
            if 'ownerReferences' in metadata:
                metadata.pop('ownerReferences', None)
                logger.debug(f"已移除 {kind} {metadata.get('name', 'unknown')} 的 ownerReferences")

            name = metadata.get('name', '')
            namespace = metadata.get('namespace') if not cluster_scoped else None

            if self.config.dry_run:
                logger.info(
                    f"[模拟运行] 将恢复 {kind}/{name}{doc_tag} (ns={namespace})"
                )
                self.restore_stats['successful_restores'] += 1
                return

            try:
                self.k8s_client.apply_resource(transformed_resource)
                ns_info = f"命名空间 {namespace}" if namespace else "集群级"
                logger.info(f"已恢复 {kind}/{name}{doc_tag} ({ns_info})")
                self.restore_stats['successful_restores'] += 1
            except ApiException as e:
                if e.status == 404:
                    logger.error(f"资源类型 {api_version}/{kind} 在集群中不存在: {e}")
                elif e.status == 403:
                    logger.error(f"恢复 {kind}/{name}{doc_tag} 时权限被拒绝: {e}")
                elif e.status == 422:
                    logger.error(f"资源 {kind}/{name}{doc_tag} 验证错误: {e}")
                else:
                    logger.error(f"恢复 {kind}/{name}{doc_tag} 失败: HTTP {e.status} - {e}")
                self.restore_stats['failed_restores'] += 1
            except ResourceNotFoundError as e:
                logger.error(f"集群不支持资源类型 {api_version}/{kind}: {e}")
                self.restore_stats['failed_restores'] += 1
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                logger.error(f"恢复 {kind}/{name}{doc_tag} 时发生意外: {e}")
                self.restore_stats['failed_restores'] += 1
        except (KeyError, AttributeError, TypeError) as e:
            logger.error(f"处理清单失败 {yaml_file}{doc_tag}: {e}")
            self.restore_stats['failed_restores'] += 1

    def _log_restore_summary(self):
        """记录恢复摘要统计信息"""
        duration = self.restore_stats['end_time'] - self.restore_stats['start_time']
        total = self.restore_stats['total_resources']
        successful = self.restore_stats['successful_restores']
        failed = self.restore_stats['failed_restores']

        summary = {
            'total_resources': total,
            'successful': successful,
            'failed': failed,
            'duration_seconds': round(duration, 2),
            'success_rate': round((successful / total * 100) if total > 0 else 100, 2)
        }

        logger.info("=== 恢复摘要 ===")
        logger.info(f"总资源数: {total}")
        logger.info(f"成功: {successful}")
        logger.info(f"失败: {failed}")
        logger.info(f"耗时: {round(duration, 2)} 秒")
        logger.info(f"成功率: {summary['success_rate']}%")
        logger.info("恢复操作完成", **summary)


# -------------------------
# Enhanced CLI Interface
# -------------------------
def show_examples():
    """显示详细使用示例和说明（功能清单、进阶详解、场景示例、参数速查；篇幅较长便于一次性说清行为）。"""
    default_resources_line = ",".join(DEFAULT_RESOURCES)
    examples = """
================================================================================
Kubernetes 备份 / 恢复工具
================================================================================

【子命令】
  backup   从集群导出资源到本地目录（可选打 tar.gz）
  restore  从备份目录写回集群（可选映射命名空间 / 镜像 / 环境变量）

【两子命令均支持的通用参数】
  --kubeconfig PATH   指定 kubeconfig（不填则用默认）
  --context NAME      指定上下文
  --debug             更详细的日志
  --dry-run           演练模式
                        · backup：仍会调用 API 列举资源；不创建备份子目录、不写 YAML、不写 backup-metadata.json、
                          不生成 tar、不做备份后校验（全程不落盘）
                        · restore：不执行 server-side apply，仅打日志，集群内对象不变
                        例：backup --dry-run 结束后不应出现新的 backup-* 空目录；restore --dry-run 仅见 [模拟运行] 恢复日志。

【backup 独有 — 全部开关】
  范围（二选一，必填其一）
    -n, --namespace NAME     只备份该命名空间
    --all-namespaces         备份所有命名空间
  内容与过滤
    -r, --resources LIST     资源类型列表，逗号分隔；省略时默认为下方「默认 -r」整表
    --include-crds           在常规备份之外，再备份 CRD 定义，并遍历备份各 CR 实例（动态客户端）
    --label-selector         按标签过滤（kubectl 风格，如 app=myapp,env=prod）
    --field-selector         按字段过滤（kubectl 风格）
    --include-names LIST     仅备份 metadata.name 在列表中的对象（逗号分隔；省略则不过滤）
  输出与性能
    -o, --output-dir PATH    备份根目录（默认 /opt/k8s-backup）
    --tar                    额外生成 .tar.gz 归档
    --backup-name NAME       自定义本次备份目录名（默认时间戳自动生成）
    --max-workers N          并行线程数（默认 5）

  默认 -r（省略 --resources 时）:
    __DEFAULT_RESOURCES__

--------------------------------------------------------------------------------
【backup：并发、CRD、过滤与输出（详解，配合上方开关阅读）】
--------------------------------------------------------------------------------

--max-workers N（默认 5）
  作用：在已完成 API 列举之后，并行写出多个资源的 YAML 文件（线程池），只影响 backup，与 restore 无关。
  何时调大：某命名空间内 Deployment/ConfigMap 等数量很多、磁盘为 SSD 时，适当增大可缩短总耗时。
  何时勿过大：值过大可能造成磁盘或 API 客户端瞬时压力；脚本内建议上限见入口校验（一般 128 内）。
  例：200 个 Deployment、max-workers=8，表示最多约 8 个资源同时写盘，并非 8 个并行 API List。

--include-crds
  作用：在默认 -r 之外，再备份全集群的 CustomResourceDefinition；并对每个已生效的 CRD，用动态客户端
  按 apiVersion/kind 列举其资源实例（Cluster 范围一次；Namespaced 则按命名空间遍历），写入备份目录。
  目录上：除各命名空间子目录外，会出现 cluster-scoped/customresourcedefinition/ 下各 CRD 定义文件；
  各 CR 实例按 kind 分子目录落在对应命名空间或 cluster-scoped 下（与备份时 API 返回一致）。
  注意：CRD 与实例量很大时耗时会明显增加；目标集群若无对应 CRD，仅恢复实例会失败，须先安装 CRD 或一并恢复定义。

--label-selector（与 kubectl 一致）
  语义：传给 List 的 labelSelector。逗号分隔多个条件时为 AND（须同时满足）。
  例：--namespace app --label-selector "tier=frontend,env=staging" 只备份 app 命名空间中同时带 tier=frontend
  与 env=staging 标签的、且在你 -r 列表中的资源。无标签或标签不匹配则该类型列表为空，不报错。
  常见用途：只导出某业务线（统一 label）相关的 Deployment/Service，缩小备份体积。

--field-selector（与 kubectl 一致，受 API 限制）
  语义：传给 List 的 fieldSelector；具体支持哪些字段因资源类型而异（与 kubectl get 相同限制）。
  例：--field-selector "metadata.name=my-api" 在每种资源类型的列表结果中筛名字为 my-api 的对象（若 API 支持）。
  注意：部分资源不支持某些 field，或行为以集群版本为准；筛选过严可能导致某类型备份为空。

--include-names（按对象名精确过滤，省略则与旧版相同：备份 -r 下列出的全部对象）
  语义：在 API List（及可选的 label/field selector）之后，仅保留 metadata.name 在名单中的资源。
  名称须为 DNS 子域名（RFC 1123），与 Kubernetes 对象名约定一致；大小写敏感、精确匹配。
  例：-n prod -r deployments,services --include-names "api-gateway,order-service"
       只写出名为 api-gateway 与 order-service 的 Deployment/Service；同命名空间其它应用不会落盘。
  与 --label-selector / --field-selector 为 AND：先 API 过滤，再按名称筛。
  注意：按名称过滤时，ConfigMap/Secret 等若与 Deployment 不同名，不会自动连带备份；须将依赖对象名一并列入，
  或扩大 -r / 使用标签过滤。Service 与 Deployment 异名时，两个名字都需写入列表。

-o / --output-dir 与 --backup-name
  单次备份落盘根路径为：输出根目录 / 备份名 /。
  例：--output-dir /data/k8s-backup --backup-name nightly-20260414
  则生成 /data/k8s-backup/nightly-20260414/，其内有 backup-metadata.json 及按命名空间/资源类型划分的子目录。

--tar
  在非 dry-run 时，在备份根路径同级再生成同名 .tar.gz，例如 nightly-20260414.tar.gz，便于拷贝归档。

【restore 独有 — 全部开关】
  必需
    --backup-dir PATH        备份目录（内含命名空间子目录或 tar 解压后的结构）
  命名空间
    --create-namespaces      目标集群缺命名空间时自动创建（别名: --create-namespace）
  跨环境改写（均可单独或组合使用）
    --namespace-mapping MAP  旧命名空间名 -> 新名（见下方「映射语法」）
    --image-mapping MAP      改写 container.image（单字符串，含仓库/标签/digest）；最长前缀优先替换
    --env-mapping MAP        按环境变量名 增 / 改 / 删；支持 @k8s:fieldPath（Downward API）（见下方）
    --env-namespace-substitute off|auto|all
                             仅在有 --namespace-mapping 时生效：是否改写 env.value 内联字符串中的源命名空间
                             （默认 auto；与 valueFrom 区分、顺序、示例见下方专节）
    --merge-patch PATH|JSON  补丁：优先文件；内联 JSON 仅短浅片段（有上限）；须配合 --merge-patch-kind（见专节）
    --merge-patch-kind KIND  至少一次，可重复；仅对该 kind 合并补丁（与清单 kind 大小写一致）
  恢复范围
    --skip-crds              不恢复 CustomResourceDefinition
    --skip-cluster-scoped    不恢复集群级资源（如 ClusterRole、StorageClass 等）
    --backup-name NAME       同一目录多份备份时，指定要恢复的那一份名称
  （恢复过程按依赖顺序串行 apply，无并行阶段；与 backup 的 --max-workers 无关）

【映射语法（namespace / image / env 通用书写规则）】
  · 每项必须为  KEY=value ；键与值之间只用第一个 = 分割，故值里可含 :、=、URL 等。
  · 整段参数须至少包含一个 '='；多项之间可用空格、逗号或逗号加空格分隔，例如:
      A=1 B=2, C=3
  · 快速对照（真实写法缩略）：
      命名空间：--namespace-mapping "dev-ns=prod-ns"
      镜像：--image-mapping "registry.old.com/proj/=registry.new.com/proj/"
      环境变量：--env-mapping "DB_HOST=db.prod.internal LOG_LEVEL="
  · 环境变量映射语义（--env-mapping）:
      KEY=新值     已有则改值，没有则新增
      KEY=         删除该环境变量（ Deployment/Pod 模板中的 env 项）
      KEY=@k8s:metadata.namespace  恢复为 Downward API（valueFrom.fieldRef），运行时注入当前 Pod 命名空间
        （官方推荐用于「值即命名空间」；fieldPath 见脚本内白名单与 metadata.labels['k'] 形式）

--------------------------------------------------------------------------------
【恢复阶段：环境变量里的「源命名空间」如何替换（--env-namespace-substitute）】
--------------------------------------------------------------------------------

（0）本脚本这一段在做什么
  恢复时，清单里所有内容（含 env 的 name）本来都在 YAML 里。本选项只解决一类问题：
  当某环境变量仍通过 Kubernetes 的 env.value 字段、把字符串直接写在清单里时，是否要把该字符串中出现的
  「源命名空间」替换成「目标命名空间」。它不解析 Secret/ConfigMap 文件内容，也不自动改 valueFrom 指向的对象；
  若某变量已用 valueFrom，除非你在第③步用 --env-mapping 把它改成 value，否则本步骤不会动它。

（1）术语（请先读这几条，再往下看选项）
  · env.value 内联字符串（文档中简称「内联 value」）
      Kubernetes 规定 env 条目在 YAML 里要么带 value，要么带 valueFrom，二者互斥（见 Pod Container env）。
      「内联 value」指：该条目使用 value 字段，且把要注入容器的字符串直接写在该字段下面，例如
        - name: DOMAIN_NAME
          value: ebd-board-server.my-old-namespace
      与之相对：使用 valueFrom 引用 Secret、ConfigMap、fieldRef 等，运行时值来自被引用对象或 Downward API，
      清单里不再用 value 字段承载那段业务字符串。本步骤只改写「仍带 value 字段」的条目中的字符串；
      对 valueFrom 条目既不替换其引用目标，也不臆造 value 字段。
  · 源命名空间（旧名）
      --namespace-mapping 里等号左侧的名字，即备份里使用的命名空间名
      （例：talkweb-project-hainan-test）。
  · 目标命名空间（新名）
      --namespace-mapping 里等号右侧的名字，即要恢复到的命名空间名
      （例：talkweb-project-hainan-prod）。
  · 与 --namespace-mapping 配合
      必须先提供至少一条 旧名=新名；本选项决定在映射已确定后，是否还要在仍为 env.value 内联字符串的
      内容里，把出现的源命名空间替换成目标命名空间。
      若无 --namespace-mapping（或映射表为空），--env-namespace-substitute 不会产生任何效果。

（2）同一资源内的处理顺序（与先后覆盖关系）
  对带 Pod 模板的资源，脚本按下面顺序执行；排在前面的步骤会改变清单，后面的步骤基于最新清单：
    ① 资源的 metadata.namespace、Pod 模板内嵌套 namespace 等（--namespace-mapping）
    ② --image-mapping（容器 image）
    ③ --env-mapping（按变量名增/删/改；若某 KEY 被改成 @k8s:...，则该 KEY 变为 valueFrom，
       不再使用内联 value）
    ④ --env-namespace-substitute：只对第③步之后仍为 env.value 内联字符串的项，按模式替换字符串中的
       源命名空间到目标命名空间
    ⑤ --merge-patch：将补丁按 kubectl patch -p 形状深度合并进资源根对象（见下方「合并补丁」专节）
    ⑥ 对容器 ports 按 (containerPort, protocol) 去重，避免 server-side apply 触发 apiserver 报错

  因此：若你用 --env-mapping 把某变量改成了 Downward API（valueFrom.fieldRef），该变量不会再参与第④步。

（3）--env-namespace-substitute 三种模式（均只作用于第（2）节第④步中的内联 value）
  · auto（默认，推荐）
      在字符串中查找源命名空间时采用较保守的规则，减少误伤：
      - 整段 value 恰好等于源命名空间：整段换成目标命名空间
      - value 以 .源命名空间 结尾（常见：应用名.命名空间 形式的主机名）：只换最后这一段
      - value 中含 .源命名空间.（中间一段）：替换该段
      - value 以 源命名空间. 开头：替换前缀
      实践：大量 Deployment 中某 env 条目的 value 为 ebd-board-server.talkweb-project-hainan-test，
      仅执行 --namespace-mapping "talkweb-project-hainan-test=talkweb-project-hainan-prod" 即可一把恢复，
      应用名 ebd-board-server 保持备份原样，只把后缀命名空间换成 prod。
  · all
      对每个源命名空间名，在整段 value 中做全局文本替换（str.replace，多规则时按旧名长度从长到短）。
      适合 URL、长串里多处出现命名空间名的场景；若某配置值里偶然含有与命名空间同名的子串，可能误改。
  · off
      不做第④步自动替换。跨环境时只能依赖 --env-mapping 逐 KEY 写死新值，或使用 @k8s:fieldPath。

（4）与 Downward API（@k8s:）的分工
  · 值就是当前命名空间、且希望运行时注入：DEPLOY_ENV=@k8s:metadata.namespace（不必写死在映射里）。
    例：恢复后清单中为 valueFrom.fieldRef metadata.namespace，Pod 在 prod-ns 里启动时容器内 DEPLOY_ENV=prod-ns。
  · 值为 应用名.命名空间 且应用名各 Deployment 不同：用 auto 加 namespace-mapping（默认即 auto）改后缀即可，
    无需为每个应用单独写 --env-mapping。

--------------------------------------------------------------------------------
【恢复阶段：合并补丁（--merge-patch，与 kubectl patch -p 形状一致）】
--------------------------------------------------------------------------------

  设计：kubectl 对运行中对象执行 patch；本脚本在 restore 时对静态清单做同类合并后再 server-side apply，
  避免对线上做逐资源 patch 带来的抖动，并与命名空间/镜像/env 映射同属「落盘前改写」生产流程。

  生产用法（与 kubectl patch 成熟语义一致；本脚本只在落盘前合并，边界由下列方式控制）：
    · 推荐（复杂、多文档、需评审）：使用 YAML/JSON 文件路径。无内联专用的结构深度上限，适合 affinity、
      长 volumes、多段用 --- 分隔的补丁等；与配置库、MR 流程一致，利于企业稳定变更。
    · 可选（简单片段）：命令行内联 JSON（首尾空白去掉后以「{」开头）。与 kubectl patch -p、
      Python dict 经 json.dumps 一致；受 UTF-8 字节数与嵌套深度上限约束，超限须改用文件，
      避免命令行长度、shell 转义与难以 Code Review 的大段 JSON。

  嵌套须与该资源在 Kubernetes API 中的结构一致，不要含根级 apiVersion、kind、status（若误带，脚本会剔除并告警）。
  内联上限（常量，与 kubectl 行为无关，仅本工具防运维风险）：UTF-8 约 __MAX_MERGE_PATCH_INLINE_UTF8_BYTES__ 字节；
  结构深度约 __MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH__（dict 向下累计，list 内取子元素最大深度）。
  详见脚本内 MAX_MERGE_PATCH_INLINE_UTF8_BYTES、MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH。

  例（与官方 Deployment API 一致，等价于对运行中 Deployment 执行）：
    kubectl patch deployment my-deploy -p '{"spec":{"template":{"spec":{"nodeSelector":{"release":"production"}}}}}'
  对应补丁文件 deploy-merge.yaml：
        spec:
          template:
            spec:
              nodeSelector:
                release: production

  合并规则：双方同一键均为映射时递归合并；否则以补丁整段覆盖（含列表）。列表项不按 strategic merge 的主键合并，
  与 apiserver 行为可能不同；复杂 patches 请以官方文档核对。

  作用范围（强制）：
    · 使用 --merge-patch 时必须同时指定至少一个 --merge-patch-kind（可重复，例如同时写 Deployment 与 StatefulSet），
      仅对清单中 kind 与之一致的文档合并补丁，与按资源类型精确 patch 的惯例一致。

  与 env：容器环境变量优先用 --env-mapping；补丁中若写 spec.template.spec.containers 等整段列表会覆盖备份子树，
    除非补丁内写全量定义。

  命令示例（补丁文件）：
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --merge-patch /path/to/deploy-merge.yaml \\
        --merge-patch-kind Deployment \\
        --create-namespaces

  命令示例（内联 JSON，注意 shell 引号；长片段建议仍用文件）：
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --merge-patch '{"spec":{"template":{"spec":{"nodeSelector":{"release":"production"}}}}}' \\
        --merge-patch-kind Deployment \\
        --create-namespaces

--------------------------------------------------------------------------------
【映射语法】续 — 镜像
--------------------------------------------------------------------------------
  · 镜像映射（对齐 Kubernetes 官方：镜像名为单一字符串，标签与 digest 均为其后缀，无单独字段）:
      - 换仓库/路径：左侧写旧 registry 或路径前缀，例如 registry.a.com/proj/=registry.b.com/proj/
      - 例：备份里 image: registry.a.com/app:1.0，映射左侧写 registry.a.com/=registry.b.com/，恢复后为 registry.b.com/app:1.0
      - 保留原标签：左侧不要包含到 ':' 为止的标签部分，则 :v1.2 会留在结果中
      - 改标签或 digest：左侧须包含要替换的旧标签或 digest 片段，例如
        myreg.io/app:v1=myreg.io/app:v2  或  nginx@sha256:abc...=nginx@sha256:def...
      - 多规则时按最长前缀优先匹配（与常见镜像重写规则一致）

--------------------------------------------------------------------------------
【restore：范围开关、备份目录布局与 NetworkPolicy（详解）】
--------------------------------------------------------------------------------

--skip-crds
  适用：目标集群已统一安装好同一套 CRD（如同一平台团队下发），你只把业务命名空间里的 CR 实例迁过去，
  希望避免对 CustomResourceDefinition 再次 server-side apply，降低与集群已有 CRD 版本冲突风险。
  不适用：目标集群根本没有该 CRD 时，跳过 CRD 后实例仍无法创建，须先在目标集群注册 CRD 或不要跳过。

--skip-cluster-scoped
  适用：目标集群已有全局 RBAC、StorageClass、IngressClass 等，你只恢复命名空间内工作负载与配置，
  避免覆盖 ClusterRole、ClusterRoleBinding、PV、StorageClass 等集群级对象。
  说明：命名空间内的 Role、RoleBinding、ConfigMap 等仍会按备份恢复（只要备份目录中存在）。

--backup-name
  适用：--backup-dir 指向「多份备份共同的父目录」时，用 --backup-name 指定要恢复的那一次子目录名。
  例：--backup-dir /opt/k8s-backup --backup-name backup-20231201-120000-default
  实际读取 /opt/k8s-backup/backup-20231201-120000-default/。
  若 --backup-dir 已直接指向某次备份根目录（其下即有 backup-metadata.json），则无需再写 --backup-name。

--create-namespaces
  恢复前会扫描备份 yaml 中出现的 metadata.namespace，对目标集群缺失的命名空间做 apply；任一创建失败则整次 restore 中止，
  避免大量对象 apply 到不存在的命名空间。

备份目录常见布局（与备份生成或 tar 解压后一致，便于核对路径）：
  <备份根>/
    backup-metadata.json
    cluster-scoped/          （若有集群级资源）
      clusterrole/
      customresourcedefinition/
      ...
    <命名空间名>/            （每个命名空间一个目录）
      deployment/
      service/
      networkpolicy/
      ...

命名空间映射与 NetworkPolicy（与专节「源命名空间」配合）
  除改写资源 metadata.namespace 外，脚本会处理 NetworkPolicy 中 namespaceSelector 里按命名空间名筛选的
  标签（如 kubernetes.io/metadata.name）及 matchExpressions 中对应值，使策略在映射后仍指向正确命名空间。
  例：原策略只允许来自 old-ns 的流量，映射 old-ns=new-ns 后，选择器中的 old-ns 会改为 new-ns，
  避免出现策略仍引用旧名导致网络策略失效或选错端点。

================================================================================
场景示例（可复制改路径后使用）
================================================================================

B1. 备份单个命名空间（默认资源类型全集）
   python KubeBackupCli.py backup \\
        --namespace default \\
        --output-dir /opt/k8s-backup

B2. 备份全集群所有命名空间 + 含 CRD + 打 tar 包 + 自定义备份名 + 并发数
   python KubeBackupCli.py backup \\
        --all-namespaces \\
        --include-crds \\
        --output-dir /opt/k8s-backup \\
        --tar \\
        --backup-name nightly-20231201 \\
        --max-workers 8

B3. 只备份部分资源类型
   python KubeBackupCli.py backup \\
        --namespace production \\
        --resources "deployments,services,configmaps,secrets" \\
        --output-dir /opt/k8s-backup

B4. 标签过滤 + 字段过滤 + 指定 kubeconfig / 上下文 + 调试日志
   python KubeBackupCli.py backup \\
        --namespace default \\
        --label-selector "app=myapp,env=production" \\
        --field-selector "metadata.name=my-deploy" \\
        --kubeconfig ~/.kube/config-prod \\
        --context prod-cluster \\
        --debug \\
        --output-dir /opt/k8s-backup

B5. 备份演练（仍访问集群 API；不建 backup-* 目录、不写 YAML / 元数据 / tar）
   python KubeBackupCli.py backup \\
        --namespace default \\
        --dry-run \\
        --debug \\
        --output-dir /opt/k8s-backup
   说明：与正式 backup 共用列举逻辑；结束后 /opt/k8s-backup 下不应新增 backup-时间戳 空目录（历史遗留空目录需手工清理）。

B6. 同一命名空间内只备份某业务线（标签 AND）
   python KubeBackupCli.py backup \\
        --namespace payments \\
        --label-selector "app=checkout,tier=backend" \\
        --output-dir /opt/k8s-backup
   说明：只备份同时带 app=checkout 与 tier=backend 的对象；若 Deployment 打了标签而 Service 未打齐，可能出现只备 Deployment 不备对应 Service，需按实际标签设计调整。

B6b. 按应用对象名只备份指定 Deployment/Service（生产常用）
   python KubeBackupCli.py backup \\
        --namespace production \\
        --resources "deployments,services" \\
        --include-names "api-gateway,order-service" \\
        --output-dir /opt/k8s-backup
   说明：省略 --include-names 时行为与旧版一致（该命名空间下全部 Deployment/Service）；指定后仅 metadata.name 匹配的会写盘。

B7. 全集群备份并包含 CRD 及其实例（耗时可较长）
   python KubeBackupCli.py backup \\
        --all-namespaces \\
        --include-crds \\
        --resources "deployments,services,configmaps,secrets" \\
        --max-workers 12 \\
        --output-dir /opt/k8s-backup \\
        --backup-name nightly-with-crds-20260414
   说明：--include-crds 会在上述 -r 之外再拉 CRD 与各 CR 实例；若集群 CR 数量巨大，建议预留磁盘与时间窗口。

R1. 恢复（最简：指定备份目录 + 自动建命名空间）
   python KubeBackupCli.py restore \\
        --backup-dir /opt/k8s-backup/backup-20231201-120000-default \\
        --create-namespaces

R2. 命名空间映射（空格或逗号分隔多项）
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --namespace-mapping "dev=prod test=staging" \\
        --create-namespaces
   含义示例：备份中 metadata.namespace 为 dev 的资源恢复到 prod；为 test 的恢复到 staging（含 Namespace 对象 metadata.name 的对应改写）。

R3. 镜像：换仓库（保留原 :tag）或连标签一起改（与官方「单字符串镜像名」一致）
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --image-mapping "registry.old.com/=registry.new.com/ myproj.io/app:v1.0=myproj.io/app:v2.0" \\
        --create-namespaces

R4. 环境变量 改值 / 删变量 / 新增变量（值中可有 https://host:443/path）
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --env-mapping "DB_HOST=prod-db.internal API_URL=https://api.prod.com:443 LOG_LEVEL= DEBUG=" \\
        --create-namespaces

R4b. 整命名空间迁环境：各 Deployment 中 DOMAIN_NAME 等为 env.value 内联（应用名.源命名空间），一把 restore 换后缀
   备份里常见结构（均在 YAML；第一项走第④步字符串替换，第二项为 valueFrom，第④步不改其引用）：
        env:
          - name: DOMAIN_NAME
            value: ebd-board-server.talkweb-project-hainan-test
          - name: SOME_FROM_SECRET
            valueFrom:
              secretKeyRef: { name: app-secret, key: k }
   命令（默认 --env-namespace-substitute=auto，一般无需再写）：
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --namespace-mapping "talkweb-project-hainan-test=talkweb-project-hainan-prod" \\
        --create-namespaces
   恢复后：DOMAIN_NAME 的 value 变为 ebd-board-server.talkweb-project-hainan-prod；SOME_FROM_SECRET 仍由集群内 Secret 解析。
   若另有变量整段值等于命名空间名、且希望运行时注入当前命名空间，可追加例如：
        --env-mapping "DEPLOY_ENV=@k8s:metadata.namespace"

R5. 三种映射同时使用 + 指定集群与演练
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --kubeconfig ~/.kube/config-dr \\
        --context dr-site \\
        --namespace-mapping "app-ns=app-ns-dr" \\
        --image-mapping "docker.io/myorg/=registry.dr.local/myorg/" \\
        --env-mapping "REPLICA_URL=https://replica:9200" \\
        --create-namespaces \\
        --dry-run \\
        --debug

R6. 跳过 CRD 与集群级资源（仅命名空间内资源）
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --skip-crds \\
        --skip-cluster-scoped \\
        --create-namespaces

R7. 备份目录中存在多份备份时按名称挑选（恢复始终串行 apply，无并发）
   python KubeBackupCli.py restore \\
        --backup-dir /opt/k8s-backup \\
        --backup-name backup-20231201-120000-default \\
        --create-namespaces

R8. 灾备场景：目标集群已有全局策略，只灌入命名空间内业务（跳过 CRD 与集群级对象）
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --namespace-mapping "prod-blue=prod-green" \\
        --skip-crds \\
        --skip-cluster-scoped \\
        --create-namespaces
   说明：适用于目标集群已安装同版本 CRD、ClusterRole/StorageClass 等由平台统一维护，只需把蓝环境命名空间
   迁到绿环境命名空间且不改集群级对象的场合。若绿集群缺少某 CRD，仍须先安装 CRD 或去掉 --skip-crds。

R9. 跨环境：与 kubectl patch -p 相同形状，改写 Deployment 的 Pod 模板（例：nodeSelector）
   方式一：补丁文件 deploy-merge.yaml（嵌套与官方 Deployment.spec 一致）同上专节。
   方式二：内联 JSON（与 Python dict 的 JSON 表示相同），须整体加引号交给 shell：
   python KubeBackupCli.py restore \\
        --backup-dir /path/to/backup \\
        --merge-patch '{"spec":{"template":{"spec":{"nodeSelector":{"release":"production"}}}}}' \\
        --merge-patch-kind Deployment \\
        --create-namespaces
   说明：--merge-patch 与 --merge-patch-kind 必须同时使用；可对多种工作负载重复写 --merge-patch-kind。
   生产上：内联仅适合短浅片段（脚本有 UTF-8 与嵌套深度上限）；affinity、长 volumes、多段 YAML 等请用方式一（文件）。

--------------------------------------------------------------------------------
【restore：命名空间内资源 apply 顺序（摘要）】
--------------------------------------------------------------------------------
  同一备份命名空间子目录内，多个 yaml 文件按资源类型优先级排序后依次 server-side apply（单线程顺序，非并行），
  以降低「工作负载先于 ConfigMap/Secret 创建」一类依赖问题。大致优先级：基础配置与 Secret/ConfigMap 类较早，
  Service、RBAC、NetworkPolicy、Ingress 等次之，Deployment/StatefulSet/DaemonSet/Job 等工作负载再次，
  HPA、PDB 等往往靠后；备份中出现的其它类型通常默认靠后。若某资源 apply 失败，计入失败统计并继续后续文件
  （以日志为准）。集群级目录 cluster-scoped 单独先处理且 CRD 优先于其它集群级资源。

================================================================================
参数速查表（与上面功能总览一一对应）
================================================================================
  backup:
    -n, --namespace NAME | --all-namespaces
    -r, --resources LIST | --include-crds | --label-selector | --field-selector
    -o, --output-dir | --tar | --backup-name | --max-workers
  restore:
    --backup-dir | --create-namespaces（--create-namespace）
    --namespace-mapping | --image-mapping | --env-mapping | --env-namespace-substitute
    --merge-patch | --merge-patch-kind
    --skip-crds | --skip-cluster-scoped | --backup-name
  通用: --kubeconfig | --context | --debug | --dry-run

================================================================================
注意事项
================================================================================
  - 映射：仅支持 KEY=value；值中含 ':'、'=' 时仍用第一个 '=' 分隔键与完整值。
  - env：@k8s:fieldPath（Downward API）见专节；env.value 内联字符串中的命名空间替换须先有 --namespace-mapping，
    再由 env-namespace-substitute（默认 auto）在 env_mapping 之后处理仍为 value 内联的条目，详见 -h 专节。
  - backup：label-selector 要求对象带齐标签，否则该类型可能备份为空；field-selector 受 Kubernetes API 支持范围限制，
    与 kubectl get 行为一致，过严或不受支持时结果可能为空。
  - backup：--include-crds 会显著增加列举与落盘量，大集群请预留时间与磁盘；目标集群恢复 CR 实例前通常需先有 CRD。
  - 启动前会校验路径、kubeconfig、并发数、资源类型名、命名空间名等；错误参数会记录日志并以退出码 1 结束，避免未处理异常。
  - 恢复：单文件多 YAML 文档（--- 分隔）会逐个对象 apply，与 kubectl 清单语义一致；禁止静默丢弃后续文档。
    例：app.yaml 内第一段 Deployment、第二段 Service，会先后两次 server-side apply，不会只建 Deployment 而丢 Service。
  - 恢复：启用 --create-namespaces 时，预创建命名空间任一失败会整次中止，避免资源写入错误拓扑。
  - 命名空间映射对 Kind=Namespace 会改写 metadata.name；NetworkPolicy 的 namespaceSelector 同时处理 matchLabels 与 matchExpressions（kubernetes.io/metadata.name）。
  - 若容器 ports 中出现相同 containerPort+protocol 的多条（仅 name 不同），server-side apply 可能报 500；
    备份与恢复阶段会自动按 (containerPort, protocol) 去重并打警告日志，保留第一条；若业务确需多端口请改为不同端口或合并探针配置。
  - 备份前预留磁盘空间；备份含 Secret，需妥善保管。
  - 恢复使用 server-side apply（force 解决字段冲突），并会去掉 finalizers / ownerReferences 等集群绑定字段。
  - 备份拒绝缺少 kind 或 apiVersion 的对象；从不写入虚构类型，避免无法 apply 的垃圾清单。
  - Service 备份保留 loadBalancerClass（用户显式 LB 实现）；仅剥离 clusterIP / 已弃用的 loadBalancerIP。
  - API 客户端对 409/429/5xx 有限次重试（ SSA 竞态与瞬时服务端错误）。
  - 生产环境建议先 restore --dry-run 再正式执行。
  - 依赖: Python 3.7+，kubernetes、pyyaml、tenacity。
""".replace("__DEFAULT_RESOURCES__", default_resources_line).replace(
        "__MAX_MERGE_PATCH_INLINE_UTF8_BYTES__", str(MAX_MERGE_PATCH_INLINE_UTF8_BYTES)
    ).replace(
        "__MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH__", str(MAX_MERGE_PATCH_INLINE_STRUCTURE_DEPTH)
    )
    print(examples)


def _add_backup_arguments(parser):
    """添加备份相关的参数"""
    parser.add_argument(
        '-n', '--namespace',
        help='要备份的命名空间'
    )
    parser.add_argument(
        '--all-namespaces',
        action='store_true',
        help='备份所有命名空间'
    )
    parser.add_argument(
        '-r', '--resources',
        default=','.join(DEFAULT_RESOURCES),
        help=f'要备份的资源类型列表（逗号分隔，默认: {",".join(DEFAULT_RESOURCES)}）'
    )
    parser.add_argument(
        '-o', '--output-dir',
        default='/opt/k8s-backup',
        help='备份输出目录（默认: /opt/k8s-backup）'
    )
    parser.add_argument(
        '--include-crds',
        action='store_true',
        help='包含自定义资源定义及其实例'
    )
    parser.add_argument(
        '--tar',
        action='store_true',
        help='创建压缩的 tar.gz 归档文件'
    )
    parser.add_argument(
        '--max-workers',
        type=int,
        default=5,
        help='备份时并行线程数（默认: 5）；与 restore 无关'
    )
    parser.add_argument(
        '--label-selector',
        help='标签选择器，用于过滤资源'
    )
    parser.add_argument(
        '--field-selector',
        help='字段选择器，用于过滤资源'
    )
    parser.add_argument(
        '--include-names',
        help='仅备份 metadata.name 在列表中的对象（逗号分隔；省略则不过滤，与旧版全量一致）'
    )
    parser.add_argument(
        '--backup-name',
        help='自定义备份名称（默认: 自动生成）'
    )


def _add_restore_arguments(parser):
    """添加恢复相关的参数"""
    parser.add_argument(
        '--backup-dir',
        required=True,
        help='备份目录路径（必需）'
    )
    parser.add_argument(
        '--namespace-mapping',
        help='源命名空间=目标命名空间（等号左=备份中的旧名，右=恢复目标新名）；多项空格或逗号分隔；'
             ' 与 env-namespace-substitute 联动见 -h 全文「恢复阶段：环境变量」专节'
    )
    parser.add_argument(
        '--image-mapping',
        help='container.image 前缀替换（单字符串含仓库/标签/digest）；最长前缀优先；多项逗号或空格分隔'
    )
    parser.add_argument(
        '--env-mapping',
        help='环境变量映射：KEY=值；多项用逗号或空格分隔；值可含 URL/冒号。'
             ' KEY= 表示删除；原资源无该 KEY 时会新增；'
             ' 值可为 @k8s:metadata.namespace 等以使用 Downward API（fieldRef）'
    )
    parser.add_argument(
        '--env-namespace-substitute',
        choices=('off', 'auto', 'all'),
        default='auto',
        help='仅当提供 --namespace-mapping 时有效：在 env_mapping 之后，是否仍对 env.value 内联字符串'
             ' 做源命名空间到目标命名空间替换（默认 auto；与 valueFrom 区分见 -h 专节）',
    )
    parser.add_argument(
        '--merge-patch',
        metavar='PATH_OR_JSON',
        dest='merge_patch',
        help='YAML/JSON 文件路径（推荐：复杂/多文档），或以「{」开头的内联 JSON（仅适合短浅片段，有长度与嵌套深度上限）；'
             ' 形状同 kubectl patch -p；勿含根级 apiVersion/kind/status；与 --merge-patch-kind 配套必填，见 -h 专节',
    )
    parser.add_argument(
        '--merge-patch-kind',
        action='append',
        dest='merge_patch_kinds',
        metavar='KIND',
        help='与 --merge-patch 配套使用且至少指定一次；可重复。仅当清单 kind 与本参数一致时才合并补丁'
             '（须与 YAML 中 kind 大小写一致，如 Deployment）。',
    )
    parser.add_argument(
        '--max-workers',
        type=int,
        default=5,
        help='（保留参数）恢复为依赖顺序串行 apply，此值不生效；并行仅用于 backup'
    )
    parser.add_argument(
        '--skip-crds',
        action='store_true',
        help='跳过自定义资源定义的恢复'
    )
    parser.add_argument(
        '--skip-cluster-scoped',
        action='store_true',
        help='跳过集群级资源的恢复'
    )
    parser.add_argument(
        '--create-namespaces', '--create-namespace',
        dest='create_namespaces',
        action='store_true',
        help='自动创建不存在的命名空间'
    )
    parser.add_argument(
        '--backup-name',
        help='指定要恢复的备份名称（如果备份目录包含多个备份）'
    )


def create_argument_parser() -> argparse.ArgumentParser:
    """创建增强的 CLI 参数解析器，支持子命令"""
    parser = argparse.ArgumentParser(
        description="企业级 Kubernetes 配置备份和恢复工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False
    )

    parser.add_argument(
        '-h', '--help',
        action='store_true',
        help='显示详细使用说明和示例'
    )

    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # Backup subcommand
    backup_parser = subparsers.add_parser('backup', help='备份 Kubernetes 资源')
    _add_backup_arguments(backup_parser)

    # Restore subcommand
    restore_parser = subparsers.add_parser('restore', help='从备份恢复 Kubernetes 资源')
    _add_restore_arguments(restore_parser)

    backup_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='演练：仍列举集群资源，但不创建备份子目录、不写 YAML/元数据/tar',
    )
    restore_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='演练：不执行 server-side apply，仅打 [模拟运行] 日志',
    )

    # Common arguments
    for subparser in [backup_parser, restore_parser]:
        subparser.add_argument(
            '--kubeconfig',
            help='kubeconfig 文件路径'
        )
        subparser.add_argument(
            '--context',
            help='要使用的 Kubernetes 上下文名称'
        )
        subparser.add_argument(
            '--debug',
            action='store_true',
            help='启用调试日志'
        )

    return parser


def validate_backup_arguments(args) -> Optional[BackupConfig]:
    """验证备份命令行参数"""
    if args.all_namespaces and _cli_strip_opt(args.namespace):
        logger.error("不能同时指定 --namespace 与 --all-namespaces")
        return None

    if not args.namespace and not args.all_namespaces:
        logger.error("必须指定 --namespace 或 --all-namespaces 参数之一")
        return None

    if not validate_cli_max_workers(args.max_workers):
        return None
    if not validate_cli_kubeconfig(args.kubeconfig):
        return None
    if not validate_cli_context(args.context):
        return None
    if not validate_cli_selector(args.label_selector, "--label-selector"):
        return None
    if not validate_cli_selector(args.field_selector, "--field-selector"):
        return None
    if not validate_cli_output_dir_for_backup(args.output_dir, args.dry_run):
        return None
    if not validate_cli_backup_name(args.backup_name):
        return None

    if args.all_namespaces:
        namespace = "all"
    else:
        namespace = str(args.namespace).strip()
        if not validate_cli_backup_namespace(namespace):
            return None

    resources = [r.strip() for r in str(args.resources).split(",") if r.strip()]
    if not resources:
        logger.error("必须指定至少一个资源类型（-r / --resources）")
        return None
    if not validate_cli_resource_types(resources):
        return None

    try:
        include_names = parse_include_names(getattr(args, 'include_names', None))
    except IncludeNamesParseError as e:
        logger.error(f"--include-names 无效: {e}")
        return None

    ctx = _cli_strip_opt(args.context)
    return BackupConfig(
        kubeconfig=_cli_strip_opt(args.kubeconfig),
        context=ctx,
        namespace=namespace,
        resources=resources,
        output_dir=os.path.abspath(os.path.expanduser(str(args.output_dir).strip())),
        include_crds=args.include_crds,
        create_tarball=args.tar,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        label_selector=_cli_strip_opt(args.label_selector),
        field_selector=_cli_strip_opt(args.field_selector),
        include_names=include_names,
        backup_name=_cli_strip_opt(args.backup_name),
    )


def validate_restore_arguments(args) -> Optional[RestoreConfig]:
    """验证恢复命令行参数"""
    backup_dir = validate_cli_restore_backup_dir(args.backup_dir)
    if backup_dir is None:
        return None

    if not validate_cli_kubeconfig(args.kubeconfig):
        return None
    if not validate_cli_context(args.context):
        return None
    if not validate_cli_backup_name(args.backup_name):
        return None

    try:
        namespace_mapping = parse_mapping(args.namespace_mapping, "namespace")
        image_mapping = parse_mapping(args.image_mapping, "image")
        env_mapping = parse_mapping(args.env_mapping, "env")
    except MappingParseError as e:
        logger.error(f"映射参数无效: {e}")
        return None

    merge_patch: Optional[Dict[str, Any]] = None
    raw_patch = _cli_strip_opt(getattr(args, "merge_patch", None))
    if raw_patch:
        merge_patch = load_merge_patch_from_cli_arg(raw_patch)
        if merge_patch is None:
            return None

    raw_mp_kinds = getattr(args, "merge_patch_kinds", None) or []
    merge_patch_kinds: Optional[Set[str]] = None
    if raw_mp_kinds:
        merge_patch_kinds = {str(x).strip() for x in raw_mp_kinds if x and str(x).strip()}
        if not merge_patch_kinds:
            merge_patch_kinds = None

    if merge_patch and not merge_patch_kinds:
        logger.error(
            "使用 --merge-patch 时必须指定至少一个 --merge-patch-kind（与清单中 kind 大小写一致，例如 Deployment）；"
            "以避免将补丁误合并到其它资源类型"
        )
        return None
    if merge_patch_kinds and not merge_patch:
        logger.error("已指定 --merge-patch-kind 但未提供 --merge-patch")
        return None

    ctx = _cli_strip_opt(args.context)
    return RestoreConfig(
        kubeconfig=_cli_strip_opt(args.kubeconfig),
        context=ctx,
        backup_dir=backup_dir,
        namespace_mapping=namespace_mapping,
        image_mapping=image_mapping,
        env_mapping=env_mapping,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        skip_crds=args.skip_crds,
        skip_cluster_scoped=args.skip_cluster_scoped,
        create_namespaces=args.create_namespaces,
        backup_name=_cli_strip_opt(args.backup_name),
        env_namespace_substitute=args.env_namespace_substitute,
        merge_patch=merge_patch,
        merge_patch_kinds=merge_patch_kinds,
    )


# -------------------------
# Main Entry Point
# -------------------------
def main():
    """主入口点"""
    parser = create_argument_parser()
    args = parser.parse_args()

    # 显示帮助信息
    if args.help or not args.command:
        show_examples()
        if not args.help:
            parser.print_help()
        sys.exit(0)

    # 设置日志级别
    if args.debug:
        logger.logger.setLevel(logging.DEBUG)
        logger.info("调试模式已启用")

    try:
        if args.command == 'backup':
            logger.info("开始执行备份操作...")
            backup_config = validate_backup_arguments(args)
            if not backup_config:
                logger.error("备份参数验证失败")
                sys.exit(1)

            backup_manager = KubernetesBackupManager(backup_config)
            success = backup_manager.execute_backup()

            if success:
                logger.info("备份操作成功完成")
            else:
                logger.error("备份操作完成，但存在错误")
                sys.exit(1)

        elif args.command == 'restore':
            logger.info("开始执行恢复操作...")
            restore_config = validate_restore_arguments(args)
            if not restore_config:
                logger.error("恢复参数验证失败")
                sys.exit(1)

            restore_manager = KubernetesRestoreManager(restore_config)
            success = restore_manager.execute_restore()

            if success:
                logger.info("恢复操作成功完成")
            else:
                logger.error("恢复操作完成，但存在错误")
                sys.exit(1)

        else:
            logger.error(f"未知命令: {args.command}")
            parser.print_help()
            sys.exit(1)

    except KeyboardInterrupt:
        logger.warning("操作被用户中断")
        sys.exit(1)
    except (SystemExit, KeyboardInterrupt):
        # 重新抛出系统退出和中断异常
        raise
    except config.ConfigException as e:
        logger.error(f"Kubernetes 配置无效（请检查 --kubeconfig、--context 及文件内容）: {e}")
        sys.exit(1)
    except Exception as e:
        # 顶级异常处理器，处理意外错误
        logger.error(f"操作失败，发生意外错误: {e}", exc_info=args.debug)
        sys.exit(1)


if __name__ == "__main__":
    main()
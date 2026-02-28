#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
k8s_backup.py - 企业级 Kubernetes 配置备份和恢复工具

支持功能：
- 备份 Kubernetes 集群资源（Deployments、Services、ConfigMaps、Secrets 等）
- 恢复备份到目标集群
- 支持命名空间映射、镜像映射、环境变量映射
- 自动处理资源依赖关系和恢复顺序
- 符合 Kubernetes 官方最佳实践

Python 3.7+ 兼容
"""

import argparse
import logging
import os
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
    """Configuration for restore operations"""
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


@dataclass
class TransformationRule:
    """Rule for transforming resources during restore"""
    namespace_mapping: Dict[str, str] = field(default_factory=dict)
    image_mapping: Dict[str, str] = field(default_factory=dict)
    env_mapping: Dict[str, str] = field(default_factory=dict)

    def transform_namespace(self, original_ns: str) -> str:
        """Transform namespace according to mapping rules"""
        return self.namespace_mapping.get(original_ns, original_ns)

    def transform_image(self, original_image: str) -> str:
        """Transform container image according to mapping rules"""
        for old_prefix, new_prefix in self.image_mapping.items():
            if original_image.startswith(old_prefix):
                return original_image.replace(old_prefix, new_prefix, 1)
        return original_image

    def transform_env_value(self, env_name: str, original_value: str) -> Optional[str]:
        """
        转换环境变量值。
        
        基于环境变量 key 的精确映射，符合 Kubernetes 和 Python 最佳实践：
        - 格式: "ENV_KEY:new_value" 表示将 ENV_KEY 的值替换为 new_value
        - 如果映射值为空字符串 ""，表示删除该环境变量（返回 None）
        - 如果原资源中不存在该 key，映射会新增该环境变量
        
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
        # 重试服务器错误（5xx）和速率限制错误（429）
        return (500 <= status < 600) or status == 429
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
        dry_run: 是否为模拟运行
        
    Raises:
        IOError: 文件写入失败
        yaml.YAMLError: YAML 序列化失败
    """
    try:
        dirpath = os.path.dirname(filepath) or "."
        ensure_directory(dirpath)
        if dry_run:
            logger.info(f"[模拟运行] 将写入 YAML 文件 {filepath}")
            return

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


def parse_mapping(mapping_str: str) -> Dict[str, str]:
    """
    从字符串格式解析命名空间/镜像映射。
    
    Args:
        mapping_str: 映射字符串，格式为 "key1:value1,key2:value2"
        
    Returns:
        Dict[str, str]: 解析后的映射字典
    """
    mapping = {}
    if mapping_str:
        for item in mapping_str.split(','):
            if ':' in item:
                key, value = item.split(':', 1)
                mapping[key.strip()] = value.strip()
    return mapping


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
        with open(metadata_file, 'r', encoding='utf-8') as f:
            return json.load(f)
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
        
        # loadBalancerIP 已弃用，改为 loadBalancerClass
        # 两者都移除以提高可移植性
        spec.pop('loadBalancerIP', None)
        spec.pop('loadBalancerClass', None)

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

        if 'apiVersion' not in resource:
            resource['apiVersion'] = 'v1'
        if 'kind' not in resource:
            resource['kind'] = 'Resource'

        return resource


# -------------------------
# Resource Transformer (新增)
# -------------------------
class ResourceTransformer:
    """Transform resources during restore for target environment"""

    def __init__(self, transformation_rule: TransformationRule):
        self.rule = transformation_rule

    def transform_resource(self, resource: Dict) -> Dict:
        """Apply all transformations to a resource"""
        if not isinstance(resource, dict):
            return resource

        # Transform namespace
        resource = self._transform_namespace(resource)

        # Transform container images
        resource = self._transform_container_images(resource)

        # Transform environment variables
        resource = self._transform_env_variables(resource)

        return resource

    def _transform_namespace(self, resource: Dict) -> Dict:
        """Transform namespace in resource metadata and references"""
        metadata = resource.get('metadata', {})
        current_ns = metadata.get('namespace')

        if current_ns and current_ns in self.rule.namespace_mapping:
            new_ns = self.rule.transform_namespace(current_ns)
            metadata['namespace'] = new_ns
            resource['metadata'] = metadata

        # Handle namespace references in role bindings, network policies, etc.
        self._transform_namespace_references(resource)

        return resource

    def _transform_namespace_references(self, resource: Dict):
        """Transform namespace references in various resource types"""
        kind = resource.get('kind', '').lower()

        if kind == 'rolebinding':
            subjects = resource.get('subjects', [])
            for subject in subjects:
                if subject.get('kind') in ['ServiceAccount', 'User', 'Group']:
                    ns = subject.get('namespace')
                    if ns and ns in self.rule.namespace_mapping:
                        subject['namespace'] = self.rule.transform_namespace(ns)

        elif kind == 'networkpolicy':
            # Handle ingress/egress namespace selectors
            self._transform_network_policy_namespaces(resource)

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
        """Transform namespace selector match expressions"""
        if 'matchExpressions' in selector:
            for expr in selector['matchExpressions']:
                if expr.get('key') == 'kubernetes.io/metadata.name':
                    values = expr.get('values', [])
                    new_values = [self.rule.transform_namespace(v) for v in values]
                    expr['values'] = new_values

    def _transform_container_images(self, resource: Dict) -> Dict:
        """Transform container images in pod specs"""
        if not self.rule.image_mapping:
            return resource

        spec = resource.get('spec', {})
        template = spec.get('template', {})
        template_spec = template.get('spec', {}) if template else {}

        # Transform init containers
        for container in template_spec.get('initContainers', []):
            if 'image' in container:
                container['image'] = self.rule.transform_image(container['image'])

        # Transform regular containers
        for container in template_spec.get('containers', []):
            if 'image' in container:
                container['image'] = self.rule.transform_image(container['image'])

        return resource

    def _transform_env_variables(self, resource: Dict) -> Dict:
        """
        转换容器中的环境变量。
        
        根据 env_mapping 规则：
        1. 如果映射中存在 key，则替换其值
        2. 如果映射值为空字符串 ""，则删除该环境变量
        3. 如果原资源中不存在该 key，则新增该环境变量
        
        Args:
            resource: Kubernetes 资源字典
            
        Returns:
            Dict: 转换后的资源字典
        """
        if not self.rule.env_mapping:
            return resource

        spec = resource.get('spec', {})
        template = spec.get('template', {})
        template_spec = template.get('spec', {}) if template else {}

        containers = template_spec.get('containers', []) + template_spec.get('initContainers', [])

        for container in containers:
            env_vars = container.get('env', [])
            if not env_vars:
                env_vars = []
                container['env'] = env_vars
            
            # 处理现有环境变量
            env_vars_to_remove = []
            for env_var in env_vars:
                env_name = env_var.get('name', '')
                if not env_name:
                    continue
                
                # 检查是否有映射
                if env_name in self.rule.env_mapping:
                    new_value = self.rule.env_mapping[env_name]
                    
                    # 如果映射值为空字符串，标记为删除
                    if new_value == "":
                        env_vars_to_remove.append(env_var)
                        continue
                    
                    # 更新值（支持 value 和 valueFrom 两种格式）
                    if 'value' in env_var:
                        env_var['value'] = new_value
                    elif 'valueFrom' in env_var:
                        # 如果原来是 valueFrom，改为 value（简化处理）
                        env_var.pop('valueFrom', None)
                        env_var['value'] = new_value
                    else:
                        # 如果既没有 value 也没有 valueFrom，添加 value
                        env_var['value'] = new_value
            
            # 删除标记的环境变量
            for env_var in env_vars_to_remove:
                env_vars.remove(env_var)
            
            # 添加新环境变量（映射中存在但原资源中不存在的 key）
            existing_env_names = {env_var.get('name', '') for env_var in env_vars}
            for env_key, env_value in self.rule.env_mapping.items():
                # 跳过空值映射（删除操作已在上面处理）
                if env_value == "":
                    continue
                # 如果环境变量不存在，添加它
                if env_key not in existing_env_names:
                    env_vars.append({
                        'name': env_key,
                        'value': env_value
                    })

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
        
        try:
            if not kind:
                raise ValueError("资源缺少 'kind' 字段")
            
            metadata = resource.get('metadata', {})
            if not metadata:
                raise ValueError("资源缺少 'metadata' 字段")
            
            namespace = metadata.get('namespace')
            name = metadata.get('name', '')
            
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
            output_path = self.get_output_path(cleaned_resource, output_dir)
            write_yaml_safely(cleaned_resource, output_path, dry_run=self.config.dry_run)
            if not self.config.dry_run:
                kind = cleaned_resource.get('kind', 'Unknown')
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
            ensure_directory(output_dir)

            # 保存备份元数据
            metadata = {
                'backup_name': self.config.backup_name,
                'timestamp': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                'namespace': self.config.namespace,
                'resources': self.config.resources,
                'include_crds': self.config.include_crds,
                'cluster_info': self._get_cluster_info()
            }

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

            normalized = []
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
                        normalized_items = []
                        for item in items:
                            if hasattr(item, 'to_dict'):
                                normalized_items.append(item.to_dict())
                            else:
                                normalized_items.append(item)

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
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        content = yaml.safe_load(f)

                    if not content or not all(key in content for key in ['apiVersion', 'kind', 'metadata']):
                        logger.warning(f"Invalid or empty Kubernetes resource in {yaml_file}")
                        success = False
                        continue

                    if 'name' not in content['metadata']:
                        logger.warning(f"Resource missing name in {yaml_file}")
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
                env_mapping=restore_config.env_mapping
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

            # 如果需要，创建命名空间
            if self.config.create_namespaces and not self.config.dry_run:
                logger.info("检查并创建缺失的命名空间...")
                self._create_missing_namespaces()

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

    def _create_missing_namespaces(self):
        """
        Create namespaces that are referenced in the backup but don't exist.
        Uses server-side apply for idempotent namespace creation (Kubernetes best practice).
        """
        try:
            existing_namespaces = set(self.k8s_client.list_namespaces())
            backup_namespaces = self._discover_backup_namespaces()

            for ns in backup_namespaces:
                target_ns = self.transformer.rule.transform_namespace(ns)
                if target_ns != 'cluster-scoped':
                    # Use apply instead of create for idempotency
                    # This handles both creation and existing namespace cases gracefully
                    if target_ns not in existing_namespaces:
                        logger.info(f"Applying namespace: {target_ns}")
                    else:
                        logger.debug(f"Namespace {target_ns} already exists, skipping")
                        continue
                    
                    try:
                        self.k8s_client.apply_namespace(target_ns)
                        existing_namespaces.add(target_ns)
                    except (ApiException, ResourceNotFoundError, IOError) as e:
                        logger.warning(f"Failed to apply namespace {target_ns}: {e}")
        except (ApiException, IOError, KeyError) as e:
            logger.warning(f"Failed to create namespaces: {e}")

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
                with open(yaml_file, 'r', encoding='utf-8') as f:
                    resource = yaml.safe_load(f)

                if resource and 'metadata' in resource:
                    ns = resource['metadata'].get('namespace')
                    if ns:
                        namespaces.add(ns)
            except (IOError, yaml.YAMLError, KeyError) as e:
                logger.warning(f"Failed to read backup file {yaml_file}: {e}")

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
            'rolebinding': 3,
            'ingress': 3,
            'deployment': 4,
            'daemonset': 4,
            'statefulset': 4,
            'job': 4,
            'cronjob': 4,
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
        从 YAML 文件恢复单个资源，使用 server-side apply。
        这遵循 Kubernetes 最佳实践，实现幂等资源管理。
        
        根据 Kubernetes 官方最佳实践：
        - 使用 server-side apply 而不是 create/update
        - 自动处理字段冲突（通过 force=True）
        - 移除集群特定字段（finalizers, ownerReferences）
        - 使用 field_manager 标识资源所有者
        
        参考: https://kubernetes.io/docs/reference/using-api/server-side-apply/
        
        Args:
            yaml_file: YAML 文件路径
            cluster_scoped: 是否为集群级资源
        """
        self.restore_stats['total_resources'] += 1
        
        try:
            with open(yaml_file, 'r', encoding='utf-8') as f:
                resource = yaml.safe_load(f)

            if not resource:
                logger.warning(f"Empty resource in {yaml_file}")
                self.restore_stats['failed_restores'] += 1
                return

            # Transform resource for target environment
            transformed_resource = self.transformer.transform_resource(resource)

            # Get resource API version and kind
            api_version = transformed_resource.get('apiVersion', 'v1')
            kind = transformed_resource.get('kind', '')

            if not kind:
                logger.warning(f"Resource missing kind in {yaml_file}")
                self.restore_stats['failed_restores'] += 1
                return

            # Ensure cluster-specific fields are removed (best practice for restore)
            # These fields reference cluster-specific resources and should not be restored
            metadata = transformed_resource.get('metadata', {})
            
            # Remove finalizers (reference cluster-specific controllers)
            if 'finalizers' in metadata:
                metadata.pop('finalizers', None)
                logger.debug(f"已移除 {kind} {metadata.get('name', 'unknown')} 的 finalizers")
            
            # Remove ownerReferences (reference cluster-specific parent resources)
            if 'ownerReferences' in metadata:
                metadata.pop('ownerReferences', None)
                logger.debug(f"已移除 {kind} {metadata.get('name', 'unknown')} 的 ownerReferences")

            name = metadata.get('name', '')
            namespace = metadata.get('namespace') if not cluster_scoped else None

            if self.config.dry_run:
                logger.info(f"[Dry-run] Would restore {kind} {name} in namespace {namespace}")
                self.restore_stats['successful_restores'] += 1
                return

            # 使用 server-side apply 而不是 create 进行幂等资源管理
            # 这是 Kubernetes 最佳实践推荐的方法
            try:
                self.k8s_client.apply_resource(transformed_resource)
                ns_info = f"命名空间 {namespace}" if namespace else "集群级"
                logger.info(f"已恢复 {kind}/{name} ({ns_info})")
                self.restore_stats['successful_restores'] += 1

            except ApiException as e:
                # 处理特定的 API 错误
                if e.status == 404:
                    logger.error(f"资源类型 {api_version}/{kind} 在集群中不存在: {e}")
                    self.restore_stats['failed_restores'] += 1
                elif e.status == 403:
                    logger.error(f"恢复 {kind}/{name} 时权限被拒绝: {e}")
                    self.restore_stats['failed_restores'] += 1
                elif e.status == 422:
                    logger.error(f"资源 {kind}/{name} 验证错误: {e}")
                    self.restore_stats['failed_restores'] += 1
                else:
                    logger.error(f"恢复 {kind}/{name} 失败: HTTP {e.status} - {e}")
                    self.restore_stats['failed_restores'] += 1
            except ResourceNotFoundError as e:
                logger.error(f"Resource type {api_version}/{kind} not found: {e}")
                self.restore_stats['failed_restores'] += 1
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                logger.error(f"Unexpected error restoring {kind} {name}: {e}")
                self.restore_stats['failed_restores'] += 1

        except yaml.YAMLError as e:
            logger.error(f"Failed to parse YAML file {yaml_file}: {e}")
            self.restore_stats['failed_restores'] += 1
        except IOError as e:
            logger.error(f"Failed to read file {yaml_file}: {e}")
            self.restore_stats['failed_restores'] += 1
        except (KeyError, AttributeError, TypeError) as e:
            logger.error(f"Failed to restore resource from {yaml_file}: {e}")
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
    """显示详细使用示例和说明"""
    examples = """
使用示例:

1. 备份单个命名空间:
   python k8sbackup.py backup \\
        --namespace default \\
        --output-dir /opt/k8s-backup
   # 说明: 备份 default 命名空间中的所有默认资源类型
   #       备份文件将保存在 /opt/k8s-backup/backup-YYYYMMDD-HHMMSS-default/ 目录下

2. 备份所有命名空间（包括 CRD）:
   python k8sbackup.py backup \\
        --all-namespaces \\
        --include-crds \\
        --output-dir /opt/k8s-backup \\
        --tar
   # 说明: --all-namespaces 备份所有命名空间
   #       --include-crds 包含自定义资源定义及其实例
   #       --tar 创建压缩的 tar.gz 归档文件

3. 备份指定资源类型:
   python k8sbackup.py backup \\
        --namespace production \\
        --resources "deployments,services,configmaps,secrets" \\
        --output-dir /opt/k8s-backup
   # 说明: 只备份指定的资源类型，多个资源类型用逗号分隔

4. 使用标签选择器备份:
   python k8sbackup.py backup \\
        --namespace default \\
        --label-selector "app=myapp,env=production" \\
        --output-dir /opt/k8s-backup
   # 说明: 只备份匹配标签选择器的资源

5. 恢复备份（基本用法）:
   python k8sbackup.py restore \\
        --backup-dir /opt/k8s-backup/backup-20231201-120000-default \\
        --create-namespaces
   # 说明: --backup-dir 指定备份目录路径
   #       --create-namespaces 自动创建不存在的命名空间

6. 恢复备份并映射命名空间:
   python k8sbackup.py restore \\
        --backup-dir /opt/k8s-backup/backup-dev \\
        --namespace-mapping "dev:prod,test:prod" \\
        --create-namespaces
   # 说明: 将备份中的 dev 和 test 命名空间映射到 prod 命名空间
   #       格式: "old1:new1,old2:new2"

7. 恢复备份并映射容器镜像:
   python k8sbackup.py restore \\
        --backup-dir /opt/k8s-backup/backup-app \\
        --image-mapping "old-registry.io:new-registry.io,dev-tag:prod-tag" \\
        --create-namespaces
   # 说明: 将镜像从旧注册表映射到新注册表，或替换镜像标签
   #       格式: "old-prefix:new-prefix,old-tag:new-tag"

8. 恢复备份并映射环境变量:
   python k8sbackup.py restore \\
        --backup-dir /opt/k8s-backup/backup-full \\
        --env-mapping "DB_HOST:prod-db.example.com,API_URL:https://api.prod.com" \\
        --create-namespaces
   # 说明: 基于环境变量 key 的精确映射，符合 Kubernetes 最佳实践
   #       格式: "ENV_KEY:new_value" - 将 ENV_KEY 的值替换为 new_value
   #       如果原资源中不存在该 key，会新增该环境变量
   #       使用 "ENV_KEY:" 可以删除该环境变量

9. 预览恢复操作（不实际执行）:
   python k8sbackup.py restore \\
        --backup-dir /opt/k8s-backup/backup-test \\
        --dry-run \\
        --create-namespaces
   # 说明: --dry-run 模拟执行，不会实际创建或修改资源
   #       用于验证恢复操作是否正常

10. 恢复时跳过 CRD 和集群级资源:
    python k8sbackup.py restore \\
         --backup-dir /opt/k8s-backup/backup-minimal \\
         --skip-crds \\
         --skip-cluster-scoped \\
         --create-namespaces
    # 说明: --skip-crds 跳过自定义资源定义
    #       --skip-cluster-scoped 跳过集群级资源（如 StorageClass、ClusterRole 等）

11. 使用自定义 kubeconfig 和上下文:
    python k8sbackup.py backup \\
         --namespace default \\
         --kubeconfig ~/.kube/config-prod \\
         --context production-cluster \\
         --output-dir /opt/k8s-backup
    # 说明: --kubeconfig 指定 kubeconfig 文件路径
    #       --context 指定要使用的 Kubernetes 上下文

12. 启用调试日志:
    python k8sbackup.py backup \\
         --namespace default \\
         --debug \\
         --output-dir /opt/k8s-backup
    # 说明: --debug 启用详细的调试日志输出

参数说明:
  backup 子命令:
    -n, --namespace NAME           指定要备份的命名空间
    --all-namespaces               备份所有命名空间
    -r, --resources LIST           要备份的资源类型列表（逗号分隔）
    -o, --output-dir PATH          备份输出目录（默认: /opt/k8s-backup）
    --include-crds                 包含自定义资源定义及其实例
    --tar                          创建压缩的 tar.gz 归档文件
    --max-workers NUM              并行工作线程数（默认: 5）
    --label-selector SELECTOR     标签选择器过滤资源
    --field-selector SELECTOR      字段选择器过滤资源
    --backup-name NAME             自定义备份名称（默认: 自动生成）

  restore 子命令:
    --backup-dir PATH              备份目录路径（必需）
    --namespace-mapping MAP         命名空间映射（格式: "old1:new1,old2:new2"）
    --image-mapping MAP             容器镜像映射（格式: "old-prefix:new-prefix"）
    --env-mapping MAP               环境变量映射（格式: "ENV_KEY:new_value"）
                                    基于环境变量 key 的精确映射：
                                    - "DB_HOST:prod-db.example.com" 替换 DB_HOST 的值
                                    - "ENV_KEY:" 或 "ENV_KEY:" 删除该环境变量
                                    - 如果原资源中不存在该 key，会新增该环境变量
    --max-workers NUM              并行工作线程数（默认: 5）
    --skip-crds                    跳过自定义资源定义
    --skip-cluster-scoped          跳过集群级资源
    --create-namespaces            自动创建不存在的命名空间
    --backup-name NAME             指定要恢复的备份名称（如果备份目录包含多个备份）

  通用参数:
    --kubeconfig PATH              kubeconfig 文件路径
    --context NAME                  Kubernetes 上下文名称
    --debug                         启用调试日志
    --dry-run                       模拟执行，不实际修改资源

注意事项:
  - 备份前请确保有足够的磁盘空间
  - 备份过程中会清理集群特定的元数据（如 UID、resourceVersion 等）
  - 恢复时会自动处理资源依赖关系（CRD 先于 CR，ConfigMap/Secret 先于 Deployment 等）
  - 使用 server-side apply 进行资源恢复，符合 Kubernetes 最佳实践
  - 恢复时会自动移除 finalizers，避免资源卡在删除状态
  - 建议在生产环境使用前先用 --dry-run 测试
  - 确保有足够的 RBAC 权限执行备份和恢复操作
  - 备份文件包含敏感信息（如 Secrets），请妥善保管
  - 支持 Python 3.7+，需要安装 kubernetes、pyyaml、tenacity 等依赖包
"""
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
        help='并行工作线程数（默认: 5）'
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
        help='命名空间映射（格式: "old1:new1,old2:new2"）'
    )
    parser.add_argument(
        '--image-mapping',
        help='容器镜像映射（格式: "old-prefix:new-prefix,old-tag:new-tag"）'
    )
    parser.add_argument(
        '--env-mapping',
        help='环境变量映射（格式: "ENV_KEY:new_value"）。基于环境变量 key 的精确映射：'
             ' "DB_HOST:prod-db.example.com" 替换 DB_HOST 的值；'
             ' "ENV_KEY:" 删除该环境变量；'
             ' 如果原资源中不存在该 key，会新增该环境变量'
    )
    parser.add_argument(
        '--max-workers',
        type=int,
        default=5,
        help='并行工作线程数（默认: 5）'
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
        '--create-namespaces',
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
        subparser.add_argument(
            '--dry-run',
            action='store_true',
            help='模拟执行，不实际修改资源'
        )

    return parser


def validate_backup_arguments(args) -> Optional[BackupConfig]:
    """验证备份命令行参数"""
    if not args.namespace and not args.all_namespaces:
        logger.error("必须指定 --namespace 或 --all-namespaces 参数之一")
        return None

    if args.all_namespaces:
        namespace = 'all'
    else:
        namespace = args.namespace

    resources = [r.strip() for r in args.resources.split(',') if r.strip()]
    if not resources:
        logger.error("必须指定至少一个资源类型")
        return None

    return BackupConfig(
        kubeconfig=args.kubeconfig,
        context=args.context,
        namespace=namespace,
        resources=resources,
        output_dir=args.output_dir,
        include_crds=args.include_crds,
        create_tarball=args.tar,
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        label_selector=args.label_selector,
        field_selector=args.field_selector,
        backup_name=args.backup_name
    )


def validate_restore_arguments(args) -> Optional[RestoreConfig]:
    """验证恢复命令行参数"""
    if not args.backup_dir:
        logger.error("恢复操作必须指定 --backup-dir 参数")
        return None

    if not os.path.exists(args.backup_dir):
        logger.error(f"备份目录不存在: {args.backup_dir}")
        return None

    if not os.path.isdir(args.backup_dir):
        logger.error(f"指定的路径不是目录: {args.backup_dir}")
        return None

    return RestoreConfig(
        kubeconfig=args.kubeconfig,
        context=args.context,
        backup_dir=args.backup_dir,
        namespace_mapping=parse_mapping(args.namespace_mapping),
        image_mapping=parse_mapping(args.image_mapping),
        env_mapping=parse_mapping(args.env_mapping),
        max_workers=args.max_workers,
        dry_run=args.dry_run,
        skip_crds=args.skip_crds,
        skip_cluster_scoped=args.skip_cluster_scoped,
        create_namespaces=args.create_namespaces,
        backup_name=args.backup_name
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
    except Exception as e:
        # 顶级异常处理器，处理意外错误
        logger.error(f"操作失败，发生意外错误: {e}", exc_info=args.debug)
        sys.exit(1)


if __name__ == "__main__":
    main()
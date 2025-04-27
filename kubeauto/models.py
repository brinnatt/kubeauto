"""
Data models for kubeauto
"""
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

@dataclass
class Cluster:
    name: str
    config: Dict[str, Any]
    hosts: List[str]
    kubeconfig: Optional[str] = None

@dataclass
class Node:
    ip: str
    role: str  # 'etcd', 'master', 'node'
    cluster: str
    extra_info: Optional[Dict[str, str]] = None

@dataclass
class KubeConfigUser:
    name: str
    user_type: str  # 'admin' or 'view'
    expiry: str
    cert_path: str
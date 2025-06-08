"""
Custom exceptions for kubeauto
"""

class KubeautoError(Exception):
    """Base exception for kubeauto"""
    pass

class ClusterExistsError(KubeautoError):
    """Cluster already exists"""
    pass

class ClusterNewError(KubeautoError):
    """Cluster newly created"""
    pass

class ClusterNotFoundError(KubeautoError):
    """Cluster not found"""
    pass

class InvalidIPError(KubeautoError):
    """Invalid IP address"""
    pass

class NodeExistsError(KubeautoError):
    """Node already exists"""
    pass

class NodeNotFoundError(KubeautoError):
    """Node not found"""
    pass

class DownloadError(KubeautoError):
    """Required binary not found"""
    pass

class CommandExecutionError(KubeautoError):
    """Command execution failed"""
    pass
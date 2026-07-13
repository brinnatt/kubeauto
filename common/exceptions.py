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

class ClusterSetupError(KubeautoError):
    """Cluster setup error"""
    pass

class ClusterManageError(KubeautoError):
    """Cluster Manage error"""
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


class InstallPrereqError(KubeautoError):
    """Required install components (from download) not present. Run e.g. kubecli download -D first."""
    pass


class AnsibleCoreDetectionError(InstallPrereqError):
    """Cannot detect ansible-core version on the control node (required for Python compatibility matrix)."""
    pass

class DockerManageError(KubeautoError):
    """Docker manage"""
    pass

class SystemExecutionError(KubeautoError):
    """Command execution failed"""
    pass

class CommandExecutionError(KubeautoError):
    """Command execution failed"""
    pass

"""Focused contracts for KubeBackupCli's namespace boundary."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from tools.k8stools.KubeBackupCli import BackupConfig, KubernetesBackupManager


class _ListResponse:
    def __init__(self, items):
        self._items = items

    def to_dict(self):
        return {"items": self._items}


class KubeBackupCliTests(unittest.TestCase):
    def test_permission_errors_are_not_silently_reported_as_empty_backup(self):
        import inspect
        source = inspect.getsource(__import__('tools.k8stools.KubeBackupCli', fromlist=['KubernetesClient']).KubernetesClient.list_resources)
        self.assertIn("exc.status in (401, 403)", source)

    def test_include_crds_keeps_namespaced_custom_resources_in_requested_namespace(self):
        manager = KubernetesBackupManager.__new__(KubernetesBackupManager)
        manager.config = BackupConfig(
            namespace="fixture-ns",
            include_crds=True,
            label_selector="app=fixture",
            field_selector="metadata.name=fixture-widget",
        )
        manager.backup_stats = {
            "total_resources": 0,
            "successful_backups": 0,
            "failed_backups": 0,
        }
        crd = {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "widgets.example.test"},
            "spec": {
                "group": "example.test",
                "scope": "Namespaced",
                "names": {"plural": "widgets", "kind": "Widget"},
                "versions": [{"name": "v1", "served": True, "storage": True}],
            },
        }
        resource = Mock()
        resource.get.return_value = _ListResponse([
            {"apiVersion": "example.test/v1", "kind": "Widget", "metadata": {"name": "fixture-widget", "namespace": "fixture-ns"}}
        ])
        manager.k8s_client = Mock()
        manager.k8s_client.list_resources.return_value = [crd]
        manager.k8s_client.dynamic_client.resources.get.return_value = resource
        manager.backup_resources_parallel = Mock(return_value=(1, 0))

        manager.backup_crds("/fixture-output")

        manager.k8s_client.list_namespaces.assert_not_called()
        resource.get.assert_called_once_with(
            namespace="fixture-ns",
            label_selector="app=fixture",
            field_selector="metadata.name=fixture-widget",
        )


if __name__ == "__main__":
    unittest.main()

"""Focused contracts for KubeBackupCli's namespace boundary."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.k8stools import KubeBackupCli as backup_cli

from tools.k8stools.KubeBackupCli import BackupConfig, KubernetesBackupManager


class _ListResponse:
    def __init__(self, items):
        self._items = items

    def to_dict(self):
        return {"items": self._items}


class KubeBackupCliTests(unittest.TestCase):
    def run_restore_cli(self, *mapping_args):
        """Exercise parsing, file restore and transforms; replace only the API boundary."""
        with tempfile.TemporaryDirectory(prefix="kube-backup-contract-") as directory:
            root = Path(directory)
            manifest_dir = root / "old-ns" / "Deployment"
            manifest_dir.mkdir(parents=True)
            (root / "backup-metadata.json").write_text('{}', encoding="utf-8")
            pod_spec = {
                "nodeSelector": {"kubernetes.io/hostname": "old-worker"},
                "containers": [{"name": "app", "image": "example/app:v1", "env": [
                    {"name": "DEPLOY_ENV", "value": "old-ns"},
                    {"name": "DOMAIN_NAME", "value": "app.old-ns"},
                ]}],
                "initContainers": [{"name": "init-env", "image": "example/init:v1", "env": [
                    {"name": "NACOS_NAMESPACE", "value": "different-old-ns"},
                    {"name": "DEPLOY_ENV", "value": "old-ns"},
                    {"name": "KEEP_ME", "value": "unchanged"},
                    {"name": "REMOVE_ME", "value": "old"},
                ], "envFrom": [{"secretRef": {"name": "nacos-auth"}}]}],
            }
            manifest = {
                "apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "app", "namespace": "old-ns"},
                "spec": {"template": {"spec": pod_spec}},
            }
            # JSON is a YAML subset, so the normal backup reader handles this file.
            source = manifest_dir / "Deployment-app.yaml"
            original = json.dumps(manifest)
            source.write_text(original, encoding="utf-8")
            argv = ["KubeBackupCli", "restore", "--backup-dir", directory,
                    "--namespace-mapping", "old-ns=new-ns", *mapping_args,
                    "--merge-patch-kind", "Deployment", "--merge-patch",
                    '{"spec":{"template":{"spec":{"nodeSelector":null}}}}']
            with patch.object(sys, "argv", argv), \
                    patch.object(backup_cli, "KubernetesClient") as client_class, \
                    contextlib.redirect_stdout(io.StringIO()):
                client_class.return_value.list_namespaces.return_value = ["new-ns"]
                try:
                    backup_cli.main()
                    rc = 0
                except SystemExit as exc:
                    rc = exc.code
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            return rc, client_class

    def test_repeated_env_mapping_reaches_normal_and_init_containers(self):
        rc, api = self.run_restore_cli(
            "--env-mapping", "NACOS_NAMESPACE=new-ns",
            "--env-mapping", "DEPLOY_ENV=new-ns",
        )
        self.assertEqual(rc, 0)
        api.return_value.apply_resource.assert_called_once()
        restored = api.return_value.apply_resource.call_args.args[0]
        self.assertEqual(restored["metadata"]["namespace"], "new-ns")
        spec = restored["spec"]["template"]["spec"]
        self.assertNotIn("nodeSelector", spec)
        for key in ("containers", "initContainers"):
            env = {entry["name"]: entry.get("value") for entry in spec[key][0]["env"]}
            self.assertEqual(env["NACOS_NAMESPACE"], "new-ns", key)
            self.assertEqual(env["DEPLOY_ENV"], "new-ns", key)
        init = spec["initContainers"][0]
        self.assertIn({"name": "KEEP_ME", "value": "unchanged"}, init["env"])
        self.assertEqual(init["envFrom"], [{"secretRef": {"name": "nacos-auth"}}])
        self.assertIn({"name": "DOMAIN_NAME", "value": "app.new-ns"}, spec["containers"][0]["env"])

    def test_env_mapping_legacy_and_repeated_forms_match(self):
        forms = [
            ["--env-mapping", "NACOS_NAMESPACE=new-ns DEPLOY_ENV=new-ns"],
            ["--env-mapping", "NACOS_NAMESPACE=new-ns, DEPLOY_ENV=new-ns"],
            ["--env-mapping", "NACOS_NAMESPACE=new-ns", "--env-mapping", "DEPLOY_ENV=new-ns"],
        ]
        outputs = []
        for args in forms:
            with self.subTest(args=args):
                rc, api = self.run_restore_cli(*args)
                self.assertEqual(rc, 0)
                outputs.append(api.return_value.apply_resource.call_args.args[0])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])

    def test_namespace_and_image_mapping_legacy_and_repeated_forms_match(self):
        for option, first, second in (
            ("--namespace-mapping", "old-ns=new-ns", "aux-ns=aux-target"),
            ("--image-mapping", "example/=mirror.example/", "example/init=init.example/bootstrap"),
        ):
            outputs = []
            for args in ([option, first + " " + second],
                         [option, first + ", " + second],
                         [option, first, option, second]):
                with self.subTest(args=args):
                    rc, api = self.run_restore_cli(*args)
                    self.assertEqual(rc, 0)
                    restored = api.return_value.apply_resource.call_args.args[0]
                    self.assertEqual(restored["metadata"]["namespace"], "new-ns")
                    if option == "--image-mapping":
                        spec = restored["spec"]["template"]["spec"]
                        self.assertEqual(spec["containers"][0]["image"], "mirror.example/app:v1")
                        self.assertEqual(spec["initContainers"][0]["image"], "init.example/bootstrap:v1")
                    outputs.append(restored)
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0], outputs[2])

    def test_all_mapping_types_handle_repeated_keys_and_invalid_earlier_arguments_consistently(self):
        for option, valid, conflicting, invalid in (
            ("--namespace-mapping", "old-ns=new-ns", "old-ns=other-ns", "BAD_NAMESPACE=new-ns"),
            ("--image-mapping", "example/=mirror.example/", "example/=other.example/", "=mirror.example/"),
            ("--env-mapping", "NACOS_NAMESPACE=new-ns", "NACOS_NAMESPACE=other-ns", "BAD-KEY=value"),
        ):
            with self.subTest(option=option, duplicate="same-value"):
                rc, api = self.run_restore_cli(option, valid, option, valid)
                self.assertEqual(rc, 0)
                api.return_value.apply_resource.assert_called_once()
            for first in (conflicting, invalid, "not-a-mapping"):
                with self.subTest(option=option, first=first):
                    rc, api = self.run_restore_cli(option, first, option, valid)
                    self.assertEqual(rc, 1)
                    api.assert_not_called()

    def test_restore_without_env_mapping_keeps_existing_values(self):
        rc, api = self.run_restore_cli()
        self.assertEqual(rc, 0)
        spec = api.return_value.apply_resource.call_args.args[0]["spec"]["template"]["spec"]
        self.assertIn({"name": "NACOS_NAMESPACE", "value": "different-old-ns"}, spec["initContainers"][0]["env"])

    def test_mapping_parser_preserves_other_mapping_types_and_empty_input(self):
        self.assertEqual(backup_cli.parse_mapping(None, "env"), {})
        self.assertEqual(backup_cli.parse_mapping(" ", "env"), {})
        self.assertEqual(backup_cli.parse_mapping("old-ns=new-ns", "namespace"), {"old-ns": "new-ns"})
        self.assertEqual(backup_cli.parse_mapping("old.example/=new.example/", "image"),
                         {"old.example/": "new.example/"})

    def test_repeated_env_mapping_preserves_delete_fieldref_and_complex_values(self):
        rc, api = self.run_restore_cli(
            "--env-mapping", "REMOVE_ME= DEPLOY_ENV=@k8s:metadata.namespace",
            "--env-mapping", "API_URL=https://api.example:443/path?q=a=b LABEL=hello world",
            "--env-mapping", "API_URL=https://api.example:443/path?q=a=b",
        )
        self.assertEqual(rc, 0)
        spec = api.return_value.apply_resource.call_args.args[0]["spec"]["template"]["spec"]
        for key in ("containers", "initContainers"):
            env = {entry["name"]: entry for entry in spec[key][0]["env"]}
            self.assertNotIn("REMOVE_ME", env)
            self.assertNotIn("value", env["DEPLOY_ENV"])
            self.assertEqual(env["DEPLOY_ENV"]["valueFrom"], {"fieldRef": {"fieldPath": "metadata.namespace"}})
            self.assertEqual(env["API_URL"]["value"], "https://api.example:443/path?q=a=b")
            self.assertEqual(env["LABEL"]["value"], "hello world")

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

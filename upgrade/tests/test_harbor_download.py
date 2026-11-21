"""Official Harbor cache integrity and exclusive publication, without Docker."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from common.exceptions import DownloadError
from service.cluster import downloader


class HarborDownloadContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.content = b'complete verified official-package fixture'
        self.manager = object.__new__(downloader.DownloadManager)
        self.manager.image_dir = self.root
        self.manager.kube_constant = SimpleNamespace(arch='x86_64', v_harbor='v2.16.0')
        self.manager.docker = Mock(is_docker_installed=True)
        self.manager._DownloadManager__handle_image = Mock()
        self.destination = self.root/'harbor-offline-installer-v2.16.0.tgz'
        self.addCleanup(patch.stopall)
        # Fixture payload changes only the trusted fixture hash. The actual
        # streaming hash, cache checks and publication code are exercised.
        patch.dict(downloader._HARBOR_OFFICIAL_SHA256,
            {name: hashlib.sha256(self.content).hexdigest()
             for name in ['x86_64', 'amd64', 'aarch64', 'arm64']}).start()

    def extract(self, name, source, destination):
        self.assertTrue(name.startswith('kubeauto-harbor-download-'))
        self.assertEqual(source, '/harbor/harbor-offline-installer-v2.16.0.tgz')
        Path(destination).write_bytes(self.content)

    def test_both_architectures_publish_verified_bytes_then_reuse_without_docker(self):
        for arch in ['x86_64', 'aarch64']:
            with self.subTest(arch=arch):
                self.manager.kube_constant.arch = arch
                self.manager.docker.copy_from_container.side_effect = self.extract
                self.manager.get_harbor_offline_pkg()
                self.assertEqual(self.destination.read_bytes(), self.content)
                self.assertEqual(self.destination.stat().st_mode & 0o777, 0o644)
                self.assertEqual(list(self.root.glob('.harbor-download-*')), [])
                count = self.manager.docker.run_container.call_count
                self.manager.get_harbor_offline_pkg()
                self.assertEqual(self.manager.docker.run_container.call_count, count)
                self.destination.unlink()
        self.assertEqual(self.manager.docker.remove_container.call_count, 2)

    def test_bad_cache_symlink_or_directory_is_preserved_and_never_downloaded(self):
        self.destination.write_bytes(b'partial')
        with self.assertRaises(DownloadError): self.manager.get_harbor_offline_pkg()
        self.assertEqual(self.destination.read_bytes(), b'partial')
        self.destination.unlink()
        self.destination.symlink_to(self.root/'absent')
        with self.assertRaises(DownloadError): self.manager.get_harbor_offline_pkg()
        self.assertTrue(self.destination.is_symlink())
        self.destination.unlink(); self.destination.mkdir()
        with self.assertRaises(DownloadError): self.manager.get_harbor_offline_pkg()
        self.manager._DownloadManager__handle_image.assert_not_called()

    def test_corrupt_or_interrupted_extract_never_publishes_and_cleans_only_own_resources(self):
        for failure in ['corrupt', 'interrupted']:
            def extract(name, source, destination):
                Path(destination).write_bytes(b'partial')
                if failure == 'interrupted': raise RuntimeError('interrupted extraction')
            self.manager.docker.copy_from_container.side_effect = extract
            with self.assertRaises((DownloadError, RuntimeError)):
                self.manager.get_harbor_offline_pkg()
            self.assertFalse(self.destination.exists())
            self.assertEqual(list(self.root.glob('.harbor-download-*')), [])
            started = self.manager.docker.run_container.call_args.args[1]
            self.manager.docker.remove_container.assert_called_with(started)

    def test_concurrent_destination_is_not_overwritten(self):
        def extract(name, source, destination):
            self.extract(name, source, destination)
            self.destination.write_bytes(b'other owner')
        self.manager.docker.copy_from_container.side_effect = extract
        with self.assertRaises(FileExistsError): self.manager.get_harbor_offline_pkg()
        self.assertEqual(self.destination.read_bytes(), b'other owner')
        self.assertEqual(list(self.root.glob('.harbor-download-*')), [])

    def test_unknown_architecture_refuses_before_download(self):
        self.manager.kube_constant.arch = 'riscv64'
        with self.assertRaises(DownloadError): self.manager.get_harbor_offline_pkg()
        self.manager._DownloadManager__handle_image.assert_not_called()

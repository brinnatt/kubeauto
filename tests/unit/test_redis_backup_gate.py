"""Independent restore fixture and artifact boundary contracts."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_backup', ROOT / 'tests/helpers/redis-backup-gate.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class RedisBackupTests(unittest.TestCase):
    def test_customer_offline_path_is_complete_and_directory_is_shell_quoted(self):
        import subprocess
        text = (ROOT / 'docs/middleware/redis/operations-manual.md').read_text()
        path = Path('/tmp/space $(should-not-execute) `literal`')
        script = backup.offline_restore_script(text, path)
        subprocess.run(['bash','-n'],input=script,text=True,check=True)
        self.assertIn("backup_dir='/tmp/space $(should-not-execute) `literal`'", script)
        for bad in (text.replace('REDIS_OFFLINE_RDB_VERIFY', 'missing'), text + '\n' + text):
            with self.assertRaises(ValueError): backup.offline_restore_script(bad, path)

    def test_offline_cleanup_refuses_foreign_label_before_container_removal(self):
        source = Mock(run_id='a' * 32)
        source.run.side_effect = ['b' * 64, 'foreign']
        with self.assertRaisesRegex(ValueError, 'foreign container'):
            backup.cleanup_offline(source)
        self.assertFalse(any('rm' in c.args[0] for c in source.run.call_args_list))
        source.run.reset_mock(); source.run.side_effect = ['b' * 64, source.run_id, 'removed']
        backup.cleanup_offline(source)
        self.assertEqual(source.run.call_args.args[0], ['docker','rm','-f','b' * 64])

    def test_offline_zero_exit_without_business_marker_is_rejected_and_cleaned(self):
        source = SimpleNamespace(base=ROOT, run_id='a' * 32, value=b'fixture')
        with (patch.object(backup.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout='')),
              patch.object(backup, 'cleanup_offline') as cleanup):
            with self.assertRaisesRegex(ValueError, 'recovery path failed'):
                backup.verify_offline_restore(source, Path('/tmp/fixture'), 'probe', 5)
            cleanup.assert_called_once_with(source)

    def test_rdb_restart_requires_changed_process_and_retained_volume_owner(self):
        old = {'metadata': {'uid': 'target'}, 'status': {'containerStatuses': [{'restartCount': 0}]}}
        current = {'metadata': {'uid': 'target'}, 'status': {'containerStatuses': [{'restartCount': 1}]}}
        source, restored, gate = Mock(), Mock(), Mock()
        source.command.side_effect = [b'run_id:old\r\n', EOFError('shutdown complete')]
        restored.command.return_value = b'run_id:new\r\n'
        gate.get.side_effect = [old, current]
        gate.client.return_value = restored
        self.assertIs(backup.restart_rdb_target(gate, 'restore', source), restored)
        source.command.assert_any_call('SHUTDOWN', 'SAVE')
        source.command.side_effect = [b'run_id:old\r\n', EOFError('shutdown complete')]
        gate.get.side_effect = [old, dict(current, metadata={'uid': 'foreign'})]
        with self.assertRaisesRegex(ValueError, 'volume owner'):
            backup.restart_rdb_target(gate, 'restore', source)

    def test_restore_target_has_no_source_pvc_no_plain_port_and_secret_argv(self):
        doc = backup.restore_pod('redis-gate', 'redis-restore-run', 'a' * 32,
                                 'registry.talkschool.cn:5000/brinnatt/redis:8.10.2')
        self.assertFalse(doc['spec']['automountServiceAccountToken'])
        volumes = doc['spec']['volumes']
        self.assertFalse(any('persistentVolumeClaim' in v for v in volumes))
        self.assertEqual(volumes[0]['emptyDir']['sizeLimit'], '256Mi')
        command = doc['spec']['containers'][0]['command'][2]
        self.assertIn('--appendonly no', command)
        self.assertIn('--port 0 --tls-port 6379', command)
        self.assertIn('--tls-auth-clients yes', command)
        self.assertIn('--aclfile /acl/user.acl', command)
        self.assertLess(command.index('redis-check-rdb'), command.index('exec redis-server'))
        self.assertNotIn('--requirepass', command)
        self.assertEqual(doc['metadata']['labels']['kubeauto.io/test-run'], 'a' * 32)

    def test_artifact_rejects_tamper_symlink_path_escape_and_export_pass_substitution(self):
        data = b'REDIS' + b'fixed-fixture-data'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / 'replication.rdb'
            artifact.write_bytes(data)
            record = {'file': artifact.name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            manifest = {'mode': 'sentinel', 'files': [record], 'restore_verified': False}
            self.assertEqual(backup.verified_artifact(root, manifest), artifact)
            artifact.write_bytes(data[:-1] + b'x')
            with self.assertRaises(ValueError):
                backup.verified_artifact(root, manifest)
            artifact.unlink()
            (root / 'foreign.rdb').write_bytes(data)
            artifact.symlink_to(root / 'foreign.rdb')
            with self.assertRaises(ValueError):
                backup.verified_artifact(root, manifest)
            artifact.unlink()
            artifact.write_bytes(data)
            for change in ({'file': '../foreign.rdb'}, {'size': 0}, {'sha256': 'invalid'}):
                bad = dict(manifest, files=[dict(record, **change)])
                with self.assertRaises(ValueError):
                    backup.verified_artifact(root, bad)
            for change in ({'mode': 'cluster'}, {'files': []}, {'restore_verified': True}):
                with self.assertRaises(ValueError):
                    backup.verified_artifact(root, dict(manifest, **change))


if __name__ == '__main__':
    unittest.main()

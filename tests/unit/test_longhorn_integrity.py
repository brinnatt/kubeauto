"""Offline copy corruption, native export isolation and restoration ordering."""
from contextlib import ExitStack, redirect_stdout
import copy
import csv
import hashlib
import importlib.util
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('integrity', ROOT/'tests/helpers/longhorn-integrity.py')
I = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(I)

# Official engine 1.12.1 commit 1fa902f0061b84b92af445c38392bfeb99faffb2,
# package/launch-simple-longhorn. Preserve exact bytes for the wrapper contract.
OFFICIAL_EXPORT_HELPER = '''#!/bin/bash

set -x
set -e
mount --rbind /host/dev /dev

volume=$1
size=$2
frontend=$3

if [ -z $volume ]
then
        echo "Usage: launch-simple-longhorn <volume_name> <frontend> "
        echo "<volume_name>: Required. User defined volume name"
        echo "<frontend>: Optional. By default 'tgt-blockdev'. "
	exit -1
fi

if [ -z $size ]
then
        echo Use default size 1g
        size="1g"
fi

if [ -z $frontend ]
then
        echo Use default frontend TGT block device
        frontend="tgt-blockdev"
fi

function start() {
    set +e
    while true;
    do
        /usr/local/bin/grpc_health_probe -addr localhost:8500
        if [[ $? -eq 0 ]];
        then
            echo longhorn instance manager is ready
            break;
        fi
        sleep 1
    done
    set -e

    tgtd -f 2>&1 | tee /var/log/tgtd.log &

    longhorn-instance-manager process create --name "$volume-r" --binary /usr/local/bin/longhorn --port-count 15 --port-args "--listen,localhost:" -- replica /volume/ "--size" $size

    # wait for the replica to be started
    sleep 5

    longhorn-instance-manager process create --name "$volume-e" --binary /usr/local/bin/longhorn --port-count 1 --port-args "--listen,localhost:" -- controller $volume --frontend $frontend "--size" $size "--current-size" $size --replica tcp://localhost:10000
}

start &

exec longhorn-instance-manager daemon
'''



class IntegrityContracts(unittest.TestCase):
    def setUp(self):
        # The test user cannot inspect root processes. Keep real /proc fd and
        # cwd checks for this process, including the deliberate open-file case.
        original = Path.iterdir
        def entries(path):
            return iter([Path('/proc')/str(os.getpid())]) if path == Path('/proc') else original(path)
        self.proc = patch.object(Path, 'iterdir', entries)
        self.proc.start(); self.addCleanup(self.proc.stop)

    def fixture(self, directory):
        base = Path(directory); source = base/'source'; source.mkdir()
        (source/'volume.meta').write_text(json.dumps({'Size': 2*1024**3, 'Head': 'volume-head-001.img',
            'BackingFileName': '', 'Rebuilding': False, 'Dirty': False}))
        (source/'volume-head-001.img').write_bytes(b'\0'*65536)
        (source/'volume-head-001.img.meta').write_text('{}')
        (source/'volume-snap-check.img').write_bytes(b'acknowledged snapshot data'*4096)
        (source/'volume-snap-check.img.meta').write_text('{}')
        original = I.source_files(source)
        request = {'base': str(base), 'uuid': 'parent-uuid', 'id': str(uuid.uuid4()),
            'source': str(source), 'original': original, 'snapshot_file': 'volume-snap-check.img',
            'export_name': 'integrity-'+uuid.uuid4().hex, 'sha256': hashlib.sha256(b'business').hexdigest(),
            'payload_bytes': 8}
        original_run = subprocess.run; original_stat = Path.stat; calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'findmnt':
                rows = [{'target': str(base), 'uuid': 'parent-uuid'}] if argv[3] == str(base) else []
                return SimpleNamespace(returncode=0 if rows else 1, stdout=json.dumps({'filesystems': rows}))
            if argv[0] == 'cp': return original_run(argv, **kwargs)
            raise AssertionError(argv)
        def pathstat(path, *a, **kw):
            return SimpleNamespace(st_dev=-1) if path == Path('/') else original_stat(path, *a, **kw)
        def context():
            stack = ExitStack()
            stack.enter_context(patch.object(subprocess, 'run', side_effect=run))
            stack.enter_context(patch.object(Path, 'stat', pathstat))
            stack.enter_context(patch.object(os, 'statvfs', return_value=SimpleNamespace(f_bavail=8*1024**2, f_frsize=4096)))
            return stack
        return source, original, request, calls, context

    def test_actual_copy_corruption_changes_one_owned_byte_and_keeps_source_and_rescue(self):
        with tempfile.TemporaryDirectory() as d:
            source, original, request, calls, context = self.fixture(d)
            with context():
                result = I.host_export(request, 'create')
                self.assertEqual(original, I.source_files(source))
                root = Path(d)/('integrity-'+request['id']); target = request['snapshot_file']
                self.assertEqual((source/target).read_bytes(), (root/'rescue'/target).read_bytes())
                before, after = (source/target).read_bytes(), (root/'corrupt'/target).read_bytes()
                differences = [index for index, pair in enumerate(zip(before, after)) if pair[0] != pair[1]]
                self.assertEqual([result['corruption']['offset']], differences)
                self.assertNotEqual(result['corruption']['original_sha512'], result['corruption']['corrupt_sha512'])
                self.assertTrue(I.host_export(request, 'remove')['removed'])
            self.assertEqual(original, I.source_files(source))
            self.assertEqual(['source'], [p.name for p in Path(d).iterdir()])
            self.assertTrue(all('--reflink=never' in a and '--sparse=always' in a for a in calls if a[0] == 'cp'))

    def test_open_source_file_symlink_or_invalid_metadata_refuses_copy(self):
        for defect in ('open', 'symlink', 'backing', 'rebuilding'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                source, original, request, calls, context = self.fixture(d)
                handle = None
                try:
                    if defect == 'open': handle = (source/'volume-snap-check.img').open('rb')
                    if defect == 'symlink': (source/'link').symlink_to(source/'volume.meta')
                    if defect in ('backing', 'rebuilding'):
                        meta = json.loads((source/'volume.meta').read_text())
                        meta['BackingFileName' if defect == 'backing' else 'Rebuilding'] = 'foreign' if defect == 'backing' else True
                        (source/'volume.meta').write_text(json.dumps(meta))
                    with context(), self.assertRaises(ValueError): I.host_export(request, 'create')
                    self.assertFalse((Path(d)/('integrity-'+request['id'])).exists())
                    self.assertFalse(any(a[0] == 'cp' for a in calls))
                finally:
                    if handle: handle.close()

    def test_foreign_copy_entry_or_changed_original_blocks_retirement(self):
        for defect in ('foreign', 'original', 'image'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                source, original, request, calls, context = self.fixture(d)
                with context():
                    I.host_export(request, 'create'); root = Path(d)/('integrity-'+request['id'])
                    if defect == 'foreign': (root/'rescue'/'foreign').write_text('preserve')
                    if defect == 'original': (source/'volume-head-001.img').write_bytes(b'changed')
                    if defect == 'image': (root/'rescue'/'volume-head-001.img').write_bytes(b'changed')
                    with self.assertRaises(ValueError): I.host_export(request, 'remove')
                    self.assertTrue(root.exists())

    def test_atomic_native_metadata_replacement_is_admitted_after_image_byte_verification(self):
        with tempfile.TemporaryDirectory() as d:
            source, original, request, calls, context = self.fixture(d)
            with context():
                I.host_export(request, 'create'); root = Path(d)/('integrity-'+request['id'])
                meta = root/'rescue'/'volume.meta'; replacement = root/'rescue'/'new'
                value = json.loads(meta.read_text()); value['Dirty'] = True
                replacement.write_text(json.dumps(value)); replacement.replace(meta)
                self.assertTrue(I.host_export(request, 'remove')['removed'])

    def test_export_pod_pins_original_engine_and_exposes_only_rescue_copy(self):
        row = {'node': 'worker', 'engine_image': 'hub.talkedu.cn/kubeauto/longhorn-engine:v1.12.1',
            'export': {'base': '/data/leased', 'id': str(uuid.uuid4()), 'export_name': 'unique-export'}}
        pod = I.export_pod({'namespace': 'owned'}, row)
        container = pod['spec']['containers'][0]
        self.assertEqual('owned', pod['metadata']['namespace'])
        self.assertEqual('worker', pod['spec']['nodeName'])
        self.assertTrue(pod['spec']['hostPID'])
        self.assertEqual('Never', pod['spec']['restartPolicy'])
        self.assertTrue(container['securityContext']['privileged'])
        self.assertEqual({'dev': '/host/dev', 'proc': '/host/proc', 'copy': '/volume'},
                         {v['name']: v['mountPath'] for v in container['volumeMounts']})
        self.assertEqual(row['engine_image'], container['image'])
        self.assertEqual(['bash', '-ec'], container['command'][:2])
        self.assertEqual('longhorn-native-export', container['command'][3])
        self.assertEqual(['unique-export', str(2*1024**3)], container['args'])
        paths = {v['name']: v['hostPath']['path'] for v in pod['spec']['volumes']}
        self.assertTrue(paths['copy'].endswith('/rescue'))
        self.assertNotIn('/replicas/', paths['copy']); self.assertNotIn('/corrupt', paths['copy'])

    def test_native_export_wrapper_checks_upstream_bytes_and_preserves_original_arguments(self):
        self.assertEqual(1377, len(OFFICIAL_EXPORT_HELPER.encode()))
        self.assertEqual('efe69a0768dc05575e452d69edfb890e0a307fc1526db676fd6394a47196ce36',
                         hashlib.sha256(OFFICIAL_EXPORT_HELPER.encode()).hexdigest())
        row = {'node': 'worker', 'engine_image': 'hub.talkedu.cn/kubeauto/longhorn-engine:v1.12.1',
               'export': {'base': '/data/leased', 'id': str(uuid.uuid4()), 'export_name': 'integrity-'+uuid.uuid4().hex}}
        container = I.export_pod({'namespace': 'owned'}, row)['spec']['containers'][0]
        wrapper = container['command'][2]
        syntax = subprocess.run(['/bin/bash', '-n', '-c', wrapper], capture_output=True, text=True)
        self.assertEqual(0, syntax.returncode, syntax.stderr)
        for defect in ('none', 'unknown-bytes', 'already-patched', 'missing-helper'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); helper = base/'launch-simple-longhorn'; capture = base/'capture.json'
                original = OFFICIAL_EXPORT_HELPER
                if defect == 'unknown-bytes': original += '\n# different upstream helper\n'
                if defect == 'already-patched': original = original.replace('--listen,localhost:', '--listen=localhost:')
                if defect != 'missing-helper': helper.write_text(original); helper.chmod(0o755)
                # Execute the wrapper's real command-v, sha256sum, mktemp and
                # sed. Capture its final exec only: no mount, engine or daemon.
                bash = base/'bash'
                bash.write_text('#!/usr/bin/python3\nimport json,os,sys\nfrom pathlib import Path\n'
                    'p=Path(sys.argv[1])\n'
                    'row={"argv":sys.argv[1:],"source":p.read_text(),"mode":p.stat().st_mode & 0o777}\n'
                    'Path(os.environ["CAPTURE"]).write_text(json.dumps(row))\np.unlink()\n')
                bash.chmod(0o755)
                env = dict(os.environ, PATH=str(base)+':/usr/bin:/bin', CAPTURE=str(capture))
                result = subprocess.run(['/bin/bash']+container['command'][1:]+container['args'],
                                        env=env, capture_output=True, text=True, timeout=15)
                if defect != 'missing-helper': self.assertEqual(original, helper.read_text())
                if defect != 'none':
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse(capture.exists())
                    continue
                self.assertEqual(0, result.returncode, result.stderr)
                observed = json.loads(capture.read_text())
                self.assertEqual(container['args'], observed['argv'][1:])
                self.assertEqual(0o600, observed['mode'])
                self.assertTrue(observed['argv'][0].startswith('/tmp/longhorn-integrity-export.'))
                self.assertNotEqual(str(helper), observed['argv'][0])
                expected = OFFICIAL_EXPORT_HELPER.replace('--listen,localhost:', '--listen=localhost:')
                self.assertEqual(2, OFFICIAL_EXPORT_HELPER.count('--listen,localhost:'))
                self.assertEqual(expected, observed['source'])
                self.assertEqual(2, observed['source'].count('--listen=localhost:'))

    def test_upstream_csv_port_prefix_is_the_exact_controller_failure_and_equals_is_single_entry(self):
        lines = [shlex.split(line) for line in OFFICIAL_EXPORT_HELPER.splitlines()
                 if line.strip().startswith('longhorn-instance-manager process create')]
        self.assertEqual(2, len(lines))
        for argv in lines:
            original = argv[argv.index('--port-args')+1]
            # Observed bundled IM CLI splits this prefix into two fields. The manager appends
            # one allocated port per field; the controller requests only one.
            broken = next(csv.reader([original]))
            fixed = next(csv.reader([original.replace('--listen,localhost:', '--listen=localhost:')]))
            self.assertEqual(['--listen', 'localhost:'], broken)
            self.assertEqual(['--listen=localhost:'], fixed)
            self.assertEqual(['--listen10000', 'localhost:10001'],
                             [prefix+str(10000+i) for i, prefix in enumerate(broken)])
            self.assertEqual(['--listen=localhost:10000'],
                             [prefix+str(10000+i) for i, prefix in enumerate(fixed)])
            if argv[argv.index('--port-count')+1] == '1':
                self.assertGreater(len(broken), 1)  # Actual original rejection.
                self.assertEqual(1, len(fixed))
            repaired = shlex.split(' '.join(shlex.quote(word) for word in argv).replace('--listen,localhost:', '--listen=localhost:'))
            repaired[repaired.index('--port-args')+1] = original
            self.assertEqual(argv, repaired)  # Every other upstream argument survives.

    def native_counter(self, root, request):
        counter = root/'rescue/revision.counter'
        counter.write_bytes(b'0'+b'\0'*4095); counter.chmod(0o600)
        receipt = root/'fixture.json'; row = json.loads(receipt.read_text())
        row['readback'] = {'sha256': request['sha256'], 'bytes': request['payload_bytes']}
        receipt.write_text(json.dumps(row))
        return counter

    def test_native_zero_counter_is_the_only_admitted_export_addition(self):
        defects = ('none', 'foreign', 'one', 'short', 'mode', 'symlink', 'missing-original', 'changed-image', 'unacknowledged')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, original, request, calls, context = self.fixture(directory)
                with context():
                    I.host_export(request, 'create'); root = Path(directory)/('integrity-'+request['id'])
                    counter = self.native_counter(root, request)
                    if defect == 'foreign': (root/'rescue/foreign').write_text('preserve')
                    elif defect == 'one': counter.write_bytes(b'1'+b'\0'*4095)
                    elif defect == 'short': counter.write_bytes(b'0')
                    elif defect == 'mode': counter.chmod(0o644)
                    elif defect == 'symlink': counter.unlink(); counter.symlink_to(source/'volume.meta')
                    elif defect == 'missing-original': (root/'rescue/volume-head-001.img.meta').unlink()
                    elif defect == 'changed-image': (root/'rescue/volume-head-001.img').write_bytes(b'changed')
                    elif defect == 'unacknowledged':
                        path = root/'fixture.json'; row = json.loads(path.read_text()); row.pop('readback'); path.write_text(json.dumps(row))
                    if defect == 'none': self.assertTrue(I.host_export(request, 'remove')['removed'])
                    else:
                        before = I.source_files(root/'corrupt')
                        with self.assertRaises(ValueError): I.host_export(request, 'remove')
                        self.assertEqual(before, I.source_files(root/'corrupt'))
                        self.assertNotIn('removal', json.loads((root/'fixture.json').read_text()))
                    self.assertEqual(original, I.source_files(source))

    def test_only_exact_old_completed_corrupt_retirement_is_recoverable(self):
        for defect in ('none', 'no-intent', 'changed-proof', 'partial-corrupt', 'foreign-rescue'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, original, request, calls, context = self.fixture(directory)
                with context():
                    I.host_export(request, 'create'); root = Path(directory)/('integrity-'+request['id'])
                    self.native_counter(root, request)
                    path = root/'fixture.json'; row = json.loads(path.read_text())
                    row['retired_copies'] = {'corrupt': I.source_files(root/'corrupt')}
                    if defect == 'no-intent': row.pop('retired_copies')
                    elif defect == 'changed-proof': row['retired_copies']['corrupt']['files']['volume.meta']['inode'] += 1
                    path.write_text(json.dumps(row))
                    files = list((root/'corrupt').iterdir())
                    for file in files if defect != 'partial-corrupt' else files[:1]: file.unlink()
                    if defect != 'partial-corrupt': (root/'corrupt').rmdir()
                    if defect == 'foreign-rescue': (root/'rescue/foreign').write_text('preserve')
                    if defect == 'none': self.assertTrue(I.host_export(request, 'remove')['removed'])
                    else:
                        before = I.source_files(root/'rescue')
                        with self.assertRaises(ValueError): I.host_export(request, 'remove')
                        self.assertEqual(before, I.source_files(root/'rescue'))
                    self.assertEqual(original, I.source_files(source))

    def test_journaled_unlink_and_rmdir_failures_resume_through_original_cleanup_actions(self):
        for failure in ('unlink-before', 'unlink-after', 'unlink-meta-after', 'rmdir-before', 'rmdir-after', 'final-rmdir'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                source, original, request, calls, context = self.fixture(directory)
                with context():
                    I.host_export(request, 'create'); root = Path(directory)/('integrity-'+request['id'])
                    self.native_counter(root, request)
                    unlink, rmdir = Path.unlink, Path.rmdir
                    injected = []
                    def remove(file, *args, **kwargs):
                        if (not injected and failure.startswith('unlink') and file.parent == root/'corrupt' and
                                (failure != 'unlink-meta-after' or file.name == 'volume.meta')):
                            injected.append(str(file))
                            if failure in ('unlink-after', 'unlink-meta-after'): unlink(file, *args, **kwargs)
                            raise OSError('injected unlink boundary')
                        return unlink(file, *args, **kwargs)
                    def remove_dir(path, *args, **kwargs):
                        if not injected and ((failure.startswith('rmdir') and path == root/'corrupt') or
                                             (failure == 'final-rmdir' and path == root)):
                            injected.append(str(path))
                            if failure == 'rmdir-after': rmdir(path, *args, **kwargs)
                            raise OSError('injected rmdir boundary')
                        return rmdir(path, *args, **kwargs)
                    with patch.object(Path, 'unlink', remove), patch.object(Path, 'rmdir', remove_dir):
                        with self.assertRaisesRegex(OSError, 'injected'): I.host_export(request, 'remove')
                    self.assertTrue(injected)
                    self.assertTrue((root/'fixture.json').is_file())
                    # clean() already calls unmount before retrying remove.
                    I.host_export(request, 'unmount')
                    self.assertTrue(I.host_export(request, 'remove')['removed'])
                    self.assertEqual(original, I.source_files(source))

    def test_hard_interruption_after_last_receipt_unlink_retains_missing_proof_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            source, original, request, calls, context = self.fixture(directory)
            with context():
                I.host_export(request, 'create'); root = Path(directory)/('integrity-'+request['id'])
                self.native_counter(root, request); unlink = Path.unlink
                def interrupt(file, *args, **kwargs):
                    result = unlink(file, *args, **kwargs)
                    if file == root/'fixture.json': raise SystemExit('hard terminal boundary')
                    return result
                with patch.object(Path, 'unlink', interrupt), self.assertRaises(SystemExit):
                    I.host_export(request, 'remove')
                self.assertTrue(root.is_dir()); self.assertFalse((root/'fixture.json').exists())
                with self.assertRaises(FileNotFoundError): I.host_export(request, 'remove')
                self.assertEqual(original, I.source_files(source))

    def test_partial_retirement_still_refuses_foreign_missing_symlink_or_changed_bytes(self):
        for defect in ('foreign', 'missing', 'symlink', 'bytes'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                source, original, request, calls, context = self.fixture(directory)
                with context():
                    I.host_export(request, 'create'); root = Path(directory)/('integrity-'+request['id'])
                    self.native_counter(root, request)
                    unlink = Path.unlink
                    def stop(file, *args, **kwargs):
                        if file.parent == root/'corrupt': raise OSError('retain exact unlink intent')
                        return unlink(file, *args, **kwargs)
                    with patch.object(Path, 'unlink', stop), self.assertRaises(OSError): I.host_export(request, 'remove')
                    file = root/'rescue/volume-head-001.img'; info = file.stat()
                    if defect == 'foreign': (root/'rescue/foreign').write_text('retain')
                    elif defect == 'missing': file.unlink()
                    elif defect == 'symlink': file.unlink(); file.symlink_to(source/'volume-head-001.img')
                    else:
                        with file.open('r+b') as stream: stream.write(b'x')
                        os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
                    with self.assertRaises(ValueError): I.host_export(request, 'remove')
                    self.assertEqual(original, I.source_files(source))

    def test_source_inventory_remote_transport_works_without_python311_file_digest(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(hashlib.__dict__):
            hashlib.__dict__.pop('file_digest', None)
            self.assertFalse(hasattr(hashlib, 'file_digest'))
            source, _, _, _, _ = self.fixture(directory)
            payload = b'confirmed snapshot bytes'*100000
            (source/'volume-snap-check.img').write_bytes(payload)
            def remote(address, argv, **kwargs):
                self.assertEqual(['python3', '-c'], argv[:2])
                with patch.object(sys, 'stdin', io.StringIO(kwargs['stdin'])), redirect_stdout(io.StringIO()) as output:
                    exec(argv[2], {})
                return SimpleNamespace(stdout=output.getvalue())
            result = I.remote(SimpleNamespace(remote=remote), 'owned-worker', I.source_files, str(source))
            entry = result['files']['volume-snap-check.img']
            self.assertEqual(hashlib.sha512(payload).hexdigest(), entry['sha512'])
            self.assertEqual(len(payload), entry['size'])
            self.assertEqual((source/'volume-snap-check.img').stat().st_ino, entry['inode'])

    def test_native_export_readback_works_without_python311_file_digest_and_keeps_hash_refusal(self):
        with patch.dict(hashlib.__dict__):
            hashlib.__dict__.pop('file_digest', None)
            self.assertFalse(hasattr(hashlib, 'file_digest'))
            self.assert_native_block_export_readback()

    def test_native_block_export_is_mounted_read_only_without_journal_replay_and_hash_checked(self):
        self.assert_native_block_export_readback()

    def assert_native_block_export_readback(self):
        for wrong_hash in (False, True):
            with self.subTest(wrong_hash=wrong_hash), tempfile.TemporaryDirectory() as d:
                source, original, request, calls, context = self.fixture(d)
                with context():
                    I.host_export(request, 'create')
                    mount = Path(d)/('integrity-'+request['id'])/'mount'
                    device = Path('/dev/longhorn/'+request['export_name'])
                    original_run = subprocess.run; original_stat = Path.stat
                    state = {'mounted': False, 'device': True}; commands = []
                    def pathstat(path, *a, **kw):
                        if path == device:
                            if not state['device']: raise FileNotFoundError(2, 'fixture device retired', str(path))
                            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 42))
                        return original_stat(path, *a, **kw)
                    def run(argv, **kwargs):
                        commands.append(argv)
                        if argv[0] == 'blkid': return SimpleNamespace(stdout='ext4\n')
                        if argv[0] == 'mount':
                            state['mounted'] = True; (mount/'confirmed').write_bytes(b'wrong' if wrong_hash else b'business')
                            return SimpleNamespace(stdout='', returncode=0)
                        if argv[0] == 'umount':
                            state['mounted'] = False; (mount/'confirmed').unlink()
                            return SimpleNamespace(stdout='', returncode=0)
                        if argv[0] == 'findmnt' and argv[3] == str(mount):
                            rows = [{'target': str(mount), 'source': str(device), 'options': 'ro,noload,nodev,nosuid'}] if state['mounted'] else []
                            return SimpleNamespace(returncode=0 if rows else 1, stdout=json.dumps({'filesystems': rows}))
                        return original_run(argv, **kwargs)
                    with patch.object(Path, 'stat', pathstat), patch.object(subprocess, 'run', side_effect=run):
                        if wrong_hash:
                            with self.assertRaisesRegex(ValueError, 'acknowledged'): I.host_export(request, 'read')
                        else:
                            result = I.host_export(request, 'read')
                            self.assertEqual(request['sha256'], result['readback']['sha256'])
                        mounted = next(a for a in commands if a[0] == 'mount')
                        self.assertIn('ro,noload,nodev,nosuid', mounted)
                        I.host_export(request, 'unmount'); state['device'] = False
                        self.assertTrue(I.host_export(request, 'remove')['removed'])

    def test_detach_gate_requires_both_status_and_desired_native_instances_stopped(self):
        for defect in ('none', 'volume', 'engine', 'replica-desired'):
            with self.subTest(defect=defect):
                volume = {'metadata': {'uid': 'original'}, 'spec': {'nodeID': ''}, 'status': {'state': 'detached'}}
                engine = {'spec': {'volumeName': 'data', 'desireState': 'stopped'}, 'status': {'currentState': 'stopped'}}
                replica = copy.deepcopy(engine)
                if defect == 'volume': volume['spec']['nodeID'] = 'worker'
                if defect == 'engine': engine['status']['currentState'] = 'running'
                if defect == 'replica-desired': replica['spec']['desireState'] = 'running'
                def get(kind, *a, **kw):
                    if kind == 'volumes.longhorn.io': return volume
                    return {'items': [engine] if kind == 'engines.longhorn.io' else [replica]}
                lab = SimpleNamespace(get=Mock(side_effect=get))
                self.assertEqual(defect == 'none', I.detached(lab, {'volume': 'data', 'volume_uid': 'original'}))

    def test_cleanup_unmounts_then_stops_export_before_copy_removal_and_business_data(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); (base/I.RECORD).touch()
            business = {'namespace': 'owned', 'objects': {'Pod/native-export': 'export-uid'}}
            (base/I.BUSINESS).write_text(json.dumps(business))
            row = {'host_ready': True, 'host_intent': True, 'address': 'worker',
                'export': {'export_name': 'unique'}, 'client': 'writer', 'digest': 'original'}
            export = {'metadata': {'uid': 'export-uid'}}; calls = []
            def get(kind, name=None, namespace=None): return export if name == 'native-export' else {'metadata': {'uid': 'writer'}}
            lab = SimpleNamespace(DIRECTORY=base, get=Mock(side_effect=get), check_business_graph=Mock(),
                remove=Mock(side_effect=lambda *a: calls.append('stop-export')),
                wait=Mock(), installation_health=Mock(), write_private=Mock(),
                cleanup_business=Mock(side_effect=lambda *a, **kw: calls.append('business-delete')))
            with patch.object(I, 'receipt', return_value=row), patch.object(I, 'remote', side_effect=lambda *a: calls.append(a[-1])), \
                    patch.object(I, 'check_data', side_effect=lambda *a: calls.append('original-data')):
                I.clean(lab, 1)
            self.assertEqual(['unmount', 'stop-export', 'remove', 'original-data', 'business-delete'], calls)

    def test_already_clean_failed_receipt_is_archived_only_after_restored_empty_boundary(self):
        for defect in ('none', 'normal', 'owner', 'passed', 'business', 'namespace', 'host',
                       'host-false', 'host-ready', 'settings', 'shared', 'config', 'native', 'health'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); config = base/'config.yml'; config.write_text('original input')
                row = {'run_id': 'run', 'cluster_uid': 'cluster', 'clean': True, 'settings': {},
                    'shared': {'uid': 'shared'}, 'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                    'namespace_intent': {'name': 'owned'}}
                if defect == 'passed': row['passed'] = True
                if defect in ('host', 'host-false'): row['host_intent'] = defect == 'host'
                if defect == 'host-ready': row['host_ready'] = True
                if defect == 'business': (base/I.BUSINESS).write_text('{}')
                if defect == 'config': config.write_text('changed')
                (base/I.RECORD).write_text(json.dumps(row))
                empty = Mock(side_effect=ValueError('native objects remain') if defect == 'native' else None)
                lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'foreign' if defect == 'owner' else 'run', 'cluster_uid': 'cluster'},
                    get=lambda *a: {} if defect == 'namespace' else None,
                    shared_snapshot_identity=lambda: {} if defect == 'shared' else {'uid': 'shared'},
                    load_helper=lambda name: SimpleNamespace(empty=empty),
                    installation_health=Mock(side_effect=ValueError('unhealthy') if defect == 'health' else None),
                    write_private=lambda path,value: path.write_text(json.dumps(value)))
                with patch.object(I, 'settings', return_value={'changed': {}} if defect == 'settings' else {}):
                    if defect in ('none', 'normal'):
                        I.clean(lab, 77, failed=defect != 'normal')
                    else:
                        with self.assertRaises(ValueError): I.clean(lab, 77, failed=True)
                archives = list(base.glob('longhorn-integrity-recovered-*.json'))
                if defect == 'none':
                    self.assertFalse((base/I.RECORD).exists()); self.assertEqual(1, len(archives))
                    self.assertTrue(json.loads(archives[0].read_text())['recovery_verified']['host_intent_absent'])
                    empty.assert_called_once_with(lab)
                else:
                    self.assertTrue((base/I.RECORD).exists()); self.assertEqual([], archives)

    def test_readback_completed_but_failed_attempt_cleanup_archives_without_module_pass(self):
        for defect in ('none', 'digest', 'bytes', 'no-export'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); config = base/'config.yml'; config.write_text('original input')
                row = {'run_id': 'run', 'cluster_uid': 'cluster', 'passed': True, 'settings': {},
                    'shared': {'uid': 'shared'}, 'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
                    'namespace_intent': {'name': 'owned'}, 'digest': 'acknowledged-digest',
                    'export': {'sha256': 'acknowledged-digest', 'payload_bytes': 9437184},
                    'export_readback': {'sha256': 'acknowledged-digest', 'bytes': 9437184}}
                if defect == 'digest': row['export_readback']['sha256'] = 'foreign'
                elif defect == 'bytes': row['export_readback']['bytes'] += 1
                elif defect == 'no-export': row.pop('export')
                (base/I.RECORD).write_text(json.dumps(row))
                empty = Mock()
                lab = SimpleNamespace(DIRECTORY=base, owner=lambda: {'run_id': 'run', 'cluster_uid': 'cluster'},
                    get=lambda *a: None, shared_snapshot_identity=lambda: {'uid': 'shared'},
                    load_helper=lambda name: SimpleNamespace(empty=empty), installation_health=Mock(),
                    write_private=lambda path, value: path.write_text(json.dumps(value)))
                with patch.object(I, 'settings', return_value={}), redirect_stdout(io.StringIO()) as output:
                    if defect == 'none': I.clean(lab, 77, failed=True)
                    else:
                        with self.assertRaises(ValueError): I.clean(lab, 77, failed=True)
                self.assertNotIn('LONGHORN_INTEGRITY_PASS', output.getvalue())
                archives = list(base.glob('longhorn-integrity-recovered-*.json'))
                if defect == 'none':
                    self.assertFalse((base/I.RECORD).exists()); self.assertEqual(1, len(archives))
                    accepted = json.loads(archives[0].read_text())
                    self.assertTrue(accepted['passed']); self.assertTrue(accepted['clean'])
                    self.assertEqual('failed-attempt-cleanup-only', accepted['recovery_verified']['scope'])
                    self.assertIs(False, accepted['recovery_verified']['delivery_pass'])
                    empty.assert_called_once_with(lab)
                else:
                    self.assertTrue((base/I.RECORD).exists()); self.assertEqual([], archives)

    def test_export_cleanup_failure_never_reattaches_or_deletes_source(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); (base/I.RECORD).touch(); (base/I.BUSINESS).write_text(json.dumps({'namespace': 'owned', 'objects': {}}))
            lab = SimpleNamespace(DIRECTORY=base, check_business_graph=Mock(), get=Mock(), cleanup_business=Mock())
            row = {'host_ready': True, 'address': 'worker', 'export': {}}
            with patch.object(I, 'receipt', return_value=row), patch.object(I, 'remote', side_effect=ValueError('mount identity changed')):
                with self.assertRaises(ValueError): I.clean(lab, 1)
            lab.get.assert_not_called(); lab.cleanup_business.assert_not_called()


if __name__ == '__main__': unittest.main()

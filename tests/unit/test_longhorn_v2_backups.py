"""V2 external-store isolation, provenance and runner failure contracts."""
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


BACKUPS = load('longhorn-backups.py')
V2 = load('longhorn-v2-backups.py')
LAB = load('longhorn-lab.py')
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')


class V2BackupContracts(unittest.TestCase):
    def test_strict_tls_reproduces_legacy_missing_aki_and_accepts_backup_chain(self):
        import ssl
        from cryptography import x509
        for profile in (BACKUPS.V1_ENDPOINTS, BACKUPS.V2_ENDPOINTS):
            for legacy in (True, False):
                with self.subTest(profile=profile, legacy=legacy), tempfile.TemporaryDirectory() as directory:
                    certs = LAB.tls_fixture(extra_ips=[BACKUPS.HOST, profile.node]) if legacy else BACKUPS.tls_fixture(profile)
                    path = Path(directory)
                    for name, value in certs.items(): (path/name).write_text(value)
                    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    server.load_cert_chain(path/'tls.crt', path/'tls.key')
                    client = ssl.create_default_context(cadata=certs['ca.crt'])
                    client.verify_flags |= ssl.VERIFY_X509_STRICT
                    ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
                    c = client.wrap_bio(ci, co, server_hostname=profile.node)
                    v = server.wrap_bio(si, so, server_side=True)
                    def handshake():
                        completed = set()
                        for _ in range(12):
                            for name, peer in (('client', c), ('server', v)):
                                try: peer.do_handshake(); completed.add(name)
                                except ssl.SSLWantReadError: pass
                            for source, target in ((co, si), (so, ci)):
                                data = source.read()
                                if data: target.write(data)
                            if len(completed) == 2: return
                        raise AssertionError('TLS handshake did not finish')
                    if legacy:
                        with self.assertRaisesRegex(ssl.SSLCertVerificationError, 'Missing Authority Key Identifier'):
                            handshake()
                    else:
                        handshake()
                        ca = x509.load_pem_x509_certificate(certs['ca.crt'].encode())
                        leaf = x509.load_pem_x509_certificate(certs['tls.crt'].encode())
                        self.assertEqual(leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier,
                            ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest)
                        self.assertTrue(ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign)

    def test_fixed_profiles_are_disjoint_and_v2_rejects_other_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = SimpleNamespace(DIRECTORY=Path(directory), NODES=NODES)
            first = BACKUPS.Store(lab)
            second = BACKUPS.Store(lab, endpoints=BACKUPS.V2_ENDPOINTS)
            self.assertEqual(first.product_endpoint, 'https://192.168.122.135:23523')
            self.assertEqual(second.product_endpoint, 'https://192.168.47.146:23623')
            self.assertEqual(second.endpoint, 'https://192.168.47.130:23621')
            self.assertFalse(set(BACKUPS.V1_ENDPOINTS[2:]) & set(BACKUPS.V2_ENDPOINTS[2:]))
            with self.assertRaisesRegex(ValueError, 'unapproved'):
                BACKUPS.Store(lab, endpoints=BACKUPS.V2_ENDPOINTS._replace(node='192.168.122.135'))
            lab.NODES = LAB.NODES
            with self.assertRaisesRegex(ValueError, 'exact three-node'):
                BACKUPS.Store(lab, endpoints=BACKUPS.V2_ENDPOINTS)

    def test_receipt_binds_complete_profile_and_preserves_legacy_v1_receipts(self):
        for profile in (BACKUPS.V1_ENDPOINTS, BACKUPS.V2_ENDPOINTS):
            for fault in ('none', 'missing-profile', 'node', 'api_port', 'console_port', 'transport_port', 'tunnel_port', 'refusal_port'):
                with self.subTest(profile=profile, fault=fault), tempfile.TemporaryDirectory() as directory:
                    lab = SimpleNamespace(DIRECTORY=Path(directory), NODES=NODES,
                        owner=lambda: {'run_id': 'abcd-1234', 'cluster_uid': 'cluster'})
                    store = BACKUPS.Store(lab, endpoints=profile)
                    row = {'run_id': 'abcd-1234', 'cluster_uid': 'cluster', 'name': 'longhorn-backup-abcd1234',
                           'bucket': 'longhorn-backup-abcd1234', 'endpoints': profile._asdict()}
                    if fault == 'missing-profile': del row['endpoints']
                    elif fault != 'none': row['endpoints'][fault] = 'foreign'
                    store.record.write_text(json.dumps(row))
                    if fault == 'none' or fault == 'missing-profile' and profile == BACKUPS.V1_ENDPOINTS:
                        self.assertEqual(store.receipt(), row)
                    else:
                        with self.assertRaises(ValueError): store.receipt()

    def test_v2_container_requires_exact_host_ports_and_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            row = {'name': 'owned', 'image_id': 'sha256:fixed', 'run_id': 'run', 'container_id': 'original'}
            obj = {'Id': 'original', 'Name': '/owned', 'Image': 'sha256:fixed',
                'Config': {'Labels': {'kubeauto.io/longhorn-backup-run': 'run'}},
                'Mounts': [{'Source': str(path/source), 'Destination': target, 'Type': 'bind', 'RW': source == 'data'}
                           for source, target in (('data', '/data'), ('certs', '/certs'), ('cas', '/tmp/longhorn-mc/certs/CAs'))],
                'HostConfig': {'PortBindings': {'9000/tcp': [{'HostIp': BACKUPS.HOST, 'HostPort': '23621'}],
                                              '9001/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '23622'}]}}}
            self.assertEqual(BACKUPS.validate_container(obj, row, path, BACKUPS.V2_ENDPOINTS), 'original')
            for key, value in (('HostIp', '0.0.0.0'), ('HostPort', '23521')):
                changed = copy.deepcopy(obj); changed['HostConfig']['PortBindings']['9000/tcp'][0][key] = value
                with self.assertRaises(ValueError): BACKUPS.validate_container(changed, row, path, BACKUPS.V2_ENDPOINTS)
            with self.assertRaises(ValueError): BACKUPS.validate_container(obj, row, path)

    def test_prepare_v2_transport_uses_its_node_ports_and_admitted_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            objects = {}; root = Path(directory)
            lab = SimpleNamespace(DIRECTORY=root, NODES=NODES, SSH=LAB.SSH, remote=Mock(),
                cache_artifacts=Mock(), write_private=LAB.write_private)
            store = BACKUPS.Store(lab, endpoints=BACKUPS.V2_ENDPOINTS); store.directory.mkdir()
            row = {'name': 'owned', 'run_id': 'run'}
            def get(kind, name, namespace=''):
                return {'metadata': {'uid': 'namespace'}} if kind == 'namespace' else objects.get(name)
            lab.get = Mock(side_effect=get)
            listener = {'binding': '127.0.0.1:23624', 'uid': 0, 'command': 'sshd: root', 'start_time': time.time()}
            def execute(argv, **kwargs):
                self.assertEqual(argv[-1], 'root@192.168.47.146')
                self.assertIn('127.0.0.1:23624:192.168.47.130:23621', argv)
                (store.directory/'transport.sock').touch()
            lab.execute = Mock(side_effect=execute)
            def create(*args, stdin):
                obj = json.loads(stdin); spec = obj['spec']; container = spec['containers'][0]
                self.assertEqual(spec['nodeName'], 'longhorn-node-146')
                self.assertEqual(container['readinessProbe']['tcpSocket'], {'host': NODES[0], 'port': 23623})
                self.assertEqual(container['command'][-1], BACKUPS.transport_program(BACKUPS.V2_ENDPOINTS))
                self.assertNotIn('192.168.122.135', stdin); self.assertNotIn('23524', stdin)
                self.assertFalse(spec['automountServiceAccountToken']); self.assertTrue(spec['hostNetwork'])
                spec['serviceAccountName'] = 'default'; obj['metadata']['uid'] = 'admitted'
                obj['status'] = {'conditions': [{'type': 'Ready', 'status': 'True'}]}
                objects['owned-transport'] = obj
                return SimpleNamespace(stdout=json.dumps(obj))
            lab.kubectl = Mock(side_effect=create)
            lab.wait = lambda stage, timeout, predicate: self.assertTrue(predicate())
            with patch.object(store, 'receipt', side_effect=lambda: row), \
                    patch.object(store, 'transport_listener', side_effect=[None, listener, listener]), redirect_stdout(io.StringIO()):
                store.prepare_transport()
            self.assertEqual(row['transport']['pod_uid'], 'admitted')
            self.assertEqual(row['transport']['pod_spec'], objects['owned-transport']['spec'])
            self.assertEqual(lab.remote.call_args.args[0], NODES[0])
            self.assertIn("s.bind(('192.168.47.146',23623))", lab.remote.call_args.args[1][-1])

    def test_v2_listener_probe_keeps_original_process_guard_and_uses_own_port(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = SimpleNamespace(DIRECTORY=Path(directory), NODES=NODES, listener_script=LAB.listener_script,
                remote=Mock(return_value=SimpleNamespace(stdout='null')))
            store = BACKUPS.Store(lab, endpoints=BACKUPS.V2_ENDPOINTS)
            self.assertIsNone(store.transport_listener())
            call = lab.remote.call_args
            self.assertEqual(call.args[0], NODES[0]); self.assertIn('assert port==23624', call.args[1][-1])
            self.assertIn("assert result==request['expected']", call.args[1][-1])
            self.assertEqual(json.loads(call.kwargs['stdin']), {'port': 23624, 'action': 'probe'})

    def test_v2_qualification_probes_three_nodes_and_reports_actual_count(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = SimpleNamespace(DIRECTORY=Path(directory), NODES=NODES, remote=Mock(), execute=Mock())
            store = BACKUPS.Store(lab, endpoints=BACKUPS.V2_ENDPOINTS)
            (store.directory/'cas').mkdir(parents=True); (store.directory/'cas/ca.crt').write_text('public-ca')
            with patch.object(store, 'receipt', return_value={'run_id': 'run', 'bucket': 'owned'}), \
                    patch.object(store, 'inspect', return_value={'Id': 'owned'}), \
                    patch.object(store, 'mc', side_effect=['', 'run']), patch.object(store, 'prepare_transport') as prepare, \
                    redirect_stdout(io.StringIO()) as output:
                lab.remote.side_effect = lambda *args, **kwargs: self.assertTrue(prepare.called)
                store.qualify()
            self.assertEqual([c.args[0] for c in lab.remote.call_args_list], list(NODES))
            for call in lab.remote.call_args_list:
                self.assertIn('ssl.create_default_context', call.args[1][-1]); self.assertIn('ProxyHandler({})', call.args[1][-1])
                self.assertEqual(json.loads(call.kwargs['stdin']), {'ca': 'public-ca',
                    'url': 'https://192.168.47.146:23623/minio/health/live'})
            self.assertEqual(output.getvalue(), 'LONGHORN_BACKUP_STORE_QUALIFIED clients=3 tls=verified object_readback=exact\n')

    def test_v2_factory_routes_nested_business_and_cleanup_to_same_profile(self):
        install = Mock(); lab = SimpleNamespace(load_helper=lambda name: BACKUPS, DIRECTORY=Path('/tmp'), NODES=NODES)
        install.fixture.return_value = (None, lab)
        spec = Mock(); spec.loader.exec_module = Mock()
        with patch.object(V2.importlib.util, 'spec_from_file_location', return_value=spec), \
                patch.object(V2.importlib.util, 'module_from_spec', return_value=install):
            got, client, _ = V2.fixture()
        self.assertIs(got, install); self.assertIs(client, lab)
        self.assertEqual(lab.backup_store().endpoints, BACKUPS.V2_ENDPOINTS)
        calls = []; lab.cleanup_business = lambda: calls.append('business')
        lab.backup_store = lambda: SimpleNamespace(cleanup=lambda: calls.append('store'))
        V2.cleanup(lab); self.assertEqual(calls, ['business', 'store'])
        lab.cleanup_business = Mock(side_effect=ValueError('consumers remain')); lab.backup_store = Mock()
        with self.assertRaisesRegex(ValueError, 'consumers remain'): V2.cleanup(lab)
        lab.backup_store.assert_not_called()

    def test_baseline_rejects_consumers_journals_labels_and_listener(self):
        for fault in ('none', 'target', 'secret', 'backup', 'pod', 'credential', 'listener', 'journal'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); target = {'metadata': {'uid': 'original'}, 'spec': {'backupTargetURL': '', 'credentialSecret': ''}}
                if fault in ('target', 'secret'):
                    target['spec']['backupTargetURL' if fault == 'target' else 'credentialSecret'] = 'foreign'
                if fault == 'journal': (root/'longhorn-backup-store.json').touch()
                def get(kind, *args, **kwargs):
                    if kind == 'backuptargets.longhorn.io': return target
                    if kind == 'backups.longhorn.io' and fault == 'backup': return {'items': [{}]}
                    if kind in ('pods', 'secrets') and fault == ('pod' if kind == 'pods' else 'credential'):
                        return {'items': [{'metadata': {'labels': {'kubeauto.io/longhorn-backup-run': 'foreign'}}}]}
                    return {'items': []}
                store = SimpleNamespace(transport_listener=lambda: {} if fault == 'listener' else None)
                lab = SimpleNamespace(DIRECTORY=root, get=get, backup_store=lambda: store)
                install = Mock()
                with redirect_stdout(io.StringIO()) as output:
                    if fault == 'none': V2.baseline(install, lab, BACKUPS)
                    else:
                        with self.assertRaisesRegex(ValueError, 'retains consumers'): V2.baseline(install, lab, BACKUPS)
                install.volume_baseline.assert_called_once_with(lab)
                self.assertEqual('BASELINE_VERIFIED' in output.getvalue(), fault == 'none')

    def test_refusal_cli_requires_qualification_before_owned_suite(self):
        for failed in (False, True):
            lab = SimpleNamespace(require_lease=Mock(), backup_suite=Mock())
            with patch.object(V2, 'fixture', return_value=(None, lab, BACKUPS)), \
                    patch.object(V2, 'qualification_current', side_effect=ValueError('stale') if failed else None) as current, \
                    patch.object(sys, 'argv', ['backups.py', 'refusal', '--relay-pid', '12']):
                if failed:
                    with self.assertRaisesRegex(ValueError, 'stale'): V2.main()
                    lab.backup_suite.assert_not_called()
                else:
                    V2.main(); lab.backup_suite.assert_called_once_with(12, 'backup_refusal')
                current.assert_called_once_with(lab)
                lab.require_lease.assert_called_once()

    def test_business_requires_current_source_and_cluster_qualification(self):
        for fault in ('none', 'rc', 'failures', 'status', 'source', 'cluster'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); evidence = {'status': 'pass', 'rc': 0, 'failure_markers': 0,
                    'frozen_inputs': {}, 'baseline_identity': {'cluster_uid': 'original'}}
                files = ('tests/helpers/longhorn-backups.py', 'tests/helpers/longhorn-v2-backups.py',
                    'tests/helpers/longhorn-v2-backups.sh', 'tests/helpers/longhorn-v2-install.py')
                for name in files:
                    path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('frozen')
                    evidence['frozen_inputs'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
                if fault == 'rc': evidence['rc'] = 1
                if fault == 'failures': evidence['failure_markers'] = 1
                if fault == 'status': evidence['status'] = 'pending'
                if fault == 'source': (root/files[0]).write_text('changed')
                path = root/'tests/evidence/longhorn-v2-backup-store-qualification-20261020.json'; path.parent.mkdir()
                path.write_text(json.dumps(evidence)); lab = SimpleNamespace(owner=lambda: {'cluster_uid': 'changed' if fault == 'cluster' else 'original'})
                with patch.object(V2, 'BASE', root), redirect_stdout(io.StringIO()):
                    if fault == 'none': V2.qualification_current(lab)
                    else:
                        with self.assertRaises(ValueError): V2.qualification_current(lab)

    def test_dispatch_allowlists_and_owning_matrix(self):
        path = ROOT/'tests/helpers/longhorn-runner.sh'
        for mode in ('v2-backup-store-qualification', 'v2-backup-qualification', 'v2-backup-refusal-qualification'):
            script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-' + mode
            result = subprocess.run(['bash', '-c', script, 'contract', str(path)], text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, mode + ' LONGHORN_' + mode.upper().replace('-', '_') + '_PASS')
            self.assertIn(mode, (ROOT/'tests/helpers/longhorn-live.sh').read_text())
        cases = yaml.safe_load((ROOT/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        self.assertEqual([case['id'] for case in cases if 'backup_store_qualification' in case], ['LONGHORN-48'])

    def test_shell_requires_qualification_markers_restored_baseline_and_cleanup(self):
        source = (ROOT/'tests/helpers/longhorn-v2-backups.sh').read_text()
        for mode in ('v2-backup-store-qualification', 'v2-backup-qualification', 'v2-backup-refusal-qualification'):
            for fault in ('none', 'qualify', 'business', 'missing-store', 'wrong-clients', 'cleanup', 'baseline-changed',
                          'qualification-current', 'missing-restore', 'same-restore', 'wrong-remount', 'wrong-mode', 'empty-object',
                          'refusal', 'wrong-refusal', 'missing-key-restore'):
                if mode == 'v2-backup-store-qualification' and fault in ('business', 'qualification-current', 'missing-restore', 'same-restore', 'wrong-remount', 'wrong-mode', 'empty-object',
                          'refusal', 'wrong-refusal', 'missing-key-restore'): continue
                if mode == 'v2-backup-qualification' and fault in ('qualify', 'refusal', 'wrong-refusal', 'missing-key-restore'): continue
                if mode == 'v2-backup-refusal-qualification' and fault in ('qualify', 'business', 'same-restore'): continue
                with self.subTest(mode=mode, fault=fault), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                    (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                    (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n'); (base/'kube-bin/kubectl').chmod(0o755)
                    helper = '''import json,os,signal,sys,time
from pathlib import Path
mode=sys.argv[1];fault=os.environ['FAULT']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==fault:sys.exit(17)
if mode=='baseline':
 count=Path('baseline-count');n=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(n))
 print('LONGHORN_V2_BACKUP_BASELINE_VERIFIED target_sha256='+('changed' if n>1 and fault=='baseline-changed' else 'original')+' backup_objects=0 fixture_objects=0')
if mode in ('qualify','business','refusal'):
 if fault!='missing-store':print('LONGHORN_BACKUP_STORE_QUALIFIED clients='+('6' if fault=='wrong-clients' else '3')+' tls=verified object_readback=exact')
 print('LONGHORN_BACKUP_TRANSPORT_CLEAN_PASS');print('LONGHORN_BACKUP_STORE_CLEAN_PASS')
if mode=='refusal':
 for kind in ('credential','ca','endpoint'):
  if fault!='missing-key-restore' or kind!='ca':print('LONGHORN_BACKUP_REFUSAL_CREDENTIAL_RESTORED kind='+kind)
  print('LONGHORN_BACKUP_REFUSAL_VERIFIED kind='+('credential' if fault=='wrong-refusal' else kind)+' available=false request_created=false backup_objects=0')
if mode in ('business','refusal'):
 for name,digest,volume in ([('full','a','restore-full'),('incremental','b','restore-incremental')] if mode=='business' else [('full','a','restore-full')]):
  r={'volume':'source','backup':name,'size':2*1024**3,'incremental':(name=='incremental' and fault!='wrong-mode') if mode=='business' else fault=='wrong-mode','newly_uploaded_bytes':2097152,'objects':{'object':{'size':1 if fault!='empty-object' else 0,'sha256':'f'*64}}}
  print('LONGHORN_BACKUP_OBJECT_RECEIPT mode='+name+' '+json.dumps(r))
  if fault!='missing-restore' or name=='full' and mode=='business':
   print('LONGHORN_BACKUP_RESTORE_BOUNDARY_VERIFIED mode='+name+' backup_sha256='+digest*64+' source_sha256='+'c'*64+' independent_volume='+('restore-full' if fault=='same-restore' else volume))
 print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=1 sha256='+('a' if fault=='wrong-remount' else 'c')*64+' replica_nodes=a,b,c')
 print('LONGHORN_BACKUP_PRODUCT_FIXTURE_RESTORED');print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS');print('LONGHORN_BACKUP_BUSINESS_PASS' if mode=='business' else 'LONGHORN_BACKUP_REFUSAL_BUSINESS_PASS')
'''
                    for name in ('core', 'install', 'backups'): (base/f'tests/helpers/longhorn-v2-{name}.py').write_text(helper)
                    script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base))
                    result = subprocess.run(['bash', '-c', script, 'contract', mode], env=dict(os.environ,
                        FAULT=fault, LONGHORN_PARENT_LEASE_HELD='yes'), text=True, capture_output=True, timeout=20)
                    calls = (base/'calls.txt').read_text().splitlines()
                    self.assertEqual(result.returncode == 0, fault == 'none', result.stdout+result.stderr)
                    self.assertEqual('_SCOPED_CLEAN_PASS' in result.stdout, fault == 'none')
                    if fault == 'qualification-current':
                        self.assertEqual(calls, ['verify-transport', 'qualification-current']); continue
                    self.assertLess(calls.index('cleanup'), calls.index('relay-clean'))
                    self.assertEqual(calls[-1], 'verify-core')
                    if fault == 'none':
                        self.assertLess(calls.index('baseline'), calls.index('qualify' if mode.endswith('store-qualification') else 'refusal' if mode.endswith('refusal-qualification') else 'business'))

"""Contracts for V2 AIO configuration and reused native cleanup/I/O boundaries."""
from contextlib import contextmanager, redirect_stdout
import importlib.util
import io
import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import yaml

from tests.unit.test_longhorn_delivery import configuration, product

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v2_install_contract', ROOT/'tests/helpers/longhorn-v2-install.py')
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')


class InstallContracts(unittest.TestCase):
    def test_published_im_adoption_requires_its_own_healthy_identity_and_exact_input_transition(self):
        failures = ('none', 'pending', 'rc', 'failure', 'scope', 'version', 'image', 'remote-input',
                    'unhealthy', 'busy', 'journal', 'readback-identity', 'foreign-cluster',
                    'old-input', 'new-input', 'frozen-input', 'symlink', 'missing', 'unrelated',
                    'prior-bound', 'prior-proof-changed', 'prior-original', 'prior-new')
        for profile in ('v2', 'v2-dr-target'):
            for failure in failures:
                with self.subTest(profile=profile, failure=failure), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); (base/'tests/evidence').mkdir(parents=True)
                    name = 'roles/longhorn/files/'+('unrelated.py' if failure == 'unrelated' else 'longhorn.py')
                    source = base/name; source.parent.mkdir(parents=True); source.write_text('published pin')
                    current = hashlib.sha256(source.read_bytes()).hexdigest()
                    identity = {'cluster_uid': profile, 'namespace_uid': 'namespace'}
                    evidence = {'baseline_identity': identity, 'frozen_inputs': {name: 'old'}}
                    accepted = {'status': 'pass', 'rc': 0, 'failure_markers': 0,
                        'signed_scope': profile+'-im-adopt-qualification', 'version': '1.12.1',
                        'instance_manager_version': 'v1.12.1-kubeauto.1',
                        'remote_input_verification': {'mismatches': 0}, 'baseline_identity': dict(identity),
                        'independent_readback': {'installation_healthy': True, 'empty_business': True,
                            'adoption_journal_absent': True, 'baseline': {'baseline_identity': dict(identity)}},
                        'frozen_inputs': {name: current},
                        'input_compatibility': {name: {'original_sha256': 'old', 'sha256': current}}}
                    if failure == 'pending': accepted['status'] = 'pending'
                    if failure in ('rc', 'failure'): accepted['rc' if failure == 'rc' else 'failure_markers'] = 1
                    if failure == 'scope': accepted['signed_scope'] = 'v1-im-adopt-qualification'
                    if failure == 'version': accepted['version'] = '1.12.0'
                    if failure == 'image': accepted['instance_manager_version'] = 'v1.12.1'
                    if failure == 'remote-input': accepted['remote_input_verification']['mismatches'] = 1
                    for defect, key in (('unhealthy','installation_healthy'), ('busy','empty_business'),
                                        ('journal','adoption_journal_absent')):
                        if failure == defect: accepted['independent_readback'][key] = False
                    if failure == 'readback-identity': accepted['independent_readback']['baseline']['baseline_identity'] = {}
                    if failure == 'foreign-cluster': evidence['baseline_identity'] = {'cluster_uid': 'foreign'}
                    if failure == 'old-input': accepted['input_compatibility'][name]['original_sha256'] = 'foreign'
                    if failure == 'new-input': accepted['input_compatibility'][name]['sha256'] = 'foreign'
                    if failure == 'frozen-input': accepted['frozen_inputs'][name] = 'foreign'
                    if failure.startswith('prior-'):
                        prior = base/'prior-impact.json'; prior.write_text('accepted prior compatibility')
                        evidence['frozen_inputs'][name] = 'earliest'
                        evidence['compatible_inputs'] = {name: {'original_sha256': 'earliest',
                            'sha256': 'old', 'evidence': 'prior-impact.json',
                            'evidence_sha256': hashlib.sha256(prior.read_bytes()).hexdigest()}}
                        if failure == 'prior-proof-changed': prior.write_text('changed')
                        if failure == 'prior-original': evidence['compatible_inputs'][name]['original_sha256'] = 'other'
                        if failure == 'prior-new': evidence['compatible_inputs'][name]['sha256'] = 'other'
                    path = base/'tests/evidence'/('longhorn-'+profile+'-im-adopt-qualification-20261021.json')
                    if failure != 'missing': path.write_text(json.dumps(accepted))
                    if failure == 'symlink': path.rename(path.with_suffix('.other')); path.symlink_to(path.with_suffix('.other'))
                    with patch.object(INSTALL, 'BASE', base):
                        self.assertEqual(INSTALL.qualification_input_current(evidence, name), failure in ('none', 'prior-bound'))

    def test_dependency_compatibility_requires_exact_old_new_and_bound_proof(self):
        for failure in ('none', 'old', 'new', 'proof', 'missing', 'symlink'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base=Path(directory)
                (base/'input.py').write_text('current source')
                proof=base/'impact.json'
                proof.write_text('reviewed dependency impact')
                row={'original_sha256':'old',
                     'sha256':hashlib.sha256((base/'input.py').read_bytes()).hexdigest(),
                     'evidence':'impact.json', 'evidence_sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}
                evidence={'frozen_inputs':{'input.py':'old'},'compatible_inputs':{'input.py':row}}
                if failure=='old':row['original_sha256']='foreign'
                if failure=='new':(base/'input.py').write_text('different source')
                if failure=='proof':proof.write_text('different evidence')
                if failure=='missing':proof.unlink()
                if failure=='symlink':
                    proof.rename(base/'other');proof.symlink_to(base/'other')
                with patch.object(INSTALL,'BASE',base):
                    self.assertEqual(INSTALL.qualification_input_current(evidence,'input.py'),failure=='none')
                    # Exact original pins need no compatibility exception.
                    evidence['frozen_inputs']['input.py']=hashlib.sha256((base/'input.py').read_bytes()).hexdigest()
                    self.assertTrue(INSTALL.qualification_input_current(evidence,'input.py'))

    def test_recovery_before_namespace_only_restores_owned_host_files(self):
        for failure in ('none', 'namespace', 'crd'):
            lab = SimpleNamespace(core_identity=MagicMock(), owner=lambda: {'host_state': {'owned': True}},
                get=lambda kind, name: {} if failure == 'none' else {'metadata': {'uid': 'foreign'}} if kind == failure else None,
                recover_installation=MagicMock())
            with patch.object(INSTALL, 'restore_hosts') as restore:
                if failure == 'none':
                    INSTALL.recover(lab, 12); restore.assert_called_once_with(lab)
                else:
                    with self.assertRaises(ValueError): INSTALL.recover(lab, 12)
                    restore.assert_not_called()
            lab.recover_installation.assert_not_called()
        lab.owner = lambda: {'namespace_uid': 'original'}
        INSTALL.recover(lab, 12)
        lab.recover_installation.assert_called_once_with(12)

    def test_install_dispatch_and_allowlists(self):
        path = ROOT/'tests/helpers/longhorn-runner.sh'
        for mode in ('v2-snapshot-qualification', 'v2-volumes', 'v2-trim-qualification', 'v2-encryption-qualification', 'v2-reclaim-qualification', 'v2-expansion-qualification', 'v2-volume-modes-qualification', 'v2-host-qualification', 'v2-host-recovery', 'v2-aio-install-qualification'):
            script = 'source "$1"\nlonghorn_launch() { printf "%s %s" "$1" "$2"; }\nlonghorn_dispatch --longhorn-' + mode + '\n'
            result = subprocess.run(['bash', '-c', script, 'contract', str(path)], text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, mode + ' LONGHORN_' + mode.upper().replace('-', '_') + '_PASS')
        for variable in ('module', 'mode'):
            branches = re.findall(r'case "\$' + variable + r'" in ([^\n]+)', path.read_text())
            self.assertEqual(len(branches), 1)
            for mode in ('v2-snapshot-qualification', 'v2-volumes', 'v2-trim-qualification', 'v2-encryption-qualification', 'v2-reclaim-qualification', 'v2-expansion-qualification', 'v2-volume-modes-qualification', 'v2-host-qualification', 'v2-host-recovery', 'v2-aio-install-qualification'):
                self.assertIn(mode, branches[0].split(')')[0].split('|'))

    def test_snapshot_qualification_belongs_only_to_v2_data_protection(self):
        cases = yaml.safe_load((ROOT/'tests/longhorn-test-matrix.yaml').read_text())['cases']
        owning = [case for case in cases if 'snapshot_qualification' in case]
        self.assertEqual([case['id'] for case in owning], ['LONGHORN-48'])
        self.assertEqual(owning[0]['data_engines'], ['v2'])
        self.assertEqual(owning[0]['snapshot_qualification'],
            'bash tests/run_enterprise_regression.sh --longhorn-v2-snapshot-qualification </dev/null')

    def test_shell_preserves_healthy_storage_or_recovers_failure_in_dependency_order(self):
        source = (ROOT/'tests/helpers/longhorn-v2-install.sh').read_text()
        cases = [('v2-aio-install-qualification', failure) for failure in ('none', 'qualification-current', 'relay-ensure', 'quiesce-multipath', 'prepare', 'configure', 'cache', 'supervise', 'health', 'business', 'missing-marker', 'recover')]
        cases += [('v2-host-qualification', failure) for failure in ('none', 'quiesce-multipath', 'restore-hosts', 'smoke')]
        cases += [('v2-host-recovery', failure) for failure in ('none', 'recover-host-qualification', 'smoke')]
        for mode, failure in cases:
            with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True); (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n')
                (base/'kube-bin/kubectl').chmod(0o755)
                helper = """import os,signal,sys,time
mode=sys.argv[1]; failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='recover' and mode=='business'):sys.exit(17)
if mode=='business':
 print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
 print('LONGHORN_INSTALL_BUSINESS_PASS')
 if failure!='missing-marker':print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
"""
                for name in ('core', 'resources', 'install'):
                    (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-install.',str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', mode],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure), text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                marker = 'LONGHORN_' + mode.upper().replace('-', '_') + '_PASS'
                if mode == 'v2-host-recovery':
                    self.assertNotIn('capture-hosts', calls); self.assertNotIn('quiesce-multipath', calls)
                    self.assertNotIn('prepare', calls); self.assertNotIn('supervise', calls)
                    if failure == 'none': self.assertLess(calls.index('recover-host-qualification'), calls.index('smoke'))
                if mode == 'v2-host-qualification':
                    self.assertNotIn('prepare', calls); self.assertNotIn('supervise', calls)
                    self.assertNotIn('qualification-current', calls)
                    if failure == 'none':
                        self.assertLess(calls.index('restore-hosts'), calls.index('smoke'))
                        self.assertNotIn('health', calls)
                if failure == 'qualification-current':
                    self.assertNotIn('relay-ensure', calls)
                    self.assertNotIn('prepare', calls)
                    self.assertNotEqual(result.returncode, 0)
                    continue
                self.assertIn('relay-clean', calls)
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('recover', calls); self.assertNotIn('restore', calls)
                    self.assertIn(marker, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(marker, result.stdout)
                    if failure not in ('relay-ensure', 'smoke', 'recover-host-qualification'):
                        self.assertLess(calls.index('business-clean'), calls.index('recover'))
                        if failure != 'recover': self.assertLess(calls.index('recover'), calls.index('restore'))
                        else:self.assertNotIn('restore',calls)

    def test_configuration_selects_only_v2_and_exact_pinned_raw_devices_before_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yml'
            config = configuration()
            config.update(longhorn_install='no', longhorn_inventory_nodes=list(NODES), longhorn_client_nodes=list(NODES))
            path.write_text(yaml.safe_dump(config))
            state = {'raw_disks': {node: {'path': '/dev/disk/by-id/wwn-' + node,
                     'identity': 'wwn-' + node} for node in NODES}}
            lab = SimpleNamespace(NODES=NODES, DIRECTORY=Path(directory), owner=lambda: state,
                verify_core_smoke=MagicMock(), get=MagicMock(return_value=None), prepare_credentials=MagicMock())
            def credentials():
                current = yaml.safe_load(path.read_text())
                product.validate(current)
                self.assertEqual(current['longhorn_data_engines'], ['v2'])
                self.assertEqual(current['longhorn_storage_nodes'], [])
                self.assertEqual(current['longhorn_v2_cpu_mask'], '0x1')
                self.assertTrue(all(sc['data_engine'] == 'v2' for sc in current['longhorn_storage_classes']))
            lab.prepare_credentials.side_effect = credentials
            resources = SimpleNamespace(verify=MagicMock())
            with patch.object(INSTALL, 'load', return_value=resources):
                INSTALL.configure(lab)
            resources.verify.assert_called_once_with(lab)
            lab.prepare_credentials.assert_called_once()
            desired = yaml.safe_load(path.read_text())
            self.assertEqual(set(desired['longhorn_block_disks']), set(NODES))
            for node, disks in desired['longhorn_block_disks'].items():
                self.assertEqual(disks[0]['path'], state['raw_disks'][node]['path'])
                self.assertEqual(disks[0]['disk_driver'], 'aio')

    def test_native_business_must_use_v2_and_its_three_qualified_disk_uuids(self):
        for failure in ('none', 'volume-engine', 'disk-driver', 'disk-path', 'replica-engine', 'replica-disk'):
            state = {'raw_disks': {node: {'path': '/dev/disk/by-id/wwn-' + node} for node in NODES}}
            def get(kind, name='', namespace=''):
                if kind == 'volumes.longhorn.io':
                    return {'spec': {'dataEngine': 'v1' if failure == 'volume-engine' else 'v2'}}
                if kind == 'nodes.longhorn.io':
                    node = next(node for node in NODES if name.endswith(node.rsplit('.', 1)[1]))
                    return {'spec': {'disks': {'kubeauto-block': {'diskType': 'block',
                        'diskDriver': 'nvme' if failure == 'disk-driver' else 'aio',
                        'path': 'foreign' if failure == 'disk-path' else state['raw_disks'][node]['path']}}},
                        'status': {'diskStatus': {'kubeauto-block': {'diskUUID': name}}}}
                return {'items': [{'spec': {'volumeName': 'source', 'nodeID': 'longhorn-node-' + node.rsplit('.', 1)[1],
                    'dataEngine': 'v1' if failure == 'replica-engine' else 'v2',
                    'diskID': 'foreign' if failure == 'replica-disk' else 'longhorn-node-' + node.rsplit('.', 1)[1]}}
                    for node in NODES]}
            lab = SimpleNamespace(NODES=NODES, owner=lambda: state, get=get)
            with self.subTest(failure=failure):
                if failure == 'none':
                    self.assertEqual(INSTALL.healthy_replicas(lab, lambda _: list(NODES), 'source'), list(NODES))
                else:
                    with self.assertRaises(ValueError): INSTALL.healthy_replicas(lab, lambda _: list(NODES), 'source')

    def test_capture_includes_v2_module_file_and_restore_checks_every_host_before_mutation(self):
        state = {'raw_disks': {node: {'machine_id': node} for node in NODES}}
        def remote(_, args, *, stdin):
            request = json.loads(stdin)
            if request['action'] == 'capture': return SimpleNamespace(stdout='{"files":{},"units":{}}')
            if request['action'] == 'check' and request['before'].get('foreign'): raise ValueError('foreign third host')
            return SimpleNamespace(stdout='')
        lab = SimpleNamespace(NODES=NODES, STATE=Path('/owned/state'), DIRECTORY=Path('/owned'), owner=lambda: state,
            core_identity=MagicMock(), host_files=lambda: {'/etc/modules-load.d/kubeauto-longhorn.conf': b'base'},
            host_state_script=lambda: 'owned native script', remote=MagicMock(side_effect=remote),
            write_private=MagicMock(), execute=MagicMock())
        with patch.object(INSTALL, 'multipath', side_effect=lambda _, node, action, **kwargs: {'machine_id': node}):
            INSTALL.capture_hosts(lab)
        self.assertIn('/etc/modules-load.d/kubeauto-longhorn-v2.conf', state['host_state']['expected'])
        state['host_state']['nodes'][NODES[2]]['foreign'] = True
        lab.remote.reset_mock()
        with patch.object(INSTALL, 'multipath'), self.assertRaisesRegex(ValueError, 'foreign third host'):
            INSTALL.restore_hosts(lab)
        self.assertEqual([json.loads(c.kwargs['stdin'])['action'] for c in lab.remote.call_args_list], ['check']*3)
        self.assertIsNotNone(state['host_state'])

    def test_multipath_checks_all_hosts_before_stop_and_retains_ambiguous_writes(self):
        for failure in ('none', 'third-check', 'stop-response', 'old-intent'):
            rows = {node: {'multipath': {'phase': 'planned', 'original': node, 'current': node}}
                    for node in NODES}
            state = {'host_state': {'nodes': rows}}
            calls = []
            def multipath(_, node, action, **kwargs):
                calls.append((node, action))
                if failure == 'third-check' and node == NODES[2] and action == 'check':
                    raise ValueError('foreign third host')
                if failure == 'stop-response' and action == 'stop':
                    raise TimeoutError('response lost')
                return node + '-stopped' if action == 'stop' else node
            lab = SimpleNamespace(NODES=NODES, STATE=Path('/owned'), DIRECTORY=Path('/owned'), host_files=lambda: {}, owner=lambda: state,
                core_identity=MagicMock(), get=lambda *args: None, write_private=MagicMock(), execute=MagicMock())
            if failure == 'old-intent': rows[NODES[2]]['multipath']['phase'] = 'stop-intent'
            with self.subTest(failure=failure), patch.object(INSTALL, 'multipath', side_effect=multipath):
                if failure == 'none':
                    INSTALL.quiesce_multipath(lab)
                    self.assertEqual(calls, [(node, 'check') for node in NODES] + [(node, 'stop') for node in NODES])
                    self.assertTrue(all(row['multipath']['phase'] == 'stopped' for row in rows.values()))
                elif failure == 'stop-response':
                    with self.assertRaises(TimeoutError): INSTALL.quiesce_multipath(lab)
                    self.assertEqual(rows[NODES[0]]['multipath']['phase'], 'stop-intent')
                    calls.clear()
                    with self.assertRaisesRegex(ValueError, 'ambiguous'): INSTALL.restore_hosts(lab)
                    self.assertEqual(calls, [])
                else:
                    with self.assertRaises(ValueError): INSTALL.quiesce_multipath(lab)
                    self.assertNotIn('stop', [action for _, action in calls])

    def test_multipath_remote_program_restores_original_state_and_refuses_drift(self):
        # Execute the actual remote program against temporary host files and a
        # deterministic systemd/daemon API; no local host service is touched.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, data in (('etc/machine-id', 'machine'), ('proc/sys/kernel/random/boot_id', 'boot'),
                               ('sys/block/dm-0/dm/uuid', 'LVM-root'), ('sys/block/dm-1/dm/uuid', 'LVM-swap'),
                               ('etc/multipath.conf', 'defaults {}')):
                path = base/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
            units = {name: {'LoadState': 'loaded', 'ActiveState': 'active', 'UnitFileState': 'enabled'}
                     for name in ('multipathd.service', 'multipathd.socket')}
            maps = []; mutations = []
            def run(argv, **kwargs):
                if argv[0] == 'multipathd': return SimpleNamespace(stdout=json.dumps({'maps': maps}))
                if argv[1] == 'show':
                    return SimpleNamespace(stdout='\n'.join(k+'='+v for k,v in units[argv[2]].items()))
                if (argv[1:3] == ['start', 'multipathd.socket'] and
                        units['multipathd.service']['ActiveState'] == 'active'):
                    raise subprocess.CalledProcessError(1, argv, stderr='Socket service already active, refusing')
                mutations.append(argv)
                units[argv[2]]['ActiveState'] = {'start': 'active', 'stop': 'inactive'}[argv[1]]
                return SimpleNamespace(stdout='')
            source = INSTALL.multipath_script()
            for prefix in ('/etc/', '/sys/', '/proc/'):
                source = source.replace("'"+prefix, "'"+str(base)+prefix)
            def invoke(action, **kwargs):
                output = io.StringIO()
                with patch.object(sys, 'stdin', io.StringIO(json.dumps(dict(kwargs, action=action)))), \
                     patch.object(subprocess, 'run', side_effect=run), redirect_stdout(output):
                    exec(compile(source, '<multipath-host-program>', 'exec'), {})
                return json.loads(output.getvalue())
            original = invoke('probe')
            self.assertEqual(set(original['dm']), {'dm-0', 'dm-1'})
            for failure in ('maps', 'dm', 'config', 'boot'):
                with self.subTest(failure=failure):
                    if failure == 'maps': maps.append({'name': 'foreign'})
                    path = base/{'dm': 'sys/block/dm-1/dm/uuid', 'config': 'etc/multipath.conf',
                                 'boot': 'proc/sys/kernel/random/boot_id'}.get(failure, 'etc/machine-id')
                    prior = path.read_text()
                    if failure != 'maps': path.write_text('mpath-foreign' if failure == 'dm' else 'changed')
                    with self.assertRaises(AssertionError): invoke('stop', expected=original)
                    self.assertEqual(mutations, [])
                    maps.clear(); path.write_text(prior)
            stopped = invoke('stop', expected=original)
            self.assertEqual([call[2] for call in mutations], ['multipathd.socket', 'multipathd.service'])
            self.assertTrue(all(unit['UnitFileState'] == 'enabled' for unit in stopped['units'].values()))
            # The native systemd refusal is the exact first-failure cause.
            units['multipathd.service']['ActiveState'] = 'active'
            with self.assertRaises(subprocess.CalledProcessError): run(['systemctl', 'start', 'multipathd.socket'])
            units['multipathd.service']['ActiveState'] = 'inactive'
            self.assertEqual(invoke('restore', expected=stopped, original=original), original)
            self.assertTrue(all(call[1] in ('start', 'stop') for call in mutations))

    def test_exact_host_recovery_checks_every_identity_before_repair_and_retains_lost_response(self):
        for failure in ('none', 'journal', 'digest', 'third-host', 'lost-response'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'tests/evidence').mkdir(parents=True)
                journal = {'expected': {}, 'nodes': {node: {'multipath': {
                    'phase': 'restore-intent' if node == NODES[0] else 'stopped',
                    'current': {'units': {'multipathd.service': {'ActiveState': 'inactive'}}}}}
                    for node in NODES}}
                evidence = {'status': 'failed', 'classification': 'test-gate', 'recovery_journal': journal,
                    'recovery_journal_sha256': hashlib.sha256((json.dumps(journal,sort_keys=True)+'\n').encode()).hexdigest()}
                state = {'host_state': copy.deepcopy(journal)}
                if failure == 'journal': state['host_state']['expected']['foreign'] = 'foreign'
                if failure == 'digest': evidence['recovery_journal_sha256'] = 'wrong'
                (base/'tests/evidence/longhorn-v2-host-multipath-first-failure-20261020.json').write_text(json.dumps(evidence))
                calls = []
                def multipath(_, node, action, **request):
                    calls.append((node, action))
                    if failure == 'third-host' and node == NODES[2]: raise ValueError('foreign identity')
                    if action == 'stop' and failure == 'lost-response': raise TimeoutError('lost response')
                    return request['expected']
                lab = SimpleNamespace(NODES=NODES, DIRECTORY=base, STATE=base/'owner.json', owner=lambda: state,
                    core_identity=MagicMock(), get=lambda *args: None, write_private=MagicMock(), execute=MagicMock(),
                    host_files=lambda: {}, host_state_script=lambda: 'public host checker', remote=MagicMock())
                with patch.object(INSTALL, 'BASE', base), patch.object(INSTALL, 'multipath', side_effect=multipath), \
                     patch.object(INSTALL, 'restore_hosts') as restore:
                    if failure == 'none':
                        INSTALL.recover_host_qualification(lab)
                        self.assertEqual(calls, [(node, 'check') for node in NODES]+[(NODES[0], 'stop')])
                        restore.assert_called_once_with(lab)
                    else:
                        with self.assertRaises((ValueError, TimeoutError)): INSTALL.recover_host_qualification(lab)
                        restore.assert_not_called()
                        if failure != 'lost-response': self.assertNotIn('stop', [action for _, action in calls])
                        else: self.assertEqual(state['host_state']['nodes'][NODES[0]]['multipath']['phase'], 'recovery-stop-intent')

    def test_volume_modes_dispatches_reused_v2_bound_business_paths(self):
        for mode in ('business-rwo', 'business-rwx', 'business-reclaim', 'business-snapshot'):
            lab = MagicMock()
            with self.subTest(mode=mode), patch.object(INSTALL, 'fixture', return_value=(None, lab)), \
                 patch.object(sys, 'argv', ['install.py', mode, '--relay-pid', '12']):
                INSTALL.main()
            lab.require_lease.assert_called_once()
            if mode == 'business-rwo':
                lab.rwo_suite.assert_called_once_with(); lab.business.assert_not_called()
            elif mode == 'business-rwx':
                lab.business.assert_called_once_with('rwx', relay_pid=12, access_mode='ReadWriteMany')
            elif mode == 'business-snapshot':
                lab.business.assert_called_once_with('snapshot', relay_pid=12)
            else:
                lab.reclaim_suite.assert_called_once_with(12); lab.business.assert_not_called()

    def test_volume_baseline_requires_original_installation_inputs_identities_and_no_residue(self):
        for failure in ('none', 'failed-evidence', 'source', 'namespace-uid', 'receipt', 'node-uid', 'disk-uuid',
                        'volume', 'pv', 'namespace', 'journal', 'key-journal', 'request-journal', 'reclaim-journal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'tests/evidence').mkdir(parents=True)
                owned = base/'cluster'; (owned/'yml/longhorn').mkdir(parents=True)
                inputs = {}
                for name in ('roles/longhorn/files/longhorn.py', 'roles/longhorn/files/block-check.py',
                             'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py', 'tests/helpers/longhorn-v2-core.py'):
                    p=base/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b'owned source')
                    inputs[name]=hashlib.sha256(p.read_bytes()).hexdigest()
                # This baseline fixture covers .2. Keep its catalog explicit;
                # completed .3 adoption is exercised by its owning contracts.
                (base/'roles/longhorn/files/images.json').write_text(json.dumps({
                    '1.12.1': {'longhorn-instance-manager': 'v1.12.1-kubeauto.2'}}))
                receipt = owned/'yml/longhorn/block-disks.json'
                receipt.write_text(json.dumps({'nodes': {'ip': {'name': 'node', 'longhorn_node_uid': 'native-node',
                    'disks': {'disk': {'disk_uuid': 'native-disk'}}}}}))
                evidence={'status': 'pass', 'rc': 0, 'failure_markers': 0, 'frozen_inputs': inputs,
                    'baseline_identity': {'cluster_uid':'cluster','namespace_uid':'namespace'},
                    'native_block_receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()}
                if failure == 'failed-evidence': evidence['rc']=1
                if failure == 'source': (base/'roles/longhorn/files/longhorn.py').write_bytes(b'changed')
                if failure == 'receipt': receipt.write_text('{}')
                (base/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').write_text(json.dumps(evidence))
                records={'journal':'longhorn-install-business.json','key-journal':'longhorn-encryption-key.json',
                    'request-journal':'longhorn-request-input.json','reclaim-journal':'longhorn-reclaim-class.json'}
                if failure in records: (owned/records[failure]).write_text('{}')
                def get(kind,*args,**kwargs):
                    if kind == 'nodes.longhorn.io':
                        return {'metadata':{'uid':'foreign' if failure=='node-uid' else 'native-node'},
                            'status': {'diskStatus': {'disk': {'diskUUID': 'foreign' if failure=='disk-uuid' else 'native-disk'}}}}
                    if kind == 'volumes.longhorn.io': return {'items':[{}] if failure=='volume' else []}
                    if kind == 'pv': return {'items':[{'spec':{'csi':{'driver':'driver.longhorn.io'}}}] if failure=='pv' else []}
                    if kind == 'namespaces': return {'items':[{'metadata':{'name':'longhorn-business-foreign'}}] if failure=='namespace' else []}
                    raise AssertionError(kind)
                state={'cluster_uid':'cluster','namespace_uid':'foreign' if failure=='namespace-uid' else 'namespace'}
                lab=SimpleNamespace(DIRECTORY=owned,owner=lambda:state,get=get)
                with patch.object(INSTALL,'BASE',base), patch.object(INSTALL,'health') as health:
                    if failure=='none': INSTALL.volume_baseline(lab); health.assert_called_once_with(lab)
                    else:
                        with self.assertRaises(ValueError): INSTALL.volume_baseline(lab)

    def test_snapshot_shell_requires_both_isolated_restores_and_original_source_remount(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none', 'business-snapshot', 'missing-csi', 'duplicate', 'same-data', 'source-mismatch',
                        'remount', 'wrong-remount', 'refusal', 'scoped-clean', 'business-marker', 'cleanup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                kubectl = base/'kube-bin/kubectl'
                kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                helper = """import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='cleanup' and mode=='business-clean'):sys.exit(17)
if mode=='business-snapshot':
 for variant in ('csi','native'):
  if failure=='missing-csi' and variant=='csi':continue
  name='csi' if failure=='duplicate' else variant
  before='a'*64;after=('a' if failure=='same-data' else 'c' if failure=='source-mismatch' and variant=='native' else 'b')*64
  print('LONGHORN_SNAPSHOT_BOUNDARY_VERIFIED type='+name+' snapshot_sha256='+before+' source_sha256='+after)
 if failure!='remount':print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=13369344 sha256='+('c' if failure=='wrong-remount' else 'b')*64+' replica_nodes=a,b,c')
 if failure!='refusal':print('LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED')
 if failure!='scoped-clean':print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 if failure!='business-marker':print('LONGHORN_SNAPSHOT_BUSINESS_PASS')
"""
                for name in ('core', 'install'):
                    (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-snapshot-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertIn('business-clean', calls); self.assertIn('relay-clean', calls)
                self.assertNotIn('business-rwo', calls); self.assertNotIn('business-trim', calls)
                self.assertNotIn('recover', calls); self.assertNotIn('restore-hosts', calls)
                marker = 'LONGHORN_V2_SNAPSHOT_QUALIFICATION_PASS'
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr); self.assertIn(marker, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0); self.assertNotIn(marker, result.stdout)
                if failure in ('business-snapshot', 'cleanup'): self.assertEqual(result.returncode, 17)

    def test_complete_volume_module_runs_all_owned_branches_and_stops_at_first_failure(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        branches = ['v2-' + name + '-qualification' for name in ('volume-modes', 'expansion', 'reclaim', 'encryption', 'trim')]
        for failure in ('none', *branches):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'tests/helpers').mkdir(parents=True)
                child = base/'tests/helpers/longhorn-v2-volumes.sh'
                child.write_text('set -eu\nprintf "%s\\n" "$1" >> calls.txt\n[[ "$1" != "$FAIL_MODE" ]] || exit 17\n')
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-volumes'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                if failure == 'none':
                    self.assertEqual(calls, branches)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('LONGHORN_V2_VOLUMES_PASS', result.stdout)
                    self.assertIn('LONGHORN_V2_VOLUMES_SCOPED_CLEAN_PASS', result.stdout)
                else:
                    self.assertEqual(calls, branches[:branches.index(failure)+1])
                    self.assertEqual(result.returncode, 17)
                    self.assertNotIn('LONGHORN_V2_VOLUMES_PASS', result.stdout)

    def test_trim_suite_uses_v2_measurement_and_restores_loader_on_failure(self):
        for failure in (False, True):
            loader = MagicMock()
            lab = SimpleNamespace(load_helper=loader)
            def business(mode, *, relay_pid):
                self.assertEqual((mode, relay_pid), ('trim', 12))
                lab.load_helper('longhorn-trim.py')
                lab.load_helper('other.py')
                if failure: raise ValueError('trim failure')
            lab.business = business
            if failure:
                with self.assertRaisesRegex(ValueError, 'trim failure'): INSTALL.trim_suite(lab, 12)
            else: INSTALL.trim_suite(lab, 12)
            self.assertIs(lab.load_helper, loader)
            self.assertEqual([c.args for c in loader.call_args_list], [('longhorn-v2-trim.py',), ('other.py',)])
        lab = MagicMock()
        with patch.object(INSTALL, 'fixture', return_value=(None, lab)), \
             patch.object(INSTALL, 'trim_suite') as suite, \
             patch.object(sys, 'argv', ['install.py', 'business-trim', '--relay-pid', '12']):
            INSTALL.main()
        lab.require_lease.assert_called_once_with()
        suite.assert_called_once_with(lab, 12)

    def test_trim_shell_requires_three_actual_allocations_snapshot_and_business_cleanup(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none', 'business-trim', 'missing-result', 'duplicate-result', 'foreign-keys',
                        'no-retention', 'no-reclaim', 'no-guest-free', 'snapshot', 'remount', 'scoped-clean',
                        'business-marker', 'cleanup', 'digest-mismatch', 'invalid-digest', 'replica-uid', 'same-node'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                kubectl = base/'kube-bin/kubectl'
                kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                helper = """import json,os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='cleanup' and mode=='business-clean'):sys.exit(17)
if mode=='business-trim':
 row={'baseline':{n:1024**2 for n in ('a','b','c')},'retained':{n:65*1024**2 for n in ('a','b','c')},
      'after':{n:1024**2 for n in ('a','b','c')},'replicas':{n:{'uid':n,'node':n} for n in ('a','b','c')},
      'snapshot':{'name':'trim-owned','uid':'snap-uid'},'filler_bytes':64*1024**2,
      'guest_freed_bytes':64*1024**2,'confirmed_sha256':'a'*64,'filler_sha256':'b'*64}
 if failure=='foreign-keys':row['after']={'d':0,'e':0,'f':0}
 if failure=='no-retention':row['retained']=dict(row['baseline'])
 if failure=='no-reclaim':row['after']=dict(row['retained'])
 if failure=='no-guest-free':row['guest_freed_bytes']=0
 if failure=='snapshot':row['snapshot']['uid']=''
 if failure=='digest-mismatch':row['confirmed_sha256']='c'*64
 if failure=='invalid-digest':row['filler_sha256']='z'*64
 if failure=='replica-uid':row['replicas']['a']['uid']=''
 if failure=='same-node':row['replicas']['a']['node']='b'
 if failure!='missing-result':print('LONGHORN_V2_TRIM_RESULT '+json.dumps(row))
 if failure=='duplicate-result':print('LONGHORN_V2_TRIM_RESULT '+json.dumps(row))
 if failure!='remount':print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
 if failure!='scoped-clean':print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 if failure!='business-marker':print('LONGHORN_TRIM_BUSINESS_PASS')
"""
                for name in ('core', 'install'):
                    (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-trim-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertIn('business-clean', calls); self.assertIn('relay-clean', calls)
                self.assertNotIn('business-rwo', calls); self.assertNotIn('business-encryption', calls)
                self.assertNotIn('recover', calls); self.assertNotIn('restore-hosts', calls)
                marker = 'LONGHORN_V2_TRIM_QUALIFICATION_PASS'
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr); self.assertIn(marker, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0); self.assertNotIn(marker, result.stdout)
                if failure in ('business-trim', 'cleanup'): self.assertEqual(result.returncode, 17)

    def test_encryption_suite_binds_v2_only_and_restores_loader_on_failure(self):
        for failure in (None, False, True):
            probe = MagicMock()
            unrelated = SimpleNamespace(Probe=MagicMock())
            loader = MagicMock(side_effect=lambda name: SimpleNamespace(Probe=probe)
                               if name == 'longhorn-encryption.py' else unrelated)
            lab = SimpleNamespace(load_helper=loader)
            calls = []
            def business(mode, *, block, storage_class):
                calls.append((mode, block, storage_class))
                self.assertIs(lab.load_helper('other.py'), unrelated)
                lab.load_helper('longhorn-encryption.py').Probe(lab)
                if block is failure:
                    raise ValueError('business failure')
            lab.business = business
            with self.subTest(failure=failure), redirect_stdout(io.StringIO()) as output:
                if failure is None:
                    INSTALL.encryption_suite(lab)
                    self.assertIn('LONGHORN_V2_ENCRYPTION_BUSINESS_PASS', output.getvalue())
                else:
                    with self.assertRaisesRegex(ValueError, 'business failure'):
                        INSTALL.encryption_suite(lab)
                    self.assertNotIn('LONGHORN_V2_ENCRYPTION_BUSINESS_PASS', output.getvalue())
            self.assertIs(lab.load_helper, loader)
            self.assertEqual(calls, [('encryption', False, 'longhorn-encrypted')] +
                             ([] if failure is False else [('encryption', True, 'longhorn-encrypted')]))
            self.assertEqual(probe.call_count, len(calls))
            for call in probe.call_args_list:
                self.assertEqual(call.args, (lab,))
                self.assertEqual(call.kwargs, {'data_engine': 'v2'})
            unrelated.Probe.assert_not_called()
        lab = MagicMock()
        with patch.object(INSTALL, 'fixture', return_value=(None, lab)), \
             patch.object(INSTALL, 'encryption_suite') as suite, \
             patch.object(sys, 'argv', ['install.py', 'business-encryption', '--relay-pid', '12']):
            INSTALL.main()
        lab.require_lease.assert_called_once_with()
        suite.assert_called_once_with(lab)

    def test_encryption_shell_requires_each_refusal_recovery_and_cleanup(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none', 'business-encryption', 'missing-mode', 'missing-variant', 'duplicate',
                        'key-not-restored', 'mapping-open', 'restore-marker', 'remount',
                        'scoped-clean', 'business-marker', 'cleanup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'.venv/bin').mkdir(parents=True)
                (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'kube-bin').mkdir()
                kubectl = base/'kube-bin/kubectl'
                kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                helper = """import json,os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='cleanup' and mode=='business-clean'):sys.exit(17)
if mode=='business-encryption':
 for volume in ('Filesystem','Block'):
  for variant in ('wrong','missing'):
   if failure=='missing-mode' and volume=='Block':continue
   if failure=='missing-variant' and variant=='missing':continue
   row={'mode':volume,'variant':'wrong' if failure=='duplicate' else variant,
        'key_restored':failure!='key-not-restored','mapping_closed_before_refusal':failure!='mapping-open'}
   print('LONGHORN_ENCRYPTION_REFUSAL_VERIFIED '+json.dumps(row))
   if failure!='restore-marker':print('LONGHORN_ENCRYPTION_ORIGINAL_KEY_RESTORED variant='+variant)
  if failure!='remount':print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
  if failure!='scoped-clean':print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 if failure!='business-marker':print('LONGHORN_V2_ENCRYPTION_BUSINESS_PASS')
"""
                for name in ('core', 'install'):
                    (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-encryption-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure),
                    text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertIn('business-clean', calls); self.assertIn('relay-clean', calls)
                self.assertNotIn('business-rwo', calls); self.assertNotIn('business-expansion', calls)
                self.assertNotIn('recover', calls); self.assertNotIn('restore-hosts', calls)
                marker = 'LONGHORN_V2_ENCRYPTION_QUALIFICATION_PASS'
                if failure == 'none':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(marker, result.stdout)
                    self.assertIn(marker.replace('_PASS', '_SCOPED_CLEAN_PASS'), result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(marker, result.stdout)
                if failure in ('business-encryption', 'cleanup'):
                    self.assertEqual(result.returncode, 17)

    def test_expansion_checks_v2_frontend_and_luks_identity_and_full_capacity(self):
        for failure in ('none', 'uid', 'engine', 'frontend', 'label', 'initial-capacity', 'final-capacity', 'luks-uuid'):
            with self.subTest(failure=failure):
                volume = {'metadata': {'uid': 'foreign' if failure == 'uid' else 'volume',
                    'labels': {'v2-encrypted-volume-with-luks-header': 'false' if failure == 'label' else 'true'}},
                    'spec': {'dataEngine': 'v1' if failure == 'engine' else 'v2',
                             'frontend': 'ublk' if failure == 'frontend' else 'blockdev'}}
                before = {'luks_uuid': 'luks', 'mapped_bytes': 2*1024**3,
                          'raw_bytes': 2*1024**3 + 16*1024**2}
                after = {'luks_uuid': 'foreign' if failure == 'luks-uuid' else 'luks',
                         'mapped_bytes': 4*1024**3, 'raw_bytes': 4*1024**3 + 16*1024**2}
                if failure == 'initial-capacity': before['mapped_bytes'] -= 16*1024**2
                if failure == 'final-capacity': after['mapped_bytes'] -= 16*1024**2
                probe = SimpleNamespace(host_probe=MagicMock(side_effect=[before, after]))
                probe_factory = MagicMock(return_value=probe)
                lab = SimpleNamespace(WORKERS=NODES, get=MagicMock(return_value=volume),
                    load_helper=MagicMock(return_value=SimpleNamespace(Probe=probe_factory)))
                journal = {'volume': 'pvc-data', 'volume_uid': 'volume', 'storage_class': 'longhorn-encrypted'}
                original = MagicMock()
                if failure == 'none':
                    INSTALL.verify_expansion(lab, original, journal, 'payload', 'digest')
                    original.assert_called_once_with(journal, 'payload', 'digest', block=False)
                    self.assertEqual(probe.host_probe.call_count, 2)
                    probe_factory.assert_called_once_with(lab, data_engine='v2')
                else:
                    with self.assertRaises(ValueError): INSTALL.verify_expansion(lab, original, journal, 'payload', 'digest')
                    if failure not in ('final-capacity', 'luks-uuid'): original.assert_not_called()
        journal['storage_class'] = 'longhorn'
        volume['metadata']['uid'] = 'volume'; volume['spec'] = {'dataEngine': 'v2', 'frontend': 'blockdev'}
        lab.load_helper.reset_mock(); original.reset_mock()
        INSTALL.verify_expansion(lab, original, journal, 'payload', 'digest', block=True)
        original.assert_called_once_with(journal, 'payload', 'digest', block=True)
        lab.load_helper.assert_not_called()

    def test_expansion_suite_restores_its_override_after_success_and_failure(self):
        for failure in (False, True):
            original = MagicMock()
            lab = SimpleNamespace(verify_expansion=original)
            def suite():
                self.assertIsNot(lab.verify_expansion, original)
                lab.verify_expansion('journal', 'payload', 'digest', block=True)
                if failure: raise ValueError('business failure')
            lab.expansion_suite = suite
            with patch.object(INSTALL, 'verify_expansion') as verify:
                if failure:
                    with self.assertRaises(ValueError): INSTALL.expansion_suite(lab)
                else: INSTALL.expansion_suite(lab)
                verify.assert_called_once_with(lab, original, 'journal', 'payload', 'digest', block=True)
            self.assertIs(lab.verify_expansion, original)
        lab = MagicMock()
        with patch.object(INSTALL, 'fixture', return_value=(None, lab)), \
             patch.object(INSTALL, 'expansion_suite') as suite, \
             patch.object(sys, 'argv', ['install.py', 'business-expansion', '--relay-pid', '12']):
            INSTALL.main()
        lab.require_lease.assert_called_once_with()
        suite.assert_called_once_with(lab)

    def test_expansion_shell_requires_all_variants_and_capacity_evidence_before_pass(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none', 'business-expansion', 'missing-variant', 'missing-capacity', 'missing-remount', 'cleanup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n'); (base/'kube-bin/kubectl').chmod(0o755)
                helper = """import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='cleanup' and mode=='business-clean'):sys.exit(17)
if mode=='business-expansion':
 for variant in ['mode=Filesystem encrypted=false','mode=Block encrypted=false','mode=Filesystem encrypted=true']:
  if failure=='missing-variant' and 'Block' in variant:continue
  print('LONGHORN_ONLINE_EXPANSION_VERIFIED '+variant+' measurement={}')
  if failure!='missing-remount':print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
  print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 if failure!='missing-capacity':print('LONGHORN_V2_ENCRYPTED_EXPANSION_CAPACITY_VERIFIED {"mapped_bytes": 4294967296, "raw_bytes": 4311744512}')
 print('LONGHORN_EXPANSION_BUSINESS_PASS')
"""
                for name in ('core', 'install'): (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-expansion-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure), text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertNotIn('business-rwo', calls); self.assertNotIn('business-rwx', calls)
                self.assertIn('business-clean', calls); self.assertIn('relay-clean', calls)
                self.assertNotIn('recover', calls); self.assertNotIn('restore-hosts', calls)
                marker = 'LONGHORN_V2_EXPANSION_QUALIFICATION_PASS'
                if failure == 'none': self.assertEqual(result.returncode, 0, result.stderr); self.assertIn(marker, result.stdout)
                else: self.assertNotEqual(result.returncode, 0); self.assertNotIn(marker, result.stdout)
                if failure in ('business-expansion', 'cleanup'): self.assertEqual(result.returncode, 17)

    def test_reclaim_shell_requires_rebind_delete_and_both_product_refusals(self):
        source = (ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none', 'business-reclaim', 'rebind', 'delete', 'refusals', 'scoped-clean', 'cleanup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n'); (base/'kube-bin/kubectl').chmod(0o755)
                helper = """import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='cleanup' and mode=='business-clean'):sys.exit(17)
if mode=='business-reclaim':
 if failure!='rebind':print('LONGHORN_RETAIN_REBIND_VERIFIED pv=pv-original sha256='+'a'*64)
 if failure!='delete':print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
 for i in range(2):
  if failure!='refusals':print('LONGHORN_V2_EXPECTED_PRODUCT_REFUSAL_VERIFIED')
  if failure!='scoped-clean':print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 print('LONGHORN_RECLAIM_BUSINESS_PASS')
"""
                for name in ('core', 'install'): (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-volumes.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', script, 'contract', 'v2-reclaim-qualification'],
                    env=dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure), text=True, capture_output=True, timeout=15)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertNotIn('business-rwo', calls); self.assertNotIn('business-expansion', calls)
                self.assertIn('business-clean', calls); self.assertIn('relay-clean', calls)
                marker = 'LONGHORN_V2_RECLAIM_QUALIFICATION_PASS'
                if failure == 'none': self.assertEqual(result.returncode, 0, result.stderr); self.assertIn(marker, result.stdout)
                else: self.assertNotEqual(result.returncode, 0); self.assertNotIn(marker, result.stdout)
                if failure in ('business-reclaim', 'cleanup'): self.assertEqual(result.returncode, 17)

    def test_volume_mode_shell_stops_at_first_failure_and_preserves_baseline_on_cleanup(self):
        source=(ROOT/'tests/helpers/longhorn-v2-volumes.sh').read_text()
        for failure in ('none','volume-baseline','relay-ensure','business-rwo','rwo-marker','business-rwx','rwx-marker','cleanup','signal'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base=Path(directory); (base/'.venv/bin').mkdir(parents=True); (base/'tests/helpers').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable); (base/'kube-bin').mkdir()
                (base/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n'); (base/'kube-bin/kubectl').chmod(0o755)
                helper="""import os,signal,sys,time
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode==failure or (failure=='cleanup' and mode=='business-clean'):sys.exit(17)
if mode=='business-rwo' and failure=='signal':os.kill(os.getppid(),signal.SIGTERM);time.sleep(.05)
if mode=='business-rwo':
 for access,volume in [('ReadWriteOnce','Filesystem'),('ReadWriteOncePod','Filesystem'),('ReadWriteOnce','Block')]:
  print('LONGHORN_EXCLUSIVE_ATTACHMENT_VERIFIED access='+access+' mode='+volume)
  print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
  print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 if failure!='rwo-marker':print('LONGHORN_RWO_BUSINESS_PASS')
if mode=='business-rwx':
 print('LONGHORN_RWX_LOCKS_VERIFIED exclusion=two-clients release=verified')
 print('LONGHORN_RWX_RECOVERY_VERIFIED old_uid=old new_uid=new share_manager_ready_s=1')
 print('LONGHORN_RWX_CLIENT_RESULT {}\\nLONGHORN_RWX_CLIENT_RESULT {}')
 print('LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=a,b,c')
 print('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS')
 if failure!='rwx-marker':print('LONGHORN_RWX_BUSINESS_PASS')
"""
                for name in ('core','install'): (base/'tests/helpers'/('longhorn-v2-'+name+'.py')).write_text(helper)
                script=source.replace('BASE=/usr/local/kubeauto','BASE='+str(base)).replace('/var/tmp/kubeauto-longhorn-v2-volumes.',str(base/'work.'))
                result=subprocess.run(['bash','-c',script,'contract','v2-volume-modes-qualification'],
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',FAIL_MODE=failure),text=True,capture_output=True,timeout=15)
                calls=(base/'calls.txt').read_text().splitlines();marker='LONGHORN_V2_VOLUME_MODES_QUALIFICATION_PASS'
                if failure=='none':self.assertEqual(result.returncode,0,result.stderr);self.assertIn(marker,result.stdout)
                else:self.assertNotEqual(result.returncode,0);self.assertNotIn(marker,result.stdout)
                if failure in ('business-rwo','rwo-marker','signal'):self.assertNotIn('business-rwx',calls)
                if failure=='volume-baseline':self.assertNotIn('relay-ensure',calls)
                else:self.assertIn('business-clean',calls);self.assertIn('relay-clean',calls)
                self.assertNotIn('recover',calls);self.assertNotIn('restore-hosts',calls)
                if failure in ('business-rwo','business-rwx','cleanup'):self.assertEqual(result.returncode,17)
                if failure=='signal':self.assertEqual(result.returncode,143,result.stderr)

    def test_supervisor_refuses_other_helper_identity_before_product_start(self):
        lab = SimpleNamespace(owner=lambda: {}, process_identity=lambda _: ([b'foreign', b'relay-maintain'], 'start'))
        with patch.object(INSTALL.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'exact live Registry'): INSTALL.supervise(lab, 10, 'longhorn-setup')
        popen.assert_not_called()

    def test_supervisor_uses_documented_cli_and_stops_on_relay_loss(self):
        @contextmanager
        def cleanup(callback):
            try: yield
            finally: callback()
        identity = ([str(Path(INSTALL.__file__).resolve()).encode(), b'relay-maintain'], 'start')
        lab = SimpleNamespace(CLUSTER='longhorn-v2-gate', owner=lambda: {},
            process_identity=MagicMock(side_effect=[identity, None]), scoped_cleanup=cleanup, stop_product=MagicMock())
        process = SimpleNamespace(poll=lambda: None)
        with patch.object(INSTALL.subprocess, 'Popen', return_value=process) as popen:
            with self.assertRaisesRegex(ValueError, 'relay exited'): INSTALL.supervise(lab, 10, 'longhorn-setup')
        self.assertEqual(popen.call_args.args[0][-3:], ['setup', 'longhorn-v2-gate', '09'])
        lab.stop_product.assert_called_once()

    def test_health_requires_only_three_v2_instance_managers(self):
        for failure in ('none', 'v1-enabled', 'extra-manager', 'v1-manager'):
            def get(kind, name='', namespace=''):
                if kind == 'settings.longhorn.io':
                    return {'value': 'true' if name == 'v2-data-engine' or failure == 'v1-enabled' else 'false'}
                return {'items': [{'spec': {'dataEngine': 'v1' if failure == 'v1-manager' else 'v2'}}
                                  for _ in range(4 if failure == 'extra-manager' else 3)]}
            lab = SimpleNamespace(get=get, installation_health=MagicMock())
            with self.subTest(failure=failure):
                if failure == 'none': INSTALL.health(lab)
                else:
                    with self.assertRaises(ValueError): INSTALL.health(lab)


if __name__ == '__main__':
    unittest.main()

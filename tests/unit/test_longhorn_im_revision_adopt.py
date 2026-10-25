"""Exact, independently signed .1 -> .2 adoption transition contracts."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('im_revision', ROOT/'tests/helpers/longhorn-im-revision-adopt.py')
REVISION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVISION)


class RevisionAdoptionContracts(unittest.TestCase):
    def test_actual_driver_signals_unwind_owned_product_scope(self):
        from contextlib import contextmanager
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
                events = []
                @contextmanager
                def product_scope():
                    try: yield
                    finally: events.append('product-stopped')
                def run():
                    with product_scope(): os.kill(os.getpid(), signum)
                try:
                    with patch.object(REVISION, 'profile', return_value=SimpleNamespace(main=run)):
                        with self.assertRaises(SystemExit) as stopped: REVISION.main()
                    self.assertEqual(128 + signum, stopped.exception.code)
                    self.assertEqual(['product-stopped'], events)
                finally:
                    for number, handler in previous.items(): signal.signal(number, handler)

    def fixture(self, base, engine='v2'):
        def write(name, value):
            path = base/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value) if isinstance(value, dict) else value)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        source = '''import json
from pathlib import Path
FILES=Path(__file__).parent
class API:
 def __init__(self,path):pass
def node_map(c,api):return {}
def healthy(c,api,nodes):
 return json.loads((FILES/'images.json').read_text())['1.12.1']['longhorn-instance-manager']=='v1.12.1-kubeauto.1'
'''
        names = set(REVISION.CORE_INPUTS) | {'playbooks/09.longhorn.yml'}
        for name in names:
            write(name, (ROOT/name).read_text() if name in (REVISION.ADOPTER, REVISION.DRIVER) else 'frozen\n')
        write(REVISION.SOURCE, source)
        write(REVISION.CATALOG, {'1.12.1': {'longhorn-instance-manager': REVISION.NEW_TAG}})
        before = {'baseline_identity': {'cluster_uid': 'cluster', 'namespace_uid': 'namespace'},
            'native_block_receipt_sha256': 'block-receipt', 'config_sha256': 'config'}
        old = {'im': {'uid': 'old-im', 'node': 'longhorn-node-149' if engine == 'v2-dr-target' else 'longhorn-node-146',
                       'image': 'registry/longhorn-instance-manager:'+REVISION.OLD_TAG}}
        prior = {'status': 'pass', 'rc': 0, 'failure_markers': 0, 'version': '1.12.1',
            'durable': {'exit': 0, 'finalized': 0}, 'signed_scope': engine+'-im-adopt-qualification',
            'instance_manager_version': REVISION.OLD_TAG, 'baseline_identity': before['baseline_identity'],
            'remote_input_verification': {'mismatches': 0},
            'independent_readback': {'installation_healthy': True, 'empty_business': True, 'adoption_journal_absent': True,
                'baseline': before, 'managers': old},
            'frozen_inputs': {name: REVISION.digest(base/name) for name in names if name != REVISION.DRIVER}}
        previous = 'tests/evidence/longhorn-'+engine+'-im-adopt-qualification-20261021.json'
        previous_sha = write(previous, prior)
        publication = {'status': 'pass', 'durable_rc': 0, 'failure_markers': 0, 'selected_image_count': 1,
            'image': 'brinnatt/longhorn-instance-manager:'+REVISION.NEW_TAG,
            'publication_record': {'key': 'brinnatt/longhorn-instance-manager:'+REVISION.NEW_TAG,
                'digest': 'sha256:'+'a'*64, 'platforms': {'linux/amd64': 'amd', 'linux/arm64': 'arm'}}}
        pub = 'tests/evidence/fixture-publication.json'
        plan = {'schema': 1, 'old_tag': REVISION.OLD_TAG, 'new_tag': REVISION.NEW_TAG,
            'publication_evidence': {'path': pub, 'sha256': write(pub, publication)},
            'profiles': {engine: {'previous_evidence': {'path': previous, 'sha256': previous_sha},
                'inputs': {name: {'old_sha256': prior['frozen_inputs'].get(name),
                    'sha256': REVISION.digest(base/name), 'source': 'official pinned source + accepted .1 evidence',
                    'impact': 'Exact bounded lifecycle integration'} for name in names}}}}
        write(REVISION.PLAN, plan)
        return plan, prior, before, old, write

    def test_exact_plan_binds_previous_independent_receipt_and_all_inputs(self):
        for engine in REVISION.ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as d:
                base = Path(d); plan, prior, _, _, _ = self.fixture(base, engine)
                with patch.object(REVISION, 'BASE', base):
                    actual = REVISION.read_plan(engine)
                    self.assertEqual((plan, plan['profiles'][engine], prior), actual[:3])
                    self.assertEqual(REVISION.digest(base/REVISION.PLAN), actual[3])
                    accepted = REVISION.qualification(engine)
                    self.assertEqual(prior['baseline_identity'], accepted['baseline_identity'])
                    if engine != 'v1': self.assertEqual('block-receipt', accepted['native_block_receipt_sha256'])
                self.assertEqual(prior, json.loads((base/plan['profiles'][engine]['previous_evidence']['path']).read_text()))

    def test_changed_or_incomplete_plan_never_grants_a_hash_exception(self):
        defects = ('old-tag', 'new-tag', 'missing-input', 'missing-retire', 'missing-upgrade-consumer', 'missing-wrapper', 'old-hash', 'new-hash', 'source', 'impact',
                   'previous-sha', 'other-profile', 'status', 'durable', 'health', 'identity', 'image', 'remote',
                   'catalog', 'publication', 'arm64', 'symlink', 'changed-file', 'traversal')
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as d:
                base = Path(d); plan, prior, _, _, write = self.fixture(base)
                selected = plan['profiles']['v2']; entry = selected['inputs'][REVISION.SOURCE]
                if defect == 'old-tag': plan['old_tag'] = 'v1.12.1'
                if defect == 'new-tag': plan['new_tag'] = 'mutable'
                if defect == 'missing-input': del selected['inputs'][REVISION.SOURCE]
                if defect == 'missing-retire': del selected['inputs']['roles/longhorn/files/block-retire.py']
                if defect == 'missing-upgrade-consumer': del selected['inputs']['tests/helpers/longhorn-v2-upgrade-baseline.py']
                if defect == 'missing-wrapper': del selected['inputs']['tests/helpers/longhorn-v2-im-adopt.sh']
                if defect == 'old-hash': entry['old_sha256'] = 'foreign'
                if defect == 'new-hash': entry['sha256'] = 'foreign'
                if defect == 'source': entry['source'] = ''
                if defect == 'impact': entry['impact'] = ''
                if defect == 'previous-sha': selected['previous_evidence']['sha256'] = 'foreign'
                if defect == 'other-profile': selected['previous_evidence']['path'] = 'tests/evidence/other.json'
                if defect == 'status': prior['status'] = 'pending'
                if defect == 'durable': prior['durable']['finalized'] = 1
                if defect == 'health': prior['independent_readback']['empty_business'] = False
                if defect == 'identity': prior['independent_readback']['baseline']['baseline_identity'] = {'cluster_uid': 'other'}
                if defect == 'image': prior['independent_readback']['managers']['im']['image'] = 'registry/mutable'
                if defect == 'remote': prior['remote_input_verification']['mismatches'] = 1
                if defect in ('status', 'durable', 'health', 'identity', 'image', 'remote'):
                    selected['previous_evidence']['sha256'] = write(selected['previous_evidence']['path'], prior)
                if defect == 'catalog':
                    entry = selected['inputs'][REVISION.CATALOG]
                    entry['sha256'] = write(REVISION.CATALOG, {'1.12.1': {'longhorn-instance-manager': REVISION.OLD_TAG}})
                if defect in ('publication', 'arm64'):
                    path = plan['publication_evidence']['path']; value = json.loads((base/path).read_text())
                    if defect == 'publication': value['durable_rc'] = 1
                    else: del value['publication_record']['platforms']['linux/arm64']
                    plan['publication_evidence']['sha256'] = write(path, value)
                if defect == 'symlink':
                    original = base/REVISION.SOURCE; moved = base/'moved'; original.rename(moved); original.symlink_to(moved)
                if defect == 'changed-file': (base/REVISION.SOURCE).write_text('unreviewed')
                if defect == 'traversal': selected['inputs']['../foreign'] = copy.deepcopy(entry)
                write(REVISION.PLAN, plan)
                with patch.object(REVISION, 'BASE', base), self.assertRaises(ValueError): REVISION.read_plan('v2')

    def test_old_health_is_read_only_and_uses_private_catalog_with_exact_old_image(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); self.fixture(base)
            directory = base/'cluster'; path = directory/'yml/longhorn/input.json'; path.parent.mkdir(parents=True)
            path.write_text('{}')
            lab = SimpleNamespace(DIRECTORY=directory, WORKERS=['192.168.47.146'])
            original = (base/REVISION.SOURCE).read_text(); catalog = (base/REVISION.CATALOG).read_bytes()
            with patch.object(REVISION, 'BASE', base):
                REVISION.old_health(lab, original)
                with self.assertRaises(ValueError): REVISION.old_health(lab, original+'\n# unreviewed')
            self.assertEqual(catalog, (base/REVISION.CATALOG).read_bytes())
            self.assertEqual(original, (base/REVISION.SOURCE).read_text())

    def test_thin_profile_reuses_exact_product_business_path_and_persists_transition_before_setup(self):
        for engine in REVISION.ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as d:
                base = Path(d); plan, prior, before, old, _ = self.fixture(base, engine)
                directory = base/'cluster'; directory.mkdir()
                (directory/'config.yml').write_text('longhorn_version: 1.12.1\nlonghorn_image_prefix: registry\nlonghorn_data_engines: ['+('v1' if engine == 'v1' else 'v2')+']\n')
                calls = []; written = []
                def write(path, row):
                    calls.append('journal'); written.append(copy.deepcopy(row)); path.write_text(json.dumps(row))
                def setup(pid, requests):
                    calls.append('setup09'); self.assertEqual((7, []), (pid, requests))
                    self.assertEqual('setup-intent', written[-1]['phase'])
                    self.assertEqual(REVISION.digest(base/REVISION.PLAN), written[-1]['transition']['plan']['sha256'])
                    self.assertEqual(plan['profiles'][engine]['previous_evidence'], written[-1]['transition']['previous_evidence'])
                lab = SimpleNamespace(DIRECTORY=directory, owner=lambda: {'run_id': 'run'}, write_private=write,
                    run_requests=setup, wait=lambda name, timeout, fn: fn(), business=Mock())
                install = SimpleNamespace(health=Mock(), business=Mock())
                new = {'new': dict(next(iter(old.values())), uid='new-im', image='registry/longhorn-instance-manager:'+REVISION.NEW_TAG)}
                original_write = lab.write_private
                with patch.object(REVISION, 'BASE', base):
                    gate = REVISION.profile()
                    with patch.object(gate, 'baseline', return_value=before), patch.object(gate, 'old_health'), \
                            patch.object(gate, 'managers', side_effect=[old, new, new]), redirect_stdout(io.StringIO()):
                        gate.adopt(install, lab, 7, engine)
                self.assertEqual('verified', written[-1]['phase'])
                self.assertEqual('journal', calls[0]); self.assertEqual('setup09', calls[1])
                self.assertEqual(REVISION.OLD_TAG, written[0]['transition']['old_tag'])
                self.assertEqual(REVISION.NEW_TAG, written[0]['transition']['new_tag'])
                self.assertIs(original_write, lab.write_private)
                if engine == 'v2-dr-target':
                    install.business.assert_called_once_with(lab, 7); lab.business.assert_not_called()
                else: lab.business.assert_called_once_with('install', relay_pid=7)
                self.assertEqual(prior, json.loads((base/plan['profiles'][engine]['previous_evidence']['path']).read_text()))

    def test_finalize_and_cleanup_reject_other_plan_before_health_or_business_delete(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d); self.fixture(base)
            directory = base/'cluster'; directory.mkdir()
            lab = SimpleNamespace(DIRECTORY=directory, write_private=Mock(), cleanup_business=Mock())
            install = SimpleNamespace(health=Mock())
            with patch.object(REVISION, 'BASE', base):
                gate = REVISION.profile()
                path = directory/gate.RECORD
                path.write_text(json.dumps({'transition': {'plan': {'sha256': 'foreign'}}}))
                with self.assertRaisesRegex(ValueError, 'finalization'): gate.finish(install, lab)
                with self.assertRaisesRegex(ValueError, 'another transition'): gate.clean(lab, 7)
            install.health.assert_not_called(); lab.cleanup_business.assert_not_called()

    def test_complete_log_binds_transition_plan_and_previous_receipt(self):
        for engine in REVISION.ENGINES:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as d:
                base = Path(d); plan, _, before, _, _ = self.fixture(base, engine)
                nodes = ['longhorn-node-'+str(n) for n in {'v1': (212,165,238), 'v2': (146,147,148), 'v2-dr-target': (149,)}[engine]]
                before['required_manager_nodes'] = nodes
                old = {n: {'uid': 'old-'+n, 'node': n, 'image': 'registry/longhorn-instance-manager:'+REVISION.OLD_TAG} for n in nodes}
                new = {n: {'uid': 'new-'+n, 'node': n, 'image': 'registry/longhorn-instance-manager:'+REVISION.NEW_TAG} for n in nodes}
                selected = plan['profiles'][engine]; sha = REVISION.digest(base/REVISION.PLAN)
                result = {'phase': 'verified', 'engine': engine, 'before': before, 'old_managers': old, 'new_managers': new,
                    'plan_sha256': sha, 'transition': {'plan': {'path': REVISION.PLAN, 'sha256': sha},
                        'previous_evidence': selected['previous_evidence'], 'publication_evidence': plan['publication_evidence'],
                        'old_tag': REVISION.OLD_TAG, 'new_tag': REVISION.NEW_TAG},
                    'frozen_inputs': {n: entry['sha256'] for n, entry in selected['inputs'].items()}}
                with patch.object(REVISION, 'BASE', base):
                    gate = REVISION.profile(); prefix = gate.prefix(engine)
                    def log(row):
                        lines = [prefix+'_PRECONDITIONS_VERIFIED '+json.dumps(before),
                            prefix+'_RUNTIME_VERIFIED '+json.dumps(new), prefix+'_RESULT '+json.dumps(row),
                            prefix+'_BUSINESS_PASS', prefix+'_FIXTURES_CLEAN_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS']
                        if engine == 'v2-dr-target':
                            lines += ['LONGHORN_V2_DR_TARGET_INSTALL_BUSINESS_PASS',
                                'LONGHORN_V2_DR_TARGET_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replicas=1 cross_node=false']
                        else:
                            lines += ['LONGHORN_INSTALL_BUSINESS_PASS',
                                'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes='+','.join(nodes)]
                        return '\n'.join(lines)
                    gate.verify_log(log(result), engine)
                    for defect in ('plan', 'previous', 'input', 'tag'):
                        changed = copy.deepcopy(result)
                        if defect == 'plan': changed['plan_sha256'] = 'foreign'
                        if defect == 'previous': changed['transition']['previous_evidence']['sha256'] = 'foreign'
                        if defect == 'input': changed['frozen_inputs'][REVISION.SOURCE] = 'foreign'
                        if defect == 'tag': changed['transition']['old_tag'] = 'v1.12.1'
                        with self.assertRaises(ValueError): gate.verify_log(log(changed), engine)

    def test_revision_shell_success_first_failure_cleanup_and_sigterm(self):
        source = (ROOT/'tests/helpers/longhorn-v2-im-adopt.sh').read_text()
        helper = '''import os,signal,sys,time
from pathlib import Path
mode=sys.argv[1];failure=os.environ['FAIL_MODE']
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as f:f.write(Path(__file__).name+':'+mode+'\\n')
if mode=='business' and failure=='sigterm':
 Path('business-ready').touch()
 while True:time.sleep(.01)
if mode==failure or (failure=='clean' and mode=='business'):sys.exit(17)
if mode=='readback' and failure!='missing-readback':
 engine=sys.argv[sys.argv.index('--engine')+1]
 print('LONGHORN_'+engine.upper().replace('-','_')+'_IM_REVISION_2_ADOPT_INDEPENDENT_READBACK {}')
 if failure!='missing-readback-file':Path(sys.argv[sys.argv.index('--output')+1]).write_text('{}')
'''
        for engine in REVISION.ENGINES:
            for failure in ('none', 'cache', 'business', 'finish', 'verify', 'clean', 'relay-clean', 'readback', 'missing-readback', 'missing-readback-file', 'sigterm'):
                with self.subTest(engine=engine, failure=failure), tempfile.TemporaryDirectory() as d:
                    base = Path(d); (base/'.venv/bin').mkdir(parents=True)
                    (base/'.venv/bin/python').symlink_to(sys.executable)
                    (base/'tests/helpers').mkdir(parents=True); (base/'kube-bin').mkdir()
                    kubectl = base/'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
                    for name in ('longhorn-lab.py', 'longhorn-v2-core.py', 'longhorn-v2-install.py',
                                 'longhorn-v2-dr-target.py', 'longhorn-im-revision-adopt.py'):
                        (base/'tests/helpers'/name).write_text(helper)
                    script = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(base)).replace(
                        '/var/tmp/kubeauto-longhorn-v2-im-adopt.', str(base/'work.'))
                    argv = ['bash', '-c', script, 'test', engine+'-im-revision-2-adopt-qualification']
                    env = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', FAIL_MODE=failure)
                    process = subprocess.Popen(argv, env=env, text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, start_new_session=True)
                    try:
                        if failure == 'sigterm':
                            deadline = time.monotonic()+5
                            while not (base/'business-ready').exists() and process.poll() is None and time.monotonic()<deadline:
                                time.sleep(.01)
                            self.assertTrue((base/'business-ready').exists())
                            os.killpg(process.pid, signal.SIGTERM)
                        stdout, stderr = process.communicate(timeout=10)
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL); process.communicate(timeout=5)
                    calls = (base/'calls.txt').read_text().splitlines()
                    modes = [line.split(':')[1] for line in calls]
                    expected = 'LONGHORN_'+engine.upper().replace('-', '_')+'_IM_REVISION_2_ADOPT_QUALIFICATION'
                    self.assertFalse(set(modes) & {'recover', 'destroy', 'restore-hosts', 'configure', 'prepare'})
                    self.assertIn('relay-clean', modes)
                    self.assertIn('longhorn-im-revision-adopt.py:cache', calls)
                    exit_paths = list(base.glob('work.*/exit')); self.assertEqual(1, len(exit_paths))
                    self.assertEqual(process.returncode, int(exit_paths[0].read_text()))
                    if failure == 'none':
                        self.assertEqual(0, process.returncode, stderr)
                        self.assertIn(expected+'_SCOPED_CLEAN_PASS', stdout)
                        self.assertIn(expected+'_PASS', stdout)
                        self.assertLess(modes.index('cache'), modes.index('business'))
                        self.assertLess(modes.index('verify'), modes.index('readback'))
                        self.assertLess(modes.index('relay-clean'), modes.index('readback'))
                    else:
                        self.assertNotEqual(0, process.returncode)
                        self.assertNotIn(expected+'_PASS', stdout)
                        if failure == 'sigterm': self.assertEqual(143, process.returncode)
                        if failure not in ('relay-clean', 'readback', 'missing-readback', 'missing-readback-file'):
                            self.assertIn('longhorn-im-revision-adopt.py:clean', calls)

    def test_independent_readback_checks_live_identities_empty_scope_and_completed_log(self):
        for engine in REVISION.ENGINES:
            for defect in ('none', 'baseline', 'managers', 'journal', 'journal-symlink', 'health', 'log'):
                with self.subTest(engine=engine, defect=defect), tempfile.TemporaryDirectory() as d:
                    base=Path(d); directory=base/'cluster'; directory.mkdir()
                    config=directory/'yml/longhorn/input.json'; config.parent.mkdir(parents=True)
                    config.write_text(json.dumps({'longhorn_image_prefix': 'registry'}))
                    baseline={'baseline_identity': {'namespace_uid': 'original'}}
                    managers={'im': {'uid': 'new', 'image': 'registry/longhorn-instance-manager:'+REVISION.NEW_TAG}}
                    result={'before': baseline, 'new_managers': managers, 'plan_sha256': 'plan', 'frozen_inputs': {'input': 'sha'}}
                    log=base/'business.log'; log.write_text('PREFIX_RESULT '+json.dumps(result)+'\n')
                    gate=SimpleNamespace(prefix=lambda _: 'PREFIX', verify_log=Mock(), baseline=Mock(return_value=baseline),
                        managers=Mock(return_value=managers), empty=Mock(), RECORD='longhorn-v2-im-revision-2-adopt.json')
                    install=SimpleNamespace(health=Mock());lab=SimpleNamespace(DIRECTORY=directory, require_lease=Mock())
                    journal=directory/gate.RECORD.replace('v2', engine)
                    if defect=='baseline':gate.baseline.return_value={}
                    if defect=='managers':gate.managers.return_value={}
                    if defect=='journal':journal.write_text('{}')
                    if defect=='journal-symlink':journal.symlink_to(base/'absent')
                    if defect=='health':install.health.side_effect=ValueError('unhealthy')
                    if defect=='log':gate.verify_log.side_effect=ValueError('incomplete log')
                    with patch.object(REVISION,'readback_fixture',return_value=(gate,install,lab)), \
                            patch.object(REVISION,'qualification',return_value={}), \
                            patch.object(REVISION,'read_plan',return_value=({}, {'inputs': {'input': {'sha256': 'sha'}}}, {}, 'plan')), \
                            redirect_stdout(io.StringIO()) as output:
                        if defect=='none':
                            row=REVISION.readback(engine,log)
                            self.assertEqual(baseline,row['baseline']);self.assertEqual(managers,row['managers'])
                            self.assertEqual(hashlib.sha256(log.read_bytes()).hexdigest(),row['business_log_sha256'])
                            self.assertIn('PREFIX_INDEPENDENT_READBACK ',output.getvalue())
                        else:
                            with self.assertRaises(ValueError):REVISION.readback(engine,log)
                            self.assertNotIn('INDEPENDENT_READBACK',output.getvalue())
                    lab.require_lease.assert_called_once();gate.verify_log.assert_called_once_with(log.read_text(),engine)


if __name__ == '__main__':
    unittest.main()

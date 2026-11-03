"""Preserve the exact native metadata before a bounded support-bundle refusal."""
import contextlib
import copy
import importlib.util
import io
import json
import hashlib
import tempfile
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('support_focus_v1', ROOT/'tests/helpers/longhorn-v1-observability.py')
GATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GATE)

class SupportMetadataContracts(unittest.TestCase):
    def test_rejected_native_metadata_is_durable_before_cleanup_without_download(self):
        for changes in ({'filesize': 0}, {'filesize': GATE.SHARED.SUPPORT_COMPRESSED_BYTES+1}, {'ownerID': 'foreign-node'}):
            with self.subTest(changes=changes):
                status = {'state': 'ReadyForDownload', 'ownerID': 'longhorn-node-238', 'filesize': 1024}
                status.update(changes)
                obj = {'metadata': {'name': 'support-bundle-owned', 'uid': 'bundle-uid'},
                       'spec': {'description': 'kubeauto-observability-run', 'issueURL': ''}, 'status': status}
                created = []; saved = []
                lab = SimpleNamespace(NODES=('192.168.122.238',), DIRECTORY=Path('/fixture'), owner=lambda: {'run_id': 'run'},
                    wait=lambda stage, timeout, condition: condition(),
                    get=lambda *a, **k: {'items': [obj] if created else []},
                    write_private=lambda path, journal: saved.append(copy.deepcopy(journal)),
                    kubectl=Mock(side_effect=lambda *a, **k: created.append(a) or SimpleNamespace(stdout='')))
                probe = object.__new__(GATE.Probe); probe.lab = lab; probe.journal = {GATE.KEY: {}}
                probe.manager = lambda: ({'metadata': {'name': 'manager'}}, 'http://manager:9500')
                probe.bundle_dependents = Mock(return_value=[])
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, 'owner or private download size'):
                    probe.support_bundle()
                expected = {'source_uid': 'bundle-uid', 'name': 'support-bundle-owned',
                            'owner': status['ownerID'], 'compressed_bytes': status['filesize'], 'state': status['state']}
                self.assertEqual(expected, probe.journal[GATE.KEY]['support_bundle']['ready_metadata'])
                self.assertTrue(any(j[GATE.KEY].get('support_bundle', {}).get('ready_metadata') == expected for j in saved))
                rows = [json.loads(line.split(' ', 1)[1]) for line in output.getvalue().splitlines()
                        if line.startswith('LONGHORN_V1_SUPPORT_BUNDLE_METADATA ')]
                self.assertEqual([expected], rows)
                self.assertEqual(1, lab.kubectl.call_count, 'a rejected bundle must not reach its download command')

class SupportFocusContracts(unittest.TestCase):
    def test_observed_212_mib_native_bundle_reaches_bounded_stream_then_native_retirement(self):
        size=222430541;created=[]
        obj={'metadata':{'name':'support-bundle-owned','uid':'bundle-uid'},'spec':{'description':'kubeauto-observability-run','issueURL':''},
             'status':{'state':'ReadyForDownload','ownerID':'longhorn-node-238','filesize':size}}
        def wait(stage,timeout,condition):
            if stage=='support-bundle-native-download-retired':
                created.clear()
            return condition()
        lab=SimpleNamespace(NODES=('192.168.122.238',),DIRECTORY=Path('/fixture'),owner=lambda:{'run_id':'run'},wait=wait,
            get=lambda *a,**kw:({'items':[obj] if created else []} if len(a)<2 else None),write_private=Mock(),
            kubectl=Mock(side_effect=lambda *a,**kw:created.append(a) or SimpleNamespace(stdout='')))
        probe=object.__new__(GATE.Probe);probe.lab=lab;probe.journal={GATE.KEY:{}};probe.manager=lambda:({'metadata':{'name':'manager'}},'http://manager:9500');probe.bundle_dependents=Mock(return_value=[])
        result={'compressed_bytes':size,'expanded_bytes':size*3,'members':500,'sha256':'a'*64,'crc_verified':True,'extracted':False,'published':False}
        with patch.object(GATE.SHARED,'download_support',return_value=result,create=True) as download,contextlib.redirect_stdout(io.StringIO()):
            probe.support_bundle()
        download.assert_called_once_with(lab,'/v1/supportbundles/longhorn-node-238/support-bundle-owned/download',size)
        self.assertTrue(probe.journal[GATE.KEY]['support_bundle']['retired_after_download'])
        self.assertEqual(size,probe.journal[GATE.KEY]['support_bundle']['ready_metadata']['compressed_bytes'])

    def test_constructor_can_omit_monitoring_only_for_explicit_support_focus(self):
        monitor = SimpleNamespace(receipt=Mock(side_effect=ValueError('no monitoring receipt')), Prometheus=Mock())
        with patch.object(GATE, 'monitoring', return_value=monitor):
            probe = GATE.Probe(SimpleNamespace(), 1, None, None, observe=False)
            self.assertIsNone(probe.monitor); self.assertIsNone(probe.prom)
            monitor.receipt.assert_not_called(); monitor.Prometheus.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'no monitoring receipt'):
                GATE.Probe(SimpleNamespace(), 1, None, None)

    def test_actual_support_callback_checks_payload_and_restores_original_hooks_on_all_exits(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                trace = []; original_hook = Mock(); digest = hashlib.sha256(b'payload'*262144).hexdigest()
                lab = SimpleNamespace(DIRECTORY=Path('/fixture'), owner=lambda: {'run_id': 'run'},
                    verify_performance=original_hook, cleanup_business=lambda *a, **k: trace.append('original-clean'),
                    write_private=Mock(), kubectl=Mock(return_value=SimpleNamespace(stdout=digest)),
                    payload_program=Mock(return_value='actual-read-only-payload-verifier'), replica_topology=Mock(return_value=True))
                original_cleanup = lab.cleanup_business
                def native(probe):
                    self.assertEqual('vol', probe.journal[GATE.KEY]['volume_uid'])
                    self.assertTrue(probe.journal[GATE.KEY]['support_focus'])
                    trace.append('native-bundle')
                    if fails: raise ValueError('exact native metadata refusal')
                def business(scenario, **kwargs):
                    self.assertEqual(('performance', {'relay_pid': 7, 'storage_class': 'longhorn-delete'}), (scenario, kwargs))
                    try:
                        lab.verify_performance({'namespace': 'owned', 'volume_uid': 'vol', 'objects': {}}, 'payload', digest)
                        trace.append('product-remount-boundary')
                    finally:
                        lab.cleanup_business(relay_pid=7)
                lab.business = business
                output = io.StringIO()
                with patch.object(GATE.Probe, 'support_bundle', native), patch.object(GATE.Probe, 'restore', lambda probe: trace.append('native-restore')):
                    with contextlib.redirect_stdout(output):
                        if fails:
                            with self.assertRaisesRegex(ValueError, 'exact native metadata refusal'): GATE.support_business(lab, 7)
                        else: GATE.support_business(lab, 7)
                self.assertIs(lab.verify_performance, original_hook); self.assertIs(lab.cleanup_business, original_cleanup)
                self.assertEqual(['native-bundle']+([] if fails else ['product-remount-boundary'])+['native-restore','original-clean'], trace)
                if fails:
                    lab.kubectl.assert_not_called(); self.assertNotIn('LONGHORN_V1_SUPPORT_BUSINESS_PASS', output.getvalue())
                else:
                    lab.payload_program.assert_called_once_with('payload', read_only=True)
                    lab.replica_topology.assert_called_once()
                    self.assertIn('LONGHORN_V1_SUPPORT_SOURCE_VERIFIED ', output.getvalue())

    def test_focused_cleanup_requires_original_owner_but_no_monitoring_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'longhorn-install-business.json'
            journal = {'volume_uid': 'vol', GATE.KEY: {'run_id': 'run', 'volume_uid': 'vol', 'support_focus': True}}
            path.write_text(json.dumps(journal)); trace = []
            lab = SimpleNamespace(DIRECTORY=Path(directory), owner=lambda: {'run_id': 'run'}, check_business_graph=Mock(),
                backup_store=lambda: SimpleNamespace(cleanup=lambda: trace.append('store-clean')),
                cleanup_business=lambda **kw: (path.unlink(),trace.append('business-clean')))
            probe = SimpleNamespace(restore=lambda: trace.append('native-restore'))
            with patch.object(GATE, 'Probe', return_value=probe) as factory, contextlib.redirect_stdout(io.StringIO()):
                GATE.cleanup(lab, 7)
                self.assertFalse(factory.call_args.kwargs['observe'])
            self.assertEqual(['native-restore', 'business-clean', 'store-clean'], trace)
            path.write_text(json.dumps(dict(journal, volume_uid='foreign')))
            with self.assertRaisesRegex(ValueError, 'another business journal'):
                GATE.cleanup(lab, 7)
            self.assertTrue(path.exists())

    def test_actual_focused_log_verifier_rejects_missing_cleanup_bad_crc_and_false_remount(self):
        digest='a'*64; rows=[]
        def emit(marker,row): rows.append(marker+' '+json.dumps(row))
        emit('LONGHORN_V1_SUPPORT_SOURCE_VERIFIED',{'volume_uid':'vol','bytes':100,'sha256':digest})
        emit('LONGHORN_V1_SUPPORT_BUNDLE_METADATA',{'source_uid':'bundle','state':'ReadyForDownload','compressed_bytes':10})
        emit('LONGHORN_V1_SUPPORT_BUNDLE_BOUNDARY',{'source_uid':'bundle','compressed_bytes':10,'expanded_bytes':100,'members':1,'sha256':'b'*64,'crc_verified':True,'extracted':False,'published':False})
        rows += ['LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=100 sha256='+digest+' replica_nodes=longhorn-node-165,longhorn-node-212,longhorn-node-238',
                 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','LONGHORN_V1_SUPPORT_BUSINESS_PASS']
        log='\n'.join(rows)
        with contextlib.redirect_stdout(io.StringIO()):GATE.verify_support_log(log)
        for changed in (log.replace('LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',''),log.replace('"crc_verified": true','"crc_verified": false'),log.replace('bytes=100','bytes=99'),log+'\nLONGHORN_FIRST_FAILURE phase=support rc=1'):
            with self.assertRaises(ValueError):GATE.verify_support_log(changed)

    def test_product_runner_dispatches_to_the_exact_separate_support_scope(self):
        script='source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch --longhorn-observability-support'
        result=subprocess.run(['bash','-c',script,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh')],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(['observability-support','LONGHORN_OBSERVABILITY_SUPPORT_PASS'],result.stdout.splitlines())
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        routes = [line.strip()[:-1].split('|') for line in live.splitlines()
                  if line.strip().endswith(')') and 'observability-support' in line]
        self.assertEqual(1, len(routes))
        self.assertTrue({'observability', 'observability-business', 'observability-support',
                         'observability-cleanup'}.issubset(routes[0]))
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v1-observability.sh" "$MODE"', live)

    def test_shell_support_owns_empty_baseline_business_cleanup_and_separate_terminal(self):
        spec=importlib.util.spec_from_file_location('support_shell_fixture',ROOT/'tests/unit/test_longhorn_v1_observability.py')
        shell=importlib.util.module_from_spec(spec);spec.loader.exec_module(shell);fixture=shell.V1ObservabilityContracts()
        for fails in ('','support-business','cleanup','baseline'):
            with self.subTest(fails=fails):
                rc,out,err,trace,durable=fixture.run_shell(mode='observability-support',fail=fails)
                self.assertEqual(0 if not fails else 17,rc,out+err)
                self.assertNotIn('monitor-cleanup',trace);self.assertNotIn('monitor-verify',trace)
                self.assertNotIn('longhorn-v1-observability.py setup',trace)
                self.assertNotIn('LONGHORN_OBSERVABILITY_PASS',out)
                if not fails:
                    self.assertEqual({'exit':'0','finalized':'0'},durable)
                    self.assertEqual(2,trace.count('longhorn-v1-observability.py baseline'))
                    self.assertLess(trace.index('support-business'),trace.index('verify-support'))
                    self.assertLess(trace.index('verify-support'),trace.index('longhorn-v1-observability.py cleanup'))
                    self.assertIn('LONGHORN_OBSERVABILITY_SUPPORT_PASS',out)
                    self.assertIn('LONGHORN_OBSERVABILITY_SUPPORT_SCOPED_CLEAN_PASS',out)
                else:
                    self.assertNotIn('LONGHORN_OBSERVABILITY_SUPPORT_PASS',out)
                    self.assertEqual('17',durable['exit'])
                    if fails!='baseline':self.assertIn('longhorn-v1-observability.py cleanup',trace)
                    if fails=='support-business':self.assertEqual('0',durable['finalized'])


if __name__ == '__main__':
    unittest.main()

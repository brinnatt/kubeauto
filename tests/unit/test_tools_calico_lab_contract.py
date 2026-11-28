"""The tools CNI fixture must own a clean, exact pool and its runtime pins."""
import copy
import importlib.util
from pathlib import Path
import re
import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import contextlib

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('tools_calico_lab',ROOT/'tests/helpers/tools-calico-lab.py')
LAB=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(LAB)


class CalicoLabContracts(unittest.TestCase):
    def test_live_fault_wrapper_delegates_once_then_rejects_hep(self):
        text=(ROOT/'tests/helpers/calico-live-regression.sh').read_text()
        start=text.index('REAL_CALICOCTL=$(command -v calicoctl)')
        end=text.index('\nexpect_fail cli --calicoctl',start)
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);binary=base/'calicoctl'
            binary.write_text('#!/bin/sh\ncat >>"$TRACE"\n');binary.chmod(0o700)
            source=text[start:end].replace('/tmp/calico-delivery-fail-calicoctl.sh',str(base/'wrapper')).replace('/tmp/calico-delivery-fail-count',str(base/'count'))
            env=dict(os.environ,PATH=directory+':'+os.environ['PATH'],TRACE=str(base/'trace'))
            subprocess.run(['bash','-euc',source],env=env,check=True)
            for kind,rc in (('GlobalNetworkPolicy',0),('HostEndpoint',42)):
                result=subprocess.run([str(base/'wrapper'),'apply','-f','-'],input='kind: '+kind+'\n',text=True,capture_output=True,env=env)
                self.assertEqual(result.returncode,rc)
            self.assertEqual((base/'trace').read_text(),'kind: GlobalNetworkPolicy')
            self.assertEqual((base/'count').read_text(),'GNP\nHEP\n')

    def test_success_markers_follow_restored_paths_and_cleanup_queries_propagate_errors(self):
        text=(ROOT/'tests/helpers/calico-live-regression.sh').read_text()
        for marker,prerequisite in (
                ('CALICO_HOST_DATAPLANE_PASS','wait_data_path remote_port "$PORT_HOST" allow'),
                ('CALICO_POD_DATAPLANE_PASS','cli delete --traffic-layer pod'),
                ('CALICO_BOTH_DATAPLANE_PASS','wait_data_path pod_port "$DENY_PROBE" "$PORT_BOTH" allow')):
            self.assertLess(text.index(prerequisite),text.index('echo '+marker))
        self.assertIn('remaining=$(kubectl get namespace "$NS" --ignore-not-found -o name)',text)
        self.assertIn('calicoctl get hostendpoints -o json |',text)
        self.assertIn('calicoctl get globalnetworkpolicies -o json |',text)
        self.assertLess(text.index('CALICO_BUSINESS_SCOPED_CLEAN_PASS'),text.index('CALICO_LIVE_REGRESSION_PASS'))
        self.assertIn("== $'GNP\\nHEP'",text)
        self.assertIn('exit \\$?',text)

    def test_host_and_pod_denial_refuse_transport_errors_and_require_probe_status(self):
        text=(ROOT/'tests/helpers/calico-live-regression.sh').read_text()
        for name in ('remote_port','pod_port'):
            function=name+'() {\n'+text.split(name+'() {\n',1)[1].split('\n}',1)[0]+'\n}\n'
            stub='''ssh(){
  case "$MODE" in lost) return 255;; open) echo CALICO_PROBE_RC=0;; denied) echo CALICO_PROBE_RC=1;; malformed) echo CALICO_PROBE_RC=2;; esac
}
kubectl(){ ssh; }
'''
            for mode in ('lost','open','denied','malformed'):
                call='remote_port 39091 deny' if name=='remote_port' else 'pod_port denied 39092 deny'
                result=subprocess.run(['bash','-euc',stub+function+'\nHOST_IP=192.168.47.131\nDENY_HOST=192.168.47.132\nNS=fixture\nPOD_IP=172.29.0.1\n'+call],
                    env=dict(os.environ,MODE=mode),capture_output=True,text=True)
                self.assertEqual(result.returncode==0,mode=='denied',(name,mode,result.stdout,result.stderr))

    def test_inventory_uses_only_the_leased_pool_and_private_product_copy(self):
        text=LAB.inventory()
        seen=re.findall(r'^(192[.]168[.]47[.]\d+) ',text,re.M)
        self.assertEqual(seen,list(LAB.NODES[3:])*2+list(LAB.NODES[:3]))
        self.assertIn('base_dir="/tmp/tools-calico-lab/product"',text)
        self.assertIn('clusters/tools-calico-gate',text)
        self.assertIn('CLUSTER_NETWORK="calico"',text)
        self.assertEqual(len(LAB.NODES),6)

    def test_configuration_disables_unrelated_products_and_checks_resolved_version(self):
        source={'calico_ver':'v3.28.4','mysql_install':'yes','ceph_install':'yes',
                'redis_install':'yes','dns_install':'yes','K8S_VER':'1.34.13'}
        result=LAB.configure(source)
        self.assertEqual(result['K8S_VER'],'1.33.6')
        self.assertEqual(result['calico_ver'],'v3.28.4')
        self.assertTrue(all(v=='no' for k,v in result.items() if k.endswith('_install')))
        self.assertEqual(source['mysql_install'],'yes')
        with self.assertRaisesRegex(ValueError,'SOURCE_VERSION_REFUSED'):
            LAB.configure(dict(source,calico_ver='unresolved'))

    def test_foreign_or_failed_services_and_configuration_refuse_before_setup(self):
        states={ip:dict(units=['inactive','unknown'],kubernetes=False,kubeconfig=False) for ip in LAB.NODES}
        LAB.admit(states)
        bad=copy.deepcopy(states);bad.pop(LAB.NODES[0])
        with self.assertRaisesRegex(ValueError,'POOL_REFUSED'):LAB.admit(bad)
        for update in (dict(units=['active']),dict(units=['failed']),dict(kubernetes=True),dict(kubeconfig=True)):
            bad=copy.deepcopy(states);bad[LAB.NODES[0]].update(update)
            with self.assertRaisesRegex(ValueError,'FOREIGN_'):LAB.admit(bad)

    def test_lifecycle_binds_binaries_to_digest_and_wipes_only_owned_nodes(self):
        for image,source,target in LAB.BINS:self.assertRegex(image,r'@sha256:[0-9a-f]{64}$')
        text=(ROOT/'tests/helpers/tools-calico-lab.py').read_text()
        self.assertIn("('01','02','03','04','05','06')",text)
        self.assertNotIn("product('download'",text)
        self.assertNotIn("product('setup',NAME,'90')",text)
        self.assertIn("admit(states)",text)
        self.assertLess(text.index('admit(states)'),text.index('ROOT.mkdir(mode=0o700)'))
        self.assertIn("'--rocky-only',*NODES",text)
        self.assertIn("'--verify','--rocky-only',*NODES",text)
        self.assertIn('original.write_bytes(saved)',text)
        self.assertIn('shutil.rmtree(ROOT)',text)


class CalicoHandoffContracts(unittest.TestCase):
    def test_probe_authorization_restores_exact_bytes_and_mode_and_refuses_foreign_changes(self):
        import shlex
        import sys
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'authorized_keys'
            def execute(argv,**kwargs):
                command=shlex.split(argv[-1])
                self.assertEqual(command[:2],['python3','-c'])
                code=command[2].replace("'/root/.ssh/authorized_keys'",repr(str(path)))
                return subprocess.run([sys.executable,'-B','-c',code],check=True,**kwargs)
            with patch.object(LAB,'run',side_effect=execute):
                for old in (b'original key\n',b'original key'):
                    path.write_bytes(old);path.chmod(0o640)
                    saved=LAB.probe_authorization(b'fixture key\n')
                    self.assertIn(b'fixture key',path.read_bytes())
                    LAB.probe_authorization(b'fixture key\n',saved)
                    self.assertEqual(path.read_bytes(),old)
                    self.assertEqual(path.stat().st_mode & 0o777,0o640)
                saved=LAB.probe_authorization(b'fixture key\n')
                path.write_bytes(path.read_bytes()+b'foreign key\n')
                with self.assertRaises(subprocess.CalledProcessError):LAB.probe_authorization(b'fixture key\n',saved)
                self.assertIn(b'foreign key',path.read_bytes())
                path.unlink();saved=LAB.probe_authorization(b'fixture key\n')
                LAB.probe_authorization(b'fixture key\n',saved)
                self.assertFalse(path.exists())

    def test_binary_image_paths_match_the_product_downloader(self):
        self.assertEqual([(source,target) for image,source,target in LAB.BINS],
                         [('/k8s','kube-bin'),('/extra','extra-bin')])
        source=(ROOT/'service/cluster/downloader.py').read_text()
        self.assertIn('"/k8s", self.kube_bin_dir',source)

    def test_control_kubeconfig_is_restored_with_original_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config';path.write_bytes(b'fixture');path.chmod(0o644)
            LAB.restore_control(path,b'customer config',0o400)
            self.assertEqual(path.read_bytes(),b'customer config')
            self.assertEqual(path.stat().st_mode & 0o777,0o400)
            LAB.restore_control(path,None,None)
            self.assertFalse(path.exists())

    def test_business_failure_still_removes_only_owned_upload_directory(self):
        calls=[]
        def fake(argv,**kwargs):
            calls.append(argv)
            if any('env CALICO_TOOL=' in str(x) for x in argv):raise RuntimeError('first business failure')
        with patch.object(LAB,'run',side_effect=fake),patch.object(LAB,'probe_identity',return_value=contextlib.nullcontext({})):
            with self.assertRaisesRegex(RuntimeError,'first business failure'):LAB.business()
        self.assertEqual(calls[0][-1],'mkdir -m 700 /tmp/tools-calico-business')
        self.assertEqual(calls[-1][-1],'rm -rf /tmp/tools-calico-business')
        self.assertIn('root@192.168.47.131',calls[-1])

    def test_live_launcher_and_remote_lifecycle_have_no_ready_marker_shortcut(self):
        remote=(ROOT/'tests/helpers/tools-calico-lab.py').read_text()
        local=(ROOT/'tests/helpers/tools-calico-delivery.py').read_text()
        self.assertLess(remote.index("for step in ('01','02','03','04','05','06')"),remote.index('            business()'))
        self.assertLess(remote.index("run([kubectl,'--kubeconfig='"),remote.index('            business()'))
        self.assertIn("outcome!=['0','0']",local)
        self.assertIn("'CALICO_LIVE_REGRESSION_PASS'",local)
        self.assertIn("'TOOLS_CALICO_FIXTURE_SCOPED_CLEAN_PASS'",local)
        self.assertIn("if cleanup_errors:raise RuntimeError",remote)


class CalicoDetachContracts(unittest.TestCase):
    def test_background_launch_closes_ssh_output_before_fixture_finishes(self):
        source=(ROOT/'tests/helpers/tools-calico-delivery.py').read_text()
        self.assertIn("'cd '+REMOTE+' && { nohup",source)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            (target/'durable.sh').write_text('#!/bin/sh\n"$@"\n')
            process=subprocess.run(['bash','-c','cd '+directory+' && { nohup bash durable.sh sh -c "sleep 1; touch done" >console.log 2>&1 </dev/null & }'],capture_output=True,timeout=0.8)
            self.assertEqual(process.returncode,0)
            subprocess.run(['bash','-c','for i in 1 2 3 4 5; do test -e "$1/done" && exit 0; sleep 0.3; done; exit 1','sh',directory],check=True)


class CalicoClockContracts(unittest.TestCase):
    def test_failed_cleanup_requires_collected_exact_raw_and_archive(self):
        spec=importlib.util.spec_from_file_location('calico_delivery_cleanup',ROOT/'tests/helpers/tools-calico-delivery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import hashlib
        def digest(data):return hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory,patch.object(module,'ROOT',Path(directory)):
            out=Path(directory)/'logs/tools-calico-live-fixture';out.mkdir(parents=True)
            def calls():return [MagicMock(stdout='1\n1\n'),MagicMock(),MagicMock(stdout=digest(b'raw')+' file'),MagicMock(stdout=digest(b'archive')+' file')]
            with patch.object(module,'run',return_value=MagicMock(stdout='0\n0\n')) as run:
                with self.assertRaisesRegex(ValueError,'STATE_REFUSED'):module.clear_failed()
                self.assertEqual(run.call_count,1)
            with patch.object(module,'run',side_effect=calls()) as run:
                with self.assertRaisesRegex(ValueError,'NOT_COLLECTED'):module.clear_failed()
                self.assertEqual(run.call_count,4)
            (out/'console.log').write_bytes(b'raw');(out/'evidence.tar.gz').write_bytes(b'archive')
            with patch.object(module,'run',side_effect=calls()+[MagicMock()]) as run:
                module.clear_failed();self.assertIn('shutil.rmtree',run.call_args.args[0])

    def test_clock_repair_refuses_foreign_and_inactive_fixture_before_any_clock_write(self):
        spec=importlib.util.spec_from_file_location('calico_delivery',ROOT/'tests/helpers/tools-calico-delivery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import json
        for nodes in ([],['192.168.122.1']):
            with patch.object(module,'run',return_value=MagicMock(stdout=json.dumps(dict(nodes=nodes)))),patch.object(module.subprocess,'run') as execute:
                with self.assertRaisesRegex(ValueError,'CLOCK_OWNER_REFUSED'):module.align_clocks()
                execute.assert_not_called()
        owner=MagicMock(stdout=json.dumps(dict(nodes=['192.168.47.'+str(n) for n in range(131,137)])))
        with patch.object(module,'run',side_effect=[owner,RuntimeError('inactive fixture')]),patch.object(module.subprocess,'run') as execute:
            with self.assertRaisesRegex(RuntimeError,'inactive fixture'):module.align_clocks()
            execute.assert_not_called()

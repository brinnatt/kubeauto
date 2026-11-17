"""Compose EtcdPatch with the real serial playbook and offline process fixtures."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from upgrade.product import next_etcd
from upgrade.product.journal import Journal
from upgrade.product.kubernetes_preflight import Refused
from upgrade.tests import test_etcd_stage as fixture

HOSTS, ROOT = fixture.HOSTS, fixture.ROOT

CLIENT = '''import json, sys
from pathlib import Path
state=json.loads((Path(__file__).parent.parent/'state.json').read_text())
args=sys.argv[1:]
assert len(args)==9 and args[-1]=='--write-out=json'
assert args[0]=='--endpoints='+','.join(m['clientURLs'][0] for m in state['members'])
assert args[1:4]==['--cacert={ssl}/ca.pem','--cert={ssl}/etcd.pem','--key={ssl}/etcd-key.pem']
assert args[4:6]==['--dial-timeout=5s','--command-timeout=15s']
if args[6:8]==['member','list']:out={{'members':state['members']}}
elif args[6:8]==['endpoint','health']:
 out=[{{'endpoint':m['clientURLs'][0],'health':not state.get('unhealthy')}} for m in state['members']]
elif args[6:8]==['endpoint','status']:
 out=[{{'Endpoint':m['clientURLs'][0],'Status':{{'version':state['version'][h],
 'header':{{'cluster_id':101,'member_id':m['ID'],'revision':100}},'leader':1,'errors':[]}}}}
 for h,m in zip({hosts},state['members'])]
else:raise AssertionError(args)
print(json.dumps(out))
'''


class NextEtcdContracts(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EtcdStageContracts(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.root = f.root
        self.assets = self.root/'extra'; self.assets.mkdir()
        self.state = self.root/'private'; self.state.mkdir(mode=0o700)
        ssl = self.root/'source/ssl'
        client = self.assets/'etcdctl'
        client.write_text('#!'+sys.executable+'\n'+CLIENT.format(ssl=ssl, hosts=repr(HOSTS)))
        client.chmod(0o700)
        (self.assets/'etcd').write_text('official binary fixture')
        (self.assets/'etcd').chmod(0o700)
        pins = {name: hashlib.sha256((self.assets/name).read_bytes()).hexdigest() for name in ['etcd','etcdctl']}
        self.pins = self.root/'pins.json'
        self.pins.write_text(json.dumps({'etcd': {'amd64':pins, 'arm64':pins}}))
        for host in HOSTS:
            path = self.state/('etcd-unit-'+host+'.json')
            path.write_text(json.dumps({'host':host, 'sha256':f.state['unit_hash'][host]}))
            path.chmod(0o600)
        # The real action adapter checks TLS paths, binary hashes and serial
        # process transitions. Map only fixture paths, never assertion bodies.
        adapter = (ROOT/'upgrade/tests/fixtures/etcd_action.py').read_text()
        adapter = adapter.replace("'/fixture/etcdctl'",repr(str(client)))
        adapter = adapter.replace("/fixture/ssl/", str(ssl)+"/")
        f.root.joinpath('actions/etcd_record.py').write_text(adapter)
        f.variables.update(UPGRADE_ETCDCTL=str(client), UPGRADE_ETCDCTL_SHA256=pins['etcdctl'])
        f.root.joinpath('state.json').write_text(json.dumps(f.state))
        self.results = []
        def run_book(cluster, book, **kwargs):
            self.assertEqual((cluster, Path(book).name), ('source','etcd.yml'))
            f.variables.update(kwargs['extra_vars'])
            result = f.run_stage()
            self.results.append(result)
            return SimpleNamespace(rc=result.returncode)
        self.manager = SimpleNamespace(extra_bin_dir=self.assets, clusters_dir=self.root,
                                       _run_playbook=run_book)
        self.source = {'effective_inventory': {'groups':{'etcd':HOSTS},
            'hosts': {host:{'parameters':{'cluster_dir':str(self.root/'source'), 'bin_dir':'/fixture/bin'}}
                      for host in HOSTS}}}

    def stage(self):
        return next_etcd.EtcdPatch(self.manager,'source',
            {'UPGRADE_PRECONDITIONS_VERIFIED':True, 'UPGRADE_RUN_ID':'next-etcd-contract'},
            self.source,self.state)

    def run_stage(self, stage):
        with Journal(self.state/'journal', {'fixture':'next-etcd'},stage.steps) as journal:
            stage.run(journal)
            return list(journal.data['completed'])

    def test_real_handoff_completes_then_verifies_without_copy_or_restart(self):
        with patch.object(next_etcd, 'PINS_PATH', self.pins):
            stage=self.stage()
            self.assertEqual(self.run_stage(stage),stage.steps, self.results[-1].stdout)
            self.assertEqual(self.fixture.final['restarted'],HOSTS)
            self.fixture.state=self.fixture.final
            self.run_stage(self.stage())
            self.assertEqual(self.results[-1].returncode,0,self.results[-1].stdout+self.results[-1].stderr)
            self.assertFalse(any(t['module'] in ['copy','systemd_service'] for t in self.fixture.trace))
            self.fixture.state['disk_hash'][HOSTS[0]]='0'*64
            with self.assertRaisesRegex(Refused,'playbook refused'):self.run_stage(self.stage())
            self.assertFalse(any(t['module'] in ['copy','systemd_service'] for t in self.fixture.trace))

    def test_first_playbook_failure_retains_active_step_and_stops_next_member(self):
        with patch.object(next_etcd,'PINS_PATH',self.pins):
            stage=self.stage()
            self.fixture.state['fail_after_restart']=True
            with self.assertRaisesRegex(Refused,'playbook refused'):self.run_stage(stage)
            self.assertEqual(self.fixture.final['restarted'],[HOSTS[0]])
            data=json.loads((self.state/'journal/journal.json').read_text())
            self.assertEqual(data['active'],stage.steps[0])
            self.assertEqual(data['completed'],[])
            self.assertEqual(data['failures'][0]['category'],'kubernetes')

    def test_unhealthy_source_or_changed_official_bytes_refuse_before_playbook(self):
        with patch.object(next_etcd,'PINS_PATH',self.pins):
            self.fixture.state['unhealthy']=True
            (self.root/'state.json').write_text(json.dumps(self.fixture.state))
            with self.assertRaisesRegex(Refused,'healthy exact'):self.stage()
            self.assertEqual(self.results,[])
            (self.assets/'etcd').write_text('changed')
            with self.assertRaisesRegex(Refused,'official bytes'):self.stage()
            self.assertEqual(self.results,[])

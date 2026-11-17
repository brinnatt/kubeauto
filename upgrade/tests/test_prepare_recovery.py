"""Exercise real snapshot command argv and refusal using subprocess fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from upgrade.product import next_etcd, prepare
from upgrade.product.kubernetes_preflight import Refused
from upgrade.tests import test_next_etcd as fixture

SNAPSHOT_CLIENT = '''
if 'snapshot' in args:
 assert args[-3:-1]==['snapshot','save']
 assert '--endpoints='+state['members'][0]['clientURLs'][0] in args
 assert not any(a.startswith('--endpoints=') and ',' in a for a in args)
 Path(args[-1]).write_bytes(b'snapshot-fixture-with-embedded-hash')
 sys.exit(0)
'''
UTILITY = '''import json,sys
from pathlib import Path
args=sys.argv[1:];state=json.loads((Path(sys.argv[0]).parent.parent/'state.json').read_text())
with (Path(sys.argv[0]).parent.parent/'recovery-calls').open('a') as f:f.write(json.dumps(args)+'\\n')
assert Path(args[2]).name=='snapshot.db'
if args[:2]==['snapshot','status']:
 assert args[-1]=='--write-out=json'
 print(json.dumps({'revision':state.get('snapshot_revision',101),'totalKey':3,'hash':1}))
elif args[:2]==['snapshot','restore']:
 assert args[3]=='--data-dir' and len(args)==5
 directory=Path(args[4]);assert directory.name=='member' and directory.parent.name.startswith('restore-check-')
 if state.get('corrupt_snapshot'):sys.exit(1)
 directory.mkdir();(directory/'offline-data').write_bytes(b'fixture')
else:raise AssertionError(args)
'''


class RecoveryContracts(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.NextEtcdContracts(methodName='runTest');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        client=f.assets/'etcdctl'
        script=client.read_text().replace("assert len(args)==9 and args[-1]=='--write-out=json'",SNAPSHOT_CLIENT+"\nassert len(args)==9 and args[-1]=='--write-out=json'")
        client.write_text(script)
        utility=f.assets/'etcdutl';utility.write_text('#!'+sys.executable+'\n'+UTILITY);utility.chmod(0o700)
        pins=json.loads(f.pins.read_text())
        for arch in ['amd64','arm64']:
            pins['etcd'][arch]['etcdctl']=hashlib.sha256(client.read_bytes()).hexdigest()
            pins['etcd'][arch]['etcdutl']=hashlib.sha256(utility.read_bytes()).hexdigest()
        f.pins.write_text(json.dumps(pins))

    def recover(self):
        f=self.fixture
        with patch.object(prepare,'PINS_PATH',f.pins),patch.object(next_etcd,'PINS_PATH',f.pins):
            return prepare.recovery_point(f.manager,'source',{},f.source,f.state)

    def test_snapshot_is_single_endpoint_tls_and_restore_is_offline_hash_checked(self):
        result=self.recover();f=self.fixture
        self.assertTrue(result['offline_restore_verified'])
        self.assertEqual(result['etcd_cluster_id'],101)
        self.assertEqual(result['status']['revision'],101)
        self.assertEqual(Path(result['path']).stat().st_mode & 0o777,0o600)
        calls=[json.loads(line) for line in (f.root/'recovery-calls').read_text().splitlines()]
        self.assertEqual([row[:2] for row in calls],[['snapshot','status'],['snapshot','restore']])
        self.assertFalse(list(f.state.glob('restore-check-*')))

    def test_stale_revision_and_restore_hash_failure_refuse(self):
        f=self.fixture
        for changes in [{'snapshot_revision':99},{'corrupt_snapshot':True}]:
            with self.subTest(changes=changes):
                state=json.loads((f.root/'state.json').read_text())
                state.pop('snapshot_revision',None);state.pop('corrupt_snapshot',None);state.update(changes)
                (f.root/'state.json').write_text(json.dumps(state))
                with self.assertRaises(Refused):self.recover()
                self.assertFalse(list(f.state.glob('restore-check-*')))

    def test_changed_utility_cannot_publish_an_unverified_recovery_point(self):
        (self.fixture.assets/'etcdutl').write_text('changed bytes')
        with self.assertRaisesRegex(Refused,'official recovery binary'):self.recover()
        self.assertFalse((self.fixture.state/'snapshot.db').exists())

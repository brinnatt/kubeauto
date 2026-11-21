"""Verify actual product kubectl argv and fail-fast data/identity boundaries."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from upgrade.product.continuity import Continuity, IntegrityRefused

SCRIPT='''import json,sys
from pathlib import Path
root=Path(sys.argv[0]).parent
args=sys.argv[1:]
assert str(Path(args[1]).resolve())==str(root/'config')
assert args[:7]==['--kubeconfig',args[1],'--server','https://master-one:6443','--insecure-skip-tls-verify=false','--request-timeout=10s',*args[6:7]]
args=args[6:]
with (root/'calls').open('a') as f: f.write(json.dumps(args)+'\\n')
s=json.loads((root/'state').read_text())
if args==['get','--raw=/apis']: result={'groups':[]}
elif len(args)==2 and args[0]=='get' and args[1].startswith('--raw=/apis/rbac.authorization.k8s.io/'): result={'items':[]}
elif len(args)==2 and args[0]=='get' and args[1] in [
 '--raw=/api/v1/pods', '--raw=/api/v1/podtemplates', '--raw=/api/v1/replicationcontrollers',
 '--raw=/apis/apps/v1/deployments', '--raw=/apis/apps/v1/replicasets',
 '--raw=/apis/apps/v1/daemonsets', '--raw=/apis/apps/v1/statefulsets',
 '--raw=/apis/batch/v1/jobs', '--raw=/apis/batch/v1/cronjobs']: result={'items':[]}
elif args[:2]==['get','namespace']: result={'metadata':{'uid':s['namespace_uid']}}
elif args[2:4]==['get','service']: result={'metadata':{'uid':s['service_uid']}}
elif args[2:4]==['get','pvc']: result={'metadata':{'uid':s['pvc_uid']},'spec':{'volumeName':'volume'},'status':{'phase':'Bound'}}
elif args[:2]==['get','pv']: result={'metadata':{'uid':s['pv_uid']},'spec':{'claimRef':{'uid':'claim-uid'}}}
elif args[2:4]==['get','pods']: result={'items':[{'metadata':{'name':'client'},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}]}
elif args[2]=='exec':
 assert args[3:]==['client','--','wget','-T','10','-qO-','http://ledger:8080/ledger']
 if s.get('transient'):
  s.pop('transient');(root/'state').write_text(json.dumps(s));sys.exit(1)
 print(s['ledger'],end='');sys.exit(0)
else: raise AssertionError(args)
print(json.dumps(result))
'''


class ContinuityContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.binary=self.root/'kubectl';self.binary.write_text('#!'+sys.executable+'\n'+SCRIPT);self.binary.chmod(0o700)
        (self.root/'config').write_text('private TLS fixture')
        self.state={'namespace_uid':'namespace-uid','service_uid':'service-uid','pvc_uid':'claim-uid','pv_uid':'volume-uid','ledger':'persistent-ledger\n'}
        self.contract={'namespace':'workload','namespace_uid':'namespace-uid','service':'ledger','service_uid':'service-uid',
            'client_selector':'app=upgrade-client','pvc':'claim','pvc_uid':'claim-uid','pv':'volume','pv_uid':'volume-uid',
            'ledger_sha256':hashlib.sha256(self.state['ledger'].encode()).hexdigest()}
        self.probe=Continuity(self.binary,self.root/'config','https://master-one:6443',self.contract)

    def save(self): (self.root/'state').write_text(json.dumps(self.state))

    def test_service_dns_and_bound_original_pvc_serve_the_exact_ledger(self):
        self.save();self.probe()
        calls=[json.loads(l) for l in (self.root/'calls').read_text().splitlines()]
        self.assertTrue(any('exec' in args for args in calls))
        self.assertFalse(any(any(verb in args for verb in ['apply','patch','create','delete']) for args in calls))

    def test_data_or_identity_drift_refuses_immediately_without_retry(self):
        for field in ['namespace_uid','service_uid','pvc_uid','pv_uid','ledger']:
            with self.subTest(field=field),patch('upgrade.product.continuity.time',
                    SimpleNamespace(monotonic=time.monotonic, sleep=Mock())) as clock:
                original=self.state[field];self.state[field]='foreign';self.save()
                with self.assertRaises(IntegrityRefused):self.probe()
                clock.sleep.assert_not_called();self.state[field]=original

    def test_transient_service_failure_rechecks_all_identities_before_retry(self):
        self.state['transient']=True;self.save()
        with patch('upgrade.product.continuity.time',
                SimpleNamespace(monotonic=time.monotonic, sleep=Mock())) as clock:self.probe()
        clock.sleep.assert_called_once_with(3)
        calls=[json.loads(l) for l in (self.root/'calls').read_text().splitlines()]
        self.assertEqual(sum('exec' in args for args in calls),2)

"""Each staged CLI apply must stop on its executor's first failure."""
import os
import unittest
from unittest import mock

import yaml

from tests.unit import test_calico_parameters as fixture


class CalicoStageFailureContracts(unittest.TestCase):
    def test_each_resource_failure_stops_later_writes_and_retains_backup(self):
        case=fixture.CalicoParameters()
        case.setUp()
        self.addCleanup(case.doCleanups)
        text=case.executor.read_text()
        text=text.replace("if 'api-resources' in args:",
            "if body and 'kind: '+os.environ.get('CALICO_FAIL_KIND','UNUSED_KIND') in body:\n"
            "    print('injected-stage-failure',file=sys.stderr);raise SystemExit(42)\n"
            "if 'api-resources' in args:",1)
        case.executor.write_text(text)
        kinds=['GlobalNetworkPolicy','HostEndpoint','NetworkPolicy']
        for index,kind in enumerate(kinds):
            with self.subTest(kind=kind):
                before=len(case.calls());backup=case.base/('backups-'+kind)
                with mock.patch.dict(os.environ,CALICO_FAIL_KIND=kind):
                    result=case.cli('apply',*case.flags(),'--confirm','--backup','--backup-dir',str(backup),
                                    '--apply-staged','--post-verify',expected=1)
                self.assertIn('injected-stage-failure',result.stdout+result.stderr)
                writes=[call for call in case.calls()[before:] if 'apply' in call['argv']]
                self.assertEqual([next(yaml.safe_load_all(call['body']))['kind'] for call in writes],kinds[:index+1])
                self.assertTrue(list(backup.iterdir()))

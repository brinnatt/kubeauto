"""Parameter acceptance must follow actual command grammar and assertions."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
import subprocess
import os
import hashlib
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('parameter_audit', ROOT/'tests/helpers/tools-parameter-audit.py')
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class ParameterAuditContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.actual = AUDIT.discover()
        cls.inventory = json.loads(AUDIT.INVENTORY.read_text())

    def test_inventory_matches_actual_parser_including_subcommands_and_aliases(self):
        self.assertEqual(AUDIT.validate(self.inventory, self.actual), [])
        changed = copy.deepcopy(self.inventory)
        changed['tools']['CalicoPolicyCli']['parameters'].pop()
        self.assertTrue(AUDIT.validate(changed, self.actual))
        changed = copy.deepcopy(self.inventory)
        changed['tools']['NetCheckCli']['parameters'][2]['grammar']['default'] = -1
        self.assertTrue(AUDIT.validate(changed, self.actual))

    def test_help_or_parsing_cannot_sign_a_functional_parameter(self):
        changed = copy.deepcopy(self.inventory)
        row = changed['tools']['NetCheckCli']['parameters'][2]
        row.update(status='pass', checks=[dict(test='help-test', assertion='option visible', kind='offline-help')],
                   evidence=['tests/evidence/help.json'])
        self.assertTrue(any('help cannot accept' in e for e in AUDIT.validate(changed, self.actual)))
        row['checks'] = []
        self.assertTrue(any('assertion missing' in e for e in AUDIT.validate(changed, self.actual)))

    def test_same_named_parameter_in_different_commands_has_distinct_identity(self):
        rows = self.actual['MyLogiBackupCli']['parameters']
        keys = [AUDIT.key(row) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))
        for name in ('preflight/compression', 'backup/compression', 'timer/install/compression'):
            self.assertIn(name, keys)

    def test_evidence_requires_exact_plan_executed_tests_durable_exit_and_cleanup(self):
        row=copy.deepcopy(self.inventory['tools']['NetCheckCli']['parameters'][2])
        test=row['checks'][0]['test']
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/'raw';run.mkdir()
            def write_proof(plan=None, outcome=None, omit=None):
                payload={'plan.json':json.dumps(plan if plan is not None else [dict(grammar=row['grammar'],checks=row['checks'])]),
                    'contracts.log':'OK\n','state.exit':'0\n',
                    'cleanup.log':'TOOLS_CLEAN_VERIFY_PASS scope=parameter-contracts\n',
                    'result.json':json.dumps(outcome or dict(executed=[c['test'] for c in row['checks']],skipped=0,errors=0,failures=0))}
                paths={}
                for name,body in payload.items():
                    p=run/name;p.write_text(body)
                    if name!=omit: paths[str(p.relative_to(root))]=AUDIT.sha(p)
                proof=dict(tool='NetCheckCli',rc=0,inputs={},files=paths,tests=[c['test'] for c in row['checks']])
                (root/'proof.json').write_text(json.dumps(proof))
            with mock.patch.object(AUDIT,'ROOT',root):
                write_proof();AUDIT.verify_evidence('proof.json','NetCheckCli',row)
                for options,marker in ((dict(plan=[]),'PLAN_CHANGED'),
                        (dict(outcome=dict(executed=[test],skipped=1,errors=0,failures=0)),'EXECUTION_INCOMPLETE'),
                        (dict(omit='state.exit'),'DURABLE_MISSING'),(dict(omit='cleanup.log'),'DURABLE_MISSING')):
                    write_proof(**options)
                    with self.assertRaisesRegex(ValueError,marker):
                        AUDIT.verify_evidence('proof.json','NetCheckCli',row)
                write_proof();(run/'contracts.log').write_text('tampered')
                with self.assertRaisesRegex(ValueError,'EVIDENCE_STALE'):
                    AUDIT.verify_evidence('proof.json','NetCheckCli',row)

    def test_timeout_persists_nonzero_exit_and_cannot_publish_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'tests/unit').mkdir(parents=True)
            (root/'tests/unit/fake.py').write_text('')
            (root/'tool.py').write_text('')
            inventory=root/'inventory.json'
            inventory.write_text(json.dumps(dict(tools=dict(Fake=dict(path='tool.py',parameters=[dict(
                grammar={},checks=[dict(test='tests.unit.fake.Example.test_x')])])))))
            with mock.patch.object(AUDIT,'ROOT',root), mock.patch.object(AUDIT,'INVENTORY',inventory), \
                    mock.patch.object(AUDIT.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',600)):
                with self.assertRaisesRegex(ValueError,'CONTRACTS_REFUSED'): AUDIT.run_contracts('Fake')
            run=next((root/'logs').iterdir())
            self.assertEqual((run/'state.exit').read_text(),'124\n')
            self.assertIn('TIMEOUT', (run/'contracts.log').read_text())
            self.assertFalse((run/'cleanup.log').exists())
            self.assertFalse((root/'tests/evidence/tools-parameters-Fake.json').exists())

    def test_selected_build_uses_exact_changed_tools_and_rejects_other_sets(self):
        selected='NetCheckCli,CalicoPolicyCli,OvpnUserCli,KubePublishCli,KafkaCli,MigrationCli,StarCli'
        sources=((ROOT/'tests/run_tools_regression.sh','BUILD_SUDO='),
                 (ROOT/'tests/helpers/build-tools-rocky8.sh','[[ "$SOURCE"'))
        for path,boundary in sources:
            prefix=path.read_text().split(boundary,1)[0]
            for value,rc in ((selected,0),(selected+',MyBackupCli',2),
                             (selected+',NetCheckCli',2),('StarCli',2)):
                result=subprocess.run(['bash','-c',prefix+'\nprintf "%s\\n" "${TOOLS[@]}"',
                    str(path),'--build-only'],env=dict(os.environ,TOOLS_SELECTED=value),
                    capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,rc,result.stderr)
                if rc==0:self.assertEqual(result.stdout.splitlines(),selected.split(','))
            result=subprocess.run(['bash','-c',prefix+'\nprintf "%s\\n" "${TOOLS[@]}"',str(path),'--build-only'],
                env=dict(os.environ,TOOLS_SELECTED='CalicoPolicyCli'),capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.splitlines(),['CalicoPolicyCli'])
            result=subprocess.run(['bash','-c',prefix+'\nprintf "%s\\n" "${TOOLS[@]}"',str(path),'--build-only'],
                env=dict(os.environ,TOOLS_SELECTED='NetCheckCli'),capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.splitlines(),['NetCheckCli'])
            result=subprocess.run(['bash','-c',prefix+'\nprintf "%s\\n" "${TOOLS[@]}"',str(path),'--build-only'],
                env=dict(os.environ,TOOLS_SELECTED='OvpnUserCli'),capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.splitlines(),['OvpnUserCli'])
        runner=sources[0][0].read_text()
        self.assertIn("('NetCheckCli', 'netcheck-build.json')",runner)
        code='for tool, proof_name'+runner.split('for tool, proof_name',1)[1].split("if 'MigrationCli' in selected:",1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('tools/NetCheckCli.py','tools-onefile.spec','tests/helpers/build-tools-rocky8.sh',
                         'dist/NetCheckCli-rocky8'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(name)
            exec(compile(code,'real-build-provenance','exec'),
                dict(root=root,selected={'NetCheckCli'},hashlib=hashlib,json=json))
            proof=json.loads((root/'dist/netcheck-build.json').read_text())
            self.assertIn('tools/NetCheckCli.py',proof['inputs'])
            self.assertEqual(set(proof['artifacts']),{'NetCheckCli'})


if __name__ == '__main__':
    unittest.main()

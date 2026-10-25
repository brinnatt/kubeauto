import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class ModuleContracts(unittest.TestCase):
    def run_module(self,directory,fault='',stage='v2-recurring-snapshot-qualification'):
        root=Path(directory)
        for folder in ('.venv/bin','tests/helpers','kube-bin'):(root/folder).mkdir(parents=True,exist_ok=True)
        driver=root/'.venv/bin/python'
        driver.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[2];mode=sys.argv[2]
p=root/'boundary-calls';calls=p.read_text().splitlines() if p.exists() else []
calls.append(mode);p.write_text('\\n'.join(calls)+'\\n')
marker='LONGHORN_V2_DR_SOURCE_BOUNDARY' if mode=='source-boundary' else 'LONGHORN_V2_DR_TARGET_STORE_BASELINE'
fault=os.environ.get('MODULE_FAULT','')
if fault=='missing-boundary':sys.exit(0)
print(marker+' sha256='+('b'*64 if calls.count(mode)>1 and fault==mode else 'a'*64))
if fault=='duplicate-boundary':print(marker+' sha256='+'a'*64)
''');driver.chmod(0o755)
        child='''#!/usr/bin/env bash
set -eu
branch="$1"
printf '%s\\n' "$branch" >> "$MODULE_ROOT/stages"
marker="LONGHORN_${branch^^}";marker="${marker//-/_}"
if [[ "$branch" == "$MODULE_FAULT_STAGE" ]]; then
 case "$MODULE_FAULT" in
   rc) exit 17 ;;
   missing-clean) echo "${marker}_PASS";exit 0 ;;
   missing-business) echo "${marker}_SCOPED_CLEAN_PASS";exit 0 ;;
   false-success) echo LONGHORN_FIRST_FAILURE; ;;
 esac
fi
echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
'''
        for name in ('broken-restore','volumes','backups','recurring','detached-backup','systembackup','systemrestore','dr-failover'):
            (root/'tests/helpers'/('longhorn-v2-'+name+'.sh')).write_text(child)
        script=(ROOT/'tests/helpers/longhorn-v2-data-protection.sh').read_text().replace('BASE=/usr/local/kubeauto','BASE='+str(root))
        path=root/'run.sh';path.write_text(script)
        result=subprocess.run(['bash',str(path),'v2-data-protection'],text=True,capture_output=True,timeout=10,
            env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',MODULE_ROOT=str(root),MODULE_FAULT=fault,MODULE_FAULT_STAGE=stage))
        stages=(root/'stages').read_text().splitlines() if (root/'stages').exists() else []
        return result,stages

    def test_full_module_runs_each_owning_product_workflow_once(self):
        with tempfile.TemporaryDirectory() as directory:
            result,stages=self.run_module(directory)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertEqual(11,len(stages));self.assertEqual(11,len(set(stages)))
            self.assertEqual('v2-broken-restore-qualification',stages[0])
            self.assertEqual('v2-integrity-qualification',stages[1]);self.assertEqual('v2-dr-failback-qualification',stages[-1])
            for stage in ('v2-backup-refusal-qualification','v2-recurring-selector-qualification','v2-recurring-rotation-qualification',
                    'v2-detached-backup-qualification','v2-systembackup-qualification','v2-systemrestore-qualification'):
                self.assertIn(stage,stages)
            self.assertIn('LONGHORN_V2_DATA_PROTECTION_SCOPED_CLEAN_PASS',result.stdout)
            self.assertIn('LONGHORN_V2_DATA_PROTECTION_PASS',result.stdout)
            self.assertEqual(11,result.stdout.count('LONGHORN_V2_DATA_PROTECTION_STAGE_PASS '))

    def test_broken_chain_refusal_must_complete_before_other_module_scenarios(self):
        for fault in ('rc','missing-clean','missing-business','false-success'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,stages=self.run_module(directory,fault,'v2-broken-restore-qualification')
                self.assertNotEqual(0,result.returncode)
                self.assertEqual(['v2-broken-restore-qualification'],stages)
                self.assertNotIn('LONGHORN_V2_DATA_PROTECTION_PASS',result.stdout)
                self.assertIn('phase=v2-broken-restore-qualification',result.stderr)

    def test_child_failure_missing_marker_or_false_success_stops_without_next_stage(self):
        for fault in ('rc','missing-clean','missing-business','false-success'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,stages=self.run_module(directory,fault)
                self.assertNotEqual(0,result.returncode)
                self.assertEqual('v2-recurring-snapshot-qualification',stages[-1]);self.assertEqual(5,len(stages))
                self.assertNotIn('LONGHORN_V2_DATA_PROTECTION_PASS',result.stdout)
                self.assertIn('phase=v2-recurring-snapshot-qualification',result.stderr)
                if fault=='rc':self.assertEqual(17,result.returncode)

    def test_each_baseline_drift_and_missing_or_ambiguous_marker_blocks_success(self):
        for fault in ('source-boundary','target-baseline','missing-boundary','duplicate-boundary'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as directory:
                result,stages=self.run_module(directory,fault)
                self.assertNotEqual(0,result.returncode)
                self.assertNotIn('LONGHORN_V2_DATA_PROTECTION_PASS',result.stdout)
                self.assertEqual(0 if fault in ('missing-boundary','duplicate-boundary') else 11,len(stages))

    def test_dispatch_lease_static_and_matrix_owner_are_fixed(self):
        runner=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertGreaterEqual(runner.count('v2-data-protection'),4)
        self.assertIn('longhorn-v2-data-protection.sh',(ROOT/'tests/helpers/longhorn-live.sh').read_text())
        self.assertIn('tests.unit.test_longhorn_v2_data_protection',(ROOT/'tests/helpers/longhorn-static.sh').read_text())
        text=(ROOT/'tests/helpers/longhorn-v2-data-protection.sh').read_text()
        self.assertIn('LONGHORN_PARENT_LEASE_HELD',text)
        self.assertNotIn('lab-wipe-nodes',text);self.assertNotIn('business-clean',text)
        self.assertNotIn('kubectl ',text);self.assertNotIn('helm ',text)


if __name__=='__main__':unittest.main()

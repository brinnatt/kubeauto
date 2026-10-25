"""Real failed-parent receipts cannot become a fabricated successful upgrade."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.unit.test_longhorn_v2_upgrade_baseline import MODULE, UpgradeBaselineContracts


class CheckpointEvidence(unittest.TestCase):
    def fixture(self, directory):
        base,evidence,after,ref=UpgradeBaselineContracts().fixture(directory)
        result_path=base/evidence['transition']['upgrade_result']['path'];result=json.loads(result_path.read_text())
        before=('plain-before','encrypted-before');work=result_path.parent
        prior={'id':'original-upgrade','stage':'upgrade-starting','source_identity':result['source_identity'],
            'target_identity':result['target_identity'],'old_runtime':{'version':'1.12.0'},
            'new_runtime':{'version':'1.12.1'},'retained_before':{'original':'bindings'},
            'successful_configuration':{'longhorn_version':'1.12.1'},'system':{'name':'original-system'},
            'legacy':{'created_version':'1.12.0'},
            'points':{key:{'sha256':'1'*64} for key in before},
            'restores':{key:result['restores'][key] for key in before}}
        original=work/'failed-journal.json';original.write_text(json.dumps(prior))
        for key in ('id','old_runtime','new_runtime','retained_before','system','points'):result[key]=copy.deepcopy(prior[key])
        result['resume_from_journal_sha256']=ref(original)['sha256'];result_path.write_text(json.dumps(result))
        evidence['transition']['upgrade_result']=ref(result_path)
        log=base/evidence['receipt_refs']['log']['path'];lines=log.read_text().splitlines()
        lines=[line for line in lines if not (line.startswith('LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED ') and json.loads(line.split(' ',1)[1])['point'] in before)]
        lines=[('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION '+json.dumps(evidence['transition'])) if line.startswith('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION ') else line for line in lines]
        lines.append('LONGHORN_V2_UPGRADE_RESUMED_FROM_CHECKPOINT sha256='+ref(original)['sha256'])
        log.write_text('\n'.join(lines)+'\n');evidence['receipt_refs']['log']=ref(log)
        parent=work/'failed-parent.log';parent.write_text('\n'.join(
            ['LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED '+json.dumps({'point':key,**prior['restores'][key]}) for key in before]+
            ['LONGHORN_FIRST_FAILURE phase=manager-engine-upgrade-and-isolated-recovery rc=1 diagnostics=/original/work','LONGHORN_GATE_EXIT rc=1'])+'\n')
        diagnostic=work/'original-diagnostic.txt';diagnostic.write_text('  File "original", in legacy_refusals\nValueError: legacy native admission returned the wrong result: cannot expand legacy linked-clone volumes\n')
        inputs=work/'parent-inputs.json';inputs.write_text(json.dumps(evidence['frozen_inputs']))
        checkpoint={'log':ref(parent),'journal':ref(original),'diagnostic':ref(diagnostic),'inputs':ref(inputs)}
        for key in ('exit','finalized'):
            p=work/('failed.'+key);p.write_text('1\n');checkpoint[key]=ref(p)
        evidence['checkpoint_execution']=checkpoint
        return base,evidence,after,ref,result

    def test_two_real_old_restores_and_two_new_restores_accept_with_failed_parent_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            base,evidence,after,ref,result=self.fixture(directory)
            self.assertEqual(after,UpgradeBaselineContracts().accept(base,evidence,after))
            self.assertEqual('1',(base/evidence['checkpoint_execution']['exit']['path']).read_text().strip())

    def test_changed_receipts_checkpoint_identity_data_input_or_handoff_cannot_sign_upgrade(self):
        for defect in ('parent-rc','diagnostic','missing-before','restore-hash','journal-stage','journal-runtime','journal-id','changed-input','handoff','duplicate-before'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base,evidence,after,ref,result=self.fixture(directory);checkpoint=evidence['checkpoint_execution']
                if defect in ('parent-rc','diagnostic','missing-before','duplicate-before'):
                    key={'parent-rc':'exit','diagnostic':'diagnostic','missing-before':'log','duplicate-before':'log'}[defect];p=base/checkpoint[key]['path']
                    if defect=='parent-rc':p.write_text('0\n')
                    elif defect=='diagnostic':p.write_text('unrelated transport failure')
                    elif defect=='missing-before':p.write_text('\n'.join(p.read_text().splitlines()[1:])+'\n')
                    else:p.write_text(p.read_text()+p.read_text().splitlines()[0]+'\n')
                    checkpoint[key]=ref(p)
                elif defect in ('restore-hash','journal-stage','journal-runtime','journal-id'):
                    p=base/checkpoint['journal']['path'];row=json.loads(p.read_text())
                    if defect=='restore-hash':row['restores']['plain-before']['confirmed_sha256']='9'*64
                    elif defect=='journal-stage':row['stage']='source-1.12.0'
                    elif defect=='journal-runtime':row['new_runtime']['version']='1.12.0'
                    else:row['id']='foreign'
                    p.write_text(json.dumps(row));checkpoint['journal']=ref(p)
                elif defect=='changed-input':
                    p=base/checkpoint['inputs']['path'];pins=json.loads(p.read_text());pins[MODULE.INPUTS[0]]='0'*64;p.write_text(json.dumps(pins));checkpoint['inputs']=ref(p)
                elif defect=='handoff':
                    p=base/evidence['receipt_refs']['log']['path'];p.write_text('\n'.join(line for line in p.read_text().splitlines() if not line.startswith('LONGHORN_V2_UPGRADE_RESUMED_FROM_CHECKPOINT '))+'\n');evidence['receipt_refs']['log']=ref(p)
                with self.assertRaises((ValueError,FileNotFoundError)):
                    UpgradeBaselineContracts().accept(base,evidence,after)



class RetainedCRDs(unittest.TestCase):
    def test_transition_preserves_original_source_and_admits_only_two_verified_new_crds(self):
        for defect in ('','foreign-crd','old-uid','credential','unverified-uid'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                base,evidence,after,ref=UpgradeBaselineContracts().fixture(directory)
                transition=evidence['transition'];old={'original.longhorn.io':'original-uid'}
                new=dict(old,**{'shards.longhorn.io':'shard-uid','shardgroups.longhorn.io':'group-uid'})
                after['retained']['crd_uids']=dict(new)
                result_path=base/transition['upgrade_result']['path'];result=json.loads(result_path.read_text())
                result['source_identity']=dict(after['retained'],crd_uids=dict(old))
                result['new_runtime']={'version':'1.12.1','crds':{name:{'uid':uid} for name,uid in new.items()}}
                if defect=='foreign-crd':after['retained']['crd_uids']['foreign.longhorn.io']='foreign-uid'
                elif defect=='old-uid':after['retained']['crd_uids']['original.longhorn.io']='changed'
                elif defect=='credential':after['retained']['credentials']={'uid':'replaced'}
                elif defect=='unverified-uid':result['new_runtime']['crds']['shards.longhorn.io']['uid']='foreign'
                result_path.write_text(json.dumps(result));transition['upgrade_result']=ref(result_path)
                prep_path=base/transition['preparation']['path'];prep=json.loads(prep_path.read_text());prep['baseline_after']=after;prep_path.write_text(json.dumps(prep));transition['preparation']=ref(prep_path)
                with patch.object(MODULE,'BASE',base):
                    if defect:
                        with self.assertRaises(ValueError):MODULE.validate_transition(transition)
                    else:self.assertEqual(after,MODULE.validate_transition(transition)[0])


if __name__=='__main__':unittest.main()

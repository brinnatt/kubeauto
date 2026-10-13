"""A completed overall log never substitutes for current, clean module evidence."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import json

ROOT=Path(__file__).resolve().parents[2]


def load(name, filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tests/helpers'/filename)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


gate=load('redis_full_evidence_contract','redis-full-evidence.py')
evidence=load('redis_stage_evidence_contract','redis-evidence.py')


class FullEvidenceTests(unittest.TestCase):
    def fixture(self, root):
        for stage in gate.STAGES:
            path=root/'stages'/stage;path.mkdir(parents=True)
            module='idempotence' if stage=='final-idempotence' else stage
            lines=['REDIS_GATE_EXIT rc=0']
            if module in evidence.CASES | evidence.PARTIAL:
                marker=(evidence.CASES | evidence.PARTIAL)[module][1]
                if module=='stability':
                    marker=('REDIS_STABILITY_PASS seconds=1800 samples=53 business-errors=0 '
                            'restarts=pod+operator+node data-and-pvc=retained')
                lines.extend([marker,'REDIS_FOCUSED_SCOPED_CLEAN_PASS module='+module,
                              'REDIS_FOCUSED_MODULE_PASS module='+module])
                lines.extend(evidence.EXTRA_MARKERS.get(module,()))
            elif stage in ('clean-before','clean-after'):
                lines.extend(['REDIS_LAB_CLEANUP_PASS','REDIS_CLEAN_VERIFY_PASS',
                              'LAB_CLEAN_VERIFY_PASS','REDIS_LAB_CLEAN_VERIFY_PASS'])
            elif stage.endswith('-verify'):
                lines.append('REDIS_LAB_CLEAN_VERIFY_PASS')
            elif stage=='install':
                lines.append('REDIS_INSTALL_MODULE_PASS')
            else:
                lines.append('REDIS_FOCUSED_MODULE_PASS module=upgrade-prepare')
            (path/'first-run.log').write_text('\n'.join(lines)+'\n')
            (path/'durable.exit').write_text('0\n')
            (path/'durable.finalized').write_text('0\n')
            (path/'launch-source.json').write_text(json.dumps(evidence.capture_dependencies(ROOT,module)))

    def test_all_stage_business_and_cleanup_evidence_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            self.assertEqual(set(gate.verify_stages(root,evidence)),set(gate.STAGES))
            for stage,filename,replacement in [
                ('cluster-data','durable.exit','1\n'),
                ('cluster-data','durable.finalized','missing\n'),
                ('maintenance-drain','first-run.log','REDIS_GATE_EXIT rc=0\n'),
                ('stability','first-run.log','REDIS_STABILITY_PASS seconds=1 samples=1\n'),
                ('clean-after','first-run.log','REDIS_GATE_EXIT rc=0\nREDIS_LAB_CLEANUP_PASS\n'),
                ('sentinel-data','launch-source.json','{}')]:
                path=root/'stages'/stage/filename;original=path.read_text();path.write_text(replacement)
                with self.subTest(stage=stage,filename=filename),self.assertRaises(ValueError):
                    gate.verify_stages(root,evidence)
                path.write_text(original)
            path=root/'stages/platform/first-run.log';path.write_text(path.read_text()+'REDIS_FIRST_FAILURE\n')
            with self.assertRaises(ValueError):gate.verify_stages(root,evidence)


if __name__=='__main__':unittest.main()

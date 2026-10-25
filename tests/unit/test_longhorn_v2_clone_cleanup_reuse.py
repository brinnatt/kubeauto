"""Clone reuse admits only the exact unreachable retired-fault cleanup branch."""
import ast,hashlib,unittest
from pathlib import Path
from tests.unit.test_longhorn_v2_clone import gate as candidate
root=Path(__file__).resolve().parents[2]

class InertFaultCleanupContracts(unittest.TestCase):
    def body(self,path):
        return next(n for n in ast.parse(path.read_text()).body if getattr(n,'name',None)=='cleanup_business')
    def test_real_original_clone_proof_hashes_remain_valid_without_rewriting_evidence(self):
        before=self.body(root/'tests/fixtures/longhorn-lab-reviewed-ed237430.py.txt')
        after=self.body(root/'tests/helpers/longhorn-lab.py')
        key='tests/helpers/longhorn-lab.py:cleanup_business'
        self.assertNotEqual(ast.dump(before,include_attributes=False),ast.dump(after,include_attributes=False))
        self.assertEqual(hashlib.sha256(ast.dump(before,include_attributes=False).encode()).hexdigest(),candidate.clone_owning_ast_sha256(after,key))
    def test_only_exact_unreachable_fault_guard_is_excluded_and_every_clone_path_drift_refuses(self):
        before=self.body(root/'tests/fixtures/longhorn-lab-reviewed-ed237430.py.txt')
        expected=hashlib.sha256(ast.dump(before,include_attributes=False).encode()).hexdigest()
        source=ast.unparse(self.body(root/'tests/helpers/longhorn-lab.py'))
        key='tests/helpers/longhorn-lab.py:cleanup_business'
        for change in ('key','size','uid','position','other-function','ordinary-statement'):
            text=source
            if change=='key':text=text.replace("journal.get('v2_all_replicas_fault')","journal.get('claims')")
            if change=='size':text=text.replace('2 * 1024 ** 3','4 * 1024 ** 3')
            if change=='uid':text=text.replace("cleanup_retired_v2_endpoint(journal['volume'], journal['volume_uid']", "cleanup_retired_v2_endpoint(journal['volume'], journal['namespace_uid']")
            if change=='position':text=text.replace('    record.unlink()', "    print('new statement')\n    record.unlink()")
            if change=='ordinary-statement':text=text.replace('    record.unlink()', '    record.unlink(missing_ok=True)')
            node=ast.parse(text).body[0]
            actual=candidate.clone_owning_ast_sha256(node,'foreign:cleanup_business' if change=='other-function' else key)
            with self.subTest(change=change):self.assertNotEqual(expected,actual)


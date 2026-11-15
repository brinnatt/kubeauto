"""A kubecli release must not silently build the independent tools product."""
import importlib.util
import ast
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]


class ProductReleaseScopeContracts(unittest.TestCase):
    def test_product_bundle_includes_the_actual_playbook_closure_without_upgrade_evidence_or_tools(self):
        tree = ast.parse((ROOT/'kubecli-onefile.spec').read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign) and
                          any(isinstance(t, ast.Name) and t.id == 'added_files' for t in node.targets))
        data = ast.literal_eval(assignment.value)
        entries = [entry for entry in data if entry[0].startswith('upgrade')]
        self.assertEqual(entries, [('upgrade/product/*.yml', 'upgrade/product')])
        bundled = {path.resolve() for path in ROOT.glob(entries[0][0])}
        self.assertIn((ROOT/'upgrade/product/node.yml').resolve(), bundled)
        for path in bundled:
            with self.subTest(path=path.name):
                content = yaml.safe_load(path.read_text())
                def includes(value):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            if key in ['ansible.builtin.import_tasks', 'ansible.builtin.include_tasks']:
                                self.assertIsInstance(child, str)
                                self.assertNotIn('{{', child, 'dynamic include needs explicit bundle coverage')
                                self.assertIn((path.parent/child).resolve(), bundled)
                            else:
                                includes(child)
                    elif isinstance(value, list):
                        for child in value:
                            includes(child)
                includes(content)
        self.assertFalse(any('tools' in src or 'evidence' in src or 'snapshots' in src for src, _ in data))

    def test_both_architecture_workflow_commands_use_the_real_kubecli_only_plan(self):
        workflow = yaml.safe_load((ROOT/'.github/workflows/release.yml').read_text())
        spec = importlib.util.spec_from_file_location('upgrade_build_scope', ROOT/'build.py')
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        for architecture in ['amd64', 'arm64']:
            with self.subTest(architecture=architecture):
                steps = workflow['jobs']['build-'+architecture]['steps']
                script = next(s['run'] for s in steps if s.get('name') == 'Build kubecli ('+architecture+')')
                syntax = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)
                commands = [shlex.split(line, comments=True) for line in script.splitlines()]
                invocation = next(c for c in commands if c[:2] == ['python3', 'build.py'])
                self.assertEqual(invocation, ['python3', 'build.py', '--kubecli-only'])
                # Exercise build.py's actual argument parser and command plan.
                # The adapter records pip/PyInstaller; it installs/builds nothing.
                with patch.object(sys, 'argv', invocation[1:]), patch.object(build, 'run_command') as execute:
                    build.main()
                calls = [call.args[0] for call in execute.call_args_list]
                packaging = [c for c in calls if c[1:3] == ['-m', 'PyInstaller']]
                self.assertEqual(packaging, [[sys.executable, '-m', 'PyInstaller', '--clean',
                                               str(ROOT/'kubecli-onefile.spec')]])
                self.assertFalse(any('tools-onefile.spec' in arg for c in calls for arg in c))


if __name__ == '__main__':
    unittest.main()

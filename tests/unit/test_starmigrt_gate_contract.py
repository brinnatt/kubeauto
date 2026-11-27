"""Exercise scoped gate refusal and real CLI supervision before any lab mutation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('starmigrt_gate_subject', ROOT / 'tests/helpers/starmigrt-live.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateContracts(unittest.TestCase):
    def test_final_refuses_each_unmet_prerequisite_before_ssh(self):
        for ident in gate.IDS[:-1]:
            cases = [dict(id=i, status='pending' if i == ident else 'pass') for i in gate.IDS]
            with self.subTest(id=ident), patch.object(gate, 'matrix', return_value=({}, cases)), patch.object(gate, 'evidence'), patch.object(gate, 'run') as runner, patch.object(gate, 'preflight') as preflight:
                with self.assertRaisesRegex(ValueError, ident):
                    gate.launch('full')
                runner.assert_not_called()
                preflight.assert_not_called()

    def test_final_refuses_stale_evidence_before_ssh(self):
        cases = [dict(id=i, status='pass') for i in gate.IDS]
        with patch.object(gate, 'matrix', return_value=({}, cases)), patch.object(gate, 'evidence', side_effect=ValueError('stale')), patch.object(gate, 'run') as runner:
            with self.assertRaisesRegex(ValueError, 'stale'):
                gate.launch('full')
            runner.assert_not_called()

    def test_cli_refuses_nonzero_even_with_success_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = type('Worker', (), dict(base=Path(tmp)))()
            scenario = gate.Scenario(worker)
            try:
                fake = subprocess.CompletedProcess([], 1, 'STARMIGRT_PASS', '')
                with patch.object(gate.subprocess, 'run', return_value=fake):
                    with self.assertRaisesRegex(ValueError, 'rc=1'):
                        scenario.cli('dump', expected='STARMIGRT_PASS')
            finally:
                scenario.log.close()

    def test_cli_requires_specific_negative_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = gate.Scenario(type('Worker', (), dict(base=Path(tmp)))())
            try:
                with patch.object(gate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'unrelated crash')):
                    with self.assertRaises(ValueError):
                        scenario.cli('dump', expected='STARMIGRT_ERROR', rc=1)
            finally:
                scenario.log.close()

    def test_remote_scope_refused_before_infrastructure(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path.cwd()
            try:
                os.chdir(tmp)
                Path('plan.json').write_text(json.dumps(dict(base='/tmp/foreign', id='a'*32)))
                with patch.object(gate, 'infrastructure') as infra:
                    with self.assertRaisesRegex(ValueError, 'ownership'):
                        gate.remote()
                    infra.assert_not_called()
            finally:
                os.chdir(old)

    def test_qualification_inventory_complete_and_two_files(self):
        _, cases = gate.matrix()
        self.assertEqual([c['id'] for c in cases], gate.IDS)
        self.assertEqual({p.name for p in (ROOT / 'tools/starrocks/starmigration').iterdir()}, {'StarMigrtCli.py', 'README.md'})
        self.assertEqual(set(gate.inputs()), {str(p) for p in gate.inputs()})


    def test_fixture_strings_fit_declared_utf8_byte_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = gate.Scenario(type('Worker', (), dict(base=Path(tmp)))())
            scenario.source = 'fixture-only'
            statements = []
            scenario.sql = lambda host, sql: statements.append(sql) or [('fixture', 'CREATE TABLE fixture')] 
            try:
                scenario.fixture()
                import re
                ddl = next(s for s in statements if s.startswith('CREATE TABLE sd_a.z_bulk'))
                insert = next(s for s in statements if s.startswith('INSERT INTO sd_a.z_bulk'))
                capacity = int(re.search(r'txt VARCHAR\((\d+)\)', ddl)[1])
                text, count = re.search(r"repeat\('([^']+)',(\d+)\)", insert).groups()
                self.assertLessEqual(len(text.encode('utf-8')) * int(count), capacity)
                self.assertIn('generate_series(1,65536)', insert)
            finally:
                scenario.log.close()

    def test_transient_ssh_does_not_cancel_healthy_durable_gate(self):
        results = [subprocess.CompletedProcess([], 255, '', 'timeout'), subprocess.CompletedProcess([], 0, 'progress', '')]
        with patch.object(gate.subprocess, 'run', side_effect=results) as runner, patch.object(gate.time, 'sleep'):
            self.assertEqual(gate.poll_remote('/tmp/ksd-' + 'a'*16), 'progress')
            self.assertEqual(runner.call_count, 2)

    def test_ssh_retry_is_bounded(self):
        with patch.object(gate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 255, '', 'timeout')) as runner, patch.object(gate.time, 'sleep'):
            with self.assertRaises(ConnectionError):
                gate.poll_remote('/tmp/ksd-' + 'a'*16)
            self.assertEqual(runner.call_count, 3)


    def test_freeze_precheck_excludes_unsupported_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'StarMigrtCli.py').write_text('print("fixture")')
            (base/'tools-onefile.spec').write_text('# isolated spec fixture')
            scenario = gate.Scenario(type('Worker', (), dict(base=base))())
            scenario.source = 'fixture-only'
            commands = []
            scenario.cli = lambda *args, **kw: commands.append(args)
            def build(args, **kw):
                self.assertEqual(kw['env']['TOOLS_SELECTED'], 'StarMigrtCli')
                dist = kw['cwd']/'dist'
                dist.mkdir()
                (dist/'StarMigrtCli').write_bytes(b'isolated-artifact')
            try:
                with patch.object(gate, 'run', side_effect=build):
                    scenario.freeze()
                check = next(c for c in commands if c[0] == 'dump' and '--check' in c)
                self.assertEqual(check[check.index('--exclude-table')+1], 'skip_*')
            finally:
                scenario.log.close()


    def test_fixture_subnets_do_not_blackhole_management_source(self):
        peer = '172.18.0.2'
        with self.assertRaisesRegex(ValueError, 'management'):
            gate.guard_subnet('172.18.0.0/16', [], peer)
        routes = [{'dst': 'default'}, {'dst': '192.168.122.0/24'}, {'dst': '172.26.94.128/26'}]
        self.assertEqual(gate.guard_subnet(gate.SUBNETS['source'], routes, peer), '10.251.240.0/28')
        routes.append({'dst': gate.SUBNETS['source']})
        self.assertEqual(gate.guard_subnet(gate.SUBNETS['target'], routes, peer), '10.251.240.16/28')
        with self.assertRaisesRegex(ValueError, 'existing route'):
            gate.guard_subnet('10.251.240.0/24', routes, peer)

    def test_current_file_input_includes_customer_readme(self):
        self.assertIn('tools/starrocks/starmigration/README.md', gate.inputs())

    def test_marker_does_not_accept_wrong_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = gate.Scenario(type('Worker', (), dict(base=Path(tmp)))())
            try:
                with patch.object(gate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'STARMIGRT_LOAD_OK', '')):
                    with self.assertRaisesRegex(ValueError, 'rc=1'):
                        scenario.cli('load', expected='STARMIGRT_LOAD_OK')
            finally:
                scenario.log.close()

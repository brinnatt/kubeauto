"""Evidence reuse must reject changed execution, raw bytes and durable failures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('production_audit', ROOT/'tests/helpers/tools-production-audit.py')
GATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GATE)


class ProductionAuditContracts(unittest.TestCase):
    def test_supply_chain_runbook_reuse_accepts_only_explicit_host_notes(self):
        name = 'tests/README.md'; owner = 'mysql80-binary-supply-chain-20261125'
        old = 'Rocky Linux 8.10（131）\n仍使用 131 与 .122.216，两端实际\nSHA256 unchanged\n每轮 `logs/mysql-backups-example`\n'
        current = old.replace('Rocky Linux 8.10（131）', 'Rocky Linux 8.10（137；既有已接受证据来自 131）', 1)
        current = current.replace('仍使用 131 与 .122.216，两端实际', '后续使用 137 与 .122.216；既有已接受的 131 独立证据保留原身份。两端实际', 1)
        current = current.replace('每轮 `logs/mysql-backups-', GATE.NATIVE_HOST_NOTE+'每轮 `logs/mysql-backups-', 1)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); path = root/name; path.parent.mkdir(); path.write_text(current)
            expected = hashlib.sha256(old.encode()).hexdigest()
            snapshot = root/'logs/mysql80-execution-inputs'/(expected+'.md'); snapshot.parent.mkdir(parents=True); snapshot.write_text(old)
            GATE.compatible_file(name, expected, owner)
            path.write_text(current.replace('SHA256 unchanged', 'SHA256 changed'))
            with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                GATE.compatible_file(name, expected, owner)

    def test_native_host_relocation_preserves_old_identity_and_refuses_other_changes(self):
        name = 'tests/helpers/mysql80-binary-live.py'
        current = (ROOT/name).read_text()
        old = current.replace("'root@192.168.47.137'", "'root@192.168.47.131'", 1)
        self.assertNotEqual(old, current)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); path = root/name; path.parent.mkdir(parents=True); path.write_text(current)
            expected = hashlib.sha256(old.encode()).hexdigest()
            saved = root/'logs/mysql80-execution-inputs'/(expected+'.py'); saved.parent.mkdir(parents=True); saved.write_text(old)
            for owner in ('mysql80-binary-standalone', 'mysql80-binary-primary-replica', 'mysql80-binary-dual-primary'):
                GATE.compatible_file(name, expected, owner)
            for changed in (current.replace("'8.10'", "'8.9'", 1),
                            current.replace("'root@192.168.47.137'", "'root@192.168.122.1'", 1),
                            current.replace('fixture.seed(); fixture.lifecycle()', 'fixture.seed()')):
                path.write_text(changed)
                with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                    GATE.compatible_file(name, expected, 'mysql80-binary-dual-primary')

    def test_physical_host_relocation_reuses_exact_scope_and_rejects_fixture_changes(self):
        physical = GATE.physical_module()
        name = 'tests/helpers/mysql80-physical-live.py'
        test = 'tests/unit/test_mysql80_physical_contract.py'
        current = (ROOT/name).read_text(); contract = (ROOT/test).read_text()
        old = current.replace("'root@192.168.47.137'", "'root@192.168.47.131'", 1)
        old = old.replace("if compatible_inputs(saved['inputs']) and", "if saved['inputs'] == inputs() and")
        old_contract = contract.replace('root@192.168.47.137', 'root@192.168.47.131', 1)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(physical, 'ROOT', Path(directory)), \
                mock.patch.object(physical, 'FILES', [name, test]):
            root = Path(directory); snapshots = root/'logs/mysql80-execution-inputs'; snapshots.mkdir(parents=True)
            recorded = {}
            for n, before, after in ((name, old, current), (test, old_contract, contract)):
                path = root/n; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(after)
                digest = hashlib.sha256(before.encode()).hexdigest(); recorded[n] = digest
                (snapshots/(digest+'.py')).write_text(before)
            self.assertTrue(physical.compatible_inputs(recorded))
            (root/name).write_text(current.replace("for mode in ('host', 'container')", "for mode in ('host',)"))
            self.assertFalse(physical.compatible_inputs(recorded))
            (root/name).write_text(current)
            (root/test).write_text(contract.replace("('8.0.33', '8.0.46')", "('8.0.33',)"))
            self.assertFalse(physical.compatible_inputs(recorded))

    def test_native_port_probe_accepts_time_wait_but_refuses_a_live_listener(self):
        import ast
        import socket
        source = ast.parse((ROOT/'tests/helpers/mysql80-binary-live.py').read_text())
        fixture = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'Fixture')
        setup = next(node for node in fixture.body if isinstance(node, ast.FunctionDef) and node.name == 'setup')
        # Compile the actual setup preflight, without starting MySQL, chown or
        # systemd. A separate reimplementation would miss this precise defect.
        statement = next(node for node in ast.walk(setup) if isinstance(node, ast.With)
                         and ast.unparse(node.items[0].context_expr) == 'socket.socket()')
        code = compile(ast.fix_missing_locations(ast.Module(body=[statement], type_ignores=[])), '<actual-port-probe>', 'exec')
        def probe(port):
            exec(code, {'socket': socket, 'port': port})
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaises(OSError): probe(port)
            with socket.socket() as client:
                client.connect(('127.0.0.1', port))
                connection, _ = listener.accept()
                with connection:
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b'')
        with socket.socket() as old_probe:
            with self.assertRaises(OSError): old_probe.bind(('127.0.0.1', port))
        probe(port)

    def test_native_evidence_reuse_allows_only_the_exact_port_and_guard_repair(self):
        name = 'tests/helpers/mysql80-binary-live.py'
        current = (ROOT/name).read_text()
        self.assertIn(GATE.NATIVE_PORT_NEW, current)
        self.assertIn(GATE.NATIVE_GUARD_NEW, current)
        old = current.replace(GATE.NATIVE_PORT_NEW, GATE.NATIVE_PORT_OLD, 1).replace(GATE.NATIVE_GUARD_NEW, GATE.NATIVE_GUARD_OLD, 1)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); path = root/name; path.parent.mkdir(parents=True); path.write_text(current)
            expected = hashlib.sha256(old.encode()).hexdigest()
            saved = root/'logs/mysql80-execution-inputs'/(expected+'.py'); saved.parent.mkdir(parents=True); saved.write_text(old)
            for owner in ('mysql80-binary-standalone', 'mysql80-binary-primary-replica'):
                GATE.compatible_file(name, expected, owner)
            with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                GATE.compatible_file(name, expected, 'mysql80-binary-dual-primary')
            path.write_text(current.replace('fixture.seed(); fixture.lifecycle()', 'fixture.seed()'))
            with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                GATE.compatible_file(name, expected, 'mysql80-binary-primary-replica')

    def test_status_rejects_stale_accepted_mybackup_contract_before_summary(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE,'ROOT',Path(directory)):
            path=Path(directory)/'tests/tools-test-matrix.yaml';path.parent.mkdir()
            cases=[{'id':'TL-MBK-%02d'%n,'status':'pass'} for n in range(1,7)]
            cases.append({'id':'TL-X-06','status':'pass'})
            cases += [{'id':key,'status':'pending'} for key in ('TL-X-09','TL-X-10')]
            path.write_text(json.dumps({'test_cases':cases}))
            with mock.patch.object(GATE,'verify_record') as verify:
                GATE.status()
                self.assertEqual([call.args[0] for call in verify.call_args_list],
                    ['mysql-backup-portability-20261125','mybackup-native-contract-20261125','mysql84-physical-requal-20261125',
                     'mysql80-binary-supply-chain-20261125'])
                verify.side_effect=ValueError('TOOLS_AUDIT_RAW_STALE')
                with self.assertRaisesRegex(ValueError,'RAW_STALE'):GATE.status()

    def test_migration_fixture_isolation_preserves_all_original_rejection_assertions(self):
        name='tests/unit/test_tools_evidence_dependency_contract.py'
        current=(ROOT/name).read_text()
        isolated=(
            '            # A deterministic input-binding fixture must not inherit another\n'
            "            # product's current acceptance state from the working checkout.\n"
            "            for case in matrix['test_cases']:\n"
            "                if case['id'].startswith(('TL-MIG-', 'TL-X-')):\n"
            "                    case['status'] = 'pass'\n")
        old=current.replace(isolated,'',1)
        self.assertNotEqual(old,current)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE,'ROOT',Path(directory)):
            root=Path(directory);path=root/name;path.parent.mkdir(parents=True);path.write_text(current)
            expected=hashlib.sha256(old.encode()).hexdigest()
            saved=root/'logs/tools-execution-inputs'/(expected+'.py');saved.parent.mkdir(parents=True);saved.write_text(old)
            GATE.compatible_file(name,expected,'tools-migration-endpoint-full-20261125')
            path.write_text(current.replace('MIGRATION_FINAL_DURABLE_REFUSED','WRONG_REJECTION'))
            with self.assertRaisesRegex(ValueError,'AUDIT_STALE'):
                GATE.compatible_file(name,expected,'tools-migration-endpoint-full-20261125')

    def test_mysql_runbook_and_inventory_count_change_do_not_resign_starmigrt(self):
        before='The tools matrix contains 78 scenarios (77 applicable and one not applicable) plus a 112-item\nSTARMIGRT commands unchanged\n'
        after='每轮 `logs/mysql-backups-example` evidence\n'
        old=before+after
        current=before.replace('78 scenarios (77','90 scenarios (89').replace('112-item','117-item')+'MySQL 8.0 的物理生命周期补充\nnew native runbook\n'+after
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE,'ROOT',Path(directory)):
            root=Path(directory); name='tests/README.md'; path=root/name;path.parent.mkdir();path.write_text(current)
            expected=hashlib.sha256(old.encode()).hexdigest()
            saved=root/'logs/tools-execution-inputs'/(expected+'.md');saved.parent.mkdir(parents=True);saved.write_text(old)
            GATE.compatible_file(name,expected,'starmigrt-full')
            path.write_text(current.replace('STARMIGRT commands unchanged','STARMIGRT changed command'))
            with self.assertRaisesRegex(ValueError,'AUDIT_STALE'):
                GATE.compatible_file(name,expected,'starmigrt-full')

    def test_all_native_owning_records_are_required_and_stale_final_is_not_ignored(self):
        physical=mock.Mock(); native=mock.Mock()
        with mock.patch.object(GATE,'verify_record'), mock.patch.object(GATE,'logical_evidence'), \
                mock.patch.object(GATE,'physical_module',return_value=physical), \
                mock.patch.object(GATE,'binary_module',return_value=native):
            GATE.verify_dependencies()
            self.assertEqual([call.args[0].name for call in native.verify_record.call_args_list],
                ['mysql80-binary-standalone.json','mysql80-binary-primary-replica.json',
                 'mysql80-binary-dual-primary.json','mysql80-binary-full.json'])
            native.verify_record.side_effect=ValueError('BINARY80_EVIDENCE_STALE')
            with self.assertRaisesRegex(ValueError,'BINARY80_EVIDENCE_STALE'):
                GATE.verify_dependencies()

    def test_unrelated_branch_and_build_allowlist_do_not_change_owning_execution(self):
        text = (ROOT/'tests/run_tools_regression.sh').read_text()
        branch = '--migration-live'
        baseline = GATE.launcher_execution(text, branch)
        self.assertEqual(baseline, GATE.launcher_execution(text.replace('TOOLS_KUBE_BACKUP_EXIT', 'UNRELATED'), branch))
        self.assertEqual(baseline, GATE.launcher_execution(text.replace(' || "$TOOLS_SELECTED" == KafkaCli,StarCli', ''), branch))
        self.assertNotEqual(baseline, GATE.launcher_execution(text.replace('set -euo pipefail', 'set +e'), branch))
        self.assertNotEqual(baseline, GATE.launcher_execution(text.replace('stage="${MIGRATION_STAGE:-full}"', 'stage=wrong'), branch))
        self.assertNotEqual(baseline, GATE.launcher_execution(text.replace('&& "${1:---preflight}" == --build-only', '&& true'), branch))

    def test_tampered_snapshot_cannot_rescue_changed_source(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory)
            path = root/'tools/product.py'; path.parent.mkdir(); path.write_text('new')
            expected = hashlib.sha256(b'old').hexdigest()
            saved = root/'logs/tools-execution-inputs'/(expected+'.py'); saved.parent.mkdir(parents=True)
            saved.write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'SNAPSHOT_MISSING'):
                GATE.compatible_file('tools/product.py', expected, 'owner')
            saved.write_text('old')
            with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                GATE.compatible_file('tools/product.py', expected, 'owner')

    def test_raw_digest_success_marker_and_durable_outcome_are_independent_requirements(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); (root/'logs').mkdir(); (root/'tests/evidence').mkdir(parents=True)
            log = root/'logs/run.log'; log.write_text('OWNING_PASS\nSCOPED_CLEAN_PASS\n')
            fence = root/'logs/run.exit'; fence.write_text('0\n')
            proof = {'rc': 0, 'markers': ['OWNING_PASS', 'SCOPED_CLEAN_PASS'], 'files': {}}
            def save():
                proof['files'] = {str(p.relative_to(root)): GATE.sha(p) for p in (log, fence)}
                (root/'tests/evidence/owner.json').write_text(json.dumps(proof))
            save(); GATE.verify_record('owner')
            log.write_text('tampered')
            with self.assertRaises(ValueError): GATE.verify_record('owner')
            save()
            with self.assertRaisesRegex(ValueError, 'MARKER_MISSING'): GATE.verify_record('owner')
            log.write_text('OWNING_PASS\nSCOPED_CLEAN_PASS\n'); fence.write_text('1\n'); save()
            with self.assertRaisesRegex(ValueError, 'DURABLE_REFUSED'): GATE.verify_record('owner')

    def test_physical_evidence_reuse_does_not_ignore_cleanup_or_backup_body_changes(self):
        text = (ROOT/'tests/helpers/mysql-backups-regression.sh').read_text()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(GATE, 'ROOT', Path(directory)):
            root = Path(directory); name = 'tests/helpers/mysql-backups-regression.sh'
            path = root/name; path.parent.mkdir(parents=True); path.write_text(text)
            old = text.replace('select_digest_image() {', 'unrelated_portability_selector() {')
            expected = hashlib.sha256(old.encode()).hexdigest()
            saved = root/'logs/tools-execution-inputs'/(expected+'.sh'); saved.parent.mkdir(parents=True); saved.write_text(old)
            GATE.compatible_file(name, expected, 'mysql84-physical-requal-20261125')
            for value in (text.replace('MYBACKUP_NATIVE_BACKUP_PASS', 'WRONG'), text.replace('trap cleanup EXIT', 'trap true EXIT')):
                path.write_text(value)
                with self.assertRaisesRegex(ValueError, 'AUDIT_STALE'):
                    GATE.compatible_file(name, expected, 'mysql84-physical-requal-20261125')

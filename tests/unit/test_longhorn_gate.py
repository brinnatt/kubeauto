"""Longhorn fixed-runner contracts, independent from existing middleware evidence."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('longhorn_matrix', ROOT / 'tests/helpers/validate_longhorn_matrix.py')
matrix = importlib.util.module_from_spec(spec); spec.loader.exec_module(matrix)


class LonghornGateContracts(unittest.TestCase):
    def test_integrity_full_material_refuses_foreign_path_before_sync_or_launch(self):
        script = '''source "$1"
ROOT="$2"
longhorn_local_gate() { exit 91; }
ssh_longhorn() { exit 92; }
export LONGHORN_FULL_RESUME_DIRECTORY="$3"
longhorn_launch v2-integrity-qualification LONGHORN_V2_INTEGRITY_QUALIFICATION_PASS
'''
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run(['bash','-c',script,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh'),
                                   str(ROOT),directory],text=True,capture_output=True)
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertIn('LONGHORN_FULL_EVIDENCE_FAIL',result.stdout)
        source=(ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        transfer=source.index('"$focused_work/" "root@192.168.47.130:$focused_remote/"')
        self.assertLess(transfer,source.index('printf \'%s\\n\' "$module" >&"$in_fd"',transfer))
        remote=source[source.index('if test "$mode" = v2-integrity-qualification; then'):]
        self.assertLess(remote.index('focused-material --directory "$focused_work"'),
                        remote.index('export LONGHORN_FULL_EVIDENCE_DIR="$focused_work/evidence"'))

    def test_integrity_remote_material_runs_outside_project_and_stops_on_refusal(self):
        source = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        remote = source.split('if test "$mode" = v2-integrity-qualification; then', 1)[1].split('if test "$mode" = full; then', 1)[0]
        remote = 'if test "$mode" = v2-integrity-qualification; then'+remote
        for accepted in (True, False):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)/'product'; outside = Path(directory)/'unrelated'
                outside.mkdir(); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                helper = base/'tests/helpers/longhorn-full-evidence.py'
                helper.parent.mkdir(parents=True)
                # Exercise executable and script resolution from the actual SSH
                # snippet; the owning reader's receipt semantics have separate tests.
                helper.write_text('import sys\nfrom pathlib import Path\n'
                                  'assert sys.argv[1:3] == ["focused-material", "--directory"]\n'
                                  'print("LONGHORN_FULL_FOCUSED_MATERIAL_PASS scope=v2-integrity-qualification", flush=True)\n'
                                  'sys.exit(0 if (Path(sys.argv[3])/"accepted").exists() else 7)\n')
                work = base/'logs/longhorn-full/run.contract'; (work/'evidence').mkdir(parents=True)
                if accepted: (work/'accepted').touch()
                script = 'set -eu\nmode=v2-integrity-qualification\n'+remote.replace('/usr/local/kubeauto', str(base))
                script += '\nprintf "%s\\n" "$LONGHORN_FULL_EVIDENCE_DIR"\n'
                result = subprocess.run(['bash', '-c', script], cwd=outside, input=str(work)+'\n',
                                        text=True, capture_output=True)
                self.assertEqual(0 if accepted else 7, result.returncode, result.stdout+result.stderr)
                self.assertEqual(str(work/'evidence')+'\n' if accepted else '', result.stdout)
                self.assertIn('LONGHORN_FULL_FOCUSED_MATERIAL_PASS', result.stderr)

    def test_uninstall_diagnostics_read_original_namespace_without_secret_payloads_or_sync(self):
        script = 'source "$1"\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-uninstall-diagnostics\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        code = result.stdout.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        compile(code, 'uninstall-diagnostics', 'exec')
        for forbidden in ('write_text', 'unlink', 'lab.remove', 'lab.kubectl', 'lab.cleanup',
                          'lab.run_requests', 'sync-kubeauto', '["data"]', '["stringData"]'):
            self.assertNotIn(forbidden, result.stdout)
        self.assertIn('lab.owner()', code)
        self.assertIn('namespace["metadata"]["uid"]==state["namespace_uid"]', code)
        self.assertIn('"leases.coordination.k8s.io"', code)
        self.assertIn('obj.get("spec") if kind=="leases.coordination.k8s.io" else None', code)
        # The diagnostic loads lab without registering it in sys.modules.
        # Use the actual Store constructor with that module, not backup_store(),
        # whose runtime CLI contract requires a registered main module.
        import ast
        import io
        import json
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location('unregistered_diagnostic_lab', ROOT/'tests/helpers/longhorn-lab.py')
        lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
        spec = importlib.util.spec_from_file_location('diagnostic_backups', ROOT/'tests/helpers/longhorn-backups.py')
        backups = importlib.util.module_from_spec(spec); spec.loader.exec_module(backups)
        branch = next(node for node in ast.parse(code).body if isinstance(node, ast.If))
        with tempfile.TemporaryDirectory() as directory:
            lab.DIRECTORY = Path(directory)
            (lab.DIRECTORY/'longhorn-backup-store.json').write_text('{}')
            with patch.object(lab, 'load_helper', return_value=backups), \
                    patch.object(backups.Store, 'receipt', return_value={'product_binding': {}, 'transport': {}}), \
                    patch.object(backups.Store, 'live', return_value=True), \
                    patch.object(backups.Store, 'inspect', return_value={'Id': 'owned'}), \
                    redirect_stdout(io.StringIO()) as output:
                exec(compile(ast.Module(body=[branch], type_ignores=[]), 'uninstall-store-diagnostic', 'exec'),
                     {'lab': lab, 'json': json})
            self.assertEqual(json.loads(output.getvalue()), {'backup_store_live': True, 'container_present': True,
                             'product_binding_present': False, 'transport_present': False})

    def test_uninstall_host_diagnostic_reads_only_exact_journal_paths(self):
        import ast
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        script = 'source "$1"\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-uninstall-diagnostics\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        code = result.stdout.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        branch = [node for node in ast.parse(code).body if isinstance(node, ast.If)][-1]
        paths = {'/owned-file': b'public-content'}
        state = {'host_state': {'expected': {'/owned-file': 'original-hash'},
                               'nodes': {'owned-node': {'files': {'/owned-file': None}}}}}
        calls = []
        def remote(node, argv, *, stdin):
            calls.append((node, json.loads(stdin)))
            compile(argv[2], 'host-diagnostic-probe', 'exec')
            for forbidden in ('write_', 'unlink', 'systemctl', 'subprocess', 'os.'):
                self.assertNotIn(forbidden, argv[2])
            return SimpleNamespace(stdout=json.dumps({'/owned-file': {'sha256': 'current-hash', 'symlink': False}}))
        lab = SimpleNamespace(NODES=['owned-node'], host_files=lambda: paths, remote=remote)
        with redirect_stdout(io.StringIO()) as output:
            exec(compile(ast.Module(body=[branch], type_ignores=[]), 'host-diagnostic', 'exec'),
                 {'lab': lab, 'state': state, 'json': json})
        row = json.loads(output.getvalue())
        self.assertEqual(calls, [('owned-node', ['/owned-file'])])
        self.assertEqual(row['journal_expected'], state['host_state']['expected'])
        state['host_state']['expected']['/foreign'] = 'foreign'
        with self.assertRaises(AssertionError):
            exec(compile(ast.Module(body=[branch], type_ignores=[]), 'host-diagnostic', 'exec'),
                 {'lab': lab, 'state': state, 'json': json})

    def test_v2_host_inventory_is_read_only_and_uses_only_approved_general_hosts(self):
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        script = 'source "$1"\npython3() { cat; }\nlonghorn_dispatch --longhorn-v2-host-inputs\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        code = result.stdout
        calls = []
        def run(argv, **options):
            import shlex
            calls.append(argv)
            self.assertEqual(argv[:5], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5'])
            remote = shlex.split(argv[-1]); self.assertEqual(remote[:2], ['python3', '-c'])
            compile(remote[2], 'v2-read-only-host', 'exec')
            for forbidden in ('write_text', 'unlink', 'sudo', 'kubectl', 'modprobe', 'wipefs', 'mount('):
                self.assertNotIn(forbidden, remote[2])
            return SimpleNamespace(returncode=0, stdout='{"kernel_ge_6_7":false}', stderr='PRIVATE-ERROR')
        with patch('subprocess.run', side_effect=run), redirect_stdout(io.StringIO()) as output:
            exec(compile(code, 'v2-host-inventory', 'exec'), {})
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual({row['host'] for row in rows}, {
            'root@192.168.122.135', 'root@192.168.122.40', 'root@192.168.122.72',
            'root@192.168.122.212', 'root@192.168.122.165', 'root@192.168.122.238',
            'brinnatt@192.168.47.128', 'ubuntu@192.168.47.138', 'root@192.168.47.143',
            'root@192.168.122.2', 'root@192.168.122.217', 'root@192.168.122.243',
            'root@192.168.122.246', 'root@192.168.122.193', 'root@192.168.122.210', 'root@192.168.122.216',
            'root@192.168.47.130', 'root@192.168.47.131', 'root@192.168.47.132',
            'root@192.168.47.133', 'root@192.168.47.134', 'root@192.168.47.135',
            'root@192.168.47.136', 'root@192.168.47.137', 'root@192.168.47.141', 'root@192.168.47.142'})
        self.assertEqual(len(calls), 26)
        self.assertNotIn('PRIVATE-ERROR', output.getvalue())
        for forbidden in ('sync-kubeauto', '145', '146', '147', '192.168.122.1"'):
            self.assertNotIn(forbidden, code)

    def test_business_diagnostics_are_current_uid_bound_and_read_only(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-business-diagnostics\n'
        result = subprocess.run(['bash','-c',script,'contract',str(source)],text=True,capture_output=True,check=True)
        code = result.stdout.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        compile(code,'business-diagnostics','exec')
        for forbidden in ('write_text','unlink','lab.remove','lab.kubectl','lab.cleanup','lab.run_requests','sync-kubeauto','"data"'):
            self.assertNotIn(forbidden,result.stdout)
        self.assertIn('lab.owner()',code)
        self.assertIn('["metadata"]["uid"]==row["namespace_uid"]',code)
        self.assertIn('obj.get("involvedObject",obj.get("regarding",{}))',code)

    def test_business_native_diagnostics_exclude_credentials_and_raw_errors(self):
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import Mock
        script = 'source "$1"\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-business-diagnostics\n'
        result = subprocess.run(['bash','-c',script,'contract',str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True,capture_output=True,check=True)
        code = result.stdout.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('longhorn-install-business.json').write_text(json.dumps({
                'namespace':'longhorn-business-owned','namespace_uid':'root-uid','objects':{}}))
            canary = 'DO-NOT-PRINT-CREDENTIAL'
            def get(kind, *args, **kwargs):
                if kind == 'namespace': return {'metadata':{'uid':'root-uid'}}
                if '.' not in kind or kind=='events.events.k8s.io': return {'items':[]}
                return {'items':[{'metadata':{'name':'native','uid':'native-uid','annotations':{'token':canary}},
                    'spec':{'volumeName':'source','nodeID':'worker','credentialSecret':canary,'data':canary},
                    'status':{'ownerID':'worker','state':'Completed','error':canary,'url':canary}}]}
            lab = SimpleNamespace(owner=Mock(),get=Mock(side_effect=get),DIRECTORY=root)
            with patch('importlib.util.spec_from_file_location',return_value=SimpleNamespace(loader=Mock())), \
                    patch('importlib.util.module_from_spec',return_value=lab), redirect_stdout(io.StringIO()) as output:
                exec(code,{})
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertNotIn(canary,output.getvalue())
            self.assertEqual(len(rows),8)
            for row in rows[1:]:
                self.assertEqual(row['status'],{'ownerID':'worker','state':'Completed'})
                self.assertEqual(row['spec'],{'volumeName':'source','nodeID':'worker'})
                self.assertTrue(row['error_present'])
            lab.owner.assert_called_once()
            root.joinpath('longhorn-backup-store.json').write_text('{}')
            root.joinpath('cas').mkdir();root.joinpath('cas/ca.crt').write_text(canary)
            store = SimpleNamespace(receipt=Mock(return_value={'secret':canary}),inspect=Mock(return_value={'secret':canary}),
                transport_receipt=Mock(return_value={'listener':{'secret':canary}}),live=Mock(return_value=True),
                directory=root,product_endpoint='https://approved-host:23523')
            lab.load_helper=Mock(return_value=SimpleNamespace(Store=Mock(return_value=store)))
            lab.NODES=('approved-a','approved-b')
            lab.remote=Mock(side_effect=[subprocess.CompletedProcess([],0,'',canary),
                                         subprocess.CompletedProcess([],1,'',canary)])
            with patch('importlib.util.spec_from_file_location',return_value=SimpleNamespace(loader=Mock())), \
                    patch('importlib.util.module_from_spec',return_value=lab), redirect_stdout(io.StringIO()) as output:
                exec(code,{})
            self.assertNotIn(canary,output.getvalue())
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(rows[-2:], [{'backup_transport_node':'approved-a','tls_health_rc':0,'diagnostic':None},
                                         {'backup_transport_node':'approved-b','tls_health_rc':1,'diagnostic':None}])
            for call in lab.remote.call_args_list:
                self.assertFalse(call.kwargs['check'])
                probe=call.args[1][-1]
                self.assertIn('ssl.create_default_context',probe)
                self.assertIn('timeout=5',probe)
                for forbidden in ('write','remove','delete','patch','apply'):
                    self.assertNotIn(forbidden,probe)

    def test_cancel_product_selects_exact_owned_storage_or_core_subtree(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-cancel-product\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)],
                                text=True, capture_output=True, check=True)
        code = compile(result.stdout.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0], 'cancel-product', 'exec')
        for step in ('09', '90', '07', '07-extra', '07 --tags mysql', '09 --tags longhorn', '09-extra', 'ambiguous', 'wrong-wrapper'):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                (base/'proc').mkdir()
                (base/'tmp').mkdir()
                (base/'tmp/kubeauto-longhorn-gate.pid').write_text('100')
                command = '/usr/local/kubeauto/.venv/bin/python /usr/local/kubeauto/kubecli.py setup longhorn-gate '
                rows = {100: (1, 'bash run-durable-gate.sh /tmp/kubeauto-longhorn-gate LONGHORN_GATE_EXIT'),
                        101: (100, command + ('09' if step == 'ambiguous' else step)),
                        102: (101, 'ansible-playbook playbooks/09.longhorn.yml'),
                        200: (1, command + '09'),
                        201: (100, 'bash -c echo ' + command + '09')}
                if step == 'ambiguous': rows[103] = (100, command + '90')
                if step == 'wrong-wrapper': rows[100] = (1, 'bash unrelated-runner')
                for pid, (parent, argv) in rows.items():
                    proc = base/'proc'/str(pid)
                    proc.mkdir()
                    (proc/'stat').write_text(f'{pid} (process) S {parent} 0 0')
                    (proc/'cmdline').write_bytes(b'\0'.join(arg.encode() for arg in argv.split())+b'\0')
                def fixture_path(path):
                    return base/str(path).lstrip('/')
                with patch('pathlib.Path', side_effect=fixture_path), patch('os.kill') as kill:
                    if step in ('07', '09', '90'):
                        exec(code, {})
                        self.assertEqual([call.args[0] for call in kill.call_args_list], [102, 101])
                    else:
                        with self.assertRaises(AssertionError): exec(code, {})
                        kill.assert_not_called()

    def test_storage_diagnostic_has_only_scoped_read_commands(self):
        text = (ROOT / 'tests/helpers/longhorn-runner.sh').read_text()
        branch = text.split('    --longhorn-storage-diagnostics)\n', 1)[1].split('\n      ;;', 1)[0]
        for forbidden in ('longhorn_launch', 'sync-kubeauto', 'apply', 'patch', 'delete', 'secret', 'kill'):
            self.assertNotIn(forbidden, branch)
        self.assertIn('--request-timeout=10s', branch)
        self.assertIn('--max-log-requests=6', branch)
        self.assertIn('csi-attacher csi-provisioner csi-resizer csi-snapshotter longhorn-csi-plugin', branch)
        self.assertIn('--tail=60 --max-log-requests=6', branch)

    def test_backup_host_probe_is_read_only_and_does_not_launch_a_gate(self):
        text = (ROOT / 'tests/helpers/longhorn-runner.sh').read_text()
        branch = text.split('    --longhorn-backup-host-probe)\n', 1)[1].split('\n      ;;', 1)[0]
        for forbidden in ('longhorn_launch', 'sync-kubeauto', 'docker run', 'pull', 'mkdir', 'chmod', 'kill'):
            self.assertNotIn(forbidden, branch)
        code = branch.split('import json,shutil,socket,subprocess\n', 1)[1].split("\nPY'", 1)[0]
        compile('import json,shutil,socket,subprocess\n' + code, '<backup-host-probe>', 'exec')

    def test_node_patch_diagnostic_uses_original_preflight_and_server_dry_run_only(self):
        text = (ROOT / 'tests/helpers/longhorn-runner.sh').read_text()
        branch = text.split('    --longhorn-node-patch-diagnostics)\n', 1)[1].split('\n      ;;', 1)[0]
        self.assertIn('product.preflight(c,api', branch)
        self.assertIn('"--dry-run=server"', branch)
        self.assertIn('stdout=subprocess.DEVNULL', branch)
        for forbidden in ('sync-kubeauto', 'product.main(', 'product.reconcile(', 'api.apply(', 'api.patch('):
            self.assertNotIn(forbidden, branch)
        code = branch.split("import importlib.util", 1)[1].rsplit('\nPY', 1)[0]
        compile('import importlib.util'+code, '<node-patch-diagnostic>', 'exec')

    def test_preflight_diagnostic_uses_current_inputs_without_mutation_or_api_payloads(self):
        script = '''source "$1"
longhorn_launch() { exit 99; }
ssh_longhorn() { printf '%s' "$1"; }
longhorn_dispatch --longhorn-preflight-diagnostics
'''
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        for text in ('product.preflight(', 'product.validate(c)', 'traced_run', 'LONGHORN_PREFLIGHT_FAILED_COMMAND'):
            self.assertIn(text, result.stdout)
        for text in ('write_text', 'product.main(', 'product.reconcile(', 'sync-kubeauto', 'print(c)', 'print(run('):
            self.assertNotIn(text, result.stdout)

    def test_image_diagnostics_are_read_only_and_show_layer_and_uncompressed_identity(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = '''source "$1"
longhorn_launch() { exit 99; }
ssh_longhorn() { printf '%s' "$1"; }
longhorn_dispatch --longhorn-image-diagnostics
'''
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)], text=True, capture_output=True, check=True)
        for text in ('--config', '--raw', 'actual_diff_ids', 'published_diff_ids', 'linux/amd64', 'equivalent_manifest'):
            self.assertIn(text, result.stdout)
        for text in ('"copy"', 'pull', 'push', 'write_text', 'sync-kubeauto'):
            self.assertNotIn(text, result.stdout)

    def test_failure_diagnostic_only_reads_current_owned_traceback(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = '''source "$1"
longhorn_launch() { exit 99; }
ssh_longhorn() { printf '%s' "$1"; }
longhorn_dispatch --longhorn-failure
'''
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)], text=True, capture_output=True, check=True)
        for required in ('/tmp/kubeauto-longhorn-live.log', 'kubeauto-longhorn-failure-', 'path.lstat()', 'info.st_uid==os.getuid()', '==0o600'):
            self.assertIn(required, result.stdout)
        for forbidden in ('glob(', 'fixture-failure-', 'write_text', 'unlink(', 'sync-kubeauto'):
            self.assertNotIn(forbidden, result.stdout)

    def test_account_error_diagnostic_binds_current_trace_and_only_emits_fixed_categories(self):
        import io
        import json
        from contextlib import redirect_stdout
        script = 'source "$1"\nlonghorn_launch() { exit 99; }\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-account-error\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        code = result.stdout.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for forbidden in ('glob(', 'write_text', 'unlink(', 'sync-kubeauto', 'print(raw', 'print(content'):
            self.assertNotIn(forbidden, code)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); diagnostic = root/('longhorn-account-failure-'+'a'*24+'.json')
            diagnostic.write_text(json.dumps({'rc':1,'stdout':json.dumps({'error':{'cause':{'error':
                {'Code':'AccessDenied','Message':'DO-NOT-PRINT-SECRET'}}}}),'stderr':'private-signed-url'}))
            diagnostic.chmod(0o600)
            trace = root/'kubeauto-longhorn-failure-current.txt'; trace.write_text('evidence='+str(diagnostic)); trace.chmod(0o600)
            log = root/'live.log'; log.write_text('LONGHORN_PRIVATE_DIAGNOSTIC file='+str(trace)+'\n')
            code = code.replace('/tmp/kubeauto-longhorn-live.log',str(log)).replace('/var/tmp',str(root)).replace('/usr/local/kubeauto/clusters/longhorn-gate',str(root))
            with redirect_stdout(io.StringIO()) as output: exec(code,{})
            row = json.loads(output.getvalue()); self.assertEqual(row['native_codes'],['AccessDenied'])
            self.assertFalse(row['mapped_permission_exact_owned_target'])
            self.assertNotIn('DO-NOT-PRINT',output.getvalue()); self.assertNotIn('private-signed-url',output.getvalue())
            target = 'https://127.0.0.1:9000/longhorn-backup-'+'a'*16+'-denied/authorization-canary'
            response = {'status':'error','error':{'type':'fatal',
                'message':'Unable to read from `lh/'+target.split('/',3)[3]+'`.',
                'cause':{'message':'Insufficient permissions to access this path `'+target+'`','error':{'Path':target}}}}
            for correct in (True,False):
                if not correct: response['error']['cause']['error']['Path'] += '-foreign'
                diagnostic.write_text(json.dumps({'rc':1,'stdout':json.dumps(response),'stderr':''}))
                with redirect_stdout(io.StringIO()) as mapped: exec(code,{})
                self.assertEqual(json.loads(mapped.getvalue())['mapped_permission_exact_owned_target'],correct)
                self.assertNotIn(target,mapped.getvalue())
            for target in (diagnostic,trace):
                target.chmod(0o644)
                with self.assertRaises(AssertionError): exec(code,{})
                target.chmod(0o600)

    def test_command_error_diagnostic_only_classifies_current_private_stderr(self):
        import io
        import json
        import os
        from contextlib import redirect_stdout
        script = 'source "$1"\nlonghorn_launch() { exit 99; }\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-command-error\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True, capture_output=True, check=True)
        code = result.stdout.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for forbidden in ('sync-kubeauto', 'write_text', 'unlink(', 'print(content', 'print(raw'):
            self.assertNotIn(forbidden, code)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); current = root/('fixture-failure-'+'a'*32+'.stderr')
            current.write_text('DO-NOT-PRINT-SECRET Invalid test operation failed'); current.chmod(0o600)
            historical = root/('fixture-failure-'+'b'*32+'.stderr'); historical.write_text('historical secret')
            log = root/'live.log'; log.write_text('LONGHORN_PRIVATE_DIAGNOSTIC file='+str(current)+'\n')
            code = code.replace('/tmp/kubeauto-longhorn-live.log', str(log)).replace('/usr/local/kubeauto/clusters/longhorn-gate', str(root))
            with redirect_stdout(io.StringIO()) as output: exec(code, {})
            row = json.loads(output.getvalue())
            self.assertEqual(row['file'], str(current))
            self.assertEqual(row['bytes'], current.stat().st_size)
            self.assertTrue(row['categories']['patch_test']); self.assertTrue(row['categories']['invalid_request'])
            self.assertNotIn('DO-NOT-PRINT', output.getvalue()); self.assertNotIn('historical', output.getvalue())
            for message in ('foreign host file', 'iSCSI session prevents service restore', 'iSCSI enable state not restored'):
                current.write_text('DO-NOT-PRINT-SECRET\nAssertionError: '+message+'\n')
                with redirect_stdout(io.StringIO()) as output: exec(code, {})
                self.assertEqual(json.loads(output.getvalue())['host_assertions'], [message])
                self.assertNotIn('DO-NOT-PRINT', output.getvalue())
            current.write_text('AssertionError: foreign host file SECRET\n')
            with redirect_stdout(io.StringIO()) as output: exec(code, {})
            self.assertEqual(json.loads(output.getvalue())['host_assertions'], [])
            current.chmod(0o644)
            with self.assertRaises(AssertionError): exec(code, {})
            current.unlink(); current.symlink_to(historical)
            with self.assertRaises(AssertionError): exec(code, {})

    def test_backup_patch_diagnostic_only_uses_server_dry_run_and_checks_unchanged_identity(self):
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import Mock
        script = 'source "$1"\nlonghorn_launch() { exit 99; }\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-backup-patch-diagnostics\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                text=True,capture_output=True,check=True)
        code=result.stdout.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        target={'metadata':{'uid':'target','resourceVersion':'12'},'spec':{'backupTargetURL':'','credentialSecret':''}}
        def get(kind,*_args,**_kwargs):
            if kind=='namespace':return {'metadata':{'uid':'namespace'}}
            if kind=='backuptargets.longhorn.io':return target
            return {'items':[]}
        lab=SimpleNamespace(BASE=Path('/owned'),DIRECTORY=Path('/owned/cluster'),get=Mock(side_effect=get),
                            owner=Mock(return_value={'namespace_uid':'namespace'}))
        def run(argv,**kwargs):
            self.assertIn('--dry-run=server',argv)
            patch_ops=json.loads(kwargs['input']);stale=patch_ops[1]['value']=='0'
            self.assertEqual(patch_ops[-1]['value'],target['spec'])
            return SimpleNamespace(returncode=int(stale),stderr='The request is invalid: the server rejected our request due to an error in our request\n' if stale else '')
        with patch('importlib.util.spec_from_file_location',return_value=SimpleNamespace(loader=Mock())), \
                patch('importlib.util.module_from_spec',return_value=lab),patch('subprocess.run',side_effect=run), \
                redirect_stdout(io.StringIO()) as output:
            exec(code,{})
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['stale_resource_version'] for row in rows],[False,True])

    def test_diagnostics_are_read_only_and_do_not_sync_running_sources(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = '''source "$1"
longhorn_launch() { exit 99; }
ssh_longhorn() { printf '%s' "$1"; }
longhorn_dispatch --longhorn-diagnostics
'''
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)],
                                text=True, capture_output=True, check=True)
        text = result.stdout
        self.assertIn('clusters/longhorn-gate/kubectl.kubeconfig', text)
        self.assertIn('--request-timeout=10s', text)
        self.assertIn('--all-containers --prefix --tail=80 --max-log-requests=6', text)
        for mutation in (' delete ', ' apply ', ' patch ', 'sync-kubeauto', 'mkdir', ' > '):
            self.assertNotIn(mutation, text)

    def test_recurring_runtime_diagnostic_reads_only_original_source_and_public_selectors(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { exit 99; }\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-recurring-diagnostics\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)], text=True, capture_output=True, check=True)
        text = result.stdout
        for expected in ('longhorn-recurring-runtime.json', 'source_labels', 'row["volume_uid"]',
                         'row["run_id"]==state["run_id"]', 'not record.is_symlink()',
                         '"snapshot_recorded"', '"snapshot_present"', '"source_matches"', '"target_matches"',
                         'bool(status.get("error"))', 'store.mc("cat"', '"backup_metadata_for"'):
            self.assertIn(expected, text)
        for forbidden in ('sync-kubeauto', 'write_text', 'write_private', 'lab.remove', 'lab.kubectl',
                          'backup_configuration', 'secret', 'print(row)', 'lab.backup_store('):
            self.assertNotIn(forbidden, text)
        code = text.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        compile(code, 'recurring-runtime-diagnostic', 'exec')

    def test_recurring_patch_diagnostic_uses_original_uid_and_server_dry_run(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { exit 99; }\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-recurring-patch-diagnostics\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)], text=True, capture_output=True, check=True)
        text = result.stdout
        self.assertIn('current["metadata"]["uid"]==row["uid"]', text)
        self.assertIn('"--dry-run=server"', text)
        self.assertIn('"--field-manager=kubeauto-longhorn"', text)
        self.assertNotIn('sync-kubeauto', text)
        code = text.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        compile(code, 'recurring-diagnostic', 'exec')

    def test_source_fingerprint_reads_only_synchronized_source_and_excludes_runtime(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = 'source "$1"\nlonghorn_launch() { exit 99; }\nssh_longhorn() { printf "%s" "$1"; }\nlonghorn_dispatch --longhorn-source-fingerprint\n'
        result = subprocess.run(['bash', '-c', script, 'contract', str(source)], text=True, capture_output=True, check=True)
        text = result.stdout
        for forbidden in ('sync-kubeauto', 'clusters/', 'kubectl', 'write_text', 'write_bytes'):
            self.assertNotIn(forbidden, text)
        self.assertIn('roles/longhorn/**/*', text)
        self.assertIn('hashlib.sha256(path.read_bytes())', text)
        compile(text.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0], 'source-fingerprint', 'exec')

    def test_read_only_status_modes_do_not_launch_or_sync(self):
        source = ROOT / 'tests/helpers/longhorn-runner.sh'
        script = '''source "$1"
longhorn_launch() { exit 99; }
remote_job_summary() { printf 'summary %s\\n' "$*"; }
remote_log_tail() { printf 'log %s\\n' "$*"; }
matrix_counts() { printf 'matrix %s\\n' "$*"; }
longhorn_dispatch "$2"
'''
        for mode, output in (('--longhorn-status', 'summary'), ('--longhorn-log', 'log')):
            result = subprocess.run(['bash', '-c', script, 'contract', str(source), mode],
                                    text=True, capture_output=True, check=True)
            self.assertTrue(result.stdout.startswith(output + ' '))
            self.assertIn('/tmp/kubeauto-longhorn-live.log', result.stdout)

    def test_complete_independent_matrix(self):
        data = matrix.validate(ROOT / 'tests/longhorn-test-matrix.yaml')
        self.assertEqual(data['meta']['component'], 'longhorn')
        self.assertEqual(len(data['cases']), 54)

    def test_v2_acceptance_cannot_inherit_v1_results_or_omit_official_failure_paths(self):
        data = yaml.safe_load((ROOT / 'tests/longhorn-test-matrix.yaml').read_text())
        self.assertEqual(set(data['boundaries']['data_engines']), {'v1', 'v2'})
        variants = [c for c in data['cases'] if c['group'].startswith('v2-')]
        self.assertEqual(len(variants), 11)
        for case in variants:
            self.assertEqual(case['data_engines'], ['v2'])
            for key in ('official_behavior', 'project_assertion', 'prerequisites', 'failure_paths'):
                self.assertTrue(case[key], (case['id'], key))
        # An upstream GA declaration or V1 PASS cannot sign off an unimplemented V2 gate.
        c = copy.deepcopy(data)
        variant = c['cases'][-1]
        variant.update(status='pass', implementation_status='pending',
                       evidence='tests/evidence/longhorn-rwo-20261019.json')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'matrix.yml'
            path.write_text(yaml.safe_dump(c))
            with self.assertRaises(ValueError):
                matrix.validate(path)

    def test_matrix_rejects_missing_case_duplicate_marker_and_unbound_pass(self):
        data = yaml.safe_load((ROOT / 'tests/longhorn-test-matrix.yaml').read_text())
        for change in ('missing', 'duplicate', 'pass'):
            c = copy.deepcopy(data)
            if change == 'missing': c['cases'].pop()
            elif change == 'duplicate': c['cases'][0]['success_marker'] = c['cases'][1]['success_marker']
            else: c['cases'][0]['status'] = 'pass'; c['cases'][0]['implementation_status'] = 'pending'
            with tempfile.TemporaryDirectory() as directory:
                p = Path(directory) / 'matrix.yaml'; p.write_text(yaml.safe_dump(c))
                with self.assertRaises(ValueError): matrix.validate(p)

    def test_dispatch_is_independent_and_precedes_core_work(self):
        text = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        self.assertIn('"$MODE" != --longhorn-*', text)
        start = text.index('if [[ "$MODE" == --longhorn-* ]]; then')
        end = text.index('\nfi', start)
        self.assertIn('longhorn_dispatch "$MODE"', text[start:end])
        self.assertIn('exit $?', text[start:end])

    def test_destructive_guards_precede_existing_mutation_and_ignore_install_switch(self):
        for path in ('32.delnode.yml', '33.delmaster.yml', '99.clean.yml'):
            plays = yaml.safe_load((ROOT / 'playbooks' / path).read_text())
            first = plays[0]
            self.assertEqual(first['hosts'], 'localhost')
            self.assertTrue(first['tasks'][0]['import_tasks'].endswith('longhorn/tasks/removal-guard.yml'))
        text = (ROOT / 'roles/longhorn/tasks/removal-guard.yml').read_text()
        self.assertTrue(all('when' not in task for task in yaml.safe_load(text)))

    def test_shared_playbook_syntax_has_required_topology_inputs(self):
        text = (ROOT / 'tests/helpers/longhorn-static.sh').read_text()
        self.assertIn('-e NODE_TO_ADD=localhost -e NODE_TO_DEL=localhost --syntax-check', text)

    def test_pool_probe_is_leased_read_only_and_cleans_independent_state(self):
        runner = (ROOT / 'tests/helpers/longhorn-runner.sh').read_text()
        self.assertLess(runner.index('longhorn_local_gate static LONGHORN_STATIC_PASS || return'), runner.index('coproc LONGHORN_LEASE'))
        self.assertLess(runner.index('LONGHORN_LEASE_READY'), runner.index('sync-kubeauto.sh'))
        for lock in ('kubeauto-ceph-priority.lock', 'kubeauto-ceph-compute.lock', 'kubeauto-redis-core-pool.lock'):
            self.assertIn(lock, runner)
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        probe = live.split('  pool-probe)\n', 1)[1].split('\n    ;;', 1)[0]
        self.assertIn('CEPH_STATE_DIR="$work"', probe)
        self.assertEqual(probe.count('lab-wipe-nodes.sh" --verify --rocky-only'), 2)
        self.assertNotIn('lab-wipe-nodes.sh" --rocky-only', probe)
        self.assertNotIn('mkfs', live)
        self.assertIn('LONGHORN_POOL_PROBE_SCOPED_CLEAN_PASS', live)

    def test_registry_qualification_preserves_failure_and_requires_cleanup_before_pass(self):
        import json
        import os
        import sys
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        branch = live.split('  registry-qualification)\n', 1)[1].split('\n    ;;', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '.venv/bin').mkdir(parents=True)
            (base / '.venv/bin/python').symlink_to(sys.executable)
            (base / 'tests/helpers').mkdir(parents=True)
            (base / 'kubecli.py').write_text('import pathlib\npathlib.Path("clusters/longhorn-gate").mkdir(parents=True)\n')
            (base / 'tests/helpers/lab-wipe-nodes.sh').write_text('exit 0\n')
            (base / 'tests/helpers/longhorn-lab.py').write_text('''import json,os,pathlib,sys
mode=sys.argv[1]
p=pathlib.Path('calls.json')
calls=json.loads(p.read_text()) if p.exists() else []
calls.append(mode);p.write_text(json.dumps(calls))
if mode==os.environ.get('FAIL_MODE'): sys.exit(17)
if mode=='qualification-clean': pathlib.Path('clusters/longhorn-gate').rmdir()
''')
            script = 'set -Eeuo pipefail\nBASE="$1"\nNODES=(192.168.122.135)\ncurl() { return 0; }\nsleep() { return 0; }\n' + branch
            for failure in ('', 'relay-qualification', 'relay-clean'):
                result = subprocess.run(['bash', '-c', script, 'contract', str(base)], text=True,
                                        capture_output=True, env=dict(os.environ, FAIL_MODE=failure))
                calls = json.loads((base / 'calls.json').read_text())
                self.assertEqual(calls[:2], ['configure', 'relay-qualification'])
                self.assertIn('relay-clean', calls)
                if failure:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('LONGHORN_REGISTRY_QUALIFICATION_PASS', result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls[-1], 'qualification-clean')
                    self.assertIn('LONGHORN_REGISTRY_QUALIFICATION_PASS', result.stdout)
                if (base / 'clusters/longhorn-gate').exists(): (base / 'clusters/longhorn-gate').rmdir()
                (base / 'calls.json').unlink()

    def test_core_stops_at_failed_product_prerequisite_and_cleans_owned_nodes(self):
        import os
        import sys
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        branch = live.split('  core)\n', 1)[1].split('\n    ;;', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '.venv/bin').mkdir(parents=True)
            (base / '.venv/bin/python').symlink_to(sys.executable)
            (base / 'tests/helpers').mkdir(parents=True)
            (base / 'kube-bin').mkdir()
            kubectl = base / 'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n'); kubectl.chmod(0o755)
            (base / 'tests/helpers/lab-wipe-nodes.sh').write_text('printf "wipe %s\\n" "$*" >> calls.txt\n')
            (base / 'kubecli.py').write_text('''import os,pathlib,sys,time
args=sys.argv[1:]
with open('calls.txt','a') as f:f.write('product '+ ' '.join(args)+'\\n')
if args[0]=='new': pathlib.Path('clusters/longhorn-gate').mkdir(parents=True)
time.sleep(.1)
if args[0]==os.environ.get('FAIL_MODE'):sys.exit(17)
''')
            (base / 'tests/helpers/longhorn-lab.py').write_text('''import pathlib,signal,subprocess,sys,time
mode=sys.argv[1]
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as f:f.write('fixture '+mode+'\\n')
if mode=='supervise-core':
 step=sys.argv[sys.argv.index('--step')+1]
 args=['download','-D'] if step=='download' else ['setup','longhorn-gate','90']
 sys.exit(subprocess.run([sys.executable,'kubecli.py']+args).returncode)
if mode=='archive-core':pathlib.Path('clusters/longhorn-gate').rmdir()
''')
            control = base / 'control-config'; control.write_text('original-config')
            branch = branch.replace('/root/.kube/config', str(control)).replace('/var/tmp/kubeauto-longhorn-core.', str(base / 'work.'))
            script = 'set -Eeuo pipefail\nBASE="$1"\nNODES=(192.168.122.135)\ncurl() { return 0; }\n' + branch
            for failure in ('download', 'setup', ''):
                result = subprocess.run(['bash', '-c', script, 'contract', str(base)], text=True,
                                        capture_output=True, env=dict(os.environ, FAIL_MODE=failure), timeout=15)
                calls = (base / 'calls.txt').read_text()
                self.assertIn('product new longhorn-gate', calls)
                self.assertIn('fixture relay-clean', calls)
                self.assertEqual(control.read_text(), 'original-config')
                if failure:
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertIn('fixture archive-core', calls)
                    self.assertIn('wipe --rocky-only', calls)
                    self.assertNotIn('LONGHORN_CORE_PASS', result.stdout)
                    if failure == 'download': self.assertNotIn('product setup', calls)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('fixture verify-core', calls)
                    self.assertNotIn('wipe --rocky-only', calls)
                    self.assertIn('LONGHORN_CORE_SCOPED_CLEAN_PASS', result.stdout)
                    (base / 'clusters/longhorn-gate').rmdir()
                (base / 'calls.txt').unlink()

    def test_disk_qualification_always_cleans_and_never_wipes_healthy_core(self):
        import json
        import os
        import sys
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        branch = live.split('  disk-qualification)\n', 1)[1].split('\n    ;;', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '.venv/bin').mkdir(parents=True)
            (base / '.venv/bin/python').symlink_to(sys.executable)
            (base / 'tests/helpers').mkdir(parents=True)
            (base / 'tests/helpers/ceph-host-probe.sh').write_text('touch "$CEPH_STATE_DIR/disk-allowlist"\n')
            (base / 'tests/helpers/longhorn-lab.py').write_text('''import json,os,pathlib,sys
mode=sys.argv[1];p=pathlib.Path('calls.json')
calls=json.loads(p.read_text()) if p.exists() else []
calls.append(mode);p.write_text(json.dumps(calls))
if mode=='recover-owned-disk-residue': print('{"recovered": false}')
if mode==os.environ.get('FAIL_MODE'):sys.exit(17)
''')
            branch = branch.replace('/var/tmp/kubeauto-longhorn-disks.', str(base / 'work.'))
            for failure in ('', 'prepare-disks', 'cleanup-disks'):
                result = subprocess.run(['bash', '-c', 'set -Eeuo pipefail\nBASE="$1"\n'+branch,
                                         'contract', str(base)], text=True, capture_output=True,
                                        env=dict(os.environ, FAIL_MODE=failure), timeout=10)
                calls = json.loads((base / 'calls.json').read_text())
                self.assertEqual(calls, ['verify-core', 'recover-owned-disk-residue', 'prepare-disks', 'cleanup-disks', 'verify-core'] +
                                 ([] if failure else ['record-disk-qualification']))
                self.assertNotIn('lab-wipe-nodes', branch)
                if failure:
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertNotIn('LONGHORN_DISK_QUALIFICATION_PASS', result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('LONGHORN_DISK_QUALIFICATION_SCOPED_CLEAN_PASS', result.stdout)
                (base / 'calls.json').unlink()

    def test_install_preserves_first_failure_and_stops_before_dependent_steps(self):
        import os
        import sys
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        branch = live.split('  install)\n', 1)[1].split('\n    ;;', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / '.venv/bin').mkdir(parents=True)
            (base / '.venv/bin/python').symlink_to(sys.executable)
            (base / 'tests/helpers').mkdir(parents=True)
            (base / 'tests/helpers/ceph-host-probe.sh').write_text('touch "$CEPH_STATE_DIR/disk-allowlist"\n')
            (base / 'kube-bin').mkdir()
            kubectl = base / 'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            (base / 'tests/helpers/longhorn-lab.py').write_text('''import os,pathlib,signal,sys,time
mode=sys.argv[1]
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
if mode=='supervise-core':mode=sys.argv[sys.argv.index('--step')+1]
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
time.sleep(.02)
if mode==os.environ.get('FAIL_MODE'):sys.exit(17)
''')
            control = base / 'control-config'; control.write_text('original')
            branch = branch.replace('/root/.kube/config', str(control)).replace('/var/tmp/kubeauto-longhorn-install.', str(base / 'work.'))
            stages = ['longhorn-download', 'prepare-disks', 'credentials', 'longhorn-setup', 'business']
            for failure in ('', *stages):
                result = subprocess.run(['bash', '-c', 'set -Eeuo pipefail\nBASE="$1"\n'+branch,
                                         'contract', str(base)], capture_output=True, text=True,
                                        env=dict(os.environ, FAIL_MODE=failure), timeout=15)
                calls = (base / 'calls.txt').read_text().splitlines()
                self.assertEqual(control.read_text(), 'original')
                self.assertEqual(calls[-1], 'relay-clean')
                if failure:
                    self.assertEqual(result.returncode, 17, result.stderr)
                    self.assertIn('business-clean', calls)
                    self.assertIn('cleanup-disks', calls)
                    self.assertNotIn('LONGHORN_INSTALL_PASS', result.stdout)
                    for skipped in stages[stages.index(failure)+1:]: self.assertNotIn(skipped, calls)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('cleanup-disks', calls)
                    self.assertNotIn('restore-hosts', calls)
                    self.assertIn('LONGHORN_INSTALL_SCOPED_CLEAN_PASS', result.stdout)
                    self.assertTrue(result.stdout.rstrip().endswith('LONGHORN_INSTALL_PASS'))
                (base / 'calls.txt').unlink()

    def test_idempotence_failure_recovers_only_probe_and_transport_and_preserves_rc(self):
        import os
        import sys
        live = (ROOT / 'tests/helpers/longhorn-live.sh').read_text()
        selector = next(line for line in live.splitlines() if line.startswith('  business-clean|idempotence|'))
        branch = live.split(selector + '\n', 1)[1].split('\n    ;;', 1)[0]
        self.assertNotIn('cleanup-disks', branch)
        self.assertNotIn('lab-wipe-nodes', branch)
        self.assertEqual(branch.count('--recover-recurring-selector'), 1)
        recovery = branch.split('if [[ "$MODE" == business-clean ]]; then', 1)[1].split('elif', 1)[0]
        self.assertIn('--recover-recurring-selector', recovery)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); (base / '.venv/bin').mkdir(parents=True)
            (base / '.venv/bin/python').symlink_to(sys.executable)
            (base / 'tests/helpers').mkdir(parents=True); (base / 'clusters/longhorn-gate').mkdir(parents=True)
            (base / 'kube-bin').mkdir()
            kubectl = base / 'kube-bin/kubectl'; kubectl.write_text('#!/bin/sh\nexit 0\n');kubectl.chmod(0o755)
            (base / 'tests/helpers/longhorn-lab.py').write_text('''import os,signal,sys,time
mode=sys.argv[1]
from pathlib import Path
ready=Path('relay-ready')
if mode=='relay-maintain':
 time.sleep(float(os.environ.get('RELAY_START_DELAY','0')))
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 ready.touch()
 while True:time.sleep(.01)
if mode not in ('health','verify-core','relay-ensure'):
 deadline=time.monotonic()+2
 while not ready.exists():
  if time.monotonic()>deadline:raise RuntimeError('fixture relay did not become ready')
  time.sleep(.005)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
time.sleep(.02)
if mode==os.environ.get('FAIL_MODE'):sys.exit(17)
if mode=='backup-store-qualify':print('LONGHORN_BACKUP_STORE_QUALIFIED clients=6 tls=verified object_readback=exact')
if mode=='backup-accounts-qualify' and os.environ.get('FAIL_MODE')!='missing-accounts-marker':
 print('LONGHORN_BACKUP_ACCOUNTS_QUALIFIED accounts=2 policy=bucket-only authorization_refusals=4 revoked_key_refusals=1 sha256='+'a'*64)
 print('LONGHORN_BACKUP_ACCOUNTS_CLEAN_PASS')
if mode=='business-backup':print('LONGHORN_BACKUP_BUSINESS_PASS')
if mode=='business-backup-refusal':print('LONGHORN_BACKUP_REFUSAL_BUSINESS_PASS')
if mode in ('business-encryption','business-encryption-block','business-encryption-backup'):
 print('LONGHORN_'+mode.removeprefix('business-').upper().replace('-','_')+'_BUSINESS_PASS')
if mode in ('business-network-policy','business-security') and os.environ.get('FAIL_MODE')!='missing-business-marker':
 print('LONGHORN_'+mode.removeprefix('business-').upper().replace('-','_')+'_BUSINESS_PASS')
if mode=='recurring-declarations-qualify':print('LONGHORN_RECURRING_DECLARATIONS_QUALIFIED identity=preserved schedule=updated removal=native')
if mode in ('business-recurring-snapshot','business-recurring-backup','business-recurring-selector','business-recurring-rotation') and os.environ.get('FAIL_MODE')!='missing-business-marker':
 prefix='LONGHORN_'+mode.removeprefix('business-').upper().replace('-','_')
 print(prefix+'_BUSINESS_PASS')
 if os.environ.get('FAIL_MODE')!='missing-recurring-stop-marker':print(('LONGHORN_RECURRING_SNAPSHOT' if mode=='business-recurring-selector' else 'LONGHORN_RECURRING_BACKUP' if mode=='business-recurring-rotation' else prefix)+'_SCHEDULES_STOPPED_PASS')
 if os.environ.get('FAIL_MODE')!='missing-recurring-clean-marker':print(prefix+'_CLEAN_PASS')
 if mode=='business-recurring-rotation' and os.environ.get('FAIL_MODE')!='missing-credentials-marker':print('LONGHORN_RECURRING_CREDENTIALS_CLEAN_PASS')
if mode=='business-ownership-request':
 markers=['LONGHORN_OWNERSHIP_REQUEST_REFUSAL_VERIFIED','LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS','LONGHORN_OWNERSHIP_REQUEST_BUSINESS_PASS', 'LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=worker-a,worker-b,worker-c']
 for scenario in ('foreign','immutable'):
  markers+=['LONGHORN_OWNERSHIP_REQUEST_PRODUCT_REFUSAL_VERIFIED scenario='+scenario,'LONGHORN_OWNERSHIP_REQUEST_SNAPSHOT_BOUND_FOR_CLEANUP scenario='+scenario]
 for marker in markers:
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='ownership-controller-refusal-qualification':
 for marker in ('LONGHORN_OWNERSHIP_CONTROLLER_PRODUCT_REFUSAL_VERIFIED','LONGHORN_OWNERSHIP_CONTROLLER_CANARY_CLEAN_PASS','LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='ownership-class-refusal-qualification':
 for marker in ('LONGHORN_OWNERSHIP_CLASS_PRODUCT_REFUSAL_VERIFIED','LONGHORN_OWNERSHIP_CLASS_CANARY_CLEAN_PASS','LONGHORN_OWNERSHIP_CLASS_REFUSAL_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='ownership-interruption-qualification':
 for signal_name,rc in (('term',143),('kill',-9)):
  if os.environ.get('FAIL_MODE')!='missing-interruption-'+signal_name:print('LONGHORN_OWNERSHIP_INTERRUPTION_OBSERVED signal='+signal_name+' rc='+str(rc)+' uid=original-uid')
 if os.environ.get('FAIL_MODE')!='missing-interruption-business':print('LONGHORN_OWNERSHIP_INTERRUPTION_BUSINESS_PASS')
if mode=='ownership-preinstall-refusal-qualification':
 for scenario in ('namespace','release','crd'):
  for prefix in ('LONGHORN_OWNERSHIP_PREINSTALL_CANARY_CLEAN_PASS','LONGHORN_OWNERSHIP_PREINSTALL_PRODUCT_REFUSAL_VERIFIED'):
   marker=prefix+' scenario='+scenario
   if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
 if os.environ.get('FAIL_MODE')!='missing-LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_BUSINESS_PASS':print('LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_BUSINESS_PASS')
if mode=='ownership-csi-refusal-qualification':
 for marker in ('LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS','LONGHORN_OWNERSHIP_CSI_PRODUCT_REFUSAL_VERIFIED','LONGHORN_OWNERSHIP_CSI_REFUSAL_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='ownership-csi-probe':
 for marker in ('LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS','LONGHORN_OWNERSHIP_CSI_PROBE_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='preflight':print('LONGHORN_PREFLIGHT_PASS')
if mode=='node-bootstrap-dry-run':print('LONGHORN_NODE_BOOTSTRAP_DRY_RUN_PASS nodes=6 patches=5')
if mode=='business-uninstall':
 for marker in ('LONGHORN_UNINSTALL_REFUSAL_BOUND_PASS', 'LONGHORN_UNINSTALL_REFUSAL_RELEASED_PASS', 'LONGHORN_RETAIN_REBIND_VERIFIED', 'LONGHORN_UNINSTALL_REFUSAL_BUSINESS_PASS', 'LONGHORN_UNINSTALL_BACKUP_RETENTION_BUSINESS_PASS', 'LONGHORN_NATIVE_UNINSTALL_RECOVERY_VERIFIED', 'LONGHORN_UNINSTALL_EXTERNAL_BACKUP_PRESERVED_PASS', 'LONGHORN_UNINSTALL_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='business-uninstall-backup-qualification':
 for marker in ('LONGHORN_UNINSTALL_BACKUP_TARGET_DISCONNECTED_PASS','LONGHORN_UNINSTALL_BACKUP_RETENTION_BUSINESS_PASS','LONGHORN_UNINSTALL_BACKUP_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
if mode=='business-uninstall-refusal-qualification':
 for marker in ('LONGHORN_UNINSTALL_REFUSAL_BOUND_PASS','LONGHORN_UNINSTALL_REFUSAL_RELEASED_PASS','LONGHORN_RETAIN_REBIND_VERIFIED','LONGHORN_UNINSTALL_REFUSAL_BUSINESS_PASS'):
  if os.environ.get('FAIL_MODE')!='missing-'+marker:print(marker)
''')
            (base / 'roles/longhorn/files').mkdir(parents=True)
            (base / 'roles/longhorn/files/longhorn.py').symlink_to(base / 'tests/helpers/longhorn-lab.py')
            control = base / 'control-config'; control.write_text('original')
            branch = branch.replace('/root/.kube/config', str(control)).replace('/var/tmp/kubeauto-longhorn-operation.', str(base / 'work.'))
            for mode, failure in (('ownership-request-refusal-qualification', ''),
                                  ('ownership-request-refusal-qualification', 'business-ownership-request'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_REQUEST_PRODUCT_REFUSAL_VERIFIED scenario=foreign'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_REQUEST_SNAPSHOT_BOUND_FOR_CLEANUP scenario=foreign'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_REQUEST_PRODUCT_REFUSAL_VERIFIED scenario=immutable'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_REQUEST_SNAPSHOT_BOUND_FOR_CLEANUP scenario=immutable'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_REQUEST_REFUSAL_VERIFIED'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_REQUEST_BUSINESS_PASS'),
                                  ('ownership-request-refusal-qualification', 'missing-LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=9437184 sha256='+'a'*64+' replica_nodes=worker-a,worker-b,worker-c'),
                                  ('ownership-request-refusal-qualification', 'relay-clean'),
                                  ('ownership-controller-refusal-qualification', ''),
                                  ('ownership-controller-refusal-qualification', 'ownership-controller-refusal-qualification'),
                                  ('ownership-controller-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CONTROLLER_PRODUCT_REFUSAL_VERIFIED'),
                                  ('ownership-controller-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CONTROLLER_CANARY_CLEAN_PASS'),
                                  ('ownership-controller-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CONTROLLER_REFUSAL_BUSINESS_PASS'),
                                  ('ownership-controller-refusal-qualification', 'relay-clean'),
                                  ('ownership-class-refusal-qualification', ''),
                                  ('ownership-class-refusal-qualification', 'ownership-class-refusal-qualification'),
                                  ('ownership-class-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CLASS_PRODUCT_REFUSAL_VERIFIED'),
                                  ('ownership-class-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CLASS_CANARY_CLEAN_PASS'),
                                  ('ownership-class-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CLASS_REFUSAL_BUSINESS_PASS'),
                                  ('ownership-class-refusal-qualification', 'relay-clean'),
                                  ('ownership-interruption-qualification', ''),
                                  ('ownership-interruption-qualification', 'ownership-interruption-qualification'),
                                  ('ownership-interruption-qualification', 'missing-interruption-term'),
                                  ('ownership-interruption-qualification', 'missing-interruption-kill'),
                                  ('ownership-interruption-qualification', 'missing-interruption-business'),
                                  ('ownership-preinstall-refusal-qualification', ''),
                                  ('ownership-preinstall-refusal-qualification', 'ownership-preinstall-refusal-qualification'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_CANARY_CLEAN_PASS scenario=namespace'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_PRODUCT_REFUSAL_VERIFIED scenario=namespace'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_CANARY_CLEAN_PASS scenario=release'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_PRODUCT_REFUSAL_VERIFIED scenario=release'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_CANARY_CLEAN_PASS scenario=crd'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_PRODUCT_REFUSAL_VERIFIED scenario=crd'),
                                  ('ownership-preinstall-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_PREINSTALL_REFUSAL_BUSINESS_PASS'),
                                  ('ownership-csi-refusal-qualification', ''),
                                  ('ownership-csi-refusal-qualification', 'ownership-csi-refusal-qualification'),
                                  ('ownership-csi-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS'),
                                  ('ownership-csi-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CSI_PRODUCT_REFUSAL_VERIFIED'),
                                  ('ownership-csi-refusal-qualification', 'missing-LONGHORN_OWNERSHIP_CSI_REFUSAL_BUSINESS_PASS'),
                                  ('ownership-csi-refusal-qualification', 'relay-clean'),
                                  ('ownership-csi-probe', ''), ('ownership-csi-probe', 'ownership-csi-probe'),
                                  ('ownership-csi-probe', 'missing-LONGHORN_OWNERSHIP_CSI_CANARY_CLEAN_PASS'),
                                  ('ownership-csi-probe', 'missing-LONGHORN_OWNERSHIP_CSI_PROBE_BUSINESS_PASS'),
                                  ('ownership-csi-probe', 'relay-clean'), ('business-clean', ''), ('business-clean', 'business-clean'), ('business-clean', 'backup-store-clean'),
                                  ('uninstall', ''),
                                  ('uninstall', 'business-uninstall'),
                                  ('uninstall', 'backup-store-clean'),
                                  ('uninstall', 'relay-clean'),
                                  ('uninstall', 'missing-LONGHORN_UNINSTALL_REFUSAL_BOUND_PASS'),
                                  ('uninstall', 'missing-LONGHORN_UNINSTALL_REFUSAL_RELEASED_PASS'),
                                  ('uninstall', 'missing-LONGHORN_RETAIN_REBIND_VERIFIED'),
                                  ('uninstall', 'missing-LONGHORN_UNINSTALL_REFUSAL_BUSINESS_PASS'),
                                  ('uninstall', 'missing-LONGHORN_UNINSTALL_BACKUP_RETENTION_BUSINESS_PASS'),
                                  ('uninstall', 'missing-LONGHORN_NATIVE_UNINSTALL_RECOVERY_VERIFIED'),
                                  ('uninstall', 'missing-LONGHORN_UNINSTALL_EXTERNAL_BACKUP_PRESERVED_PASS'),
                                  ('uninstall', 'missing-LONGHORN_UNINSTALL_BUSINESS_PASS'),
                                  ('uninstall-backup-qualification', ''),
                                  ('uninstall-backup-qualification', 'business-uninstall-backup-qualification'),
                                  ('uninstall-backup-qualification', 'missing-LONGHORN_UNINSTALL_BACKUP_TARGET_DISCONNECTED_PASS'),
                                  ('uninstall-backup-qualification', 'missing-LONGHORN_UNINSTALL_BACKUP_RETENTION_BUSINESS_PASS'),
                                  ('uninstall-backup-qualification', 'missing-LONGHORN_UNINSTALL_BACKUP_BUSINESS_PASS'),
                                  ('uninstall-backup-qualification', 'backup-store-clean'),
                                  ('uninstall-backup-qualification', 'relay-clean'),
                                  ('uninstall-refusal-qualification', ''),
                                  ('uninstall-refusal-qualification', 'business-uninstall-refusal-qualification'),
                                  ('uninstall-refusal-qualification', 'missing-LONGHORN_UNINSTALL_REFUSAL_BOUND_PASS'),
                                  ('uninstall-refusal-qualification', 'missing-LONGHORN_UNINSTALL_REFUSAL_RELEASED_PASS'),
                                  ('uninstall-refusal-qualification', 'missing-LONGHORN_RETAIN_REBIND_VERIFIED'),
                                  ('uninstall-refusal-qualification', 'missing-LONGHORN_UNINSTALL_REFUSAL_BUSINESS_PASS'),
                                  ('uninstall-refusal-qualification', 'relay-clean'),
                                  ('recurring-declarations-qualification', ''),
                                  ('recurring-declarations-qualification', 'recurring-declarations-qualify'),
                                  ('recurring-declarations-qualification', 'recurring-declarations-clean'),
                                  ('backup-accounts-qualification', ''),
                                  ('backup-accounts-qualification', 'backup-accounts-qualify'),
                                  ('backup-accounts-qualification', 'backup-store-clean'),
                                  ('backup-accounts-qualification', 'missing-accounts-marker'),
                                  ('recurring-rotation-qualification', ''),
                                  ('recurring-rotation-qualification', 'business-recurring-rotation'),
                                  ('recurring-rotation-qualification', 'missing-business-marker'),
                                  ('recurring-rotation-qualification', 'missing-recurring-clean-marker'),
                                  ('recurring-rotation-qualification', 'missing-recurring-stop-marker'),
                                  ('recurring-rotation-qualification', 'missing-credentials-marker'),
                                  ('recurring-rotation-qualification', 'backup-store-clean'),
                                  ('recurring-rotation-qualification', 'relay-clean'),
                                  ('recurring', ''),
                                  ('recurring', 'business-recurring-snapshot'),
                                  ('recurring', 'business-recurring-backup'),
                                  ('recurring', 'business-recurring-rotation'),
                                  ('recurring', 'missing-business-marker'),
                                  ('recurring', 'missing-recurring-clean-marker'),
                                  ('recurring', 'missing-recurring-stop-marker'),
                                  ('recurring', 'missing-credentials-marker'),
                                  ('recurring', 'backup-store-clean'),
                                  ('recurring', 'relay-clean'),
                                  ('recurring-backup-qualification', ''),
                                  ('recurring-backup-qualification', 'business-recurring-backup'),
                                  ('recurring-backup-qualification', 'missing-business-marker'),
                                  ('recurring-backup-qualification', 'missing-recurring-clean-marker'),
                                  ('recurring-backup-qualification', 'missing-recurring-stop-marker'),
                                  ('recurring-backup-qualification', 'backup-store-clean'),
                                  ('recurring-backup-qualification', 'relay-clean'),
                                  ('recurring-selector-qualification', ''),
                                  ('recurring-selector-qualification', 'business-recurring-selector'),
                                  ('recurring-selector-qualification', 'missing-business-marker'),
                                  ('recurring-selector-qualification', 'missing-recurring-clean-marker'),
                                  ('recurring-selector-qualification', 'missing-recurring-stop-marker'),
                                  ('recurring-selector-qualification', 'relay-clean'),
                                  ('recurring-snapshot-qualification', ''),
                                  ('recurring-snapshot-qualification', 'business-recurring-snapshot'),
                                  ('recurring-snapshot-qualification', 'missing-business-marker'),
                                  ('recurring-snapshot-qualification', 'missing-recurring-clean-marker'),
                                  ('recurring-snapshot-qualification', 'missing-recurring-stop-marker'),
                                  ('recurring-snapshot-qualification', 'relay-clean'),
                                  ('backup', ''), ('backup', 'business-backup'), ('backup', 'backup-store-clean'),
                                  ('backup-refusal', ''), ('backup-refusal', 'business-backup-refusal'),
                                  ('backup-refusal', 'backup-store-clean'),
                                  ('trim', ''), ('trim', 'business-trim'), ('trim', 'supervise-core'), ('trim', 'relay-clean'),
                                  ('engine-failure', ''), ('engine-failure', 'business-engine-failure'),
                                  ('engine-failure', 'supervise-core'), ('engine-failure', 'relay-clean'),
                                  ('encryption', ''), ('encryption', 'business-encryption'),
                                  ('encryption', 'supervise-core'), ('encryption', 'backup-store-clean'), ('encryption', 'relay-clean'),
                                  ('encryption-block', ''), ('encryption-block', 'business-encryption-block'),
                                  ('encryption-block', 'relay-clean'),
                                  ('encryption-backup', ''), ('encryption-backup', 'business-encryption-backup'),
                                  ('encryption-backup', 'backup-store-clean'), ('encryption-backup', 'relay-clean'),
                                  ('network-policy', ''), ('network-policy', 'business-network-policy'),
                                  ('network-policy', 'supervise-core'), ('network-policy', 'relay-clean'),
                                  ('network-policy', 'missing-business-marker'),
                                  ('security', ''), ('security', 'business-security'),
                                  ('security', 'supervise-core'), ('security', 'relay-clean'),
                                  ('security', 'missing-business-marker'),
                                  ('idempotence', ''), ('idempotence', 'business-idempotence'),
                                  ('rwo', ''), ('rwo', 'business-rwo'), ('rwx', ''), ('rwx', 'business-rwx'),
                                  ('rwx', 'supervise-core'), ('replica-failure', ''), ('replica-failure', 'business-replica-failure'),
                                  ('replica-failure', 'supervise-core'), ('replica-scale', ''), ('replica-scale', 'business-replica-scale'),
                                  ('performance', ''), ('performance', 'business-performance'), ('performance', 'supervise-core'),
                                  ('replica-scale', 'supervise-core'), ('clone', ''), ('clone', 'business-clone'),
                                  ('expansion', ''), ('expansion', 'business-expansion'), ('expansion', 'supervise-core'), ('snapshot', ''), ('snapshot', 'business-snapshot'), ('core-smoke', ''), ('core-smoke', 'business-core-smoke'), ('reclaim', ''), ('reclaim', 'business-reclaim'), ('reclaim', 'supervise-core'), ('recover', ''), ('recover', 'business-recover'), ('cache-qualification', ''), ('cache-qualification', 'cache'), ('cache-qualification', 'supervise-core'), ('backup-store-qualification', ''), ('backup-store-qualification', 'backup-store-qualify'), ('preflight-qualification', ''), ('preflight-qualification', 'preflight'), ('preflight-qualification', 'node-bootstrap-dry-run')):
                # Cleanup must see a relay whose TERM handler is installed, even
                # when the fixture interpreter starts after a short business case.
                (base / 'relay-ready').unlink(missing_ok=True)
                delay = '.2' if (mode, failure) == ('trim', 'relay-clean') else '0'
                result = subprocess.run(['bash', '-c', 'set -Eeuo pipefail\nMODE="$2"\nBASE="$1"\n'+branch,
                                        'contract', str(base), mode], text=True, capture_output=True,
                                        env=dict(os.environ, FAIL_MODE=failure, RELAY_START_DELAY=delay), timeout=15)
                calls = (base / 'calls.txt').read_text().splitlines()
                expected_rc = 1 if failure.startswith('missing-') else 17 if failure else 0
                self.assertEqual(result.returncode, expected_rc, (mode, failure, result.stderr))
                self.assertEqual(control.read_text(), 'original')
                self.assertEqual(calls[-2:], ['relay-clean', 'verify-core' if mode in ('recover', 'core-smoke', 'cache-qualification', 'preflight-qualification', 'uninstall', 'ownership-csi-probe', 'ownership-csi-refusal-qualification', 'ownership-preinstall-refusal-qualification') else 'health'])
                self.assertEqual('business-clean' in calls, mode == 'business-clean' or bool(failure) and failure not in ('recurring-declarations-clean', 'backup-store-clean', 'relay-clean'))
                self.assertEqual('LONGHORN_' + mode.upper().replace('-', '_') + '_PASS' in result.stdout, not failure)
                if mode not in ('ownership-request-refusal-qualification', 'ownership-controller-refusal-qualification', 'ownership-class-refusal-qualification', 'ownership-interruption-qualification', 'ownership-preinstall-refusal-qualification', 'ownership-csi-refusal-qualification', 'ownership-csi-probe', 'business-clean', 'idempotence', 'recover', 'core-smoke', 'preflight-qualification', 'backup-store-qualification', 'backup-accounts-qualification', 'recurring-declarations-qualification', 'recurring-snapshot-qualification', 'recurring-selector-qualification', 'recurring-backup-qualification', 'recurring-rotation-qualification', 'recurring', 'uninstall-refusal-qualification', 'uninstall-backup-qualification', 'uninstall', 'backup', 'backup-refusal', 'encryption-block', 'encryption-backup') and failure != 'cache':
                    self.assertIn('supervise-core', calls)
                else: self.assertNotIn('supervise-core', calls)
                if failure == 'supervise-core':
                    self.assertNotIn('business-rwx', calls, 'failed product prerequisite must stop business assertions')
                if mode in ('ownership-csi-probe', 'ownership-csi-refusal-qualification', 'ownership-preinstall-refusal-qualification'):
                    self.assertEqual(calls[0], 'verify-core')
                    self.assertIn(mode, calls)
                    for forbidden in ('prepare-disks', 'credentials', 'supervise-core'):
                        self.assertNotIn(forbidden, calls)
                if mode == 'cache-qualification':
                    self.assertFalse(any(c.startswith('business-') and c != 'business-clean' for c in calls))
                    self.assertNotIn('prepare-disks', calls)
                    self.assertNotIn('credentials', calls)
                    if not failure: self.assertEqual(calls.count('cache'), 2)
                if mode == 'backup-accounts-qualification':
                    self.assertIn('backup-store-clean', calls)
                    self.assertIn('backup-accounts-qualify', calls)
                    self.assertNotIn('supervise-core', calls)
                    self.assertFalse(any(c.startswith('business-') and c != 'business-clean' for c in calls))
                if mode == 'backup-store-qualification':
                    self.assertIn('backup-store-clean', calls)
                    self.assertIn('backup-store-qualify', calls)
                    self.assertNotIn('supervise-core', calls)
                if mode in ('recurring-rotation-qualification', 'recurring'):
                    self.assertIn('backup-store-clean', calls)
                    self.assertNotIn('supervise-core', calls)
                    if mode == 'recurring-rotation-qualification':
                        self.assertIn('business-recurring-rotation', calls)
                    else:
                        ordered = ['business-recurring-' + stage for stage in ('snapshot', 'backup', 'rotation')]
                        if not failure or failure in ('backup-store-clean', 'relay-clean', 'missing-credentials-marker'):
                            self.assertEqual([call for call in calls if call in ordered], ordered)
                        elif failure in ordered:
                            self.assertEqual([call for call in calls if call in ordered], ordered[:ordered.index(failure)+1])
                        else:
                            self.assertEqual([call for call in calls if call in ordered], ordered[:1])
                if mode == 'recurring-backup-qualification':
                    self.assertIn('business-recurring-backup', calls)
                    self.assertIn('backup-store-clean', calls)
                    self.assertNotIn('supervise-core', calls)
                if mode == 'recurring-selector-qualification':
                    self.assertIn('business-recurring-selector', calls)
                    self.assertNotIn('backup-store-clean', calls)
                    self.assertNotIn('supervise-core', calls)
                if mode == 'recurring-snapshot-qualification':
                    self.assertIn('business-recurring-snapshot', calls)
                    self.assertNotIn('backup-store-clean', calls)
                    self.assertNotIn('supervise-core', calls)
                if mode in ('backup', 'backup-refusal', 'encryption', 'encryption-backup'):
                    self.assertIn('backup-store-clean', calls)
                    self.assertEqual('business-' + mode in calls, failure != 'supervise-core')
                    self.assertEqual('supervise-core' in calls, mode == 'encryption')
                if mode == 'encryption-block':
                    self.assertIn('business-encryption-block', calls)
                    self.assertNotIn('supervise-core', calls)
                    self.assertNotIn('backup-store-clean', calls)
                if mode in ('network-policy', 'security'):
                    self.assertNotIn('backup-store-clean', calls)
                    self.assertEqual('business-' + mode in calls, failure != 'supervise-core')
                if mode == 'uninstall':
                    self.assertIn('business-uninstall', calls)
                    self.assertNotIn('supervise-core', calls)
                    self.assertIn('backup-store-clean', calls)
                    self.assertEqual(calls[0], 'health', 'native uninstall requires healthy storage before teardown')
                if mode == 'uninstall-backup-qualification':
                    self.assertIn('business-uninstall-backup-qualification', calls)
                    self.assertNotIn('supervise-core', calls)
                    self.assertIn('backup-store-clean', calls)
                if mode == 'uninstall-refusal-qualification':
                    self.assertIn('business-uninstall-refusal-qualification', calls)
                    self.assertNotIn('supervise-core', calls)
                    self.assertNotIn('backup-store-clean', calls)
                if mode == 'business-clean':
                    self.assertNotIn('supervise-core', calls)
                    self.assertFalse(any(c.startswith('business-') and c != 'business-clean' for c in calls))
                    if failure != 'business-clean':
                        self.assertLess(calls.index('business-clean'), calls.index('backup-store-clean'))
                if mode == 'recurring-declarations-qualification':
                    self.assertIn('recurring-declarations-clean', calls)
                    self.assertIn('recurring-declarations-qualify', calls)
                    self.assertNotIn('supervise-core', calls)
                    self.assertFalse(any(c.startswith('business-') and c != 'business-clean' for c in calls))
                if mode == 'preflight-qualification':
                    self.assertIn('preflight', calls)
                    if failure != 'preflight': self.assertIn('node-bootstrap-dry-run', calls)
                    self.assertFalse(any(c.startswith('business-') and c != 'business-clean' for c in calls))
                (base / 'calls.txt').unlink()

    def test_tls_recovery_checks_core_before_restore_and_storage_health_afterwards(self):
        import os
        import sys
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        selector = next(line for line in live.splitlines() if line.startswith('  business-clean|idempotence|'))
        branch = live.split(selector+'\n', 1)[1].split('\n    ;;', 1)[0]
        for record_kind in ('file', 'symlink'):
            with self.subTest(record_kind=record_kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); (base/'.venv/bin').mkdir(parents=True)
                (base/'.venv/bin/python').symlink_to(sys.executable)
                (base/'tests/helpers').mkdir(parents=True); (base/'clusters/longhorn-gate').mkdir(parents=True)
                record = base/'clusters/longhorn-gate/longhorn-security-tls.json'
                if record_kind == 'symlink': record.symlink_to(base/'absent-escrow')
                else: record.touch()
                (base/'tests/helpers/longhorn-lab.py').write_text('''import signal,sys,time
from pathlib import Path
mode=sys.argv[1]; record=Path('clusters/longhorn-gate/longhorn-security-tls.json')
if mode=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 while True:time.sleep(.01)
with open('calls.txt','a') as stream:stream.write(mode+'\\n')
if mode=='health' and (record.exists() or record.is_symlink()):sys.exit(17)
if mode=='business-clean':record.unlink(missing_ok=True)
''')
                control = base/'control-config'; control.write_text('original')
                script = branch.replace('/root/.kube/config', str(control)).replace('/var/tmp/kubeauto-longhorn-operation.', str(base/'work.'))
                result = subprocess.run(['bash', '-c', 'set -Eeuo pipefail\nMODE=business-clean\nBASE="$1"\n'+script,
                                         'contract', str(base)], text=True, capture_output=True, env=os.environ, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (base/'calls.txt').read_text().splitlines()
                self.assertEqual(calls[0], 'verify-core')
                self.assertLess(calls.index('business-clean'), calls.index('health'))
                self.assertEqual(calls[-1], 'health')
                self.assertIn('LONGHORN_BUSINESS_CLEAN_PASS', result.stdout)
                self.assertEqual(control.read_text(), 'original')

    def test_remote_summary_counts_longhorn_failure_even_when_rc_zero(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        function = 'remote_job_summary()' + runner.split('remote_job_summary()', 1)[1].split('remote_log_tail()', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); prefix = root / 'gate'
            for suffix in ('pid', 'exit', 'finalized'): prefix.with_suffix('.' + suffix).write_text('0')
            log = root / 'gate.log'
            log.write_text('LONGHORN_POOL_PROBE_PASS\nLONGHORN_PRODUCT_FAIL reason=canary\nLONGHORN_FIRST_FAILURE phase=canary\nLONGHORN_V2_RESOURCES_FAIL reason=canary\nLONGHORN_V2_RESOURCES_FAIL reason=cleanup\n')
            script = function + '\nlocal_ssh() { bash -c "$1"; }\nremote_job_summary local_ssh "" "$1/gate" "$1/gate.log"\n'
            result = subprocess.run(['bash', '-c', script, 'contract', directory], capture_output=True, text=True, check=True)
            self.assertIn('failure_markers=4', result.stdout)

    def test_local_summary_rejects_digit_bearing_failure_markers_even_with_rc_zero(self):
        import json
        script = (ROOT/'tests/helpers/longhorn-runner.sh').read_text().split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'gate.log').write_text('LONGHORN_STATIC_PASS\nLONGHORN_V2_RESOURCES_FAIL reason=canary\nLONGHORN_V2_RESOURCES_FAIL reason=cleanup\nLONGHORN_FIRST_FAILURE phase=canary\nLONGHORN_CLEANUP_FAIL phase=canary\n')
            result = subprocess.run(['python3', '-c', script, directory, '0', 'LONGHORN_STATIC_PASS', 'static'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, '')
            self.assertEqual(json.loads((path/'status.json').read_text())['failure_markers'], 4)

    def test_progress_uses_longhorn_pending_counts(self):
        runner = (ROOT / 'tests/run_enterprise_regression.sh').read_text()
        function = 'matrix_counts()' + runner.split('matrix_counts()', 1)[1].split('\n}\n', 1)[0] + '\n}\n'
        result = subprocess.run(['bash', '-c', 'ROOT="$1"; MODE=--longhorn-pool-probe\n' + function + 'matrix_counts longhorn',
                                 'contract', str(ROOT)], capture_output=True, text=True, check=True)
        cases = matrix.validate(ROOT / 'tests/longhorn-test-matrix.yaml')['cases']
        self.assertIn('matrix_pending=' + str(sum(c['status'] == 'pending' for c in cases)), result.stdout)
        self.assertIn('matrix_pass=' + str(sum(c['status'] == 'pass' for c in cases)), result.stdout)

    def test_host_preparation_never_formats_or_disables_security(self):
        text = (ROOT / 'roles/longhorn/tasks/host.yml').read_text()
        for forbidden in ('mkfs', 'wipefs', 'setenforce', 'state: stopped', 'iscsiadm --mode node --op delete'):
            self.assertNotIn(forbidden, text)
        self.assertIn('ansible_facts.python.executable', text)
        self.assertIn('RequiresMountsFor=', text)
        self.assertIn('ExecStartPre=/usr/bin/mountpoint', text)

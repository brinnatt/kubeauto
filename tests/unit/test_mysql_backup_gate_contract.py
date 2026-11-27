"""Affected-scope release contracts; never mutate a lab from these tests."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class MysqlBackupGateTests(unittest.TestCase):
    def test_pending_prerequisite_refuses_real_launcher_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('tests/run_tools_regression.sh', 'tests/tools-test-matrix.yaml',
                         'tests/helpers/validate_tools_test_matrix.py', 'tests/helpers/mysql-backups-regression.sh', 'tools-onefile.spec'):
                target = root/name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT/name, target)
            shutil.copytree(ROOT/'tools', root/'tools', ignore=shutil.ignore_patterns('__pycache__'))
            matrix_path = root/'tests/tools-test-matrix.yaml'
            matrix = yaml.safe_load(matrix_path.read_text())
            for case in matrix['test_cases']:
                if case['id'] == 'TL-MBK-03':
                    case['status'] = 'pending'
            counts = {key: sum(c['status'] == key for c in matrix['test_cases']) for key in ('pass','pending','fail','skip','na')}
            matrix['coverage_summary'].update(counts)
            matrix['coverage_summary']['overall_assessment'] = 'contract fixture'
            matrix_path.write_text(yaml.safe_dump(matrix, allow_unicode=True))
            wrapper = root/'python'
            wrapper.write_text('#!'+sys.executable+'\nimport os, sys\nif sys.argv[1:3] == ["-m", "unittest"]: sys.exit(0)\nos.execv(sys.executable,[sys.executable]+sys.argv[1:])\n')
            wrapper.chmod(0o755)
            result = subprocess.run(['bash', str(root/'tests/run_tools_regression.sh'), '--mysql-backup-live'],
                                    env=dict(os.environ, PYTHON=str(wrapper), MYSQL_BACKUP_STAGE='full'),
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('MYSQL_BACKUP_FINAL_PREREQUISITES_REFUSED', result.stderr)
            self.assertIn('TL-MBK-03', result.stderr)
            self.assertNotIn('ssh:', result.stderr)

    def test_selected_build_refuses_unrelated_or_malformed_scope_before_remote_access(self):
        for value in ('MyBackupCli,', 'MyBackupCli,StarCli', 'MyBackupCli,MyLogiBackupCli,MyBackupCli'):
            result = subprocess.run(['bash', str(ROOT/'tests/run_tools_regression.sh'), '--build-only'],
                                    env=dict(os.environ, TOOLS_SELECTED=value), capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('TOOLS_SELECTED_SCOPE_REFUSED', result.stderr)

    def test_help_delivery_selection_matches_the_ten_changed_tools_and_not_starmigrt(self):
        selection = 'CalicoPolicyCli,NetCheckCli,KafkaCli,MyBackupCli,MyLogiBackupCli,MigrationCli,StarCli,KubeBackupCli,KubePublishCli,OvpnUserCli'
        for name in ('tests/run_tools_regression.sh', 'tests/helpers/build-tools-rocky8.sh'):
            source = (ROOT/name).read_text()
            # Execute the actual preflight/selection only, with no remote setup.
            stop = '\nBUILD_SUDO=' if name.endswith('run_tools_regression.sh') else '\n[[ "$SOURCE"'
            prefix = source.split(stop, 1)[0]
            with tempfile.TemporaryDirectory() as directory:
                script = Path(directory)/'selection.sh'
                script.write_text(prefix + '\nprintf "%s\\n" "${TOOLS[@]}"\n')
                accepted = subprocess.run(['bash', str(script), '--build-only'],
                    env=dict(os.environ, TOOLS_SELECTED=selection), capture_output=True, text=True)
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                self.assertEqual(accepted.stdout.splitlines(), selection.split(','))
                for bad in (selection+',StarMigrtCli', selection+',MyBackupCli', selection+','):
                    refused = subprocess.run(['bash', str(script), '--build-only'],
                        env=dict(os.environ, TOOLS_SELECTED=bad), capture_output=True, text=True)
                    self.assertEqual(refused.returncode, 2, refused.stderr)

    def test_selected_kubernetes_tools_build_only_pinned_dependencies_and_separate_provenance(self):
        helper = (ROOT/'tests/helpers/build-tools-rocky8.sh').read_text()
        runner = (ROOT/'tests/run_tools_regression.sh').read_text()
        self.assertIn('"$TOOLS_SELECTED" == MyBackupCli,KubeBackupCli,KubePublishCli', helper)
        self.assertIn('kubernetes==35.0.0 PyYAML==6.0.3 tenacity==9.1.2', helper)
        block = runner.split("<<'PY_BUILD_PROOF'\n", 1)[1].split('\nPY_BUILD_PROOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('tools/k8stools/KubeBackupCli.py', 'tools/k8stools/KubePublishCli.py',
                         'tools-onefile.spec', 'tests/helpers/build-tools-rocky8.sh',
                         'dist/KubeBackupCli-rocky8', 'dist/KubePublishCli-rocky8'):
                path = root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('fixture')
            manifest = root/'inputs.json'; manifest.write_text('{}')
            subprocess.run([sys.executable, '-c', block, str(root), 'KubeBackupCli,KubePublishCli', str(manifest)], check=True)
            manifest.write_text('{"tools-onefile.spec":"stale"}')
            refused = subprocess.run([sys.executable, '-c', block, str(root), 'KubeBackupCli,KubePublishCli', str(manifest)], capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('TOOLS_BUILD_INPUT_CHANGED tools-onefile.spec', refused.stderr)
            self.assertTrue((root/'dist/kube-backup-build.json').is_file())
            self.assertTrue((root/'dist/kube-publish-build.json').is_file())
            self.assertFalse((root/'dist/mysql-backup-build.json').exists())

    def test_build_checksum_boundary_handles_provenance_directory(self):
        source = (ROOT/'tests/run_tools_regression.sh').read_text()
        block = source.split('    # Keep the target-built binary', 1)[1]
        block = '    # Keep the target-built binary' + block.split('    "$PY" - "$ROOT"', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); stage = root/'stage'; (stage/'provenance').mkdir(parents=True)
            (root/'dist').mkdir(); binary = stage/'MyBackupCli'; binary.write_text('artifact'); binary.chmod(0o755)
            result = subprocess.run(['bash', '-euc', 'TOOLS=(MyBackupCli)\n' + block],
                                    env=dict(os.environ, ROOT=str(root), stage=str(stage)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('MyBackupCli', result.stdout)
            self.assertNotIn('provenance', result.stdout)

    def test_build_sync_excludes_runtime_and_download_caches(self):
        runner = (ROOT/'tests/run_tools_regression.sh').read_text()
        sync = runner[runner.index('    rsync -a --delete'):runner.index('    scp ', runner.index('    rsync -a --delete'))]
        for excluded in ('--exclude .cache', '--exclude .venv', '--exclude logs', '--exclude dist', '--exclude build'):
            self.assertIn(excluded, sync)

    def test_unchanged_integration_contracts_and_loop_mount_handoff(self):
        helper = (ROOT/'tests/helpers/mysql-backups-regression.sh').read_text()
        logical = (ROOT/'tests/helpers/mylogi-backup-live-regression.sh').read_text()
        self.assertLess(helper.index('flock -n 9'), helper.index('mkdir evidence'))
        self.assertIn('mount -o loop backup.ext4', helper)
        self.assertIn('MYLOGI_OWNED_MOUNT=yes', helper)
        self.assertIn('MYLOGI_BACKUP_HOST_DIR=/tmp/kubeauto-mysql-backup-disk', helper)
        self.assertIn('"$mount_target" != "/"', logical)
        self.assertIn('for version in 8.0.46 8.4.4 9.2.0', helper)
        self.assertIn('umount /tmp/kubeauto-mysql-backup-disk', helper)
        self.assertIn('MYLOGI_NATIVE_ARCHIVE_DIAGNOSTICS_PASS', logical)

    def test_physical_service_wrapper_really_stops_and_restarts_database(self):
        source = (ROOT/'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        block = source.split('cat >"$ROOT/bin/systemctl" <<\'EOF\'\n', 1)[1].split('\nEOF', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); log=root/'docker.log'
            docker = root/'docker'
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_TRACE"\n')
            docker.chmod(0o755)
            wrapper=root/'systemctl'
            wrapper.write_text(block.replace('/tmp/tools-mbk-live/mysql.state', str(root/'state')))
            env=dict(os.environ, PATH=str(root)+':'+os.environ['PATH'], DOCKER_TRACE=str(log))
            subprocess.run(['bash', str(wrapper), 'stop'], env=env, check=True)
            self.assertIn('stop --time 30 tools-mbk-mysql', log.read_text())
            self.assertEqual((root/'state').read_text().strip(), 'inactive')
            subprocess.run(['bash', str(wrapper), 'start'], env=env, check=True)
            self.assertIn('start tools-mbk-mysql', log.read_text())
            self.assertIn('exec tools-mbk-mysql', log.read_text())
            self.assertEqual((root/'state').read_text().strip(), 'active')
        self.assertLess(source.index('DELETE FROM $MARKER.items'), source.index('echo MYBACKUP_STAGE restore;'))
        self.assertIn("grep -Fxq '0'", source)

    def test_prepare_and_copyback_do_not_require_stopped_mysql_network(self):
        source = (ROOT/'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        start=source.index('cat >"$ROOT/bin/xtrabackup" <<EOF')
        block=source[start:source.index('\nEOF', start)+4]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'bin').mkdir()
            subprocess.run(['bash', '-c', block], env=dict(os.environ, ROOT=str(root), PXB_IMAGE='fixture:version'), check=True)
            fake=root/'bin/docker'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n'); fake.chmod(0o755)
            for option in ('--backup','--prepare','--decompress','--copy-back'):
                result=subprocess.run(['bash', str(root/'bin/xtrabackup'), option],
                                      env=dict(os.environ, PATH=str(root/'bin')+':'+os.environ['PATH']),
                                      capture_output=True, text=True, check=True)
                self.assertNotIn('--network', result.stdout.splitlines())
                self.assertNotIn('tools-mbk-mysql', result.stdout.splitlines())

    def test_real_timer_branch_reaches_product_cli_and_cleans_units(self):
        source=(ROOT/'tests/helpers/mylogi-backup-live-regression.sh').read_text()
        branch=source.split('if [[ "${MYLOGI_REAL_TIMER:-no}" == yes ]]',1)[1]
        self.assertIn('systemctl start mysql-logi-backup.service', branch)
        self.assertIn('MYLOGI_MYSQLDUMP_BIN=', branch)
        self.assertIn('/tmp/MyLogiBackupCli', branch)
        self.assertIn('ExecMainStatus --value', branch)
        self.assertIn('"$TOOL" timer remove', branch)
        self.assertIn('"$timer_stamp_existed" -eq 1', branch)
        self.assertIn('rm -f /var/lib/systemd/timers/stamp-mysql-logi-backup.timer', branch)

    def test_physical_pulls_accepted_digest_even_after_tag_changes(self):
        source=(ROOT/'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        setup=source[:source.index('cleanup()')]
        result=subprocess.run(['bash','-c',setup+'\nprintf "%s\\n" "$PXB_IMAGE" "$MYSQL_IMAGE"\n'],
                              env=dict(os.environ,MYBACKUP_TOOL='/fixture/tool'),capture_output=True,text=True,check=True)
        refs=result.stdout.splitlines()
        self.assertTrue(refs[0].endswith('@sha256:6f3f3735320eb77e7bb00300b37efd9a0cc797c34e2c637a2f687c77504d94b5'))
        self.assertTrue(refs[1].endswith('@sha256:c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83'))

    def test_actual_archive_extractor_explicitly_enables_tar_wildcards(self):
        import io
        import tarfile
        source=(ROOT/'tests/helpers/mylogi-backup-live-regression.sh').read_text()
        start=source.index('archive_contains() {')
        function=source[start:source.index('\n}',start)+2]
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/'backup.tar.gz'
            with tarfile.open(archive,'w:gz') as out:
                payload=b'CREATE DATABASE `app`;\n'; member=tarfile.TarInfo('mysql-selected-20261108.sql'); member.size=len(payload)
                out.addfile(member,io.BytesIO(payload))
            good=subprocess.run(['bash','-euo','pipefail','-c',function+'\narchive_contains "$1" "CREATE DATABASE"','contract',str(archive)],capture_output=True,text=True)
            self.assertEqual(good.returncode,0,good.stderr)
            missing=subprocess.run(['bash','-euo','pipefail','-c',function+'\narchive_contains "$1" "not-present"','contract',str(archive)],capture_output=True,text=True)
            self.assertNotEqual(missing.returncode,0)

    def test_pass_status_alone_cannot_bypass_incomplete_qualification(self):
        import hashlib
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('tests/run_tools_regression.sh','tests/tools-test-matrix.yaml','tests/helpers/validate_tools_test_matrix.py','tests/helpers/mysql-backups-regression.sh','tools-onefile.spec'):
                target=root/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(ROOT/name,target)
            shutil.copytree(ROOT/'tools',root/'tools',ignore=shutil.ignore_patterns('__pycache__'))
            matrix_path=root/'tests/tools-test-matrix.yaml'; matrix=yaml.safe_load(matrix_path.read_text())
            for case in matrix['test_cases']:
                if case['status']!='na': case['status']='pass'
            matrix['coverage_summary'].update({'pass':sum(c['status']=='pass' for c in matrix['test_cases']),'pending':0,'fail':0,'overall_assessment':'contract fixture'})
            matrix['mysql_backup_requalification']['evidence']='qualification.json'
            matrix_path.write_text(yaml.safe_dump(matrix,allow_unicode=True))
            (root/'qualification.json').write_text(json.dumps({'inputs':{},'stages':{}}))
            wrapper=root/'python'; wrapper.write_text('#!'+sys.executable+'\nimport os,sys\nif sys.argv[1:3]==["-m","unittest"]: sys.exit(0)\nos.execv(sys.executable,[sys.executable]+sys.argv[1:])\n'); wrapper.chmod(0o755)
            result=subprocess.run(['bash',str(root/'tests/run_tools_regression.sh'),'--mysql-backup-live'],
                                  env=dict(os.environ,PYTHON=str(wrapper),MYSQL_BACKUP_STAGE='full'),capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('MYSQL_BACKUP_FINAL_QUALIFICATION_INCOMPLETE',result.stderr)
            self.assertNotIn('ssh:',result.stderr)

    def test_portability_real_shell_body_needs_no_find_or_wc(self):
        source = (ROOT/'tests/helpers/mysql-backups-regression.sh').read_text()
        body = source.split('--entrypoint /bin/sh "$image" -ec \'\n', 1)[1].split("\n    '", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for tool in ('MyBackupCli-rocky8', 'MyLogiBackupCli-rocky8'):
                executable = root/tool
                executable.write_text('#!/bin/sh\nprintf "help\\n"\n')
                executable.chmod(0o755)
            work = root/'work'; work.mkdir()
            script = body.replace('cd /work', 'cd "'+str(work)+'"').replace('/tools/', str(root)+'/')
            result = subprocess.run(['/bin/sh', '-ec', script], env={'PATH': '/nonexistent'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            (work/'foreign').touch()
            result = subprocess.run(['/bin/sh', '-ec', script], env={'PATH': '/nonexistent'}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

"""Fixed parallel supervisor contracts; all product fixtures are local."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('parallel', ROOT/'tests/helpers/longhorn-parallel.py')
parallel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parallel)

class ParallelContracts(unittest.TestCase):
    def manager_adoption_fixture(self, root, *, reject=False):
        # Cluster adoption is simulated; the real aggregate must invoke it
        # before the snapshot/business fixtures can consume the idle profile.
        (root/'tests/helpers/longhorn-manager-adopt.py').write_text('''import json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
assert not sys.argv[1:] and os.environ['LONGHORN_PARENT_LEASE_HELD']=='yes'
assert not (root/'manager.ready').exists()
# The pool branch starts with adoption, before the clone fixture's barrier.
(root/'v2.started').touch()
if REJECT or os.environ.get('FAILED_STAGE')=='manager-adopt':raise SystemExit(9)
(root/'manager.ready').write_text(json.dumps({'healthy':True,'empty_business':True,'profile':'v2'}))
'''.replace('REJECT', repr(reject)))

    def test_standalone_linked_aggregate_keeps_real_durable_handoff_private_under_022(self):
        for failed in ('none', 'linked-lifecycle', 'manager-adopt'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                helpers = root/'tests/helpers'; helpers.mkdir(parents=True)
                (root/'.venv/bin').mkdir(parents=True)
                (root/'.venv/bin/python').symlink_to(sys.executable)
                self.manager_adoption_fixture(root)
                for name in ('longhorn-parallel.py', 'run-durable-gate.sh'):
                    (helpers/name).write_bytes((ROOT/'tests/helpers'/name).read_bytes())
                shell = (ROOT/'tests/helpers/longhorn-v2-linked-acceptance.sh').read_text()
                shell = shell.replace('BASE=/usr/local/kubeauto', 'BASE='+str(root)).replace(
                    '/var/tmp/kubeauto-longhorn-linked-acceptance', str(root/'acceptance'))
                (helpers/'aggregate.sh').write_text(shell)
                # Only the expensive cluster behavior is a fixture. Execute the
                # real aggregate, durable writer and strict branch verifier.
                (helpers/'longhorn-live.sh').write_text('''set -euo pipefail
stage="${1#v2-}"; stage="${stage%-qualification}"
echo "$stage" >> "$BASE/order"
[[ "$stage" != "$FAILED_STAGE" ]] || exit 8
marker="LONGHORN_V2_${stage^^}_QUALIFICATION"; marker="${marker//-/_}"
echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
''')
                (helpers/'longhorn-v2-clone.py').write_text('''import json,sys
from pathlib import Path
base=Path(__file__).resolve().parents[2]
assert json.loads((base/'manager.ready').read_text())=={'healthy':True,'empty_business':True,'profile':'v2'}
args=sys.argv[1:]
if args[0]=='snapshot':
 assert args in (['snapshot','--phase','before'],['snapshot','--phase','after'])
 (base/'snapshots').open('a').write(args[-1]+'\\n')
elif args[0]=='stage-receipt':
 assert args[1]=='--directory' and args[3]=='--stage'
 root=Path(args[2]); stage=args[4]
 assert all((root/(stage+'.'+key)).read_text().strip()=='0' for key in ('exit','finalized'))
else:raise SystemExit(2)
''')
                env = dict(os.environ, BASE=str(root), FAILED_STAGE=failed, LONGHORN_PARENT_LEASE_HELD='yes')
                result = subprocess.run(['bash', '-c', 'umask 022; exec bash "$1" v2-linked-acceptance',
                                         'contract', str(helpers/'aggregate.sh')],
                                        env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, {'none':0,'linked-lifecycle':8,'manager-adopt':9}[failed], result.stdout+result.stderr)
                work = next(root.glob('acceptance.*'))
                for path in work.iterdir():
                    self.assertEqual(path.stat().st_mode & 0o077, 0, path)
                if failed=='manager-adopt':
                    self.assertFalse((root/'snapshots').exists())
                    self.assertFalse((root/'order').exists())
                    self.assertNotIn('LONGHORN_V2_LINKED_ACCEPTANCE_PASS', result.stdout)
                    continue
                parallel.verify_branch(work, 'linked-clone')
                if failed=='none':
                    for stage in ('linked-lifecycle', 'incomplete-clone'):
                        parallel.verify_branch(work, stage)
                    self.assertEqual((root/'order').read_text().splitlines(),
                                     ['linked-clone', 'linked-lifecycle', 'incomplete-clone'])
                    self.assertIn('LONGHORN_V2_LINKED_ACCEPTANCE_PASS', result.stdout)
                else:
                    self.assertEqual((work/'linked-lifecycle.exit').read_text().strip(), '8')
                    self.assertFalse((work/'incomplete-clone.log').exists())
                    self.assertNotIn('LONGHORN_V2_LINKED_ACCEPTANCE_PASS', result.stdout)

    def test_fixed_dispatch_routes_once_without_launching_the_lab(self):
        script = '''source "$1"
longhorn_launch() { printf '%s\\n' "$@"; }
longhorn_dispatch "$2"
'''
        for mode in ('v2-linked-acceptance', 'v1-operations-v2-disk-tail', 'v1-operations-v2-worker-tail', 'v1-ownership-tail-v2-failure-tail', 'v1-ownership-operations-v2-failure-platform', 'v1-ownership-v2-failure-remaining', 'v1-operations-v2-platform-install', 'v1-ownership-v2-linked-acceptance', 'v1-post-revision-backups-v2-failure-acceptance', 'v1-operations-remaining-v2-monitoring', 'v1-system-restore-v2-runtime-resources', 'v1-recovery-tail-v2-install-tail', 'v1-dr-tail-v2-install-tail', 'v1-upgrade-tail-v2-install-tail', 'v1-monitoring-v2-resource-tail', 'v1-upgrade-tail-v2-maintenance-tail', 'v1-stability-v2-observability', 'v1-v2-control-qualification', 'v1-backups-v2-linked-qualification', 'v1-v2-im-adopt-qualification', 'v2-source-target-reboot-recovery', 'v2-source-target-im-adopt-qualification', 'v2-source-target-im-revision-2-adopt-qualification', 'v2-source-target-im-revision-3-adopt-qualification'):
            with self.subTest(mode=mode):
                result = subprocess.run(['bash', '-c', script, 'contract',
                                         str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-'+mode],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.splitlines(),
                                 [mode, 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS'])

    def test_dispatch_preflight_live_route_and_static_registration_are_complete(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        static = (ROOT/'tests/helpers/longhorn-static.sh').read_text()
        module_modes = re.search(r'case "\$module" in ([^)]+)\)', runner).group(1).split('|')
        lease_modes = re.search(r'case "\$mode" in ([^)]+)\) ;; \*\) exit 2', runner).group(1).split('|')
        for mode in ('v2-linked-acceptance', 'v1-operations-v2-disk-tail', 'v1-operations-v2-worker-tail', 'v1-ownership-tail-v2-failure-tail', 'v1-ownership-operations-v2-failure-platform', 'v1-ownership-v2-failure-remaining', 'v1-operations-v2-platform-install', 'v1-ownership-v2-linked-acceptance', 'v1-post-revision-backups-v2-failure-acceptance', 'v1-operations-remaining-v2-monitoring', 'v1-system-restore-v2-runtime-resources', 'v1-recovery-tail-v2-install-tail', 'v1-dr-tail-v2-install-tail', 'v1-upgrade-tail-v2-install-tail', 'v1-monitoring-v2-resource-tail', 'v1-upgrade-tail-v2-maintenance-tail', 'v1-stability-v2-observability', 'v1-v2-control-qualification', 'v1-backups-v2-linked-qualification', 'v1-v2-im-adopt-qualification', 'v2-source-target-reboot-recovery', 'v2-source-target-im-adopt-qualification', 'v2-source-target-im-revision-2-adopt-qualification', 'v2-source-target-im-revision-3-adopt-qualification'):
            self.assertIn(mode, module_modes)
            self.assertIn(mode, lease_modes)
            if mode.startswith('v1') and mode not in ('v1-system-restore-v2-runtime-resources', 'v1-recovery-tail-v2-install-tail', 'v1-stability-v2-observability', 'v1-post-revision-backups-v2-failure-acceptance'): self.assertIn('"$module" == '+mode, runner)
        self.assertIn('longhorn-lab.py" verify-transport', runner)
        self.assertIn('v2-linked-acceptance|v2-linked-failure-acceptance)\n    exec bash "$BASE/tests/helpers/longhorn-v2-linked-acceptance.sh" "$MODE"', live)
        live_modes = re.search(r'([^\n]+)\)\n    exec bash "\$BASE/tests/helpers/longhorn-parallel.sh" "\$MODE"', live).group(1).strip().split('|')
        self.assertEqual(set(live_modes), {'v1-backup-v2-data-protection', 'v1-operations-v2-disk-tail', 'v1-operations-v2-worker-tail', 'v1-ownership-tail-v2-failure-tail', 'v1-ownership-operations-v2-failure-platform', 'v1-ownership-v2-failure-remaining', 'v1-operations-v2-platform-install', 'v1-ownership-v2-linked-acceptance', 'v1-post-revision-backups-v2-failure-acceptance', 'v1-operations-remaining-v2-monitoring', 'v1-system-restore-v2-runtime-resources', 'v1-recovery-tail-v2-install-tail', 'v1-dr-tail-v2-install-tail', 'v1-upgrade-tail-v2-install-tail', 'v1-monitoring-v2-resource-tail', 'v1-upgrade-tail-v2-maintenance-tail', 'v1-stability-v2-observability', 'v1-v2-control-qualification', 'v1-backups-v2-linked-qualification', 'v1-v2-im-adopt-qualification', 'v2-source-target-reboot-recovery', 'v2-source-target-im-adopt-qualification', 'v1-backups', 'v1-platform-v2-environment-recovery', 'v2-source-target-environment-recovery', 'v1-backups-remaining', 'v1-remaining-v2-linked-acceptance', 'v1-platform-propagation-v2-im-adopt-qualification', 'v1-backups-v2-linked-acceptance', 'v1-backups-v2-linked-failure-acceptance', 'v2-source-target-im-revision-2-adopt-qualification', 'v2-source-target-im-revision-3-adopt-qualification'})
        self.assertIn('py_compile tests/helpers/longhorn-parallel.py', static)
        self.assertIn('unittest tests.unit.test_longhorn_parallel', static)

    def test_source_target_modes_keep_independent_durable_fences_and_exact_markers(self):
        for operation in ('reboot', 'adopt', 'revision-2-adopt', 'revision-3-adopt'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for profile, prefix in (('source', 'v1'), ('target', 'v2')):
                    for suffix in ('.exit', '.finalized'):
                        path = root/(prefix+suffix); path.write_text('0\n'); path.chmod(0o600)
                    log = root/(prefix+'.log')
                    log.write_text('\n'.join(parallel.MARKERS[profile+'-'+operation])+'\n'); log.chmod(0o600)
                    self.assertEqual(parallel.verify_branch(root, profile+'-'+operation)['rc'], 0)
                (root/'v1.exit').write_text('7\n')
                with self.assertRaises(ValueError): parallel.verify_branch(root, 'source-'+operation)
                self.assertEqual(parallel.verify_branch(root, 'target-'+operation)['rc'], 0)

    def test_standalone_v1_backup_batch_is_public_and_keeps_explicit_context(self):
        script = 'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch --longhorn-v1-backups'
        result = subprocess.run(['bash', '-c', script, 'contract',
                                  str(ROOT/'tests/helpers/longhorn-runner.sh')],
                                 capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ['v1-backups', 'LONGHORN_PARALLEL_V1_BACKUPS_PASS'])
        source = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        self.assertIn('"$module" == v1-backups ||', source)

    def test_context_is_read_only_and_does_not_return_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config'
            self.assertEqual(parallel.context(path), {'exists': False})
            path.write_text('PRIVATE-CREDENTIAL')
            before = path.stat()
            record = Path(directory)/'record.json'
            record.write_text(json.dumps(parallel.context(path)))
            record.chmod(0o600)
            parallel.verify_context(record, path)
            self.assertNotIn('PRIVATE', json.dumps(parallel.context(path)))
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            path.write_text('unexpected-context')
            with self.assertRaisesRegex(ValueError, 'changed'):
                parallel.verify_context(record, path)
            path.unlink()
            path.symlink_to(record)
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                parallel.context(path)

    def terminal_fixture(self, directory):
        root = Path(directory)
        for suffix in ('.exit', '.finalized'):
            path = root/('v1'+suffix)
            path.write_text('0\n')
            path.chmod(0o600)
        log = root/'v1.log'
        log.write_text('\n'.join(parallel.MARKERS['v1'])+'\n')
        log.chmod(0o600)
        return root, log

    def test_success_requires_both_durable_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            root, log = self.terminal_fixture(directory)
            self.assertEqual(parallel.verify_branch(root, 'v1')['log_sha256'], hashlib.sha256(log.read_bytes()).hexdigest())
            (root/'v1.finalized').unlink()
            with self.assertRaises(FileNotFoundError):
                parallel.verify_branch(root, 'v1')

    def test_nonzero_failure_duplicate_and_missing_marker_are_rejected(self):
        for defect in ('nonzero', 'failure', 'duplicate', 'missing'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                root, log = self.terminal_fixture(directory)
                if defect == 'nonzero':
                    (root/'v1.exit').write_text('7\n')
                elif defect == 'failure':
                    log.write_text(log.read_text()+'LONGHORN_FIRST_FAILURE phase=owned\n')
                elif defect == 'duplicate':
                    log.write_text(log.read_text()+parallel.MARKERS['v1'][0]+'\n')
                else:
                    log.write_text('\n'.join(parallel.MARKERS['v1'][1:])+'\n')
                with self.assertRaises(ValueError):
                    parallel.verify_branch(root, 'v1')

    def run_supervisor(self, directory, fail=False, mode='v1-v2-control-qualification',
                       missing_marker=None, v2_fail=False, cache_fail=False, sequence_defect=None, startup_delay=0,
                       launch_delay=0, resume_stability=None):
        root = Path(directory)
        helpers = root/'tests/helpers'
        helpers.mkdir(parents=True)
        (root/'.venv/bin').mkdir(parents=True)
        (root/'.venv/bin/python').symlink_to(sys.executable)
        self.manager_adoption_fixture(root, reject=sequence_defect=='manager-adopt')
        (helpers/'longhorn-v2-im-adopt.py').write_text(
            'import pathlib,sys\nroot=pathlib.Path(__file__).resolve().parents[2]\n'
            'assert sys.argv[1:]==["cache","--engine","v2"]\n'
            '(root/"cache-calls").open("a").write("cache\\n")\n' +
            ('raise SystemExit(9)\n' if cache_fail else '(root/"cache.ready").touch()\n'))
        (helpers/'longhorn-im-revision-adopt.py').write_text((helpers/'longhorn-v2-im-adopt.py').read_text())
        (helpers/'longhorn-im-revision-3-adopt.py').write_text(
            'import pathlib,sys\nroot=pathlib.Path(__file__).resolve().parents[2]\n'
            'args=sys.argv[1:]; assert args in (["preflight","--engine","v2"],["preflight","--engine","v2-dr-target"],["cache","--engine","v2"])\n'
            '(root/"revision3-order").open("a").write(" ".join(args)+"\\n")\n'
            'if args[0]=="preflight":\n'
            ' assert not (root/"v1.started").exists() and not (root/"v2.started").exists()\n'
            + (' raise SystemExit(11)\n' if sequence_defect=='revision3-preflight' else '') +
            'else:\n'
            ' assert (root/"revision3-order").read_text().splitlines()[:2]==["preflight --engine v2","preflight --engine v2-dr-target"]\n'
            + (' raise SystemExit(9)\n' if cache_fail else ' (root/"cache.ready").touch()\n'))
        (helpers/'longhorn-v2-clone.py').write_text('''import json,pathlib,sys
root=pathlib.Path(__file__).resolve().parents[2]
assert json.loads((root/'manager.ready').read_text())=={'healthy':True,'empty_business':True,'profile':'v2'}
args=sys.argv[1:]
if args[0]=='snapshot':
 assert args[1:3]==['--phase',args[2]] and args[2] in ('before','after') and len(args)==3
 print('LONGHORN_V2_CLONE_BASELINE_'+args[2].upper()+' '+json.dumps({'baseline':{'fixture':True},'inputs':{'fixture':True}},sort_keys=True))
elif args[0]=='stage-receipt':
 assert args[1]=='--directory' and args[3]=='--stage' and len(args)==5
 directory=pathlib.Path(args[2]);stage=args[4]
 assert stage in ('linked-clone','linked-lifecycle','incomplete-clone')
 proof={kind:int((directory/(stage+'.'+kind)).read_text()) for kind in ('exit','finalized')}
 assert proof=={'exit':0,'finalized':0}
 print('LONGHORN_V2_CLONE_STAGE_RECEIPT stage='+stage+' '+json.dumps(proof,sort_keys=True))
else:raise SystemExit(2)
''')
        locks = root/'locks'
        locks.mkdir()
        for name in ('kubeauto-ceph-priority.lock', 'kubeauto-ceph-compute.lock', 'kubeauto-redis-core-pool.lock'):
            (locks/name).touch()
        source = (ROOT/'tests/helpers/longhorn-parallel.sh').read_text()
        source = source.replace('BASE=/usr/local/kubeauto', 'BASE='+str(root))
        source = source.replace('/var/lock/', str(locks)+'/')
        source = source.replace('/var/tmp/kubeauto-longhorn-parallel', str(root/'parallel'))
        source = source.replace('/var/tmp/kubeauto-longhorn-v2-platform-install', str(root/'v2-platform-install'))
        source = source.replace('sleep 15', 'sleep 0.02')
        if launch_delay:
            # Model a background child not being scheduled before its parent's
            # first log read. Redirection belongs to that child, not the parent.
            source = source.replace('setsid env KUBEAUTO_LOG_DIR=',
                                    '( sleep '+str(launch_delay)+'; setsid env KUBEAUTO_LOG_DIR=')
            for branch in ('v1', 'v2'):
                source = source.replace('> "$work/'+branch+'.log" 2>&1 &',
                                        '> "$work/'+branch+'.log" 2>&1 ) &')
        (helpers/'longhorn-parallel.sh').write_text(source)
        for name in ('longhorn-parallel.py', 'run-durable-gate.sh'):
            content = (ROOT/'tests/helpers'/name).read_text()
            if name == 'longhorn-parallel.py':
                content = content.replace('/root/.kube/config', str(root/'shared-config'))
            (helpers/name).write_text(content)
        if sequence_defect in ('resume-remote-finalized', 'resume-worker-finalized', 'disk-tail-first-finalized'):
            durable=helpers/'run-durable-gate.sh'
            durable.rename(helpers/'run-durable-gate-original.sh')
            fault={'resume-remote-finalized':'remote-spdk','resume-worker-finalized':'worker-reboot','disk-tail-first-finalized':'single-disk'}[sequence_defect]
            durable.write_text('#!/bin/bash\nset -e\nbash "$(dirname "$0")/run-durable-gate-original.sh" "$@"\nif [[ "${!#}" == v2-failure-'+fault+' ]]; then rm -f "$1.finalized"; fi\n')
        common = """#!/usr/bin/env bash
set -euo pipefail
BASE="$(cd "$(dirname "$0")/../.." && pwd)"
"""
        barrier = """
if [[ "$role" == v1 && ! -f "$BASE/v1.started" ]]; then sleep STARTUP_DELAY; fi
touch "$BASE/$role.started"
ready=no
# Wait for the peer, rather than assuming both local subprocesses start in 1s.
for attempt in {1..500}; do
  if [[ -f "$BASE/$other.started" ]]; then ready=yes; break; fi
  sleep 0.01
done
[[ "$ready" == yes ]]
""".replace('STARTUP_DELAY', str(startup_delay))
        v1 = common+'role=v1 other=v2\n'+barrier+"""
case "$1" in
 maintenance-cleanup|ownership-controller-refusal-qualification|ownership-request-refusal-qualification|system-restore|system-restore-prepared-recovery|disaster-recovery|v1-upgrade-lifecycle|observability-image-cache|observability-cleanup|observability-support|observability) ;;
 *) [[ "$LONGHORN_PARALLEL_CONTEXT" == explicit-kubeconfig ]] ;;
esac
echo "$1" >> "$BASE/v1-order"
"""+('[[ "$1" != '+(fail if isinstance(fail, str) else 'idempotence')+' ]] || exit 7\n' if fail else '')+"""
case "$1" in
 maintenance-cleanup|ownership-controller-refusal-qualification|ownership-request-refusal-qualification|v1-im-revision-2-adopt-qualification|idempotence|platform|platform-propagation-qualification|security|backup|backup-refusal|recurring|encryption|uninstall|maintenance|capacity-cleanup|capacity|node-failure-cleanup|integrity|disk-lifecycle|node-failure|network-failure|disk-failure|last-replica|middleware|system-restore|system-restore-prepared-recovery|disaster-recovery|v1-upgrade-lifecycle|observability-image-cache|observability-cleanup|observability-support|observability)
   marker="LONGHORN_${1^^}"; marker="${marker//-/_}"
   case "$1" in disk-lifecycle|node-failure|last-replica|system-restore) marker="${marker}_GATE";;
     system-restore-prepared-recovery) marker=LONGHORN_SYSTEM_RESTORE_GATE;;
     v1-upgrade-lifecycle) marker=LONGHORN_V1_UPGRADE_GATE;; esac
"""+('   [[ "$1" == '+missing_marker+' ]] || ' if missing_marker else '   ')+"""echo "${marker}_SCOPED_CLEAN_PASS"
   if [[ "$1" == observability-cleanup ]]; then echo LONGHORN_OBSERVABILITY_SCOPED_CLEAN_PASS; fi
   echo "${marker}_PASS" ;;
 *) exit 2 ;;
esac
"""
        if sequence_defect in ('remaining-node-receipt-absent',
                               'remaining-node-receipt-retained', 'remaining-node-receipt-symlink'):
            cluster = root/'clusters/longhorn-gate'
            cluster.mkdir(parents=True)
            journal = cluster/'longhorn-node-failure.json'
            if sequence_defect == 'remaining-node-receipt-retained':
                journal.write_text(json.dumps({'owned_fixture': True}))
            elif sequence_defect == 'remaining-node-receipt-symlink':
                journal.symlink_to(cluster/'missing-receipt')
            v1 = v1.replace('echo "$1" >> "$BASE/v1-order"', '''echo "$1" >> "$BASE/v1-order"
journal="$BASE/clusters/longhorn-gate/longhorn-node-failure.json"
archive="$BASE/clusters/longhorn-gate/longhorn-node-failure-recovered-fixture.json"
if [[ "$1" == node-failure-cleanup ]]; then
 [[ -f "$journal" && ! -L "$journal" ]]
 rm "$journal"
 touch "$archive"
elif [[ "$1" == node-failure ]]; then
 [[ ! -e "$journal" && ! -L "$journal" ]]
fi''')
        if sequence_defect == 'resume-context':
            v1 = v1.replace('echo "$1" >> "$BASE/v1-order"', 'echo "$1" >> "$BASE/v1-order"\n[[ "$1" != ownership-controller-refusal-qualification ]] || echo changed > "$BASE/shared-config"')
        if sequence_defect == 'operations-context':
            v1 = v1.replace('echo "$1" >> "$BASE/v1-order"', 'echo "$1" >> "$BASE/v1-order"\n[[ "$1" != maintenance ]] || echo changed > "$BASE/shared-config"')
        v2 = common+'role=v2 other=v1\n'+barrier+"""
sleep 0.05
touch "$BASE/v2.completed"
echo LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_SCOPED_CLEAN_PASS
echo LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION_PASS
"""
        if sequence_defect == 'middleware-before-v1':
            v1 = v1.replace('case "$1" in', '''if [[ "$1" == maintenance ]]; then
 touch "$BASE/v1.awaiting-middleware"
 for attempt in {1..200}; do
   [[ ! -f "$BASE/v2-middleware.started" ]] || break
   sleep 0.01
 done
 [[ -f "$BASE/v2-middleware.started" ]]
fi
case "$1" in''')
        (helpers/'longhorn-live.sh').write_text(common+'''
case "$1" in
 v2-linked-clone-qualification) exec bash "$BASE/tests/helpers/longhorn-v2-linked-clones.sh" "$1" ;;
 v2-linked-lifecycle-qualification) exec bash "$BASE/tests/helpers/longhorn-v2-linked-lifecycle.sh" "$1" ;;
 v2-incomplete-clone-qualification) exec bash "$BASE/tests/helpers/longhorn-v2-incomplete-clone.sh" "$1" ;;
 v2-runtime-resources|v2-coexist-qualification|v2-maintenance-drain-qualification) exec bash "$BASE/tests/helpers/longhorn-v2-platform-fixture.sh" "$1" ;;
 v2-observability-monitoring) exec bash "$BASE/tests/helpers/longhorn-v2-observability.sh" "$1" ;;
 v2-interrupt-qualification) exec bash "$BASE/tests/helpers/longhorn-v2-volumes.sh" "$1" ;;
esac
'''+v1)
        acceptance = (ROOT/'tests/helpers/longhorn-v2-linked-acceptance.sh').read_text()
        acceptance = acceptance.replace('BASE=/usr/local/kubeauto', 'BASE='+str(root)).replace(
            '/var/tmp/kubeauto-longhorn-linked-acceptance', str(root/'linked-acceptance'))
        (helpers/'longhorn-v2-linked-acceptance.sh').write_text(acceptance)
        (helpers/'longhorn-v2-interrupt-manager.sh').write_text(v2)
        linked = v2.replace('echo LONGHORN_V2_INTERRUPT_MANAGER_QUALIFICATION',
                            'echo LONGHORN_V2_LINKED_CLONE_QUALIFICATION')
        linked = linked.replace('sleep 0.05', '[[ "$1" == v2-linked-clone-qualification ]]\nsleep 0.05')
        if v2_fail:
            linked = linked.replace('sleep 0.05', 'exit 8\nsleep 0.05')
        (helpers/'longhorn-v2-linked-clones.sh').write_text(linked)
        lifecycle = linked.replace('v2-linked-clone-qualification', 'v2-linked-lifecycle-qualification').replace(
            'LONGHORN_V2_LINKED_CLONE_QUALIFICATION', 'LONGHORN_V2_LINKED_LIFECYCLE_QUALIFICATION')
        (helpers/'longhorn-v2-linked-lifecycle.sh').write_text(lifecycle)
        incomplete = lifecycle.replace('v2-linked-lifecycle-qualification', 'v2-incomplete-clone-qualification').replace(
            'LONGHORN_V2_LINKED_LIFECYCLE_QUALIFICATION', 'LONGHORN_V2_INCOMPLETE_CLONE_QUALIFICATION')
        (helpers/'longhorn-v2-incomplete-clone.sh').write_text(incomplete)
        ownership = common+'role=v1 other=v2\n'+barrier+('exit 7\n' if fail is True or fail == 'ownership' else '')+"""
[[ "$1" == ownership ]]
echo ownership >> "$BASE/v1-order"
echo LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS
echo LONGHORN_OWNERSHIP_PASS
"""
        (helpers/'longhorn-ownership-aggregate.sh').write_text(ownership)
        (helpers/'longhorn-v2-failure.sh').write_text(common+'role=v2 other=v1\n'+barrier+('exit 8\n' if v2_fail else '')+'''
echo "$1" >> "$BASE/v2-order"
case "$1" in v2-failure-cleanup|v2-failure|v2-failure-remaining|v2-failure-remote-spdk|v2-failure-replica-network|v2-failure-worker-reboot|v2-failure-single-disk|v2-failure-rebuild-qos|v2-failure-all-replicas) ;; *) exit 2;; esac
marker="LONGHORN_${1^^}"; marker="${marker//-/_}"
echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
''')
        fault_path=helpers/'longhorn-v2-failure.sh'
        fault=fault_path.read_text().replace('echo "${marker}_SCOPED_CLEAN_PASS"', 'if [[ "$1" != v2-failure && "$1" != v2-failure-remaining && "$1" != v2-failure-cleanup ]]; then echo "LONGHORN_V2_FAILURE_STAGE_PASS case=${1#v2-failure-} durable_rc=0 finalized=0"; fi\necho "${marker}_SCOPED_CLEAN_PASS"')
        if isinstance(sequence_defect,str) and sequence_defect.startswith(('resume-remote-', 'resume-worker-', 'disk-tail-first-')):
            target='single-disk' if sequence_defect.startswith('disk-tail-first-') else ('remote-spdk' if sequence_defect.startswith('resume-remote-') else 'worker-reboot')
            kind=sequence_defect.rsplit('-',1)[1]
            if kind=='nonzero': fault=fault.replace('marker="LONGHORN_${1^^}"', '[[ "$1" != v2-failure-'+target+' ]] || exit 17\nmarker="LONGHORN_${1^^}"')
            elif kind=='clean': fault=fault.replace('echo "${marker}_SCOPED_CLEAN_PASS"', '[[ "$1" == v2-failure-'+target+' ]] || echo "${marker}_SCOPED_CLEAN_PASS"')
            elif kind=='terminal': fault += '\n[[ "$1" != v2-failure-'+target+' ]] || echo LONGHORN_V2_FAILURE_'+target.upper().replace("-","_")+'_PASS\n'
            elif kind=='stage': fault=fault.replace('echo "LONGHORN_V2_FAILURE_STAGE_PASS case=${1#v2-failure-} durable_rc=0 finalized=0"', '[[ "$1" == v2-failure-'+target+' ]] || echo "LONGHORN_V2_FAILURE_STAGE_PASS case=${1#v2-failure-} durable_rc=0 finalized=0"')
        if sequence_defect=='resume-network-nonzero':
            fault=fault.replace('marker="LONGHORN_${1^^}"', '[[ "$1" != v2-failure-replica-network ]] || exit 18\nmarker="LONGHORN_${1^^}"')
        if sequence_defect in ('resume-cleanup-nonzero', 'resume-disk-nonzero'):
            target='cleanup' if sequence_defect=='resume-cleanup-nonzero' else 'single-disk'
            fault=fault.replace('marker="LONGHORN_${1^^}"', '[[ "$1" != v2-failure-'+target+' ]] || exit 19\nmarker="LONGHORN_${1^^}"')
        fault_path.write_text(fault)
        platform_fixture = common+'role=v2 other=v1\n'+barrier+('exit 8\n' if v2_fail else '')+'''
echo "$1" >> "$BASE/v2-order"
marker="LONGHORN_${1^^}"; marker="${marker//-/_}"
if [[ "$1" == v2-runtime-resources ]]; then echo "${marker}_GATE_SCOPED_CLEAN_PASS"; else echo "${marker}_SCOPED_CLEAN_PASS"; fi
echo "${marker}_PASS"
if [[ "$1" == v2-maintenance-drain-qualification ]]; then
 echo LONGHORN_V2_MAINTENANCE_DRAIN_BUSINESS_PASS
 echo LONGHORN_V2_MAINTENANCE_DRAIN_FIXTURES_CLEAN_PASS
fi
touch "$BASE/v2.completed"
'''
        (helpers/'longhorn-v2-platform-fixture.sh').write_text(platform_fixture)
        middleware = common+'role=v2 other=v1\n'+barrier+'''
[[ "$1" == v2-middleware ]]
echo "$1" >> "$BASE/v2-order"
touch "$BASE/v2-middleware.started"
'''+('exit 18\n' if sequence_defect == 'middleware-nonzero' else '')+'''
echo LONGHORN_V2_MIDDLEWARE_SCOPED_CLEAN_PASS
echo LONGHORN_V2_MIDDLEWARE_PASS
'''
        if sequence_defect == 'middleware-clean':
            middleware = middleware.replace('echo LONGHORN_V2_MIDDLEWARE_SCOPED_CLEAN_PASS', ':')
        if sequence_defect == 'middleware-terminal':
            middleware += 'echo LONGHORN_V2_MIDDLEWARE_PASS\n'
        if sequence_defect == 'middleware-before-v1':
            middleware = middleware.replace('touch "$BASE/v2-middleware.started"', '''for attempt in {1..200}; do
 [[ ! -f "$BASE/v1.awaiting-middleware" ]] || break
 sleep 0.01
done
[[ -f "$BASE/v1.awaiting-middleware" ]]
touch "$BASE/v2-middleware.started"''')
        (helpers/'longhorn-v2-middleware.sh').write_text(middleware)
        interrupt = common+'role=v2 other=v1\n'+barrier+'''
[[ "$1" == v2-interrupt-qualification ]]
echo "$1" >> "$BASE/v2-order"
'''+('exit 19\n' if sequence_defect == 'interrupt-nonzero' else '')+'''
echo LONGHORN_V2_INTERRUPT_QUALIFICATION_SCOPED_CLEAN_PASS
echo LONGHORN_V2_INTERRUPT_QUALIFICATION_PASS
'''
        if sequence_defect == 'interrupt-clean':
            interrupt = interrupt.replace('echo LONGHORN_V2_INTERRUPT_QUALIFICATION_SCOPED_CLEAN_PASS', ':')
        if sequence_defect == 'interrupt-terminal':
            interrupt += 'echo LONGHORN_V2_INTERRUPT_QUALIFICATION_PASS\n'
        (helpers/'longhorn-v2-volumes.sh').write_text(interrupt)
        (helpers/'longhorn-v2-platform-install.py').write_text('''import pathlib,sys
action=sys.argv[1]
if action=='snapshot':
 p=pathlib.Path(sys.argv[sys.argv.index('--receipt')+1]);assert not p.exists();p.write_text('{"original":true}')
elif action=='verify-stage':
 p=pathlib.Path(sys.argv[sys.argv.index('--directory')+1])
 assert (p/'durable-exit').read_text().strip()=='0'
 assert (p/'durable-finalized').read_text().strip()=='0'
 assert (p/'baseline-before.json').read_bytes()==(p/'baseline-after.json').read_bytes()
 assert 'LONGHORN_GATE_EXIT rc=0' in (p/'gate.log').read_text().splitlines()
else:raise SystemExit(2)
''')
        adoption = common+'role="${1%%-*}"\n'+"""
[[ -f "$BASE/cache.ready" ]]
if [[ "$role" == v1 ]]; then other=v2; else other=v1; fi
"""+barrier+"""
marker="LONGHORN_${1^^}"; marker="${marker//-/_}"
echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
touch "$BASE/$role.completed"
"""
        if v2_fail:
            adoption = adoption.replace('echo "${marker}_SCOPED_CLEAN_PASS"',
                '[[ "$role" != v2 ]] || exit 8\necho "${marker}_SCOPED_CLEAN_PASS"')
        (helpers/'longhorn-v2-im-adopt.sh').write_text(adoption)
        if mode.startswith('v2-source-target-'):
            script = common+'''role=v1 other=v2
if [[ "$1" == *dr-target* ]]; then role=v2 other=v1; fi
'''+barrier+'''marker="LONGHORN_${1^^}"; marker="${marker//-/_}"
'''+('[[ "$role" != v2 ]] || exit 8\n' if v2_fail else '')+'''echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
'''
            (helpers/'longhorn-v2-resources-reboot.sh').write_text(script)
            (helpers/'longhorn-v2-im-adopt.sh').write_text(script)
            if mode=='v2-source-target-im-revision-3-adopt-qualification':
                script=script.replace('role=v1 other=v2', '[[ -f "$BASE/cache.ready" ]]\nrole=v1 other=v2',1)
                (helpers/'longhorn-im-revision-3-adopt.sh').write_text(script)
        (helpers/'longhorn-platform.sh').write_text(v1.replace(
            '[[ "$LONGHORN_PARALLEL_CONTEXT" == explicit-kubeconfig ]]', ':'))
        environment = common+'''role=v1 other=v2
if [[ "$1" == *dr-target* ]]; then role=v2 other=v1; fi
'''+barrier+'''marker="LONGHORN_${1^^}"; marker="${marker//-/_}"
'''+('[[ "$role" != v2 ]] || exit 8\n' if v2_fail else '')+'''echo "${marker}_SCOPED_CLEAN_PASS"
echo "${marker}_PASS"
'''
        (helpers/'longhorn-v2-post-reboot-environment.sh').write_text(environment)
        stability = common+'role=v1 other=v2\n'+barrier+"""
[[ "$1" == stability ]]
echo LONGHORN_STABILITY_SCOPED_CLEAN_PASS
echo LONGHORN_STABILITY_PASS
touch "$BASE/v1.completed"
"""
        observation = common+'role=v2 other=v1\n'+barrier+"""
case "$1" in
 v2-observability) marker=LONGHORN_V2_OBSERVABILITY_PERFORMANCE ;;
 v2-observability-monitoring) marker=LONGHORN_V2_OBSERVABILITY_MONITORING; touch "$BASE/v2-monitoring.started" ;;
 *) exit 2 ;;
esac
"""+('exit 8\n' if v2_fail else '')+"""if [[ "$1" == v2-runtime-resources ]]; then echo "${marker}_GATE_SCOPED_CLEAN_PASS"; else echo "${marker}_SCOPED_CLEAN_PASS"; fi
echo "${marker}_PASS"
touch "$BASE/v2.completed"
"""
        (helpers/'longhorn-v1-stability.sh').write_text(stability)
        if mode in ('v1-monitoring-v2-resource-tail', 'v1-recovery-tail-v2-install-tail', 'v1-dr-tail-v2-install-tail', 'v1-upgrade-tail-v2-install-tail', 'v1-upgrade-tail-v2-maintenance-tail'):
            observation = observation.replace('case \"$1\" in', 'echo \"$1\" >> \"$BASE/v2-order\"\ncase \"$1\" in', 1)
        (helpers/'longhorn-v2-observability.sh').write_text(observation)
        if sequence_defect in ('ownership-clean', 'context'):
            fixture = (helpers/'longhorn-ownership-aggregate.sh').read_text()
            if sequence_defect == 'ownership-clean':
                fixture = fixture.replace('echo LONGHORN_OWNERSHIP_SCOPED_CLEAN_PASS', ':')
            else:
                fixture += '\nprintf foreign-context > "$BASE/shared-config"\n'
            (helpers/'longhorn-ownership-aggregate.sh').write_text(fixture)
        if sequence_defect == 'failure-clean':
            fixture = (helpers/'longhorn-v2-failure.sh').read_text().replace(
                'echo "${marker}_SCOPED_CLEAN_PASS"', ':')
            (helpers/'longhorn-v2-failure.sh').write_text(fixture)
        if sequence_defect in ('tail-target-fixture-retained', 'tail-coexist-config-retained', 'tail-prepared-checkpoint-invalid'):
            if sequence_defect == 'tail-target-fixture-retained':
                probe = """from pathlib import Path
from types import SimpleNamespace
import importlib.util
spec=importlib.util.spec_from_file_location('actual_target',ACTUAL)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
t=m.Target.__new__(m.Target);t.lab=SimpleNamespace(NODES=(m.HOST,));t.path=Path(__file__).with_name('unfinished-target.json');t.path.write_text('{}')
t.prepare(1)
""".replace('ACTUAL',repr(str(ROOT/'tests/helpers/longhorn-v1-recovery-target.py')))
                path=helpers/'longhorn-live.sh';trigger='disaster-recovery'
            elif sequence_defect == 'tail-prepared-checkpoint-invalid':
                probe = """import importlib.util
spec=importlib.util.spec_from_file_location('actual_recovery',ACTUAL)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
r=m.Recovery.__new__(m.Recovery);r.row={'stage':'restoring'};r.read=lambda:None
r.resume_prepared()
""".replace('ACTUAL',repr(str(ROOT/'tests/helpers/longhorn-system-recovery.py')))
                path=helpers/'longhorn-live.sh';trigger='system-restore-prepared-recovery'
            else:
                probe = """from pathlib import Path
from types import SimpleNamespace
import importlib.util
spec=importlib.util.spec_from_file_location('actual_drain',ACTUAL)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(__file__).parent/'coexist-state';root.mkdir();(root/'config.yml').write_text('longhorn_data_engines: [v1, v2]\\nlonghorn_requests: []\\n')
m.profile(SimpleNamespace(DIRECTORY=root))
""".replace('ACTUAL',repr(str(ROOT/'tests/helpers/longhorn-v2-maintenance-drain.py')))
                path=helpers/'longhorn-v2-platform-fixture.sh';trigger='v2-maintenance-drain-qualification'
            probe_path=helpers/'actual-handoff-refusal.py';probe_path.write_text(probe)
            original=path.read_text();path.write_text(original.replace('set -euo pipefail',
                'set -euo pipefail\n'+('touch '+str(root/'v1.started')+'\n' if sequence_defect in ('tail-target-fixture-retained','tail-prepared-checkpoint-invalid') else 'touch '+str(root/'v2.started')+'\n')+'if [[ "$1" == '+trigger+' ]]; then '+sys.executable+' '+str(probe_path)+'; fi',1))
        if sequence_defect in ('monitoring-tail-pending', 'resource-tail-config'):
            if sequence_defect == 'monitoring-tail-pending':
                probe = """import importlib.util
spec=importlib.util.spec_from_file_location('actual_monitor',ACTUAL)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.DATA_ENGINE='v1'
m.desired('prom_install: "yes"\\nlonghorn_monitoring_enabled: true\\nlonghorn_requests: []\\nlonghorn_data_engines: [v1]\\n', 'owned-monitor')
""".replace('ACTUAL',repr(str(ROOT/'tests/helpers/longhorn-v2-monitoring.py')))
                trigger='observability';role='v1';path=helpers/'longhorn-live.sh'
            else:
                probe = """import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
spec=importlib.util.spec_from_file_location('actual_runtime',ACTUAL)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path(__file__).parent/'bad-runtime';root.mkdir()
(root/'config.yml').write_text('longhorn_data_engines: [v1]\\nlonghorn_requests: []\\n')
lab=SimpleNamespace(DIRECTORY=root,run_requests=Mock(),installation_health=Mock(),get=Mock(return_value={'items':[]}))
m.business(lab,1)
""".replace('ACTUAL',repr(str(ROOT/'tests/helpers/longhorn-v2-runtime-resources.py')))
                trigger='v2-runtime-resources';role='v2';path=helpers/'longhorn-v2-platform-fixture.sh'
            probe_path=helpers/'actual-resource-monitor-handoff.py';probe_path.write_text(probe)
            original=path.read_text();path.write_text(original.replace('set -euo pipefail',
                'set -euo pipefail\ntouch '+str(root/(role+'.started'))+'\nif [[ "$1" == '+trigger+' ]]; then '+sys.executable+' '+str(probe_path)+'; fi',1))
        launcher = """
import fcntl,os,pathlib,sys
root=pathlib.Path(sys.argv[1])
for fd,name in [(9,'kubeauto-ceph-priority.lock'),(7,'kubeauto-ceph-compute.lock'),(8,'kubeauto-redis-core-pool.lock')]:
 f=os.open(root/'locks'/name,os.O_RDWR)
 high=fcntl.fcntl(f,fcntl.F_DUPFD,30);os.close(f)
 os.dup2(high,fd);os.close(high);fcntl.flock(fd,fcntl.LOCK_EX)
os.environ['LONGHORN_PARENT_LEASE_HELD']='yes'
os.execv('/bin/bash',['bash',str(root/'tests/helpers/longhorn-parallel.sh'),sys.argv[2]])
"""
        env = dict(os.environ)
        if resume_stability is not None:
            env.update(LONGHORN_FULL_RESUME='yes', LONGHORN_FULL_EVIDENCE_DIR=str(root/'evidence'))
            original = helpers/'longhorn-v1-stability.sh'
            original.write_text(original.read_text().replace('set -euo pipefail',
                'set -euo pipefail\ntouch '+str(root/'stability-fresh'), 1))
            fixture = ('import pathlib,sys\nroot=pathlib.Path(__file__).resolve().parents[2]\n'
                'assert sys.argv[1:]==["resume-child","v1-stability"]\n'
                '(root/"v1.started").touch()\n(root/"stability-reuse-called").touch()\n')
            if resume_stability == 'reuse':
                fixture += '\n'.join('print('+repr(marker)+')' for marker in parallel.MARKERS['v1-stability'][:-1])+'\n'
            else:
                fixture += 'raise SystemExit('+('3' if resume_stability == 'absent' else '1')+')\n'
            (helpers/'longhorn-full-evidence.py').write_text(fixture)
        return subprocess.run([sys.executable, '-c', launcher, str(root), mode], capture_output=True, text=True, timeout=10, env=env)

    def test_continuation_rejects_missing_original_cleanup_and_changed_context_before_next_scope(self):
        for defect in ('ownership-clean', 'failure-clean', 'context'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory,
                    mode='v1-ownership-operations-v2-failure-platform', sequence_defect=defect)
                self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
                root=Path(directory)
                parent=next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
                failed_v1=defect!='failure-clean'
                failed_branch='v1-ownership-operations' if failed_v1 else 'v2-failure-platform'
                passed_branch='v2-failure-platform' if failed_v1 else 'v1-ownership-operations'
                with self.assertRaises(ValueError):parallel.verify_branch(parent, failed_branch)
                self.assertEqual(parallel.verify_branch(parent, passed_branch)['rc'],0)
                if failed_v1:
                    self.assertEqual((root/'v1-order').read_text().splitlines(), ['ownership'])
                else:
                    self.assertEqual((root/'v2-order').read_text().splitlines(), ['v2-failure-remaining'])
                self.assertNotIn('LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM_PASS',result.stdout)

    def test_v2_runtime_shell_preserves_concurrent_foreign_context_on_success_and_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); helpers = root/'tests/helpers'; helpers.mkdir(parents=True)
                (root/'.venv/bin').mkdir(parents=True)
                (root/'.venv/bin/python').symlink_to(sys.executable)
                (root/'kube-bin').mkdir()
                (root/'kube-bin/kubectl').write_text('#!/bin/sh\nexit 0\n')
                (root/'kube-bin/kubectl').chmod(0o700)
                (root/'clusters/longhorn-v2-gate').mkdir(parents=True)
                context = root/'shared-context'
                context.write_bytes(b'original-owner-context')
                script = (ROOT/'tests/helpers/longhorn-v2-runtime-resources.sh').read_text().replace(
                    'BASE=/usr/local/kubeauto', 'BASE='+str(root)).replace(
                    '/var/tmp/kubeauto-longhorn-v2-runtime-resources', str(root/'runtime')).replace(
                    '/root/.kube/config', str(context))
                (helpers/'runtime.sh').write_text(script)
                (helpers/'run-durable-gate.sh').write_bytes((ROOT/'tests/helpers/run-durable-gate.sh').read_bytes())
                fixture = r"""import json,os,pathlib,signal,sys,time
root=pathlib.Path(__file__).resolve().parents[2]
action=sys.argv[1]
assert os.environ['KUBECONFIG']==str(root/'clusters/longhorn-v2-gate/kubectl.kubeconfig')
if action=='relay-maintain':
 signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
 (root/'relay-started').touch()
 while True:time.sleep(.01)
if action=='business':
 deadline=time.monotonic()+2
 while not (root/'relay-started').exists():
  assert time.monotonic()<deadline
  time.sleep(.01)
 p=root/'shared-context';other=root/'foreign-update';other.write_bytes(b'new-concurrent-owner-context')
 os.replace(other,p);st=p.stat()
 (root/'expected-context').write_text(json.dumps([st.st_ino,st.st_mtime_ns,p.read_bytes().hex()]))
 (root/'clusters/longhorn-v2-gate/longhorn-v2-runtime-resources.json').write_text('{}')
 if os.environ['TEST_RUNTIME_FAIL']=='yes':sys.exit(7)
 print('LONGHORN_V2_RUNTIME_RESOURCES_BUSINESS_PASS')
 print('LONGHORN_V2_RUNTIME_RESOURCES_SCOPED_CLEAN_PASS')
 print('LONGHORN_V2_RUNTIME_RESOURCES_RESULT {}')
if action=='cleanup':print('LONGHORN_V2_RUNTIME_RESOURCES_SCOPED_CLEAN_PASS')
"""
                for name in ('longhorn-v2-core.py', 'longhorn-v2-install.py', 'longhorn-v2-runtime-resources.py'):
                    (helpers/name).write_text(fixture)
                env = dict(os.environ, LONGHORN_PARENT_LEASE_HELD='yes', TEST_RUNTIME_FAIL='yes' if failed else 'no')
                result = subprocess.run(['bash', str(helpers/'run-durable-gate.sh'), str(root/'owning'),
                    'LONGHORN_GATE_EXIT', 'bash', str(helpers/'runtime.sh'), 'v2-runtime-resources'],
                    env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 7 if failed else 0, result.stdout+result.stderr)
                st=context.stat()
                self.assertEqual([st.st_ino, st.st_mtime_ns, context.read_bytes().hex()],
                                 json.loads((root/'expected-context').read_text()))
                self.assertEqual((root/'owning.exit').read_text().strip(), '7' if failed else '0')
                self.assertEqual((root/'owning.finalized').read_text().strip(), '7' if failed else '0')
                self.assertFalse(any(root.glob('runtime.*/control-kubeconfig')))

    def test_continuation_runs_each_original_scope_once_and_stops_only_failed_pool(self):
        for fail_v1, fail_v2 in ((False, False), ('ownership', False), (False, True)):
            with self.subTest(v1=fail_v1, v2=fail_v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory,
                    mode='v1-ownership-operations-v2-failure-platform', fail=fail_v1, v2_fail=fail_v2)
                self.assertEqual(result.returncode == 0, not (fail_v1 or fail_v2), result.stdout+result.stderr)
                root = Path(directory)
                parents = [p for p in root.glob('parallel.*') if (p/'v1.log').exists()]
                self.assertEqual(len(parents), 1)
                parent = parents[0]
                for branch, failed in (('v1-ownership-operations', fail_v1), ('v2-failure-platform', fail_v2)):
                    if failed:
                        with self.assertRaises(ValueError): parallel.verify_branch(parent, branch)
                    else:
                        self.assertEqual(parallel.verify_branch(parent, branch)['rc'], 0)
                if not fail_v1:
                    self.assertEqual((root/'v1-order').read_text().splitlines(),
                        ['ownership']+list(parallel.V1_OPERATION_MODULES)+
                        ['system-restore','disaster-recovery','v1-upgrade-lifecycle','observability'])
                else:
                    self.assertFalse((root/'v1-order').exists())
                if not fail_v2:
                    self.assertEqual((root/'v2-order').read_text().splitlines(),
                        ['v2-failure-remaining', 'v2-runtime-resources',
                         'v2-coexist-qualification', 'v2-maintenance-drain-qualification', 'v2-middleware', 'v2-interrupt-qualification'])
                else:
                    self.assertFalse((root/'v2-order').exists())
                self.assertEqual('LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM_PASS' in result.stdout,
                                 not (fail_v1 or fail_v2))

    def test_middleware_owning_scope_rejects_failure_or_missing_cleanup_without_discarding_v1(self):
        for defect in ('none', 'middleware-nonzero', 'middleware-clean', 'middleware-terminal'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory,
                    mode='v1-ownership-operations-v2-failure-platform', sequence_defect=defect)
                self.assertEqual(result.returncode == 0, defect == 'none', result.stdout+result.stderr)
                root = Path(directory)
                parent = next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
                self.assertEqual(parallel.verify_branch(parent, 'v1-ownership-operations')['rc'], 0)
                self.assertEqual((root/'v1-order').read_text().splitlines(),
                                 ['ownership']+list(parallel.V1_OPERATION_MODULES)+
                        ['system-restore','disaster-recovery','v1-upgrade-lifecycle','observability'])
                self.assertEqual((root/'v2-order').read_text().splitlines(),
                    ['v2-failure-remaining', 'v2-runtime-resources', 'v2-coexist-qualification',
                     'v2-maintenance-drain-qualification', 'v2-middleware']+
                    (['v2-interrupt-qualification'] if defect=='none' else []))
                sequence = next(p for p in root.glob('parallel-sequence.*') if (p/'v2-middleware').exists())
                self.assertEqual(parallel.verify_branch(sequence/'v2-failure-remaining', 'v2-failure-remaining')['rc'], 0)
                self.assertEqual(parallel.verify_branch(sequence/'v2-platform-install', 'v2-platform-install')['rc'], 0)
                if defect == 'none':
                    self.assertEqual(parallel.verify_branch(sequence/'v2-middleware', 'v2-middleware')['rc'], 0)
                    self.assertEqual(parallel.verify_branch(parent, 'v2-failure-platform')['rc'], 0)
                else:
                    with self.assertRaises(ValueError):
                        parallel.verify_branch(sequence/'v2-middleware', 'v2-middleware')
                    with self.assertRaises(ValueError):parallel.verify_branch(parent, 'v2-failure-platform')
                    self.assertNotIn('LONGHORN_PARALLEL_V2_FAILURE_PLATFORM_STAGE_PASS stage=v2-middleware',
                                     (parent/'v2.log').read_text())
                self.assertEqual('LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM_PASS' in result.stdout,
                                 defect == 'none')

    def test_interrupt_scope_failure_retains_completed_v2_stages_and_v1_tail(self):
        for defect in ('none','interrupt-nonzero','interrupt-clean','interrupt-terminal'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-ownership-operations-v2-failure-platform',sequence_defect=defect)
                self.assertEqual(result.returncode==0,defect=='none',result.stdout+result.stderr)
                root=Path(directory);parent=next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
                self.assertEqual(parallel.verify_branch(parent,'v1-ownership-operations')['rc'],0)
                self.assertEqual((root/'v2-order').read_text().splitlines(),
                    ['v2-failure-remaining','v2-runtime-resources','v2-coexist-qualification',
                     'v2-maintenance-drain-qualification','v2-middleware','v2-interrupt-qualification'])
                sequence=next(p for p in root.glob('parallel-sequence.*') if (p/'v2-interrupt-qualification').exists())
                for prior in ('v2-failure-remaining','v2-platform-install','v2-middleware'):
                    self.assertEqual(parallel.verify_branch(sequence/prior,prior)['rc'],0)
                if defect=='none':
                    self.assertEqual(parallel.verify_branch(sequence/'v2-interrupt-qualification','v2-interrupt-qualification')['rc'],0)
                    self.assertEqual(parallel.verify_branch(parent,'v2-failure-platform')['rc'],0)
                else:
                    with self.assertRaises(ValueError):parallel.verify_branch(sequence/'v2-interrupt-qualification','v2-interrupt-qualification')
                    with self.assertRaises(ValueError):parallel.verify_branch(parent,'v2-failure-platform')
                    self.assertNotIn('LONGHORN_PARALLEL_V2_FAILURE_PLATFORM_STAGE_PASS stage=v2-interrupt-qualification',
                                     (parent/'v2.log').read_text())
                self.assertEqual((root/'v1-order').read_text().splitlines(),['ownership']+list(parallel.V1_OPERATION_MODULES)+
                                 ['system-restore','disaster-recovery','v1-upgrade-lifecycle','observability'])

    def test_v1_tail_failure_stops_only_its_pool_before_later_owned_scopes(self):
        tail=['system-restore','disaster-recovery','v1-upgrade-lifecycle','observability']
        for index,failed in enumerate(tail):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-ownership-operations-v2-failure-platform',fail=failed)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                root=Path(directory);parent=next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
                self.assertEqual((root/'v1-order').read_text().splitlines(),['ownership']+list(parallel.V1_OPERATION_MODULES)+tail[:index+1])
                self.assertEqual(parallel.verify_branch(parent,'v2-failure-platform')['rc'],0)
                with self.assertRaises(ValueError):parallel.verify_branch(parent,'v1-ownership-operations')
                sequence=next(p for p in root.glob('parallel-sequence.*') if (p/'system-restore').exists())
                for prior in ['v1-ownership','v1-operations']+tail[:index]:
                    self.assertEqual(parallel.verify_branch(sequence/prior,prior)['rc'],0)
                with self.assertRaises(ValueError):parallel.verify_branch(sequence/failed,failed)
                for later in tail[index+1:]:self.assertFalse((sequence/later).exists())
                self.assertNotIn('LONGHORN_V1_OWNERSHIP_OPERATIONS_V2_FAILURE_PLATFORM_PASS',result.stdout)

    def test_v2_continues_to_middleware_while_v1_still_has_operations_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory,
                mode='v1-ownership-operations-v2-failure-platform', sequence_defect='middleware-before-v1')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            root = Path(directory)
            self.assertTrue((root/'v1.awaiting-middleware').exists())
            self.assertTrue((root/'v2-middleware.started').exists())
            parent = next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
            for branch in ('v1-ownership-operations', 'v2-failure-platform'):
                self.assertEqual(parallel.verify_branch(parent, branch)['rc'], 0)

    def test_post_revision_batch_continues_only_ready_v1_cases_alongside_faults(self):
        for fail in (False, 'backup-refusal'):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-post-revision-backups-v2-failure-acceptance', fail=fail)
                self.assertEqual(result.returncode == 0, not fail, result.stdout+result.stderr)
                root = Path(directory)
                self.assertEqual((root/'v1-order').read_text().splitlines(),
                    ['v1-im-revision-2-adopt-qualification', 'backup-refusal'] if fail else
                    ['v1-im-revision-2-adopt-qualification', 'backup-refusal', 'recurring', 'encryption', 'uninstall'])
                parent = next(root.glob('parallel.*'))
                self.assertEqual(parallel.verify_branch(parent, 'v2-failure-acceptance')['rc'], 0)
                if fail:
                    self.assertNotIn('LONGHORN_V1_POST_REVISION_BACKUPS_V2_FAILURE_ACCEPTANCE_PASS', result.stdout)
                else:
                    self.assertEqual(parallel.verify_branch(parent, 'v1-post-revision-backups')['rc'], 0)

    def test_operations_batch_stops_at_first_unaccepted_module_and_uses_real_terminals(self):
        for defect in ('none', 'nonzero', 'missing-clean', 'reused', 'reuse-rejected'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); helpers = root/'tests/helpers'; helpers.mkdir(parents=True)
                (root/'.venv/bin').mkdir(parents=True)
                (root/'.venv/bin/python').symlink_to(sys.executable)
                locks = root/'locks'; locks.mkdir()
                source = (ROOT/'tests/helpers/longhorn-parallel.sh').read_text().replace(
                    'BASE=/usr/local/kubeauto', 'BASE='+str(root)).replace(
                    '/var/lock/', str(locks)+'/').replace(
                    '/var/tmp/kubeauto-longhorn-parallel', str(root/'parallel'))
                (helpers/'longhorn-parallel.sh').write_text(source)
                for name in ('longhorn-parallel.py', 'run-durable-gate.sh'):
                    (helpers/name).write_bytes((ROOT/'tests/helpers'/name).read_bytes())
                fixture = ['#!/usr/bin/env bash', 'set -eu',
                    '[[ "$LONGHORN_PARALLEL_CONTEXT" == explicit-kubeconfig ]]',
                    'echo "$1" >> '+str(root/'order'), 'case "$1" in']
                for mode in parallel.V1_OPERATION_MODULES:
                    lines = list(parallel.MARKERS[mode][:-1])
                    if mode == 'integrity' and defect == 'missing-clean': lines = lines[1:]
                    action = 'exit 17' if mode == 'integrity' and defect == 'nonzero' else '\n'.join(
                        'echo '+line for line in lines)
                    fixture.append(mode+')\n'+action+'\n;;')
                fixture.append('*) exit 2;;\nesac\n')
                (helpers/'longhorn-live.sh').write_text('\n'.join(fixture))
                if defect in ('reused','reuse-rejected'):
                    (helpers/'longhorn-full-evidence.py').write_text('''import pathlib,sys
root=pathlib.Path(__file__).resolve().parents[2]
assert sys.argv[1]=='resume-child'
mode=sys.argv[2]
if mode not in ('maintenance','capacity'): raise SystemExit(3)
if (root/'reject-reuse').exists(): raise SystemExit(1)
markers={'maintenance':('LONGHORN_MAINTENANCE_SCOPED_CLEAN_PASS','LONGHORN_MAINTENANCE_PASS'),
 'capacity':('LONGHORN_CAPACITY_SCOPED_CLEAN_PASS','LONGHORN_CAPACITY_PASS')}
for marker in markers[mode]: print(marker)
''')
                    if defect=='reuse-rejected':(root/'reject-reuse').touch()
                launcher = '''
import fcntl,os,pathlib,sys
root=pathlib.Path(sys.argv[1])
for fd,name in [(9,'kubeauto-ceph-priority.lock'),(7,'kubeauto-ceph-compute.lock'),(8,'kubeauto-redis-core-pool.lock')]:
 f=os.open(root/'locks'/name,os.O_CREAT|os.O_RDWR,0o600)
 high=fcntl.fcntl(f,fcntl.F_DUPFD,30);os.close(f)
 os.dup2(high,fd);os.close(high);fcntl.flock(fd,fcntl.LOCK_EX)
os.environ['LONGHORN_PARENT_LEASE_HELD']='yes'
os.execv('/bin/bash',['bash',str(root/'tests/helpers/longhorn-parallel.sh'),'v1-operations'])
'''
                env=dict(os.environ)
                if defect in ('reused','reuse-rejected'):
                    env.update(LONGHORN_FULL_RESUME='yes',LONGHORN_FULL_EVIDENCE_DIR=str(root/'evidence'))
                result = subprocess.run([sys.executable, '-c', launcher, str(root)],env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode == 0, defect in ('none','reused'), result.stdout+result.stderr)
                if defect=='reuse-rejected':
                    self.assertFalse((root/'order').exists())
                    self.assertNotIn('LONGHORN_PARALLEL_V1_OPERATIONS_PASS',result.stdout)
                    continue
                expected = list(parallel.V1_OPERATION_MODULES) if defect in ('none','reused') else list(parallel.V1_OPERATION_MODULES[:3])
                if defect=='reused':expected=expected[2:]
                self.assertEqual((root/'order').read_text().splitlines(), expected)
                self.assertEqual('LONGHORN_PARALLEL_V1_OPERATIONS_PASS' in result.stdout, defect in ('none','reused'))

    def test_remaining_operations_resume_only_unaccepted_original_scopes(self):
        expected = ['node-failure', 'network-failure', 'disk-failure', 'last-replica', 'middleware',
                    'system-restore', 'disaster-recovery', 'v1-upgrade-lifecycle', 'observability']
        for failed, missing, v2_failed in ((False, None, False), ('node-failure', None, False),
                                          (False, 'node-failure', False), (False, None, True)):
            with self.subTest(failed=failed, missing=missing, v2_failed=v2_failed), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-operations-remaining-v2-monitoring',
                    fail=failed, missing_marker=missing, v2_fail=v2_failed,
                    sequence_defect='remaining-node-receipt-absent')
                root = Path(directory)
                self.assertEqual(result.returncode == 0, not (failed or missing or v2_failed), result.stdout+result.stderr)
                order = (root/'v1-order').read_text().splitlines()
                self.assertEqual(order, expected[:1] if failed or missing else expected)
                self.assertNotIn('maintenance', order)
                self.assertNotIn('capacity', order)
                self.assertNotIn('integrity', order)
                self.assertNotIn('disk-lifecycle', order)
                self.assertNotIn('capacity-cleanup', order)
                self.assertNotIn('node-failure-cleanup', order)
                self.assertFalse((root/'clusters/longhorn-gate/longhorn-node-failure.json').exists())
                self.assertNotIn('stability', order)
                self.assertTrue((root/'v2-monitoring.started').exists())
                parent = next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
                if not failed and not missing:
                    self.assertEqual(parallel.verify_branch(parent, 'v1-operations-remaining-recovery')['rc'], 0)
                else:
                    with self.assertRaises(ValueError):
                        parallel.verify_branch(parent, 'v1-operations-remaining-recovery')
                if not v2_failed:
                    self.assertEqual(parallel.verify_branch(parent, 'v2-observability-monitoring')['rc'], 0)
                else:
                    with self.assertRaises(ValueError):
                        parallel.verify_branch(parent, 'v2-observability-monitoring')

    def test_remaining_retained_receipt_runs_original_cleanup_before_node_failure_and_keeps_v2_independent(self):
        for fail, missing, symlink in ((False, None, False), ('node-failure-cleanup', None, False),
                                       (False, 'node-failure-cleanup', False), (False, None, True)):
            with self.subTest(fail=fail, missing=missing, symlink=symlink), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-operations-remaining-v2-monitoring',
                    fail=fail, missing_marker=missing, sequence_defect='remaining-node-receipt-'+
                    ('symlink' if symlink else 'retained'))
                root = Path(directory)
                accepted = not (fail or missing or symlink)
                self.assertEqual(result.returncode == 0, accepted, result.stdout+result.stderr)
                order = (root/'v1-order').read_text().splitlines()
                self.assertEqual(order[:2] if accepted else order,
                                 ['node-failure-cleanup', 'node-failure'] if accepted else ['node-failure-cleanup'])
                self.assertEqual(order.count('node-failure-cleanup'), 1)
                self.assertTrue((root/'v2-monitoring.started').exists())
                parent = next(p for p in root.glob('parallel.*') if (p/'v1.log').exists())
                self.assertEqual(parallel.verify_branch(parent, 'v2-observability-monitoring')['rc'], 0)
                if accepted:
                    self.assertEqual(parallel.verify_branch(parent, 'v1-operations-remaining-recovery')['rc'], 0)
                else:
                    with self.assertRaises(ValueError):
                        parallel.verify_branch(parent, 'v1-operations-remaining-recovery')

    def test_remaining_cleanup_prefix_requires_exact_markers_order_and_durable_success(self):
        stage = 'LONGHORN_PARALLEL_V1_OPERATIONS_REMAINING_STAGE_'
        cleanup = [stage+'START stage=node-failure-cleanup', *parallel.MARKERS['node-failure-cleanup'],
                   stage+'PASS stage=node-failure-cleanup durable_rc=0 finalized=0']
        variants = [[], cleanup]
        variants += [cleanup[:i]+cleanup[i+1:] for i in range(len(cleanup))]
        variants += [cleanup[:i]+[cleanup[i]]+cleanup[i:] for i in range(len(cleanup))]
        variants += [list(reversed(cleanup)), cleanup+[cleanup[0]+' duplicate'],
                     cleanup[:-1]+[cleanup[-1].replace('finalized=0', 'finalized=1')],
                     ['LONGHORN_NODE_FAILURE_CLEANUP_UNKNOWN']]
        for branch in ('v1-operations-remaining', 'v1-operations-remaining-recovery'):
            for index, prefix in enumerate(variants):
                with self.subTest(branch=branch, variant=index), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    for suffix in ('.exit', '.finalized'):
                        f = root/('v1'+suffix); f.write_text('0\n'); f.chmod(0o600)
                    lines = prefix+[stage+'START stage=node-failure']+list(parallel.MARKERS[branch])
                    log = root/'v1.log'; log.write_text('\n'.join(lines)+'\n'); log.chmod(0o600)
                    if index < 2:
                        self.assertEqual(parallel.verify_branch(root, branch)['rc'], 0)
                    else:
                        with self.assertRaises(ValueError): parallel.verify_branch(root, branch)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for suffix in ('.exit', '.finalized'):
                    f = root/('v1'+suffix); f.write_text('0\n'); f.chmod(0o600)
                log = root/'v1.log'
                log.write_text('\n'.join([stage+'START stage=node-failure', *cleanup, *parallel.MARKERS[branch]])+'\n')
                log.chmod(0o600)
                with self.assertRaises(ValueError): parallel.verify_branch(root, branch)

    def test_operations_and_v2_platform_use_disjoint_parallel_children_and_stop_only_failed_scope(self):
        for fail_v1, fail_v2 in ((False, False), ('capacity', False), (False, True)):
            with self.subTest(v1=fail_v1, v2=fail_v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-operations-v2-platform-install',
                                             fail=fail_v1, v2_fail=fail_v2)
                self.assertEqual(result.returncode == 0, not (fail_v1 or fail_v2), result.stdout+result.stderr)
                root = Path(directory); parent = next(root.glob('parallel.*'))
                self.assertEqual((root/'v1-order').read_text().splitlines(),
                    ['maintenance', 'capacity'] if fail_v1 else list(parallel.V1_OPERATION_MODULES))
                if not fail_v1:
                    self.assertEqual(parallel.verify_branch(parent, 'v1-operations')['rc'], 0)
                if not fail_v2:
                    self.assertEqual(parallel.verify_branch(parent, 'v2-platform-install')['rc'], 0)
                    self.assertEqual((root/'v2-order').read_text().splitlines(),
                        ['v2-runtime-resources', 'v2-coexist-qualification', 'v2-maintenance-drain-qualification'])
                else:
                    self.assertFalse((root/'v2-order').exists())
                self.assertEqual('LONGHORN_V1_OPERATIONS_V2_PLATFORM_INSTALL_PASS' in result.stdout,
                                 not (fail_v1 or fail_v2))

    def test_ownership_reinstall_overlaps_linked_and_isolates_either_branch_failure(self):
        for fail_v1, fail_v2 in ((False, False), ('ownership', False), (False, True)):
            with self.subTest(v1=fail_v1, v2=fail_v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-ownership-v2-linked-acceptance',
                                             fail=fail_v1, v2_fail=fail_v2)
                self.assertEqual(result.returncode == 0, not (fail_v1 or fail_v2), result.stdout+result.stderr)
                parent = next(Path(directory).glob('parallel.*'))
                if not fail_v1:
                    self.assertEqual(parallel.verify_branch(parent, 'v1-ownership')['rc'], 0)
                if not fail_v2:
                    self.assertEqual(parallel.verify_branch(parent, 'v2-linked-acceptance')['rc'], 0)
                    linked = next(Path(directory).glob('linked-acceptance.*'))
                    self.assertEqual(parallel.verify_branch(linked, 'incomplete-clone')['rc'], 0)
                else:
                    linked = next(Path(directory).glob('linked-acceptance.*'))
                    self.assertFalse((linked/'incomplete-clone.log').exists())
                if fail_v1 or fail_v2:
                    self.assertNotIn('LONGHORN_V1_OWNERSHIP_V2_LINKED_ACCEPTANCE_PASS', result.stdout)

    def test_ownership_reinstall_overlaps_remaining_faults_and_preserves_each_real_exit(self):
        for fail_v1, fail_v2 in ((False, False), ('ownership', False), (False, True)):
            with self.subTest(v1=fail_v1, v2=fail_v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-ownership-v2-failure-remaining',
                                             fail=fail_v1, v2_fail=fail_v2)
                self.assertEqual(result.returncode == 0, not (fail_v1 or fail_v2), result.stdout+result.stderr)
                parent = next(Path(directory).glob('parallel.*'))
                for branch, failed in (('v1-ownership', fail_v1), ('v2-failure-remaining', fail_v2)):
                    if failed:
                        with self.assertRaises(ValueError): parallel.verify_branch(parent, branch)
                    else: self.assertEqual(parallel.verify_branch(parent, branch)['rc'], 0)
                self.assertEqual('LONGHORN_V1_OWNERSHIP_V2_FAILURE_REMAINING_PASS' in result.stdout,
                                 not (fail_v1 or fail_v2))

    def test_pending_recovery_and_runtime_overlap_with_independent_real_exits(self):
        for fail_v1,fail_v2 in ((False,False),('system-restore',False),(False,True)):
            with self.subTest(v1=fail_v1,v2=fail_v2),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-system-restore-v2-runtime-resources',fail=fail_v1,v2_fail=fail_v2)
                self.assertEqual(result.returncode==0,not(fail_v1 or fail_v2),result.stdout+result.stderr)
                root=Path(directory);parent=next(root.glob('parallel.*'))
                self.assertEqual((root/'v1-order').read_text().splitlines(),['system-restore'])
                if not fail_v2:self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-runtime-resources'])
                for branch,failed in (('system-restore',fail_v1),('v2-runtime-resources',fail_v2)):
                    if failed:
                        with self.assertRaises(ValueError):parallel.verify_branch(parent,branch)
                    else:self.assertEqual(parallel.verify_branch(parent,branch)['rc'],0)

    def test_pending_parallel_pair_preserves_actual_owner_prerequisites_and_cleanup(self):
        live=(ROOT/'tests/helpers/longhorn-live.sh').read_text()
        source=(ROOT/'tests/helpers/longhorn-system-recovery.sh').read_text()
        target=(ROOT/'tests/helpers/longhorn-v2-runtime-resources.sh').read_text()
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-system-recovery.sh" "$MODE"',live)
        self.assertIn('exec bash "$BASE/tests/helpers/longhorn-v2-runtime-resources.sh" "$MODE"',live)
        for text in (source,target):
            self.assertIn('LONGHORN_PARENT_LEASE_HELD',text)
            self.assertIn('trap finish EXIT' if text is source else 'trap runtime_exit EXIT',text)
            self.assertIn('relay-clean',text)
            self.assertIn('cleanup_rc',text)
        self.assertIn('clusters/longhorn-gate/kubectl.kubeconfig',source)
        self.assertIn('clusters/longhorn-v2-gate/kubectl.kubeconfig',target)
        self.assertNotIn('/root/.kube/config',target)
        self.assertNotIn('192.168.47.149',target)
        self.assertIn('[[ ! -e "$JOURNAL" && ! -L "$JOURNAL" ]]',source)
        self.assertIn('"$DRIVER" clean',source)
        self.assertIn('"$DRIVER" cleanup',target)
        self.assertIn('"$INSTALL" health',target)
        driver=(ROOT/'tests/helpers/longhorn-v2-runtime-resources.py').read_text()
        self.assertIn('runtime resource module refuses foreign volumes',driver)
        self.assertIn('runtime resource module requires previous scoped cleanup',driver)
        self.assertIn('canonical_before!=canonical_after',driver)

    def test_long_windows_overlap_and_failed_observation_keeps_stability(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-stability-v2-observability', v2_fail=fail)
                self.assertEqual(result.returncode == 0, not fail, result.stdout+result.stderr)
                parent = next(Path(directory).glob('parallel.*'))
                self.assertEqual(parallel.verify_branch(parent, 'v1-stability')['rc'], 0)
                if fail:
                    self.assertNotIn('LONGHORN_V1_STABILITY_V2_OBSERVABILITY_PASS', result.stdout)
                    with self.assertRaises(ValueError): parallel.verify_branch(parent, 'v2-observability')
                else:
                    self.assertEqual(parallel.verify_branch(parent, 'v2-observability')['rc'], 0)
                    self.assertIn('LONGHORN_V1_STABILITY_V2_OBSERVABILITY_PASS', result.stdout)

    def test_stability_continuation_reuses_or_refuses_before_starting_fresh_work(self):
        for disposition in ('reuse', 'absent', 'rejected'):
            with self.subTest(disposition=disposition), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-stability-v2-observability',
                                             resume_stability=disposition)
                root = Path(directory)
                self.assertTrue((root/'stability-reuse-called').exists())
                self.assertEqual((root/'stability-fresh').exists(), disposition == 'absent')
                self.assertEqual(result.returncode == 0, disposition != 'rejected', result.stdout+result.stderr)
                parent = next(root.glob('parallel.*'))
                self.assertEqual(parallel.verify_branch(parent, 'v2-observability')['rc'], 0)
                if disposition != 'rejected':
                    self.assertEqual(parallel.verify_branch(parent, 'v1-stability')['rc'], 0)

    def test_source_and_target_recovery_and_adoption_actually_overlap(self):
        for operation in ('reboot', 'adopt', 'revision-2-adopt', 'revision-3-adopt'):
            for fail in (False, True):
                with self.subTest(operation=operation, fail=fail), tempfile.TemporaryDirectory() as directory:
                    result = self.run_supervisor(directory, mode='v2-source-target-'+
                        {'reboot':'reboot-recovery', 'adopt':'im-adopt-qualification',
                         'revision-2-adopt':'im-revision-2-adopt-qualification', 'revision-3-adopt':'im-revision-3-adopt-qualification'}[operation], v2_fail=fail)
                    self.assertEqual(result.returncode == 0, not fail, result.stdout+result.stderr)
                    parent = next(Path(directory).glob('parallel.*'))
                    self.assertEqual(parallel.verify_branch(parent, 'source-'+operation)['rc'], 0)
                    if fail:
                        self.assertEqual((parent/'v2.finalized').read_text().strip(), '8')
                    else: self.assertEqual(parallel.verify_branch(parent, 'target-'+operation)['rc'], 0)

    def test_revision3_preflight_or_shared_cache_failure_starts_neither_cluster(self):
        for defect in ('revision3-preflight', 'cache'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,
                    mode='v2-source-target-im-revision-3-adopt-qualification',
                    sequence_defect=defect, cache_fail=defect=='cache')
                self.assertEqual(result.returncode, 11 if defect=='revision3-preflight' else 9)
                root=Path(directory)
                self.assertFalse((root/'v1.started').exists())
                self.assertFalse((root/'v2.started').exists())
                self.assertNotIn('LONGHORN_V2_SOURCE_TARGET_IM_REVISION_3_ADOPT_QUALIFICATION_PASS',result.stdout)

    def test_revision3_handoff_uses_real_adopter_preflight_and_independent_readback(self):
        source=(ROOT/'tests/helpers/longhorn-parallel.sh').read_text()
        block=source.split('  v2-source-target-im-revision-3-adopt-qualification)',1)[1].split('    ;;',1)[0]
        self.assertEqual(block.count('=longhorn-im-revision-3-adopt.sh'),2)
        self.assertIn('for engine in v2 v2-dr-target',block)
        self.assertLess(block.index('preflight --engine'),block.index('cache --engine'))
        adopter=(ROOT/'tests/helpers/longhorn-im-revision-3-adopt.sh').read_text()
        self.assertLess(adopter.index('preflight --engine'),adopter.index('phase=relay'))
        for predicate in ('"$core_step"', 'business --engine', 'finish --engine',
                          'verify --engine', 'readback --engine',
                          'INDEPENDENT_READBACK', 'SCOPED_CLEAN_PASS'):
            self.assertIn(predicate,adopter)
        reader=(ROOT/'tests/helpers/longhorn-im-revision-3-adopt.py').read_text()
        for predicate in ('completed exact dual-architecture publication evidence',
                          'independently accepted .2 installation',
                          'revision plan input changed or has no exact provenance'):
            self.assertIn(predicate,reader)

    def test_platform_and_two_environment_recoveries_keep_independent_outcomes(self):
        for fail in (False, True):
            with self.subTest(target_fail=fail), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-platform-v2-environment-recovery', v2_fail=fail)
                self.assertEqual(result.returncode == 0, not fail, result.stdout+result.stderr)
                parents = list(Path(directory).glob('parallel.*'))
                outer = next(p for p in parents if (p/'v1.log').read_text().startswith('LONGHORN_PLATFORM_'))
                inner = next(p for p in parents if p != outer)
                self.assertEqual(parallel.verify_branch(outer, 'v1-platform')['rc'], 0)
                self.assertEqual(parallel.verify_branch(inner, 'source-environment')['rc'], 0)
                if fail:
                    self.assertEqual((inner/'v2.exit').read_text().strip(), '8')
                    self.assertNotIn('LONGHORN_V1_PLATFORM_V2_ENVIRONMENT_RECOVERY_PASS', result.stdout)
                else:
                    self.assertEqual(parallel.verify_branch(inner, 'target-environment')['rc'], 0)
                    self.assertEqual(parallel.verify_branch(outer, 'v2-environment')['rc'], 0)

    def test_parent_can_stream_before_background_children_open_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, launch_delay=0.1)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            parent = next(Path(directory).glob('parallel.*'))
            for branch in ('v1', 'v2'):
                self.assertEqual(parallel.verify_branch(parent, branch)['rc'], 0)

    def test_propagation_preflight_overlaps_both_adoptions_and_has_independent_fences(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-platform-propagation-v2-im-adopt-qualification', v2_fail=failure)
                self.assertEqual(not failure, result.returncode == 0, result.stdout+result.stderr)
                parents = list(Path(directory).glob('parallel.*'))
                outer = next(p for p in parents if (p/'v1.log').read_text().startswith('LONGHORN_PLATFORM_PROPAGATION'))
                self.assertEqual(0, parallel.verify_branch(outer, 'v1-propagation')['rc'])
                if not failure: self.assertEqual(0, parallel.verify_branch(outer, 'v2-adoption')['rc'])
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        for mode in ('v1-platform-propagation-v2-im-adopt-qualification', 'platform-propagation-qualification'):
            self.assertIn('"$module" == '+mode+' ||', runner)
            result = subprocess.run(['bash', '-c', 'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                'contract', str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-'+mode], capture_output=True, text=True, check=True)
            self.assertEqual([mode, 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS'], result.stdout.splitlines())

    def test_manager_adoption_refusal_stops_linked_before_capture_while_v1_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, mode='v1-backups-v2-linked-acceptance',
                                         sequence_defect='manager-adopt')
            self.assertNotEqual(0, result.returncode, result.stdout+result.stderr)
            root = Path(directory)
            parent = next(root.glob('parallel.*'))
            self.assertEqual(0, parallel.verify_branch(parent, 'v1-backups')['rc'])
            linked = next(root.glob('linked-acceptance.*'))
            self.assertFalse((root/'manager.ready').exists())
            self.assertFalse(list(linked.glob('*.log')))
            self.assertNotIn('LONGHORN_V2_CLONE_BASELINE_BEFORE ', (parent/'v2.log').read_text())
            self.assertNotIn('LONGHORN_V2_LINKED_ACCEPTANCE_PASS', result.stdout)

    def test_linked_failures_stop_own_fault_chain_while_v1_finishes(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-backups-v2-linked-failure-acceptance', v2_fail=failed)
                self.assertEqual(not failed, result.returncode == 0, result.stdout+result.stderr)
                parent = next(Path(directory).glob('parallel.*'))
                self.assertEqual(0, parallel.verify_branch(parent, 'v1-backups')['rc'])
                linked = next(Path(directory).glob('linked-acceptance.*'))
                if failed:
                    self.assertFalse((linked/'failure.log').exists())
                else:
                    self.assertEqual(0, parallel.verify_branch(parent, 'v2-linked-failure-acceptance')['rc'])
                    self.assertEqual(0, parallel.verify_branch(linked, 'failure')['rc'])

    def test_full_backup_module_can_follow_accepted_propagation_with_the_same_linked_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, mode='v1-backups-v2-linked-acceptance')
            self.assertEqual(0, result.returncode, result.stdout+result.stderr)
            self.assertEqual(list(parallel.V1_BACKUP_MODULES), (Path(directory)/'v1-order').read_text().splitlines())
            parent = next(Path(directory).glob('parallel.*'))
            self.assertEqual(0, parallel.verify_branch(parent, 'v1-backups')['rc'])
            self.assertEqual(0, parallel.verify_branch(parent, 'v2-linked-acceptance')['rc'])

    def test_remaining_public_routes_require_both_pool_qualifications(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        allowed = re.search(r'case "\$module" in ([^)]+)\)', runner).group(1).split('|')
        leased = re.search(r'case "\$mode" in ([^)]+)\) ;; \*\) exit 2', runner).group(1).split('|')
        for mode, marker in (('v1-backups-v2-linked-failure-acceptance', 'LONGHORN_V1_BACKUPS_V2_LINKED_FAILURE_ACCEPTANCE_PASS'),
                             ('v1-backups-v2-linked-acceptance', 'LONGHORN_V1_BACKUPS_V2_LINKED_ACCEPTANCE_PASS'),
                             ('v1-backups-remaining', 'LONGHORN_PARALLEL_V1_BACKUPS_REMAINING_PASS'),
                             ('v1-remaining-v2-linked-acceptance', 'LONGHORN_V1_REMAINING_V2_LINKED_ACCEPTANCE_PASS')):
            self.assertIn(mode, allowed); self.assertIn(mode, leased)
            self.assertIn('"$module" == '+mode+' ||', runner)
            result = subprocess.run(['bash', '-c', 'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                'contract', str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-'+mode], capture_output=True, text=True, check=True)
            self.assertEqual([mode, marker], result.stdout.splitlines())

    def test_remaining_backup_batch_and_linked_chain_are_independent_and_ordered(self):
        for failed_v1, failed_v2 in ((False, False), ('backup', False), (False, True)):
            with self.subTest(v1=failed_v1, v2=failed_v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-remaining-v2-linked-acceptance', fail=failed_v1, v2_fail=failed_v2)
                self.assertEqual(not (failed_v1 or failed_v2), result.returncode == 0, result.stdout+result.stderr)
                order = (Path(directory)/'v1-order').read_text().splitlines()
                self.assertEqual(['security', 'backup'] if failed_v1 else list(parallel.V1_BACKUP_MODULES[1:]), order)
                parent = next(Path(directory).glob('parallel.*'))
                if not failed_v1: self.assertEqual(0, parallel.verify_branch(parent, 'v1-backups-remaining')['rc'])
                if not failed_v2:
                    self.assertEqual(0, parallel.verify_branch(parent, 'v2-linked-acceptance')['rc'])
                    linked = next(Path(directory).glob('linked-acceptance.*'))
                    for branch in ('linked-clone', 'linked-lifecycle'):
                        self.assertEqual(0, parallel.verify_branch(linked, branch)['rc'])
                else:
                    linked = next(Path(directory).glob('linked-acceptance.*'))
                    self.assertFalse((linked/'linked-lifecycle.log').exists())
                self.assertNotIn('platform', order)

    def test_branches_overlap_and_v1_remains_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('LONGHORN_V1_V2_CONTROL_QUALIFICATION_PASS', result.stdout)
            self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(), ['idempotence', 'security'])

    def test_failed_v1_stops_its_next_case_and_preserves_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, fail=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('LONGHORN_V1_V2_CONTROL_QUALIFICATION_PASS', result.stdout)
            self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(), ['idempotence'])
            self.assertTrue((Path(directory)/'v2.completed').exists())
            work = next(Path(directory).glob('parallel.*'))
            self.assertEqual((work/'v1.exit').read_text().strip(), '7')
            self.assertEqual((work/'v2.exit').read_text().strip(), '0')
            self.assertEqual((work/'v2.finalized').read_text().strip(), '0')

    def test_second_v1_failure_preserves_first_case_durable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, fail='security')
            self.assertNotEqual(result.returncode, 0)
            child = next(Path(directory).glob('parallel-v1.*'))
            self.assertEqual(parallel.verify_branch(child, 'idempotence')['rc'], 0)
            self.assertEqual((child/'security.exit').read_text().strip(), '7')
            self.assertTrue((Path(directory)/'v2.completed').exists())
            self.assertIn('LONGHORN_PARALLEL_BRANCH_LOG_BEGIN branch=v1', result.stdout)
            self.assertIn('LONGHORN_PARALLEL_BRANCH_LOG_END branch=v2', result.stdout)

    def test_both_engine_images_are_adopted_concurrently_and_have_separate_durable_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, mode='v1-v2-im-adopt-qualification')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('LONGHORN_V1_V2_IM_ADOPT_QUALIFICATION_PASS', result.stdout)
            self.assertEqual((Path(directory)/'cache-calls').read_text().splitlines(), ['cache'])
            self.assertIn('LONGHORN_PARALLEL_IM_CACHE_READY', result.stdout)
            parent = next(Path(directory).glob('parallel.*'))
            for branch in ('v1-adopt', 'v2-adopt'):
                self.assertEqual(parallel.verify_branch(parent, branch)['rc'], 0)

    def test_failed_shared_image_cache_starts_neither_adoption_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, mode='v1-v2-im-adopt-qualification', cache_fail=True)
            self.assertEqual(result.returncode, 9)
            self.assertFalse((Path(directory)/'v1.started').exists())
            self.assertFalse((Path(directory)/'v2.started').exists())
            self.assertNotIn('LONGHORN_V1_V2_IM_ADOPT_QUALIFICATION_PASS', result.stdout)

    def test_failed_v2_adoption_preserves_completed_v1_without_claiming_parent_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, mode='v1-v2-im-adopt-qualification', v2_fail=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('LONGHORN_V1_V2_IM_ADOPT_QUALIFICATION_PASS', result.stdout)
            parent = next(Path(directory).glob('parallel.*'))
            self.assertEqual(parallel.verify_branch(parent, 'v1-adopt')['rc'], 0)
            self.assertEqual((parent/'v2.exit').read_text().strip(), '8')

    def test_backup_batch_overlaps_linked_and_uninstalls_last(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, mode='v1-backups-v2-linked-qualification')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('LONGHORN_V1_BACKUPS_V2_LINKED_QUALIFICATION_PASS', result.stdout)
            self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(),
                             list(parallel.V1_BACKUP_MODULES))
            parent = next(Path(directory).glob('parallel.*'))
            self.assertEqual(parallel.verify_branch(parent, 'v1-backups')['rc'], 0)
            self.assertEqual(parallel.verify_branch(parent, 'v2-linked')['rc'], 0)
            child = next(Path(directory).glob('parallel-v1.*'))
            for branch in parallel.V1_BACKUP_MODULES:
                self.assertEqual(parallel.verify_branch(child, branch)['rc'], 0)

    def test_backup_batch_stops_before_uninstall_and_preserves_linked_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, fail='encryption',
                                         mode='v1-backups-v2-linked-qualification')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(),
                             ['platform', 'security', 'backup', 'backup-refusal', 'recurring', 'encryption'])
            parent = next(Path(directory).glob('parallel.*'))
            self.assertEqual(parallel.verify_branch(parent, 'v2-linked')['rc'], 0)
            child = next(Path(directory).glob('parallel-v1.*'))
            for branch in ('platform', 'security', 'backup', 'backup-refusal', 'recurring'):
                self.assertEqual(parallel.verify_branch(child, branch)['rc'], 0)
            self.assertFalse((child/'uninstall.pid').exists())

    def test_platform_and_security_prerequisites_stop_only_their_dependent_v1_path(self):
        for failed, completed in (('platform', ()), ('security', ('platform',))):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, fail=failed,
                                             mode='v1-backups-v2-linked-qualification')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(),
                                 [*completed, failed])
                parent = next(Path(directory).glob('parallel.*'))
                self.assertEqual(parallel.verify_branch(parent, 'v2-linked')['rc'], 0)
                child = next(Path(directory).glob('parallel-v1.*'))
                for module in completed:
                    self.assertEqual(parallel.verify_branch(child, module)['rc'], 0)
                self.assertFalse((child/'backup.pid').exists())

    def test_platform_uses_its_explicit_context_without_mutating_the_shared_default(self):
        source = (ROOT/'tests/helpers/longhorn-platform.sh').read_text()
        self.assertIn('export KUBECONFIG="$BASE/clusters/longhorn-gate/kubectl.kubeconfig"', source)
        self.assertNotIn('/root/.kube/config', source)
        self.assertEqual(parallel.V1_BACKUP_MODULES[:2], ('platform', 'security'))

    def test_missing_full_module_clean_marker_blocks_remaining_backup_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, missing_marker='backup-refusal',
                                         mode='v1-backups-v2-linked-qualification')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(),
                             ['platform', 'security', 'backup', 'backup-refusal'])
            child = next(Path(directory).glob('parallel-v1.*'))
            self.assertEqual((child/'backup-refusal.exit').read_text().strip(), '0')
            with self.assertRaisesRegex(ValueError, 'markers differ'):
                parallel.verify_branch(child, 'backup-refusal')
            parent = next(Path(directory).glob('parallel.*'))
            self.assertEqual(parallel.verify_branch(parent, 'v2-linked')['rc'], 0)

    def test_linked_failure_preserves_all_completed_v1_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_supervisor(directory, v2_fail=True,
                                         mode='v1-backups-v2-linked-qualification')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((Path(directory)/'v1-order').read_text().splitlines(),
                             list(parallel.V1_BACKUP_MODULES))
            parent = next(Path(directory).glob('parallel.*'))
            self.assertEqual(parallel.verify_branch(parent, 'v1-backups')['rc'], 0)
            self.assertEqual((parent/'v2.finalized').read_text().strip(), '8')
            self.assertNotIn('LONGHORN_V1_BACKUPS_V2_LINKED_QUALIFICATION_PASS', result.stdout)

    def test_only_exact_v1_modes_skip_shared_context_mutation(self):
        source = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        before = source.split('    parallel_context=no\n', 1)[1].split('    phase=transport\n', 1)[0]
        arm = before.split('    else\n', 1)[0]
        modes = re.search(r'case "\$MODE" in ([^)]+)\)', arm).group(1).split('|')
        self.assertEqual(set(modes), {'idempotence', 'security', 'backup', 'backup-refusal',
                                     'recurring', 'encryption', 'uninstall'})
        self.assertIn('context-fingerprint', arm)
        self.assertNotIn('cp -a', arm)
        cleanup = source.split('      if [[ "$parallel_context" == yes ]]; then\n', 1)[1].split('      elif ', 1)[0]
        self.assertIn('context-verify', cleanup)
        self.assertNotIn('cp -a', cleanup)
        self.assertNotIn('rm -f', cleanup)


    def resume_orders(self):
        v1=['ownership-controller-refusal-qualification','ownership-request-refusal-qualification',
            *parallel.V1_OPERATION_MODULES,'system-restore','disaster-recovery','v1-upgrade-lifecycle','observability']
        v2=['v2-failure-'+case for case in ('remote-spdk','replica-network','worker-reboot','single-disk','rebuild-qos','all-replicas')]
        v2+=['v2-runtime-resources','v2-coexist-qualification','v2-maintenance-drain-qualification','v2-middleware','v2-interrupt-qualification']
        return v1,v2

    def resume_parent(self,directory):
        return next(p for p in Path(directory).glob('parallel.*') if (p/'v1.log').exists())

    def test_resume_executes_only_unfinished_owning_scopes_once_with_independent_durable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            previous=Path(directory)/'previous-failed-parent';previous.mkdir()
            original={'v1.log':'first controller preflight failure\n','v1.exit':'1\n','v2.exit':'1\n','v2.finalized':'1\n'}
            for name,value in original.items():(previous/name).write_text(value)
            result=self.run_supervisor(directory,mode='v1-ownership-tail-v2-failure-tail')
            self.assertEqual({name:(previous/name).read_text() for name in original},original)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            root=Path(directory);v1,v2=self.resume_orders()
            self.assertEqual((root/'v1-order').read_text().splitlines(),v1)
            self.assertEqual((root/'v2-order').read_text().splitlines(),v2)
            parent=self.resume_parent(directory)
            for branch in ('v1-ownership-tail-operations','v2-failure-tail-platform'):
                self.assertEqual(parallel.verify_branch(parent,branch)['rc'],0)
            for profile in ('v1','v2'):
                sequence=next(p for p in root.glob('parallel-sequence.*') if any(p.glob('*/'+profile+'.log')))
                for stage in sequence.iterdir():
                    if stage.is_dir():
                        self.assertEqual((stage/(profile+'.exit')).read_text(),'0\n')
                        self.assertEqual((stage/(profile+'.finalized')).read_text(),'0\n')
            self.assertTrue((root/'v1.started').exists());self.assertTrue((root/'v2.started').exists())
            self.assertEqual(result.stdout.count('LONGHORN_V1_OWNERSHIP_TAIL_V2_FAILURE_TAIL_PASS'),1)

    def test_resume_v1_first_failure_or_missing_cleanup_stops_only_its_dependents(self):
        v1,v2=self.resume_orders()
        cases=(('ownership-controller-refusal-qualification',None),('ownership-request-refusal-qualification',None),
               (False,'ownership-request-refusal-qualification'))
        for fail,missing in cases:
            with self.subTest(fail=fail,missing=missing),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-ownership-tail-v2-failure-tail',fail=fail,missing_marker=missing)
                self.assertNotEqual(result.returncode,0)
                root=Path(directory);stop=fail or missing
                self.assertEqual((root/'v1-order').read_text().splitlines(),v1[:v1.index(stop)+1])
                self.assertEqual((root/'v2-order').read_text().splitlines(),v2)
                parent=self.resume_parent(directory)
                self.assertEqual(parallel.verify_branch(parent,'v2-failure-tail-platform')['rc'],0)
                with self.assertRaises(ValueError):parallel.verify_branch(parent,'v1-ownership-tail-operations')
                self.assertNotIn('LONGHORN_V1_OWNERSHIP_TAIL_V2_FAILURE_TAIL_PASS',result.stdout)

    def test_resume_remote_owning_scope_requires_business_cleanup_unique_markers_and_real_durable_fences(self):
        v1,_=self.resume_orders()
        for defect in ('nonzero','clean','terminal','stage','finalized'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-ownership-tail-v2-failure-tail',sequence_defect='resume-remote-'+defect)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                root=Path(directory)
                self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-failure-remote-spdk'])
                self.assertEqual((root/'v1-order').read_text().splitlines(),v1)
                parent=self.resume_parent(directory)
                self.assertEqual(parallel.verify_branch(parent,'v1-ownership-tail-operations')['rc'],0)
                self.assertNotIn('LONGHORN_PARALLEL_V2_FAILURE_TAIL_PLATFORM_STAGE_PASS stage=v2-failure-remote-spdk',result.stdout)
                with self.assertRaises(ValueError):parallel.verify_branch(parent,'v2-failure-tail-platform')

    def test_resume_next_fault_failure_preserves_remote_acceptance_and_does_not_repeat_it(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-ownership-tail-v2-failure-tail',sequence_defect='resume-network-nonzero')
            self.assertNotEqual(result.returncode,0)
            root=Path(directory)
            self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-failure-remote-spdk','v2-failure-replica-network'])
            self.assertEqual(result.stdout.splitlines().count('LONGHORN_PARALLEL_V2_FAILURE_TAIL_PLATFORM_STAGE_PASS stage=v2-failure-remote-spdk durable_rc=0 finalized=0'),1)
            self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v1-ownership-tail-operations')['rc'],0)

    def test_resume_changed_shared_context_blocks_request_while_the_v2_branch_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-ownership-tail-v2-failure-tail',sequence_defect='resume-context')
            self.assertNotEqual(result.returncode,0)
            root=Path(directory)
            self.assertEqual((root/'v1-order').read_text().splitlines(),['ownership-controller-refusal-qualification'])
            self.assertEqual((root/'v2-order').read_text().splitlines(),self.resume_orders()[1])
            self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v2-failure-tail-platform')['rc'],0)

    def worker_tail_orders(self):
        v1=[*parallel.V1_OPERATION_MODULES,'system-restore','disaster-recovery','v1-upgrade-lifecycle','observability']
        v2=['v2-failure-cleanup']+['v2-failure-'+case for case in ('worker-reboot','single-disk','rebuild-qos','all-replicas')]
        v2+=['v2-runtime-resources','v2-coexist-qualification','v2-maintenance-drain-qualification','v2-middleware','v2-interrupt-qualification']
        return v1,v2

    def test_worker_tail_keeps_old_failure_and_runs_only_original_remaining_scopes_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            previous=Path(directory)/'previous-failed-parent';previous.mkdir()
            original={'v1.log':'original maintenance nonconvergence\n','v1.exit':'143\n','v2.exit':'143\n','v2.finalized':'143\n'}
            for name,value in original.items():(previous/name).write_text(value)
            result=self.run_supervisor(directory,mode='v1-operations-v2-worker-tail')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual({name:(previous/name).read_text() for name in original},original)
            root=Path(directory);v1,v2=self.worker_tail_orders()
            self.assertEqual((root/'v1-order').read_text().splitlines(),v1)
            self.assertEqual((root/'v2-order').read_text().splitlines(),v2)
            self.assertTrue((root/'v1.started').exists());self.assertTrue((root/'v2.started').exists())
            parent=self.resume_parent(directory)
            for branch in ('v1-operations-recovery','v2-worker-tail-platform'):
                self.assertEqual(parallel.verify_branch(parent,branch)['rc'],0)
            cleanup_sequence=next(p for p in root.glob('parallel-sequence.*') if (p/'v2-failure-cleanup').is_dir())
            cleanup=cleanup_sequence/'v2-failure-cleanup'
            self.assertEqual(parallel.verify_branch(cleanup,'v2-failure-cleanup')['rc'],0)
            self.assertNotIn('LONGHORN_V2_FAILURE_STAGE_PASS', (cleanup/'v2.log').read_text())
            self.assertEqual(result.stdout.count('LONGHORN_V1_OPERATIONS_V2_WORKER_TAIL_PASS'),1)

    def test_worker_tail_maintenance_failure_or_missing_cleanup_stops_only_v1_dependents(self):
        v1,v2=self.worker_tail_orders()
        for fail,missing in (('maintenance',None),(False,'maintenance')):
            with self.subTest(fail=fail,missing=missing),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-operations-v2-worker-tail',fail=fail,missing_marker=missing)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                root=Path(directory)
                self.assertEqual((root/'v1-order').read_text().splitlines(),['maintenance'])
                self.assertEqual((root/'v2-order').read_text().splitlines(),v2)
                parent=self.resume_parent(directory)
                self.assertEqual(parallel.verify_branch(parent,'v2-worker-tail-platform')['rc'],0)
                with self.assertRaises(ValueError):parallel.verify_branch(parent,'v1-operations-recovery')
                self.assertNotIn('LONGHORN_V1_OPERATIONS_V2_WORKER_TAIL_PASS',result.stdout)

    def test_worker_tail_cleanup_failure_stops_before_worker_and_preserves_independent_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-operations-v2-worker-tail',sequence_defect='resume-cleanup-nonzero')
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            root=Path(directory)
            self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-failure-cleanup'])
            self.assertEqual((root/'v1-order').read_text().splitlines(),self.worker_tail_orders()[0])
            parent=self.resume_parent(directory)
            self.assertEqual(parallel.verify_branch(parent,'v1-operations-recovery')['rc'],0)
            with self.assertRaises(ValueError):parallel.verify_branch(parent,'v2-worker-tail-platform')
            self.assertNotIn('LONGHORN_V1_OPERATIONS_V2_WORKER_TAIL_PASS',result.stdout)

    def test_worker_tail_first_fault_requires_original_business_cleanup_unique_markers_and_durable_fences(self):
        for defect in ('nonzero','clean','terminal','stage','finalized'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-operations-v2-worker-tail',sequence_defect='resume-worker-'+defect)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                root=Path(directory)
                self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-failure-cleanup','v2-failure-worker-reboot'])
                self.assertEqual((root/'v1-order').read_text().splitlines(),self.worker_tail_orders()[0])
                parent=self.resume_parent(directory)
                self.assertEqual(parallel.verify_branch(parent,'v1-operations-recovery')['rc'],0)
                self.assertNotIn('LONGHORN_PARALLEL_V2_WORKER_TAIL_PLATFORM_STAGE_PASS stage=v2-failure-worker-reboot',result.stdout)
                with self.assertRaises(ValueError):parallel.verify_branch(parent,'v2-worker-tail-platform')

    def test_worker_tail_later_fault_failure_keeps_preceding_acceptance_without_repeating_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-operations-v2-worker-tail',sequence_defect='resume-disk-nonzero')
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            root=Path(directory)
            self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-failure-cleanup','v2-failure-worker-reboot','v2-failure-single-disk'])
            self.assertEqual(result.stdout.splitlines().count('LONGHORN_PARALLEL_V2_WORKER_TAIL_PLATFORM_STAGE_PASS stage=v2-failure-worker-reboot durable_rc=0 finalized=0'),1)
            self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v1-operations-recovery')['rc'],0)

    def test_worker_tail_changed_shared_context_stops_before_system_restore_and_preserves_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-operations-v2-worker-tail',sequence_defect='operations-context')
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            root=Path(directory)
            self.assertEqual((root/'v1-order').read_text().splitlines(),list(parallel.V1_OPERATION_MODULES))
            self.assertEqual((root/'v2-order').read_text().splitlines(),self.worker_tail_orders()[1])
            self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v2-worker-tail-platform')['rc'],0)

    def disk_tail_orders(self):
        v1=['maintenance-cleanup',*self.worker_tail_orders()[0]]
        v2=[mode for mode in self.worker_tail_orders()[1] if mode not in ('v2-failure-cleanup','v2-failure-worker-reboot')]
        return v1,v2

    def test_delayed_peer_start_does_not_replace_the_requested_disk_fault_with_a_fixture_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-operations-v2-disk-tail',
                sequence_defect='disk-tail-first-nonzero',startup_delay=1.25)
            root=Path(directory)
            self.assertNotEqual(0,result.returncode)
            self.assertTrue((root/'v2-order').exists(),result.stdout+result.stderr)
            self.assertEqual(['v2-failure-single-disk'],(root/'v2-order').read_text().splitlines())
            self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v1-operations-cleanup-recovery')['rc'],0)

    def test_disk_tail_recovers_v1_and_runs_only_three_remaining_faults_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            previous=Path(directory)/'previous-failed-parent';previous.mkdir()
            original={'v1.log':'original cleanup failure\n','v1.exit':'1\n','v2.exit':'1\n'}
            for name,value in original.items():(previous/name).write_text(value)
            result=self.run_supervisor(directory,mode='v1-operations-v2-disk-tail')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            root=Path(directory);v1,v2=self.disk_tail_orders()
            self.assertEqual((root/'v1-order').read_text().splitlines(),v1)
            self.assertEqual((root/'v2-order').read_text().splitlines(),v2)
            self.assertEqual({name:(previous/name).read_text() for name in original},original)
            self.assertTrue((root/'v1.started').exists());self.assertTrue((root/'v2.started').exists())
            for branch in ('v1-operations-cleanup-recovery','v2-disk-tail-platform'):
                self.assertEqual(parallel.verify_branch(self.resume_parent(directory),branch)['rc'],0)
            self.assertEqual(result.stdout.splitlines().count('LONGHORN_V1_OPERATIONS_V2_DISK_TAIL_PASS'),1)

    def test_disk_tail_v1_cleanup_failure_or_missing_marker_stops_only_v1(self):
        for fail,missing in (('maintenance-cleanup',None),(False,'maintenance-cleanup')):
            with self.subTest(fail=fail,missing=missing),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-operations-v2-disk-tail',fail=fail,missing_marker=missing)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                root=Path(directory)
                self.assertEqual((root/'v1-order').read_text().splitlines(),['maintenance-cleanup'])
                self.assertEqual((root/'v2-order').read_text().splitlines(),self.disk_tail_orders()[1])
                self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v2-disk-tail-platform')['rc'],0)
                self.assertNotIn('LONGHORN_V1_OPERATIONS_V2_DISK_TAIL_PASS',result.stdout)

    def test_disk_tail_first_fault_requires_business_cleanup_unique_markers_and_durable_fences(self):
        for defect in ('nonzero','clean','terminal','stage','finalized'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-operations-v2-disk-tail',sequence_defect='disk-tail-first-'+defect)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                root=Path(directory)
                self.assertEqual((root/'v2-order').read_text().splitlines(),['v2-failure-single-disk'])
                self.assertEqual((root/'v1-order').read_text().splitlines(),self.disk_tail_orders()[0])
                self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v1-operations-cleanup-recovery')['rc'],0)
                with self.assertRaises(ValueError):parallel.verify_branch(self.resume_parent(directory),'v2-disk-tail-platform')
                self.assertNotIn('LONGHORN_V1_OPERATIONS_V2_DISK_TAIL_PASS',result.stdout)

    def test_disk_tail_context_change_stops_only_v1_dependents(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-operations-v2-disk-tail',sequence_defect='operations-context')
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            root=Path(directory)
            self.assertEqual((root/'v1-order').read_text().splitlines(),['maintenance-cleanup',*parallel.V1_OPERATION_MODULES])
            self.assertEqual((root/'v2-order').read_text().splitlines(),self.disk_tail_orders()[1])
            self.assertEqual(parallel.verify_branch(self.resume_parent(directory),'v2-disk-tail-platform')['rc'],0)

    def test_only_pending_tail_modules_overlap_with_real_exit_boundaries(self):
        for v1, v2 in ((False, False), ('disaster-recovery', False), (False, True)):
            with self.subTest(v1=v1, v2=v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-recovery-tail-v2-install-tail',
                                             fail=v1, v2_fail=v2)
                self.assertEqual(result.returncode == 0, not (v1 or v2), result.stdout+result.stderr)
                root = Path(directory)
                parent = next(root.glob('parallel.*'))
                expected = ['system-restore-prepared-recovery', 'disaster-recovery'] if v1 else [
                    'system-restore-prepared-recovery', 'disaster-recovery', 'v1-upgrade-lifecycle', 'observability-support', 'observability']
                self.assertEqual((root/'v1-order').read_text().splitlines(), expected)
                if not v2:
                    self.assertEqual((root/'v2-order').read_text().splitlines(),
                                     ['v2-runtime-resources', 'v2-coexist-qualification', 'v2-maintenance-drain-qualification',
                                      'v2-middleware', 'v2-interrupt-qualification', 'v2-observability-monitoring'])
                for branch, failed in (('v1-recovery-tail', v1), ('v2-ready-tail', v2)):
                    if failed:
                        with self.assertRaises(ValueError): parallel.verify_branch(parent, branch)
                    else:
                        self.assertEqual(parallel.verify_branch(parent, branch)['rc'], 0)

    def test_actual_original_owner_refusals_stop_only_dependent_tail(self):
        for defect, reason, failed, passed in (
                ('tail-prepared-checkpoint-invalid', 'exact pre-refusal checkpoint',
                 'v1-recovery-tail', 'v2-ready-tail'),
                ('tail-target-fixture-retained', 'existing or foreign recovery target fixture',
                 'v1-recovery-tail', 'v2-ready-tail'),
                ('tail-coexist-config-retained', 'idle V2-only configuration',
                 'v2-ready-tail', 'v1-recovery-tail')):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-recovery-tail-v2-install-tail',
                                             sequence_defect=defect)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stdout+result.stderr)
                parent = next(Path(directory).glob('parallel.*'))
                with self.assertRaises(ValueError): parallel.verify_branch(parent, failed)
                self.assertEqual(parallel.verify_branch(parent, passed)['rc'], 0)

    def test_dr_and_install_tail_exclude_only_blocked_or_finished_scopes(self):
        for v1,v2 in ((False,False),('disaster-recovery',False),(False,True)):
            with self.subTest(v1=v1,v2=v2),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-dr-tail-v2-install-tail',fail=v1,v2_fail=v2)
                self.assertEqual(result.returncode==0,not (v1 or v2),result.stdout+result.stderr)
                root=Path(directory);parent=next(root.glob('parallel.*'))
                self.assertEqual((root/'v1-order').read_text().splitlines(),['disaster-recovery'] if v1 else
                    ['disaster-recovery','v1-upgrade-lifecycle','observability-support','observability'])
                if not v2:self.assertEqual((root/'v2-order').read_text().splitlines(),
                    ['v2-coexist-qualification','v2-maintenance-drain-qualification','v2-middleware',
                     'v2-interrupt-qualification','v2-observability-monitoring'])
                for branch,failed in (('v1-dr-tail',v1),('v2-install-ready-tail',v2)):
                    if failed:
                        with self.assertRaises(ValueError):parallel.verify_branch(parent,branch)
                    else:self.assertEqual(parallel.verify_branch(parent,branch)['rc'],0)

    def test_dr_and_install_tail_preserve_actual_target_and_drain_refusals(self):
        for defect,reason,failed,passed in (
                ('tail-target-fixture-retained','existing or foreign recovery target fixture','v1-dr-tail','v2-install-ready-tail'),
                ('tail-coexist-config-retained','idle V2-only configuration','v2-install-ready-tail','v1-dr-tail')):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-dr-tail-v2-install-tail',sequence_defect=defect)
                self.assertNotEqual(result.returncode,0);self.assertIn(reason,result.stdout+result.stderr)
                parent=next(Path(directory).glob('parallel.*'))
                with self.assertRaises(ValueError):parallel.verify_branch(parent,failed)
                self.assertEqual(parallel.verify_branch(parent,passed)['rc'],0)

    def test_upgrade_tail_skips_accepted_dr_and_preserves_independent_failure_boundaries(self):
        for v1, v2 in ((False, False), ('v1-upgrade-lifecycle', False), (False, True)):
            with self.subTest(v1=v1, v2=v2), tempfile.TemporaryDirectory() as directory:
                result = self.run_supervisor(directory, mode='v1-upgrade-tail-v2-install-tail', fail=v1, v2_fail=v2)
                self.assertEqual(result.returncode == 0, not (v1 or v2), result.stdout+result.stderr)
                root = Path(directory); parent = next(root.glob('parallel.*'))
                self.assertEqual((root/'v1-order').read_text().splitlines(), ['v1-upgrade-lifecycle'] if v1 else
                                 ['v1-upgrade-lifecycle', 'observability-support', 'observability'])
                if not v2:
                    self.assertEqual((root/'v2-order').read_text().splitlines(),
                                     ['v2-coexist-qualification', 'v2-maintenance-drain-qualification',
                                      'v2-middleware', 'v2-interrupt-qualification', 'v2-observability-monitoring'])
                for branch, failed in (('v1-upgrade-tail', v1), ('v2-install-ready-tail', v2)):
                    if failed:
                        with self.assertRaises(ValueError): parallel.verify_branch(parent, branch)
                    else: self.assertEqual(0, parallel.verify_branch(parent, branch)['rc'])

    def test_upgrade_and_maintenance_tail_skip_only_independently_accepted_coexistence(self):
        for v1,v2 in ((False,False),('v1-upgrade-lifecycle',False),(False,True)):
            with self.subTest(v1=v1,v2=v2),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-upgrade-tail-v2-maintenance-tail',fail=v1,v2_fail=v2)
                self.assertEqual(result.returncode==0,not(v1 or v2),result.stdout+result.stderr)
                root=Path(directory);parent=next(root.glob('parallel.*'))
                self.assertEqual((root/'v1-order').read_text().splitlines(),['v1-upgrade-lifecycle'] if v1 else
                    ['v1-upgrade-lifecycle','observability-support','observability'])
                if not v2:self.assertEqual((root/'v2-order').read_text().splitlines(),
                    ['v2-maintenance-drain-qualification','v2-middleware','v2-interrupt-qualification','v2-observability-monitoring'])
                for branch,failed in (('v1-upgrade-tail',v1),('v2-maintenance-tail',v2)):
                    if failed:
                        with self.assertRaises(ValueError):parallel.verify_branch(parent,branch)
                    else:self.assertEqual(0,parallel.verify_branch(parent,branch)['rc'])

    def test_maintenance_tail_keeps_real_idle_profile_refusal_and_independent_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            result=self.run_supervisor(directory,mode='v1-upgrade-tail-v2-maintenance-tail',sequence_defect='tail-coexist-config-retained')
            self.assertNotEqual(0,result.returncode);self.assertIn('idle V2-only configuration',result.stdout+result.stderr)
            parent=next(Path(directory).glob('parallel.*'))
            with self.assertRaises(ValueError):parallel.verify_branch(parent,'v2-maintenance-tail')
            self.assertEqual(0,parallel.verify_branch(parent,'v1-upgrade-tail')['rc'])

    def test_monitoring_and_resource_tail_overlap_only_unfinished_scopes(self):
        for failed_v1, failed_v2 in ((False, False), ('observability-image-cache', False), (False, True)):
            with self.subTest(v1=failed_v1,v2=failed_v2),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-monitoring-v2-resource-tail',fail=failed_v1,v2_fail=failed_v2)
                self.assertEqual(result.returncode==0,not(failed_v1 or failed_v2),result.stdout+result.stderr)
                root=Path(directory);parent=next(root.glob('parallel.*'))
                self.assertEqual((root/'v1-order').read_text().splitlines(),['observability-image-cache'] if failed_v1 else
                    ['observability-image-cache','observability-cleanup','observability'])
                if not failed_v2:self.assertEqual((root/'v2-order').read_text().splitlines(),
                    ['v2-runtime-resources','v2-middleware','v2-interrupt-qualification','v2-observability-monitoring'])
                if not failed_v2:
                    stage=next(root.glob('parallel-sequence.*/v2-runtime-resources'))
                    self.assertEqual((stage/'baseline-before.json').read_bytes(),
                                     (stage/'baseline-after.json').read_bytes())
                    for name in ('owning.exit','owning.finalized','durable-exit','durable-finalized'):
                        self.assertEqual('0', (stage/name).read_text().strip())
                    self.assertEqual(1, (stage/'gate.log').read_text().splitlines().count('LONGHORN_GATE_EXIT rc=0'))
                for branch,failed in (('v1-monitoring-tail',failed_v1),('v2-resource-tail',failed_v2)):
                    if failed:
                        with self.assertRaises(ValueError):parallel.verify_branch(parent,branch)
                    else:self.assertEqual(parallel.verify_branch(parent,branch)['rc'],0)

    def test_monitoring_tail_respects_actual_cleanup_and_resource_refusals(self):
        for defect,reason,failed,passed in (
                ('monitoring-tail-pending','unused independent V2 product baseline','v1-monitoring-tail','v2-resource-tail'),
                ('resource-tail-config','default hugepage/polling/mask/pools baseline','v2-resource-tail','v1-monitoring-tail')):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as directory:
                result=self.run_supervisor(directory,mode='v1-monitoring-v2-resource-tail',sequence_defect=defect)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn(reason,result.stdout+result.stderr)
                parent=next(Path(directory).glob('parallel.*'))
                with self.assertRaises(ValueError):parallel.verify_branch(parent,failed)
                self.assertEqual(parallel.verify_branch(parent,passed)['rc'],0)


if __name__ == '__main__':
    unittest.main()

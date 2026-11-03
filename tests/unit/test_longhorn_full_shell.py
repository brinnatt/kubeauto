#!/usr/bin/env python3
"""Execute the unchanged full shell with isolated owning CLI boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
HELPERS=ROOT/'tests/helpers'
REPO=ROOT
SPEC=importlib.util.spec_from_file_location('full_shell_evidence',HELPERS/'longhorn-full-evidence.py')
MODULE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(MODULE)

class FullShellContracts(unittest.TestCase):
    def test_target_retirement_child_uses_only_explicit_reviewed_recovery_journal(self):
        for journal in ('', '/var/tmp/kubeauto-longhorn-v2-retirement.original/retirement.json'):
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as value:
                base=Path(value); helpers=base/'tests/helpers';helpers.mkdir(parents=True)
                (base/'.venv/bin').mkdir(parents=True);(base/'.venv/bin/python').symlink_to(sys.executable)
                shutil.copyfile(HELPERS/'longhorn-full.sh',helpers/'longhorn-full.sh')
                (helpers/'longhorn-full-evidence.py').write_text('import sys\nassert sys.argv[1]=="retirement-resume"\nprint('+repr(journal)+')\n')
                (helpers/'longhorn-live.sh').write_text('printf "%s|%s" "$1" "${LONGHORN_V2_RETIREMENT_JOURNAL:-}"\n')
                result=subprocess.run(['bash',str(helpers/'longhorn-full.sh'),'child','v2-cleanup-target'],
                    env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LONGHORN_FULL_RESUME='yes'),text=True,capture_output=True)
                self.assertEqual(0,result.returncode,result.stderr)
                expected='v2-cleanup-target-recovery' if journal else 'v2-cleanup-target'
                self.assertEqual(expected+'|'+journal,result.stdout)

    def test_resume_allows_owned_cluster_inputs_until_stage_identity_checks(self):
        source = (HELPERS/'longhorn-full.sh').read_text()
        self.assertIn('if [[ "${LONGHORN_FULL_RESUME:-no}" == yes ]]; then', source)
        self.assertIn('Later stage readers bind their owner journals', source)
        self.assertIn('[[ ! -e "$BASE/clusters/$name" && ! -L "$BASE/clusters/$name" ]]', source)
        self.assertIn('if [[ -f "$work/accepted/core.json" ]]; then', source)
        self.assertIn('"$EVIDENCE" resume-baseline', source)
        self.assertIn('Before V1 core installation, the independent V1 pool must be clean.', source)
        self.assertIn('LONGHORN_FULL_STAGE_REUSED stage=$mode', source)
        self.assertIn('longhorn-full-stage-verify.py" "$mode"', source)

    def run_full(self,fail=''):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);base=Path(temp.name)
        proof=base/'release-proofs';proof.mkdir()
        work=base/'logs/longhorn-full/run.fixture';work.mkdir(parents=True)
        helper=base/'tests/helpers';helper.mkdir(parents=True)
        lock=base/'locks';lock.mkdir()
        (base/'.venv/bin').mkdir(parents=True);(base/'.venv/bin/python').symlink_to(sys.executable)
        text=(HELPERS/'longhorn-full.sh').read_text()
        # Only the lock root changes. FD/inode/flock assertions and all shell
        # flow, stage loops, pipes, traps and durable invocations are original.
        text=text.replace('/var/lock/',str(lock)+'/')
        (helper/'longhorn-full.sh').write_text(text)
        shutil.copyfile(REPO/'tests/helpers/run-durable-gate.sh',helper/'run-durable-gate.sh')
        shutil.copyfile(ROOT/'tests/fixtures/longhorn-full-cli.py',helper/'fixture-cli.py')
        (base/'fixture.json').write_text(json.dumps({'modes':MODULE.MODES,'fail':fail,'proof_root':str(proof)}))
        for name,kind in [('longhorn-full-evidence.py','evidence'),('longhorn-parallel.py','context'),('longhorn-lab.py','lab')]:
            (helper/name).write_text('import os,sys\nos.execv(sys.executable,[sys.executable,'+repr(str(helper/'fixture-cli.py'))+','+repr(kind)+',*sys.argv[1:]])\n')
        for name,kind in [('longhorn-live.sh','own'),('lab-wipe-nodes.sh','wipe')]:
            (helper/name).write_text('#!/bin/bash\nexec '+shlex_quote(sys.executable)+' '+shlex_quote(str(helper/'fixture-cli.py'))+' '+kind+' "$@"\n')
        for name,stage in [('longhorn-static.sh','static'),('longhorn-artifacts.py','artifacts'),('longhorn-documentation.py','documentation')]:
            if name.endswith('.sh'):
                (helper/name).write_text('#!/bin/bash\nexec '+shlex_quote(sys.executable)+' '+shlex_quote(str(helper/'fixture-cli.py'))+' local '+stage+'\n')
            else:(helper/name).write_text('import os,sys\nos.execv(sys.executable,[sys.executable,'+repr(str(helper/'fixture-cli.py'))+',"local",'+repr(stage)+'])\n')
        unit=base/'tests/unit';unit.mkdir();(base/'tests/__init__.py').touch();(unit/'__init__.py').touch()
        (unit/'test_longhorn_delivery.py').write_text('import unittest\nclass InputContracts(unittest.TestCase):\n def test_disabled_cli_never_uses_cluster_or_helm(self):pass\n')
        env=dict(os.environ,LONGHORN_PARENT_LEASE_HELD='yes',LONGHORN_FULL_EVIDENCE_DIR=str(work/'evidence'))
        local=subprocess.run(['bash',str(helper/'longhorn-full.sh'),'local',str(work)],env=env,text=True,capture_output=True,timeout=20)
        self.assertEqual(local.returncode,0,local.stdout+local.stderr)
        shell='\n'.join(['exec '+str(fd)+'>'+shlex_quote(str(lock/name)) for fd,name in
            [(9,'kubeauto-ceph-priority.lock'),(7,'kubeauto-ceph-compute.lock'),(8,'kubeauto-redis-core-pool.lock')]])
        shell+='\nexec bash '+shlex_quote(str(helper/'longhorn-full.sh'))+' full'
        result=subprocess.run(['bash',str(helper/'run-durable-gate.sh'),str(work/'outer'),'LONGHORN_GATE_EXIT','bash','-c',shell],
            env=env,text=True,capture_output=True,timeout=30)
        trace=[json.loads(line) for line in (base/'trace.jsonl').read_text().splitlines()]
        return base,work,result,trace

    def test_complete_actual_shell_executes_56_stages_once_and_parallel_children_overlap(self):
        base,work,result,trace=self.run_full()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        accepts=[row['stage'] for row in trace if row['event']=='accepted']
        self.assertEqual(set(accepts),set(MODULE.MODES));self.assertEqual(len(accepts),56)
        self.assertEqual(accepts.count('v1-upgrade-lifecycle'),1)
        for parallel in ('v1-ownership-v2-linked-acceptance','v1-operations-v2-platform-install','v1-backup-v2-data-protection','v1-stability-v2-observability'):
            overlap=[r['branch'] for r in trace if r['event']=='parallel_overlap' and r['stage']==parallel]
            self.assertCountEqual(overlap,['v1','v2'])
            done=[r for r in trace if r['event']=='parallel_complete' and r['stage']==parallel]
            self.assertEqual([(r['v1_rc'],r['v2_rc']) for r in done],[(0,0)])
        consumed=[(r['stage'],r['scope'],r['host']) for r in trace if r['event']=='release_consumed']
        self.assertEqual(consumed,[('v2-disk-lifecycle','target','192.168.47.149'),('node-lifecycle','source','192.168.47.148'),('node-lifecycle','target','192.168.47.149')])
        self.assertFalse(list(base.glob('owned-*')));self.assertFalse(any((base/'clusters'/name).exists() for name in ('longhorn-gate','longhorn-v2-gate','longhorn-v2-dr-target')))
        self.assertEqual((work/'outer.exit').read_text(),'0\n');self.assertEqual((work/'outer.finalized').read_text(),'0\n')
        self.assertTrue(json.loads((work/'full-result.json').read_text())['outer_pending'])
        self.assertFalse((work/'accepted-full.json').exists()) # Real importer alone signs the outer fence.
        self.assertIn('LONGHORN_GATE_EXIT rc=0',result.stdout)
        self.assertTrue(any(r['event']=='context_restored' for r in trace))

    def test_first_failed_scope_cleans_own_fixture_and_stops_its_dependent_path(self):
        base,work,result,trace=self.run_full('v2-dr-store-qualification')
        self.assertNotEqual(result.returncode,0)
        started=[r['stage'] for r in trace if r['event']=='module_start']
        self.assertEqual(started[-1],'v2-dr-store-qualification')
        self.assertNotIn('v2-dr-standby-qualification',started)
        self.assertFalse(list(base.glob('owned-*')));self.assertFalse((work/'full-result.json').exists())
        self.assertEqual((work/'stages/v2-dr-store-qualification.exit').read_text(),'19\n')
        self.assertEqual((work/'stages/v2-dr-store-qualification.finalized').read_text(),'19\n')
        self.assertIn('phase=v2-dr-store-qualification',(work/'first-failure.status').read_text())
        self.assertTrue((base/'clusters/longhorn-v2-gate').exists()) # Healthy prerequisites retained.

    def test_parallel_failure_waits_for_other_branch_durable_clean_before_stopping(self):
        mode='v1-operations-v2-platform-install'
        base,work,result,trace=self.run_full(mode+':v2')
        self.assertNotEqual(result.returncode,0)
        done=[r for r in trace if r['event']=='parallel_complete' and r['stage']==mode]
        self.assertEqual([(r['v1_rc'],r['v2_rc']) for r in done],[(0,23)])
        for branch in ('v1','v2'):
            self.assertFalse((base/('parallel-'+mode)/(branch+'.owned')).exists())
            self.assertTrue((base/('parallel-'+mode)/(branch+'.finalized')).exists())
        self.assertFalse((work/'full-result.json').exists())
        self.assertFalse(any(r.get('stage')=='idempotence' for r in trace))


def shlex_quote(value):
    import shlex
    return shlex.quote(value)

if __name__=='__main__':unittest.main(verbosity=2)

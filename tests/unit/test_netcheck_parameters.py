"""Real CLI, threads, reports and exit status; only ping is an external fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'tools/NetCheckCli.py'


class NetCheckParameters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='netcheck-parameters-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.bin = self.base/'bin'
        self.bin.mkdir()
        ping = self.bin/'ping'
        ping.write_text('#!' + sys.executable + '\n'
                        'import json,os,sys\n'
                        'with open(os.environ["PING_CALLS"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                        'print("3 packets transmitted, 3 received, 0% packet loss")\n'
                        'print("rtt min/avg/max/mdev = 1.0/2.0/3.0/0.1 ms")\n')
        ping.chmod(0o700)
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                        NETCHECK_LOG_DIR=str(self.base/'logs'),
                        KUBEAUTO_LOG_DIR=str(self.base/'logs'),
                        PING_CALLS=str(self.base/'ping.jsonl'), PATH=str(self.bin))

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-B', str(TOOL), *args], env=self.env,
                                cwd=self.base, text=True, capture_output=True, timeout=12)
        self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
        self.assertNotIn('Traceback', result.stdout+result.stderr)
        return result.stdout+result.stderr

    def test_duration_workers_expansion_logging_report_and_json(self):
        start = time.monotonic()
        out = self.cli('--no-default-nodes', '-d', '1', '-v', '--max-expand', '4',
                       '--max-icmp-workers', '1', '--log-file', 'nested/custom.log',
                       '--report-dir', 'reports', '--json-summary', 'summary.json',
                       '127.0.0.1-2', '127.0.0.0/30', 'localhost')
        self.assertGreaterEqual(time.monotonic()-start, 1)
        self.assertIn('ICMP 总目标: 3 | ICMP 工作线程上限: 1', out)
        targets = {json.loads(line)[-1] for line in (self.base/'ping.jsonl').read_text().splitlines()}
        self.assertEqual(targets, {'127.0.0.1', '127.0.0.2', 'localhost'})
        report = list((self.base/'reports').glob('network_test_report_*.txt'))
        self.assertEqual(len(report), 1)
        self.assertIn('localhost', report[0].read_text())
        summary = json.loads((self.base/'summary.json').read_text())
        self.assertTrue(summary['overall_ok'])
        self.assertEqual(summary['target_count'], 3)
        self.assertTrue((self.base/'logs/custom.log').is_file())
        self.assertFalse((self.base/'nested').exists())

    def test_no_log_no_report_and_dry_run_produce_no_probe(self):
        out = self.cli('--dry-run', '--no-default-nodes', '--no-log-file', '--no-report-file',
                       '--json-summary', 'summary.json', '--max-expand', '2',
                       '--max-icmp-workers', '2', '127.0.0.1-2')
        self.assertIn('icmp_cli=2', out)
        self.assertFalse((self.base/'ping.jsonl').exists())
        self.assertFalse((self.base/'summary.json').exists())
        self.assertFalse((self.base/'logs').exists())
        self.cli('--no-default-nodes', '--duration', '1', '--no-log-file', '--no-report-file', '127.0.0.1')
        self.assertFalse(list(self.base.glob('network_test_report*')))
        self.assertFalse((self.base/'logs').exists())

    def test_unsampled_target_remains_visible_and_cannot_pass_quality(self):
        ping=self.bin/'ping'
        ping.write_text(ping.read_text().replace('import json,os,sys',
                                                'import json,os,sys,time\ntime.sleep(2)'))
        self.cli('--no-default-nodes','--duration','1','--max-icmp-workers','1',
                 '--no-log-file','--no-report-file','--json-summary','summary.json',
                 '127.0.0.1-2',expected=2)
        summary=json.loads((self.base/'summary.json').read_text())
        self.assertEqual(summary['target_count'],2)
        self.assertFalse(summary['overall_ok'])
        self.assertEqual(summary['failed_targets'],['127.0.0.2'])
        self.assertIn('127.0.0.2: 无数据',summary['notes'])

    def test_default_nodes_and_verbose_reach_runtime(self):
        # NODE_IPS are fixture input; the parser and complete main path are real.
        # Trace the actual main entry to constrain built-ins to loopback and
        # observe logging level, without replacing any product function.
        wrapper = '''import logging,runpy,sys
def trace(frame,event,arg):
    if event == 'call' and frame.f_code.co_name == 'main':
        frame.f_globals['NODE_IPS']=['127.0.0.1']
    if event == 'call' and frame.f_code.co_name == 'start_test':
        print('VERBOSE_LEVEL='+str(logging.getLogger('netcheck').level))
    return trace
sys.settrace(trace)
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
        for flags, level in (([], '20'), (['--verbose'], '10')):
            result = subprocess.run([sys.executable, '-B', '-c', wrapper, str(TOOL),
                                     '--duration', '1', '--no-log-file', '--no-report-file', *flags],
                                    env=self.env, cwd=self.base, capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('默认节点数: 1', result.stdout)
            self.assertIn('VERBOSE_LEVEL='+level, result.stdout)

    def test_numeric_target_and_output_boundaries_refuse_before_probe(self):
        for args in (['--duration', '0'], ['--duration', '-1'], ['--duration', 'bad'],
                     ['--duration', '2592001'], ['--max-expand', '0'],
                     ['--max-icmp-workers', '0'], ['--max-icmp-workers', 'bad'],
                     ['--max-expand', '1', '127.0.0.1-2'], ['127.0.0.1;touch injected'],
                     ['127.0.0.1:65536'], ['--report-dir', '/dev/null/no-directory']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, '-B', str(TOOL), '--no-default-nodes',
                                         '--no-log-file', *args], env=self.env, cwd=self.base,
                                        text=True, capture_output=True, timeout=12)
                self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.base/'ping.jsonl').exists())
        self.assertFalse((self.base/'injected').exists())
        self.cli('--no-default-nodes', '--no-log-file', expected=1)

    def test_help_version_and_short_aliases_are_offline(self):
        self.assertIn('--max-icmp-workers', self.cli('-h'))
        self.assertIn('netcheck ', self.cli('--version'))
        self.assertFalse((self.base/'ping.jsonl').exists())

    def test_requested_json_output_failure_is_nonzero(self):
        (self.base/'summary.json').mkdir()
        self.cli('--no-default-nodes', '--duration', '1', '--no-log-file',
                 '--no-report-file', '--json-summary', 'summary.json', '127.0.0.1', expected=1)
        self.assertFalse((self.base/'ping.jsonl').exists())

    def test_signal_after_samples_writes_summary_and_returns_documented_interruption(self):
        wrapper='''import runpy,signal,sys
done=False
def trace(frame,event,arg):
    global done
    if not done and event=='call' and frame.f_code.co_name=='generate_report':
        done=True
        signal.raise_signal(signal.SIGINT)
    return trace
sys.settrace(trace)
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
        result=subprocess.run([sys.executable,'-B','-c',wrapper,str(TOOL),
            '--duration','1','--no-log-file','--no-default-nodes','--no-report-file',
            '--json-summary','interrupted.json','127.0.0.1'],cwd=self.base,env=self.env,
            capture_output=True,text=True,timeout=12)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertTrue(json.loads((self.base/'interrupted.json').read_text())['overall_ok'])
        self.assertIn('SIGINT',result.stdout+result.stderr)

    def test_output_failure_after_preflight_is_nonzero_after_real_probes(self):
        wrapper='''import os,pathlib,runpy,sys
done=False
def trace(frame,event,arg):
    global done
    if not done and event=='call' and frame.f_code.co_name=='generate_report':
        done=True
        path=pathlib.Path(os.environ['FAIL_PATH'])
        if os.environ['FAIL_KIND']=='json': path.mkdir()
        else:
            if path.is_dir(): path.rmdir()
            path.write_text('fixture-blocker')
    return trace
sys.settrace(trace)
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
        for kind,flags in (('json',['--json-summary','late.json','--no-report-file']),
                           ('report',['--report-dir','late-report'])):
            target='late.json' if kind=='json' else 'late-report'
            result=subprocess.run([sys.executable,'-B','-c',wrapper,str(TOOL),
                '--duration','1','--no-log-file','--no-default-nodes','127.0.0.1',*flags],
                cwd=self.base,env=dict(self.env,FAIL_PATH=target,FAIL_KIND=kind),
                capture_output=True,text=True,timeout=12)
            self.assertEqual(result.returncode,1,result.stdout+result.stderr)
            self.assertIn('无法写入',result.stdout+result.stderr)
            self.assertTrue((self.base/'ping.jsonl').exists())


if __name__ == '__main__':
    unittest.main()

"""The legacy fixture must archive the incremental snapshot's active binlog."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LegacyArchiveHandoffContracts(unittest.TestCase):
    def test_legacy_container_fixture_declares_real_service_and_namespace(self):
        source=(ROOT/'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        branch='echo MYBACKUP_STAGE full\n'+source.split('echo MYBACKUP_STAGE full\n',1)[1].split('export MYBACKUP_INJECT_ENOSPC=1',1)[0]
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'backup.json'
            config.write_text(json.dumps({'mysql_datadir':directory+'/mysql','mysql_service':'mysqld'}))
            result=subprocess.run(['bash','-euc',branch],env=dict(os.environ,CONFIG=str(config),MYSQL_CTN='tools-mbk-mysql'),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(config.read_text()).get('mysql_container'),'tools-mbk-mysql',
                             'A Docker fixture must not masquerade as a native systemd server')

    def test_closed_log_precondition_and_flush_failure_stop_the_real_fixture_branch(self):
        source = (ROOT/'tests/helpers/mybackup-cli-live-regression.sh').read_text()
        branch = 'echo MYBACKUP_STAGE incr;' + source.split('echo MYBACKUP_STAGE incr;', 1)[1].split('echo MYBACKUP_STAGE checksum;', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory)/'events'
            prelude = '''closed=0
run_tool() {
  echo "$1 closed=$closed" >> "$TRACE"
  if [[ "$1" == binlog && "$closed" != 1 ]]; then
    echo ASSERT_SNAPSHOT_FILE_WAS_NOT_CLOSED >&2; return 37
  fi
}
mysql() {
  [[ "$*" == *'FLUSH BINARY LOGS'* ]] || return 38
  [[ "$FLUSH_FAILURE" != 1 ]] || return 39
  closed=1
}
'''
            env = dict(os.environ, TRACE=str(trace), CONFIG='fixture', PASSWORD='fixture-only',
                       ROOT=directory, FLUSH_FAILURE='0')
            result = subprocess.run(['bash', '-euc', prelude+branch], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(trace.read_text().splitlines(), ['incr closed=0', 'binlog closed=1'])
            trace.unlink()
            result = subprocess.run(['bash', '-euc', prelude+branch], env=dict(env, FLUSH_FAILURE='1'), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(trace.read_text().splitlines(), ['incr closed=0'])


if __name__ == '__main__':
    unittest.main()

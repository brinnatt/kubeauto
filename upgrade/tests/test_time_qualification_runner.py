from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'upgrade/lab/time-qualification.sh'

class TimeQualificationRunnerContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_shell_and_allowlist(self):
        result = subprocess.run(['bash','-n',str(SCRIPT)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        for host in ('192.168.47.131','192.168.47.132','192.168.47.133',
                     '192.168.47.134','192.168.47.135','192.168.47.136'):
            self.assertIn(host,self.text)
        self.assertNotIn('192.168.122.1',self.text)

    def test_only_time_services_and_bounded_delta_are_admitted(self):
        self.assertIn('systemctl restart chronyd',self.text)
        self.assertIn('chronyc -a makestep',self.text)
        self.assertIn('abs(row[\'delta_seconds\']) > 120',self.text)
        self.assertNotIn('kubecli',self.text)
        self.assertNotIn('kubectl',self.text)

if __name__ == '__main__':
    unittest.main()

"""Linux comm may be containerd-shim even when argv/exe is runc-v2."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

class LabShimContracts(unittest.TestCase):
    def test_real_verify_snippet_detects_both_actual_linux_names(self):
        source=(ROOT/'tests/helpers/lab-wipe-nodes.sh').read_text()
        snippets=[line for line in source.splitlines() if line.startswith('shim_count=')]
        self.assertEqual(len(snippets),2)
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/'ps'
            binary.write_text('#!/bin/sh\nprintf "%s\\n" systemd containerd-shim containerd-shim-runc-v2 sshd\n')
            binary.chmod(0o755)
            for snippet in snippets:
                r=subprocess.run(['bash','-euc',snippet+'\nprintf "%s" "$shim_count"'],env=dict(os.environ,PATH=directory+':'+os.environ['PATH']),capture_output=True,text=True,check=True)
                self.assertEqual(r.stdout,'2')

if __name__=='__main__':unittest.main()

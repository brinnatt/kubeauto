"""Stage real Git inputs and byte-qualified packs without SSH or downloads."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from upgrade.lab import next_inputs as inputs, next_session as session


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class NextInputContracts(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.output=self.root/'staged'
        for args in [['init','-q'],['config','user.name','Fixture'],
                     ['config','user.email','fixture@example.invalid']]:
            subprocess.run(['git',*args],cwd=self.repo,check=True,capture_output=True)
        (self.repo/'product.txt').write_text('accepted source\n')
        subprocess.run(['git','add','product.txt'],cwd=self.repo,check=True)
        subprocess.run(['git','commit','-qm','accepted fixture'],cwd=self.repo,check=True)
        self.commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.repo,text=True).strip()
        (self.repo/'product.txt').write_text('current target\n')
        self.records=self.repo/'upgrade/v1.36.6/evidence';self.records.mkdir(parents=True)
        self.extra=self.root/'extra';self.extra.mkdir()
        (self.extra/'nerdctl').write_bytes(b'qualified nerdctl 2.4.2')
        self.pack={'image':'hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.17.1',
                   'files':{'nerdctl':sha((self.extra/'nerdctl').read_bytes())}}
        self.save_pack()
        self.binaries={}
        for version in ['v1.35.10','v1.36.6']:
            directory=self.root/version;directory.mkdir();self.binaries[version]=directory
            raw=version.encode();(directory/'kubectl').write_bytes(raw)
            record={'image':'hub.talkedu.cn/kubeauto/kubeauto-k8s-bin:'+version,
                'architectures':[{'architecture':'amd64','files':{'kubectl':{'sha256':sha(raw)}}}]}
            p=self.repo/'upgrade'/version/'evidence/k8s-binary-content.json'
            p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(record))
        for module,name,value in [(inputs,'ROOT',self.repo),(inputs,'DIRECTORY',self.records.parent),
                (inputs,'SOURCE_COMMIT',self.commit),(session,'SOURCE_COMMIT',self.commit)]:
            override=patch.object(module,name,value);override.start();self.addCleanup(override.stop)

    def save_pack(self):
        (self.records/'source-pack-content.json').write_text(json.dumps(self.pack))

    def stage(self):
        inputs.stage(self.output,source_extra=self.extra,
            source_k8s=self.binaries['v1.35.10'],target_k8s=self.binaries['v1.36.6'])

    def test_real_archive_and_worktree_keep_distinct_source_and_target_versions(self):
        self.stage()
        self.assertEqual((self.output/'source-product/product.txt').read_text(),'accepted source\n')
        self.assertEqual((self.output/'target-product/product.txt').read_text(),'current target\n')
        for product,version in [('source-product','v1.35.10'),('target-product','v1.36.6')]:
            self.assertEqual((self.output/product/'kube-bin/kubectl').read_bytes(),version.encode())
            self.assertEqual((self.output/product/'extra-bin/nerdctl').read_bytes(),b'qualified nerdctl 2.4.2')
        session.verify_inputs(self.output,self.output/'target-product')
        with self.assertRaisesRegex(ValueError,'never overwritten'):
            self.stage()

    def test_historical_pack_and_input_byte_drift_refuse_before_staging(self):
        self.pack['image']='hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.17.0';self.save_pack()
        with self.assertRaisesRegex(ValueError,'maintenance pack'):self.stage()
        self.assertFalse(self.output.exists())
        self.pack['image']='hub.talkedu.cn/kubeauto/kubeauto-ext-bin:1.17.1';self.save_pack()
        (self.binaries['v1.35.10']/'kubectl').write_bytes(b'wrong version')
        with self.assertRaisesRegex(ValueError,'content differs'):self.stage()
        self.assertFalse(self.output.exists())

    def test_extra_or_symlink_pack_files_refuse_before_copying(self):
        extra=self.extra/'unrecorded';extra.write_bytes(b'unreviewed')
        with self.assertRaisesRegex(ValueError,'extra or symbolic-link'):self.stage()
        extra.unlink();extra.symlink_to(self.extra/'nerdctl')
        with self.assertRaisesRegex(ValueError,'extra or symbolic-link'):self.stage()
        self.assertFalse(self.output.exists())

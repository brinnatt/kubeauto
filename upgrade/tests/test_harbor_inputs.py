"""Qualify local payload staging and refuse drift before a lab can use it."""
import hashlib
import io
from pathlib import Path
import tarfile
import unittest
from unittest.mock import patch

from upgrade.lab import harbor_inputs as inputs
from upgrade.tests import test_next_inputs


class HarborInputContracts(unittest.TestCase):
    def setUp(self):
        self.fixture=test_next_inputs.NextInputContracts('runTest')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        bridge=b'qualified cri-dockerd'
        (self.fixture.extra/'cri-dockerd').write_bytes(bridge)
        self.fixture.pack['files']['cri-dockerd']=hashlib.sha256(bridge).hexdigest()
        self.fixture.save_pack()
        self.fixture.stage()
        self.payloads=self.fixture.root/'payloads';self.payloads.mkdir()
        docker=self.payloads/'docker-28.5.2-official.tgz'
        with tarfile.open(docker,mode='w:gz') as archive:
            for name in inputs.DOCKER_FILES:
                raw=('qualified-'+name).encode();member=tarfile.TarInfo('docker/'+name);member.size=len(raw)
                archive.addfile(member,io.BytesIO(raw))
        self.packages={}
        for version in inputs.PACKAGES:
            raw=('official-Harbor-'+version).encode()
            (self.payloads/('harbor-offline-'+version+'-amd64.tgz')).write_bytes(raw)
            self.packages[version]=hashlib.sha256(raw).hexdigest()
        for name,value in [('DOCKER_SHA',hashlib.sha256(docker.read_bytes()).hexdigest()),
                           ('PACKAGES',self.packages),('SOURCE_COMMIT',self.fixture.commit)]:
            override=patch.object(inputs,name,value);override.start();self.addCleanup(override.stop)

    def test_qualified_archive_yields_exact_source_and_target_payloads(self):
        inputs.add_payloads(self.fixture.output,self.payloads)
        inputs.verify_payloads(self.fixture.output)
        self.assertTrue((self.fixture.output/'source-product/down/harbor-offline-installer-v2.13.0.tgz').is_file())
        self.assertFalse((self.fixture.output/'source-product/down/harbor-offline-installer-v2.16.0.tgz').exists())
        self.assertEqual((self.fixture.output/'target-product/docker-bin/docker').read_bytes(),b'qualified-docker')
        self.assertEqual((self.fixture.output/'target-product/docker-bin/cri-dockerd').read_bytes(),b'qualified cri-dockerd')
        (self.fixture.output/'source-product/docker-bin/docker').write_bytes(b'wrong runtime')
        with self.assertRaisesRegex(ValueError,'Docker bytes differ'):
            inputs.verify_payloads(self.fixture.output)

    def test_changed_official_payload_refuses_before_docker_directories_are_created(self):
        (self.payloads/'harbor-offline-v2.16.0-amd64.tgz').write_bytes(b'wrong package')
        with self.assertRaisesRegex(ValueError,'payload differs'):
            inputs.add_payloads(self.fixture.output,self.payloads)
        self.assertFalse((self.fixture.output/'source-product/docker-bin').exists())

    def test_cri_bridge_required_by_actual_docker_role_is_bound_to_ext_bin(self):
        inputs.add_payloads(self.fixture.output,self.payloads)
        (self.fixture.output/'target-product/docker-bin/cri-dockerd').write_bytes(b'wrong bridge')
        with self.assertRaisesRegex(ValueError,'CRI bridge differs'):
            inputs.verify_payloads(self.fixture.output)

    def test_symbolic_package_cannot_reuse_a_qualified_hash(self):
        path=self.payloads/'harbor-offline-v2.13.0-amd64.tgz'
        target=self.payloads/'original';path.rename(target);path.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'payload differs'):
            inputs.add_payloads(self.fixture.output,self.payloads)
        self.assertFalse((self.fixture.output/'source-product/docker-bin').exists())


if __name__=='__main__':unittest.main()

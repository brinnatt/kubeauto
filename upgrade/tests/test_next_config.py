"""Final config publication preserves custom source fields and journal identity."""
from pathlib import Path
import tempfile
import unittest
import yaml
from upgrade.product.next_config import configuration,publish
from upgrade.product.kubernetes_preflight import Refused


class FinalConfigContracts(unittest.TestCase):
    def test_original_source_remains_stable_across_atomic_final_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);state=root/'state';state.mkdir(mode=0o700)
            path=root/'config.yml'
            path.write_text('# customer annotation\nK8S_VER: "1.34.13"\ncalico_ver: v3.28.4\nCONTAINER_RUNTIME: containerd\ndns_install: "yes"\nENABLE_LOCAL_DNS_CACHE: true\nmetricsserver_install: "yes"\nCUSTOM: preserve\n')
            original,text=configuration(path,state)
            self.assertEqual(yaml.safe_load(path.read_text())['K8S_VER'],'1.34.13')
            publish(path,text)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            current=yaml.safe_load(path.read_text())
            self.assertEqual((current['K8S_VER'],current['calico_ver'],current['corednsVer'],current['CUSTOM']),('1.35.10','v3.32.4','1.15.0','preserve'))
            self.assertTrue(text.startswith('# customer annotation'))
            self.assertEqual(configuration(path,state),(original,text))
            before=path.stat().st_mtime_ns;publish(path,text)
            self.assertEqual(path.stat().st_mtime_ns,before)
            path.write_text(path.read_text().replace('preserve','foreign'))
            with self.assertRaisesRegex(Refused,'drifted'):configuration(path,state)

    def test_uninstalled_consumers_are_not_silently_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'config.yml';path.write_text('K8S_VER: 1.34.13\ncalico_ver: v3.28.4\ndns_install: "no"\nmetricsserver_install: "no"\n')
            original,text=configuration(path,root)
            self.assertNotIn('corednsVer',yaml.safe_load(text))
            self.assertNotIn('metricsVer',yaml.safe_load(text))

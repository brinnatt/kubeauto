"""Current supply-chain acceptance must reject stale or broadened proof."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('tools_supply',ROOT/'tests/helpers/tools-supply-chain-audit.py')
GATE=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(GATE)


class SupplyChainContracts(unittest.TestCase):
    def test_only_builder_may_use_a_sha_matching_snapshot(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(GATE,'ROOT',Path(directory)):
            root=Path(directory);source=root/'tools/tool.py';source.parent.mkdir();source.write_text('changed')
            old=hashlib.sha256(b'original').hexdigest()
            saved=root/'logs/tools-build-inputs'/(old+'.sh');saved.parent.mkdir(parents=True);saved.write_text('original')
            self.assertEqual(GATE.input_file('tests/helpers/build-tools-rocky8.sh',old),saved)
            with self.assertRaisesRegex(ValueError,'BUILD_INPUT_STALE'):GATE.input_file('tools/tool.py',old)
            saved.write_text('tampered')
            with self.assertRaisesRegex(ValueError,'SNAPSHOT_MISSING'):GATE.input_file('tests/helpers/build-tools-rocky8.sh',old)

    def test_current_build_inventory_requires_every_tool_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(GATE,'ROOT',Path(directory)):
            root=Path(directory);(root/'tools').mkdir();(root/'dist').mkdir()
            (root/'tools/Demo.py').write_text('source');(root/'dist/Demo-rocky8').write_text('artifact')
            proof=dict(inputs={'tools/Demo.py':GATE.sha(root/'tools/Demo.py')},artifacts={'Demo':GATE.sha(root/'dist/Demo-rocky8')},runtime='glibc 2.28')
            p=root/'dist/demo-build.json';p.write_text(json.dumps(proof));GATE.builds()
            (root/'tools/Missing.py').write_text('source')
            with self.assertRaisesRegex(ValueError,'INVENTORY_REFUSED'):GATE.builds()
            (root/'tools/Missing.py').unlink();(root/'dist/duplicate-build.json').write_text(json.dumps(proof))
            with self.assertRaisesRegex(ValueError,'ARTIFACT_REFUSED'):GATE.builds()
            (root/'dist/duplicate-build.json').unlink();(root/'dist/Demo-rocky8').write_text('changed artifact')
            with self.assertRaisesRegex(ValueError,'ARTIFACT_REFUSED'):GATE.builds()

    def test_package_reuse_does_not_consume_unrelated_tool_claims(self):
        source=(ROOT/'tests/helpers/tools-supply-chain-audit.py').read_text()
        packages=source.split('def packages():',1)[1].split('\ndef audit():',1)[0]
        self.assertIn("record['archive']",packages)
        self.assertIn("record['member']",packages)
        self.assertIn("digests signatures OK",packages)
        self.assertIn("len(records)!=16",packages)
        self.assertNotIn("proof['inputs']",packages)
        self.assertNotIn('StarMigrt',packages)

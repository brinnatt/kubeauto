"""Cluster exports require exact artifacts and complete, non-overlapping slots."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_cluster_snapshot_contract',
        ROOT / 'tests/helpers/redis-cluster-snapshot-gate.py')
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


class SnapshotTests(unittest.TestCase):
    def test_inventory_preserves_hosts_and_only_replaces_the_exact_private_directory(self):
        name = 'redis-snapshot-' + 'a' * 32
        text = '[all:vars]\ncluster_dir="{{ base_dir }}/clusters/redis-cluster-gate"\n[worker]\nnode-131\n'
        result = gate.snapshot_inventory(text, name)
        self.assertEqual(result.replace(name, 'redis-cluster-gate'), text)
        for bad in (text.replace('redis-cluster-gate', 'foreign'), text + text):
            with self.assertRaises(ValueError): gate.snapshot_inventory(bad, name)
        with self.assertRaises(ValueError): gate.snapshot_inventory(text, '../foreign')

    def test_manifest_rejects_missing_overlap_unstable_slots_tamper_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); image = 'registry.talkschool.cn:5000/brinnatt/redis:8.10.2-kubeauto.1'
            data = b'REDIS0012' + b'contract-fixture'
            rows = []
            for i, slots in enumerate(('0-5460','5461-10922','10923-16383')):
                identifier = str(i + 1) * 40
                (root / (identifier + '.rdb')).write_bytes(data)
                rows.append({'id':identifier,'file':identifier+'.rdb','slots':[slots],
                             'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
            manifest = {'mode':'cluster','image':image,'restore_verified':False,'files':rows}
            gate.verify_exports(root, manifest, image)
            for change in (lambda d: d['files'].pop(),
                           lambda d: d['files'][1].update(slots=['5460-10922']),
                           lambda d: d['files'][2].update(slots=['10923-16382']),
                           lambda d: d['files'][0].update(slots=['[0->-foreign]']),
                           lambda d: d['files'][0].update(file='../foreign.rdb'),
                           lambda d: d['files'][0].update(sha256='0'*64),
                           lambda d: d.update(image='foreign'),
                           lambda d: d.update(restore_verified=True)):
                bad = deepcopy(manifest); change(bad)
                with self.assertRaises(ValueError): gate.verify_exports(root, bad, image)
            path = root / rows[0]['file']; path.unlink()
            path.symlink_to(root / rows[1]['file'])
            with self.assertRaises(ValueError): gate.verify_exports(root, manifest, image)


if __name__ == '__main__': unittest.main()

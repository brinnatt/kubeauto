"""Validate real upgrade records without reopening historical GA acceptance."""
import hashlib
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('upgrade_validate', ROOT / 'upgrade/validate.py')
records = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(records)


class RecordIntegrityContracts(unittest.TestCase):
    def test_official_records_have_unique_owned_sources_and_matching_snapshots(self):
        data = records.read_mapping(ROOT / 'upgrade/official-sources.yaml')
        ids = [source['id'] for source in data['sources']]
        self.assertEqual(len(ids), len(set(ids)))
        # A YAML list appended at EOF can parse successfully under access_gaps.
        # Reject that exact mistake: obtained observations belong in sources.
        for gap in data.get('access_gaps', []):
            self.assertNotIn('obtained_content', gap)
            self.assertNotIn('obtained_contents', gap)
            self.assertNotIn('id', gap, 'captured source records belong in sources')
        for source in data['sources']:
            if 'obtained_content' in source or 'obtained_contents' in source:
                copies = records.official_copies(source)
            else:
                copies = []
            for copied in copies:
                with self.subTest(source=source['id']):
                    path = records.owned_path(ROOT, copied['path'])
                    self.assertEqual(path.stat().st_size, copied['bytes'])
                    records.check_hash(ROOT, copied['path'], copied['sha256'], evidence=True)
        for filename, key, refs in [
            ('release.yaml', 'component_decisions', 'source_ids'),
            ('research.yaml', 'component_records', 'known_source_ids'),
        ]:
            target = records.read_mapping(ROOT / 'upgrade/v1.34.13' / filename)
            for component in target[key]:
                self.assertTrue(set(component[refs]) <= set(ids), component['component_id'])

    def test_copied_ga_evidence_keeps_its_original_bytes(self):
        manifest = records.read_mapping(ROOT / 'upgrade/v1.33.6/evidence-manifest.yaml')
        self.assertEqual(len(manifest['files']), manifest['summary']['total_files'])
        for item in manifest['files']:
            with self.subTest(path=item['copy_path']):
                path = records.owned_path(ROOT, item['copy_path'])
                self.assertEqual(path.stat().st_size, item['bytes'])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item['sha256'])


if __name__ == '__main__':
    unittest.main()

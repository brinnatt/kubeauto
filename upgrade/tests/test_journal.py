"""Exercise interruption, stale inputs and competing cluster owners on disk."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from upgrade.product.journal import Journal, JournalRefused


class JournalContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'cluster'
        self.binding = {'run_id': 'first-hop', 'cluster_uid': 'immutable-namespace-uid',
                        'source': 'v1.33.6', 'target': 'v1.34.13',
                        'input_sha256': 'a' * 64, 'artifact_digest': 'sha256:' + 'b' * 64}
        self.steps = ['api', 'controllers', 'node/worker-1', 'business']

    def open(self, binding=None, steps=None):
        return Journal(self.directory, binding or self.binding, steps or self.steps)

    def read(self):
        return json.loads((self.directory / 'journal.json').read_text())

    def test_interruption_retains_active_boundary_and_first_failure(self):
        with self.open() as journal:
            journal.begin('api')
            journal.complete('api')
            journal.begin('controllers')
            journal.failed('controllers', 2, 'environment')
        first = self.read()['failures'][0]
        with self.open() as journal:
            self.assertEqual(journal.data['completed'], ['api'])
            self.assertEqual(journal.data['active'], 'controllers')
            journal.begin('controllers')
            journal.failed('controllers', 3, 'product')
            journal.begin('controllers')
            journal.complete('controllers')
        self.assertEqual(self.read()['failures'][0], first)
        self.assertEqual(self.read()['completed'], ['api', 'controllers'])
        self.assertEqual(len(self.read()['failures']), 2)
        self.assertEqual((self.directory / 'journal.json').stat().st_mode & 0o777, 0o600)

    def test_competing_run_cannot_touch_the_current_journal(self):
        with self.open() as journal:
            journal.begin('api')
            before = (self.directory / 'journal.json').read_bytes()
            binding = {**self.binding, 'run_id': 'competing-run'}
            with self.assertRaisesRegex(JournalRefused, 'active upgrade owner'):
                with self.open(binding):
                    self.fail('competing owner admitted')
            self.assertEqual((self.directory / 'journal.json').read_bytes(), before)

    def test_changed_identity_config_artifact_run_or_plan_is_refused(self):
        with self.open():
            pass
        before = (self.directory / 'journal.json').read_bytes()
        for key in self.binding:
            with self.subTest(key=key), self.assertRaises(JournalRefused):
                with self.open({**self.binding, key: 'foreign'}):
                    self.fail('changed binding admitted')
        with self.assertRaises(JournalRefused):
            with self.open(steps=list(reversed(self.steps))):
                self.fail('changed order admitted')
        self.assertEqual((self.directory / 'journal.json').read_bytes(), before)
        with self.open():  # Refusals must not leak the exclusive lock.
            pass

    def test_skipping_repeating_and_accepting_unstarted_steps_are_refused(self):
        with self.open() as journal:
            for action in [lambda: journal.begin('controllers'),
                           lambda: journal.complete('api')]:
                with self.assertRaises(JournalRefused):
                    action()
            journal.begin('api')
            journal.complete('api')
            with self.assertRaises(JournalRefused):
                journal.begin('api')
            with self.assertRaises(JournalRefused):
                journal.begin('node/worker-1')

    def test_corrupt_prefix_cannot_create_progress_or_overwrite_evidence(self):
        with self.open():
            pass
        original = self.read()
        for changes in [{'completed': ['controllers']}, {'active': 'business'},
                        {'completed': self.steps + ['extra']}, {'failures': 'not-a-list'}]:
            value = {**deepcopy(original), **changes}
            path = self.directory / 'journal.json'
            path.write_text(json.dumps(value))
            before = path.read_bytes()
            with self.assertRaises(JournalRefused):
                with self.open():
                    self.fail('invalid journal admitted')
            self.assertEqual(path.read_bytes(), before)

    def test_symlinks_foreign_hardlinks_and_public_permissions_are_refused(self):
        self.directory.mkdir(mode=0o700)
        target = Path(self.temp.name) / 'foreign'
        target.write_text('foreign evidence')
        path = self.directory / 'owner.lock'
        path.symlink_to(target)
        with self.assertRaises(OSError):
            with self.open():
                self.fail('symlink lock admitted')
        path.unlink()
        path.hardlink_to(target)
        with self.assertRaises(JournalRefused):
            with self.open():
                self.fail('hardlinked lock admitted')
        path.unlink()
        self.directory.chmod(0o755)
        with self.assertRaises(JournalRefused):
            with self.open():
                self.fail('public directory admitted')
        self.assertEqual(target.read_text(), 'foreign evidence')


if __name__ == '__main__':
    unittest.main()

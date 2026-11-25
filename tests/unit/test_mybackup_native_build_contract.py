"""Native one-tool packaging refuses drift before installation or compilation."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('mybackup_native_build',ROOT/'tests/helpers/build-mybackup-native.py')
BUILD=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(BUILD)


class NativeBuildContracts(unittest.TestCase):
    def test_scope_and_selected_tool_are_checked_before_execution(self):
        plan={'base':'/tmp/b80build-0123456789ab','selected':['MyBackupCli'],
              'requirements':BUILD.REQUIREMENTS,'inputs':{'source.py':'a'*64}}
        with mock.patch.object(BUILD,'sha',return_value='a'*64):
            BUILD.validate_plan(plan,Path(plan['base']))
            plan['selected'].append('MigrationCli')
            with self.assertRaisesRegex(ValueError,'SELECTION_REFUSED'): BUILD.validate_plan(plan,Path(plan['base']))
        with self.assertRaisesRegex(ValueError,'SCOPE_REFUSED'): BUILD.validate_plan(plan,Path('/var/lib/mysql'))

    def test_input_drift_is_not_built(self):
        plan={'base':'/tmp/b80build-0123456789ab','selected':['MyBackupCli'],
              'requirements':BUILD.REQUIREMENTS,'inputs':{'source.py':'a'*64}}
        with mock.patch.object(BUILD,'sha',return_value='b'*64):
            with self.assertRaisesRegex(ValueError,'INPUT_REFUSED'): BUILD.validate_plan(plan,Path(plan['base']))

    def test_build_is_single_native_spec_with_fixed_domestic_dependencies(self):
        source=(ROOT/'tests/helpers/build-mybackup-native.py').read_text()
        self.assertIn("TOOLS_SELECTED='MyBackupCli'",source)
        self.assertIn("'https://repo.huaweicloud.com/repository/pypi/simple'",source)
        self.assertIn("'glibc 2.28'",source)
        self.assertIn("'--help-native'",source)
        self.assertNotIn('docker build',source)
        self.assertNotIn('docker push',source)


if __name__=='__main__': unittest.main()

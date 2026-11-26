"""Offline customer help must expose the real parser and current configuration."""
import ast
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ToolsHelpContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tools-help-contract-')
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        for name in ('KUBEAUTO_LOG_DIR', 'NETCHECK_LOG_DIR', 'GENOVPN_LOG_DIR',
                     'KAFKA_LOG_DIR', 'STARROCKS_LOG_DIR', 'MYSQL_MIGRATION_LOG_DIR'):
            self.env[name] = self.tmp.name
        self.env['MYSQLBACKUP_LOG_FILE'] = str(Path(self.tmp.name) / 'backup.log')
        self.env.pop('MYSQL_BACKUP_PASSWORD', None)

    def cli(self, path, *args):
        result = subprocess.run([sys.executable, '-B', str(ROOT / path), *args],
                                env=self.env, cwd=self.tmp.name, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        return result.stdout

    def test_all_public_options_are_reachable_in_offline_help(self):
        # Ask the actual parser for its tree before it handles --help. This
        # discovers nested subcommands without keeping a second option list.
        probe = '''import argparse,json,runpy,sys
original=argparse.ArgumentParser.parse_args
def walk(parser, prefix=()):
    rows=[(prefix, [s for a in parser._actions for s in a.option_strings])]
    for action in parser._actions:
        if isinstance(action,argparse._SubParsersAction):
            for name,child in action.choices.items(): rows.extend(walk(child,prefix+(name,)))
    return rows
def parse(parser,*args,**kwargs):
    print("HELP_TREE="+json.dumps(walk(parser)),file=sys.stderr)
    return original(parser,*args,**kwargs)
argparse.ArgumentParser.parse_args=parse
sys.argv=[sys.argv[1],"--help"]
runpy.run_path(sys.argv[0],run_name="__main__")
'''
        paths = sorted((ROOT / 'tools').rglob('*.py'))
        self.assertEqual(len(paths), 11)
        pages = 0
        options_checked = 0
        for path in paths:
            with self.subTest(tool=path.stem):
                result = subprocess.run([sys.executable, '-B', '-c', probe, str(path)],
                                        cwd=self.tmp.name, env=self.env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                tree = next(line[10:] for line in result.stderr.splitlines()
                            if line.startswith('HELP_TREE='))
                for prefix, options in json.loads(tree):
                    help_text = self.cli(path, *prefix, '--help')
                    pages += 1
                    for option in options:
                        if option.startswith('--'):
                            self.assertIn(option, help_text, (path.stem, prefix, option))
                            options_checked += 1
        print('TOOLS_HELP_OPTIONS_PASS tools=11 pages={} options={}'.format(pages, options_checked))

    def test_mybackup_embedded_template_matches_shipped_json(self):
        text = self.cli('tools/mysqltools/MyBackupCli.py', '--help-config')
        embedded = json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
        example = json.loads((ROOT / 'tools/mysqltools/mybackup.conf.example').read_text())
        self.assertEqual(embedded, example)
        keys = {key for key in example if not key.startswith('_comment')}
        source = ast.parse((ROOT / 'tools/mysqltools/MyBackupCli.py').read_text())
        config = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'Config')
        defaults = next(node.value for node in ast.walk(config)
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == 'defaults'
                                for target in node.targets))
        for key, value in zip(defaults.keys, defaults.values):
            # The password is deliberately an example, not the process secret.
            if ast.literal_eval(key) != 'mysql_password':
                self.assertEqual(example[ast.literal_eval(key)], ast.literal_eval(value))
        consumed = set()
        for node in ast.walk(config):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == 'defaults' and node.func.attr == 'get' and node.args:
                    consumed.add(ast.literal_eval(node.args[0]))
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == 'defaults':
                consumed.add(ast.literal_eval(node.slice))
        self.assertEqual(keys, consumed)

    def test_mybackup_help_explains_restore_and_execution_choices(self):
        help_text = self.cli('tools/mysqltools/MyBackupCli.py', '--help')
        for value in ('mysql_container', 'mysql_binlog_dir', 'mysql_binlog_index',
                      'mysql_topology', 'primary-replica', 'dual-primary',
                      'backup-restore.conf', '--help-config', '0600', '30 秒'):
            self.assertIn(value, help_text)
        self.assertNotIn('Production Ready', help_text)

    def test_publish_help_uses_current_tool_name_and_json_boundary(self):
        help_text = self.cli('tools/k8stools/KubePublishCli.py', '--help')
        self.assertNotIn('k8simgmanager.py', help_text)
        self.assertIn('JSON 只读取', help_text)

    def test_calico_allow_network_examples_are_valid_cidrs(self):
        help_text = self.cli('tools/k8stools/CalicoPolicyCli.py', '--help')
        networks = re.findall(r'(?:^|\s)-a\s+(\S+/\d+)', help_text)
        self.assertTrue(networks)
        for network in networks:
            with self.subTest(network=network):
                ipaddress.ip_network(network)

    def test_mysql_special_help_needs_no_native_clients_or_credentials(self):
        self.env['PATH'] = ''
        self.env['MYSQLBACKUP_LOG_FILE'] = '/proc/tools-help-unwritable/backup.log'
        for tool, arguments in (
            ('MyBackupCli', ('--help-native',)),
            ('MyBackupCli', ('--help-config',)),
            ('MigrationCli', ('--help-native',)),
            ('MigrationCli', ('--guide',)),
            ('MyLogiBackupCli', ('--help-native',)),
            ('MyLogiBackupCli', ('preflight', '--help-native')),
            ('MyLogiBackupCli', ('backup', '--help-native')),
            ('MyLogiBackupCli', ('timer', 'install', '--help-native')),
        ):
            with self.subTest(tool=tool, arguments=arguments):
                self.assertTrue(self.cli('tools/mysqltools/' + tool + '.py', *arguments))

    def test_preflight_and_dry_run_do_not_claim_zero_local_side_effects(self):
        text = self.cli('tools/mysqltools/MyLogiBackupCli.py', '--help')
        self.assertIn('创建本地备份目录', text)
        self.assertNotIn('全部只读', text)
        text = self.cli('tools/k8stools/KubeBackupCli.py', '--help')
        self.assertNotIn('全程不落盘', text)
        self.assertIn('卷内数据', text)


if __name__ == '__main__':
    unittest.main()

"""Public dispatch and lease allowlists for the remaining fixed-runner modes."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
ROUTES = {
    'v2-disk-lifecycle': ('longhorn-v2-disk-lifecycle.sh', 'LONGHORN_V2_DISK_LIFECYCLE_PASS'),
    'v2-disk-lifecycle-cleanup': ('longhorn-v2-disk-lifecycle.sh', 'LONGHORN_V2_DISK_LIFECYCLE_CLEANUP_PASS'),
    'v2-upgrade-lifecycle': ('longhorn-v2-upgrade-lifecycle.sh', 'LONGHORN_V2_UPGRADE_GATE_PASS'),
    'v2-upgrade-cleanup': ('longhorn-v2-upgrade-lifecycle.sh', 'LONGHORN_V2_UPGRADE_CLEANUP_PASS'),
    'disk-failure': ('longhorn-v1-storage-faults.sh', 'LONGHORN_DISK_FAILURE_PASS'),
    'disk-failure-cleanup': ('longhorn-v1-storage-faults.sh', 'LONGHORN_DISK_FAILURE_CLEANUP_PASS'),
    'middleware': ('longhorn-v1-middleware.sh', 'LONGHORN_MIDDLEWARE_PASS'),
    'middleware-business': ('longhorn-v1-middleware.sh', 'LONGHORN_MIDDLEWARE_BUSINESS_BASELINE_RETAINED'),
    'middleware-cleanup': ('longhorn-v1-middleware.sh', 'LONGHORN_MIDDLEWARE_CLEANUP_PASS'),
    'v2-runtime-resources': ('longhorn-v2-runtime-resources.sh', 'LONGHORN_V2_RUNTIME_RESOURCES_PASS'),
    'v2-runtime-resources-cleanup': ('longhorn-v2-runtime-resources.sh', 'LONGHORN_V2_RUNTIME_RESOURCES_CLEANUP_PASS'),
    'last-replica': ('longhorn-last-replica.sh', 'LONGHORN_LAST_REPLICA_GATE_PASS'),
    'last-replica-cleanup': ('longhorn-last-replica.sh', 'LONGHORN_LAST_REPLICA_CLEANUP_PASS'),
    'network-failure': ('longhorn-v1-storage-faults.sh', 'LONGHORN_NETWORK_FAILURE_PASS'),
    'network-failure-cleanup': ('longhorn-v1-storage-faults.sh', 'LONGHORN_NETWORK_FAILURE_CLEANUP_PASS'),
    'system-restore': ('longhorn-system-recovery.sh', 'LONGHORN_SYSTEM_RESTORE_GATE_PASS'),
    'system-restore-cleanup': ('longhorn-system-recovery.sh', 'LONGHORN_SYSTEM_RESTORE_CLEANUP_PASS'),
    'disaster-recovery': ('longhorn-v1-disaster.sh', 'LONGHORN_DISASTER_RECOVERY_PASS'),
    'disaster-recovery-cleanup': ('longhorn-v1-disaster.sh', 'LONGHORN_DISASTER_RECOVERY_CLEANUP_PASS'),
    'node-failure': ('longhorn-node-failure.sh', 'LONGHORN_NODE_FAILURE_GATE_PASS'),
    'node-failure-cleanup': ('longhorn-node-failure.sh', 'LONGHORN_NODE_FAILURE_CLEANUP_PASS'),
    'observability': ('longhorn-v1-observability.sh', 'LONGHORN_OBSERVABILITY_PASS'),
    'observability-business': ('longhorn-v1-observability.sh', 'LONGHORN_OBSERVABILITY_BUSINESS_BASELINE_RETAINED'),
    'observability-cleanup': ('longhorn-v1-observability.sh', 'LONGHORN_OBSERVABILITY_CLEANUP_PASS'),
}
for engine in ('v1', 'v2', 'v2-dr-target'):
    mode = engine+'-im-revision-2-adopt-qualification'
    ROUTES[mode] = ('longhorn-v2-im-adopt.sh', 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS')
for suffix in ('qualification', 'cleanup'):
    mode = 'v2-maintenance-drain-'+suffix
    ROUTES[mode] = ('longhorn-v2-maintenance-drain.sh', 'LONGHORN_'+mode.upper().replace('-', '_')+'_PASS')


class PublicRoutes(unittest.TestCase):
    def test_matrix_upgrade_aliases_share_one_complete_acceptance_chain(self):
        runner = ROOT/'tests/helpers/longhorn-runner.sh'
        for mode in ('upgrade', 'upgrade-recovery'):
            result = subprocess.run(['bash', '-c',
                'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                'contract', str(runner), '--longhorn-'+mode], check=True, capture_output=True, text=True)
            self.assertEqual(['v1-upgrade-lifecycle', 'LONGHORN_V1_UPGRADE_GATE_PASS'], result.stdout.splitlines())

    def test_monitoring_performance_matrix_alias_uses_complete_wrapper(self):
        result = subprocess.run(['bash', '-c',
            'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
            'contract', str(ROOT/'tests/helpers/longhorn-runner.sh'), '--longhorn-v2-observability-performance'],
            check=True, capture_output=True, text=True)
        self.assertEqual(['v2-observability', 'LONGHORN_V2_OBSERVABILITY_PERFORMANCE_PASS'], result.stdout.splitlines())

    def test_dispatch_preserves_exact_mode_and_terminal_marker(self):
        runner = ROOT/'tests/helpers/longhorn-runner.sh'
        for mode, (_, marker) in ROUTES.items():
            with self.subTest(mode=mode):
                result = subprocess.run(['bash', '-c',
                    'source "$1"; longhorn_launch() { printf "%s\\n" "$@"; }; longhorn_dispatch "$2"',
                    'contract', str(runner), '--longhorn-'+mode],
                    check=True, capture_output=True, text=True)
                self.assertEqual([mode, marker], result.stdout.splitlines())

    def test_modes_reach_only_their_fixed_wrapper_under_both_lease_allowlists(self):
        runner = (ROOT/'tests/helpers/longhorn-runner.sh').read_text()
        local = re.search(r'case "\$module" in (.*?)esac', runner, re.S).group(1)
        remote = re.search(r'case "\$mode" in ([^)]+)\)', runner).group(1).split('|')
        live = (ROOT/'tests/helpers/longhorn-live.sh').read_text()
        branches = re.findall(r'^  ([a-z0-9|_-]+)\)\n    exec bash "\$BASE/tests/helpers/([^"]+)" "\$MODE"', live, re.M)
        for mode, (wrapper, _) in ROUTES.items():
            with self.subTest(mode=mode):
                self.assertRegex(local, r'(?<![a-z0-9_-])'+re.escape(mode)+r'(?![a-z0-9_-])')
                self.assertIn(mode, remote)
                self.assertEqual([wrapper], [name for modes, name in branches if mode in modes.split('|')])
        self.assertIn('longhorn_local_gate static LONGHORN_STATIC_PASS || return', runner)
        self.assertIn('flock -n 9', runner)
        self.assertIn('flock -n 7', runner)
        self.assertIn('flock -n 8', runner)


if __name__ == '__main__':
    unittest.main()

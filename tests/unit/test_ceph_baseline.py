import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import ceph_baseline as baseline


class CephBaselineTests(unittest.TestCase):
    def test_focused_failure_keeps_original_exit_and_runs_owned_fallback_when_reuse_is_unproven(self):
        helper = Path(__file__).resolve().parents[2] / "tests/helpers/ceph_focused_baseline.sh"
        for retained in (True, False):
            with self.subTest(retained=retained), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                (state / "focused-baseline.json").write_text("owned checkpoint")
                script = '''set -euo pipefail
source "$1"
STATE_DIR="$2"
RESTORE_RC="$3"
restore_focused_module() { return "$RESTORE_RC"; }
failure_cleanup() { local rc=$?; echo OWNED_FALLBACK; exit "$rc"; }
trap focused_failure_cleanup EXIT
exit 23
'''
                result = subprocess.run(["bash", "-c", script, "fixture", str(helper), str(state),
                                         "0" if retained else "1"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertEqual("OWNED_FALLBACK" in result.stdout, not retained, result.stdout)
                self.assertEqual((state / "focused-baseline.json").exists(), retained)

    def test_healthy_focused_baseline_reuse_does_not_repeat_product_setup(self):
        helper = Path(__file__).resolve().parents[2] / "tests/helpers/ceph_focused_baseline.sh"
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "focused-baseline.json").write_text("owned checkpoint")
            script = '''set -euo pipefail
source "$1"
STATE_DIR="$2"
MODE=--slow-osd-fixed
load_focused_baseline() { echo VERIFIED_EXISTING_PRODUCT; }
prepare_focused_environment() { echo FORBIDDEN_REINSTALL; exit 42; }
prepare_or_reuse_focused_environment
'''
            result = subprocess.run(["bash", "-c", script, "fixture", str(helper), str(state)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CEPH_FOCUSED_BASELINE_REUSED", result.stdout)
            self.assertNotIn("FORBIDDEN_REINSTALL", result.stdout)

    def test_upgrade_rebuilds_its_source_and_a_foreign_checkpoint_is_preserved(self):
        helper = Path(__file__).resolve().parents[2] / "tests/helpers/ceph_focused_baseline.sh"
        for owner in ("kubeauto-ceph-regression", "foreign"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                (state / "focused-baseline.json").write_text("target version checkpoint")
                (state / "owner").write_text(owner)
                script = '''set -euo pipefail
source "$1"
STATE_DIR="$2"
OWNER_FILE="$STATE_DIR/owner"
MODE=--upgrade
load_focused_baseline() { echo FORBIDDEN_TARGET_REUSE; exit 42; }
prepare_focused_environment() { echo EXACT_SOURCE_SETUP; }
prepare_slow_osd_context() { :; }
ceph_shell() { :; }
prepare_or_reuse_focused_environment
'''
                result = subprocess.run(["bash", "-c", script, "fixture", str(helper), str(state)], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, owner == "kubeauto-ceph-regression", result.stderr)
                self.assertNotIn("FORBIDDEN_TARGET_REUSE", result.stdout)
                self.assertEqual("EXACT_SOURCE_SETUP" in result.stdout, owner == "kubeauto-ceph-regression")
                self.assertEqual((state / "focused-baseline.json").exists(), owner == "foreign")

    def test_reuse_guard_rejects_an_earlier_hash_failure_even_if_a_later_check_returns_zero(self):
        root = Path(__file__).resolve().parents[2]
        helper = root / "tests/helpers/ceph_focused_baseline.sh"
        script = '''set -euo pipefail
source "$1"
verify_post_fault_hashes() { echo 'CEPH_STAGE_FAIL id=CEPH-28 reason=RADOS mismatch' >&2; return 0; }
if verify_focused_business_hashes; then exit 42; fi
INJECTOR_PID=123
NETEM_IFACE=
THROTTLE_UNIT=
RECOVERY_OSD_ID=
fail() { echo CEPH_STAGE_FAIL >&2; return 1; }
wait_clean() { echo WRONG_WAIT; }
if verify_focused_fault_boundary; then exit 43; fi
'''
        result = subprocess.run(["bash", "-c", script, "fixture", str(helper)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WRONG_WAIT", result.stdout)
        self.assertIn("RADOS mismatch", result.stderr)

    def test_health_probe_accepts_normal_flags_and_rejects_faults_and_foreign_nodes(self):
        fsid = "903e1999-28a2-40c3-96bd-b2964bb7174a"
        for fault in (None, "flag", "node", "pod", "network", "version"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, value in (("owner", "kubeauto-ceph-regression"), ("fsid", fsid),
                                    ("rados.sha256", "hash"), ("csi-marker", "marker"),
                                    ("disk-allowlist", "owned-disks"), ("fault-tables", "owned-linear-tables")):
                    (root / name).write_text(value)
                nodes = [{"metadata": {"name": "node-" + suffix, "uid": suffix}, "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "addresses": [{"type": "InternalIP", "address": "192.168.47." + suffix}]}}
                    for suffix in ("134", "135", "136", "131", "132", "137")]
                if fault == "node":
                    nodes[0]["status"]["addresses"][0]["address"] = "192.168.47.129"
                fixtures = [{"kind": "Pod", "metadata": {"name": name, "uid": name,
                    "labels": {"kubeauto.io/owner": "ceph-regression"}},
                    "status": {"containerStatuses": [{"ready": True}]}}
                    for name in ("storage-client", "cephfs-reader", "s3-client")]
                fixtures.extend({"kind": "PersistentVolumeClaim", "metadata": {"name": name, "uid": name},
                                 "status": {"phase": "Bound"}} for name in ("rbd-data", "cephfs-data"))
                if fault == "pod":
                    fixtures[1]["status"]["containerStatuses"] = []
                def execute(argv):
                    if "nodes" in argv:
                        rows = nodes
                    elif "pod,pvc" in argv:
                        rows = fixtures
                    elif "secret" in argv:
                        rows = [{"metadata": {"name": name, "uid": name}, "data": {"key": "private-key-value"}}
                                for name in ("csi-rbd-secret", "csi-cephfs-secret")]
                    else:
                        rows = [fixtures[0]]
                    return json.dumps({"items": rows})
                def remote(host, command):
                    if "versions" in command:
                        return json.dumps({"overall": {"ceph version " + ("20.2.3" if fault == "version" else "20.2.4") + " stable": 12}})
                    if "osd dump" in command:
                        return json.dumps({"flags": "sortbitwise,recovery_deletes" + (",noout" if fault == "flag" else ""),
                                           "osds": [{"up": 1, "in": 1} for _ in range(12)]})
                    if "pool ls" in command:
                        return '["rbd"]'
                    if "auth ls" in command:
                        return '{"auth_dump": [{"entity": "client.test", "key": "private-key-value"}]}'
                    if "qdisc" in command:
                        return "qdisc netem 1:" if fault == "network" else "qdisc fq_codel 0:"
                    return fsid
                with patch.object(baseline, "verify_disks"), patch.object(baseline, "execute", side_effect=execute), patch.object(
                        baseline, "remote", side_effect=remote), patch.object(baseline, "source_dependencies", return_value={}):
                    if fault:
                        with self.assertRaises(ValueError):
                            baseline.identities(root, root)
                    else:
                        data = baseline.identities(root, root)
                        self.assertEqual(data["fsid"], fsid)
                        self.assertNotIn("private-key-value", json.dumps(data))

    def test_disk_reuse_rejects_changed_identity_size_system_overlap_and_foreign_targets(self):
        for fault in (None, "identity", "size", "system", "host"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                hosts = baseline.STORAGE.copy()
                if fault == "host":
                    hosts[0] = "192.168.122.1"
                rows = [host + "|/dev/vd" + letter + "|/dev/disk/by-path/pci-" + letter +
                        "|||path:pci-" + letter + "|1024" for host in hosts for letter in "bcd"]
                (state / "disk-allowlist").write_text("\n".join(rows) + "\n")
                def remote(host, command):
                    letter = next(letter for letter in "bcd" if "pci-" + letter + "'" in command)
                    return ("resolved=/dev/vd" + letter + "\nbytes=" + ("512" if fault == "size" else "1024") +
                            "\npath=" + ("changed" if fault == "identity" else "pci-" + letter) +
                            "\nserial=\nwwn=\nroot=/dev/vda\n" + ("root=/dev/vd" + letter if fault == "system" else ""))
                with patch.object(baseline, "remote", side_effect=remote) as probe:
                    if fault:
                        with self.assertRaises(ValueError):
                            baseline.verify_disks(state)
                    else:
                        baseline.verify_disks(state)
                        self.assertEqual(probe.call_count, 18)
                    if fault == "host":
                        probe.assert_not_called()

    def test_checkpoint_requires_exact_identity_and_preserves_original_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            original = {"fsid": "owned", "dependencies": {"product": "same"}, "pools": ["rbd"]}
            with patch.object(baseline, "identities", return_value=original):
                baseline.checkpoint(state, state, True)
                baseline.checkpoint(state, state, False)
                with self.assertRaisesRegex(ValueError, "replace"):
                    baseline.checkpoint(state, state, True)
            for change in ({"fsid": "foreign"}, {"dependencies": {"product": "changed"}}, {"pools": ["rbd", "stale-scrub"]}):
                with patch.object(baseline, "identities", return_value={**original, **change}):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        baseline.checkpoint(state, state, False)
            self.assertEqual(json.loads((state / "focused-baseline.json").read_text()), original)

    def test_baseline_refuses_every_unapproved_ssh_target_before_command(self):
        with patch.object(baseline, "execute") as execute:
            for host in ("192.168.122.1", "192.168.47.129", "foreign"):
                with self.assertRaisesRegex(ValueError, "foreign"):
                    baseline.remote(host, "true")
            execute.assert_not_called()

    def test_probe_failure_does_not_disclose_authentication_output(self):
        from types import SimpleNamespace
        with patch.object(baseline.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="private-key", stderr="private-token")):
            with self.assertRaises(ValueError) as failure:
                baseline.execute(["ssh", "-o", "BatchMode=yes", "root@approved", "auth query"])
        self.assertNotIn("private", str(failure.exception))


if __name__ == "__main__":
    unittest.main()

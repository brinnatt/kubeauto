"""Contracts for the independent tools matrix and runner boundary."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.helpers.validate_tools_test_matrix import validate_matrix


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests" / "tools-test-matrix.yaml"
RUNNER = ROOT / "tests" / "run_tools_regression.sh"


class ToolsMatrixContractTests(unittest.TestCase):
    def test_tools_matrix_summary_matches_cases(self):
        self.assertEqual(validate_matrix(MATRIX), [])
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        summary = data["coverage_summary"]
        counts = {
            status: sum(case["status"] == status for case in data["test_cases"])
            for status in ("pass", "pending", "fail", "skip", "na")
        }
        self.assertEqual(data["meta"]["status"], "approved")
        self.assertEqual({key: summary[key] for key in counts}, counts)

    def test_tools_matrix_require_pass_matches_case_status(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        errors = validate_matrix(MATRIX, require_pass=True)
        if all(case["status"] in {"pass", "na"} for case in data["test_cases"]):
            self.assertEqual(errors, [])
        else:
            self.assertIn(
                "matrix contains non-pass item(s); tools delivery PASS is prohibited",
                errors,
            )

    def test_public_function_inventory_is_fully_mapped_to_cases(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        inventory = data["functional_inventory"]
        self.assertEqual(set(inventory), {
            "NetCheckCli", "OvpnUserCli", "CalicoPolicyCli", "KubeBackupCli",
            "KubePublishCli", "KafkaCli", "MigrationCli", "MyBackupCli",
            "MyLogiBackupCli", "StarCli",
        })
        case_ids = {case["id"] for case in data["test_cases"]}
        capabilities = [cap for entries in inventory.values() for cap in entries]
        self.assertGreaterEqual(len(capabilities), 80)
        self.assertTrue(all(cap["case_ids"] and set(cap["case_ids"]) <= case_ids for cap in capabilities))

    def test_live_artifacts_record_existing_mylogi_talkedu_digests(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        artifacts = data["meta"]["artifact_prerequisites"]
        self.assertEqual(artifacts["owner_repository"], "kubeauto-ext-images-dockerfile")
        self.assertEqual(artifacts["publication"], "GitHub Actions dual-push")
        self.assertEqual(artifacts["primary_registry"], "hub.talkedu.cn/kubeauto")
        self.assertIn("manifest digest or SHA256", artifacts["required_evidence"])
        self.assertEqual(
            artifacts["mysql_tools"]["migration"],
            [
                "hub.talkedu.cn/kubeauto/mysql:8.0.46@sha256:0b6938c55ad3ef982d41cfa3ee01a63074cd5c9f3907487badeda53f6feb14da",
                "hub.talkedu.cn/kubeauto/mysql-8.4:8.4.4@sha256:0a3e659b9fb960330299e2a1847414f6185c573a3fd2cf1320221066904ea77d",
                "hub.talkedu.cn/kubeauto/mysql-9.2:9.2.0@sha256:867954f8c74131e891c2d3501560abbb7548ab8f439245c2feb4648f99b0caeb",
            ],
        )
        prerequisites = "\n".join(data["meta"]["live_prerequisites"])
        logical = artifacts["mysql_tools"]["logical_backup"]
        self.assertEqual(logical["owner"], "TalkEdu Hub existing published artifacts")
        self.assertIn("no GitHub Actions", logical["publication"])
        self.assertEqual(logical["images"], [
            "hub.talkedu.cn/kubeauto/mysql@sha256:2f27838ce14a31d6e434efb442658c4ce19a7cd0ec834e329ca213569faa7d3c",
            "hub.talkedu.cn/kubeauto/mysql-8.4@sha256:c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83",
            "hub.talkedu.cn/kubeauto/mysql-9.2@sha256:308515a860be3b21aa44ced3d39f7f91d800efe7ff3719c249f19a89a8480740",
        ])
        self.assertIn("不触发 ext-images", prerequisites)
        self.assertIn("无动态镜像发现", prerequisites)
        self.assertEqual(
            artifacts["tool_fixtures"]["kube_publish"],
            "hub.talkedu.cn/kubeauto/pause@sha256:"
            "1d048b53f4285cc9d20fbb8d7be785c50e9e4ccf4cf1194d9b176001862d900a",
        )

    def test_stale_summary_is_rejected(self):
        text = MATRIX.read_text(encoding="utf-8")
        pending = yaml.safe_load(text)["coverage_summary"]["pending"]
        text = text.replace(f"pending: {pending}", f"pending: {pending + 1}", 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tools.yaml"
            path.write_text(text, encoding="utf-8")
            errors = validate_matrix(path)
        self.assertTrue(any("coverage_summary.pending" in error for error in errors))

    def test_runner_is_a_separate_review_gated_entrypoint(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn("validate_tools_test_matrix.py", text)
        self.assertIn("TOOLS_MATRIX_APPROVED", text)
        self.assertNotIn("TOOLS_LIVE_NOT_IMPLEMENTED", text)
        self.assertIn("--build-only", text)
        self.assertIn("TOOLS_BUILD_EXIT", text)
        build_branch = text[text.index("  --build-only)"):text.index("  --star-artifact-prepare)")]
        self.assertIn("build_deadline=$((SECONDS + 1800))", build_branch)
        self.assertIn("TOOLS_BUILD_TIMEOUT seconds=1800", build_branch)
        self.assertIn('exit 124', build_branch)
        self.assertIn("flock -n 9", text)
        self.assertIn("run-durable-gate.sh", text)
        self.assertIn("TOOLS_CALICO_EXIT", text)
        self.assertIn("--kube-backup-live", text)
        self.assertIn("TOOLS_KUBE_BACKUP_EXIT", text)
        self.assertIn('env PYTHON="$PY" bash', text)
        self.assertIn("KUBE_PUBLISH_TOOL=/tmp/KubePublishCli.py", text)
        publish_branch = text[
            text.index("  --kube-publish-live)"):text.index("  --migration-live)")
        ]
        self.assertIn('finalized_rc="$(ssh', publish_branch)
        self.assertIn(
            'echo "KUBE_PUBLISH_DURABLE_STATUS rc=$gate_rc finalized=$finalized_rc"',
            publish_branch,
        )
        self.assertNotIn('test \\"$(cat', publish_branch)
        self.assertIn("--star-live", text)
        self.assertIn("TOOLS_STARCLI_EXIT", text)
        self.assertIn("--cross-live", text)
        self.assertIn("TOOLS_CROSS_LIVE_REGRESSION_PASS", text)
        self.assertIn("cross_host=root@192.168.47.131", text)
        self.assertIn("9df39a1d5bfac0249f0eef61a3ff74fcf7576c5947f23127c57e92267ad98ced", text)
        self.assertIn("b0c08a4b639b5fca9aa4943ecec614fe241a0cebd1a7b460093ccaeae70df698", text)
        self.assertIn("deadline=$((SECONDS + 180))", text)
        self.assertIn("trap 'exit 130' INT", text)
        self.assertIn("TOOLS_FULL_REGRESSION_PASS", text)
        self.assertIn("TOOLS_FULL_INNER_PASS", text)
        self.assertIn("TOOLS_CLEAN_VERIFY_PASS scope=all-tools", text)
        full = (ROOT / "tests/helpers/tools-full-regression.sh").read_text(encoding="utf-8")
        for option in (
            "--preflight", "--star-artifact-prepare", "--build-only", "--cross-live", "--calico-live",
            "--kube-backup-live", "--kafka-live", "--kube-publish-live",
            "--migration-live", "--mybackup-live", "--mylogi-backup-live", "--star-live",
        ):
            self.assertIn(f"run_stage {option}", full)
        fixture = (ROOT / "tests/helpers/prepare-starrocks-fixture.sh").read_text(encoding="utf-8")
        self.assertIn("https://releases.starrocks.io/starrocks/StarRocks-3.5.12-centos-amd64.tar.gz", fixture)
        self.assertIn("ec385951242bb3943141633bd73395a6668d23d6c64373bd546d9a2950fd76f9", fixture)
        self.assertIn('PARTIAL="${DEST}.partial"', fixture)
        self.assertIn("--silent --show-error", fixture)
        self.assertLess(fixture.index("sha256sum -c -"), fixture.index('mv -f "$PARTIAL" "$DEST"'))
        self.assertIn("--mylogi-backup-live", text)
        self.assertIn("TOOLS_MYLOGI_BACKUP_EXIT", text)
        self.assertIn("MYLOGI_LIVE_STAGE pre-clean-verified", text)
        self.assertIn("verify_mylogi_clean", text)
        self.assertIn("DockerRootDir", text)
        self.assertIn(
            "root@192.168.47.131 root@192.168.47.132 "
            "root@192.168.47.133 root@192.168.47.134",
            text,
        )
        for digest in (
            "2f27838ce14a31d6e434efb442658c4ce19a7cd0ec834e329ca213569faa7d3c",
            "c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83",
            "308515a860be3b21aa44ced3d39f7f91d800efe7ff3719c249f19a89a8480740",
        ):
            self.assertIn(digest, text)
        self.assertIn('"$STARCLI_BINARY" "$STAR_HOST:/tmp/StarCli"', text)
        self.assertIn("STARCLI_LIVE_STAGE pre-clean-verified", text)
        self.assertIn("systemctl kill --kill-who=all", text)
        self.assertIn('test "$(cat "${state}.exit")" = 0', text)
        star_branch = text[text.index("  --star-live)"):]
        self.assertNotIn('"cat \'${state}.exit\'"', star_branch)
        self.assertNotIn("run_enterprise_regression.sh", text)

    def test_mylogi_fixture_signals_container_process_and_uses_host_tar(self):
        fixture = (ROOT / "tests/helpers/mylogi-backup-live-regression.sh").read_text(
            encoding="utf-8"
        )
        source = (ROOT / "tools/mysqltools/MyLogiBackupCli.py").read_text(encoding="utf-8")
        self.assertIn("MYSQL_BACKUP_LOCK_ACQUIRED lock=\\/locks\\/signal.lock", fixture)
        self.assertNotIn('docker exec "$TOOL_CTN" kill -TERM "$signal_target"', fixture)
        self.assertGreaterEqual(fixture.count("MYLOGI_LOCK_FILE=/locks/signal.lock"), 2)
        self.assertIn("MYLOGI_LOCK_HELD_PASS", fixture)
        self.assertIn("MYLOGI_NON_INNODB_WARNING_PASS", fixture)
        self.assertIn("app.myisam_data(MyISAM)", fixture)
        self.assertIn("/proc/locks", fixture)
        self.assertIn('SIGNAL_CTN="mylogi-${VERSION//./-}-signal"', fixture)
        self.assertIn("MYLOGI_SIGNAL_LOCK_TARGET_MISSING", fixture)
        self.assertIn('-v "$RUN_ROOT/signal-login.cnf:/tmp/signal-login.cnf:ro"', fixture)
        self.assertNotIn('-v "$RUN_ROOT/signal-login.cnf:/root/.mylogin.cnf:ro"', fixture)
        self.assertIn("cp /tmp/signal-login.cnf /root/.mylogin.cnf", fixture)
        self.assertIn("chmod 0600 /root/.mylogin.cnf", fixture)
        self.assertIn("exec /tmp/MyLogiBackupCli backup --database app", fixture)
        self.assertIn('if [ "\\$#" -eq 1 ] && [ "\\$1" = --version ]; then', fixture)
        self.assertIn('exec "$MYSQLDUMP_BIN" "\\$@"', fixture)
        self.assertIn('docker kill --signal TERM "$SIGNAL_CTN"', fixture)
        self.assertIn('signal_rc="$(docker wait "$SIGNAL_CTN")"', fixture)
        self.assertNotIn('docker exec -i "$TOOL_CTN" env HOME=/root MYLOGI_MYSQL_BIN="$MYSQL_BIN" \\\n  MYLOGI_MYSQLDUMP_BIN=/tmp/slow-mysqldump', fixture)
        self.assertIn("--entrypoint sh", fixture)
        self.assertIn("-ec '", fixture)
        self.assertIn("/usr/libexec/mysqlsh/mysql_config_editor --version", fixture)
        self.assertIn('tar -xOf "$archive"', fixture)
        self.assertIn('docker exec -i "$RESTORE_CTN" env MYSQL_PWD="$PASSWORD" mysql -uroot', fixture)
        self.assertIn("archive_contains()", fixture)
        self.assertNotRegex(fixture, r"tar -xOf[^\n]+\| grep -[EF]*q")
        self.assertIn('"mysql-${app_scope}-*.tar.*"', fixture)
        self.assertNotIn("-name 'mysql-selected-*.tar.gz' | wc -l", fixture)
        self.assertNotIn('docker cp "$archive"', fixture)
        self.assertIn("MYLOGI_LIVE_REGRESSION_PASS", fixture)
        self.assertIn('"--source-data=2"', source)
        self.assertNotIn("--master-data", source)
        self.assertNotIn("MySQL 5.7", source)

    def test_mylogi_runner_names_the_binary_and_cannot_mask_remote_failures(self):
        runner = RUNNER.read_text(encoding="utf-8")
        branch = runner[runner.index("  --mylogi-backup-live)"):runner.index("  --star-live)")]
        self.assertIn('"$host:/tmp/MyLogiBackupCli"', branch)
        self.assertNotIn('"$tool_binary" \\\n        "$ROOT/tests/helpers/mylogi-backup-live-regression.sh"', branch)
        self.assertNotIn("done | tee", branch)
        self.assertIn("TOOLS_MYLOGI_BACKUP_EXIT rc=[^0]", branch)
        self.assertIn('if [[ "$rc" != 0 || "$finalized" != 0 ]]', branch)
        self.assertIn('if [[ "$gate_failed" -ne 0 ]]', branch)
        self.assertLess(branch.index('if [[ "$gate_failed" -ne 0 ]]'), branch.index("MYLOGI_BACKUP_LIVE_REGRESSION_PASS hosts="))
        self.assertIn("MYLOGI_HOST_GATE_PASS host=$host version=$version rc=$rc finalized=$finalized", branch)
        self.assertIn("hosts=131,132,133,134", branch)
        self.assertIn("MYLOGI_BACKUP_LIVE_REGRESSION_FAILED", branch)
        fixture = (ROOT / "tests/helpers/mylogi-backup-live-regression.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("MYLOGI_LIVE_COMMAND_FAILED line=", fixture)

    def test_mybackup_fixture_pulls_talkedu_images_before_digest_validation(self):
        fixture = (ROOT / "tests/helpers/mybackup-cli-live-regression.sh").read_text(
            encoding="utf-8"
        )
        pxb_pull = 'docker pull "$PXB_IMAGE"'
        mysql_pull = 'docker pull "$MYSQL_IMAGE"'
        pxb_inspect = 'docker image inspect "$PXB_IMAGE"'
        mysql_inspect = 'docker image inspect "$MYSQL_IMAGE"'
        self.assertLess(fixture.index(pxb_pull), fixture.index(pxb_inspect))
        self.assertLess(fixture.index(mysql_pull), fixture.index(mysql_inspect))
        self.assertEqual(fixture.count("timeout --signal=TERM --kill-after=15s 20m docker pull"), 2)
        self.assertIn("hub.talkedu.cn/kubeauto/percona-xtrabackup:8.4.0-5.1", fixture)
        self.assertIn("sha256:6f3f3735320eb77e7bb00300b37efd9a0cc797c34e2c637a2f687c77504d94b5", fixture)
        self.assertIn("sha256:c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83", fixture)
        self.assertNotIn("percona/percona-xtrabackup", fixture)

    def test_starcli_live_fixture_keeps_product_entrypoint_and_negative_cleanup(self):
        fixture = (ROOT / "tests/helpers/starcli-live-regression.sh").read_text(encoding="utf-8")
        self.assertIn('STARCLI_ARCHIVE_SHA256', fixture)
        self.assertIn('run python3 - "$ARCHIVE" "$ROOT"', fixture)
        self.assertIn("import tarfile", fixture)
        self.assertIn("os.path.commonpath((destination, target)) != destination", fixture)
        self.assertIn("absolute archive link has no sibling target", fixture)
        self.assertIn("member.linkname = os.path.basename(member.linkname)", fixture)
        self.assertIn('filter="data"', fixture)
        self.assertNotIn('run tar -xzf "$ARCHIVE"', fixture)
        self.assertIn("hub.talkedu.cn/kubeauto/mysql-8.4:8.4.4", fixture)
        self.assertIn("c26ba5d7363cdae3f0a31665b2ab9106397324dde56ed536364776901b924b83", fixture)
        self.assertIn('docker image inspect "$MYSQL_CLIENT_REF"', fixture)
        self.assertIn('--pull=never --network host', fixture)
        self.assertIn('-e MYSQL_PWD --entrypoint mysql', fixture)
        self.assertIn('export PATH="$ROOT/bin:$PATH"', fixture)
        self.assertIn('--setup --root-password', fixture)
        self.assertIn('--storage-root-path "../outside"', fixture)
        self.assertIn('--root-password "$PASSWORD" --user root --group root >/tmp/starcli-invalid.out', fixture)
        self.assertIn("grep -Eqi", fixture)
        self.assertIn("不能同时", fixture)
        self.assertIn('STARCLI_LIVE_REGRESSION_PASS', fixture)
        self.assertIn('rm -f /tmp/starcli-invalid.out', fixture)
        runner = RUNNER.read_text(encoding="utf-8")
        star_branch = runner[runner.index("  --star-live)"):runner.index("  --full)")]
        self.assertIn("starcli-mysql-client-", star_branch)
        self.assertIn("docker rm -f", star_branch)

    def test_starcli_fixture_python_extractor_rejects_path_traversal(self):
        fixture = (ROOT / "tests/helpers/starcli-live-regression.sh").read_text(
            encoding="utf-8"
        )
        marker = 'run python3 - "$ARCHIVE" "$ROOT" <<\'PY\'\n'
        start = fixture.index(marker) + len(marker)
        extractor = fixture[start:fixture.index("\nPY\n", start)]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "extract"
            destination.mkdir()
            valid_archive = root / "valid.tar.gz"
            with tarfile.open(valid_archive, "w:gz") as archive:
                payload = b"ok\n"
                member = tarfile.TarInfo("starrocks/be/lib/libjemalloc.so.2")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
                link = tarfile.TarInfo("starrocks/be/lib/libjemalloc.so")
                link.type = tarfile.SYMTYPE
                link.linkname = "/build/workspace/starrocks/be/lib/libjemalloc.so.2"
                archive.addfile(link)
            valid = subprocess.run(
                [sys.executable, "-", str(valid_archive), str(destination)],
                input=extractor,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            extracted_link = destination / "starrocks/be/lib/libjemalloc.so"
            self.assertEqual(extracted_link.read_bytes(), b"ok\n")
            self.assertEqual(extracted_link.readlink(), Path("libjemalloc.so.2"))

            malicious_archive = root / "malicious.tar.gz"
            with tarfile.open(malicious_archive, "w:gz") as archive:
                payload = b"escaped\n"
                member = tarfile.TarInfo("../escaped")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            rejected = subprocess.run(
                [sys.executable, "-", str(malicious_archive), str(destination)],
                input=extractor,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("archive member escapes destination", rejected.stderr)
            self.assertFalse((root / "escaped").exists())

            unsafe_link_archive = root / "unsafe-link.tar.gz"
            with tarfile.open(unsafe_link_archive, "w:gz") as archive:
                link = tarfile.TarInfo("starrocks/be/lib/unsafe")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                archive.addfile(link)
            unsafe_link = subprocess.run(
                [sys.executable, "-", str(unsafe_link_archive), str(destination)],
                input=extractor,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unsafe_link.returncode, 0)
            self.assertIn("absolute archive link has no sibling target", unsafe_link.stderr)

    def test_kube_backup_live_fixture_is_scoped_and_exercises_the_cli(self):
        fixture = (ROOT / "tests/helpers/kube-backup-live-regression.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("root@192.168.122.243", fixture)
        forbidden_host = "192.168.122." + "1"
        self.assertNotIn(forbidden_host, fixture)
        self.assertIn("KUBE_BACKUP_LIVE_REGRESSION_PASS", fixture)
        self.assertIn("KUBE_BACKUP_CLEAN_VERIFY_PASS", fixture)
        self.assertNotIn("tar -tzf", fixture)
        self.assertIn("import tarfile", fixture)
        self.assertIn("backup archive is missing backup-metadata.json", fixture)
        self.assertIn("--include-crds", fixture)
        self.assertIn("--namespace-mapping", fixture)
        self.assertIn("--merge-patch-kind", fixture)
        self.assertIn("'configmap kb-config'", fixture)
        self.assertIn("hub.talkedu.cn/kubeauto/busybox@sha256:3e0b302381acd9c4092a89b51ccc8727534f044b2b3db17f55575e27f62ec6cc", fixture)
        runner = RUNNER.read_text(encoding="utf-8")
        branch = runner[runner.index("  --kube-backup-live)"):runner.index("  --kafka-live)")]
        self.assertIn('rm -f "${state}.pid" "${state}.exit" "${state}.finalized"', branch)

    def test_kube_backup_archive_checker_requires_metadata_without_a_pipeline(self):
        fixture = (ROOT / "tests/helpers/kube-backup-live-regression.sh").read_text(
            encoding="utf-8"
        )
        marker = '"$PYTHON_BIN" - "$primary.tar.gz" <<\'PY\'\n'
        start = fixture.index(marker) + len(marker)
        checker = fixture[start:fixture.index("\nPY\n", start)]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_archive = root / "valid.tar.gz"
            with tarfile.open(valid_archive, "w:gz") as archive:
                payload = b"{}\n"
                member = tarfile.TarInfo("primary/backup-metadata.json")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            valid = subprocess.run(
                [sys.executable, "-", str(valid_archive)],
                input=checker,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

            missing_archive = root / "missing.tar.gz"
            with tarfile.open(missing_archive, "w:gz") as archive:
                payload = b"data\n"
                member = tarfile.TarInfo("primary/data.yaml")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            missing = subprocess.run(
                [sys.executable, "-", str(missing_archive)],
                input=checker,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("backup archive is missing backup-metadata.json", missing.stderr)

    def test_rocky8_build_repairs_python_expat_abi_before_pip(self):
        build = (ROOT / "tests" / "helpers" / "build-tools-rocky8.sh").read_text(
            encoding="utf-8"
        )
        install = build.index("dnf install -y expat expat-devel")
        upgrade = build.index("dnf upgrade -y expat expat-devel")
        probe = build.index('python3.12 -c "import pyexpat"')
        self.assertLess(install, upgrade)
        self.assertLess(upgrade, probe)

    def test_kafka_live_gate_persists_evidence_then_removes_all_remote_state(self):
        runner = RUNNER.read_text(encoding="utf-8")
        fixture = (ROOT / "tests/helpers/kafka-cli-multinode-regression.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("KAFKA_FIXTURE_DIGEST=sha256:", runner)
        self.assertIn("KAFKA_FIXTURE_REPOSITORY=quay.io/strimzi/kafka", runner)
        self.assertIn('$KAFKA_FIXTURE_REPOSITORY@$KAFKA_FIXTURE_DIGEST', runner)
        self.assertNotIn('$KAFKA_FIXTURE_IMAGE@$KAFKA_FIXTURE_DIGEST', runner)
        self.assertIn('KAFKA_DURABLE_STATUS rc=$gate_rc finalized=$finalized_rc', runner)
        self.assertIn("cleanup_kafka_live() (", runner)
        self.assertIn("'${state}.pid' '${state}.exit' '${state}.finalized'", runner)
        self.assertIn("! test -e '$remote_log'", runner)
        self.assertIn("kafkacli-*.client.properties", runner)
        self.assertIn("python3 -m tarfile -c kafka.tgz kafka", runner)
        self.assertNotIn("tar -C '$KAFKA_MULTI_ROOT' -czf", runner)
        self.assertNotIn("/usr/lib/jvm/jre-21", runner)
        self.assertIn("dnf install -y java-17-openjdk-headless || exit 42", runner)
        self.assertIn("KAFKA_FIXTURE_RUNTIME_PASS", runner)
        self.assertIn('"set -euo pipefail; command -v python3', runner)
        self.assertIn("KAFKA_FIXTURE_RUNTIME_RC rc=%s", runner)
        live_fixture = (ROOT / "tests/helpers/kafka-cli-live-regression.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("KAFKA_CLI_LIVE_STAGE standalone-cluster-id", live_fixture)
        self.assertIn("new_cluster_id", live_fixture)
        self.assertNotIn("random-uuid | tail -n 1", live_fixture)
        pre_clean = runner.index('echo "KAFKA_LIVE_STAGE pre-clean"')
        ssh_bootstrap = runner.index('echo "KAFKA_LIVE_STAGE control-ssh-bootstrap"')
        pre_clean_verified = runner.index('echo "KAFKA_LIVE_STAGE pre-clean-verified"')
        lease = runner.index('echo "KAFKA_LIVE_STAGE lease-acquire', pre_clean_verified)
        self.assertLess(ssh_bootstrap, pre_clean)
        self.assertLess(pre_clean, pre_clean_verified)
        self.assertLess(pre_clean_verified, lease)
        cleanup = runner.index("cleanup_kafka_live", runner.index("grep -q '^KAFKA_CLI_MULTINODE"))
        clean_marker = runner.index("TOOLS_CLEAN_VERIFY_PASS scope=kafka", cleanup)
        self.assertLess(cleanup, clean_marker)
        self.assertIn('>"$MULTI_ROOT/broker-down.out"', fixture)
        self.assertNotIn(">/tmp/kafka-cli-multi-broker-down.out", fixture)

    def test_calico_interface_input_is_constrained_before_manifest_generation(self):
        source = (ROOT / "tools/k8stools/CalicoPolicyCli.py").read_text(encoding="utf-8")
        self.assertIn("仅允许真实网卡名", source)
        self.assertIn("含非法网卡名", source)

    def test_calico_cases_distinguish_the_live_gate_from_kdd_architecture(self):
        data = yaml.safe_load(MATRIX.read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in data["test_cases"]}
        calico = [case for case in cases.values() if case["tool"] == "CalicoPolicyCli"]
        self.assertTrue(calico)
        self.assertEqual(cases["TL-CAL-07"]["status"], "pass")
        self.assertEqual(cases["TL-CAL-08"]["status"], "na")

    def test_calico_live_fixture_uses_supported_delete_arguments(self):
        fixture = (ROOT / "tests/helpers/calico-live-regression.sh").read_text(encoding="utf-8")
        self.assertNotIn('--delete-hostendpoints --hep-prefix', fixture)
        self.assertIn('CALICO_LIVE_REGRESSION_PASS', fixture)
        self.assertIn('hub.talkedu.cn/kubeauto/busybox@sha256:3e0b302381acd9c4092a89b51ccc8727534f044b2b3db17f55575e27f62ec6cc', fixture)
        self.assertNotIn('image: busybox:', fixture)
        self.assertIn('--dry-run=client', fixture)
        self.assertIn('--dry-run=server', fixture)

    def test_calico_live_fixture_owns_its_namespace(self):
        fixture = (ROOT / "tests/helpers/calico-live-regression.sh").read_text(encoding="utf-8")
        runner = RUNNER.read_text(encoding="utf-8")
        self.assertIn("NS=kubeauto-tools-calico-live", fixture)
        self.assertIn('kubectl create namespace "$NS"', fixture)
        self.assertIn('kubectl delete namespace "$NS" --ignore-not-found --wait=true', fixture)
        self.assertIn("CALICO_LIVE_NAMESPACE_NOT_CLEAN", fixture)
        self.assertIn("! kubectl get namespace kubeauto-tools-calico-live", runner)
        self.assertIn("kubectl describe pod", fixture)
        self.assertIn("kubectl get events", fixture)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from common.constants import KubeConstant


ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT.parent
ROLE = ROOT / "roles" / "cluster-addon"
CHART = ROLE / "files" / "pxc-operator-1.20.0.tgz"
CHART_SHA256 = "b8bff81d0f9691b1e495f958fba1bb268e48838cf548adf6ae6570ea0e0059cf"
TASKS = (ROLE / "tasks" / "percona-pxc.yml").read_text()
CLUSTER_TEMPLATE = (ROLE / "templates" / "percona-pxc" / "cluster.yaml.j2").read_text()
OPERATOR_VALUES = (ROLE / "templates" / "percona-pxc" / "operator-values.yaml.j2").read_text()
CONFIG = (ROOT / "conf" / "config.yml").read_text()
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
MYSQL_GATE = (ROOT / "tests" / "helpers" / "mysql-regression.sh").read_text()
MYSQL_CLEANUP = (ROOT / "tests" / "helpers" / "mysql-cleanup.sh").read_text()
MYSQL_STAGE = (ROOT / "tests" / "helpers" / "mysql-stage-images.sh").read_text()
MYSQL_NODE_STAGE = (ROOT / "tests" / "helpers" / "mysql-stage-node-image.sh").read_text()
SYNC_HELPER = (ROOT / "tests" / "helpers" / "sync-kubeauto.sh").read_text()


class PerconaPxcDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()

    def test_ga_versions_and_component_images_are_locked(self):
        self.assertEqual(self.kc.v_pxc_operator, "1.20.0")
        self.assertEqual(self.kc.v_pxc, "8.4.8-8.1")
        self.assertEqual(self.kc.v_pxc_xtrabackup, "8.4.0-5.1")
        self.assertEqual(self.kc.v_pxc_haproxy, "2.8.18-1")
        self.assertEqual(self.kc.v_pxc_fluentbit, "5.0.6-1")
        self.assertEqual(
            self.kc.component_images["mysql"],
            [
                "brinnatt/percona-xtradb-cluster-operator:1.20.0",
                "brinnatt/percona-xtradb-cluster:8.4.8-8.1",
                "brinnatt/percona-xtrabackup:8.4.0-5.1",
                "brinnatt/percona-haproxy:2.8.18-1",
                "brinnatt/percona-fluentbit:5.0.6-1",
            ],
        )
        self.assertNotRegex("\n".join(self.kc.component_images["mysql"]), r":(latest|main[^:]*)$")

    def test_official_operator_chart_is_vendored_and_verified(self):
        digest = hashlib.sha256(CHART.read_bytes()).hexdigest()
        self.assertEqual(digest, CHART_SHA256)
        self.assertEqual(self.kc.v_pxc_operator_chart_sha256, CHART_SHA256)
        with tarfile.open(CHART) as archive:
            names = set(archive.getnames())
        self.assertIn("pxc-operator/Chart.yaml", names)
        self.assertIn("pxc-operator/crds/crd.yaml", names)
        self.assertIn("pxc-operator/templates/deployment.yaml", names)

    def test_config_defaults_are_safe_and_explicit(self):
        for phrase in (
            'mysql_install: "no"',
            'mysql_storage_class: ""',
            "mysql_pxc_size: 3",
            "mysql_haproxy_size: 3",
            "mysql_tls_enabled: true",
            "mysql_backup_enabled: false",
            "mysql_pitr_enabled: false",
            "mysql_backup_verify_tls: true",
            "mysql_backup_ca_bundle_secret: \"\"",
            "mysql_backup_force_path_style: false",
            'mysql_pitr_storage_name: "s3-binlogs"',
            'mysql_secrets_name: ""',
        ):
            self.assertIn(phrase, CONFIG)
        self.assertNotRegex(CONFIG, r"(?m)^mysql_.*password:")

    def test_role_fails_early_and_uses_server_side_validation(self):
        for phrase in (
            "mysql_storage_class | length > 0",
            "mysql_pxc_size | int % 2 == 1",
            "mysql_haproxy_size | int % 2 == 1",
            "not (mysql_pitr_enabled | bool) or (mysql_backup_enabled | bool)",
            "mysql_pitr_storage_name != mysql_backup_storage_name",
            "mysql_backup_ca_bundle_key | length > 0",
            "sha256sum",
            "--dry-run=server",
            "--field-manager=kubeauto-mysql",
            "PXC_CONTROL_PLANE_READY",
            "app.kubernetes.io/component=pxc",
            "for attempt in $(seq 1 12)",
            "validatingwebhookconfiguration percona-xtradbcluster-webhook",
        ):
            self.assertIn(phrase, TASKS)
        self.assertNotIn("ignore_errors: true", TASKS)

    def test_cluster_template_has_production_safety_controls(self):
        for phrase in (
            "updateStrategy: SmartUpdate",
            "unsafeFlags:",
            "tls: false",
            "pxcSize: false",
            "proxySize: false",
            "antiAffinityTopologyKey: kubernetes.io/hostname",
            "maxUnavailable: 1",
            "type: ClusterIP",
            "onlyReaders: true",
            "gcache.recover=yes",
            "verifyTLS:",
            "forcePathStyle:",
            "checksumAlgorithm: SHA256",
            "caBundle:",
        ):
            self.assertIn(phrase, CLUSTER_TEMPLATE)
        self.assertEqual(CLUSTER_TEMPLATE.count("maxUnavailable: 1"), 2)
        self.assertNotRegex(CLUSTER_TEMPLATE, r"(?i)(password|secretKey):\s*[^<{\n]+")

    def test_all_runtime_images_use_the_local_registry(self):
        expected = {
            "percona-xtradb-cluster-operator",
            "percona-xtradb-cluster",
            "percona-xtrabackup",
            "percona-haproxy",
            "percona-fluentbit",
        }
        refs = set(re.findall(r"registry\.talkschool\.cn:5000/brinnatt/([a-z0-9-]+):", CLUSTER_TEMPLATE + OPERATOR_VALUES))
        self.assertEqual(refs, expected)

    def test_cluster_template_renders_valid_disabled_and_s3_modes(self):
        env = Environment(undefined=StrictUndefined)
        env.filters["bool"] = bool
        env.filters["quote"] = lambda value: json.dumps(str(value))
        template = env.from_string(CLUSTER_TEMPLATE)
        values = {
            "mysql_cluster_name": "cluster1",
            "mysql_namespace": "mysql",
            "mysql_secrets_name": "",
            "pxc_operator_ver": "1.20.0",
            "pxc_ver": "8.4.8-8.1",
            "pxc_haproxy_ver": "2.8.18-1",
            "pxc_fluentbit_ver": "5.0.6-1",
            "pxc_xtrabackup_ver": "8.4.0-5.1",
            "mysql_tls_enabled": True,
            "mysql_pxc_size": 3,
            "mysql_haproxy_size": 3,
            "mysql_gcache_size": "1G",
            "mysql_pxc_cpu_request": "2",
            "mysql_pxc_memory_request": "4Gi",
            "mysql_pxc_cpu_limit": "4",
            "mysql_pxc_memory_limit": "8Gi",
            "mysql_storage_class": "openebs-hostpath",
            "mysql_pvc_size": "100Gi",
            "mysql_haproxy_cpu_request": "500m",
            "mysql_haproxy_memory_request": "512Mi",
            "mysql_haproxy_cpu_limit": "1",
            "mysql_haproxy_memory_limit": "1Gi",
            "mysql_logcollector_enabled": False,
            "mysql_pitr_enabled": False,
            "mysql_backup_enabled": False,
            "mysql_pitr_upload_interval": 60,
            "mysql_backup_storage_name": "s3-prod",
            "mysql_backup_bucket": "",
            "mysql_backup_credentials_secret": "",
            "mysql_backup_region": "",
            "mysql_backup_endpoint": "",
            "mysql_backup_verify_tls": True,
            "mysql_backup_ca_bundle_secret": "",
            "mysql_backup_ca_bundle_key": "ca.crt",
            "mysql_backup_force_path_style": False,
            "mysql_backup_schedule": "0 1 * * *",
            "mysql_backup_retention_count": 7,
            "mysql_pitr_storage_name": "s3-binlogs",
            "mysql_pitr_bucket": "",
        }

        disabled = yaml.safe_load(template.render(**values))
        self.assertEqual(disabled["spec"]["secretsName"], "cluster1-secrets")
        self.assertFalse(disabled["spec"]["backup"]["pitr"]["enabled"])
        self.assertNotIn("storages", disabled["spec"]["backup"])
        self.assertNotIn("schedule", disabled["spec"]["backup"])

        values.update(
            mysql_backup_enabled=True,
            mysql_pitr_enabled=True,
            mysql_backup_bucket="pxc-prod",
            mysql_backup_credentials_secret="cluster1-backup-s3",
            mysql_backup_region="cn-north-4",
            mysql_backup_endpoint="https://s3.example.invalid",
            mysql_backup_ca_bundle_secret="s3-ca",
            mysql_backup_force_path_style=True,
            mysql_pitr_bucket="pxc-prod/binlogs",
        )
        enabled = yaml.safe_load(template.render(**values))
        backup = enabled["spec"]["backup"]
        self.assertTrue(backup["pitr"]["enabled"])
        self.assertEqual(backup["pitr"]["storageName"], "s3-binlogs")
        self.assertEqual(backup["storages"]["s3-prod"]["s3"]["bucket"], "pxc-prod")
        self.assertEqual(
            backup["storages"]["s3-prod"]["s3"]["caBundle"],
            {"name": "s3-ca", "key": "ca.crt"},
        )
        self.assertTrue(backup["storages"]["s3-prod"]["s3"]["forcePathStyle"])
        self.assertEqual(
            backup["storages"]["s3-binlogs"]["s3"]["bucket"],
            "pxc-prod/binlogs",
        )
        self.assertEqual(backup["schedule"][0]["retention"]["count"], 7)

    def test_ext_images_ci_and_dockerfiles_match_constants(self):
        repo = PROJECTS / "kubeauto-ext-images-dockerfile"
        ci = (repo / ".github" / "workflows" / "build.yml").read_text()
        images = {
            "percona-xtradb-cluster-operator": "1.20.0",
            "percona-xtradb-cluster": "8.4.8-8.1",
            "percona-xtrabackup": "8.4.0-5.1",
            "percona-haproxy": "2.8.18-1",
            "percona-fluentbit": "5.0.6-1",
        }
        for name, tag in images.items():
            dockerfile_path = f"./{name}/Dockerfile"
            context_path = f"./{name}/"
            self.assertRegex(
                ci,
                rf"(?s)dockerfile:\s*{re.escape(dockerfile_path)}.*?"
                rf"image:\s*brinnatt/{re.escape(name)}.*?"
                rf"tag:\s*['\"]?{re.escape(tag)}['\"]?.*?"
                rf"ctx:\s*{re.escape(context_path)}",
            )
            dockerfile = (repo / name / "Dockerfile").read_text()
            self.assertFalse((repo / f"Dockerfile.{name}").exists())
            upstream = name.removeprefix("percona-")
            if name == "percona-haproxy":
                upstream = "haproxy"
            elif name == "percona-fluentbit":
                upstream = "fluentbit"
            elif name == "percona-xtrabackup":
                upstream = "percona-xtrabackup"
            elif name == "percona-xtradb-cluster-operator":
                upstream = "percona-xtradb-cluster-operator"
            elif name == "percona-xtradb-cluster":
                upstream = "percona-xtradb-cluster"
            self.assertIn(f"FROM docker.io/percona/{upstream}:{tag}", dockerfile)

    def test_mysql_live_gate_is_independent_durable_and_cleanup_scoped(self):
        self.assertIn('--mysql-only', RUNNER)
        self.assertIn('--mysql-clean-only', RUNNER)
        self.assertIn('--mysql-source-probe <runtime-registry-prefix> [repository] [tag]', RUNNER)
        self.assertIn('mysql_probe_repository="${3:-percona/percona-xtradb-cluster-operator}"', RUNNER)
        self.assertIn('mysql_probe_tag="${4:-1.20.0}"', RUNNER)
        self.assertIn('--mysql-source-head <runtime-registry-prefix> [repository] [tag]', RUNNER)
        self.assertIn('mysql_head_repository="${3:-percona/percona-xtradb-cluster-operator}"', RUNNER)
        self.assertIn('mysql_head_tag="${4:-1.20.0}"', RUNNER)
        self.assertIn('MYSQL_IMAGE_VERIFY_PREFIXES=', RUNNER)
        self.assertIn('MYSQL_RUNTIME_IMAGE_FALLBACK_PREFIX', RUNNER)
        self.assertIn('source_repository()', MYSQL_GATE)
        self.assertIn('kubeauto/percona-xtradb-cluster-operator', MYSQL_GATE)
        self.assertIn('SOURCE_FALLBACK_PREFIX', MYSQL_GATE)
        self.assertIn('source_probe_digest" != sha256:*', MYSQL_GATE)
        self.assertIn('talkedu-dockerhub-dual-push', MYSQL_GATE)
        self.assertIn('brinnatt/${source_repository_path#kubeauto/}', MYSQL_GATE)
        self.assertIn('matching_verifier_digest', MYSQL_GATE)
        self.assertIn("IFS=',' read -r -a prefixes", MYSQL_GATE)
        self.assertIn('MYSQL_PXC_CLEAN_ONLY_PASS', RUNNER)
        self.assertIn('MYSQL_PXC_GATE_EXIT', RUNNER)
        self.assertIn('MYSQL_PXC_CORE_GATE_PASS', MYSQL_GATE)
        self.assertIn('--mysql-ssl=VERIFY_CA', MYSQL_GATE)
        self.assertIn('--mysql-ssl-ca=/etc/mysql/ssl-internal/ca.crt', MYSQL_GATE)
        self.assertIn('secretName: ${MYSQL_CLUSTER}-ssl-internal', MYSQL_GATE)
        self.assertNotIn('--mysql-ssl=on', MYSQL_GATE)
        self.assertIn('minio_object_evidence', MYSQL_GATE)
        self.assertIn('mc find "gate/$1" --print "{}"', MYSQL_GATE)
        self.assertIn('mc cat "$sample" | sha256sum', MYSQL_GATE)
        minio_evidence = MYSQL_GATE[
            MYSQL_GATE.index("minio_object_evidence()") : MYSQL_GATE.index("sysbench_command()")
        ]
        self.assertIn('while IFS= read -r object', minio_evidence)
        self.assertIn('size="$(mc cat "$object" | wc -c)"', minio_evidence)
        self.assertIn('count=$((count + 1))', minio_evidence)
        self.assertIn('bytes=$((bytes + size))', minio_evidence)
        self.assertNotIn('awk', minio_evidence)
        self.assertIn('full_object_path="${full_destination#s3://}"', MYSQL_GATE)
        self.assertIn('baseline_object_path="${baseline_destination#s3://}"', MYSQL_GATE)
        self.assertIn('full_object_path" =~ ^[A-Za-z0-9._:/+-]+$', MYSQL_GATE)
        self.assertNotIn('mc find "gate/$1" --type f', MYSQL_GATE)
        self.assertIn('MYSQL_PXC_FULL_GATE_PASS', RUNNER)
        self.assertIn('MYSQL_CLEAN_VERIFY_PASS', MYSQL_CLEANUP)
        self.assertIn("stage_pull_pids", MYSQL_CLEANUP)
        self.assertIn("ctr -n k8s.io images rm \"$source_ref\"", MYSQL_CLEANUP)
        self.assertIn("node_stage_source_residue", MYSQL_CLEANUP)
        self.assertIn('MYSQL_STAGE_IMAGES_PASS', MYSQL_STAGE)
        self.assertIn('MYSQL_IMAGE_STAGE_NODE', RUNNER)
        self.assertIn('MYSQL_STAGE_EXPECTED_DIGEST', MYSQL_NODE_STAGE)
        self.assertIn('MYSQL_STAGE_STATE_DIR', MYSQL_NODE_STAGE)
        self.assertIn('kubeauto-mysql-stage-active-baseline', MYSQL_NODE_STAGE)
        self.assertIn("content active | awk 'NR > 1", MYSQL_NODE_STAGE)
        self.assertIn('abort_stage_ingest', MYSQL_CLEANUP)
        self.assertIn('containerd 2.1 local Store.Abort', MYSQL_CLEANUP)
        self.assertNotIn('content abort', MYSQL_CLEANUP)
        self.assertIn('wait_pxc_custom_resources_deleted', MYSQL_CLEANUP)
        self.assertIn('recover_orphaned_pxc_finalizers', MYSQL_CLEANUP)
        self.assertIn('recover_orphaned_pxc_webhooks', MYSQL_CLEANUP)
        self.assertIn('select(.metadata.deletionTimestamp != null)', MYSQL_CLEANUP)
        self.assertIn('endswith(".pxc.percona.com")', MYSQL_CLEANUP)
        self.assertIn('.clientConfig.service.namespace == $namespace', MYSQL_CLEANUP)
        self.assertIn('Refusing to remove mixed webhook configuration', MYSQL_CLEANUP)
        delete_cr = MYSQL_CLEANUP.index('kubectl -n "$MYSQL_NAMESPACE" delete')
        wait_cr = MYSQL_CLEANUP.index('wait_pxc_custom_resources_deleted', delete_cr)
        uninstall_operator = MYSQL_CLEANUP.index(
            'helm -n "$MYSQL_OPERATOR_NAMESPACE" uninstall', wait_cr
        )
        self.assertLess(delete_cr, wait_cr)
        self.assertLess(wait_cr, uninstall_operator)
        self.assertIn('MYSQL_NODE_ORPHAN_INGEST_ABORT_PASS', RUNNER)
        self.assertIn('ctr -n k8s.io images push --local --plain-http --platform', MYSQL_NODE_STAGE)
        self.assertIn('--manifest "$platform_digest"', MYSQL_NODE_STAGE)
        self.assertIn('json.load(sys.stdin)["mediaType"]', MYSQL_NODE_STAGE)
        self.assertIn('application/vnd.oci.image.manifest.v1+json', MYSQL_NODE_STAGE)
        self.assertIn('application/vnd.docker.distribution.manifest.v2+json', MYSQL_NODE_STAGE)
        self.assertIn('-H "Content-Type: ${platform_media_type}"', MYSQL_NODE_STAGE)
        self.assertIn("sha256:[0-9a-f]\\{64\\}", MYSQL_NODE_STAGE)
        self.assertIn('KUBEAUTO_SSH_JUMP', SYNC_HELPER)
        self.assertIn('KUBEAUTO_SYNC_SKIP_CONTROL_SETUP', SYNC_HELPER)
        for phrase in (
            'MYSQL-02 capacity, three failure domains and PVC read/write',
            '--ssl-mode=VERIFY_CA',
            '"users": [{',
            '"withGrantOption": false',
            'CREATE DATABASE kubeauto_forbidden',
            'scale deployment pxc-operator --replicas=0',
            "SET GLOBAL wsrep_provider_options='pc.weight=0'",
            '--grace-period=30 --wait=true',
            'scale deployment pxc-operator --replicas=1',
            'kubeauto.io/mysql-gate-reconcile=',
            'PXC_TWO_MEMBER_LOSS isolated_peer=${MYSQL_CLUSTER}-pxc-0',
            'PXC_TWO_MEMBER_CRASH injected=true quorum_weight=0',
            "Received NON-PRIMARY|status: non-primary",
            'members\\(1\\):',
            'remaining PXC member did not log Non-Primary size=1',
            'MYSQL_EXEC_TIMEOUT=10 mysql_exec',
            'kill-after=5s',
            'minority component unexpectedly accepted a write',
            'old root credential remained valid after rotation',
            'MYSQL-09 full S3 backup and object readability',
            'MYSQL-10 full restore and new backup baseline',
            'MYSQL-11 PITR transaction target',
            'BinlogGapDetected',
            "Backup doesn't guarantee consistent recovery with PITR",
            'MYSQL-12 fixed sysbench baseline and node-loss load',
            'SYSBENCH_RESULT',
            'SYSBENCH_COMMAND_FAILED',
            'minio/mc:RELEASE.2025-04-08T15-39-49Z',
        ):
            self.assertIn(phrase, MYSQL_GATE)
        self.assertNotRegex(MYSQL_GATE, r'(?m)echo\s+"\$(APP|OLD_ROOT|NEW_ROOT)_PASSWORD')
        self.assertLess(RUNNER.index('monitor_remote_job()'), RUNNER.index('if [[ "$MODE" == "--mysql-only" ]]'))
        all_delivery = RUNNER[RUNNER.index('delivery_modes=('):RUNNER.index(')', RUNNER.index('delivery_modes=('))]
        self.assertNotIn('--mysql-only', all_delivery)

    def _run_node_stage_fixture(self, root_media_type, inspect_output, blobs):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_path = tmp_path / "bin"
            bin_path.mkdir()
            inspect_file = tmp_path / "inspect.txt"
            inspect_file.write_text(inspect_output)
            blobs_file = tmp_path / "blobs.json"
            blobs_file.write_text(json.dumps(blobs))
            ctr = bin_path / "ctr"
            ctr.write_text(
                """#!/usr/bin/env bash
set -eu
if [[ "$*" == *"images inspect"* ]]; then
  count_file="$MOCK_STATE/inspect-count"
  count=0
  [[ ! -f "$count_file" ]] || count=$(cat "$count_file")
  count=$((count + 1))
  printf '%s' "$count" >"$count_file"
  (( count > 1 )) || exit 1
  cat "$MOCK_INSPECT"
elif [[ "$*" == *"content active"* ]]; then
  printf 'REF TYPE\n'
elif [[ "$*" == *"content get"* ]]; then
  digest="${*: -1}"
  python3 -c 'import json, os, sys; print(json.dumps(json.load(open(os.environ["MOCK_BLOBS"]))[sys.argv[1]]))' "$digest"
fi
"""
            )
            ctr.chmod(0o755)
            curl = bin_path / "curl"
            curl.write_text("#!/usr/bin/env bash\ncat >/dev/null\nprintf 201\n")
            curl.chmod(0o755)
            digest = next(iter(blobs))
            env = os.environ.copy()
            env.update(
                PATH=f"{bin_path}:{env['PATH']}",
                MOCK_STATE=tmp,
                MOCK_INSPECT=str(inspect_file),
                MOCK_BLOBS=str(blobs_file),
                MYSQL_STAGE_SOURCE_REF="source.invalid/example:1",
                MYSQL_STAGE_TARGET_REF="registry.invalid/brinnatt/example:1",
                MYSQL_STAGE_EXPECTED_DIGEST=digest,
                MYSQL_STAGE_STATE_DIR=tmp,
            )
            result = subprocess.run(
                ["bash", str(ROOT / "tests/helpers/mysql-stage-node-image.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"media_type={root_media_type}", result.stdout)
            return result.stdout

    def test_node_stage_supports_single_platform_schema2_manifest(self):
        digest = "sha256:" + "a" * 64
        media_type = "application/vnd.docker.distribution.manifest.v2+json"
        output = (
            "source.invalid/example:1\n"
            f"└── {media_type} @{digest} (1480 bytes)\n"
        )
        stdout = self._run_node_stage_fixture(
            media_type,
            output,
            {digest: {"schemaVersion": 2, "mediaType": media_type}},
        )
        self.assertIn(f"platform_digest={digest}", stdout)

    def test_node_stage_selects_platform_manifest_from_image_index(self):
        index_digest = "sha256:" + "b" * 64
        platform_digest = "sha256:" + "c" * 64
        index_media_type = "application/vnd.oci.image.index.v1+json"
        platform_media_type = "application/vnd.oci.image.manifest.v1+json"
        output = (
            "source.invalid/example:1\n"
            f"└── {index_media_type} @{index_digest} (1000 bytes)\n"
            f"    ├── {platform_media_type} @{platform_digest} (900 bytes)\n"
            "    │   Platform: linux/amd64\n"
        )
        stdout = self._run_node_stage_fixture(
            platform_media_type,
            output,
            {
                index_digest: {"schemaVersion": 2, "mediaType": index_media_type},
                platform_digest: {"schemaVersion": 2, "mediaType": platform_media_type},
            },
        )
        self.assertIn(f"index_digest={index_digest}", stdout)
        self.assertIn(f"platform_digest={platform_digest}", stdout)

    def test_mysql_gate_minio_manifest_is_valid_multi_document_yaml(self):
        section = MYSQL_GATE[MYSQL_GATE.index("MYSQL-09/11 TLS S3 test dependency") :]
        heredoc_start = section.index("cat <<EOF | kubectl apply -f - >/dev/null")
        manifest_start = section.index("\n", heredoc_start) + 1
        manifest_end = section.index("\nEOF", manifest_start)
        manifest = section[manifest_start:manifest_end]
        replacements = {
            "${MYSQL_NAMESPACE}": "mysql",
            "${MYSQL_STORAGE_CLASS}": "local-path",
            "${REGISTRY_HOST}": "registry.talkschool.cn:5000",
            "${MINIO_IMAGE##*:}": "RELEASE.2025-04-08T15-41-24Z",
        }
        for source, target in replacements.items():
            manifest = manifest.replace(source, target)
        self.assertNotIn("${", manifest)
        resources = list(yaml.safe_load_all(manifest))
        self.assertEqual(
            [resource["kind"] for resource in resources],
            ["PersistentVolumeClaim", "Deployment", "Service"],
        )
        args = resources[1]["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual(args[-1], ":9001")

    def test_mysql_gate_defaults_to_official_sources_without_public_accelerators(self):
        self.assertIn('SOURCE_PREFIX="${MYSQL_IMAGE_SOURCE_PREFIX:-docker.io}"', MYSQL_GATE)
        self.assertIn('official_manifest_digest()', MYSQL_GATE)
        self.assertIn('registry_manifest_digest()', MYSQL_GATE)
        self.assertIn('Docker-Content-Digest'.lower(), MYSQL_GATE.lower())
        self.assertIn('two-independent-runtime-manifest-digests', MYSQL_GATE)
        self.assertIn('pulled_digest" == "$expected_digest', MYSQL_GATE)
        self.assertIn('docker.io/library/registry:2.8.3', MYSQL_GATE)
        self.assertIn('percona/percona-xtradb-cluster:8.4.8-8.1', MYSQL_GATE)
        production_files = (
            CLUSTER_TEMPLATE,
            OPERATOR_VALUES,
            TASKS,
            MYSQL_GATE,
            MYSQL_CLEANUP,
            MYSQL_NODE_STAGE,
            (ROOT / "tests" / "mysql-test-matrix.yaml").read_text(),
        )
        public_accelerators = r"docker\.sparkcr\.cn|status\.anye\.xyz|ghproxy|mirror\.ghproxy"
        for content in production_files:
            self.assertNotRegex(content, public_accelerators)

    def test_reconstruction_gate_rejects_the_terminating_old_pod(self):
        function = MYSQL_GATE[
            MYSQL_GATE.index("wait_recreated_pod_ready()") : MYSQL_GATE.index("mysql_exec()")
        ]
        self.assertIn('current_uid" != "$old_uid', function)
        self.assertIn('deletion_timestamp" == none', function)
        self.assertIn('phase" == Running', function)
        self.assertIn('ready" == True', function)
        self.assertIn('old_uid_gone=true', function)
        self.assertLess(
            function.index('current_uid" != "$old_uid'),
            function.index('ready" == True'),
        )
        failure_gate = MYSQL_GATE[
            MYSQL_GATE.index('echo "========== MYSQL-06/07') : MYSQL_GATE.index(
                'echo "========== MYSQL-13'
            )
        ]
        self.assertIn('wait_recreated_pod_ready', failure_gate)
        self.assertNotIn('wait --for=condition=Ready', failure_gate)

    def test_cluster_ready_gate_rejects_stale_cr_status(self):
        function = MYSQL_GATE[
            MYSQL_GATE.index("wait_pxc_ready()") : MYSQL_GATE.index(
                "wait_recreated_pod_ready()"
            )
        ]
        self.assertIn('app.kubernetes.io/component=pxc', function)
        self.assertIn('app.kubernetes.io/component=haproxy', function)
        self.assertIn('.metadata.deletionTimestamp == null', function)
        self.assertIn('.status.phase == "Running"', function)
        self.assertIn('.type == "Ready" and .status == "True"', function)
        self.assertIn('"$pxc_ready_pods" == 3', function)
        self.assertIn('"$haproxy_ready_pods" == 3', function)


if __name__ == "__main__":
    unittest.main()

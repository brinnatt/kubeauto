import hashlib
import re
import tarfile
import unittest
from pathlib import Path

import jinja2
import yaml

from common.constants import KubeConstant


ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "cluster-addon"
TASKS = (ROLE / "tasks" / "prometheus.yml").read_text()
VALUES = (ROLE / "templates" / "prometheus" / "values.yaml.j2").read_text()
GATE = (ROOT / "tests" / "helpers" / "prometheus-regression.sh").read_text()
ARTIFACT_GATE = (ROOT / "tests" / "helpers" / "prometheus-artifact-gate.sh").read_text()
LAB_WIPE = (ROOT / "tests" / "helpers" / "lab-wipe-nodes.sh").read_text()
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
BOOTSTRAP = (ROOT / "tests" / "helpers" / "bootstrap-brinnatt-mirrors.sh").read_text()
OPERATIONS = (ROOT / "docs" / "operations-manual.md").read_text()
WHITEPAPER = (ROOT / "docs" / "whitepaper" / "12-addons-observability.md").read_text()
DEVELOPMENT = (ROOT / "docs" / "development-manual.md").read_text()
VERSION_MATRIX = (ROOT / "docs" / "whitepaper" / "A-version-matrix.md").read_text()
CHART = ROLE / "files" / "kube-prometheus-stack-88.0.0.tgz"
PROVENANCE = CHART.with_suffix(CHART.suffix + ".prov")
EXPECTED_CHART_SHA256 = "f96cb0a0999f7375b2899e4b60e6ec0e8f7133e5847f39ac87d53baea765eb32"


class PrometheusDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.constants = KubeConstant()

    def test_prometheus_versions_are_locked(self):
        self.assertEqual(self.constants.v_promchart, "88.0.0")
        self.assertEqual(self.constants.v_prometheus, "v3.13.1-distroless")
        self.assertEqual(self.constants.v_prometheus_operator, "v0.93.0")
        self.assertEqual(self.constants.v_alertmanager, "v0.33.1")
        self.assertEqual(self.constants.v_grafana, "13.1.1")
        self.assertEqual(self.constants.v_busybox, "1.37")
        self.assertEqual(self.constants.v_kube_state_metrics, "v2.18.0")
        self.assertEqual(self.constants.v_node_exporter, "v1.12.1")
        self.assertEqual(self.constants.v_prometheus_admission_webhook, "v0.93.0")
        self.assertEqual(self.constants.v_prometheus_webhook_certgen, "1.8.5")

    def test_chart_is_vendored_checksum_locked_and_has_provenance(self):
        self.assertTrue(CHART.exists())
        self.assertGreater(CHART.stat().st_size, 100000)
        self.assertEqual(hashlib.sha256(CHART.read_bytes()).hexdigest(), EXPECTED_CHART_SHA256)
        provenance = PROVENANCE.read_text()
        self.assertIn(f"sha256:{EXPECTED_CHART_SHA256}", provenance)
        self.assertIn("version: 88.0.0", provenance)
        self.assertIn("-----BEGIN PGP SIGNATURE-----", provenance)
        with tarfile.open(CHART) as archive:
            names = set(archive.getnames())
            self.assertIn("kube-prometheus-stack/Chart.yaml", names)
            chart = yaml.safe_load(archive.extractfile("kube-prometheus-stack/Chart.yaml"))
            official_values = yaml.safe_load(archive.extractfile("kube-prometheus-stack/values.yaml"))
        self.assertEqual(chart["version"], "88.0.0")
        self.assertEqual(chart["appVersion"], "v0.93.0")
        self.assertIn("podDisruptionBudget", official_values["prometheus"])
        self.assertIn("podDisruptionBudget", official_values["alertmanager"])
        self.assertIn("deployment", official_values["prometheusOperator"]["admissionWebhooks"])

    def test_task_file_and_rendered_values_enable_production_ha_contract(self):
        self.assertIsInstance(yaml.safe_load(TASKS), list)
        rendered = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
        ).from_string(VALUES).render(
            K8S_VER="1.33.6",
            CLUSTER_NAME="prometheus-gate",
            prom_storage_class="local-path",
            groups={
                "etcd": ["192.168.122.243", "192.168.122.246", "192.168.122.217"],
                "kube_master": ["192.168.122.243", "192.168.122.246", "192.168.122.217"],
                "kube_node": ["192.168.122.210", "192.168.122.216", "192.168.122.193"],
            },
        )
        values = yaml.safe_load(rendered)
        self.assertEqual(values["prometheus"]["prometheusSpec"]["replicas"], 2)
        self.assertEqual(values["alertmanager"]["alertmanagerSpec"]["replicas"], 3)
        self.assertEqual(
            values["prometheus"]["prometheusSpec"]["image"]["tag"],
            self.constants.v_prometheus,
        )
        self.assertEqual(
            values["alertmanager"]["alertmanagerSpec"]["image"]["tag"],
            self.constants.v_alertmanager,
        )
        self.assertEqual(values["grafana"]["image"]["tag"], self.constants.v_grafana)
        self.assertEqual(values["grafana"]["initChownData"]["image"]["tag"], self.constants.v_busybox)
        self.assertEqual(values["grafana"]["initChownData"]["image"]["repository"], "brinnatt/busybox")
        self.assertEqual(
            values["prometheusOperator"]["image"]["tag"],
            self.constants.v_prometheus_operator,
        )
        self.assertEqual(
            values["kube-state-metrics"]["image"]["tag"],
            self.constants.v_kube_state_metrics,
        )
        self.assertEqual(
            values["prometheus-node-exporter"]["image"]["tag"],
            self.constants.v_node_exporter,
        )
        self.assertFalse(values["prometheus-node-exporter"]["image"]["distroless"])
        self.assertEqual(values["prometheus"]["podDisruptionBudget"]["minAvailable"], 1)
        self.assertEqual(values["alertmanager"]["podDisruptionBudget"]["minAvailable"], 2)
        self.assertNotIn("podDisruptionBudget", values["prometheus"]["prometheusSpec"])
        webhook = values["prometheusOperator"]["admissionWebhooks"]["deployment"]
        self.assertTrue(webhook["enabled"])
        self.assertEqual(webhook["replicas"], 2)
        self.assertEqual(webhook["podDisruptionBudget"]["minAvailable"], 1)
        required_anti_affinity = webhook["affinity"]["podAntiAffinity"]
        self.assertEqual(
            required_anti_affinity["requiredDuringSchedulingIgnoredDuringExecution"][0]
            ["topologyKey"],
            "kubernetes.io/hostname",
        )
        self.assertEqual(values["grafana"]["admin"]["existingSecret"], "grafana-admin")
        self.assertTrue(values["grafana"]["persistence"]["enabled"])
        self.assertEqual(values["grafana"]["persistence"]["storageClassName"], "local-path")
        self.assertEqual(
            values["prometheus"]["prometheusSpec"]["storageSpec"]
            ["volumeClaimTemplate"]["spec"]["storageClassName"],
            "local-path",
        )
        self.assertIn("openssl rand -hex 24", TASKS)
        self.assertNotIn("Admin1234!", VALUES)

    def test_gate_covers_required_lifecycle_and_api_checks(self):
        for phrase in (
            "PROM_STAGE_BEGIN id=artifact-stage",
            "PROM_STAGE_BEGIN id=capacity-preflight",
            "PROM_STAGE_BEGIN id=cluster-install",
            "PROM_STAGE_BEGIN id=addon-install",
            "PROM_STAGE_BEGIN id=helm-render",
            "PROM_STAGE_BEGIN id=prometheus-api",
            "PROM_STAGE_BEGIN id=grafana-api",
            "PROM_STAGE_BEGIN id=admission-and-failure-recovery",
            "PROM_STAGE_BEGIN id=alertmanager-notification",
            "PROM_STAGE_BEGIN id=idempotency-and-persistence",
            "PROM_STAGE_BEGIN id=promql-performance",
            "--atomic --timeout 90s",
            "admission webhook accepted an invalid PrometheusRule",
            "api/v1/targets",
            "api/v1/rules",
            "api/v1/alerts",
            "api/datasources",
            "api/search?type=dash-db",
            "api/v2/alerts",
            "send_resolved: true",
            "inhibitedBy",
            "group_by: ['delivery_id', 'severity']",
            "delivery-notification-warning",
            "brinnatt/json-mock:v1.3.1",
            '"$K" download -E network-check',
            "query_range",
            "PVC identity changed",
            "xargs -P 20",
            "PROMETHEUS_FULL_GATE_PASS",
            '"$K" download -E prometheus',
            '"$HELM" lint',
            '"$HELM" template',
            "192.168.122.243",
            "192.168.122.193",
        ):
            self.assertIn(phrase, GATE)

        inventory = re.search(r"cat > .*?\.hosts.*?<<'EOF'\n(.*?)\nEOF", GATE, re.S).group(1)
        self.assertNotIn("192.168.122.2 ", inventory)
        self.assertEqual(len(re.findall(r"^192\.168\.122\.", inventory, re.M)), 9)
        self.assertIn('prom_storage_class: "local-path"', GATE)
        self.assertIn('local_path_provisioner_install: "yes"', GATE)

    def test_runner_exposes_independent_prometheus_branch(self):
        self.assertIn('MODE" == "--prometheus-only"', RUNNER)
        self.assertIn("tests.unit.test_prometheus_delivery", RUNNER)
        self.assertIn('lab-wipe-nodes.sh" --rocky-only', RUNNER)
        self.assertIn('lab-wipe-nodes.sh" --verify --rocky-only', RUNNER)
        self.assertIn("lab-control-ssh-bootstrap.sh", RUNNER)
        self.assertIn("prometheus-artifact-gate.sh", RUNNER)
        self.assertIn("PROMETHEUS_DELIVERY_BRANCH_PASS", RUNNER)

    def test_artifact_gate_requires_dual_registry_multi_arch_identity(self):
        for phrase in (
            "hub.talkedu.cn/kubeauto/",
            '${dockerhub_verify_prefix}/brinnatt/',
            'IFS=, read -r -a dockerhub_verify_prefixes',
            "linux/amd64",
            "linux/arm64",
            "dual-push digest mismatch",
            "PROMETHEUS_ARTIFACT_GATE_PASS",
            'PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX:-docker.io',
            'test("^sha256:[0-9a-f]{64}$")',
            "printf '%s\\n' \"$digest\"",
            "local-path-provisioner:v0.0.31",
            "json-mock:v1.3.1",
        ):
            self.assertIn(phrase, ARTIFACT_GATE)
        self.assertIn("PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX", RUNNER)
        self.assertNotIn("docker.1panel.live", ARTIFACT_GATE + RUNNER)
        self.assertEqual(ARTIFACT_GATE.count("prometheus:v3.13.1-distroless"), 1)
        for image in self.constants.component_images["prometheus"]:
            self.assertIn(image.removeprefix("brinnatt/"), ARTIFACT_GATE)

    def test_lab_cleanup_removes_and_rejects_local_path_data(self):
        self.assertGreaterEqual(LAB_WIPE.count("/opt/local-path-provisioner"), 4)
        self.assertIn("LAB_CLEAN_VERIFY_PASS", LAB_WIPE)

    def test_active_bootstrap_and_customer_documents_match_release(self):
        release_pins = (
            "88.0.0",
            "v3.13.1-distroless",
            "v0.33.1",
            "13.1.1",
            "v0.93.0",
            "v1.12.1",
            "1.37",
            "v2.18.0",
        )
        for pin in release_pins[1:]:
            self.assertIn(pin, BOOTSTRAP)
        for document in (OPERATIONS, WHITEPAPER, DEVELOPMENT, VERSION_MATRIX):
            self.assertIn("prometheus", document.lower())
        for pin in release_pins:
            self.assertIn(pin, VERSION_MATRIX)
        self.assertIn("grafana-admin", OPERATIONS)
        self.assertIn("--prometheus-only", DEVELOPMENT)
        for stale in ("v3.4.2", "v0.83.0", "v0.28.1", "v1.9.1", "12.0.2", "v2.16.0"):
            self.assertNotIn(stale, BOOTSTRAP)
            self.assertNotIn(stale, VERSION_MATRIX)


if __name__ == "__main__":
    unittest.main()

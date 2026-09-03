import hashlib
import json
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
OPTIONAL_TASKS = (ROLE / "tasks" / "prometheus-optional.yml").read_text()
ADDON_TASKS = (ROLE / "tasks" / "main.yml").read_text()
VALUES = (ROLE / "templates" / "prometheus" / "values.yaml.j2").read_text()
BLACKBOX_PROBE = (ROLE / "templates" / "prometheus" / "blackbox-probe.yaml.j2").read_text()
GATE = (ROOT / "tests" / "helpers" / "prometheus-regression.sh").read_text()
OPTIONAL_GATE = (ROOT / "tests" / "helpers" / "prometheus-optional-regression.sh").read_text()
ARTIFACT_GATE = (ROOT / "tests" / "helpers" / "prometheus-artifact-gate.sh").read_text()
OPTIONAL_ARTIFACT_GATE = (ROOT / "tests" / "helpers" / "prometheus-optional-artifact-gate.sh").read_text()
DURABLE_GATE = (ROOT / "tests" / "helpers" / "run-durable-gate.sh").read_text()
CONFIG = (ROOT / "conf" / "config.yml").read_text()
LAB_WIPE = (ROOT / "tests" / "helpers" / "lab-wipe-nodes.sh").read_text()
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
BOOTSTRAP = (ROOT / "tests" / "helpers" / "bootstrap-brinnatt-mirrors.sh").read_text()
OPERATIONS = (ROOT / "docs" / "operations-manual.md").read_text()
WHITEPAPER = (ROOT / "docs" / "whitepaper" / "12-addons-observability.md").read_text()
DEVELOPMENT = (ROOT / "docs" / "development-manual.md").read_text()
VERSION_MATRIX = (ROOT / "docs" / "whitepaper" / "A-version-matrix.md").read_text()
CHART = ROLE / "files" / "kube-prometheus-stack-88.0.0.tgz"
BASELINE_CHART = ROLE / "files" / "kube-prometheus-stack-75.7.0.tgz"
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
        self.assertEqual(self.constants.v_thanos, "v0.41.0")
        self.assertEqual(self.constants.v_prometheus_adapter, "v0.12.0")
        self.assertEqual(self.constants.v_blackbox_exporter, "v0.27.0")

    def test_optional_prometheus_components_are_pinned_and_catalogued(self):
        optional = self.constants.component_images["prometheus-optional"]
        self.assertEqual(
            optional,
            [
                "brinnatt/thanos:v0.41.0",
                "brinnatt/prometheus-adapter:v0.12.0",
                "brinnatt/blackbox-exporter:v0.27.0",
            ],
        )
        for chart, version in (
            (ROLE / "files" / "prometheus-adapter-5.3.0.tgz", "5.3.0"),
            (ROLE / "files" / "prometheus-blackbox-exporter-11.3.1.tgz", "11.3.1"),
        ):
            self.assertTrue(chart.exists(), chart)
            with tarfile.open(chart) as archive:
                chart_yaml = yaml.safe_load(
                    archive.extractfile(next(name for name in archive.getnames() if name.endswith("/Chart.yaml")))
                )
            self.assertEqual(chart_yaml["version"], version)

    def test_adapter_registry_override_uses_the_official_chart_image_schema(self):
        chart = ROLE / "files" / "prometheus-adapter-5.3.0.tgz"
        with tarfile.open(chart) as archive:
            defaults = yaml.safe_load(archive.extractfile("prometheus-adapter/values.yaml"))
            deployment = archive.extractfile("prometheus-adapter/templates/deployment.yaml").read().decode()

        # Chart 5.3.0 has one image.repository field; image.registry is ignored.
        self.assertNotIn("registry", defaults["image"])
        self.assertIn(".Values.image.repository", deployment)
        adapter_task = OPTIONAL_TASKS.split("- name: 安装或升级 Prometheus Adapter", 1)[1].split(
            "- name: 等待 Prometheus Adapter Custom Metrics API", 1
        )[0]
        self.assertIn(
            "image.repository=registry.talkschool.cn:5000/brinnatt/prometheus-adapter",
            adapter_task,
        )
        self.assertNotIn("image.registry=registry.talkschool.cn:5000", adapter_task)

    def test_optional_extensions_are_explicitly_disabled_and_dependency_gated(self):
        self.assertIsInstance(yaml.safe_load(OPTIONAL_TASKS), list)
        for key in (
            'prom_install: "no"',
            'prom_thanos_install: "no"',
            'prom_adapter_install: "no"',
            'prom_blackbox_install: "no"',
            'prom_optional_uninstall: "no"',
        ):
            self.assertIn(key, CONFIG)
        self.assertIn("prometheus-optional.yml", ADDON_TASKS)
        self.assertIn('prom_install == "yes"', ADDON_TASKS)
        for phrase in (
            "prom_thanos_install",
            "prom_adapter_install",
            "prom_blackbox_install",
            "prom_optional_uninstall",
            "prometheus-adapter-5.3.0.tgz",
            "prometheus-blackbox-exporter-11.3.1.tgz",
            "thanos-querier",
            "custom.metrics.k8s.io",
            "prometheus-thanos-discovery",
            "prom_adapter_prometheus_url | default('http://prometheus-operated.' + prom_namespace + '.svc.cluster.local', true)",
            "校验 Blackbox Probe 目标",
            "item.name is match",
            "delete deployment/thanos-querier",
            "delete probe -l kubeauto.io/component=prometheus-optional",
        ):
            self.assertIn(phrase, OPTIONAL_TASKS)

    def test_blackbox_targets_use_the_documented_name_module_url_contract(self):
        environment = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
        )
        environment.filters["quote"] = json.dumps
        rendered = environment.from_string(BLACKBOX_PROBE).render(
            prom_namespace="monitor",
            prom_blackbox_probe_targets=[
                {
                    "name": "customer-http",
                    "module": "http_2xx",
                    "url": "https://app.example.com/healthz",
                },
                {
                    "name": "internal-ready",
                    "module": "http_2xx",
                    "url": "http://service.default.svc.cluster.local/ready",
                },
            ],
        )
        probes = list(yaml.safe_load_all(rendered))
        self.assertEqual([probe["metadata"]["name"] for probe in probes], [
            "kubeauto-blackbox-customer-http",
            "kubeauto-blackbox-internal-ready",
        ])
        self.assertEqual(probes[0]["spec"]["module"], "http_2xx")
        self.assertEqual(
            probes[0]["spec"]["targets"]["staticConfig"]["static"],
            ["https://app.example.com/healthz"],
        )

    def test_blackbox_service_name_is_explicitly_locked_to_the_probe_contract(self):
        chart = ROLE / "files" / "prometheus-blackbox-exporter-11.3.1.tgz"
        with tarfile.open(chart) as archive:
            helpers = archive.extractfile("prometheus-blackbox-exporter/templates/_helpers.tpl").read().decode()
            service = archive.extractfile("prometheus-blackbox-exporter/templates/service.yaml").read().decode()

        self.assertIn(".Values.fullnameOverride", helpers)
        self.assertIn('name: {{ include "prometheus-blackbox-exporter.fullname" . }}', service)
        blackbox_task = OPTIONAL_TASKS.split("- name: 安装或升级 Blackbox Exporter", 1)[1].split(
            "- name: 应用显式 Blackbox Probe 目标", 1
        )[0]
        self.assertIn("fullnameOverride=blackbox-exporter", blackbox_task)
        self.assertIn("fullnameOverride=blackbox-exporter", OPTIONAL_GATE)
        self.assertIn("blackbox-exporter.{{ prom_namespace }}.svc.cluster.local:9115", BLACKBOX_PROBE)

    def test_optional_customer_document_set_describes_same_switches(self):
        docs = [
            ROOT / "docs/middleware/prometheus/operations-manual.md",
            ROOT / "docs/middleware/prometheus/technical-whitepaper.md",
            ROOT / "docs/middleware/prometheus/development-manual.md",
        ]
        for document in docs:
            content = document.read_text()
            for key in ("prom_install", "prom_thanos_install", "prom_adapter_install", "prom_blackbox_install"):
                self.assertIn(key, content)
            self.assertIn("kubecli download -E prometheus-optional", content)

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
        with tarfile.open(CHART) as archive:
            prometheus_crd = archive.extractfile("kube-prometheus-stack/charts/crds/crds/crd-prometheuses.yaml").read().decode()
        self.assertIn('Default: "prometheus_replica"', prometheus_crd)

    def test_baseline_upgrade_chart_is_vendored_and_version_locked(self):
        self.assertTrue(BASELINE_CHART.exists())
        self.assertGreater(BASELINE_CHART.stat().st_size, 100000)
        self.assertEqual(
            hashlib.sha256(BASELINE_CHART.read_bytes()).hexdigest(),
            "754aeaefaf64352116e1cd0993b8c8ffc97a88f86adcedf16a6db6e8a462a791",
        )
        with tarfile.open(BASELINE_CHART) as archive:
            chart = yaml.safe_load(archive.extractfile("kube-prometheus-stack/Chart.yaml"))
        self.assertEqual(chart["version"], "75.7.0")
        self.assertEqual(chart["appVersion"], "v0.83.0")

    def test_task_file_and_rendered_values_enable_production_ha_contract(self):
        self.assertIsInstance(yaml.safe_load(TASKS), list)
        rendered = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
        ).from_string(VALUES).render(
            K8S_VER="1.33.6",
            CLUSTER_NAME="prometheus-gate",
            prom_chart_ver="88.0.0",
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
        self.assertEqual(values["alertmanager"]["alertmanagerSpec"]["resources"]["requests"]["cpu"], "100m")
        self.assertEqual(values["alertmanager"]["alertmanagerSpec"]["resources"]["limits"]["memory"], "512Mi")
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
        self.assertEqual(
            values["prometheusOperator"]["admissionWebhooks"]["patch"]["image"]["repository"],
            "brinnatt/prometheus-webhook-certgen",
        )
        self.assertTrue(values["grafana"]["persistence"]["enabled"])
        self.assertEqual(values["grafana"]["persistence"]["storageClassName"], "local-path")
        self.assertEqual(values["grafana"]["resources"]["requests"]["memory"], "512Mi")
        self.assertEqual(values["grafana"]["resources"]["limits"]["cpu"], "1")
        self.assertEqual(values["prometheus"]["prometheusSpec"]["resources"]["requests"]["cpu"], "1")
        self.assertEqual(values["prometheus"]["prometheusSpec"]["resources"]["limits"]["memory"], "8Gi")
        self.assertEqual(
            values["prometheus"]["prometheusSpec"]["storageSpec"]
            ["volumeClaimTemplate"]["spec"]["storageClassName"],
            "local-path",
        )
        self.assertIn("openssl rand -hex 24", TASKS)
        self.assertNotIn("Admin1234!", VALUES)
        self.assertNotIn("insecureSkipVerify: true", VALUES)
        self.assertGreaterEqual(VALUES.count("caFile: /etc/prometheus/secrets/etcd-client-cert/etcd-ca"), 3)

        baseline_rendered = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
        ).from_string(VALUES).render(
            K8S_VER="1.33.6",
            CLUSTER_NAME="prometheus-gate",
            prom_chart_ver="75.7.0",
            prom_storage_class="local-path",
            groups={"etcd": [], "kube_master": [], "kube_node": []},
        )
        baseline_values = yaml.safe_load(baseline_rendered)
        self.assertEqual(
            baseline_values["prometheusOperator"]["admissionWebhooks"]["patch"]["image"]["repository"],
            "brinnatt/kube-webhook-certgen",
        )
        self.assertEqual(
            baseline_values["prometheusOperator"]["admissionWebhooks"]["patch"]["image"]["tag"],
            "v1.6.0",
        )

    def test_thanos_replica_label_is_explicit_and_matches_querier(self):
        rendered = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
        ).from_string(VALUES).render(
            K8S_VER="1.33.6",
            CLUSTER_NAME="prometheus-gate",
            prom_chart_ver="88.0.0",
            prom_storage_class="local-path",
            prom_thanos_install="yes",
            prom_optional_uninstall="no",
            groups={"etcd": [], "kube_master": [], "kube_node": []},
        )
        values = yaml.safe_load(rendered)
        spec = values["prometheus"]["prometheusSpec"]
        self.assertEqual(spec["replicaExternalLabelName"], "prometheus_replica")
        self.assertEqual(spec["thanos"]["version"], self.constants.v_thanos)
        self.assertIn("--query.replica-label=prometheus_replica", OPTIONAL_TASKS)

    def test_thanos_live_gate_waits_for_the_updated_querier_rollout(self):
        # Counting ready Pods alone can mix old and new ReplicaSets while the
        # Service still selects a terminating backend.
        apply_index = OPTIONAL_GATE.index('"${K[@]}" apply -f "${RUN_DIR}/prometheus-thanos-querier.yaml"')
        rollout_index = OPTIONAL_GATE.index(
            '"${K[@]}" -n "$NAMESPACE" rollout status deployment/thanos-querier --timeout=10m'
        )
        ready_index = OPTIONAL_GATE.index("ready 'app=thanos-querier' 2", apply_index)
        self.assertLess(apply_index, rollout_index)
        self.assertLess(rollout_index, ready_index)

    def test_custom_metrics_raw_requests_do_not_use_kubectl_output_flags(self):
        # kubectl rejects --raw combined with -o/--output before making the API call.
        self.assertIn("get --raw /apis/custom.metrics.k8s.io/v1beta1 >", OPTIONAL_GATE)
        self.assertIn('get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/${NAMESPACE}/pods/*/pod_info" >', OPTIONAL_GATE)
        self.assertNotRegex(OPTIONAL_GATE, r"get --raw[^\n]*(?:-o|--output)\\b")

    def test_gate_covers_required_lifecycle_and_api_checks(self):
        for phrase in (
            "PROM_STAGE_BEGIN id=artifact-stage",
            "PROM_STAGE_BEGIN id=capacity-preflight",
            "PROM_STAGE_BEGIN id=cluster-install",
            "PROM_STAGE_BEGIN id=addon-install",
            "PROM_STAGE_BEGIN id=chart-upgrade",
            "PROM_STAGE_BEGIN id=helm-render",
            "PROM_STAGE_BEGIN id=prometheus-api",
            "PROM_STAGE_BEGIN id=grafana-api",
            "PROM_STAGE_BEGIN id=node-failure-recovery",
            "PROM_STAGE_BEGIN id=admission-and-failure-recovery",
            "PROM_STAGE_BEGIN id=alertmanager-notification",
            "PROM_STAGE_BEGIN id=idempotency-and-persistence",
            "PROM_STAGE_BEGIN id=promql-performance",
            "--atomic --timeout 90s",
            "admission webhook accepted an invalid PrometheusRule",
            "api/v1/targets",
            "query=up",
            "PROM_UP_HEARTBEAT",
            "api/v1/rules",
            "api/v1/alerts",
            "select(.state == \"firing\")",
            "increase(prometheus_rule_evaluation_failures_total[5m])",
            "startup/reload evaluation blip",
            "api/datasources",
            "api/datasources/uid/",
            "api/ds/query",
            "systemctl stop kubelet",
            "systemctl start kubelet",
            "repeated firing notification was not suppressed",
            "Alertmanager member failure and cluster recovery",
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
            "optional-branches",
            "75.7.0",
            '"$K" download -E prometheus',
            "current chart SHA256 mismatch",
            "baseline chart SHA256 mismatch",
            "chart_member()",
            "extract_chart_crds()",
            "tarfile.open",
            '"$HELM" lint',
            '"$HELM" template',
            "kubectl --kubeconfig=\"$KC\" diff --server-side",
            "--force-conflicts",
            "server-side rendered resource diff reviewed",
            '"$HELM" upgrade prometheus "$CHART"',
            "192.168.122.243",
            "192.168.122.193",
            'awk -v ip="$failure_node_ip" \'$6 == ip && name == "" {name = $1} END {print name}\'',
        ):
            self.assertIn(phrase, GATE)
        self.assertIn("charts/crds/crds/", GATE)
        self.assertIn('"$K" download -E prometheus-optional', GATE)
        self.assertLess(
            GATE.index('"$K" download -E prometheus-optional'),
            GATE.index('bash "${BASE}/tests/helpers/prometheus-optional-regression.sh"'),
        )
        performance_stage = GATE.split(
            'echo "PROM_STAGE_BEGIN id=promql-performance"', 1
        )[1].split('echo "PROM_STAGE_BEGIN id=optional-branches"', 1)[0]
        self.assertLess(
            performance_stage.index('stop_port_forward "$PROM_PF_PID"'),
            performance_stage.index(
                "start_port_forward svc/prometheus-operated 19090 9090 prometheus-performance"
            ),
        )

        for phrase in (
            "PROM_OPTIONAL_STAGE_BEGIN id=thanos",
            "PROM_OPTIONAL_STAGE_BEGIN id=product-config",
            "empty prom_adapter_prometheus_url did not fall back",
            "Kubeauto explicit optional uninstall removed owned resources and preserved core",
            "--endpoint=dnssrv+_grpc._tcp.",
            "--query.replica-label=prometheus_replica",
            "/api/v1/stores",
            "dedup=false",
            "thanos_dedup_ready=0",
            "Thanos query visibility pending",
            "Thanos dedup failed after query visibility wait",
            "query_range",
            "PROM_OPTIONAL_STAGE_BEGIN id=adapter",
            "v1beta1.custom.metrics.k8s.io",
            "pods/pod_info",
            "PROM_OPTIONAL_STAGE_BEGIN id=blackbox",
            "kind: Probe",
            "probe_success",
            "PROMETHEUS_OPTIONAL_FULL_GATE_PASS",
        ):
            self.assertIn(phrase, OPTIONAL_GATE)

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
        self.assertIn("prometheus-optional-artifact-gate.sh", RUNNER)
        self.assertIn("PROM_ARTIFACT_DOCKERHUB_VERIFY_PREFIX=${prom_runtime_prefix_quoted}", RUNNER)
        self.assertIn("PROMETHEUS_DELIVERY_BRANCH_PASS", RUNNER)

    def test_gate_run_files_are_isolated_and_namespace_wait_is_diagnostic(self):
        self.assertIn(
            'RUN_DIR="${PROM_RUN_DIR:-$(mktemp -d /tmp/kubeauto-prometheus-gate.XXXXXX)}"',
            GATE,
        )
        self.assertIn('PROM_RUN_DIR="$RUN_DIR"', GATE)
        self.assertIn(': "${PROM_RUN_DIR:?prometheus run directory is required}"', OPTIONAL_GATE)
        self.assertNotRegex(GATE, r"/tmp/(?:prometheus|grafana|alertmanager)-")
        self.assertNotRegex(OPTIONAL_GATE, r"/tmp/(?:prometheus|grafana|alertmanager)-")
        for phrase in (
            "namespace_wait_diagnostics()",
            "PROM_NAMESPACE_NOT_READY",
            "get pods -o wide",
            "get events --sort-by=.lastTimestamp",
            "describe \"pod/${pod}\"",
            "logs \"pod/${pod}\" --all-containers --tail=120",
            'namespace_wait_diagnostics "timeout"',
        ):
            self.assertIn(phrase, GATE)

    def test_durable_terminal_fence_precedes_prometheus_cleanup(self):
        self.assertIn('rm -f "${STATE_PREFIX}.exit" "${STATE_PREFIX}.finalized"', DURABLE_GATE)
        self.assertLess(
            DURABLE_GATE.index('printf \'%s\\n\' "$rc" > "${STATE_PREFIX}.exit"'),
            DURABLE_GATE.index('printf \'%s\\n\' "$rc" > "${STATE_PREFIX}.finalized"'),
        )
        self.assertIn("test -r '${state_prefix}.finalized'", RUNNER)
        self.assertIn("state=finalizing", RUNNER)
        self.assertIn("running|starting|finalizing)", RUNNER)

    def test_addon_rerun_reconciles_prometheus_even_when_operator_pod_exists(self):
        self.assertIn('- import_tasks: prometheus.yml', ADDON_TASKS)
        prometheus_block = ADDON_TASKS.split('- import_tasks: prometheus.yml', 1)[1].split(
            '- import_tasks: minio.yml', 1
        )[0]
        self.assertIn('when: \'prom_install == "yes"\'', prometheus_block)
        self.assertNotIn('kube-prometheus-operator', prometheus_block)

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

    def test_optional_artifact_gate_is_checksum_and_dual_registry_locked(self):
        self.assertIn("prometheus-adapter-5.3.0.tgz", OPTIONAL_ARTIFACT_GATE)
        self.assertIn("prometheus-blackbox-exporter-11.3.1.tgz", OPTIONAL_ARTIFACT_GATE)
        self.assertIn("PROMETHEUS_OPTIONAL_ARTIFACT_GATE_PASS", OPTIONAL_ARTIFACT_GATE)
        self.assertIn("linux/amd64,arm64", OPTIONAL_ARTIFACT_GATE)

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

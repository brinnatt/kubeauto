import hashlib
import re
import tarfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from common.constants import KubeConstant


ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT.parent
ROLE = ROOT / "roles" / "cluster-addon"
OPERATOR_CHART = ROLE / "files" / "strimzi-kafka-operator-1.2.0.tgz"
DRAIN_CHART = ROLE / "files" / "strimzi-drain-cleaner-1.6.1.tgz"
OPERATOR_SHA256 = "0f8a50b2f19bd99482f9fd6e17cf42902f72f9e594a136813ac3f0b7af422efd"
DRAIN_SHA256 = "ce84b8ddcd105f1b10d085fe69ed0d9185f798d009de3fba967386af2b8f6fdd"
TASKS = (ROLE / "tasks" / "kafka.yml").read_text()
CLUSTER_TEMPLATE = (ROLE / "templates" / "kafka" / "cluster.yaml.j2").read_text()
OPERATOR_VALUES = (ROLE / "templates" / "kafka" / "operator-values.yaml.j2").read_text()
DRAIN_VALUES = (ROLE / "templates" / "kafka" / "drain-cleaner-values.yaml.j2").read_text()
MONITORING_TEMPLATE = (ROLE / "templates" / "kafka" / "monitoring.yaml.j2").read_text()
CONFIG = (ROOT / "conf" / "config.yml").read_text()
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text()
GATE = (ROOT / "tests" / "helpers" / "kafka-regression.sh").read_text()
CLEANUP = (ROOT / "tests" / "helpers" / "kafka-cleanup.sh").read_text()
STORAGE_GATE = (ROOT / "tests" / "helpers" / "kafka-lab-storage.sh").read_text()
MATRIX = (ROOT / "tests" / "kafka-test-matrix.yaml").read_text()
KAFKA_DOC_DIR = ROOT / "docs" / "middleware" / "kafka"


def render_cluster(**overrides):
    values = {
        "kafka_cluster_name": "kafka-prod",
        "kafka_namespace": "kafka",
        "kafka_storage_class": "csi-block",
        "kafka_controller_size": 3,
        "kafka_broker_size": 3,
        "kafka_controller_pvc_size": "20Gi",
        "kafka_broker_pvc_size": "100Gi",
        "kafka_delete_claim": False,
        "kafka_controller_cpu_request": "500m",
        "kafka_controller_memory_request": "1Gi",
        "kafka_controller_cpu_limit": "2",
        "kafka_controller_memory_limit": "2Gi",
        "kafka_controller_heap": "768m",
        "kafka_broker_cpu_request": "2",
        "kafka_broker_memory_request": "4Gi",
        "kafka_broker_cpu_limit": "4",
        "kafka_broker_memory_limit": "8Gi",
        "kafka_broker_heap": "3g",
        "kafka_topology_key": "topology.kubernetes.io/zone",
        "kafka_ver": "4.3.1",
        "kafka_metadata_ver": "4.3-IV0",
        "kafka_default_topic_partitions": 6,
        "kafka_default_topic_retention_ms": 604800000,
        "kafka_cluster_ca_validity_days": 365,
        "kafka_cluster_ca_renewal_days": 30,
        "kafka_cruise_control_enabled": True,
        "kafka_metrics_enabled": True,
        "kafka_bootstrap_resources_enabled": True,
        "kafka_default_topic": "kubeauto-events",
        "kafka_app_user": "kafka-app",
        "kafka_app_password_secret": "kafka-app-password",
        "kafka_app_password_secret_key": "password",
        "kafka_default_group_prefix": "kubeauto-",
        "kafka_default_transactional_id_prefix": "kubeauto-",
        "kafka_user_producer_byte_rate": 10485760,
        "kafka_user_consumer_byte_rate": 10485760,
        "kafka_user_request_percentage": 50,
    }
    values.update(overrides)
    env = Environment(undefined=StrictUndefined)
    env.filters["bool"] = bool
    rendered = env.from_string(CLUSTER_TEMPLATE).render(**values)
    return [document for document in yaml.safe_load_all(rendered) if document]


def render_monitoring(**overrides):
    values = {
        "kafka_cluster_name": "kafka-prod",
        "kafka_namespace": "kafka",
        "kafka_operator_namespace": "kafka-operator",
        "kafka_monitoring_release_label": "prometheus",
    }
    values.update(overrides)
    rendered = Environment(undefined=StrictUndefined).from_string(MONITORING_TEMPLATE).render(**values)
    return [document for document in yaml.safe_load_all(rendered) if document]


class KafkaDeliveryTests(unittest.TestCase):
    def test_customer_documentation_has_combined_user_operations_manual(self):
        expected = {
            "operations-manual.md",
            "technical-whitepaper.md",
            "development-manual.md",
        }
        self.assertEqual({path.name for path in KAFKA_DOC_DIR.glob("*.md")}, expected)
        documents = "\n".join(
            (KAFKA_DOC_DIR / name).read_text(encoding="utf-8") for name in sorted(expected)
        )
        for title in ("用户与运维手册", "技术白皮书", "开发手册"):
            self.assertIn(title, documents)
        for internal_phrase in (
            "方案评审稿",
            "复盘基线",
            "本次评审",
            "方案获批后",
            "尚未编码",
            "第一期",
            "专项矩阵于",
            "我们",
        ):
            self.assertNotIn(internal_phrase, documents)

    def test_production_task_file_is_valid_yaml(self):
        tasks = yaml.safe_load(TASKS)
        self.assertIsInstance(tasks, list)
        self.assertTrue(tasks)

    @classmethod
    def setUpClass(cls):
        cls.kc = KubeConstant()

    def test_ga_versions_and_component_images_are_locked(self):
        self.assertEqual(self.kc.v_strimzi_operator, "1.2.0")
        self.assertEqual(self.kc.v_kafka, "4.3.1")
        self.assertEqual(self.kc.v_kafka_metadata, "4.3-IV0")
        self.assertEqual(self.kc.v_strimzi_drain_cleaner, "1.6.1")
        self.assertEqual(
            self.kc.component_images["kafka"],
            [
                "brinnatt/strimzi-operator:1.2.0",
                "brinnatt/strimzi-kafka:1.2.0-kafka-4.3.1",
                "brinnatt/strimzi-drain-cleaner:1.6.1",
            ],
        )

    def test_official_charts_are_vendored_and_checksum_verified(self):
        self.assertEqual(hashlib.sha256(OPERATOR_CHART.read_bytes()).hexdigest(), OPERATOR_SHA256)
        self.assertEqual(hashlib.sha256(DRAIN_CHART.read_bytes()).hexdigest(), DRAIN_SHA256)
        self.assertEqual(self.kc.v_strimzi_operator_chart_sha256, OPERATOR_SHA256)
        self.assertEqual(self.kc.v_strimzi_drain_cleaner_chart_sha256, DRAIN_SHA256)
        with tarfile.open(OPERATOR_CHART) as archive:
            names = set(archive.getnames())
        self.assertIn("strimzi-kafka-operator/crds/040-Crd-kafka.yaml", names)
        self.assertIn("strimzi-kafka-operator/crds/045-Crd-kafkanodepool.yaml", names)
        with tarfile.open(DRAIN_CHART) as archive:
            names = set(archive.getnames())
        self.assertIn("strimzi-drain-cleaner/templates/070-ValidatingWebhookConfiguration.yaml", names)

    def test_config_is_opt_in_and_has_no_secret_value(self):
        for phrase in (
            'kafka_install: "no"',
            'kafka_storage_class: ""',
            "kafka_controller_size: 3",
            "kafka_broker_size: 3",
            "kafka_operator_replicas: 2",
            "kafka_delete_claim: false",
            "kafka_cruise_control_enabled: true",
            "kafka_drain_cleaner_enabled: true",
            'kafka_app_password_secret: ""',
        ):
            self.assertIn(phrase, CONFIG)
        self.assertNotRegex(CONFIG, r"(?m)^kafka_.*password:\s*[^\"\n]+")

    def test_operator_values_preserve_version_aware_image_map(self):
        self.assertIn("defaultImageRegistry: registry.talkschool.cn:5000", OPERATOR_VALUES)
        self.assertEqual(OPERATOR_VALUES.count("tagPrefix: {{ strimzi_operator_ver }}"), 5)
        self.assertNotIn("tag: {{ strimzi_operator_ver }}-kafka-{{ kafka_ver }}", OPERATOR_VALUES)
        for name in ("strimzi-operator", "strimzi-kafka"):
            self.assertIn(f"name: {name}", OPERATOR_VALUES)
        self.assertIn("leaderElection:\n  enable: true", OPERATOR_VALUES)
        self.assertIn("replicas: {{ kafka_operator_replicas | int }}", OPERATOR_VALUES)
        self.assertIn("requiredDuringSchedulingIgnoredDuringExecution", OPERATOR_VALUES)

    def test_cluster_template_renders_production_kraft_contract(self):
        documents = render_cluster()
        self.assertEqual(
            [document["kind"] for document in documents],
            ["ConfigMap", "KafkaNodePool", "KafkaNodePool", "Kafka", "KafkaTopic", "KafkaUser"],
        )
        controller, broker = documents[1:3]
        self.assertEqual(controller["spec"]["roles"], ["controller"])
        self.assertEqual(controller["spec"]["replicas"], 3)
        self.assertEqual(broker["spec"]["roles"], ["broker"])
        self.assertEqual(broker["spec"]["replicas"], 3)
        for pool in (controller, broker):
            self.assertEqual(pool["spec"]["storage"]["type"], "persistent-claim")
            self.assertEqual(pool["spec"]["storage"]["class"], "csi-block")
            self.assertEqual(pool["spec"]["storage"]["kraftMetadata"], "shared")
            self.assertFalse(pool["spec"]["storage"]["deleteClaim"])
            self.assertEqual(
                pool["spec"]["template"]["pod"]["topologySpreadConstraints"][0]["topologyKey"],
                "topology.kubernetes.io/zone",
            )

        kafka = documents[3]["spec"]
        self.assertEqual(kafka["kafka"]["version"], "4.3.1")
        self.assertEqual(kafka["kafka"]["metadataVersion"], "4.3-IV0")
        self.assertEqual(kafka["kafka"]["listeners"], [{
            "name": "tls",
            "port": 9093,
            "type": "internal",
            "tls": True,
            "authentication": {"type": "scram-sha-512"},
            "networkPolicyPeers": [
                {"podSelector": {}},
                {"namespaceSelector": {"matchLabels": {"kubeauto.io/kafka-client": "true"}}},
            ],
        }])
        config = kafka["kafka"]["config"]
        self.assertEqual(config["default.replication.factor"], 3)
        self.assertEqual(config["min.insync.replicas"], 2)
        self.assertFalse(config["unclean.leader.election.enable"])
        self.assertFalse(config["auto.create.topics.enable"])
        self.assertEqual(kafka["cruiseControl"]["autoRebalance"], [
            {"mode": "add-brokers"}, {"mode": "remove-brokers"}
        ])
        self.assertIn("kafkaExporter", kafka)

        user = documents[5]["spec"]
        self.assertEqual(user["authentication"]["type"], "scram-sha-512")
        self.assertEqual(
            user["authentication"]["password"]["valueFrom"]["secretKeyRef"],
            {"name": "kafka-app-password", "key": "password"},
        )
        self.assertEqual(user["quotas"]["producerByteRate"], 10485760)
        resource_types = {acl["resource"]["type"] for acl in user["authorization"]["acls"]}
        self.assertEqual(resource_types, {"topic", "group", "transactionalId", "cluster"})

    def test_optional_resources_can_be_disabled_without_invalid_yaml(self):
        documents = render_cluster(
            kafka_cruise_control_enabled=False,
            kafka_metrics_enabled=False,
            kafka_bootstrap_resources_enabled=False,
            kafka_app_password_secret="",
        )
        self.assertEqual([document["kind"] for document in documents], [
            "ConfigMap", "KafkaNodePool", "KafkaNodePool", "Kafka"
        ])
        kafka = documents[-1]["spec"]
        self.assertNotIn("cruiseControl", kafka)
        self.assertNotIn("kafkaExporter", kafka)
        self.assertNotIn("metricsConfig", kafka["kafka"])

    def test_monitoring_template_renders_collection_and_alert_contract(self):
        playbook_vars = set(re.findall(r"^    (kafka_[a-z0-9_]+):", GATE, re.MULTILINE))
        monitoring_vars = set(re.findall(r"{{\s*(kafka_[a-z0-9_]+)", MONITORING_TEMPLATE))
        self.assertTrue(monitoring_vars <= playbook_vars)
        documents = render_monitoring()
        self.assertEqual(
            [document["kind"] for document in documents],
            ["PodMonitor", "PodMonitor", "PrometheusRule"],
        )
        self.assertEqual(documents[0]["spec"]["podMetricsEndpoints"][0]["port"], "tcp-prometheus")
        self.assertEqual(documents[1]["spec"]["podMetricsEndpoints"][0]["port"], "http")
        rules = [
            rule
            for group in documents[2]["spec"]["groups"]
            for rule in group["rules"]
        ]
        self.assertEqual(
            {rule["alert"] for rule in rules},
            {
                "KafkaUnderReplicatedPartitions",
                "KafkaOfflinePartitions",
                "KafkaNoActiveController",
                "KafkaConsumerLagHigh",
                "KafkaMetricsAbsent",
                "KafkaPersistentVolumeFillingUp",
            },
        )
        self.assertIn("{{ $labels.consumergroup }}", rules[3]["annotations"]["description"])
        self.assertIn("{{ $labels.persistentvolumeclaim }}", rules[5]["annotations"]["description"])

    def test_drain_cleaner_is_secure_without_cert_manager_dependency(self):
        for phrase in (
            "certManager:\n  create: false",
            "secret:\n  create: false",
            "ca_bundle: {{ kafka_drain_cleaner_ca_bundle }}",
            'value: "false"',
            "failurePolicy: Fail",
            "kubeauto.io/middleware: kafka",
            "runAsNonRoot: true",
            "readOnlyRootFilesystem: true",
            "minAvailable: 1",
        ):
            self.assertIn(phrase, DRAIN_VALUES)
        for phrase in (
            "openssl verify",
            "openssl x509 -in \"$TMP/tls.crt\" -noout -checkhost",
            "mktemp -d",
            "no_log: true",
            "rotated=true",
            "base64 -d",
        ):
            self.assertIn(phrase, TASKS)
        self.assertNotRegex(DRAIN_VALUES + TASKS, r"-----BEGIN (?:RSA )?PRIVATE KEY-----")

    def test_role_applies_crds_server_side_and_waits_for_business_resources(self):
        for phrase in (
            "tar -tzf",
            "tar -xOf",
            "test \"$crd_count\" -eq 10",
            "--field-manager=kubeauto-kafka-crd",
            "--dry-run=server",
            "--field-manager=kubeauto-kafka",
            "kafka_controller_size | int == 3",
            "kafka_broker_size | int >= 3",
            "kafka_operator_replicas | int >= 2",
            "KAFKA_WAIT_HEARTBEAT",
            "KAFKA_CONTROL_PLANE_READY",
            "kafkatopic/{{ kafka_default_topic }}",
            "kafkauser/{{ kafka_app_user }}",
        ):
            self.assertIn(phrase, TASKS)
        self.assertNotIn("ignore_errors: true", TASKS)

    def test_ext_images_ci_and_dockerfiles_match_release_contract(self):
        repo = PROJECTS / "kubeauto-ext-images-dockerfile"
        ci = (repo / ".github" / "workflows" / "build.yml").read_text()
        image_root = repo / "middleware" / "kafka" / "strimzi"
        expected = {
            "strimzi-operator": ("1.2.0", "quay.io/strimzi/operator:1.2.0"),
            "strimzi-kafka": ("1.2.0-kafka-4.3.1", "quay.io/strimzi/kafka:1.2.0-kafka-4.3.1"),
            "strimzi-drain-cleaner": ("1.6.1", "quay.io/strimzi/drain-cleaner:1.6.1"),
        }
        for name, (tag, upstream) in expected.items():
            self.assertRegex(
                ci,
                rf"(?s)dockerfile:\s*\./middleware/kafka/strimzi/{name}/Dockerfile.*?"
                rf"image:\s*brinnatt/{name}.*?"
                rf"talkedu_image:\s*hub\.talkedu\.cn/kubeauto/{name}.*?"
                rf"tag:\s*['\"]?{re.escape(tag)}['\"]?.*?ctx:\s*\./middleware/kafka/strimzi/{name}/",
            )
            self.assertIn(f"FROM {upstream}", (image_root / name / "Dockerfile").read_text())
            self.assertFalse((repo / f"Dockerfile.{name}").exists())

        self.assertRegex(
            ci,
            r"(?s)middleware/kafka/test-support/strimzi-kafka-4\.3\.0/Dockerfile.*?"
            r"image:\s*brinnatt/strimzi-kafka.*?tag:\s*['\"]?1\.2\.0-kafka-4\.3\.0",
        )
        longhorn_root = repo / "middleware" / "kafka" / "test-storage" / "longhorn"
        self.assertEqual(len(list(longhorn_root.glob("*/Dockerfile"))), 13)

    def test_runner_gate_and_cleanup_are_independent_and_auditable(self):
        for phrase in (
            "--kafka-only",
            "--kafka-status",
            "--kafka-follow",
            "--kafka-cancel",
            "--kafka-clean-only",
            "KAFKA_GATE_EXIT",
            "KAFKA_FULL_GATE_PASS",
            "KAFKA_DELIVERY_BRANCH_PASS",
        ):
            self.assertIn(phrase, RUNNER + GATE)
        for case_id in range(1, 20):
            self.assertIn(f"KAFKA-{case_id:02d}", GATE if case_id != 19 else MATRIX)
        for phrase in (
            "KAFKA_CLEAN_WAIT",
            "KAFKA_CLEAN_VERIFY_PASS",
            "KAFKA_CLEAN_RESIDUE cluster-resource=",
            "KAFKA_CLEAN_RESIDUE crd=",
            "api_resource_exists",
            "force_remove_owned_custom_resources",
            "force_finalize_owned_namespace",
            "/api/v1/namespaces/${namespace}/finalize",
            "force-remove-owned-finalizers",
            "action=wait-dependent-custom-resources",
            "action=force-remove-dependent-finalizers",
            "Kafka custom resource deletion timed out",
            ".kubeauto-kafka-gate",
            "refusing to delete unowned namespace",
            "ctr -n k8s.io images rm",
            "mapfile -t owned_crds",
            "if (( ${#owned_crds[@]} > 0 ))",
            "action=delete-owned-cluster-resources",
            "kubeauto.io/component=kafka,strimzi.io/cluster=${KAFKA_CLUSTER}",
        ):
            self.assertIn(phrase, CLEANUP)
        self.assertIn("grep -E '(kafka\\.strimzi\\.io|core\\.strimzi\\.io)$' || true", CLEANUP)
        self.assertIn("KAFKA_STORAGE_CLEAN_VERIFY_PASS", CLEANUP + STORAGE_GATE)
        self.assertNotIn("lab-wipe-nodes.sh", GATE + CLEANUP)

    def test_live_gate_statically_parses_role_before_stateful_preparation(self):
        self.assertIn("ansible.builtin.import_role:", GATE)
        self.assertIn("--syntax-check /tmp/kubeauto-kafka-playbook.yml", GATE)
        syntax_check = GATE.index("--syntax-check /tmp/kubeauto-kafka-playbook.yml")
        image_materialization = GATE.index("stage KAFKA-03")
        storage_preparation = GATE.index('kafka-lab-storage.sh\" prepare')
        self.assertLess(syntax_check, image_materialization)
        self.assertLess(syntax_check, storage_preparation)

    def test_ready_gate_requires_target_version_before_returning(self):
        self.assertIn('expected_version="${1:?wait_kafka_ready requires an expected Kafka version}"', GATE)
        self.assertIn('"$version" == "$expected_version"', GATE)
        self.assertIn("wait_kafka_ready 4.3.0", GATE)
        self.assertIn("wait_kafka_ready 4.3.1", GATE)

    def test_live_gate_covers_production_semantics(self):
        for phrase in (
            "skopeo inspect --raw",
            "source/verifier manifest digest mismatch",
            "kafka-metadata-quorum.sh",
            "KAFKA_TRANSACTION_COMMIT_ABORT_PASS",
            "wrong SCRAM password unexpectedly succeeded",
            "majority loss rejected mutation",
            "Cruise Control rebalance",
            '"6Gi"',
            "old password remained valid",
            "strimzi.io/force-renew=true",
            "kafka_consumergroup_",
            "kafka-producer-perf-test.sh",
            "KAFKA_PRODUCE_SUBMITTED",
            "kubeauto-upgrade-persisted",
            'kubectl -n "$KAFKA_NAMESPACE" exec -i "$CLIENT_POD"',
            "kubectl drain",
            "KAFKA_DRAIN_CLEANER_REFUSAL_PASS",
            "will be rolled by the Strimzi Cluster Operator",
            "node drain failed without the expected Drain Cleaner denial",
            "kubernetes.io/service-name=strimzi-drain-cleaner",
            "KAFKA-17_NOT_APPLICABLE",
        ):
            self.assertIn(phrase, GATE)
        self.assertNotRegex(GATE, r"\bcmp\s+/tmp/")
        self.assertIn('sha256sum /tmp/expected.txt', GATE)

    def test_client_uses_pem_contents_for_kafka_ssl_truststore(self):
        self.assertIn('while IFS= read -r line; do ca_pem+=', GATE)
        self.assertIn("ssl.truststore.certificates=${ca_pem}", GATE)
        self.assertNotIn("ssl.truststore.certificates=/opt/kafka/gate/ca/ca.crt", GATE)

    def test_new_branch_does_not_persist_dynamic_accelerators(self):
        texts = "\n".join((
            TASKS,
            CLUSTER_TEMPLATE,
            OPERATOR_VALUES,
            DRAIN_VALUES,
            MONITORING_TEMPLATE,
            GATE,
            CLEANUP,
            STORAGE_GATE,
            MATRIX,
        ))
        for forbidden in ("sparkcr", "gh-proxy", "status.anye", "v4.gh-proxy"):
            self.assertNotIn(forbidden, texts)

    def test_china_live_gate_does_not_require_public_registry_reverification(self):
        self.assertIn(
            'KAFKA_IMAGE_VERIFY_PREFIX="${KAFKA_IMAGE_VERIFY_PREFIX:-}"', GATE
        )
        self.assertIn("KAFKA_IMAGE_DUAL_PUSH_ONLINE_VERIFY_SKIPPED", GATE)
        self.assertIn("KAFKA_STORAGE_DUAL_PUSH_ONLINE_VERIFY_SKIPPED", STORAGE_GATE)
        self.assertIn('&& -n "$KAFKA_IMAGE_VERIFY_PREFIX"', GATE)
        self.assertIn('&& -n "$VERIFY_PREFIX"', STORAGE_GATE)

    def test_live_gate_uses_disposable_expandable_distributed_block_storage(self):
        chart = ROOT / "tests" / "fixtures" / "longhorn-1.10.1.tgz"
        self.assertEqual(
            hashlib.sha256(chart.read_bytes()).hexdigest(),
            "a871d397e19cf3243949abd41fd294869f5c2c490014f29e71866a2433ec7fb9",
        )
        for phrase in (
            "driver.longhorn.io",
            "allowVolumeExpansion: true",
            "volumeBindingMode: WaitForFirstConsumer",
            "KAFKA_STORAGE_LAB_PASS",
            'KAFKA_LAB_IMAGE_SOURCE_PREFIX:-hub.talkedu.cn/kubeauto',
            "Longhorn TalkEdu/Docker Hub digest mismatch",
            "KAFKA_STORAGE_DUAL_PUSH_ONLINE_VERIFY_SKIPPED",
            "source/official digest mismatch",
            "--override-arch amd64",
            "timeout --signal=TERM --kill-after=10s 300s",
            '.manifests[]? | select(.platform.os == "linux" and .platform.architecture == "amd64")',
            "kafka-lab/longhornio",
            "refusing to clean unowned storage namespace",
            "KAFKA_STORAGE_WAIT_HEARTBEAT",
            "Longhorn Ready nodes below six after 10m",
        ):
            self.assertIn(phrase, STORAGE_GATE + GATE)
        self.assertIn("KAFKA_LAB_IMAGE_SOURCE_PREFIX", RUNNER + STORAGE_GATE)
        self.assertIn("KAFKA_IMAGE_VERIFY_PREFIX", RUNNER + GATE)
        self.assertNotIn("gh-proxy", STORAGE_GATE)
        self.assertEqual(STORAGE_GATE.count("materialize_image longhornio/"), 13)


if __name__ == "__main__":
    unittest.main()

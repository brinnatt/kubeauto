"""Static delivery contracts for the opt-in EFK and Loki branch."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[2]
CONFIG = (ROOT / "conf/config.yml").read_text()
TASKS = (ROOT / "roles/cluster-addon/tasks/logging.yml").read_text()
ADDON_TASKS = (ROOT / "roles/cluster-addon/tasks/main.yml").read_text()
INGRESS_TASKS = (ROOT / "roles/cluster-addon/tasks/ingress-nginx.yml").read_text()
EFK = (ROOT / "roles/cluster-addon/templates/logging/efk.yaml.j2").read_text()
DIRECT = (ROOT / "roles/cluster-addon/templates/logging/fluent-bit-direct.yaml.j2").read_text()
KAFKA = (ROOT / "roles/cluster-addon/templates/logging/fluent-bit-kafka.yaml.j2").read_text()
LOKI = (ROOT / "roles/cluster-addon/templates/logging/loki-values.yaml.j2").read_text()
REGRESSION = (ROOT / "tests/helpers/logging-regression.sh").read_text()
CLEANUP = (ROOT / "tests/helpers/logging-cleanup.sh").read_text()
RUNNER = (ROOT / "tests/run_enterprise_regression.sh").read_text()


def render_efk(delivery: str):
    env = Environment(undefined=StrictUndefined)
    env.filters.update(quote=str, bool=bool, to_json=json.dumps)
    rendered = env.from_string(EFK).render(
        logging_efk_delivery=delivery,
        logging_efk_es_heap="4g",
        logging_efk_es_memory_limit="8Gi",
        logging_efk_es_memory_request="8Gi",
        logging_efk_es_nodes=["node-a", "node-b", "node-c"],
        logging_efk_es_pvc_size="100Gi",
        logging_efk_kibana_host="kibana.example.com",
        logging_efk_kibana_replicas=2,
        logging_efk_snapshot_endpoint="https://s3.example.com",
        logging_efk_snapshot_path_style=True,
        logging_efk_snapshot_region="region-1",
        logging_efk_snapshot_secret="logging-snapshot",
        logging_efk_storage_class="production-storage",
        logging_elasticsearch_ver="9.5.1",
        logging_ingress_class="nginx",
        logging_ingress_controller="ingress-nginx",
        logging_ingress_tls_secret="logging-ingress-tls",
        logging_kibana_ver="9.5.1",
        logging_monitoring_release_label="prometheus",
        logging_namespace="logging",
    )
    return [document for document in yaml.safe_load_all(rendered) if document]


def render_collector(template: str):
    env = Environment(undefined=StrictUndefined)
    env.filters.update(quote=str, bool=bool, to_json=json.dumps)
    rendered = env.from_string(template).render(
        kafka_cluster_name="logging-kafka",
        kafka_namespace="kafka",
        logging_efk_kafka_group="efk-replay",
        logging_efk_kafka_topic="efk-replay",
        logging_efk_kafka_user="efk-pipeline",
        logging_efk_logstash_replicas=2,
        logging_elasticsearch_ver="9.5.1",
        logging_fluent_bit_ver="5.1.1",
        logging_logstash_ver="9.5.1",
        logging_monitoring_release_label="prometheus",
        logging_namespace="logging",
    )
    return [document for document in yaml.safe_load_all(rendered) if document]


def render_loki_values():
    env = Environment(undefined=StrictUndefined)
    env.filters.update(quote=str, bool=bool, to_json=json.dumps)
    rendered = env.from_string(LOKI).render(
        logging_alloy_ver="1.18.1",
        logging_ingress_class="nginx",
        logging_ingress_tls_secret="logging-ingress-tls",
        logging_loki_bucket_admin="loki-admin",
        logging_loki_bucket_chunks="loki-chunks",
        logging_loki_bucket_ruler="loki-ruler",
        logging_loki_gateway_auth_secret="loki-gateway-auth",
        logging_loki_ingress_host="logs.example.com",
        logging_loki_memory_limit="4Gi",
        logging_loki_memory_request="2Gi",
        logging_loki_retention_days=30,
        logging_loki_storage_ca_configmap="loki-storage-ca",
        logging_loki_storage_class="production-rwo",
        logging_loki_storage_endpoint="https://s3.example.com",
        logging_loki_storage_secret="loki-storage",
        logging_loki_tolerate_control_plane=False,
        logging_loki_ver="3.7.6",
        logging_monitoring_release_label="prometheus",
        logging_namespace="logging",
    )
    return yaml.safe_load(rendered)


class LoggingDeliveryContracts(unittest.TestCase):
    def test_default_is_inert_and_routes_are_exclusive(self) -> None:
        self.assertIn('logging_install: "no"', CONFIG)
        self.assertIn('logging_solution: ""', CONFIG)
        self.assertIn("logging_solution in ['efk', 'loki']", TASKS)
        self.assertIn("requested={{ logging_solution }}", TASKS)
        self.assertIn("logging_efk_delivery in ['direct', 'kafka-buffer']", TASKS)

    def test_efk_production_floor_and_no_plaintext_credentials(self) -> None:
        for value in ("logging_efk_es_nodes | length == 3", "logging_efk_es_memory_request == '8Gi'", "logging_efk_es_heap == '4g'", "logging_efk_kibana_replicas | int == 2"):
            self.assertIn(value, TASKS)
        self.assertIn("secureSettings:", EFK)
        self.assertIn("secretName: {{ logging_efk_snapshot_secret }}", EFK)
        self.assertIn("envFrom: [{secretRef: {name: logging-fluent-bit-es-auth}}]", DIRECT)
        self.assertIn("storage.path /buffers/storage", DIRECT)
        self.assertIn("DB /buffers/tail-containers.db", DIRECT)
        self.assertIn("storage.backlog.mem_limit 50M", DIRECT)
        self.assertIn("mountPath: /buffers", DIRECT)
        self.assertIn("hostPath: {path: /var/lib/fluent-bit, type: DirectoryOrCreate}", DIRECT)
        self.assertIn("Suppress_Type_Name On", DIRECT)
        self.assertIn("ServiceMonitor", EFK)
        self.assertNotIn("insecureSkipVerify: true", EFK)
        self.assertIn("Parsers_File /fluent-bit/etc/parsers.conf", DIRECT)
        self.assertIn("Name cri", DIRECT)
        self.assertIn("subPath: parsers.conf", DIRECT)
        self.assertIn(
            "app.kubernetes.io/name: fluent-bit{{ '-kafka' if logging_efk_delivery == 'kafka-buffer' else '' }}",
            EFK,
        )
        self.assertNotIn(
            "{% if logging_efk_delivery == 'kafka-buffer' %}-kafka{% endif %}",
            EFK,
        )
        self.assertIn("logging_efk_writer_secret", CONFIG)
        self.assertIn("Bootstrap least-privilege Elasticsearch writer", TASKS)
        self.assertIn('"auto_configure","create","create_doc","index","view_index_metadata"', TASKS)
        self.assertIn("_ilm/policy/k8s-retention", TASKS)
        self.assertIn("_index_template/k8s-logs", TASKS)
        self.assertIn("_snapshot/logging-s3", TASKS)
        self.assertIn("_slm/policy/logging-daily", TASKS)
        self.assertIn('{schedule:"0 30 17 * * ?"', TASKS)
        self.assertNotIn('{schedule:"17:30"', TASKS)
        for step in ("role", "user", "ilm", "index-template", "snapshot-repository", "snapshot-verify", "slm"):
            self.assertIn(f"es_request {step}", TASKS)
        self.assertIn("s3.client.default.endpoint", EFK)
        self.assertIn("s3.client.default.path_style_access", EFK)
        self.assertIn("vm.max_map_count=1048576", TASKS)
        self.assertNotIn("logging_efk_snapshot_password", CONFIG)
        self.assertNotIn("logging_loki_gateway_password", CONFIG)
        self.assertNotIn("KUBE.v_", EFK + DIRECT)
        for key in ("logging_elasticsearch_ver", "logging_kibana_ver", "logging_fluent_bit_ver"):
            self.assertIn(key, CONFIG)

    def test_efk_collectors_have_bounded_resources(self) -> None:
        fluent_bit_resources = (
            "requests: {cpu: 50m, memory: 100Mi}\n"
            "            limits: {cpu: 500m, memory: 500Mi}"
        )
        self.assertIn(fluent_bit_resources, DIRECT)
        self.assertIn(fluent_bit_resources, KAFKA)
        self.assertIn('requests: {cpu: 500m, memory: 1Gi}', KAFKA)
        self.assertIn('limits: {cpu: "2", memory: 2Gi}', KAFKA)
        for template in (DIRECT, KAFKA):
            documents = render_collector(template)
            self.assertGreater(len(documents), 0)
            workloads = {
                (document["kind"], document["metadata"]["name"]): document
                for document in documents
                if document["kind"] in {"DaemonSet", "Deployment"}
            }
            for workload in workloads.values():
                for container in workload["spec"]["template"]["spec"]["containers"]:
                    self.assertIn("requests", container["resources"])
                    self.assertIn("limits", container["resources"])

    def test_kafka_buffer_cannot_dual_write_or_own_another_operator(self) -> None:
        self.assertIn("logging_efk_delivery != 'kafka-buffer'", TASKS)
        self.assertIn("kafka_install | default('no') | string | lower == 'yes'", TASKS)
        self.assertIn("SASL_SSL", KAFKA)
        self.assertIn("SCRAM-SHA-512", KAFKA)
        self.assertIn("kind: KafkaTopic", KAFKA)
        self.assertIn("kind: KafkaUser", KAFKA)
        self.assertEqual(KAFKA.count("apiVersion: kafka.strimzi.io/v1\n"), 2)
        self.assertNotIn("kafka.strimzi.io/v1beta2", KAFKA)
        self.assertIn(
            "operations: [Read, Write, Describe], resource: {type: topic",
            KAFKA,
        )
        for resource in ("kind: ServiceAccount", "kind: ClusterRole", "kind: ClusterRoleBinding", "kind: Service"):
            self.assertIn(resource, KAFKA)
        self.assertIn("HTTP_Server On", KAFKA)
        self.assertIn("Health_Check On", KAFKA)
        self.assertIn("storage.path /buffers/storage", KAFKA)
        self.assertIn("storage.checksum on", KAFKA)
        self.assertIn("DB /buffers/tail.db", KAFKA)
        self.assertIn("rdkafka.request.required.acks all", KAFKA)
        self.assertIn("rdkafka.retries 10", KAFKA)
        self.assertIn("storage.total_limit_size 20G", KAFKA)
        self.assertIn("Parsers_File /fluent-bit/etc/parsers.conf", KAFKA)
        self.assertIn("Name cri", KAFKA)
        self.assertIn("subPath: parsers.conf", KAFKA)
        self.assertIn("secretName: logging-kafka-cluster-ca-cert", KAFKA)
        self.assertIn("get secret {{ kafka_cluster_name }}-cluster-ca-cert -o json", TASKS)
        for variable in ("KAFKA_USERNAME", "KAFKA_PASSWORD", "ES_USERNAME", "ES_PASSWORD"):
            self.assertIn(f"name: {variable}, valueFrom: {{secretKeyRef:", KAFKA)
        self.assertNotIn("envFrom:", KAFKA)
        self.assertIn("kind: Deployment", KAFKA)
        self.assertIn("group_id => \"{{ logging_efk_kafka_group }}\"", KAFKA)
        self.assertIn("consumer_threads => 3", KAFKA)
        self.assertIn("codec => json", KAFKA)
        self.assertIn('index => "k8s-kafka-%%{+YYYY.MM.dd}"', KAFKA)
        self.assertIn('action => "create"', KAFKA)
        self.assertIn("startupProbe:", KAFKA)
        self.assertIn("hostPath: {path: /var/lib/fluent-bit, type: DirectoryOrCreate}", KAFKA)
        self.assertIn("manage_template => false", KAFKA)
        self.assertIn("ilm_enabled => false", KAFKA)
        self.assertIn("logging-efk-kafka-client", KAFKA)
        self.assertIn("Derive Kafka logging client Secret", TASKS)
        self.assertIn("name: fluent-bit-kafka", KAFKA)
        self.assertNotIn("Name es", KAFKA)
        self.assertIn(
            "        - {name: kafka-ca, secret: {secretName: logging-kafka-cluster-ca-cert}}",
            KAFKA,
        )
        self.assertNotIn("            - {name: kafka-ca, secret:", KAFKA)

    def test_loki_values_use_secret_backed_s3_and_monolithic_replication(self) -> None:
        for value in ("deploymentMode: Monolithic", "replication_factor: 3", "type: s3", "extraEnvFrom:", "-config.expand-env=true", "${LOKI_S3_ACCESS_KEY}", "${LOKI_BUCKET_CHUNKS}"):
            self.assertIn(value, LOKI)
        self.assertIn("memory: {{ logging_loki_memory_request | default('2Gi') | quote }}", LOKI)
        self.assertIn("logging_loki_tolerate_control_plane | default(false) | bool", LOKI)
        self.assertNotIn(r"\\${LOKI_S3_ACCESS_KEY}", LOKI)
        self.assertNotIn(r"\\${LOKI_S3_SECRET_KEY}", LOKI)
        self.assertIn("registry: registry.talkschool.cn:5000", LOKI)
        self.assertIn("registry: registry.talkschool.cn:5000", (ROOT / "roles/cluster-addon/templates/logging/alloy-values.yaml.j2").read_text())
        self.assertIn("logging_loki_storage_endpoint", CONFIG)
        self.assertIn("logging_loki_storage_ca_configmap", CONFIG)
        self.assertIn("ingressClassName: {{ logging_ingress_class | quote }}", LOKI)
        self.assertIn("host: {{ logging_loki_ingress_host | quote }}", LOKI)
        self.assertIn("secretName: {{ logging_ingress_tls_secret | quote }}", LOKI)
        self.assertIn("logging_ingress_tls_secret | length > 0", TASKS)
        values = render_loki_values()
        self.assertTrue(values["gateway"]["ingress"]["enabled"])
        self.assertEqual(values["gateway"]["ingress"]["ingressClassName"], "nginx")
        self.assertEqual(values["gateway"]["ingress"]["tls"][0]["secretName"], "logging-ingress-tls")

    def test_alloy_rbac_excludes_secret_read_access(self) -> None:
        values = (ROOT / "roles/cluster-addon/templates/logging/alloy-values.yaml.j2").read_text()
        self.assertIn("rbac:\n", values)
        self.assertIn('resources: ["pods", "pods/log", "namespaces"]', values)
        self.assertIn("clusterRules:\n", values)
        self.assertIn('resources: ["nodes"]', values)
        self.assertNotIn('resources: ["configmaps", "secrets"]', values)

    def test_logging_matrix_uses_authoritative_validator_schema(self) -> None:
        matrix = (ROOT / "tests/logging-test-matrix.yaml").read_text()
        self.assertIn("coverage_summary:", matrix)
        self.assertIn("tier2_matrix:", matrix)
        self.assertNotIn("\ncases:\n", matrix)

    def test_route_templates_are_rendered_exclusively(self) -> None:
        self.assertIn("logging_solution == 'efk' and logging_efk_delivery == 'direct'", TASKS)
        self.assertIn("logging_solution == 'efk' and logging_efk_delivery == 'kafka-buffer'", TASKS)
        self.assertIn('unsched="$($KC get node', TASKS)

    def test_sysctl_uses_kubernetes_internal_ip_not_node_dns(self) -> None:
        self.assertIn("status.addresses[?(@.type==\"InternalIP\")].address", TASKS)
        self.assertIn('target="$($KC get node "$node"', TASKS)
        self.assertIn('root@$target', TASKS)

    def test_writer_bootstrap_forwards_only_to_ready_elasticsearch_pod(self) -> None:
        self.assertIn('wait pod --for=condition=Ready', TASKS)
        self.assertIn('common.k8s.elastic.co/type=elasticsearch --timeout=120s', TASKS)
        self.assertIn('port-forward "pod/$ready_pod" 19200:9200', TASKS)
        self.assertNotIn('port-forward svc/logging-es-http 19200:9200', TASKS)

    def test_writer_bootstrap_preserves_eck_tls_server_name_through_forward(self) -> None:
        self.assertIn('es_tls_name="logging-es-http.{{ logging_namespace }}.svc"', TASKS)
        self.assertIn('--resolve "${es_tls_name}:19200:127.0.0.1"', TASKS)
        self.assertIn('"${es_curl[@]}" -fsS "$es_url"', TASKS)
        self.assertNotIn('curl -fsS --cacert "$tmp/ca.crt" -u "elastic:$admin" https://127.0.0.1:19200', TASKS)

    def test_kafka_client_secret_is_derived_after_kafka_user_apply(self) -> None:
        self.assertLess(
            TASKS.index("Apply EFK collector resources after writer bootstrap"),
            TASKS.index("Derive Kafka logging client Secret after Strimzi user reconciliation"),
        )

    def test_efk_monitoring_matches_the_fluent_bit_production_path(self) -> None:
        self.assertEqual(EFK.count("kind: ServiceMonitor"), 1)
        self.assertNotIn("logging-elasticsearch", EFK)
        self.assertNotIn("/_prometheus/metrics", EFK)
        self.assertNotIn("reconcile-after-bootstrap", EFK + TASKS)
        for alert in ("FluentBitTargetDown", "FluentBitOutputErrors", "FluentBitRetriesFailed"):
            self.assertIn(f"- alert: {alert}", EFK)
        self.assertIn("name: fluent-bit.rules", EFK)
        self.assertIn("team: platform", EFK)
        for delivery, service in (("direct", "fluent-bit"), ("kafka-buffer", "fluent-bit-kafka")):
            documents = render_efk(delivery)
            monitors = [document for document in documents if document["kind"] == "ServiceMonitor"]
            self.assertEqual(len(monitors), 1)
            self.assertEqual(
                monitors[0]["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"],
                service,
            )
            rule = next(document for document in documents if document["kind"] == "PrometheusRule")
            alerts = rule["spec"]["groups"][0]["rules"]
            self.assertEqual(len(alerts), 3)
            self.assertIn(f'service="{service}"', alerts[0]["expr"])

    def test_live_api_checks_wait_for_readiness_and_cover_every_prometheus(self) -> None:
        self.assertIn('kill -0 "$pf_pid"', REGRESSION)
        self.assertIn("cleanup_loki_forwarders()", REGRESSION)
        self.assertIn('kill "$gateway_pid"', REGRESSION)
        self.assertIn('kill "$grafana_pid"', REGRESSION)
        self.assertNotIn('kill "${gateway_pid:-0}"', REGRESSION)
        self.assertNotIn('kill "${grafana_pid:-0}"', REGRESSION)
        self.assertIn('"value":"vector(0) > 1"', REGRESSION)
        self.assertNotIn('"value":"vector(0)"', REGRESSION)
        self.assertIn('gateway_deployment=', REGRESSION)
        self.assertIn('get deployment -o name', REGRESSION)
        self.assertIn('rollout restart "deployment/$gateway_deployment"', REGRESSION)
        self.assertIn('kubectl -n logging port-forward svc/loki-gateway 19100:80', REGRESSION)
        self.assertIn('gateway_ready_code=000', REGRESSION)
        self.assertIn('wait "$old_gateway_pid"', REGRESSION)
        self.assertIn('LOGGING_RECOVERY_GATEWAY_READY', REGRESSION)
        self.assertIn('curl -sS -u "$auth_user:$auth_password" -o /dev/null -w \'%{http_code}\' http://127.0.0.1:19100/loki/api/v1/status/buildinfo', REGRESSION)
        self.assertIn('LOGGING_RECOVERY_ALLOY_ROLLOUT_PASS', REGRESSION)
        self.assertIn('LOGGING_RECOVERY_FIRST_QUERY', REGRESSION)
        self.assertIn('rollout status statefulset/loki --timeout=15m', REGRESSION)
        recovery = REGRESSION.split('echo LOGGING_STAGE_BEGIN loki-failure-recovery', 1)[1]
        self.assertNotIn('wait --for=condition=Ready pod -l app.kubernetes.io/component=single-binary', recovery)
        self.assertNotIn('wait --for=condition=Ready pod -l app.kubernetes.io/name=loki-gateway', REGRESSION)
        self.assertIn('https://${es_tls_name}:19200" >/dev/null', REGRESSION)
        self.assertIn('targets_ready=0', REGRESSION)
        self.assertIn('rules_ready=0', REGRESSION)
        self.assertIn('expected_prom_replicas=', REGRESSION)
        self.assertIn('expected_targets=', REGRESSION)
        self.assertIn('fluentbit_output_errors_total fluentbit_output_retries_failed_total', REGRESSION)
        self.assertIn('kubectl -n monitor get prometheus -o json', REGRESSION)
        self.assertIn('Prometheus CR not found in namespace monitor', REGRESSION)
        self.assertNotIn('kubectl -n monitor get prometheus -l app.kubernetes.io/name=prometheus', REGRESSION)
        self.assertIn('snapshot_response_file="/tmp/kubeauto-logging-${snapshot_name}-snapshot.json"', REGRESSION)
        self.assertIn('LOGGING_SNAPSHOT_RESPONSE=', REGRESSION)
        self.assertIn('.snapshot.state == "SUCCESS" and .snapshot.shards.failed == 0', REGRESSION)
        self.assertNotIn('.snapshot.failed_shards == 0', REGRESSION)
        self.assertIn('restore_response_file="/tmp/kubeauto-logging-${restore_name}-restore.json"', REGRESSION)
        self.assertIn("restore_http_code=", REGRESSION)
        self.assertIn("LOGGING_RESTORE_HTTP_CODE=", REGRESSION)
        self.assertIn('LOGGING_RESTORE_RESPONSE=', REGRESSION)
        self.assertIn('restore_body="$(jq -cn --arg replacement "$restore_replacement"', REGRESSION)
        self.assertIn('_restore?wait_for_completion=true', REGRESSION)
        self.assertIn('LOGGING_RESTORE_COUNT_RESPONSE=', REGRESSION)
        self.assertIn('LOGGING_RESTORE_INDICES=', REGRESSION)
        self.assertIn('LOGGING_RESTORE_RECOVERY=', REGRESSION)
        self.assertIn('delete_restore_http_code=', REGRESSION)
        self.assertIn('mapfile -t restore_indices', REGRESSION)
        self.assertIn('encoded_restore_index="${restore_index//%/%25}"', REGRESSION)
        self.assertNotIn('expand_wildcards=all&allow_no_indices=true', REGRESSION)
        self.assertIn('LOGGING_RESTORE_DELETE_RESPONSE=', REGRESSION)
        self.assertIn('REGISTRY_HOST_IP: "192.168.122.243"', REGRESSION)
        self.assertIn('if [[ "${LOGGING_EFK_DELIVERY:-direct}" == kafka-buffer ]]; then\n  : > /var/tmp/kubeauto-kafka-crds-owned', REGRESSION)
        self.assertNotIn('\\\\\\$1', REGRESSION)
        self.assertIn('/pods/${prom_pod}:9090/proxy/api/v1/targets', REGRESSION)
        self.assertIn('/pods/${prom_pod}:9090/proxy/api/v1/rules', REGRESSION)
        self.assertNotIn('port-forward "svc/$prom_svc"', REGRESSION)
        self.assertIn('apply --server-side --force-conflicts --field-manager=kubeauto-eck-operator', TASKS)
        self.assertIn('apply --server-side --force-conflicts --field-manager=kubeauto-logging-efk', TASKS)

    def test_loki_fixture_cleanup_removes_unlabelled_minio(self) -> None:
        self.assertIn('deployment/minio service/minio service/minio-console pod/logging-minio-mc', CLEANUP)
        expected = '--env=MINIO_ROOT_USER=test-access --env=MINIO_ROOT_PASSWORD=test-secret \\\n+    --command -- sleep 3600'.replace('+    ', '    ')
        self.assertIn(expected, REGRESSION)
        self.assertIn('kubectl taint node logging-master-243 node.kubernetes.io/unschedulable:NoSchedule-', REGRESSION)
        self.assertIn('gateway_htpasswd="admin:$(openssl passwd -apr1', REGRESSION)
        self.assertIn('--from-literal=.htpasswd="$gateway_htpasswd"', REGRESSION)
        self.assertIn('LOGGING_LOKI_GATEWAY_CODES ready=', REGRESSION)
        self.assertIn('LOGGING_LOKI_INGRESS_TLS_CODES unauth=', REGRESSION)
        self.assertIn('--cacert "$tls_tmp/tls.crt"', REGRESSION)
        self.assertIn('--resolve loki.logging.test:19443:127.0.0.1', REGRESSION)
        self.assertIn('get secret logging-loki-gateway-client -o jsonpath', REGRESSION)
        self.assertNotIn('LOGGING_LOKI_INSTALL_GATE_PASS', REGRESSION)
        self.assertIn('kubectl uncordon "$node"', REGRESSION)
        self.assertIn('hub.talkedu.cn/kubeauto+runtime-registry', REGRESSION)
        self.assertIn('logging-master-246 logging-master-217', REGRESSION)
        self.assertIn('for node in logging-master-243 logging-master-246 logging-master-217', CLEANUP)
        self.assertIn('[[ "$NS" == logging ]] && CLEAN_CLUSTER_SCOPED=1', CLEANUP)
        self.assertIn('if (( CLEAN_CLUSTER_SCOPED == 1 )); then', CLEANUP)
        self.assertIn('SMOKE_NS="logging-smoke"', CLEANUP)
        self.assertIn('delete namespace "$SMOKE_NS"', CLEANUP)
        self.assertIn('! $KC get namespace "$SMOKE_NS"', CLEANUP)

    def test_loki_full_chain_has_current_evidence_for_data_observability_and_recovery(self) -> None:
        for marker in (
            "case_pass LOGGING-33 stdout-alloy-gateway-loki-logql",
            "case_pass LOGGING-34 grafana-loki-save-test-dashboard-query",
            "case_pass LOGGING-35 loki-alloy-targets-rules-alert-recovery",
            "case_pass LOGGING-36 loki-member-gateway-alloy-recovery-positions",
            "case_pass LOGGING-37 object-storage-restart-retained-readability",
            "case_pass LOGGING-38 upgrade-preflight-rollback-boundary-retention",
            "case_pass LOGGING-39 secret-rotation-tls-rbac-no-leakage",
            "case_pass LOGGING-40 bounded-ingestion-query-backpressure-resource-limits",
            "case_pass LOGGING-41 efk-scoped-normal-failed-interrupted-cleanup",
            "case_pass LOGGING-42 loki-scoped-normal-failed-interrupted-cleanup",
            "case_pass LOGGING-43 exclusive-route-no-silent-adoption",
            "case_pass LOGGING-44 customer-route-main-and-rollback-documentation",
        ):
            self.assertIn(marker, REGRESSION)
        self.assertIn("kubectl -n logging apply -f - <<'YAML'", REGRESSION)
        self.assertIn("name: logging-minio-data", REGRESSION)
        self.assertIn("Keep the object-store fixture durable across the restart/recovery gate", REGRESSION)
        self.assertIn('"claimName":"logging-minio-data"', REGRESSION)
        self.assertIn('restart_loki_gateway_forward\n      change_id="logging-loki-change-', REGRESSION)
        self.assertIn('rollout status statefulset/loki --timeout=20m\n      restart_loki_gateway_forward', REGRESSION)
        self.assertIn("/api/datasources/uid/${grafana_ds_uid}/health", REGRESSION)
        self.assertIn("/api/v1/query_range", REGRESSION)
        self.assertIn("curl --connect-timeout 5 --max-time 20 -fsS -u", REGRESSION)
        self.assertIn('local query_end="$(date -u +%s)000000000"', REGRESSION)
        self.assertIn("vector(1)", REGRESSION)
        self.assertIn("vector(0)", REGRESSION)
        self.assertIn("rollout restart deployment/minio", REGRESSION)
        self.assertIn('grafana_node_port="$(kubectl -n monitor get svc prometheus-grafana', REGRESSION)
        self.assertIn('grafana_url="http://${grafana_node_ip}:${grafana_node_port}"', REGRESSION)
        self.assertIn('grafana_logql="{namespace=\\"logging-smoke\\",pod=\\"${loki_marker}\\"}"', REGRESSION)
        self.assertIn('--arg expr "$grafana_logql"', REGRESSION)
        self.assertIn('.status == "OK" or .status == "success"', REGRESSION)
        self.assertIn('Prometheus CR not found in namespace monitor', REGRESSION)
        self.assertIn('mapfile -t prom_pods < <(', REGRESSION)
        self.assertIn('kubectl -n logging rollout status deployment/logstash --timeout=20m', REGRESSION)
        self.assertIn('kubeauto.io/kafka-client=true', TASKS)
        self.assertIn('"cluster":["monitor"]', TASKS)
        self.assertIn('$KC cordon "$node"', CLEANUP)
        self.assertIn('kubeauto-logging-runtime', CLEANUP)
        self.assertIn('kubeauto-logging-storage-default.before', REGRESSION)
        self.assertIn('storageclass.kubernetes.io/is-default-class-', CLEANUP)
        self.assertIn('$HELM uninstall prometheus --namespace monitor', REGRESSION)

    def test_logging_performance_gate_proves_source_and_query_counts(self) -> None:
        self.assertIn('LOGGING_FOCUS_CASE="${LOGGING_FOCUS_CASE:-}"', REGRESSION)
        self.assertIn("LOGGING_PERF_SOURCE marker=$perf_marker lines=$perf_source_count unique=$perf_source_unique", REGRESSION)
        self.assertIn('printf "%s-%02d\\n"', REGRESSION)
        self.assertIn('done; sleep 300', REGRESSION)
        self.assertIn("--for=jsonpath='{.status.phase}'=Running", REGRESSION)
        self.assertIn('[[ "$perf_source_count" -eq 20 && "$perf_source_unique" -eq 20 ]]', REGRESSION)
        self.assertIn("loki_query_capture", REGRESSION)
        self.assertIn("LOGGING_PERF_QUERY attempt=${attempt}/36 http=$perf_http", REGRESSION)
        self.assertIn('.status // "missing"', REGRESSION)
        self.assertIn(".count // 0", REGRESSION)
        self.assertIn("LOGGING_PERF_DIAGNOSTIC response=$perf_response_file", REGRESSION)
        self.assertIn("kind: NetworkPolicy", REGRESSION)
        self.assertIn("LOGGING_BACKPRESSURE_BLOCKED", REGRESSION)
        self.assertGreaterEqual(REGRESSION.count('prefix:{"message.keyword":$marker}'), 2)
        self.assertNotIn('--data-urlencode "q=$perf_marker"', REGRESSION)
        self.assertIn('      start_es_forward\n      "${es_curl[@]}" -sS -XPOST', REGRESSION)
        self.assertNotIn('rollout restart daemonset/$collector_name', REGRESSION)
        self.assertNotIn('sh -c "for i in \\$(seq 1 20); do echo $perf_marker; done"', REGRESSION)

    def test_extended_cases_execute_change_rotation_conflict_and_cleanup(self) -> None:
        for marker in (
            "LOGGING_CHANGE_ROLLBACK_PASS",
            "LOGGING_SECRET_ROTATION_PASS",
            "LOGGING_ROUTE_CONFLICT_REJECTED",
            "LOGGING_CLEANUP_SCENARIO_PASS",
        ):
            self.assertIn(marker, REGRESSION)
        self.assertIn('"$HELM" rollback loki "$loki_revision_before"', REGRESSION)
        self.assertIn('logging_efk_retention_days: 31', REGRESSION)
        self.assertIn('new_auth_code', REGRESSION)
        self.assertIn('old_auth_code', REGRESSION)
        self.assertIn('new_writer_code', REGRESSION)
        self.assertIn('old_writer_code', REGRESSION)
        self.assertIn('"$K" setup "$CLUSTER" 07', REGRESSION)
        self.assertIn('for cleanup_outcome in normal failed interrupted', REGRESSION)
        self.assertIn('tests.unit.test_logging_documentation', REGRESSION)
        self.assertIn("loki_wait_for_exact_marker", REGRESSION)
        self.assertIn("json_status=$status count=$count error=$error", REGRESSION)
        self.assertIn("LOGGING_CHANGE_DATA_WAIT 36", REGRESSION)
        self.assertIn("LOGGING_SECRET_ROTATION_WAIT 36", REGRESSION)
        self.assertIn(
            '      start_es_forward\n'
            '      "${es_curl[@]}" -fsS --get --data-urlencode "q=$smoke_marker"',
            REGRESSION,
        )
        self.assertIn('term:{"message.keyword":$marker}', REGRESSION)
        self.assertIn("select(.[1] | contains($marker))", REGRESSION)
        self.assertNotIn("select(.[1] == $marker)", REGRESSION)
        self.assertNotIn('${change_response:-{}}', REGRESSION)
        self.assertNotIn('${rotation_response:-{}}', REGRESSION)

    def test_logging_runner_supports_only_the_proven_focused_case(self) -> None:
        self.assertIn('logging_focus_case="${LOGGING_FOCUS_CASE:-}"', RUNNER)
        self.assertIn('"$logging_focus_case" == extended', RUNNER)
        self.assertIn('^LOGGING-(38|39|40|41|42|43|44)$', RUNNER)
        self.assertIn("logging_success_marker=LOGGING_FOCUSED_GATE_PASS", RUNNER)
        self.assertIn("LOGGING_FOCUS_CASE=$logging_focus_case", RUNNER)
        self.assertIn('echo "LOGGING_FOCUSED_GATE_PASS case=$LOGGING_FOCUS_CASE"', REGRESSION)
        self.assertIn(
            'if [[ -n "$LOGGING_FOCUS_CASE" ]]; then\n'
            '  echo "LOGGING_FOCUSED_GATE_PASS case=$LOGGING_FOCUS_CASE"\n'
            'else\n'
            '  echo LOGGING_FULL_GATE_PASS\n'
            'fi',
            REGRESSION,
        )

    def test_logging_serviceaccount_match_does_not_trigger_pipefail_sigpipe(self) -> None:
        self.assertIn(
            "kubectl -n logging get serviceaccount -o name | grep -F '/alloy' >/dev/null",
            REGRESSION,
        )
        self.assertNotIn(
            "kubectl -n logging get serviceaccount -o name | grep -q '/alloy'",
            REGRESSION,
        )

    def test_logging_gate_reports_failure_line_without_command_or_credentials(self) -> None:
        self.assertIn("LOGGING_COMMAND_FAILURE line=", REGRESSION)
        self.assertIn("trap logging_command_failure ERR", REGRESSION)
        self.assertNotIn("BASH_COMMAND", REGRESSION)

    def test_logging_ingress_artifacts_are_staged_before_live_mutation(self) -> None:
        self.assertIn(
            "registry.k8s.io/ingress-nginx/controller:v1.13.0="
            "brinnatt/ingress-nginx-controller:v1.13.0",
            REGRESSION,
        )
        self.assertIn(
            "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.6.0="
            "brinnatt/kube-webhook-certgen:v1.6.0",
            REGRESSION,
        )
        self.assertIn('"$K" download -E ingress-nginx', REGRESSION)
        self.assertIn(
            "for image_tag in ingress-nginx-controller:v1.13.0 "
            "kube-webhook-certgen:v1.6.0",
            REGRESSION,
        )
        for media_type in (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ):
            self.assertIn(media_type, REGRESSION)
        self.assertIn('-H "Accept: $manifest_accept"', REGRESSION)
        self.assertLess(
            REGRESSION.index("for image_tag in ingress-nginx-controller:v1.13.0"),
            REGRESSION.index("echo LOGGING_ARTIFACT_UPLOAD_PASS"),
        )

    def test_ingress_controller_readiness_precedes_admission_consumers(self) -> None:
        self.assertIn("rollout status", INGRESS_TASKS)
        self.assertIn("deployment/ingress-nginx-controller", INGRESS_TASKS)
        self.assertLess(
            INGRESS_TASKS.index("deployment/ingress-nginx-controller"),
            INGRESS_TASKS.index("name: 提示 WARNNING"),
        )
        ingress_import = ADDON_TASKS.index("- import_tasks: ingress-nginx.yml")
        prometheus_import = ADDON_TASKS.index("- import_tasks: prometheus.yml")
        logging_import = ADDON_TASKS.index("- import_tasks: logging.yml")
        self.assertLess(ingress_import, prometheus_import)
        self.assertLess(ingress_import, logging_import)

    def test_eck_artifacts_match_official_digest(self) -> None:
        expected = {
            "eck-3.5.0-crds.yaml": "0e126dbd003f8f98c9b84f2af6263b5ac8a00b52cd9a6d6da225aa3af66cc13c",
            "eck-3.5.0-operator.yaml": "450f59d5026341226c54bbd3fafb68adcf99de8c674b47e32c22fae8aa183bf7",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((ROOT / "roles/cluster-addon/files" / name).read_bytes()).hexdigest(), digest)
            self.assertIn(digest, TASKS)

    def test_eck_has_three_nodes_tls_and_recovery_safe_pvc_policy(self) -> None:
        for value in ("count: 3", "requiredDuringSchedulingIgnoredDuringExecution", "volumeClaimDeletePolicy: DeleteOnScaledownOnly", "xpack.security.http.ssl.enabled: true"):
            self.assertIn(value, EFK)


if __name__ == "__main__":
    unittest.main()

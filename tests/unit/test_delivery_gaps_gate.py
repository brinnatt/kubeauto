"""Contracts for the supervised remaining-delivery-gaps full-chain gate."""

from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
RETEST = (ROOT / "tests/helpers/delivery-gap-retest.sh").read_text()
FULLCHAIN = (ROOT / "tests/helpers/delivery-gaps-fullchain.sh").read_text()
RUNNER = (ROOT / "tests/run_enterprise_regression.sh").read_text()
MK_CLUSTER_133 = (ROOT / "tests/helpers/mk-cluster-133.sh").read_text()
LAB_WIPE = (ROOT / "tests/helpers/lab-wipe-nodes.sh").read_text()


def extract_shell_function(script, name):
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


class TestDeliveryGapsGate(unittest.TestCase):
    def test_set_cfg_preserves_yaml_values_with_sed_metacharacters(self):
        jdbc = (
            "'characterEncoding=utf8&connectTimeout=1000"
            "&allowPublicKeyRetrieval=true\\|strict'"
        )
        expected = f"nacos_mysql_db_param: {jdbc}"

        for script in (RETEST, (ROOT / "tests/helpers/delivery-gap-smoke.sh").read_text()):
            with self.subTest(script=script.splitlines()[1]), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.yml"
                config.write_text('nacos_mysql_db_param: "old"\nunchanged: true\n')
                command = "\n".join(
                    (
                        extract_shell_function(script, "set_cfg"),
                        'set_cfg "$1" nacos_mysql_db_param "$2"',
                    )
                )
                subprocess.run(
                    ["bash", "-s", "--", str(config), jdbc],
                    input=command,
                    text=True,
                    check=True,
                )
                lines = config.read_text().splitlines()
                self.assertEqual(expected, lines[0])
                self.assertEqual(jdbc[1:-1], yaml.safe_load(config.read_text())["nacos_mysql_db_param"])

    def test_retest_has_strict_terminal_contract(self):
        self.assertIn('if [ "$FAIL_N" -ne 0 ] || [ "$SKIP_N" -ne 0 ]', RETEST)
        self.assertIn("DELIVERY_RETEST_FAILED", RETEST)
        self.assertIn("DELIVERY_RETEST_PASS", RETEST)
        self.assertIn('if $K docker -e </dev/null', RETEST)
        self.assertNotIn('$K docker -e </dev/null || true', RETEST)

    def test_waits_report_progress_and_require_container_readiness(self):
        self.assertIn("[WAIT] namespace=$ns pods status=not-ready", RETEST)
        self.assertIn('split($2, ready, "/")', RETEST)
        self.assertIn('ready[1] != ready[2]', RETEST)
        self.assertIn("[WAIT] cluster=$c nodes status=not-ready", RETEST)

    def test_nacos_minio_and_rocketmq_are_real_ha_gates(self):
        self.assertIn(
            "docker push 127.0.0.1:5000/brinnatt/mysql:8.0.36",
            RETEST,
        )
        self.assertNotIn(
            "docker push registry.talkschool.cn:5000/brinnatt/mysql",
            RETEST,
        )
        self.assertIn("/home/nacos/conf/mysql-schema.sql", RETEST)
        self.assertIn("nacos_schema_tables", RETEST)
        self.assertIn("SPRING_DATASOURCE_PLATFORM", RETEST)
        self.assertIn("information_schema.processlist WHERE USER='nacos'", RETEST)
        self.assertNotIn("cluster mode.*external storage", RETEST)
        self.assertIn("rocketmq_nameservice_size '2'", RETEST)
        self.assertIn("wait_rocketmq_ha 90", RETEST)
        self.assertIn("rocketmq_nameservice_running", RETEST)
        self.assertIn(".status.healthStatus", RETEST)
        self.assertIn("pool=4 health=green", RETEST)

    def test_rocketmq_gate_waits_for_the_asynchronous_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state = tmp_path / "calls"
            state.write_text("0\n")
            kubectl = tmp_path / "kubectl"
            kubectl.write_text(
                """#!/bin/bash
calls=$(cat "$MOCK_STATE")
calls=$((calls + 1))
printf '%s\\n' "$calls" > "$MOCK_STATE"
cat <<'EOF'
rocketmq-operator-abc 1/1 Running 0 10s
name-service-0 1/1 Running 0 10s
name-service-1 1/1 Running 0 10s
EOF
if [ "$calls" -eq 2 ]; then
  echo 'broker-0-master-0 0/1 Pending 0 1s'
elif [ "$calls" -ge 3 ]; then
  echo 'broker-0-master-0 1/1 Running 0 2s'
fi
"""
            )
            kubectl.chmod(0o755)
            command = "\n".join(
                (
                    extract_shell_function(RETEST, "wait_rocketmq_ha"),
                    "sleep() { :; }",
                    "wait_rocketmq_ha 3",
                )
            )
            result = subprocess.run(
                ["bash", "-s"],
                input=command,
                text=True,
                capture_output=True,
                check=True,
                env={"PATH": f"{tmp_path}:/usr/bin:/bin", "MOCK_STATE": str(state)},
            )
            self.assertEqual("3", state.read_text().strip())
            self.assertIn("broker_ready=0/1", result.stdout)
            self.assertIn("broker_ready=1/1", result.stdout)
            self.assertIn("attempt=3/3", result.stdout)

    def test_storage_diagnostics_query_each_resource_by_its_own_name(self):
        smoke = (ROOT / "tests/helpers/delivery-gap-smoke.sh").read_text()
        for script in (RETEST, smoke):
            with self.subTest(script=script.splitlines()[1]):
                self.assertNotIn("kubectl get pvc,pod", script)
                self.assertIn("kubectl get pvc lvm-smoke-pvc -o wide", script)
                self.assertIn("kubectl get pod lvm-smoke-pod -o wide", script)
                self.assertIn("kubectl get pvc nfs-smoke-pvc -o wide", script)
                self.assertIn("kubectl get pod nfs-smoke-pod -o wide", script)

    def test_nacos_fixture_waits_for_mysql_and_application_readiness(self):
        self.assertIn("mysqladmin ping -h 127.0.0.1", RETEST)
        self.assertIn("readinessProbe:", RETEST)
        self.assertIn("wait_nacos_external_mysql", RETEST)
        self.assertIn("mysql -h127.0.0.1", RETEST)
        self.assertIn("mysql_connections=", RETEST)
        self.assertIn("attempt=$attempt/$tries", RETEST)

    def test_focused_gate_is_fail_fast(self):
        fail_calls = [
            line.strip()
            for line in RETEST.splitlines()
            if 'fail "' in line
        ]
        self.assertEqual(['fail "$*"'], fail_calls)
        self.assertIn("abort_after_namespace_cleanup", RETEST)
        self.assertIn(
            'abort_after_namespace_cleanup "P0 Nacos replicas/external-MySQL" nacos',
            RETEST,
        )
        self.assertIn(
            'abort_after_namespace_cleanup "P0 RocketMQ HA pods" rocketmq',
            RETEST,
        )

    def test_small_host_and_ha_capacity_preconditions(self):
        self.assertIn('KUBE_RESERVED_ENABLED SYS_RESERVED_ENABLED', MK_CLUSTER_133)
        self.assertIn('${key}: \\"no\\"', MK_CLUSTER_133)
        self.assertIn('ensure_test_ha_worker 132 worker-132', RETEST)
        self.assertIn('ensure_test_ha_worker 133 worker-133', RETEST)
        self.assertIn('schedulable_ready_count', RETEST)
        self.assertIn('Schedulable Ready nodes=$schedulable_n required=3', RETEST)
        self.assertNotIn('grep -c Ready', RETEST)

    def test_network_dingtalk_and_rocky_gates_execute_workloads(self):
        self.assertIn("P1 Rocky G8b on large-memory test137", RETEST)
        self.assertLess(
            RETEST.index("P1 Rocky G8b on large-memory test137"),
            RETEST.index("P0 LVM+NFS on deliver-lvm (133)"),
        )
        self.assertIn("rollout status deploy/webhook-dingtalk", RETEST)
        self.assertIn("brinnatt/prometheus-webhook-dingtalk:v2.1.0", RETEST)
        self.assertIn('"${#network_cronjobs[@]}" -ne 9', RETEST)
        self.assertIn("create job --from=cronjob/$cronjob", RETEST)
        self.assertIn("set_ha_addons_disabled", RETEST)
        self.assertIn("wait_namespace_deleted", RETEST)
        self.assertIn("recover_ha_control_plane", RETEST)
        self.assertLess(
            RETEST.index('wait_namespace_deleted openebs 120'),
            RETEST.index('for cronjob in "${network_cronjobs[@]}"'),
        )
        self.assertIn('describe pod -l job-name="$job"', RETEST)
        self.assertIn("network-check 9/9 jobs Complete", RETEST)

    def test_heavy_ha_addons_are_serialized_and_cleanup_is_a_gate(self):
        nacos = RETEST.index('section "P0 Nacos HA with external MySQL"')
        minio = RETEST.index('section "P1 MinIO official four-server Tenant"')
        rocketmq = RETEST.index('section "P0 RocketMQ HA"')
        prometheus = RETEST.index('section "P1 Prometheus, Ingress, Dashboard and DingTalk"')
        network = RETEST.index('section "P1 network-check workloads"')
        self.assertLess(nacos, minio)
        self.assertLess(minio, rocketmq)
        self.assertLess(rocketmq, prometheus)
        self.assertLess(prometheus, network)
        self.assertIn('wait_namespace_deleted nacos 120 || abort', RETEST[nacos:minio])
        self.assertIn('wait_namespace_deleted minio 120 || abort', RETEST[minio:rocketmq])
        self.assertIn('wait_namespace_deleted rocketmq 120 || abort', RETEST[rocketmq:prometheus])
        self.assertIn('wait_namespace_deleted monitor 120 || abort', RETEST[prometheus:network])

    def test_single_node_rocketmq_uses_large_memory_aio_and_storage_checks_are_scoped(self):
        self.assertIn('section "P0 RocketMQ single-node on large-memory aio (138)"', RETEST)
        self.assertIn('set_cfg "$CFG" rocketmq_storage_class \'"local-path"\'', RETEST)
        self.assertIn('wait_pods_ns rocketmq 72 && assert_no_imagepull rocketmq', RETEST)
        self.assertIn('wait_namespace_deleted rocketmq 120 || abort', RETEST)
        self.assertIn('section "P0 LVM+NFS on deliver-lvm (133)"', RETEST)
        self.assertIn('wait_pods_ns openebs 48 && assert_no_imagepull openebs', RETEST)
        single_node = RETEST.split(
            'section "P0 RocketMQ single-node on large-memory aio (138)"', 1
        )[1].split('section "P0 LVM+NFS on deliver-lvm (133)"', 1)[0]
        self.assertNotIn('rocketmq_storage_class \'"openebs-hostpath"\'', single_node)

    def test_required_delivery_images_and_failed_pods_are_diagnostic_gates(self):
        self.assertIn('require_registry_tag brinnatt/rocketmq-console 2.0.0', RETEST)
        self.assertIn('abort "local registry missing brinnatt/rocketmq-console:2.0.0"', RETEST)
        self.assertIn('kubectl get events -n "$ns" --sort-by=.lastTimestamp', RETEST)
        self.assertIn('kubectl describe pod -n "$ns" "$pod"', RETEST)

    def test_transient_setup_retry_is_bounded_and_diagnostic(self):
        self.assertIn("setup_with_transient_retry", RETEST)
        self.assertIn("etcdserver: request timed out", RETEST)
        self.assertIn('attempts="${3:-3}"', RETEST)

    def test_nacos_gate_sizes_official_jvm_and_captures_restart_evidence(self):
        template = (ROOT / "roles/cluster-addon/templates/nacos/nacos-sts.yaml.j2").read_text()
        config = (ROOT / "conf/config.yml").read_text()
        for variable in ("JVM_XMS", "JVM_XMX", "JVM_XMN"):
            self.assertIn(f"name: {variable}", template)
        self.assertIn('nacos_jvm_xms: "1g"', config)
        self.assertIn('nacos_jvm_xmx: "1g"', config)
        self.assertIn('nacos_jvm_xmn: "512m"', config)
        self.assertIn("set_cfg \"$CFG\" nacos_jvm_xms '\"512m\"'", RETEST)
        self.assertIn("set_cfg \"$CFG\" nacos_jvm_xmx '\"512m\"'", RETEST)
        self.assertIn("set_cfg \"$CFG\" nacos_jvm_xmn '\"256m\"'", RETEST)
        self.assertIn("LAST_REASON", RETEST)
        self.assertIn("lastState.terminated.exitCode", RETEST)
        self.assertIn("/home/nacos/logs/start.out", RETEST)

    def test_nacos_mysql8_uses_the_official_database_parameter_hook(self):
        template = (ROOT / "roles/cluster-addon/templates/nacos/nacos-sts.yaml.j2").read_text()
        config = (ROOT / "conf/config.yml").read_text()
        self.assertIn("name: MYSQL_SERVICE_DB_PARAM", template)
        self.assertIn("nacos_mysql_db_param", template)
        self.assertIn("nacos_mysql_db_param:", config)
        self.assertIn("allowPublicKeyRetrieval=true", RETEST)

    def test_fullchain_builds_topology_before_gaps(self):
        self.assertLess(
            FULLCHAIN.index("regression-full.sh"),
            FULLCHAIN.index("delivery-gap-retest.sh"),
        )
        self.assertIn("GAPS_BASELINE_REGRESSION_PASS", FULLCHAIN)
        self.assertIn("DELIVERY_GAPS_FULLCHAIN_PASS", FULLCHAIN)
        self.assertIn("timeout --signal=TERM --kill-after=30s", FULLCHAIN)

    def test_runner_supervises_and_always_cleans(self):
        self.assertIn('--gaps-only', RUNNER)
        self.assertIn('--cancel-gaps', RUNNER)
        self.assertIn(
            "cancel_remote_job gaps-138 ssh138 sudo /tmp/kubeauto-gaps-gate",
            RUNNER,
        )
        self.assertIn('/tmp/kubeauto-gaps-gate.exit', RUNNER)
        self.assertIn('DELIVERY_GAPS_FULLCHAIN_PASS', RUNNER)
        branch = RUNNER.split('if [[ "$MODE" == "--gaps-only" ]]', 1)[1]
        branch = branch.split("\nfi", 1)[0]
        self.assertGreaterEqual(branch.count("lab-wipe-nodes.sh"), 4)
        self.assertIn("monitor_remote_job", branch)
        self.assertIn("--diagnose-nacos-last", RUNNER)
        self.assertIn("--diagnose-nacos-images", RUNNER)
        self.assertIn("--rocketmq-image-integrity", RUNNER)
        self.assertIn("registry_blob_integrity.py", RUNNER)

    def test_snap_registry_storage_is_cleaned_from_the_container_view(self):
        self.assertIn(
            "docker exec local_registry sh -c 'rm -rf /var/lib/registry/docker'",
            LAB_WIPE,
        )
        self.assertIn("/var/snap/docker/common/kubeauto-registry", LAB_WIPE)
        self.assertIn("/data/registry", LAB_WIPE)

    def test_lab_wipe_falls_back_to_the_jumper(self):
        self.assertIn("JUMPER=192.168.47.130", LAB_WIPE)
        self.assertIn('rocky_ssh "$ip"', LAB_WIPE)
        self.assertIn('ssh -J "root@$JUMPER"', LAB_WIPE)
        self.assertLess(
            LAB_WIPE.index('ssh "${ssh_opts[@]}" "root@$ip"'),
            LAB_WIPE.index('ssh -J "root@$JUMPER"'),
        )


if __name__ == "__main__":
    unittest.main()

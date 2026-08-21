import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PXC = ROOT / "docs" / "middleware" / "perconaPXC"
FILES = {
    name: (PXC / name).read_text()
    for name in (
        "README.md",
        "technical-whitepaper.md",
        "operations-manual.md",
        "development-manual.md",
        "official-sources.md",
    )
}


def operations_bash_blocks():
    blocks = {}
    for block in re.findall(
        r"```bash\n(.*?)\n```", FILES["operations-manual.md"], re.DOTALL
    ):
        match = re.match(r"bash <<'([A-Z0-9_]+)'\n", block)
        if match:
            blocks[match.group(1)] = block
    return blocks


class PerconaPxcDocumentationTests(unittest.TestCase):
    def test_enterprise_document_set_is_complete(self):
        for name in (
            "README.md",
            "technical-whitepaper.md",
            "operations-manual.md",
            "development-manual.md",
            "official-sources.md",
        ):
            self.assertTrue((PXC / name).is_file(), name)

    def test_locked_ga_versions_and_boundaries_are_explicit(self):
        all_text = "\n".join(FILES.values())
        for phrase in (
            "1.20.0",
            "8.4.8-8.1",
            "8.4.0-5.1",
            "2.8.18-1",
            "5.0.6-1",
            "已实现",
            "独立 MySQL middleware 分路",
        ):
            self.assertIn(phrase, all_text)

    def test_whitepaper_covers_required_architecture_and_data_protection(self):
        text = FILES["technical-whitepaper.md"]
        for phrase in (
            "Galera/PXC 复制原理",
            "多数派和脑裂",
            "IST、SST 和 gcache",
            "HAProxy 连接和故障转移",
            "备份、恢复和 PITR",
            "wsrep_cluster_status",
            "三份在线副本提供节点级可用性，不提供误删保护",
        ):
            self.assertIn(phrase, text)
        self.assertGreaterEqual(text.count("```mermaid"), 10)

    def test_operations_covers_full_production_lifecycle(self):
        text = FILES["operations-manual.md"]
        for phrase in (
            "部署目标和容量规划",
            "安装前置检查",
            "应用连接和 SQL 验收",
            "一次只维护一个 PXC 节点",
            "密码轮换",
            "备份、恢复和 PITR",
            "PITR",
            "升级、回滚和下线",
            "故障排查",
            "交付验收表",
            "kubectl create namespace mysql-operator",
        ):
            self.assertIn(phrase, text)
        self.assertGreaterEqual(text.count("```mermaid"), 5)

    def test_operations_workdir_blocks_are_self_contained(self):
        text = FILES["operations-manual.md"]
        bash_blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
        for index, block in enumerate(bash_blocks, start=1):
            if "$PXC_WORKDIR" not in block:
                continue
            self.assertRegex(
                block,
                r"(?m)^PXC_WORKDIR=",
                f"bash block {index} uses PXC_WORKDIR without defining it",
            )

    def test_examples_are_versioned_and_do_not_ship_sample_passwords(self):
        all_text = "\n".join(FILES.values())
        for forbidden in (
            "root_password",
            "backup_password",
            "admin_password",
            "operatoradmin",
            "/main/deploy/cr.yaml",
        ):
            self.assertNotIn(forbidden, all_text)
        for document in FILES.values():
            self.assertIn("v1.3（生产运维深化版）", document)

    def test_operations_has_executable_mainline_and_auxiliary_contracts(self):
        text = FILES["operations-manual.md"]
        for phrase in (
            "统一执行契约",
            "客户首次部署主线路",
            "生产运行线路",
            "日常运维主线",
            "容量变更辅助线",
            "性能诊断辅助线",
            "故障应急辅助线",
            "PXC_DAILY_ACCEPTANCE_PASS",
            "PXC_STORAGE_CAPACITY_AUDIT_COLLECTION_PASS",
            "PXC_PERFORMANCE_ACCEPTANCE_PASS",
            "[1/6]",
            "原子发布",
        ):
            self.assertIn(phrase, text)
        self.assertIn("> **异常处理：日常巡检失败。**", text)
        self.assertIn("> **扩容失败和回滚边界：**", text)

    def test_operations_covers_storage_full_and_volume_expansion_boundaries(self):
        text = FILES["operations-manual.md"]
        for phrase in (
            "磁盘即将耗尽或已经写满",
            "AllowVolumeExpansion",
            "spec.storageScaling.enableVolumeScaling",
            "pvc-resize-in-progress",
            ".status.storageAutoscaling",
            "currentSize",
            "lastResizeTime",
            "resizeCount",
            "Kubernetes PVC 只能扩大，不能原地缩小",
            "请求值、实际容量和文件系统容量一致",
            "当前分路未配置、未回归，不得宣称已交付",
        ):
            self.assertIn(phrase, text)

    def test_performance_guidance_is_measurable_and_cleans_up(self):
        text = FILES["operations-manual.md"]
        for phrase in (
            "performance-slo.tsv",
            "prepare、预热、1/4/16 线程阶梯",
            "P95/P99 独立采样",
            "error_percent",
            "wsrep-before.tsv",
            "wsrep-after.tsv",
            "cleanup-on-error.log",
            "PXC_PERFORMANCE_DIAG_COLLECTION_PASS",
            "wsrep_local_bf_aborts",
        ):
            self.assertIn(phrase, text)

    def test_every_operations_script_has_steps_and_one_terminal_marker(self):
        expected = {
            "PXC_WORKDIR",
            "PXC_DEPLOY",
            "PXC_PREFLIGHT",
            "PXC_NAMESPACES",
            "PXC_STORAGE",
            "PXC_STORAGE_CLEAN",
            "PXC_ARTIFACT",
            "PXC_CHART_VERIFY",
            "PXC_OPERATOR_INSTALL",
            "PXC_SECRET_CHECK",
            "PXC_APPLY",
            "PXC_READY",
            "PXC_CLIENT_CONNECT",
            "PXC_WSREP",
            "PXC_DAILY",
            "PXC_ONE_NODE_MAINTENANCE",
            "PXC_SCALE",
            "PXC_STORAGE_AUDIT",
            "PXC_VOLUME_EXPANSION",
            "PXC_ROTATE_PASSWORD",
            "PXC_SYSBENCH",
            "PXC_PERFORMANCE_DIAG",
            "PXC_BACKUP",
            "PXC_RESTORE",
            "PXC_PITR_PREFLIGHT",
            "PXC_UPGRADE_PREFLIGHT",
            "PXC_OPERATOR_UPGRADE",
            "PXC_DATABASE_UPGRADE",
            "PXC_DECOMMISSION_PREFLIGHT",
            "PXC_DIAG",
            "PXC_OPERATOR_DIAG",
            "PXC_SCHEDULING_DIAG",
            "PXC_QUORUM_DIAG",
            "PXC_HAPROXY_DIAG",
        }
        blocks = operations_bash_blocks()
        self.assertEqual(set(blocks), expected)

        terminal_markers = {}
        for name, block in blocks.items():
            self.assertIn("set -Eeuo pipefail", block, name)
            self.assertRegex(block, r"printf ['\"]\[[1-9][0-9]*/[1-9][0-9]*\]", name)
            markers = [
                marker
                for marker in re.findall(r"printf [^\n]*?(PXC_[A-Z0-9_]+)", block)
                if marker.endswith(("_PASS", "_READY", "_SUBMITTED", "_SUCCEEDED"))
            ]
            self.assertEqual(len(markers), 1, f"{name}: {markers}")
            marker = markers[0]
            self.assertNotIn(marker, terminal_markers, marker)
            terminal_markers[marker] = name
            tail = block[block.rfind(marker) :].splitlines()[1:]
            self.assertFalse(
                any(re.search(r"\b(?:printf|echo)\b", line) for line in tail),
                f"{name}: terminal marker is not the final log",
            )

    def test_operations_script_index_documents_every_script_contract(self):
        text = FILES["operations-manual.md"]
        self.assertIn("脚本功能、影响和幂等索引", text)
        for name in operations_bash_blocks():
            self.assertIn(f"| `{name}` |", text, name)
        for phrase in (
            "功能与稳定输入",
            "资源影响和产物",
            "终端标志与重跑语义",
            "同名异参立即拒绝",
            "不接管旧 Pod",
            "不覆盖旧证据",
        ):
            self.assertIn(phrase, text)

    def test_stateful_scripts_have_provable_idempotency_guards(self):
        text = FILES["operations-manual.md"]
        blocks = operations_bash_blocks()
        for phrase in (
            "app.kubernetes.io/managed-by: kubeauto-pxc-preflight",
            "kubeauto.talkedu.cn/purpose: storage-preflight",
            "拒绝接管客户对象",
            'TARGET_NODE="<变更单批准的固定 Kubernetes 节点名>"',
            'CHANGE_ID="<审批变更 ID，例如 CHG-20260821-002>"',
            'data.get("mysql_pxc_size", "")',
            'data.get("mysql_pvc_size", "")',
            'ROTATION_INPUT="<密码系统按 ROTATION_ID 导出的临时文件绝对路径>"',
            'CURRENT_VALUE" != "$EXPECTED_VALUE',
            "cmp <(jq -S '.spec'",
            "同名对象存在时必须 spec 完全相同",
            "test ! -e \"$OUT\"",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn('TARGET_NODE="$(kubectl', blocks["PXC_ONE_NODE_MAINTENANCE"])
        self.assertNotIn(
            'kubectl apply -f "$PXC_WORKDIR/cluster1-backup.yaml"',
            blocks["PXC_BACKUP"],
        )
        self.assertNotIn(
            'kubectl apply -f "$PXC_WORKDIR/cluster1-restore.yaml"',
            blocks["PXC_RESTORE"],
        )
        self.assertGreaterEqual(text.count("mktemp -d"), 7)
        self.assertNotIn('TMP="$PXC_WORKDIR/evidence/.', text)

    def test_long_waits_emit_heartbeat_and_temporary_resources_are_reclaimed(self):
        blocks = operations_bash_blocks()
        for name in (
            "PXC_STORAGE",
            "PXC_STORAGE_CLEAN",
            "PXC_READY",
            "PXC_ONE_NODE_MAINTENANCE",
            "PXC_SCALE",
            "PXC_VOLUME_EXPANSION",
            "PXC_SYSBENCH",
            "PXC_BACKUP",
            "PXC_RESTORE",
        ):
            self.assertIn("elapsed_seconds", blocks[name], name)
        self.assertIn("cleanup_on_exit", blocks["PXC_SYSBENCH"])
        self.assertIn("delete_benchmark_pod", blocks["PXC_SYSBENCH"])
        self.assertIn("--ignore-not-found --wait=false", blocks["PXC_STORAGE_CLEAN"])

    def test_diagnostic_markers_do_not_claim_health(self):
        blocks = operations_bash_blocks()
        for name in (
            "PXC_STORAGE_AUDIT",
            "PXC_PERFORMANCE_DIAG",
            "PXC_DIAG",
            "PXC_OPERATOR_DIAG",
            "PXC_SCHEDULING_DIAG",
            "PXC_QUORUM_DIAG",
            "PXC_HAPROXY_DIAG",
        ):
            self.assertIn("COLLECTION_PASS", blocks[name], name)
            self.assertIn("health_verified=false", blocks[name], name)

    def test_unverified_volume_expansion_is_not_misrepresented_as_delivered(self):
        all_text = "\n".join(FILES.values())
        for phrase in (
            "当前 kubeauto 矩阵尚未包含在线扩容场景",
            "在线扩容、部分扩容失败和不支持扩容卷的逐卷重建仍需独立门禁",
            "建议 `MYSQL-15`",
        ):
            self.assertIn(phrase, all_text)

    def test_development_keeps_mysql_gate_independent(self):
        text = FILES["development-manual.md"]
        for phrase in (
            "不把 full enterprise test 作为 PXC 日常交付门禁",
            "component_images mysql",
            "tests/mysql-test-matrix.yaml",
            "六仓 CI、镜像 tag、digest 和 fallback",
            "独立测试分路",
            "Pod Running",
            "roles/cluster-addon/templates/percona-pxc",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("templates/perconaPXC", text)

    def test_local_links_resolve(self):
        pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        for document in PXC.glob("*.md"):
            for target in pattern.findall(document.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                path = target.split("#", 1)[0]
                self.assertTrue(
                    (document.parent / path).resolve().exists(),
                    f"broken link in {document.relative_to(ROOT)}: {target}",
                )


if __name__ == "__main__":
    unittest.main()

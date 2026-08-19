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
            "尚未编码",
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
            self.assertIn("v1.0（编码前技术基线）", document)

    def test_development_keeps_mysql_gate_independent(self):
        text = FILES["development-manual.md"]
        for phrase in (
            "不把 full enterprise test 作为 PXC 日常交付门禁",
            "component_images mysql",
            "tests/mysql-test-matrix.yaml",
            "六仓 CI、镜像 tag、digest 和 fallback",
            "独立测试分路",
            "Pod Running",
        ):
            self.assertIn(phrase, text)

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

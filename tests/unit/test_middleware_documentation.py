import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = (ROOT / "docs" / "middleware" / "delivery-playbook.md").read_text(
    encoding="utf-8"
)
INDEX = (ROOT / "docs" / "middleware" / "README.md").read_text(encoding="utf-8")


class MiddlewareDocumentationTests(unittest.TestCase):
    def test_playbook_has_product_neutral_title_and_mainline(self):
        self.assertTrue(PLAYBOOK.startswith("# Kubeauto 中间件企业交付规范\n"))
        headings = (
            "## 第一章、适用范围与统一原则",
            "## 第二章、中间件能力模型",
            "## 第三章、全生命周期交付阶段",
            "## 第四章、官方基线与技术选型",
            "## 第五章、六仓供应链与辅助制品",
            "## 第六章、实现、幂等与资源所有权",
            "## 第七章、专项测试与生产验收",
            "## 第八章、运维可观测性与证据体系",
            "## 第九章、企业文档规范",
            "## 第十章、组件接入与动态扩展",
            "## 第十一章、产品差异示例",
            "## 第十二章、统一验收定义",
        )
        positions = [PLAYBOOK.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))

    def test_component_examples_are_after_the_generic_mainline(self):
        examples = PLAYBOOK.index("## 第十一章、产品差异示例")
        acceptance = PLAYBOOK.index("## 第十二章、统一验收定义")
        for component in ("Percona PXC", "Apache Kafka"):
            first_reference = PLAYBOOK.index(component)
            self.assertGreater(first_reference, examples)
            self.assertLess(first_reference, acceptance)

    def test_playbook_preserves_equal_component_extension_contract(self):
        for phrase in (
            "所有组件使用同级入口、相同状态词和同一文档集合",
            "不改变通用主线的产品中立性",
            "MongoDB",
            "Redis",
            "Elasticsearch",
            "COMPONENT_CLEAN_VERIFY_PASS",
            "专项现场矩阵达到 100% PASS",
        ):
            self.assertIn(phrase, PLAYBOOK)

    def test_customer_document_excludes_internal_narrative(self):
        for phrase in (
            "Percona PXC 复盘",
            "PXC 已交付成果",
            "PXC 过程复盘",
            "复盘基线",
            "可复用提示词",
            "本次评审",
            "方案获批后",
            "后续中间件必须",
        ):
            self.assertNotIn(phrase, PLAYBOOK)

    def test_index_uses_the_enterprise_standard_title(self):
        self.assertIn(
            "[Kubeauto 中间件企业交付规范](delivery-playbook.md)", INDEX
        )
        self.assertNotIn("Percona PXC 复盘", INDEX)

    def test_index_gives_each_component_the_same_document_set(self):
        for component_path in ("perconaPXC", "kafka"):
            for document in (
                "operations-manual.md",
                "technical-whitepaper.md",
                "development-manual.md",
            ):
                self.assertIn(f"{component_path}/{document}", INDEX)
        self.assertIn("| Percona PXC | 已交付 |", INDEX)
        self.assertIn("| Apache Kafka on Kubernetes | 已交付 |", INDEX)

    def test_index_links_resolve_and_playbook_fences_are_balanced(self):
        link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        index_path = ROOT / "docs" / "middleware" / "README.md"
        for target in link_pattern.findall(INDEX):
            resolved = (index_path.parent / target.split("#", 1)[0]).resolve()
            self.assertTrue(resolved.is_file(), target)
        self.assertEqual(PLAYBOOK.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()

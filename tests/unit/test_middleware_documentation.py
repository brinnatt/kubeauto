import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIDDLEWARE_ROOT = ROOT / "docs" / "middleware"
PLAYBOOK = (ROOT / "docs" / "middleware" / "delivery-playbook.md").read_text(
    encoding="utf-8"
)
INDEX = (MIDDLEWARE_ROOT / "README.md").read_text(encoding="utf-8")
ROOT_README = (ROOT / "README.md").read_text(encoding="utf-8")
OPERATIONS = (ROOT / "docs" / "operations-manual.md").read_text(encoding="utf-8")
WHITEPAPER = (ROOT / "docs" / "technical-whitepaper.md").read_text(encoding="utf-8")
DEVELOPMENT = (ROOT / "docs" / "development-manual.md").read_text(encoding="utf-8")
STACK_INDEX = (ROOT / "docs" / "technology-stack-index.md").read_text(encoding="utf-8")
RUNNER = (ROOT / "tests" / "run_enterprise_regression.sh").read_text(encoding="utf-8")
STANDARD_MANUALS = (
    "operations-manual.md",
    "technical-whitepaper.md",
    "development-manual.md",
)


def component_directories():
    return sorted(path.name for path in MIDDLEWARE_ROOT.iterdir() if path.is_dir())


def linked_components(text, prefix=""):
    pattern = re.compile(
        rf"\]\({re.escape(prefix)}([^/)]+)/operations-manual\.md\)"
    )
    return sorted(set(pattern.findall(text)))


def indexed_statuses():
    statuses = {}
    for line in INDEX.splitlines():
        match = re.match(r"\|\s*[^|]+\s*\|\s*([^|]+)\s*\|\s*(.*)\|", line)
        if not match:
            continue
        links = re.findall(r"\(([^/)]+)/operations-manual\.md\)", match.group(2))
        for component in links:
            statuses[component] = match.group(1).strip()
    return statuses


def indexed_names():
    names = {}
    for line in INDEX.splitlines():
        match = re.match(r"\|\s*([^|]+?)\s*\|\s*[^|]+\s*\|\s*(.*)\|", line)
        if not match:
            continue
        for component in re.findall(r"\(([^/)]+)/operations-manual\.md\)", match.group(2)):
            names[component] = match.group(1).strip()
    return names


def indexed_matrices():
    matrices = {}
    for line in INDEX.splitlines():
        links = re.findall(r"\]\(\.\./\.\./tests/([^)/]+\.yaml)\)", line)
        components = re.findall(r"\(([^/)]+)/operations-manual\.md\)", line)
        for component in components:
            matrix_name = links[0] if links else None
            if matrix_name:
                matrices[component] = matrix_name
    return matrices


def root_statuses():
    statuses = {}
    for line in ROOT_README.splitlines():
        match = re.match(r"\|\s*\*\*[^|]+\*\*\s*\|\s*(.*?)\s*\|\s*([^|]+)\s*\|", line)
        if not match:
            continue
        links = re.findall(r"\./docs/middleware/([^/)]+)/operations-manual\.md", match.group(1))
        for component in links:
            statuses[component] = match.group(2).strip()
    return statuses


def root_names():
    names = {}
    for line in ROOT_README.splitlines():
        match = re.match(r"\|\s*\*\*([^|]+?)\*\*\s*\|\s*(.*?)\s*\|", line)
        if not match:
            continue
        for component in re.findall(
            r"\./docs/middleware/([^/)]+)/operations-manual\.md", match.group(2)
        ):
            names[component] = match.group(1).strip()
    return names


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
        components = component_directories()
        self.assertEqual(components, linked_components(INDEX))
        self.assertEqual(components, linked_components(ROOT_README, "./docs/middleware/"))
        for component_path in components:
            for document in STANDARD_MANUALS:
                self.assertTrue((MIDDLEWARE_ROOT / component_path / document).is_file())
                self.assertIn(f"{component_path}/{document}", INDEX)
                self.assertIn(
                    f"./docs/middleware/{component_path}/{document}", ROOT_README
                )
                self.assertEqual(INDEX.count(f"({component_path}/{document})"), 1)
                self.assertEqual(
                    ROOT_README.count(f"(./docs/middleware/{component_path}/{document})"),
                    1,
                )
        self.assertEqual(set(indexed_statuses()), set(components))
        self.assertEqual(set(root_statuses()), set(components))

    def test_index_and_root_use_the_same_controlled_status(self):
        self.assertEqual(indexed_names(), root_names())
        self.assertEqual(indexed_statuses(), root_statuses())
        allowed = {"已设计", "已实现", "已验证", "已交付"}
        self.assertTrue(set(indexed_statuses().values()) <= allowed)

    def test_delivered_status_is_bound_to_a_current_passing_matrix(self):
        components = component_directories()
        matrices = indexed_matrices()
        self.assertEqual(set(matrices), set(components))
        for component, matrix_name in matrices.items():
            matrix_path = ROOT / "tests" / matrix_name
            self.assertTrue(matrix_path.is_file(), matrix_name)
            matrix = matrix_path.read_text(encoding="utf-8")
            result_is_pass = bool(
                re.search(r"(?m)^\s*result:\s*PASS\s*$", matrix)
                or re.search(r"(?m)^\s*regression_result:\s*[\"']PASS\b", matrix)
            )
            if indexed_statuses()[component] == "已交付":
                self.assertTrue(result_is_pass, f"{component} matrix is not PASS")

    def test_customer_navigation_has_one_complete_middleware_entry(self):
        for document in (ROOT_README, OPERATIONS, WHITEPAPER, DEVELOPMENT, STACK_INDEX):
            self.assertIn("middleware/README.md", document)
        for component_path in component_directories():
            for document in STANDARD_MANUALS[:2]:
                self.assertIn(f"middleware/{component_path}/{document}", STACK_INDEX)

    def test_shared_navigation_gate_precedes_all_delivery_modes(self):
        gate = RUNNER.index("tests.unit.test_middleware_documentation")
        for mode in ("--mysql-only", "--kafka-only", "--prometheus-only", "--logging-only"):
            self.assertLess(gate, RUNNER.index(f'if [[ "$MODE" == "{mode}" ]]'), mode)

    def test_index_links_resolve_and_playbook_fences_are_balanced(self):
        link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        index_path = ROOT / "docs" / "middleware" / "README.md"
        for target in link_pattern.findall(INDEX):
            resolved = (index_path.parent / target.split("#", 1)[0]).resolve()
            self.assertTrue(resolved.is_file(), target)
        self.assertEqual(PLAYBOOK.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()

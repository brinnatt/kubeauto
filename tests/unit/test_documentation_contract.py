import re
import string
import unicodedata
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
WHITEPAPER = (ROOT / "docs/technical-whitepaper.md").read_text()
OPERATIONS = (ROOT / "docs/operations-manual.md").read_text()
DEVELOPMENT = (ROOT / "docs/development-manual.md").read_text()
STACK_INDEX = (ROOT / "docs/technology-stack-index.md").read_text()
OPEN_EBS = (ROOT / "docs/whitepaper/16-storage-openebs.md").read_text()
DATA_ADDONS = (ROOT / "docs/whitepaper/17-storage-middleware-addons.md").read_text()
OPEN_EBS_ROLE_README = (
    ROOT / "roles/cluster-addon/templates/openebs/readme.md"
).read_text()
CUSTOMER_MARKDOWN = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))


def prose_lines(document):
    """Yield Markdown lines with fenced and inline code removed."""
    fence_marker = None
    fence_length = 0
    for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker = fence.group(1)
            if fence_marker is None:
                fence_marker = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_marker
                and len(marker) >= fence_length
                and not fence.group(2).strip()
            ):
                fence_marker = None
                fence_length = 0
            continue
        if fence_marker is None:
            yield line_number, re.sub(r"(`+).*?\1", "CODE", line)
    if fence_marker is not None:
        raise AssertionError(f"unclosed Markdown fence in {document.relative_to(ROOT)}")


def is_markdown_punctuation(character):
    return character in string.punctuation or unicodedata.category(character).startswith("P")


class DocumentationContractTests(unittest.TestCase):
    def test_customer_markdown_uses_github_supported_math_delimiters(self):
        r"""GitHub documents dollar delimiters and math fences, not \(...\) or \[...\]."""
        unsupported = re.compile(r"\\[()[\]]")
        for document in CUSTOMER_MARKDOWN:
            for line_number, line in prose_lines(document):
                self.assertIsNone(
                    unsupported.search(line),
                    f"unsupported GitHub math delimiter in "
                    f"{document.relative_to(ROOT)}:{line_number}",
                )

    def test_customer_markdown_strong_emphasis_delimiters_can_open_and_close(self):
        """Enforce GFM left- and right-flanking rules for strong emphasis."""
        strong = re.compile(r"\*\*([^*\n]+?)\*\*")
        for document in CUSTOMER_MARKDOWN:
            for line_number, line in prose_lines(document):
                for match in strong.finditer(line):
                    content = match.group(1)
                    preceding = line[match.start() - 1] if match.start() else "\n"
                    following = line[match.end()] if match.end() < len(line) else "\n"
                    can_open = not content[0].isspace() and (
                        not is_markdown_punctuation(content[0])
                        or preceding.isspace()
                        or is_markdown_punctuation(preceding)
                    )
                    can_close = not content[-1].isspace() and (
                        not is_markdown_punctuation(content[-1])
                        or following.isspace()
                        or is_markdown_punctuation(following)
                    )
                    self.assertTrue(
                        can_open and can_close,
                        f"strong emphasis delimiter violates GFM rules in "
                        f"{document.relative_to(ROOT)}:{line_number}: {match.group(0)}",
                    )

    def test_openebs_customer_entry_points_are_linked(self):
        for text in (README, WHITEPAPER, OPERATIONS, DEVELOPMENT, STACK_INDEX):
            self.assertIn("16-storage-openebs.md", text)
        self.assertIn("technology-stack-index.md", README)
        self.assertIn("technology-stack-index.md", WHITEPAPER)

    def test_openebs_whitepaper_covers_required_delivery_questions(self):
        required = (
            "Local PV Hostpath",
            "Local PV LVM",
            "WaitForFirstConsumer",
            "openebs_lvm_enabled",
            "allowedTopologies",
            "thin provisioning",
            "所有节点都没有 VG",
            "同时启用会冲突吗",
            "不自带跨节点数据副本",
            "真实 PVC `Bound`、Pod 挂载、写入、读取",
        )
        for phrase in required:
            self.assertIn(phrase, OPEN_EBS)

    def test_openebs_versions_and_parameter_precedence_are_explicit(self):
        for phrase in ("4.3.2", "4.3.0", "1.7.0", "volgroup", "vgpattern"):
            self.assertIn(phrase, OPEN_EBS)
            self.assertIn(phrase, DEVELOPMENT)
        self.assertIn("`volgroup` 优先", OPEN_EBS)

    def test_operations_has_data_path_and_failure_sop(self):
        required = (
            "OpenEBS 生产运维",
            "Hostpath PVC 读写验收",
            "LVM PVC 读写验收",
            "只有部分节点提供 LVM",
            "PVC Pending 故障树",
            "thin pool 容量与扩容",
            "备份、删除与卸载",
            "现场签收表",
        )
        for phrase in required:
            self.assertIn(phrase, OPERATIONS)

    def test_other_storage_and_middleware_boundaries_are_documented(self):
        required = (
            "不支持 volume capacity limit",
            "不提供 NFS 服务",
            "requiredDuringSchedulingIgnoredDuringExecution",
            "外部 MySQL",
            "rocketmq_replica_per_group",
            "异步 CR 协调",
            "生产带唯一 ID 的消息",
        )
        for phrase in required:
            self.assertIn(phrase, DATA_ADDONS)
        self.assertIn("17-storage-middleware-addons.md", README)
        self.assertIn("17-storage-middleware-addons.md", STACK_INDEX)

    def test_thin_pool_size_is_not_documented_as_fixed_default(self):
        misleading = re.compile(r"默认\s*vg_k8s_thinpool\s*只有\s*10G", re.IGNORECASE)
        self.assertIsNone(misleading.search(OPEN_EBS))
        self.assertIsNone(misleading.search(OPERATIONS))
        self.assertIsNone(misleading.search(OPEN_EBS_ROLE_README))
        self.assertIn("不是固定默认 10Gi", OPEN_EBS_ROLE_README)

    def test_customer_document_local_links_resolve(self):
        docs = (
            ROOT / "README.md",
            ROOT / "docs/technical-whitepaper.md",
            ROOT / "docs/operations-manual.md",
            ROOT / "docs/development-manual.md",
            ROOT / "docs/technology-stack-index.md",
            ROOT / "docs/whitepaper/12-addons-observability.md",
            ROOT / "docs/whitepaper/16-storage-openebs.md",
            ROOT / "docs/whitepaper/17-storage-middleware-addons.md",
            ROOT / "docs/whitepaper/A-version-matrix.md",
        )
        link_pattern = re.compile(r"\[[^]]+\]\(([^)]+)\)")
        for document in docs:
            for target in link_pattern.findall(document.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                path = target.split("#", 1)[0]
                resolved = (document.parent / path).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"broken local link in {document.relative_to(ROOT)}: {target}",
                )


if __name__ == "__main__":
    unittest.main()

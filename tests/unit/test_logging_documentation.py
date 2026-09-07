"""Customer-documentation contracts for the EFK and Loki delivery branch."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC_ROOT = ROOT / "docs/middleware/efk"
OPERATIONS = (DOC_ROOT / "operations-manual.md").read_text()
WHITEPAPER = (DOC_ROOT / "technical-whitepaper.md").read_text()
DEVELOPMENT = (DOC_ROOT / "development-manual.md").read_text()
ALL_DOCS = "\n".join((OPERATIONS, WHITEPAPER, DEVELOPMENT))


class LoggingDocumentationContracts(unittest.TestCase):
    def test_customer_document_set_has_exactly_three_manuals(self) -> None:
        manuals = sorted(path.name for path in DOC_ROOT.glob("*.md"))
        self.assertEqual(
            manuals,
            ["development-manual.md", "operations-manual.md", "technical-whitepaper.md"],
        )

    def test_operations_manual_covers_each_exclusive_customer_route(self) -> None:
        for setting in (
            'logging_install: "no"',
            'logging_solution: "efk"',
            'logging_efk_delivery: "direct"',
            'logging_efk_delivery: "kafka-buffer"',
            'logging_solution: "loki"',
        ):
            self.assertIn(setting, OPERATIONS)
        for route in (
            "CRI -> Fluent Bit -> Elasticsearch -> Kibana",
            "CRI -> Fluent Bit -> Kafka -> Logstash -> Elasticsearch -> Kibana",
            "CRI -> Alloy -> Gateway -> Loki -> Grafana",
        ):
            self.assertIn(route, OPERATIONS)
        self.assertIn("kubecli setup <cluster> 07", OPERATIONS)
        self.assertIn("existing kubeauto logging solution", OPERATIONS)

    def test_operations_manual_has_full_lifecycle_and_machine_gates(self) -> None:
        for heading in (
            "容量规划",
            "前置门禁",
            "安全",
            "业务验收",
            "监控与告警",
            "备份与恢复",
            "凭据轮换",
            "升级与回滚",
            "故障处理",
            "下线与清理",
        ):
            self.assertIn(heading, OPERATIONS)
        for marker in (
            "LOGGING_FULL_GATE_PASS",
            "LOGGING_GATE_EXIT rc=0",
            "LOGGING_CLEAN_VERIFY_PASS",
            "44/44",
        ):
            self.assertIn(marker, OPERATIONS)
        self.assertRegex(OPERATIONS, r"> \*\*回滚：[^*]+\*\*")
        self.assertRegex(OPERATIONS, r"> \*\*异常处理：[^*]+\*\*")

    def test_whitepaper_explains_product_mechanisms_and_boundaries(self) -> None:
        for concept in (
            "ECK",
            "ILM",
            "TSDB",
            "replication_factor",
            "RPO",
            "RTO",
            "NetworkPolicy",
            "安全拒绝",
            "对象存储",
            "消费者组",
        ):
            self.assertIn(concept, WHITEPAPER)
        self.assertGreaterEqual(WHITEPAPER.count("```mermaid"), 3)
        self.assertIn("Elastic License 2.0", WHITEPAPER)
        self.assertIn("GNU AGPLv3", WHITEPAPER)

    def test_development_manual_maps_configuration_to_owners(self) -> None:
        for path in (
            "conf/config.yml",
            "roles/cluster-addon/tasks/logging.yml",
            "roles/cluster-addon/templates/logging/efk.yaml.j2",
            "roles/cluster-addon/templates/logging/fluent-bit-direct.yaml.j2",
            "roles/cluster-addon/templates/logging/fluent-bit-kafka.yaml.j2",
            "roles/cluster-addon/templates/logging/loki-values.yaml.j2",
            "roles/cluster-addon/templates/logging/alloy-values.yaml.j2",
            "tests/logging-test-matrix.yaml",
        ):
            self.assertIn(path, DEVELOPMENT)
        for topic in ("field manager", "ownership", "Secret", "durable", "focused", "六仓"):
            self.assertIn(topic, DEVELOPMENT)

    def test_versions_and_official_references_are_consistent(self) -> None:
        for version in ("3.5.0", "9.5.1", "5.1.1", "3.7.6", "18.9.0", "1.18.1", "1.11.1"):
            self.assertIn(version, WHITEPAPER)
            self.assertIn(version, DEVELOPMENT)
        for domain in (
            "elastic.co/guide/en/cloud-on-k8s/3.5",
            "elastic.co/guide/en/elasticsearch/reference/current",
            "docs.fluentbit.io",
            "grafana.com/docs/loki/latest",
            "grafana.com/docs/alloy/latest",
        ):
            self.assertIn(domain, ALL_DOCS)
        self.assertIn("最后核验日期：2026-09-06", WHITEPAPER)

    def test_docs_do_not_persist_test_credentials_or_dynamic_proxies(self) -> None:
        for forbidden in (
            "test-writer-password-change-me",
            "test-gateway-password",
            "gh-proxy.com",
            "status.anye.xyz",
            "insecureSkipVerify: true",
        ):
            self.assertNotIn(forbidden, ALL_DOCS)
        self.assertIsNone(re.search(r"(?i)(password|token):\s*['\"]?[A-Za-z0-9]{12,}", ALL_DOCS))

    def test_markdown_fences_are_balanced(self) -> None:
        for document in (OPERATIONS, WHITEPAPER, DEVELOPMENT):
            self.assertEqual(document.count("```") % 2, 0)


if __name__ == "__main__":
    unittest.main()

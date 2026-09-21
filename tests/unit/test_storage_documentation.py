import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = ROOT / "docs" / "storage"
CEPH_ROOT = STORAGE_ROOT / "ceph"
INDEX = (STORAGE_ROOT / "README.md").read_text(encoding="utf-8")

CEPH_DOCUMENTS = (
    "01-architecture.md",
    "02-cephadm.md",
    "03-rados.md",
    "04-cephfs.md",
    "05-rbd.md",
    "06-radosgw.md",
    "07-mgr.md",
    "08-mgr-dashboard.md",
    "09-monitoring.md",
)

MINIMUM_LINES = {
    "01-architecture.md": 1500,
    "02-cephadm.md": 400,
    "03-rados.md": 500,
    "04-cephfs.md": 350,
    "05-rbd.md": 350,
    "06-radosgw.md": 450,
    "07-mgr.md": 250,
    "08-mgr-dashboard.md": 300,
    "09-monitoring.md": 180,
}

ARCHITECTURE_REQUIRED_FACTS = {
    "cluster model": (
        "RADOS",
        "Object ID",
        "BlueStore",
        "Monitor Map",
        "OSD Map",
        "PG Map",
        "CRUSH Map",
        "MDS Map",
    ),
    "monitor consensus": (
        "Paxos",
        "epoch",
        "Leader",
        "Provider",
        "Requester",
        "NTP",
        "RocksDB",
    ),
    "cephx protocol": (
        "principal secret",
        "auth session key",
        "service session key",
        "rotating service secret",
        "global_id",
        "capabilities",
        "Messenger v2 secure mode",
    ),
    "osd membership": (
        "osd_heartbeat_grace",
        "mon_osd_min_down_reporters",
        "mon_osd_reporter_subtree_level",
        "mon_osd_report_timeout",
        "MOSDBeacon",
        "up + in",
        "down + in",
    ),
    "placement and peering": (
        "liverpool",
        "Up Set",
        "Acting Set",
        "authoritative history",
        "last_complete",
        "backfill_toofull",
        "mark_unfound_lost",
    ),
    "data integrity": (
        "light scrub",
        "deep scrub",
        "K+M",
        "shard_t",
        "systematic code",
        "divergent",
        "K+1",
    ),
    "protocol and layout": (
        "Object Class",
        "src/objclass/objclass.h",
        "Watch/Notify",
        "stripe_count=1",
        "Object set 1",
        "dual-ack semantics",
    ),
    "client services": (
        "FastCGI",
        "kernel rbd",
        "librbd",
        "libcephfs",
        "directory fragment",
        "metadata pool",
    ),
    "production constraints": (
        "target_max_bytes",
        "cache-flush-evict-all",
        "6800:7568",
        "osd_memory_target",
        "backfillfull",
        "O_DIRECT",
        "fsync()",
    ),
    "crush execution": (
        "root=default host=HOSTNAME",
        "shadow hierarchy",
        "take -> choose/chooseleaf -> emit",
        "weight-set",
        "straw2",
        "CRUSH MSR",
        "CRUSH_MSR",
        "primary-affinity",
        "--show-mappings",
    ),
    "pg lifecycle": (
        "pg_num",
        "pgp_num",
        "hashpspool",
        "pg_autoscale_mode",
        "split PG",
        "PG replicas/OSD",
    ),
    "cephx operations": (
        "client.admin-backup",
        "profile role-definer",
        "profile simple-rados-client",
        "object_prefix",
        "auth_service_cipher",
        "mon_auth_emergency_allowed_ciphers",
    ),
    "failure diagnosis": (
        "mon_osd_down_out_interval=600",
        "osd_max_backfills",
        "osd_backfill_retry_interval",
        "ceph pg dump_stuck",
        "homeless PG",
        "osd_scrub_auto_repair_num_errors=5",
    ),
    "monitor guardrails": (
        "mon_allow_pool_delete",
        "nodelete",
        "nopgchange",
        "nosizechange",
        "mon_data_size_warn",
    ),
    "network contracts": (
        "public_network",
        "cluster_network",
        "public_addr",
        "cluster_addr",
        "ms_bind_ipv4",
        "ms_bind_ipv6",
        "ms_tcp_nodelay",
    ),
    "hardware acceptance": (
        "ARM container",
        "4-5 个 HDD OSD",
        "15 个 HDD OSD",
        "partition alignment",
        "100 GB SSD",
        "BMC",
        "DWPD/TBW",
    ),
}

MERMAID_BLOCK = re.compile(r"(?ms)^```mermaid\s*$\n(.*?)^```\s*$")
MERMAID_TYPES = {"flowchart", "sequenceDiagram", "stateDiagram-v2"}

REQUIRED_DOMAINS = {
    "01-architecture.md": (
        "Cluster Map",
        "CephX",
        "CRUSH",
        "peering",
        "acting set",
        "Erasure",
        "Watch/Notify",
        "stripe unit",
    ),
    "02-cephadm.md": (
        "bootstrap",
        "Service Spec",
        "_no_schedule",
        "tuned-profile",
        "certmgr",
        "UPGRADE_FAILED_PULL",
        "SNMP",
        "Adoption",
    ),
    "03-rados.md": (
        "Messenger v2",
        "BlueStore",
        "BLUEFS_SPILLOVER",
        "PG autoscaler",
        "CRUSH tunables",
        "mClock",
        "OBJECT_UNFOUND",
        "Object Class",
        "SQLite VFS",
    ),
    "04-cephfs.md": (
        "Dokan",
        "dirfrag",
        "charmap",
        "LazyIO",
        "quiesce",
        "blocklist epoch barrier",
        "cephfs-journal-tool",
        "libcephfs",
    ),
    "05-rbd.md": (
        "Persistent Write Log",
        "immutable-object-cache",
        "LUKS",
        "split-brain",
        "Live migration",
        "rbd-wnbd",
        "iSCSI",
        "NVMe-oF",
        "Librbd API",
    ),
    "06-radosgw.md": (
        "Admin Ops API",
        "Swift API",
        "STS Lite",
        "Dynamic reshard",
        "Sync policy",
        "Vault/KMIP/Barbican",
        "D3N",
        "Orphan",
        "S3 Select",
    ),
    "07-mgr.md": (
        "completion",
        "get_store/set_store",
        "Prometheus",
        "Telemetry",
        "NFS",
        "SMB",
        "Rook",
        "REST API",
    ),
    "08-mgr-dashboard.md": (
        "per-MGR",
        "SAML2",
        "OAuth2",
        "Grafana frontend URL",
        "Alertmanager",
        "Silence",
        "Admin Ops API",
        "API auditing",
    ),
    "09-monitoring.md": (
        "ceph_daemon_socket_up",
        "ceph_disk_occupation_human",
        "ceph_pool_metadata",
        "ceph_rgw_metadata",
        "ceph_mds_metadata",
        "rbd_stats_pools",
        "rate(latency_sum",
        "Alertmanager silence",
    ),
}


class StorageDocumentationTests(unittest.TestCase):
    def test_ceph_is_an_independent_nine_document_storage_branch(self):
        self.assertEqual(
            sorted(path.name for path in CEPH_ROOT.glob("*.md")),
            sorted(CEPH_DOCUMENTS),
        )
        self.assertIn("完全独立", INDEX)
        self.assertNotRegex(INDEX, r"\]\([^)]*middleware/")

    def test_index_links_each_ceph_module_once(self):
        for document in CEPH_DOCUMENTS:
            self.assertEqual(INDEX.count(f"./ceph/{document}"), 1, document)

    def test_each_module_is_substantive_and_diagram_driven(self):
        for document in CEPH_DOCUMENTS:
            text = (CEPH_ROOT / document).read_text(encoding="utf-8")
            self.assertGreaterEqual(
                len(text.splitlines()), MINIMUM_LINES[document], document
            )
            self.assertGreaterEqual(len(re.findall(r"(?m)^## ", text)), 9, document)
            self.assertGreaterEqual(text.count("```mermaid"), 2, document)
            self.assertEqual(text.count("```") % 2, 0, document)
            self.assertIn("Tentacle", text, document)
            self.assertIn("76fba24cef67d9219f97eeaa68cd1a848da3f2b2", text, document)
            self.assertIn("CC BY-SA 3.0", text, document)

    def test_each_module_keeps_its_required_mechanism_domains(self):
        for document, domains in REQUIRED_DOMAINS.items():
            text = (CEPH_ROOT / document).read_text(encoding="utf-8")
            for domain in domains:
                self.assertIn(domain.lower(), text.lower(), f"{document}: {domain}")

    def test_architecture_preserves_official_mechanism_facts(self):
        text = (CEPH_ROOT / "01-architecture.md").read_text(encoding="utf-8")
        for mechanism, facts in ARCHITECTURE_REQUIRED_FACTS.items():
            for fact in facts:
                self.assertIn(fact.lower(), text.lower(), f"{mechanism}: {fact}")

        self.assertGreaterEqual(text.count("```mermaid"), 40)
        self.assertGreaterEqual(len(re.findall(r"(?m)^## ", text)), 25)

    def test_architecture_mermaid_uses_github_renderable_contract(self):
        text = (CEPH_ROOT / "01-architecture.md").read_text(encoding="utf-8")
        blocks = MERMAID_BLOCK.findall(text)
        self.assertEqual(len(blocks), text.count("```mermaid"))

        for index, block in enumerate(blocks, start=1):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            diagram_type = lines[0].split()[0]
            self.assertIn(diagram_type, MERMAID_TYPES, f"diagram {index}")
            self.assertNotRegex(block, r"%%\{|<script|\bicon:|@\{")

            if diagram_type == "flowchart":
                subgraphs = sum(line.startswith("subgraph ") for line in lines)
                ends = sum(line == "end" for line in lines)
                self.assertEqual(subgraphs, ends, f"flowchart {index}")

            if diagram_type == "sequenceDiagram":
                participants = []
                for line in lines[1:]:
                    match = re.fullmatch(
                        r"participant\s+([A-Za-z_][A-Za-z0-9_]*)\s+as\s+.+",
                        line,
                    )
                    if match:
                        participants.append(match.group(1))
                self.assertEqual(
                    len(participants), len(set(participants)), f"sequence {index}"
                )

                messages = [
                    re.fullmatch(
                        r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:->>|-->>)"
                        r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*.+",
                        line,
                    )
                    for line in lines[1:]
                    if "->>" in line or "-->>" in line
                ]
                self.assertTrue(all(messages), f"sequence {index}")
                for message in messages:
                    self.assertIn(message.group(1), participants, f"sequence {index}")
                    self.assertIn(message.group(2), participants, f"sequence {index}")

            if diagram_type == "stateDiagram-v2":
                self.assertIn("[*]", block, f"state diagram {index}")
                self.assertRegex(block, r"(?m)^\s*\S+\s+-->\s+\S+")

    def test_content_is_not_replaced_by_coverage_claims(self):
        combined = "\n".join(
            (CEPH_ROOT / document).read_text(encoding="utf-8")
            for document in CEPH_DOCUMENTS
        )
        for phrase in (
            "官方覆盖清单",
            "本篇覆盖",
            "页面清单",
            "详见官方",
            "参见官方",
            "请参考官方",
        ):
            self.assertNotIn(phrase, combined)

    def test_ceph_module_bodies_do_not_delegate_to_cross_references(self):
        for document in CEPH_DOCUMENTS:
            text = (CEPH_ROOT / document).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\[[^]]+\]\([^)]+\)", document)

    def test_local_index_links_resolve(self):
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", INDEX):
            if target.startswith(("https://", "http://")):
                continue
            path = target.split("#", 1)[0]
            self.assertTrue((STORAGE_ROOT / path).resolve().is_file(), target)


if __name__ == "__main__":
    unittest.main()

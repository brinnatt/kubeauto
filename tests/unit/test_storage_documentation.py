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
    "02-cephadm.md": 2000,
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

CEPHADM_REQUIRED_FACTS = {
    "bootstrap contract": (
        "--cluster-network",
        "--output-dir",
        "--registry-json",
        "--apply-spec",
        "--single-host-defaults",
        "osd_crush_chooseleaf_type = 0",
        "osd_pool_default_size = 2",
        "mgr_standby_modules = false",
        "--ssh-signed-cert",
        "2.2.1 明确不可用",
    ),
    "host lifecycle": (
        "--keep-conf-keyring",
        "--zap-osd-devices",
        "--rm-crush-entry",
        "--yes-i-really-mean-it",
        "_no_conf_keyring",
        "_no_autotune_memory",
        "--with-summary",
        "/etc/sysctl.d/<profile>-cephadm-tuned-profile.conf",
    ),
    "service specification": (
        "CEPHADM_INVALID_CONFIG_OPTION",
        "CEPHADM_FAILED_SET_OPTION",
        "count_per_host",
        "regex:",
        "extra_container_args",
        "extra_entrypoint_args",
        "split: false",
        "custom_configs",
        "daemon_cache_timeout",
    ),
    "osd lifecycle": (
        "device_enhanced_scan",
        "lsmcli ldl",
        "all-available-devices",
        "filter_logic: OR",
        "db_slots",
        "wal_slots",
        "tpm2: true",
        "autotune_memory_target_ratio",
        "osd_memory_target 16G",
        "--replace",
        "destroyed",
        "ceph orch device replace",
        "Is being replaced",
        "osd activate",
    ),
    "core services": (
        "public_network",
        "crush_locations",
        "max_mds",
        "disable_multisite_sync_traffic",
        "rgw_exit_timeout_secs",
        "virtual_ips_list",
        "first_virtual_router_id",
        "keepalive_only",
        "enable_haproxy_protocol",
    ),
    "gateway services": (
        "trusted_ip_list",
        "cluster_meta_uri",
        "cluster_lock_uri",
        "cephfs-proxy",
        "TCP 445",
        "mgmt-gateway",
        "oauth2-proxy",
        "CEPH-MIB.txt",
        "200 秒",
    ),
    "monitoring": (
        "secure_monitoring_stack",
        "admin/admin",
        "service_discovery_port",
        "retention_time",
        "retention_size",
        "anonymous_access",
        "custom_alerts.yml",
        "ceph orch rm prometheus --force",
    ),
    "certificate management": (
        "CEPHADM_CERT_ERROR",
        "certificate_automated_rotation_enabled",
        "certificate_duration_days",
        "certificate_renewal_threshold_days",
        "certificate_check_period",
        "cert-key set",
        "generate-certificates",
        "config-check ls",
    ),
    "upgrade controls": (
        "ceph osd pool set noautoscale",
        "mgr -> mon -> crash -> osd -> mds -> rgw",
        "mgr/orchestrator/fail_fs=true",
        "UPGRADE_NO_STANDBY_MGR",
        "UPGRADE_FAILED_PULL",
        "daemon_types",
        "ceph orch upgrade stop",
        "ceph orch update service",
    ),
    "recovery and adoption": (
        "cephadm:v1",
        "ceph config assimilate-conf",
        "cephadm adopt --style legacy",
        "仅支持 BlueStore OSD",
        "MON config-key",
        "/var/lib/ceph/<fsid>/removed",
        "--no-ceph-conf",
        "/var/lib/systemd/coredump",
        "cephadm rm-cluster --force --zap-osds --fsid",
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

    def test_cephadm_preserves_official_production_facts(self):
        text = (CEPH_ROOT / "02-cephadm.md").read_text(encoding="utf-8")
        for mechanism, facts in CEPHADM_REQUIRED_FACTS.items():
            for fact in facts:
                self.assertIn(fact.lower(), text.lower(), f"{mechanism}: {fact}")

        self.assertGreaterEqual(text.count("```mermaid"), 40)
        self.assertGreaterEqual(len(re.findall(r"(?m)^## ", text)), 35)

    def test_mature_documents_use_github_renderable_mermaid_contract(self):
        for document in ("01-architecture.md", "02-cephadm.md"):
            text = (CEPH_ROOT / document).read_text(encoding="utf-8")
            blocks = MERMAID_BLOCK.findall(text)
            self.assertEqual(len(blocks), text.count("```mermaid"), document)

            for index, block in enumerate(blocks, start=1):
                diagram = f"{document} diagram {index}"
                lines = [line.strip() for line in block.splitlines() if line.strip()]
                diagram_type = lines[0].split()[0]
                self.assertIn(diagram_type, MERMAID_TYPES, diagram)
                self.assertNotRegex(block, r"%%\{|<script|\bicon:|@\{", diagram)

                if diagram_type == "flowchart":
                    subgraphs = sum(line.startswith("subgraph ") for line in lines)
                    ends = sum(line == "end" for line in lines)
                    self.assertEqual(subgraphs, ends, diagram)

                if diagram_type == "sequenceDiagram":
                    participants = []
                    for line in lines[1:]:
                        match = re.fullmatch(
                            r"participant\s+([A-Za-z_][A-Za-z0-9_]*)\s+as\s+.+",
                            line,
                        )
                        if match:
                            participants.append(match.group(1))
                    self.assertEqual(len(participants), len(set(participants)), diagram)

                    messages = [
                        re.fullmatch(
                            r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:->>|-->>)"
                            r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*.+",
                            line,
                        )
                        for line in lines[1:]
                        if "->>" in line or "-->>" in line
                    ]
                    self.assertTrue(all(messages), diagram)
                    for message in messages:
                        self.assertIn(message.group(1), participants, diagram)
                        self.assertIn(message.group(2), participants, diagram)

                if diagram_type == "stateDiagram-v2":
                    self.assertIn("[*]", block, diagram)
                    self.assertRegex(block, r"(?m)^\s*\S+\s+-->\s+\S+", diagram)

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

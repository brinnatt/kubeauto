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
    "customer sign-off decisions": (
        "L × R < U × (C - F)",
        "MON 失去多数派",
        "新客户端能认证并完成 I/O",
        "重新注入旧 CRUSH 规则不会撤销",
        "既有对象的 striping、EC profile 不能靠原地改参数重写",
        "不可用 PG 增加",
    ),
}

CEPHADM_REQUIRED_FACTS = {
    "bootstrap contract": (
        "--cluster-network",
        "--output-dir",
        "--registry-json",
        "--apply-spec",
        "--single-host-defaults",
        "orchestrator interface",
        "Ansible、Rook 或 Salt",
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
        "--host-status offline",
        "ssh_identity_cert",
        "StrictHostKeyChecking no",
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
        "ceph orch set-unmanaged <service>",
        "ceph orch set-managed <service>",
        "ceph orch daemon rm <daemon-name>",
        "daemon_cache_timeout 60",
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
        "block_db_size",
        "block_wal_size",
        "osds_per_device",
        "data_allocate_fraction",
        "osd_id_claims",
        "method: lvm",
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
        "rgw_frontend_ssl_certificate",
        "rgw_realm_token",
        "only_bind_port_on_networks",
        "data_pool_attributes",
        "enable_nlm",
        "idmap_conf",
        "keepalived_password",
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
        "remote_control_ssl_cert",
        "cephadm list-networks",
        "ssl_session_tickets",
        "enable_health_check_endpoint",
        "redirect_url",
        "allowlist_domains",
        "16、24 或 32 bytes",
        "init_containers",
        "互斥",
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
        "Prometheus | 9095",
        "node-exporter | 9100",
        "services/mgmt-gateway/nginx.conf",
        "container_image_jaeger_query",
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
        "cephadm_root_ca_cert",
        "cephadm_root_ca_key",
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
        "--ceph-version <version>",
        "container_image_base",
        "cephadm` 包更新",
        "container_image <target-image>",
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
        "/unit.run",
        "ceph --admin-daemon",
        "ceph-monstore-tool",
        "ceph-objectstore-tool",
        "Failed to infer CIDR network",
        "ceph orch set backend ''",
        "--config-json config-json.json",
        "cephadm logs --fsid",
        "cephadm_private_key",
        "--extract-monmap /tmp/monmap",
        "monmaptool /tmp/monmap --rm",
        "--inject-monmap /tmp/monmap",
        "ceph orch resume",
    ),
    "implementation and scalability": (
        "service_name",
        "O(1)",
        "serve()",
        "最多并行抓取 10 台主机",
        "compliance enable|disable|status",
        "proposal",
        "vstart --cephadm",
        "cstart.sh",
        "--shared_ceph_folder",
        "cephadm box",
        "Python Zip Application",
        "version --verbose",
        "Docker Live Restore",
        "多文档 YAML",
        "on|off|build|string",
        "NG_CLI_ANALYTICS=false npm ci",
        "--extended --osds 5 --hosts 5",
        "每个 loop OSD 消耗 5 GiB",
    ),
    "registry and recovery operations": (
        "ceph cephadm registry-login <registry> <username> <password>",
        "cephadm registry-login --registry-json <file> --fsid <fsid>",
        "立即补建此前缺失",
        "Service Spec 不是 Ceph 备份",
        "运行中直接复制 RocksDB 目录不构成一致备份",
        "恢复声明",
        "恢复业务状态",
    ),
}

RADOS_REQUIRED_FACTS = {
    "configuration contract": (
        "--no-mon-config",
        "config diff",
        "$CEPH_CONF",
        "addrvec",
        "runtime override",
        "osd/class:ssd",
        "osd/host:storage-03",
        "带 `dev` level",
        "hostname -s",
        "tmp_dir",
        "tmp_file_template",
        "fatal_signal_handlers",
        "自定义 cluster name 已 deprecated",
    ),
    "network and cephx": (
        "ms_cluster_mode",
        "ms_service_mode",
        "ms_client_mode",
        "messenger dump client --tcp-info",
        "_ceph-mon._tcp",
        "rotating service secret",
        "object_prefix",
        "mon_auth_emergency_allowed_ciphers",
    ),
    "bluestore engineering": (
        "1%-4%",
        "RGW 大量使用 omap，至少按 4%",
        "RBD 通常 1%-2%",
        "3/30/300 GiB",
        "TCMalloc",
        "crc32c_16",
        "1/65,536",
        "none/passive/aggressive/force",
        "ceph-bluestore-tool ... reshard",
        "Pacific 起 HDD/SSD 默认均为 4 KiB",
        "bluestore_block_db_path",
        "Sapphire Rapids",
    ),
    "pool and pg": (
        "mon_allow_pool_delete",
        "target_size_bytes",
        "target_size_ratio",
        "pg_num_min",
        "pg_num_max",
        "100-250 PG replicas/shards per OSD",
        "might_have_unfound",
        "mark_unfound_lost revert",
        "osd_scrub_auto_repair_num_errors",
        "wait/laggy",
    ),
    "crush and erasure": (
        "take default class hdd",
        "chooseleaf firstn",
        "CRUSH_MSR",
        "crushtool -i crush.before.bin --compare",
        "compat weight-set",
        "pg-upmap-primary",
        "k+1 <= d <= k+m-1",
        "d*S/(d-k+1)",
        "allow_ec_overwrites",
        "Jerasure",
        "ISA",
        "LRC",
        "SHEC",
        "CLAY",
    ),
    "mclock exactness": (
        "50% / 1 / MAX",
        "5% / 2 / 90%",
        "60% / 2 / MAX",
        "5% / 4 / 70%",
        "70% / 2 / MAX",
        "5% / 2 / MAX",
        "osd_mclock_override_recovery_settings",
        "HDD 500 IOPS",
        "SSD 80,000 IOPS",
        "1 shard x 5 threads",
        "injectargs",
    ),
    "recovery procedures": (
        "ceph-mon -i <survivor-id> --extract-monmap",
        "ceph-mon -i <survivor-id> --inject-monmap",
        "ceph-monstore-tool /secure/mon-store rebuild",
        "不能恢复其他 client/MDS keyrings",
        "ceph osd safe-to-destroy osd.<id>",
        "ceph osd destroy <id>",
        "ceph osd purge <id>",
        "ceph osd crush reweight osd.<id> 0",
        "osd lost",
        "ceph osd add-noout",
        "ceph osd rm-noout",
        "ceph osd set-group noout",
        "ceph osd unset-group noout",
    ),
    "device health operations": (
        "ceph device monitoring on",
        "mgr/devicehealth/scrape_frequency",
        "scrape-daemon-health-metrics",
        "get-health-metrics",
        "device_failure_prediction_mode local",
        "predict-life-expectancy",
        "set-life-expectancy",
        "mgr/devicehealth/warn_threshold",
        "mgr/devicehealth/mark_out_threshold",
        "mgr/devicehealth/self_heal",
    ),
    "developer interfaces": (
        "-ENOENT",
        "compare/assert version",
        "Object Class SDK",
        "ceph-clsinfo",
        "PRAGMA journal_mode=PERSIST",
        "PRAGMA locking_mode=EXCLUSIVE",
        "150-250 TPS",
        "不支持 concurrent readers",
        "SQLite Backup API",
        "librados::Rados::init2",
        "librados::IoCtx",
        "librados::AioCompletion",
        "C++ API/ABI 不保证",
        "libradospp-devel",
    ),
    "rados toolchain": (
        "ceph-volume-systemd",
        "ceph-authtool",
        "ceph-debugpack",
        "ceph-dencoder",
        "ceph-kvstore-tool",
        "ceph-run",
        "ceph-syn",
        "crushdiff",
        "librados-config",
        "monmaptool",
        "osdmaptool",
        "ceph-post-file",
    ),
    "cephx cipher migration": (
        "auth_allowed_ciphers aes,aes256k",
        "auth_preferred_cipher aes256k",
        "auth rotate --key-type=aes256k mon.",
        "set-label-key --key osd_key",
        "auth_service_cipher aes256k",
        "auth wipe-rotating-service-keys",
        "mon auth allow insecure key",
        "client.admin-backup",
        "auth_allowed_ciphers aes256k",
        "auth dump-keys",
    ),
    "monitor elections and store": (
        "election_strategy classic",
        "election_strategy disallow",
        "election_strategy connectivity",
        "add disallowed_leader",
        "connection scores dump",
        "connection scores reset",
        "mon_sync_timeout",
        "paxos_max_join_drift",
        "mon_lease",
        "mon_scrub_interval",
        "mon_memory_target",
    ),
    "osd failure detector": (
        "mon_osd_min_up_ratio",
        "mon_osd_min_in_ratio",
        "mon_osd_laggy_halflife",
        "mon_osd_adjust_heartbeat_grace",
        "mon_osd_auto_mark_auto_out_in",
        "mon_osd_down_out_subtree_limit",
        "mon_osd_min_down_reporters",
        "mon_osd_reporter_subtree_level",
        "osd_heartbeat_interval",
        "osd_heartbeat_grace",
        "osd_mon_heartbeat_interval",
        "osd_mon_heartbeat_stat_stale",
        "osd_mon_report_interval",
    ),
    "osd tuning atlas": (
        "osd_max_scrubs",
        "osd_scrub_begin_hour",
        "osd_scrub_end_hour",
        "osd_scrub_begin_week_day",
        "osd_scrub_end_week_day",
        "osd_scrub_during_recovery",
        "osd_scrub_load_threshold",
        "osd_scrub_chunk_min",
        "osd_scrub_chunk_max",
        "osd_shallow_scrub_chunk_min",
        "osd_shallow_scrub_chunk_max",
        "osd_deep_scrub_stride",
        "osd_op_queue_cut_off",
        "osd_op_complaint_time",
        "osd_op_history_size",
        "osd_op_history_duration",
        "osd_backfill_scan_min",
        "osd_backfill_scan_max",
        "osd_backfill_retry_interval",
        "osd_map_dedup",
        "osd_map_cache_size",
        "osd_map_message_max",
        "osd_recovery_delay_start",
        "osd_recovery_max_chunk",
        "osd_recovery_max_single_start",
        "osd_recover_clone_overlap",
        "osd_recovery_priority",
    ),
    "pool and legacy cache decisions": (
        "allow_ec_optimizations",
        "启用后不能关闭",
        "reed_sol_van",
        "hashpspool",
        "write_fadvise_dontneed",
        "fast_read",
        "recovery_priority",
        "target_max_objects",
        "hit_set_count",
        "min_read_recency_for_promote",
        "cache_target_dirty_high_ratio",
        "cache_min_flush_age",
        "cache-flush-evict-all",
        ".ceph-internal::hit_set_",
    ),
    "balancer and stretch operations": (
        "target_max_misplaced_ratio",
        "upmap_max_deviation",
        "sleep_interval",
        "begin_weekday",
        "pool_ids",
        "read_balance_score",
        "set-require-min-compat-client luminous",
        "set-require-min-compat-client reef",
        "rm-pg-upmap-primary-all",
        "osdmaptool om --upmap",
        "--upmap-active",
        "osdmaptool om --read",
        "source out.txt",
        "ceph-mon --set-crush-location",
        "set_new_tiebreaker",
        "force_recovery_stretch_mode",
        "force_healthy_stretch_mode",
    ),
    "control and binding contract": (
        "test-reweight-by-utilization",
        "osd blocklist range add",
        "cache status",
        "JSON 是机器合同",
        "open_ioctx2",
        "get_last_version",
        "set_locator_key",
        "aio_write_full",
        "aio_flush",
        "list_objects()",
    ),
    "deep diagnostics and escalation": (
        "opcontrol --setup",
        "opreport -cal",
        "opcontrol --reset",
        "heap start_profiler",
        "google-pprof --text --base",
        "heap release",
        "--tool=massif",
        "ceph report > ceph-report.json",
        "Ceph users",
        "Ceph devel",
    ),
}

RADOS_HEALTH_CODES = (
    "DAEMON_OLD_VERSION", "MON_DOWN", "MON_CLOCK_SKEW",
    "MON_MSGR2_NOT_ENABLED", "MON_DISK_LOW", "MON_DISK_CRIT",
    "MON_DISK_BIG", "MON_NETSPLIT", "AUTH_INSECURE_GLOBAL_ID_RECLAIM",
    "AUTH_INSECURE_GLOBAL_ID_RECLAIM_ALLOWED", "AUTH_INSECURE_KEYS_CREATABLE",
    "AUTH_INSECURE_SERVICE_TICKETS", "AUTH_INSECURE_SERVICE_KEY_TYPE",
    "AUTH_INSECURE_ROTATING_SERVICE_KEY_TYPE", "AUTH_INSECURE_CLIENT_KEY_TYPE",
    "AUTH_INSECURE_KEYS_ALLOWED", "AUTH_EMERGENCY_CIPHERS_SET", "MGR_DOWN",
    "MGR_MODULE_DEPENDENCY", "MGR_MODULE_ERROR", "OSD_DOWN", "OSD_ORPHAN",
    "OSD_OUT_OF_ORDER_FULL", "OSD_FULL", "OSD_BACKFILLFULL", "OSD_NEARFULL",
    "OSDMAP_FLAGS", "OSD_FLAGS", "OLD_CRUSH_TUNABLES",
    "OLD_CRUSH_STRAW_CALC_VERSION", "CACHE_POOL_NO_HIT_SET", "OSD_NO_SORTBITWISE",
    "OSD_FILESTORE", "OSD_UNREACHABLE", "POOL_FULL", "BLUEFS_SPILLOVER",
    "BLUEFS_AVAILABLE_SPACE", "BLUEFS_LOW_SPACE", "BLUESTORE_FRAGMENTATION",
    "BLUESTORE_LEGACY_STATFS", "BLUESTORE_NO_PER_POOL_OMAP",
    "BLUESTORE_NO_PER_PG_OMAP", "BLUESTORE_DISK_SIZE_MISMATCH",
    "BLUESTORE_NO_COMPRESSION", "BLUESTORE_SPURIOUS_READ_ERRORS",
    "BLOCK_DEVICE_STALLED_READ_ALERT", "WAL_DEVICE_STALLED_READ_ALERT",
    "DB_DEVICE_STALLED_READ_ALERT", "BLUESTORE_SLOW_OP_ALERT", "DEVICE_HEALTH",
    "DEVICE_HEALTH_IN_USE", "DEVICE_HEALTH_TOOMANY", "PG_AVAILABILITY",
    "PG_DEGRADED", "PG_RECOVERY_FULL", "PG_BACKFILL_FULL", "PG_DAMAGED",
    "OSD_SCRUB_ERRORS", "OSD_TOO_MANY_REPAIRS", "LARGE_OMAP_OBJECTS",
    "CACHE_POOL_NEAR_FULL", "TOO_FEW_PGS", "POOL_PG_NUM_NOT_POWER_OF_TWO",
    "POOL_TOO_FEW_PGS", "TOO_MANY_PGS", "POOL_TOO_MANY_PGS",
    "POOL_TARGET_SIZE_BYTES_OVERCOMMITTED", "POOL_HAS_TARGET_SIZE_BYTES_AND_RATIO",
    "TOO_FEW_OSDS", "SMALLER_PGP_NUM", "MANY_OBJECTS_PER_PG",
    "POOL_APP_NOT_ENABLED", "POOL_NEAR_FULL", "OBJECT_MISPLACED", "OBJECT_UNFOUND",
    "SLOW_OPS", "PG_NOT_SCRUBBED", "PG_NOT_DEEP_SCRUBBED",
    "PG_SLOW_SNAP_TRIMMING", "INCORRECT_NUM_BUCKETS_STRETCH_MODE",
    "STRETCH_MODE_BUCKET_WEIGHT_IMBALANCE", "NONEXISTENT_MON_CRUSH_LOC_STRETCH_MODE",
    "NVMEOF_SINGLE_GATEWAY", "NVMEOF_GATEWAY_DOWN", "NVMEOF_GATEWAY_DELETING",
    "RECENT_CRASH", "RECENT_MGR_MODULE_CRASH", "TELEMETRY_CHANGED", "AUTH_BAD_CAPS",
    "OSD_NO_DOWN_OUT_INTERVAL", "DASHBOARD_DEBUG",
)

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

    def test_each_module_has_customer_delivery_structure(self):
        for document in CEPH_DOCUMENTS:
            text = (CEPH_ROOT / document).read_text(encoding="utf-8")
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

    def test_architecture_signoff_keeps_failure_and_rollback_boundaries(self):
        text = (CEPH_ROOT / "01-architecture.md").read_text(encoding="utf-8")
        failure_matrix = text.split("### 25.6 故障影响矩阵", 1)[1].split(
            "### 25.7", 1
        )[0]
        rollback = text.split("### 25.7 架构变更", 1)[1].split("## 26.", 1)[0]

        for plane in (
            "MON 失去多数派",
            "Active MGR 失败",
            "Primary OSD/host 失败",
            "Active MDS 失败",
            "RGW 实例失败",
            "Public network 分区",
            "Cluster network 分区",
        ):
            self.assertIn(plane, failure_matrix)

        self.assertIn("重新注入旧 CRUSH 规则不会撤销已经发生的 I/O", rollback)
        self.assertIn("把它当成一次新的完整变更", rollback)
        self.assertIn("不能靠原地改参数重写", rollback)
        self.assertIn("停止意味着不再提交下一批变更并保留现场", rollback)

    def test_cephadm_preserves_official_production_facts(self):
        text = (CEPH_ROOT / "02-cephadm.md").read_text(encoding="utf-8")
        for mechanism, facts in CEPHADM_REQUIRED_FACTS.items():
            for fact in facts:
                self.assertIn(fact.lower(), text.lower(), f"{mechanism}: {fact}")

    def test_rados_preserves_official_production_facts(self):
        text = (CEPH_ROOT / "03-rados.md").read_text(encoding="utf-8")
        for mechanism, facts in RADOS_REQUIRED_FACTS.items():
            for fact in facts:
                self.assertIn(fact.lower(), text.lower(), f"{mechanism}: {fact}")

        for code in RADOS_HEALTH_CODES:
            self.assertIn(code, text, f"RADOS health code: {code}")

    def test_rados_cephx_cipher_upgrade_preserves_safe_order(self):
        text = (CEPH_ROOT / "03-rados.md").read_text(encoding="utf-8")
        migration = text.split("## 45. CephX", 1)[1].split("## 46.", 1)[0]
        ordered_facts = (
            "ceph mon set auth_allowed_ciphers aes,aes256k",
            "ceph mon set auth_preferred_cipher aes256k",
            "ceph auth rotate --key-type=aes256k mon.",
            "ceph mon set auth_service_cipher aes256k",
            "ceph config set mon 'mon auth allow insecure key' false",
            "ceph auth get-or-create client.admin-backup",
            "ceph auth rotate --key-type=aes256k client.admin",
            "ceph auth rotate --key-type=aes256k client.<id>",
            "ceph mon set auth_allowed_ciphers aes256k",
        )
        positions = [migration.index(fact) for fact in ordered_facts]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("out-of-quorum", migration)
        self.assertIn("不是普通迁移建议", migration)
        self.assertIn("AUTH_EMERGENCY_CIPHERS_SET", migration)

    def test_rados_cache_tier_removal_preserves_flush_first_order(self):
        text = (CEPH_ROOT / "03-rados.md").read_text(encoding="utf-8")
        removal = text.split("### 50.3 Writeback", 1)[1].split("## 51.", 1)[0]
        ordered_facts = (
            "ceph osd tier cache-mode <cache> proxy",
            "rados -p <cache> ls",
            "rados -p <cache> cache-flush-evict-all",
            "ceph osd tier remove-overlay <base>",
            "ceph osd tier remove <base> <cache>",
        )
        positions = [removal.index(fact) for fact in ordered_facts]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("禁止继续 remove overlay/tier", removal)

    def test_rados_ec_optimization_is_documented_as_irreversible(self):
        text = (CEPH_ROOT / "03-rados.md").read_text(encoding="utf-8")
        pool = text.split("## 49. Pool", 1)[1].split("## 50.", 1)[0]
        for fact in (
            "启用后不能关闭",
            "所有 MON 和 OSD 必须已经升级到 Tentacle",
            "gateway/client 不需要同步升级",
            "Jerasure",
            "ISA-L",
            "reed_sol_van",
            "至少 16 KiB",
            "256 KiB",
            "既有 pool 不能修改",
            "m <= 3",
        ):
            self.assertIn(fact, pool)

        self.assertNotIn("全 daemon/client compatibility", text)

    def test_rados_has_no_conflicting_legacy_cache_removal_advice(self):
        text = (CEPH_ROOT / "03-rados.md").read_text(encoding="utf-8")
        self.assertNotIn("Read-only cache 退出：移除 overlay", text)
        self.assertNotIn("先切 `forward`", text)

    def test_cephadm_examples_follow_tentacle_service_schema(self):
        text = (CEPH_ROOT / "02-cephadm.md").read_text(encoding="utf-8")
        oauth = text.split("### 22.2 OAuth2 Proxy", 1)[1].split("## 23.", 1)[0]
        rgw = text.split("### 17.2 HTTPS", 1)[1].split("### 17.3", 1)[0]

        self.assertIn("ssl_cert:", oauth)
        self.assertIn("ssl_key:", oauth)
        self.assertIn("redirect_url:", oauth)
        self.assertIn("allowlist_domains:", oauth)
        self.assertNotRegex(oauth, r"(?m)^\s+ssl_certificate:\s*$")
        self.assertNotRegex(oauth, r"(?m)^\s+ssl_certificate_key:\s*$")

        self.assertIn("rgw_frontend_ssl_certificate:", rgw)
        self.assertIn("`generate_cert: true` 必须同时设置 `ssl: true`", rgw)

    def test_cephadm_quorum_recovery_preserves_official_destructive_order(self):
        text = (CEPH_ROOT / "02-cephadm.md").read_text(encoding="utf-8")
        recovery = text.split("### 30.1 恢复 MON quorum", 1)[1].split(
            "### 30.2", 1
        )[0]

        ordered_facts = (
            "cephadm unit --fsid <fsid> --name mon.<id> stop",
            "--extract-monmap /tmp/monmap",
            "monmaptool /tmp/monmap --rm",
            "--inject-monmap /tmp/monmap",
            "mon.<survivor-id> start",
            "ceph quorum_status --format json-pretty",
            "ceph orch pause",
            "ceph orch resume",
        )
        positions = [recovery.index(fact) for fact in ordered_facts]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("FSID 必须等于事故集群 FSID", recovery)
        self.assertIn("其余 MON unit 保持停止", recovery)
        self.assertIn("cephadm shell --fsid <fsid> --name mon.<survivor-id>", recovery)
        self.assertNotIn("cephadm enter --name mon.<survivor-id>", recovery)

    def test_cephadm_service_change_preserves_evidence_before_mutation(self):
        text = (CEPH_ROOT / "02-cephadm.md").read_text(encoding="utf-8")
        change = text.split("### 34.5 Service Spec", 1)[1].split("### 34.6", 1)[0]
        ordered_facts = (
            "ceph orch ls --service_name <service> --export",
            "ceph orch ps --service_name <service> --refresh",
            "ceph orch apply -i <service>.candidate.yaml --dry-run",
            "ceph orch apply -i <service>.candidate.yaml",
        )
        positions = [change.index(fact) for fact in ordered_facts]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("只能恢复编排意图", change)
        self.assertIn("仅看到旧 YAML 已接受不算恢复", change)
        self.assertIn("恢复声明", change)
        self.assertIn("恢复业务状态", change)

    def test_cephadm_recovery_package_does_not_claim_specs_are_backups(self):
        text = (CEPH_ROOT / "02-cephadm.md").read_text(encoding="utf-8")
        recovery = text.split("### 34.6 控制面恢复资料包", 1)[1].split(
            "## 35.", 1
        )[0]
        for fact in (
            "Service Spec 不是 Ceph 备份",
            "不保证 MON/auth/config-key 可恢复",
            "运行中直接复制 RocksDB 目录不构成一致备份",
            "cephadm paused",
            "RADOS 与每种对外协议的真实 I/O",
        ):
            self.assertIn(fact, recovery)

    def test_mature_documents_use_github_renderable_mermaid_contract(self):
        for document in ("01-architecture.md", "02-cephadm.md", "03-rados.md"):
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

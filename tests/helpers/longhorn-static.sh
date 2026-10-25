#!/usr/bin/env bash
# Level 2 only: no SSH, registry pulls or Kubernetes calls.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="${LONGHORN_PYTHON:-$ROOT/.venv/bin/python}"
HELM="${LONGHORN_HELM:-helm}"
ANSIBLE="${LONGHORN_ANSIBLE_PLAYBOOK:-ansible-playbook}"
work="$(mktemp -d /tmp/kubeauto-longhorn-static.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
export PYTHONPATH="$ROOT" PYTHONPYCACHEPREFIX="$work/cache" ANSIBLE_ROLES_PATH="$ROOT/roles"
"$HELM" version --short | "$PY" -c 'import sys; assert sys.stdin.read().startswith("v3.19.0+")'
# Unit render tests use the same verified executable, even when supplied by path.
mkdir "$work/bin"
ln -s "$(command -v "$HELM")" "$work/bin/helm"
export PATH="$work/bin:$PATH"
for file in tests/helpers/longhorn-*.sh tests/run_enterprise_regression.sh; do bash -n "$file"; done
"$PY" -m py_compile roles/longhorn/files/longhorn.py roles/longhorn/files/host-check.py roles/longhorn/files/block-check.py tests/helpers/validate_longhorn_matrix.py tests/helpers/longhorn-lab.py tests/helpers/longhorn-backups.py tests/helpers/longhorn-accounts.py tests/helpers/longhorn-uninstall.py tests/helpers/longhorn-ownership.py tests/helpers/longhorn-recurring.py tests/helpers/longhorn-recurring-backup.py tests/helpers/longhorn-recurring-rotation.py tests/helpers/longhorn-performance.py tests/helpers/longhorn-faults.py tests/helpers/longhorn-control-faults.py tests/helpers/longhorn-trim.py tests/helpers/longhorn-encryption.py tests/helpers/longhorn-network.py tests/helpers/longhorn-security.py tests/helpers/longhorn-artifacts.py
"$PY" -m py_compile tests/helpers/longhorn-ownership-evidence.py
"$PY" -m unittest tests.unit.test_longhorn_ownership_evidence -v
"$PY" -m unittest tests.unit.test_longhorn_filesystem_reconcile -v
"$PY" -m py_compile tests/helpers/longhorn-parallel.py tests/helpers/longhorn-v2-im-adopt.py
"$PY" -m py_compile tests/helpers/longhorn-v2-incomplete-clone.py
"$PY" -m unittest tests.unit.test_longhorn_v2_incomplete_clone -v
"$PY" -m py_compile tests/helpers/longhorn-documentation.py
"$PY" -m unittest tests.unit.test_longhorn_documentation -v
"$PY" -m py_compile tests/helpers/longhorn-v2-post-reboot-environment.py
"$PY" -m unittest tests.unit.test_longhorn_v2_post_reboot_environment -v
"$PY" -m py_compile tests/helpers/longhorn-v2-resources-reboot.py
"$PY" -m unittest tests.unit.test_longhorn_v2_resources_reboot -v
"$PY" -m py_compile tests/helpers/longhorn-maintenance.py tests/helpers/longhorn-capacity.py tests/helpers/longhorn-integrity.py tests/helpers/longhorn-disk-lifecycle.py
"$PY" -m unittest tests.unit.test_longhorn_maintenance tests.unit.test_longhorn_capacity tests.unit.test_longhorn_integrity tests.unit.test_longhorn_disk_lifecycle tests.unit.test_longhorn_disk_lifecycle_shell tests.unit.test_longhorn_v1_operations_wiring -v
"$PY" -m py_compile tests/helpers/longhorn-platform.py
"$PY" -m unittest tests.unit.test_longhorn_platform -v
"$PY" -m unittest tests.unit.test_longhorn_parallel tests.unit.test_longhorn_v2_im_adopt tests.unit.test_longhorn_ownership_aggregate -v
"$PY" -m py_compile tests/helpers/longhorn-v2-reboot-relay.py
"$PY" -m unittest tests.unit.test_longhorn_v2_reboot_relay -v
"$PY" -m py_compile tests/helpers/longhorn-v2-runtime-faults.py tests/helpers/longhorn-v2-disk-faults.py tests/helpers/longhorn-v2-all-replicas.py tests/helpers/longhorn-v2-boot-fixture.py
"$PY" -m unittest tests.unit.test_longhorn_v2_runtime_faults tests.unit.test_longhorn_v2_disk_faults tests.unit.test_longhorn_v2_all_replicas tests.unit.test_longhorn_v2_failure_runner tests.unit.test_longhorn_v2_boot_fixture -v
"$PY" -m py_compile tests/helpers/longhorn-v1-upgrade-prepare.py tests/helpers/longhorn-v1-upgrade-lifecycle.py
"$PY" -m unittest tests.unit.test_longhorn_v1_upgrade_prepare tests.unit.test_longhorn_v1_upgrade_shell -v
"$PY" -m py_compile roles/longhorn/files/block-retire.py tests/helpers/longhorn-v2-maintenance-drain.py
"$PY" -m unittest tests.unit.test_longhorn_block_retirement tests.unit.test_longhorn_cpu_isolation_policy tests.unit.test_longhorn_restore_reassert tests.unit.test_longhorn_v2_maintenance_drain tests.unit.test_longhorn_v2_maintenance_drain_shell -v
"$PY" -m py_compile tests/helpers/longhorn-v2-coexist.py tests/helpers/longhorn-v2-cleanup.py tests/helpers/longhorn-v1-stability.py tests/helpers/longhorn-v1-reboot-relay.py tests/helpers/longhorn-node-lifecycle.py tests/helpers/longhorn-v2-observability.py tests/helpers/longhorn-v2-cpu-isolation.py
"$PY" -m py_compile tests/helpers/longhorn-node-local.py
"$PY" -m unittest tests.unit.test_longhorn_node_local tests.unit.test_longhorn_node_local_shell -v
"$PY" -m unittest tests.unit.test_longhorn_v2_coexist tests.unit.test_longhorn_v2_coexist_shell -v
"$PY" -m unittest tests.unit.test_longhorn_v2_cleanup tests.unit.test_longhorn_v2_cleanup_recovery tests.unit.test_longhorn_v1_stability tests.unit.test_longhorn_v1_reboot_relay tests.unit.test_longhorn_node_lifecycle tests.unit.test_longhorn_node_lifecycle_shell tests.unit.test_longhorn_v2_observability tests.unit.test_longhorn_v2_cpu_isolation -v
"$PY" -m py_compile tests/helpers/longhorn-v2-linked-lifecycle.py
"$PY" -m unittest tests.unit.test_longhorn_v2_linked_lifecycle tests.unit.test_longhorn_v2_linked_lifecycle_shell -v
"$PY" -m py_compile tests/helpers/longhorn-im-revision-adopt.py tests/helpers/longhorn-system-recovery.py tests/helpers/longhorn-v1-recovery-target.py tests/helpers/longhorn-v1-disaster.py tests/helpers/longhorn-node-failure.py tests/helpers/longhorn-v1-storage-faults.py
"$PY" -m unittest tests.unit.test_longhorn_im_revision_adopt tests.unit.test_longhorn_system_recovery tests.unit.test_longhorn_system_recovery_shell tests.unit.test_longhorn_v1_recovery_target tests.unit.test_longhorn_v1_disaster tests.unit.test_longhorn_node_failure tests.unit.test_longhorn_node_failure_shell tests.unit.test_longhorn_v1_storage_faults tests.unit.test_longhorn_v1_storage_faults_shell tests.unit.test_longhorn_im_revision_evidence tests.unit.test_longhorn_remaining_public_routes -v
"$PY" -m py_compile tests/helpers/longhorn-upgrade-lifecycle.py tests/helpers/longhorn-v2-upgrade-prepare.py tests/helpers/longhorn-v2-upgrade-baseline.py
"$PY" -m unittest tests.unit.test_longhorn_upgrade_lifecycle tests.unit.test_longhorn_v2_upgrade_prepare tests.unit.test_longhorn_v2_upgrade_shell tests.unit.test_longhorn_v2_upgrade_baseline -v
"$PY" -m py_compile tests/helpers/longhorn-v2-disk-lifecycle.py
"$PY" -m unittest tests.unit.test_longhorn_v2_disk_lifecycle -v
"$PY" -m py_compile tests/helpers/longhorn-v2-maintenance.py
"$PY" -m unittest tests.unit.test_longhorn_v2_maintenance -v
"$PY" -m py_compile tests/helpers/longhorn-v2-runtime-resources.py
"$PY" -m unittest tests.unit.test_longhorn_v2_runtime_resources tests.unit.test_longhorn_v2_runtime_resources_shell -v
"$PY" -m py_compile tests/helpers/longhorn-last-replica.py
"$PY" -m unittest tests.unit.test_longhorn_last_replica tests.unit.test_longhorn_last_replica_shell -v
"$PY" -m py_compile tests/helpers/longhorn-v1-middleware.py tests/helpers/longhorn-v2-middleware.py
"$PY" -m unittest tests.unit.test_longhorn_v1_middleware tests.unit.test_longhorn_v2_middleware tests.unit.test_longhorn_middleware_webhook -v
"$PY" -m py_compile tests/helpers/longhorn-v1-observability.py
"$PY" -m unittest tests.unit.test_longhorn_v1_observability tests.unit.test_longhorn_support_bundle_focus tests.unit.test_longhorn_support_stream tests.unit.test_longhorn_v2_monitoring tests.unit.test_longhorn_monitoring_cache_reconcile -v
"$PY" -m py_compile tests/helpers/longhorn-cleanup.py
"$PY" -m unittest tests.unit.test_longhorn_cleanup -v
"$PY" -m py_compile tests/helpers/longhorn-v2-platform-install.py
"$PY" -m unittest tests.unit.test_longhorn_v2_platform_install -v
"$PY" tests/helpers/validate_longhorn_matrix.py
"$PY" -m py_compile tests/helpers/longhorn-v2-pool.py tests/helpers/longhorn-v2-lab.py tests/helpers/longhorn-v2-core.py tests/helpers/longhorn-v2-resources.py tests/helpers/longhorn-v2-install.py tests/helpers/longhorn-v2-trim.py tests/helpers/longhorn-v2-backups.py tests/helpers/longhorn-v2-recurring.py tests/helpers/longhorn-v2-integrity.py tests/helpers/longhorn-v2-clones.py tests/helpers/longhorn-v2-clone-lifecycle.py tests/helpers/longhorn-v2-control-faults.py tests/helpers/longhorn-v2-interrupt.py tests/helpers/longhorn-v2-manager-loss.py tests/helpers/longhorn-v2-linked-clones.py tests/helpers/longhorn-v2-broken-restore.py tests/helpers/longhorn-v2-systembackup.py tests/helpers/longhorn-v2-systemrestore.py tests/helpers/longhorn-v2-detached-backup.py tests/helpers/longhorn-v2-dr-target.py
"$PY" -m py_compile tests/helpers/longhorn-v2-dr-target-install.py tests/helpers/longhorn-v2-dr-store.py tests/helpers/longhorn-v2-disaster.py tests/helpers/longhorn-v2-dr-failover.py
"$PY" -m unittest tests.unit.test_longhorn_v2_dr_target_install tests.unit.test_longhorn_v2_dr_store -v
"$PY" -m unittest tests.unit.test_longhorn_v2_disaster -v
"$PY" -m unittest tests.unit.test_longhorn_v2_dr_failover -v
"$PY" -m unittest tests.unit.test_longhorn_protection_context -v
"$PY" -m unittest tests.unit.test_longhorn_v2_data_protection -v
"$PY" -m unittest tests.unit.test_longhorn_v2_pool tests.unit.test_longhorn_v2_lab tests.unit.test_longhorn_v2_core tests.unit.test_longhorn_v2_resources tests.unit.test_longhorn_v2_install tests.unit.test_longhorn_v2_trim tests.unit.test_longhorn_v2_backups tests.unit.test_longhorn_v2_recurring tests.unit.test_longhorn_v2_integrity tests.unit.test_longhorn_v2_clones tests.unit.test_longhorn_v2_clone_lifecycle tests.unit.test_longhorn_v2_control_faults tests.unit.test_longhorn_v2_interrupt tests.unit.test_longhorn_v2_manager_loss tests.unit.test_longhorn_v2_interrupt_manager tests.unit.test_longhorn_v2_linked_clones tests.unit.test_longhorn_v2_broken_restore tests.unit.test_longhorn_v2_systembackup tests.unit.test_longhorn_v2_systemrestore tests.unit.test_longhorn_v2_detached_backup tests.unit.test_longhorn_v2_dr_target -v
"$PY" -m unittest tests.unit.test_longhorn_delivery tests.unit.test_longhorn_v2 tests.unit.test_longhorn_gate tests.unit.test_longhorn_lab tests.unit.test_longhorn_faults tests.unit.test_longhorn_control_faults tests.unit.test_longhorn_encryption tests.unit.test_longhorn_network tests.unit.test_longhorn_security tests.unit.test_longhorn_backup_refusal tests.unit.test_longhorn_accounts tests.unit.test_longhorn_uninstall tests.unit.test_longhorn_ownership tests.unit.test_longhorn_recurring tests.unit.test_longhorn_recurring_credentials tests.unit.test_longhorn_recurring_rotation tests.unit.test_longhorn_recurring_rotation_business tests.unit.test_longhorn_trim tests.unit.test_storage_documentation tests.unit.test_middleware_documentation tests.unit.test_six_repo_version_sync -v
"$PY" - "$work" <<'PY'
import json,sys
from pathlib import Path
import yaml
root=Path.cwd();work=Path(sys.argv[1])
for pattern in ('roles/longhorn/**/*.yml','playbooks/*.yml'):
    for path in root.glob(pattern): list(yaml.safe_load_all(path.read_text()))
config=yaml.safe_load((root/'roles/longhorn/defaults/main.yml').read_text())
config.update(base_dir=str(root),cluster_dir=str(work/'disabled'),longhorn_install='no')
work.joinpath('disabled.json').write_text(json.dumps(config))
# No kubeconfig, Helm, inventory clients or API spies: disabled execution must
# finish without attempting any of these prerequisites.
work.joinpath('disabled-play.yml').write_text('''- import_playbook: %s/playbooks/09.longhorn.yml
''' % root)
PY
for playbook in 09.longhorn 07.cluster-addon 90.setup 22.addnode 23.addmaster 32.delnode 33.delmaster 99.clean; do
  "$ANSIBLE" -i localhost, -c local "playbooks/$playbook.yml" -e NODE_TO_ADD=localhost -e NODE_TO_DEL=localhost --syntax-check > "$work/$playbook-syntax.log"
done
"$ANSIBLE" -i localhost, -c local "$work/disabled-play.yml" -e "@$work/disabled.json" > "$work/disabled.log"
"$PY" - "$work/disabled.log" <<'PY'
import sys
from pathlib import Path
text=Path(sys.argv[1]).read_text()
assert 'failed=0' in text and 'unreachable=0' in text
assert 'Gathering Facts' not in text
assert 'Check or reconcile the Longhorn product' in text
PY
"$PY" -m py_compile tests/helpers/longhorn-full-material.py tests/helpers/longhorn-full-evidence.py tests/fixtures/longhorn-full-cli.py
"$PY" -m unittest tests.unit.test_longhorn_full_material tests.unit.test_longhorn_full_evidence tests.unit.test_longhorn_full_shell tests.unit.test_longhorn_full_maintenance -v
"$PY" -m py_compile tests/helpers/longhorn-v2-runtime-continuation.py tests/helpers/longhorn-v2-clone.py
"$PY" -m unittest tests.unit.test_longhorn_v2_runtime_reader tests.unit.test_longhorn_v2_clone -v
"$PY" -m py_compile tests/helpers/longhorn-im-revision-3-adopt.py
"$PY" -m unittest tests.unit.test_longhorn_im_revision_3_adopt -v
"$PY" -m unittest tests.unit.test_longhorn_v2_runtime_consumers tests.unit.test_longhorn_v2_runtime_continuation_runner -v
"$PY" -m unittest tests.unit.test_longhorn_static_reuse -v
"$PY" -m py_compile tests/helpers/longhorn-v1-monitoring-cache.py
"$PY" -m unittest tests.unit.test_longhorn_v1_monitoring_cache -v
echo LONGHORN_STATIC_SCOPED_CLEAN_PASS
echo LONGHORN_STATIC_PASS

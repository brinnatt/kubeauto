"""Product-owned first-hop entry for ``config.yml + kubecli upgrade``.

The target branch owns this entry.  It deliberately refuses while the matrix
or runner prerequisites are incomplete; the legacy 93 playbook remains the
patch-only path and is never used to perform a minor upgrade.
"""
from __future__ import annotations

from pathlib import Path
import sys

from upgrade.product.launcher import DEFAULT_MATRIX, run as admit_and_run
from upgrade.product.coordinator import run_next_hop
from upgrade.product.prepare import prepare_next_hop
from upgrade.product.release_policy import admit_packaged
from upgrade.product.hop import MATRIX_PATH

DEFAULT_MATRIX = MATRIX_PATH


def _admit(runner, required):
    if getattr(sys, 'frozen', False):
        admit_packaged(Path(__file__).resolve().parents[2])
        return runner()
    if not DEFAULT_MATRIX.is_file():
        raise RuntimeError('upgrade admission matrix is not packaged with this product')
    return admit_and_run(DEFAULT_MATRIX, runner=runner, required=required)


def run_minor_upgrade(manager, cluster: str):
    """Admit the target matrix before binding the real Ansible callback."""
    def product_runner():
        return run_next_hop(manager, cluster)

    return _admit(product_runner, ['UPG-01', 'UPG-02', 'UPG-03', 'UPG-04', 'UPG-05'])


def prepare_minor_upgrade(manager, cluster: str, continuity_path):
    """Admit static prerequisites before observing or saving a recovery point."""
    return _admit(lambda: prepare_next_hop(manager, cluster, continuity_path),
                  ['UPG-01', 'UPG-02', 'UPG-03', 'UPG-04'])


def run_harbor_operation(manager, cluster, *, operation, backup_directory,
                         ca_file=None, bridge_package=None, target_package=None, transfer_directory=None):
    """A separate config.yml-owned operation; recovery never launches K8s work."""
    import re

    def remote_path(value):
        if (not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or
                value == '/' or '..' in Path(value).parts):
            raise ValueError('Harbor inputs require absolute managed-host paths without spaces or parent traversal')
        return value

    if operation not in ['backup', 'upgrade', 'restore', 'export', 'import', 'abort-import']:
        raise ValueError('unknown Harbor maintenance operation')
    backup_directory = remote_path(backup_directory)
    if operation == 'backup' and not ca_file:
        raise ValueError('Harbor backup requires --harbor-ca-file on the managed host')
    if operation in ['upgrade', 'import'] and not target_package:
        raise ValueError('Harbor upgrade requires the verified target offline package on the managed host')
    if (operation == 'import') != bool(transfer_directory):
        raise ValueError('--harbor-transfer-dir is required only for --harbor import')
    transfer_directory = remote_path(transfer_directory) if transfer_directory else ''
    packages = {version: remote_path(path) for version,path in
                [('v2.14.6', bridge_package), ('v2.16.0', target_package)] if path}

    def runner():
        manager._validate_for_setup(cluster)
        variables = manager._yaml_to_dict(manager.clusters_dir/cluster/'config.yml')
        variables.update(UPGRADE_HARBOR_OPERATION=operation, UPGRADE_HARBOR_BACKUP=backup_directory,
                         UPGRADE_HARBOR_CA=remote_path(ca_file) if ca_file else '',
                         UPGRADE_HARBOR_PACKAGES=packages, UPGRADE_HARBOR_TRANSFER=transfer_directory)
        return manager._run_playbook(cluster, 'upgrade/product/harbor.yml', extra_vars=variables,
                                     fail_msg='Harbor maintenance stopped; preserve its private checkpoint and diagnostics')

    if operation in ['restore', 'abort-import']:
        # Recovery must remain reachable after a failed migration, even when
        # its acceptance is pending. The module validates the complete original
        # checkpoint and ownership before touching any current state.
        if getattr(sys, 'frozen', False):
            admit_packaged(Path(__file__).resolve().parents[2])
        return runner()
    return _admit(runner, ['UPG-01', 'UPG-02', 'UPG-03', 'UPG-04'])

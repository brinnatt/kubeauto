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

DEFAULT_MATRIX = Path(__file__).resolve().parents[1] / 'v1.35.10/test-matrix.yaml'


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

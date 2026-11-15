"""Product-owned first-hop entry for ``config.yml + kubecli upgrade``.

The target branch owns this entry.  It deliberately refuses while the matrix
or runner prerequisites are incomplete; the legacy 93 playbook remains the
patch-only path and is never used to perform a minor upgrade.
"""
from __future__ import annotations

from pathlib import Path

from upgrade.product.launcher import DEFAULT_MATRIX, run as admit_and_run
from upgrade.product.coordinator import run_first_hop


def run_minor_upgrade(manager, cluster: str):
    """Admit the target matrix before binding the real Ansible callback."""
    matrix = DEFAULT_MATRIX
    if not matrix.is_file():
        raise RuntimeError("first-hop upgrade matrix is not packaged with this product")

    def product_runner():
        return run_first_hop(manager, cluster)

    return admit_and_run(
        matrix,
        runner=product_runner,
        required=["UPG-01", "UPG-02", "UPG-03", "UPG-04", "UPG-05"],
    )

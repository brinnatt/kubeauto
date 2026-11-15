"""Admission boundary for the first-hop upgrade product path.

The launcher owns only read-only admission and callback ordering.  A caller
must provide the real kubecli runner and a mutation callback after admission;
pending matrix rows, a stale combination, or disabled execution are refused
before that callback can be reached.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from upgrade import validate


class UpgradeAdmissionRefused(ValueError):
    """The requested upgrade is not admitted for the current matrix state."""


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "upgrade/v1.34.13/test-matrix.yaml"


def _read_matrix(path: Path) -> tuple[dict, list[str]]:
    data, errors = validate.validate(ROOT, path)
    if errors:
        raise UpgradeAdmissionRefused("; ".join(errors))
    return data, errors


def admit_static(path: Path = DEFAULT_MATRIX, *, required: Iterable[str] = ()) -> dict:
    """Return a frozen matrix snapshot after all requested prerequisites pass."""
    matrix, _ = _read_matrix(Path(path))
    errors = validate.combination_errors(ROOT, Path(path).parent, matrix["meta"])
    if errors:
        raise UpgradeAdmissionRefused("; ".join(errors))
    rows = {case["id"]: case for case in matrix["cases"]}
    required_ids = list(required)
    pending = [cid for cid in required_ids if rows.get(cid, {}).get("status") != "pass"]
    if pending:
        raise UpgradeAdmissionRefused(
            "required matrix prerequisites are not pass: " + ", ".join(pending)
        )
    return validate.snapshot(matrix)


def run(path: Path = DEFAULT_MATRIX, *, runner: Callable[[], object],
        required: Iterable[str]) -> object:
    """Admit and then invoke the already-bound real product runner.

    ``runner`` is deliberately injected so tests can prove that refusal occurs
    before any remote or stateful action.  The production caller binds it to
    the kubecli/config.yml path only after leases and recovery points exist.
    """
    matrix, _ = _read_matrix(Path(path))
    errors = validate.combination_errors(ROOT, Path(path).parent, matrix["meta"])
    if errors:
        raise UpgradeAdmissionRefused("; ".join(errors))
    rows = {case["id"]: case for case in matrix["cases"]}
    required_ids = list(required)
    pending = [cid for cid in required_ids if rows.get(cid, {}).get("status") != "pass"]
    if pending:
        raise UpgradeAdmissionRefused(
            "required matrix prerequisites are not pass: " + ", ".join(pending)
        )
    if matrix["meta"].get("execution_enabled") is not True:
        raise UpgradeAdmissionRefused("upgrade execution remains disabled")
    if not matrix["meta"].get("runner"):
        raise UpgradeAdmissionRefused("upgrade runner is not bound")
    return runner()

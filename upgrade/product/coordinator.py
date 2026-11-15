"""Outer coordinator for the first Kubernetes minor-upgrade hop.

This module is deliberately small: admission remains in :mod:`launcher`, the
stage owns the ordered Kubernetes mutations, and ``ClusterManager`` remains the
only Ansible execution boundary.  The coordinator binds those pieces to the
actual cluster directory and keeps the recovery/observation inputs durable.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from upgrade.product.inventory import observe_source
from upgrade.product.journal import Journal
from upgrade.product.kubernetes_preflight import Refused, mapping, query
from upgrade.product.kubernetes_stage import CONTROL, KubernetesStage, SOURCE, TARGET


CLUSTER = "upgrade-v1336-source"
STATE = Path("/var/tmp/kubeauto-upgrade-v1336-source")
LEASE = Path("/var/tmp/kubeauto-upgrade-v1336-source.lease")
SOURCE_RECEIPT = Path("/var/tmp/kubeauto-upgrade-v1336-source.receipt.json")
RUN_ID = "k8s-v1336-to-v13413"
RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_MANIFEST = (REPOSITORY_ROOT / "upgrade" / "v1.34.13" /
                    "runtime" / "manifest.json")


class CoordinatorRefused(ValueError):
    """The outer product boundary cannot prove a safe first-hop handoff."""


def _private_regular(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise CoordinatorRefused(f"required private file is missing: {path}") from None
    if (not path.is_file() or path.is_symlink() or info.st_uid != os.geteuid() or
            (info.st_mode & 0o777) != 0o600):
        raise CoordinatorRefused(f"required private file has unsafe ownership or mode: {path}")


def _load_json(path: Path) -> dict:
    _private_regular(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CoordinatorRefused(f"invalid durable JSON receipt: {path}") from None
    if not isinstance(value, dict):
        raise CoordinatorRefused(f"durable receipt must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise CoordinatorRefused(f"recovery point cannot be read: {path}") from None
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise CoordinatorRefused(f"{label} is missing: {path}") from None
    if not path.is_file() or path.is_symlink() or not (info.st_mode & 0o111) and label.startswith("runtime bundle"):
        raise CoordinatorRefused(f"{label} is not a usable regular file: {path}")


def _runtime_inputs() -> dict:
    """Load and verify the frozen, official containerd bridge inputs."""
    _regular_file(RUNTIME_MANIFEST, "runtime manifest")
    try:
        data = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CoordinatorRefused("runtime manifest is not valid JSON") from None
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or
            data.get("source") != SOURCE or data.get("target") != TARGET):
        raise CoordinatorRefused("runtime manifest is not bound to this first hop")
    if (not re.fullmatch(r"[0-9a-f]{64}", data.get("source_config_sha256", "")) or
            not re.fullmatch(r"[0-9a-f]{64}", data.get("unit_sha256", ""))):
        raise CoordinatorRefused("runtime source identity hashes are incomplete")
    config = data.get("config")
    bundles = data.get("bundles")
    if (not isinstance(config, dict) or not isinstance(config.get("path"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", config.get("sha256", "")) or
            not isinstance(bundles, dict) or set(bundles) != {"2.2.10", "2.3.7"}):
        raise CoordinatorRefused("runtime bridge manifest is incomplete")

    def owned(relative: str, label: str) -> Path:
        path = (REPOSITORY_ROOT / relative).resolve()
        runtime_root = (REPOSITORY_ROOT / "upgrade" / "v1.34.13" / "runtime").resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(runtime_root):
            raise CoordinatorRefused(f"{label} escapes the upgrade runtime directory")
        _regular_file(path, label)
        return path

    config_path = owned(config["path"], "runtime config")
    if _sha256(config_path) != config["sha256"]:
        raise CoordinatorRefused("reviewed runtime config SHA256 changed")
    result = {
        "UPGRADE_CONTAINERD_CONFIG": {
            "path": str(config_path), "sha256": config["sha256"]},
        "UPGRADE_CONTAINERD_SOURCE_CONFIG_SHA256": data["source_config_sha256"],
        "UPGRADE_CONTAINERD_UNIT_SHA256": data["unit_sha256"],
        "UPGRADE_CONTAINERD_BUNDLES": {},
    }
    for version in ("2.2.10", "2.3.7"):
        item = bundles[version]
        if not isinstance(item, dict) or not isinstance(item.get("directory"), str) or not isinstance(item.get("sha256"), dict):
            raise CoordinatorRefused(f"runtime bundle {version} is incomplete")
        if set(item["sha256"]) != {"containerd", "containerd-shim-runc-v2", "ctr", "runc"}:
            raise CoordinatorRefused(f"runtime bundle {version} has an unexpected file set")
        directory = (REPOSITORY_ROOT / item["directory"]).resolve()
        runtime_root = (REPOSITORY_ROOT / "upgrade" / "v1.34.13" / "runtime").resolve()
        if (Path(item["directory"]).is_absolute() or not directory.is_relative_to(runtime_root) or
                not directory.is_dir() or directory.is_symlink()):
            raise CoordinatorRefused(f"runtime bundle {version} directory is unsafe")
        checksums = {}
        for name, expected in item["sha256"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
                raise CoordinatorRefused(f"runtime bundle {version} has an invalid SHA256")
            path = directory / name
            _regular_file(path, f"runtime bundle {version} file")
            if _sha256(path) != expected:
                raise CoordinatorRefused(f"runtime bundle {version} file SHA256 changed: {name}")
            checksums[name] = expected
        result["UPGRADE_CONTAINERD_BUNDLES"][version] = {
            "directory": str(directory), "sha256": checksums}
    if (result["UPGRADE_CONTAINERD_BUNDLES"]["2.2.10"]["sha256"]["runc"] !=
            result["UPGRADE_CONTAINERD_BUNDLES"]["2.3.7"]["sha256"]["runc"]):
        raise CoordinatorRefused("runtime bridge runc identity is not stable")
    return result


def _verify_source_lease() -> dict:
    _private_regular(LEASE)
    receipt = _load_json(SOURCE_RECEIPT)
    recovery = receipt.get("recovery_point", {})
    if (receipt.get("schema_version") != 1 or receipt.get("source") != SOURCE or
            receipt.get("target") != TARGET or receipt.get("cluster") != CLUSTER or
            receipt.get("mutation") != "none-after-setup" or
            receipt.get("cluster_identity", {}).get("namespace") != "kube-system" or
            not receipt.get("cluster_identity", {}).get("uid")):
        raise CoordinatorRefused("source receipt is not the frozen v1.33.6 source boundary")
    path = Path(recovery.get("path", ""))
    expected = recovery.get("sha256", "")
    if (not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected) or
            not path.is_file() or _sha256(path) != expected):
        raise CoordinatorRefused("source recovery point is missing or its SHA256 changed")
    return receipt


def _target_observer(manager, cluster: str, variables: dict, kubectl: Path,
                     kubeconfig: Path, output: Path) -> dict:
    """Observe through the product runner, then the real target kubectl."""
    try:
        return observe_source(
            lambda playbook, **kwargs: manager._run_playbook(cluster, playbook, **kwargs),
            variables,
            kubectl=kubectl,
            kubectl_sha256=hashlib.sha256(kubectl.read_bytes()).hexdigest(),
            kubeconfig=kubeconfig,
            output=output,
        )
    except (Refused, OSError, TypeError, KeyError, AttributeError) as error:
        raise CoordinatorRefused(f"source observation refused: {error}") from error


def _durable_source_observation(manager, cluster: str, variables: dict,
                                kubectl: Path, kubeconfig: Path, state: Path) -> dict:
    path = state / "source-observation.json"
    if path.exists() or path.is_symlink():
        return _load_json(path)
    with tempfile.NamedTemporaryFile(dir=state, prefix="source-observation-",
                                     suffix=".json", delete=False) as stream:
        temporary = Path(stream.name)
    temporary.unlink()
    try:
        observed = _target_observer(manager, cluster, variables, kubectl, kubeconfig, temporary)
        temporary.write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
        return observed
    finally:
        temporary.unlink(missing_ok=True)


def _business_probe(kubectl: Path, kubeconfig: Path, backends: list[str],
                    expected_versions: set[str]) -> None:
    """Small read-only continuity probe used at every journal boundary."""
    for server in backends:
        version = mapping(query(kubectl, kubeconfig, server, ["get", "--raw=/version"]))
        if version.get("gitVersion") not in expected_versions:
            raise CoordinatorRefused("business boundary observed an unexpected API version")
        query(kubectl, kubeconfig, server, ["get", "--raw=/readyz"])
    pods = mapping(query(kubectl, kubeconfig, backends[0], ["get", "pods", "-A", "-o", "json"]))
    items = pods.get("items")
    if not isinstance(items, list):
        raise CoordinatorRefused("business boundary returned an invalid Pod list")
    bad = [item.get("metadata", {}).get("name", "unknown") for item in items
           if item.get("status", {}).get("phase") == "Failed"]
    if bad:
        raise CoordinatorRefused("business boundary observed failed Pods: " + ",".join(bad[:8]))


def run_first_hop(manager, cluster: str):
    """Run the admitted first hop through the real ``ClusterManager`` runner."""
    if cluster != CLUSTER:
        raise CoordinatorRefused(f"first-hop source cluster must be {CLUSTER}")
    receipt = _verify_source_lease()
    cluster_dir = manager.clusters_dir / cluster
    config_path = cluster_dir / "config.yml"
    kubeconfig = cluster_dir / "kubectl.kubeconfig"
    kubectl = manager.kube_bin_dir / "kubectl"
    for path in (config_path, kubeconfig, kubectl):
        if not path.is_file():
            raise CoordinatorRefused(f"product input is missing: {path}")
    variables = manager._yaml_to_dict(config_path)
    variables.update({
        "K8S_VER": TARGET.removeprefix("v"),
        "UPGRADE_SOURCE_VERSION": SOURCE,
        "UPGRADE_PRECONDITIONS_VERIFIED": True,
        "UPGRADE_RUN_ID": RUN_ID,
        "UPGRADE_DRAIN_TIMEOUT_SECONDS": 300,
        "UPGRADE_EXPECT_MASTER_UNSCHEDULABLE": True,
    })
    state = STATE
    if not state.is_dir() or state.is_symlink():
        raise CoordinatorRefused("source state directory is missing or unsafe")
    observed = _durable_source_observation(manager, cluster, variables, kubectl, kubeconfig, state)
    inventory = observed.get("effective_inventory", {})
    hosts = inventory.get("nodes", {})
    backend_records = observed.get("api_backends", [])
    if (not isinstance(hosts, dict) or not hosts or not isinstance(backend_records, list) or
            not backend_records or any(not isinstance(item, dict) for item in backend_records)):
        raise CoordinatorRefused("complete effective inventory observation is required")
    backends = [item.get("server") for item in backend_records]
    if (any(not isinstance(server, str) for server in backends) or
            len(backends) != len(set(backends))):
        raise CoordinatorRefused("complete unique API backend observation is required")
    variables["UPGRADE_API_BACKENDS"] = backends
    stage_source = dict(observed)
    stage_source.update({
        "mutation": "none",
        "nodes": observed.get("nodes", []),
        "api_backends": observed.get("api_backends", []),
        "kubectl_sha256": observed.get("kubectl_sha256"),
    })
    stage = KubernetesStage(stage_source, hosts, variables, kubectl, kubeconfig,
        lambda playbook, **kwargs: manager._run_playbook(cluster, playbook, **kwargs))
    journal_dir = state / "journal"
    with Journal(journal_dir, {**stage.binding, "run_id": RUN_ID}, stage.steps) as journal:
        # Runtime assets are a node-stage input.  Keep them out of the
        # control-plane journal fingerprint so a control-plane boundary that
        # was already accepted can resume safely after this admission was
        # added; node admission still verifies every file and checksum before
        # the first drain.
        if CONTROL in journal.data["completed"]:
            stage.variables.update(_runtime_inputs())
        def verify_business():
            active = journal.data.get("active")
            # During the active control-plane boundary the playbook has already
            # advanced some or all API backends; the stage observer is the
            # authority for the exact prefix. Accept that journal-owned mixed
            # window without inferring completion from it.
            allowed = ({SOURCE, TARGET} if active == CONTROL else
                       ({SOURCE} if not journal.data["completed"] else {TARGET}))
            _business_probe(kubectl, kubeconfig, backends, allowed)
        stop_after = None if CONTROL in journal.data['completed'] else CONTROL
        stage.run(journal, verify_business, stop_after=stop_after)
        cleanup_marker = ("KUBERNETES_CONTROL_PLANE_SCOPE_VERIFY_PASS"
                          if len(journal.data["completed"]) == 1 else
                          "KUBERNETES_STAGE_SCOPE_VERIFY_PASS")
        result = {
            "schema_version": 1, "run_id": RUN_ID, "source": SOURCE, "target": TARGET,
            "cluster": cluster, "completed": list(journal.data["completed"]),
            "failures": list(journal.data["failures"]),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "success_marker": ("UPGRADE_CONTROL_PLANE_STAGE_PASS"
                               if CONTROL in journal.data["completed"] and
                               len(journal.data["completed"]) == 1 else
                               "UPGRADE_KUBERNETES_STAGE_PASS"),
            "cleanup_marker": cleanup_marker,
        }
    result_path = state / "first-hop-result.json"
    temporary = result_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, result_path)
    print(result["success_marker"], flush=True)
    print(result["cleanup_marker"], flush=True)
    return result

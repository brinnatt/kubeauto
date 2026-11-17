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
import platform
import fcntl

from upgrade.product.inventory import observe_source, collect, BOOK as INVENTORY_BOOK
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


def _next_runtime_inputs(extra_bin_dir):
    """Consume the already published dual-architecture pack; never fetch or compile."""
    pins = json.loads((Path(__file__).parent.parent/'v1.35.10/artifact-pins.json').read_text())
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if arch not in pins['runtime']:
        raise CoordinatorRefused('runtime architecture is outside the admitted pack')
    root = Path(extra_bin_dir)
    bundles = {}
    for version, item in pins['runtime'][arch].items():
        directory = root/item['directory']
        for name, expected in item['sha256'].items():
            binary = directory/name
            _regular_file(binary, 'runtime bundle file')
            if _sha256(binary) != expected:
                raise CoordinatorRefused('runtime pack file differs from the admitted official bytes')
        bundles[version] = {**item, 'directory': str(directory)}
    config = Path(__file__).with_name('containerd-config-v3.toml')
    return {'UPGRADE_CONTAINERD_BUNDLES': bundles,
            'UPGRADE_CONTAINERD_CONFIG': {'path': str(config), 'sha256': _sha256(config)},
            'UPGRADE_CONTAINERD_SOURCE_CONFIG_SHA256': pins['runtime_source'],
            'UPGRADE_CONTAINERD_UNIT_SHA256': pins['runtime_unit'],
            'UPGRADE_CONTAINERD_ACCEPT_EXISTING_TARGET': True}


def _verify_source_lease(*, source=SOURCE, target=TARGET, cluster=CLUSTER,
                         lease=None, receipt_path=None) -> dict:
    _private_regular(LEASE if lease is None else Path(lease))
    receipt = _load_json(SOURCE_RECEIPT if receipt_path is None else Path(receipt_path))
    recovery = receipt.get("recovery_point", {})
    if (receipt.get("schema_version") != 1 or receipt.get("source") != source or
            receipt.get("target") != target or receipt.get("cluster") != cluster or
            receipt.get("mutation") != "none-after-setup" or
            receipt.get("cluster_identity", {}).get("namespace") != "kube-system" or
            not receipt.get("cluster_identity", {}).get("uid")):
        raise CoordinatorRefused("source receipt is not the reviewed source boundary")
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
    return _run_hop(manager, cluster, source=SOURCE, target=TARGET,
                    expected_cluster=CLUSTER, state=STATE, run_id=RUN_ID)


def run_next_hop(manager, cluster: str):
    """The independently admitted second hop; no global state is patched."""
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", cluster):
        raise CoordinatorRefused("invalid cluster name")
    state = Path("/var/tmp") / ("kubeauto-upgrade-" + cluster)
    lease = Path(str(state) + '.lease')
    _private_regular(lease)
    descriptor = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CoordinatorRefused('another preparation or upgrade owns this cluster boundary') from None
        if os.fstat(descriptor).st_ino != lease.stat().st_ino:
            raise CoordinatorRefused('upgrade lease identity changed')
        return _run_hop(manager, cluster, source="v1.34.13", target="v1.35.10",
                        expected_cluster=cluster, state=state,
                        run_id="k8s-v13413-to-v13510", next_hop=True)
    finally:
        os.close(descriptor)


def _run_hop(manager, cluster, *, source, target, expected_cluster, state, run_id,
             next_hop=False):
    """Run the admitted first hop through the real ``ClusterManager`` runner."""
    if cluster != expected_cluster:
        raise CoordinatorRefused(f"first-hop source cluster must be {expected_cluster}")
    receipt = (_verify_source_lease(source=source, target=target, cluster=cluster,
        lease=Path(str(state)+".lease"), receipt_path=Path(str(state)+".receipt.json"))
        if next_hop else _verify_source_lease())
    cluster_dir = manager.clusters_dir / cluster
    config_path = cluster_dir / "config.yml"
    kubeconfig = cluster_dir / "kubectl.kubeconfig"
    kubectl = manager.kube_bin_dir / "kubectl"
    for path in (config_path, kubeconfig, kubectl):
        if not path.is_file():
            raise CoordinatorRefused(f"product input is missing: {path}")
    variables = manager._yaml_to_dict(config_path)
    if next_hop:
        if (not state.is_dir() or state.is_symlink() or state.stat().st_uid != os.geteuid()
                or state.stat().st_mode & 0o777 != 0o700):
            raise CoordinatorRefused('private admitted source state directory required')
        from upgrade.product.next_config import configuration, publish
        variables, final_config = configuration(config_path, state)
    variables.update({
        "K8S_VER": target.removeprefix("v"),
        "UPGRADE_SOURCE_VERSION": source,
        "UPGRADE_PRECONDITIONS_VERIFIED": True,
        "UPGRADE_RUN_ID": run_id,
        "UPGRADE_DRAIN_TIMEOUT_SECONDS": 300,
        "UPGRADE_EXPECT_MASTER_UNSCHEDULABLE": True,
    })
    if not state.is_dir() or state.is_symlink():
        raise CoordinatorRefused("source state directory is missing or unsafe")
    if state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o777 != 0o700:
        raise CoordinatorRefused('source state directory must be private and owned')
    variables['UPGRADE_STATE_DIRECTORY'] = str(state)
    observed = _durable_source_observation(manager, cluster, variables, kubectl, kubeconfig, state)
    inventory = observed.get("effective_inventory", {})
    if next_hop:
        if receipt.get('source_observation_sha256') != _sha256(state / 'source-observation.json'):
            raise CoordinatorRefused('source observation differs from the prepared recovery boundary')
        current_inventory = collect(manager._run_playbook(cluster, INVENTORY_BOOK,
            extra_vars=variables, controller_only=True))
        if current_inventory != inventory:
            raise CoordinatorRefused('effective inventory changed since the source recovery boundary')
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
    if observed.get("cluster_identity") != receipt.get("cluster_identity"):
        raise CoordinatorRefused("observed cluster UID differs from the recovery receipt")
    original_nodes = {n["name"]: n["uid"] for n in receipt.get("nodes", [])}
    if original_nodes != {n["name"]: n["uid"] for n in observed.get("nodes", [])}:
        raise CoordinatorRefused("observed node UIDs differ from the recovery receipt")
    if next_hop:
        if type(variables.get("KUBELET_FAIL_CGROUP_V1", True)) is not bool:
            raise CoordinatorRefused("cgroup policy must be an explicit boolean")
        result = manager._run_playbook(cluster, Path(__file__).with_name('next-preconditions.yml'),
                                       extra_vars=variables)
        if type(result.rc) is not int or result.rc != 0:
            raise CoordinatorRefused('next-hop platform prerequisites refused before mutation')
        result = manager._run_playbook(cluster, Path(__file__).with_name('api-auth-observe.yml'),
                                       extra_vars=variables)
        if type(result.rc) is not int or result.rc != 0:
            raise CoordinatorRefused('API authentication compatibility refused before mutation')
        from upgrade.product.next_api import admit as admit_api
        for server in backends:
            admit_api(kubectl, kubeconfig, server)
        variables.update(_next_runtime_inputs(manager.extra_bin_dir))
        from upgrade.product.continuity import Continuity
        from upgrade.product.next_platform import PlatformPrerequisites
        from upgrade.product.next_etcd import EtcdPatch
        from upgrade.product.next_addons import AddonStages
        traffic = Continuity(kubectl, kubeconfig, backends[0], receipt.get('continuity'))
        traffic()  # Prove the existing ledger before admitting any mutation.
        platform_stage = PlatformPrerequisites(manager, cluster, observed, variables, state, traffic)
        etcd_stage = EtcdPatch(manager, cluster, variables, observed, state)
        addon_stage = AddonStages(kubectl, kubeconfig, observed, variables, state,
                                  traffic, receipt['continuity'])
    stage = KubernetesStage(stage_source, hosts, variables, kubectl, kubeconfig,
        lambda playbook, **kwargs: manager._run_playbook(cluster, playbook, **kwargs))
    journal_dir = state / "journal"
    steps = ([*etcd_stage.steps, *platform_stage.steps, *stage.steps, *addon_stage.steps, 'product/config']
             if next_hop else stage.steps)
    binding = {**stage.binding, "run_id": run_id}
    if next_hop:
        binding['platform_inputs_sha256'] = hashlib.sha256(json.dumps({
            'receipt_sha256': _sha256(Path(str(state)+'.receipt.json')),
            'implementation': {p.name: _sha256(p) for p in [
                Path(__file__), Path(__file__).with_name('next_platform.py'),
                Path(__file__).with_name('continuity.py'), Path(__file__).with_name('calico_stage.py'),
                Path(__file__).with_name('calico-client.yml'), Path(__file__).with_name('calico-qualify.yml'),
                Path(__file__).with_name('next-preconditions.yml'), Path(__file__).with_name('next_api.py'),
                Path(__file__).with_name('api-auth.yml'), Path(__file__).with_name('api-auth-observe.yml'),
                Path(__file__).with_name('next_etcd.py'), Path(__file__).with_name('next_addons.py'),
                Path(__file__).with_name('next_config.py'), Path(__file__).with_name('etcd.yml'),
                Path(__file__).with_name('etcd-observe.yml'), Path(__file__).with_name('dns_stage.py'),
                Path(__file__).with_name('metrics_stage.py'),
                Path(__file__).parent.parent/'v1.35.10/artifact-pins.json']}
        }, sort_keys=True).encode()).hexdigest()
    with Journal(journal_dir, binding, steps) as journal:
        if next_hop:
            # Verify actual cluster/node UIDs and journal-owned progress before
            # any etcd or CNI mutation, including on a resumed invocation.
            stage.observe(journal)
        # Runtime assets are a node-stage input.  Keep them out of the
        # control-plane journal fingerprint so a control-plane boundary that
        # was already accepted can resume safely after this admission was
        # added; node admission still verifies every file and checksum before
        # the first drain.
        if not next_hop and CONTROL in journal.data["completed"]:
            stage.variables.update(_runtime_inputs())
        def verify_business():
            active = journal.data.get("active")
            # During the active control-plane boundary the playbook has already
            # advanced some or all API backends; the stage observer is the
            # authority for the exact prefix. Accept that journal-owned mixed
            # window without inferring completion from it.
            allowed = ({source, target} if active == CONTROL else
                       ({source} if CONTROL not in journal.data["completed"] else {target}))
            _business_probe(kubectl, kubeconfig, backends, allowed)
            if next_hop:
                traffic()
        if next_hop:
            etcd_stage.run(journal)
            platform_stage.run(journal)
        stop_after = None if CONTROL in journal.data['completed'] else CONTROL
        stage.run(journal, verify_business, stop_after=stop_after)
        if next_hop and all(step in journal.data['completed'] for step in stage.steps):
            addon_stage.run(journal)
            if 'product/config' in journal.data['completed']:
                if config_path.read_text() != final_config:
                    raise CoordinatorRefused('accepted final product config changed')
            else:
                journal.begin('product/config')
                try:
                    verify_business()
                    publish(config_path, final_config)
                except BaseException as error:
                    journal.failed('product/config', getattr(error, 'rc', 1), 'product')
                    raise
                journal.complete('product/config')
        control_only = CONTROL in journal.data['completed'] and not any(
            s in journal.data['completed'] for s in stage.steps[1:])
        cleanup_marker = ("KUBERNETES_CONTROL_PLANE_SCOPE_VERIFY_PASS"
                          if control_only else
                          "KUBERNETES_STAGE_SCOPE_VERIFY_PASS")
        result = {
            "schema_version": 1, "run_id": run_id, "source": source, "target": target,
            "cluster": cluster, "completed": list(journal.data["completed"]),
            "failures": list(journal.data["failures"]),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "success_marker": ("UPGRADE_NEXT_HOP_STAGE_PASS" if next_hop and journal.data['completed'] == steps else
                               "UPGRADE_CONTROL_PLANE_STAGE_PASS"
                               if control_only else
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

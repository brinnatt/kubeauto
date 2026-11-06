#!/usr/bin/env python3
"""Freeze the exact ES and fixture image inventory before any stateful gate.

Publication evidence proves the two formal registries. This read-only gate
checks current China manifests and reuses the existing catalog fingerprint
implementation; it never builds an image or silently substitutes a source.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]


def inventory():
    sys.path.insert(0, str(ROOT))
    from common.constants import KubeConstant
    k = KubeConstant()
    images = {k.ansible_execution_image}
    for group in ("elasticsearch", "elasticsearch-smartcn", "elasticsearch-qualification", "prometheus", "logging-efk", "logging-loki", "ingress-nginx", "kafka", "longhorn", "local-path-provisioner"):
        images.update(k.component_images[group])
    images.update("brinnatt/" + name + ":" + tag for name, tag in k.longhorn_image_tags(k.v_longhorn).items())
    images.add("brinnatt/snapshot-controller:" + k.v_longhorn_snapshot_controller)
    images.update(("brinnatt/registry:" + k.v_docker_registry, "brinnatt/busybox:1.37", "brinnatt/minio:RELEASE.2025-04-08T15-41-24Z", "brinnatt/minio-mc:RELEASE.2025-04-08T15-39-49Z"))
    for name, tag in (("calico-cni", k.v_calico), ("calico-node", k.v_calico), ("calico-kube-controllers", k.v_calico), ("coredns", k.v_coredns), ("k8s-dns-node-cache", k.v_dnsnodecache), ("metrics-server", k.v_metricsserver), ("pause", k.v_pause)):
        images.add("brinnatt/" + name + ":" + tag)
    return images


def records(publication):
    if (publication.get("status") != "pass" or publication.get("failure_markers") != 0 or publication.get("durable_rc") != 0):
        raise ValueError("publication is incomplete")
    runs = publication.get("runs", [publication])
    if not runs or not publication.get("artifact_sha256"):
        raise ValueError("publication lacks raw archive provenance")
    for run in runs:
        if (run.get("workflow_conclusion") != "success" or run.get("durable_rc") != 0 or run.get("failure_markers") != 0 or
                run.get("run_url") != f'https://github.com/brinnatt/kubeauto-ext-images-dockerfile/actions/runs/{run.get("run_id")}'):
            raise ValueError("publication run is not successful")
    result = {}
    for row in publication["images"]:
        key = row["key"]
        if (key in result or row.get("dockerhub_ref") != key or row.get("talkedu_ref") != "hub.talkedu.cn/kubeauto/" + key.removeprefix("brinnatt/") or
                set(row.get("platforms", {})) != {"linux/amd64", "linux/arm64"} or
                not re.fullmatch(r"[a-f0-9]{40}", row.get("head", "")) or
                not re.fullmatch(r"[a-f0-9]{64}", row.get("fingerprint", "")) or
                any(not re.fullmatch(r"sha256:[a-f0-9]{64}", d) for d in [row.get("digest", ""), *row.get("platforms", {}).values()])):
            raise ValueError("publication image identity is invalid")
        result[key] = row
    if set(result) != inventory():
        raise ValueError("publication does not cover the exact consumed inventory: " + ",".join(sorted(inventory() - set(result))))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, default=ROOT / "tests/evidence/elasticsearch-images-20261103.json")
    args = parser.parse_args()
    data = json.loads(args.publication.read_text())
    images = records(data)
    spec = importlib.util.spec_from_file_location("catalog_identity", ROOT / "tests/helpers/longhorn-artifacts.py")
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    rows = {r["image"] + ":" + r["tag"]: r for r in json.loads((identity.REPO / "catalog.json").read_text())}
    for key, image in images.items():
        if image["fingerprint"] != identity.fingerprint(rows[key], image["head"]):
            raise ValueError("artifact build input differs: " + key)
    spec=importlib.util.spec_from_file_location('es_offline_eck',ROOT/'roles/cluster-addon/files/eck.py')
    offline=importlib.util.module_from_spec(spec);spec.loader.exec_module(offline)
    offline.manifests()
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(identity.read_manifest, row["talkedu_ref"]): row for row in images.values()}
        for task, image in pending.items():
            digest, platforms = task.result()
            if (digest, platforms) != (image["digest"], image["platforms"]):
                raise ValueError("current China manifest differs: " + image["key"])
            print("ELASTICSEARCH_ARTIFACT_VERIFIED image=" + image["key"] + " digest=" + digest, flush=True)
    print("ELASTICSEARCH_ARTIFACTS_PASS count=" + str(len(images)) + " publication_sha256=" + hashlib.sha256(args.publication.read_bytes()).hexdigest())
    for case in ('ES-05','ES-06'):print('ELASTICSEARCH_CASE_PASS id='+case)
    print('ELASTICSEARCH_ARTIFACTS_SCOPED_CLEAN_PASS registry_mutation=false offline_manifests=verified')


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("ELASTICSEARCH_ARTIFACTS_REFUSED class=supply-chain reason=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)

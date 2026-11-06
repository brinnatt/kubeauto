#!/usr/bin/env python3
"""One shared, pinned ECK installation for independent kubeauto consumers.

The official manifests are vendored with the logging branch. Never apply the
upstream image and subsequently set-image: that produces needless rollouts.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import time

import yaml


ROOT = Path(__file__).resolve().parent
VERSION = "3.5.0"
PINS = {
    "crds": "0e126dbd003f8f98c9b84f2af6263b5ac8a00b52cd9a6d6da225aa3af66cc13c",
    "operator": "450f59d5026341226c54bbd3fafb68adcf99de8c674b47e32c22fae8aa183bf7",
}
LABELS = {"app.kubernetes.io/managed-by": "kubeauto", "kubeauto.io/component": "eck"}


class API:
    def __init__(self, kubeconfig):
        self.command = ["kubectl", "--kubeconfig=" + kubeconfig]

    def run(self, *args, body=None, timeout=180):
        result = subprocess.run(self.command + list(args), input=body, text=True,
                                capture_output=True, timeout=timeout)
        if result.returncode:
            # Kubernetes errors can contain admission details; no Secret bodies
            # or client credentials are ever sent through this helper.
            raise ValueError("ECK Kubernetes command failed: " + result.stderr.strip())
        return result.stdout

    def get(self, kind, name="", namespace=""):
        args = (["-n", namespace] if namespace else []) + ["get", kind]
        args += [name] if name else ["--all-namespaces"] if kind != "crd" else []
        raw = self.run(*args, "--ignore-not-found", "-o", "json", "--show-managed-fields=true")
        return json.loads(raw) if raw.strip() else None if name else {"items": []}

    def apply(self, objects, manager):
        return self.run("apply", "--server-side", "--field-manager=" + manager,
                        "-f", "-", body=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))


def manifests():
    result = {}
    for kind, checksum in PINS.items():
        path = ROOT / f"eck-{VERSION}-{kind}.yaml"
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError("ECK official manifest checksum differs: " + path.name)
        result[kind] = [o for o in yaml.safe_load_all(path.read_text()) if o]
    return result


def consumers(api):
    """Enumerate every installed Elastic resource, not just ES and Kibana.

    Failed discovery/list permissions must fail closed before any deletion.
    Deleting consumers remain consumers until their finalizers have completed.
    """
    found = []
    for crd in api.get("crd").get("items", []):
        spec = crd["spec"]
        if not spec["group"].endswith(".k8s.elastic.co"):
            continue
        resource = spec["names"]["plural"] + "." + spec["group"]
        for obj in (api.get(resource) or {}).get("items", []):
            meta = obj["metadata"]
            found.append({"kind": obj["kind"], "namespace": meta.get("namespace"),
                          "name": meta["name"], "uid": meta["uid"]})
    return found


def owned(obj):
    labels = obj.get("metadata", {}).get("labels", {})
    if all(labels.get(k) == v for k, v in LABELS.items()):
        return True
    # Delivered logging installations predate the shared label. Prove the
    # existing product field manager instead of adopting a similarly named ECK.
    return any(m.get("manager") == "kubeauto-eck-operator"
               for m in obj.get("metadata", {}).get("managedFields", []))


def consumer_preflight(api, namespace, component, secret=""):
    """Refuse cross-product namespace adoption and shared S3 credentials.

    Check the actual namespace and secureSettings of installed consumers;
    references with different Secret names do not establish isolation.
    """
    expected = "logging" if component == "logging-efk" else "elasticsearch"
    obj = api.get("namespace", namespace)
    if obj:
        labels = obj["metadata"].get("labels", {})
        if (labels.get("app.kubernetes.io/managed-by") != "kubeauto" or
                labels.get("kubeauto.io/component") != expected):
            raise ValueError("consumer namespace belongs to another product")
    current = api.get("secret", secret, namespace) if secret else None
    keys = {"s3.client.default.access_key", "s3.client.default.secret_key"}
    if secret and (not current or not keys <= current.get("data", {}).keys()):
        raise ValueError("S3 credentials must exist before consumer mutation")
    credentials = {key: current["data"][key] for key in keys} if current else None
    for row in consumers(api):
        if row["kind"] != "Elasticsearch":
            continue
        other = api.get("elasticsearch", row["name"], row["namespace"])
        if not other or other["metadata"]["uid"] != row["uid"]:
            raise ValueError("Elastic consumer changed during isolation preflight")
        labels = other["metadata"].get("labels", {})
        other_component = labels.get("kubeauto.io/component")
        if row["namespace"] == namespace and other_component != component:
            raise ValueError("consumer namespace contains another product's Elasticsearch")
        if not credentials or other_component == component:
            continue
        for reference in other.get("spec", {}).get("secureSettings", []):
            source = api.get("secret", reference["secretName"], row["namespace"])
            if not source:
                raise ValueError("cannot prove another consumer's S3 credential isolation")
            values = source.get("data", {})
            if keys <= values.keys() and values["s3.client.default.access_key"] == credentials["s3.client.default.access_key"]:
                raise ValueError("EFK and business Elasticsearch require distinct S3 access keys")


def upgrade_consumers(api):
    """An unknown kind, owner or version blocks a platform-wide upgrade."""
    rows = consumers(api)
    for row in rows:
        if row["kind"] not in ("Elasticsearch", "Kibana"):
            raise ValueError("unqualified Elastic consumer blocks the platform upgrade")
        obj = api.get(row["kind"].lower(), row["name"], row["namespace"])
        labels = (obj or {}).get("metadata", {}).get("labels", {})
        versions = {"logging-efk": {"9.5.1"}, "elasticsearch": {"9.5.5", "9.5.6"}}
        if (not obj or obj["metadata"]["uid"] != row["uid"] or
                labels.get("app.kubernetes.io/managed-by") != "kubeauto" or
                obj.get("spec", {}).get("version") not in versions.get(labels.get("kubeauto.io/component"), set()) or
                obj["metadata"].get("deletionTimestamp")):
            raise ValueError("unqualified Elastic consumer blocks the platform upgrade")
        row["spec_sha256"] = hashlib.sha256(json.dumps(obj["spec"], sort_keys=True).encode()).hexdigest()
    return rows


def consumer_ready(obj, kind):
    if not obj:
        return False
    annotations = obj["metadata"].get("annotations", {})
    if (annotations.get("eck.k8s.elastic.co/managed") == "false" or
            any(annotations.get(key) == "true" for key in
                ("common.k8s.elastic.co/pause", "eck.k8s.elastic.co/pause-orchestration"))):
        return False
    status = obj.get("status", {})
    ready = (obj["metadata"].get("generation") is not None and
             status.get("observedGeneration") == obj["metadata"]["generation"] and
             status.get("health") == "green")
    if kind == "Elasticsearch":
        ready = ready and status.get("availableNodes") == sum(group["count"] for group in obj["spec"]["nodeSets"])
        ready = ready and status.get("phase") == "Ready"
    return ready


def reconciled_consumers(api, identities, operator_uid, image):
    """Fence old healthy status with completed runs from the target process.

    ECK 3.5.0's pinned manifest has no readiness probe. Its Pod can be Ready
    before leader election and initial reconciliation (cmd/manager/main.go).
    Both pinned resource controllers log their run's end after updating status.
    Read only that process's logs; never annotate or patch a consumer to force
    a new generation. Missing/truncated logs fail closed within the normal wait.
    """
    def process(pod):
        if (not pod or pod["metadata"].get("deletionTimestamp") or
                not any(owner.get("uid") == operator_uid for owner in pod["metadata"].get("ownerReferences", [])) or
                not any(c.get("name") == "manager" and c.get("image") == image
                        for c in pod.get("spec", {}).get("containers", []))):
            return None
        manager = next((c for c in pod.get("status", {}).get("containerStatuses", [])
                        if c.get("name") == "manager"), {})
        if not manager.get("ready") or not manager.get("containerID"):
            return None
        return pod["metadata"]["uid"], manager["containerID"], manager.get("restartCount", 0)

    before = process(api.get("pod", "elastic-operator-0", "elastic-system"))
    if before is None:
        return set()
    raw = api.run("-n", "elastic-system", "logs", "elastic-operator-0", "-c", "manager", "--tail=10000", timeout=60)
    started, rejected, completed = set(), set(), set()
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # The runtime can prepend non-JSON startup messages.
        if not isinstance(row, dict) or row.get("service.version", "").split("+", 1)[0] != VERSION:
            continue
        kind, field = {"elasticsearch-controller": ("Elasticsearch", "es_name"),
                       "kibana-controller": ("Kibana", "kibana_name")}.get(row.get("log.logger"), ("", ""))
        identity = kind, row.get("namespace"), row.get(field)
        key = identity + (row.get("iteration"),)
        if not kind or not key[-1]:
            continue
        if row.get("message") == "Starting reconciliation run": started.add(key)
        if row.get("log.level") == "error": rejected.add(key)
        if row.get("message") == "Ending reconciliation run" and key in started: completed.add(key)
    if process(api.get("pod", "elastic-operator-0", "elastic-system")) != before:
        return set()
    return {row["uid"] for row in identities if any(
        key[:3] == (row["kind"], row["namespace"], row["name"]) for key in completed - rejected)}


def wait_consumers(api, identities, *, operator_uid="", image=""):
    """Operator rollout alone is not a platform maintenance success exit."""
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        pending = 0
        reconciled = reconciled_consumers(api, identities, operator_uid, image) if operator_uid else {row["uid"] for row in identities}
        for row in identities:
            obj = api.get(row["kind"].lower(), row["name"], row["namespace"])
            if not obj or obj["metadata"].get("uid") != row["uid"]:
                raise ValueError("consumer identity changed during platform maintenance")
            if row["uid"] not in reconciled or not consumer_ready(obj, row["kind"]):
                pending += 1
        if not pending:
            return
        print("ECK_PLATFORM_WAIT_HEARTBEAT pending_consumers=" + str(pending), flush=True)
        time.sleep(10)
    raise ValueError("platform maintenance consumers did not recover within 1800 seconds")


def upgrade_record(api, namespace, image, change_id):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", change_id):
        raise ValueError("ECK upgrade requires a stable DNS change ID")
    controller = api.get("statefulset", "elastic-operator", "elastic-system")
    if not controller or not owned(controller) or not controller["metadata"].get("uid"):
        raise ValueError("ECK upgrade requires an existing owned Operator identity")
    identities = sorted(upgrade_consumers(api), key=lambda r: (r["kind"], r["namespace"] or "", r["name"], r["uid"]))
    inputs = {"from": "3.4.1", "to": VERSION, "image": image, "consumer_namespace": namespace,
              "operator_uid": controller["metadata"]["uid"], "consumers": identities}
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    name = "kubeauto-eck-change-" + change_id
    existing = api.get("configmap", name, "elastic-system")
    if not existing:
        for row in identities:
            obj = api.get(row["kind"].lower(), row["name"], row["namespace"])
            if not consumer_ready(obj, row["kind"]) or obj["metadata"].get("uid") != row["uid"]:
                raise ValueError("new platform maintenance requires every consumer to be healthy")
    source_image = image.rsplit(":", 1)[0] + ":3.4.1"
    if not existing and not any(c.get("name") == "manager" and c.get("image") == source_image
                                for c in controller["spec"]["template"]["spec"]["containers"]):
        raise ValueError("new ECK upgrade requires the declared 3.4.1 source image")
    if existing and (not owned(existing) or existing.get("data", {}).get("fingerprint") != fingerprint or
                     existing["data"].get("phase") not in ("running", "complete")):
        raise ValueError("ECK change ID is bound to different input")
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "elastic-system", "labels": LABELS},
            "data": {"fingerprint": fingerprint, "from": "3.4.1", "to": VERSION,
                     "inputs": json.dumps(inputs, sort_keys=True), "phase": existing["data"]["phase"] if existing else "running"}}


def verify_crds(api, target_schema=True):
    installed = {o["metadata"]["name"]: o for o in api.get("crd").get("items", [])}
    for expected in manifests()["crds"]:
        obj = installed.get(expected["metadata"]["name"])
        if not obj or not any(m.get("manager") == "kubeauto-eck-crd" for m in obj["metadata"].get("managedFields", [])):
            raise ValueError("shared ECK CRD ownership is unproven: " + expected["metadata"]["name"])
        if target_schema:
            current = {v["name"]: {key: v.get(key) for key in ("schema", "served", "storage")} for v in obj["spec"]["versions"]}
            pinned = {v["name"]: {key: v.get(key) for key in ("schema", "served", "storage")} for v in expected["spec"]["versions"]}
            if current != pinned:
                raise ValueError("shared ECK CRD schema differs from the pinned target: " + expected["metadata"]["name"])


def preflight(api, namespace, image, upgrade=False, change_id="", *, platform=False):
    if upgrade:
        upgrade_record(api, namespace, image, change_id)
    controllers = []
    for obj in api.get("deployments,statefulsets").get("items", []):
        containers = obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if (obj["metadata"]["name"] == "elastic-operator" or any("eck-operator" in c.get("image", "") or "--config=/conf/eck.yaml" in c.get("args", []) for c in containers)):
            controllers.append(obj)
    if len(controllers) > 1:
        raise ValueError("multiple ECK controllers; overlapping watches are forbidden")
    existing = api.get("statefulset", "elastic-operator", "elastic-system")
    if controllers and (not existing or controllers[0]["metadata"]["uid"] != existing["metadata"]["uid"]):
        raise ValueError("foreign ECK controller; automatic adoption is forbidden")
    ns = api.get("namespace", "elastic-system")
    if not existing:
        if ns and not owned(ns):
            raise ValueError("elastic-system exists without a proven kubeauto Operator")
        if consumers(api):
            raise ValueError("existing Elastic consumers without a proven shared Operator")
        # Do not seize foreign CRDs just because there is no running operator.
        for obj in api.get("crd").get("items", []):
            if obj["spec"]["group"].endswith(".k8s.elastic.co") and not any(
                    m.get("manager") == "kubeauto-eck-crd" for m in obj["metadata"].get("managedFields", [])):
                raise ValueError("foreign Elastic CRD: " + obj["metadata"]["name"])
        return False
    if not owned(existing):
        raise ValueError("ECK ownership is unproven")
    pod = existing["spec"]["template"]["spec"]
    manager = next((c for c in pod["containers"] if c["name"] == "manager"), None)
    accepted_images = {image}
    if upgrade:
        accepted_images.add(image.rsplit(":", 1)[0] + ":3.4.1")
    if (not manager or manager.get("image") not in accepted_images or
            pod.get("serviceAccountName") != "elastic-operator" or
            manager.get("args") != ["manager", "--config=/conf/eck.yaml"]):
        raise ValueError("shared ECK version, image or controller arguments differ; use the explicit upgrade path")
    verify_crds(api, target_schema=manager["image"] == image)
    config = api.get("configmap", "elastic-operator", "elastic-system")
    if not config:
        raise ValueError("shared ECK watch configuration is missing")
    values = yaml.safe_load(config["data"]["eck.yaml"])
    watched = values.get("namespaces", [])
    required_namespaces = {row["namespace"] for row in consumers(api)} if platform else {namespace}
    if (values.get("operator-namespace") != "elastic-system" or
            values.get("managed-namespace-selector") or
            values.get("namespace-selector") or
            values.get("restrict-watched-resources", False) or
            (watched and not required_namespaces <= set(watched))):
        raise ValueError("shared ECK does not watch this consumer under the Basic baseline")
    for env in manager.get("env", []):
        if env["name"] in ("NAMESPACES", "MANAGED_NAMESPACE_SELECTOR", "RESTRICT_WATCHED_RESOURCES"):
            raise ValueError("unmanaged ECK watch environment override")
    return True


def ensure(api, namespace, image, upgrade=False, change_id=""):
    source = manifests()
    reused = preflight(api, namespace, image, upgrade, change_id, platform=upgrade)
    previous = api.get("statefulset", "elastic-operator", "elastic-system") if reused else None
    upgrading = previous and previous["spec"]["template"]["spec"]["containers"][0]["image"] != image
    change = upgrade_record(api, namespace, image, change_id) if upgrade else None
    if upgrading and change["data"]["phase"] == "complete":
        raise ValueError("completed ECK upgrade no longer has its target Operator image")
    continuing = change is not None and change["data"]["phase"] == "running"
    if continuing:
        api.apply([change], "kubeauto-eck-operator")
    if not reused or upgrading or continuing:
        for objects in source.values():
            for obj in objects:
                obj["metadata"].setdefault("labels", {}).update(LABELS)
                if obj["kind"] == "StatefulSet":
                    obj["spec"]["template"]["spec"]["containers"][0]["image"] = image
        api.apply(source["crds"], "kubeauto-eck-crd")
        for crd in source["crds"]:
            api.run("wait", "--for=condition=Established", "crd/" + crd["metadata"]["name"], "--timeout=120s")
        api.apply(source["operator"], "kubeauto-eck-operator")
    api.run("-n", "elastic-system", "rollout", "status", "statefulset/elastic-operator", "--timeout=10m", timeout=660)
    if change is not None:
        # A rollout alone does not prove CRD/Operator agreement or preservation
        # of all consumers. Re-read the identities before completing the receipt.
        preflight(api, namespace, image, platform=True)
        inputs = json.loads(change["data"]["inputs"])
        wait_consumers(api, inputs["consumers"], operator_uid=inputs["operator_uid"], image=image)
        current = upgrade_record(api, namespace, image, change_id)
        if current["data"]["fingerprint"] != change["data"]["fingerprint"]:
            raise ValueError("ECK upgrade identities changed during convergence")
        change["data"]["phase"] = "complete"
        api.apply([change], "kubeauto-eck-operator")
        print("ECK_SHARED_UPGRADE_COMPLETE change_id=" + change_id)
    print("ECK_SHARED_READY version=" + VERSION + " reused=" + str(reused).lower())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "ensure", "upgrade", "consumers", "can-remove"])
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace")
    parser.add_argument("--consumer-component", choices=["logging-efk", "elasticsearch"])
    parser.add_argument("--s3-secret", default="")
    parser.add_argument("--upgrade-from", choices=["", "3.4.1"], default="")
    parser.add_argument("--change-id", default="")
    parser.add_argument("--image", default="registry.talkschool.cn:5000/brinnatt/eck-operator:3.5.0")
    args = parser.parse_args()
    if args.upgrade_from and not args.change_id:
        parser.error("an ECK upgrade requires a stable --change-id")
    if args.action == "upgrade" and not args.upgrade_from:
        parser.error("platform upgrade requires --upgrade-from")
    if args.action != "upgrade" and (args.upgrade_from or args.change_id):
        parser.error("consumer commands cannot upgrade shared ECK; use the platform upgrade entry")
    api = API(args.kubeconfig)
    if args.action == "consumers":
        print(json.dumps(consumers(api)))
    elif args.action == "can-remove":
        if consumers(api):
            raise ValueError("shared ECK still has consumers")
        existing = api.get("statefulset", "elastic-operator", "elastic-system")
        namespace = api.get("namespace", "elastic-system")
        if existing and not owned(existing):
            raise ValueError("shared ECK ownership is unproven")
        if namespace and not existing and not owned(namespace):
            raise ValueError("foreign elastic-system namespace")
        print("ECK_SHARED_REMOVAL_ALLOWED")
    elif not args.namespace:
        parser.error("--namespace is required")
    elif args.action in ("ensure", "upgrade"):
        if args.consumer_component:
            consumer_preflight(api, args.namespace, args.consumer_component, args.s3_secret)
        ensure(api, args.namespace, args.image, args.action == "upgrade", args.change_id)
    else:
        manifests()
        if args.consumer_component:
            consumer_preflight(api, args.namespace, args.consumer_component, args.s3_secret)
        preflight(api, args.namespace, args.image, bool(args.upgrade_from), args.change_id)
        print("ECK_SHARED_PREFLIGHT_PASS")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print("ECK_SHARED_REFUSED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

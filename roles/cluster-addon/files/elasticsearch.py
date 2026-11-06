#!/usr/bin/env python3
"""Business Elasticsearch declarations and guarded operations for ECK 3.5.

Configuration contains Secret references, never passwords. Elasticsearch owns
its shard and voting state; this entry point never rewrites StatefulSets.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit


LABELS = {"app.kubernetes.io/managed-by": "kubeauto", "kubeauto.io/component": "elasticsearch"}
VERSION = "9.5.6"
UPGRADE_SOURCE = "9.5.5"
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def flag(value):
    if value is True or isinstance(value, str) and value in ("yes", "true"):
        return True
    if value is False or isinstance(value, str) and value in ("no", "false"):
        return False
    raise ValueError("expected boolean or yes/no")


def quantity(value):
    # Match the pinned Kubernetes Quantity suffixes, including other tenants'
    # DecimalSI requests and the nano/micro CPU values emitted by its API.
    match = re.fullmatch(r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]|[eE][+-]?[0-9]+)?", str(value))
    if not match:
        raise ValueError("unsupported Kubernetes resource quantity")
    suffix = match[2]
    if suffix and len(suffix) > 1 and suffix[0] in "eE" and suffix != "Ei":
        result = float(str(value))
    else:
        scales = {None: 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3,
                  "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
                  "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
                  "Pi": 1024**5, "Ei": 1024**6}
        result = float(match[1]) * scales[suffix]
    if not math.isfinite(result):
        raise ValueError("non-finite Kubernetes resource quantity")
    return result


def data_count(c):
    """Zero follows the eligible host count; a positive count reserves spares."""
    return c["elasticsearch_data_count"] or len(c["elasticsearch_data_nodes"])


def member_count(c):
    return data_count(c) + len(c["elasticsearch_master_nodes"])


def validate(c):
    def require(ok, message):
        if not ok:
            raise ValueError(message)
    for key in ("namespace", "name"):
        value = c["elasticsearch_" + key]
        require(isinstance(value, str) and len(value) <= 40 and DNS.fullmatch(value), "invalid " + key)
    require(c["elasticsearch_namespace"] not in ("default", "kube-system", "elastic-system", "logging"),
            "business namespace must be dedicated")
    require(c["elasticsearch_topology"] in ("standard", "compact"), "unsupported topology")
    require(c["elasticsearch_ver"] in (UPGRADE_SOURCE, VERSION), "version is outside the pinned upgrade pair")
    require(c["elasticsearch_eck_ver"] == "3.5.0", "production ECK must be 3.5.0")
    require(not c["elasticsearch_eck_upgrade_from"] and not c["elasticsearch_eck_upgrade_change_id"],
            "business configuration cannot upgrade shared ECK; use the platform maintenance entry")
    for key in ("kibana_enabled", "smartcn_enabled", "exporter_enabled", "network_policy_enabled", "manage_sysctl", "tolerate_control_plane"):
        flag(c["elasticsearch_" + key])
    require(c["elasticsearch_storage_class"] and DNS.fullmatch(c["elasticsearch_storage_class"]),
            "an explicit filesystem RWO StorageClass is required")
    for group in ("master", "data"):
        require(re.fullmatch(r"[1-9][0-9]*(Gi|Ti)", c[f"elasticsearch_{group}_pvc_size"]), "invalid PVC size")
        memory = quantity(c[f"elasticsearch_{group}_memory"])
        require(memory >= 1024**3 and quantity(c[f"elasticsearch_{group}_cpu"]) > 0, "invalid resources")
    require(re.fullmatch(r"[1-9][0-9]*[mg]", c["elasticsearch_data_heap"]), "invalid data heap")
    heap = int(c["elasticsearch_data_heap"][:-1]) * (1024**3 if c["elasticsearch_data_heap"][-1] == "g" else 1024**2)
    require(heap <= quantity(c["elasticsearch_data_memory"]) / 2, "data heap must not exceed half of Pod memory")
    nodes = c["elasticsearch_data_nodes"]
    require(isinstance(nodes, list) and len(nodes) >= 3 and all(isinstance(n, str) for n in nodes) and len(set(nodes)) == len(nodes), "at least three distinct data nodes required")
    require(all(isinstance(n, str) and len(n) <= 253 and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", n) for n in nodes), "invalid data node")
    count = c["elasticsearch_data_count"]
    require(type(count) is int and (count == 0 or 3 <= count <= len(nodes)), "data count must be zero or at least three within eligible hosts")
    if c["elasticsearch_topology"] == "compact":
        require(data_count(c) == 3, "compact requires exactly three mixed members")
        require(not c["elasticsearch_master_nodes"], "compact must not declare a separate master set")
    else:
        masters = c["elasticsearch_master_nodes"]
        require(isinstance(masters, list) and len(masters) == 3 and all(isinstance(n, str) for n in masters) and len(set(masters)) == 3, "standard requires three dedicated masters")
        require(all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", n) for n in masters), "invalid master node")
        require(not set(masters) & set(nodes), "standard master/data hosts must have separate failure budgets")
    for key in ("http_tls_secret", "s3_secret", "s3_ca_configmap", "exporter_secret", "admin_secret", "file_realm_secret"):
        value = c["elasticsearch_" + key]
        require(not value or DNS.fullmatch(value), "invalid Secret/ConfigMap reference")
    require(not flag(c["elasticsearch_exporter_enabled"]) or c["elasticsearch_exporter_secret"], "exporter requires a least-privilege credentials Secret")
    require(not c["elasticsearch_s3_ca_configmap"] or c["elasticsearch_s3_secret"], "S3 CA requires secureSettings credentials")
    require(not flag(c["elasticsearch_smartcn_enabled"]) or c["elasticsearch_ver"] == VERSION, "SmartCN is pinned to 9.5.6")
    require(re.fullmatch(r"[a-z_][a-z0-9_-]*", c["elasticsearch_node_ssh_user"]), "invalid SSH account")
    require(c["elasticsearch_state"] in ("present", "absent"), "invalid desired state")
    require(c["elasticsearch_repository_role"] in ("writer", "reader"), "repository role must be writer or reader")
    flag(c.get("elasticsearch_repository_analysis_copy_during_write_contention", True))
    require(60 <= c["elasticsearch_wait_seconds"] <= 3600, "wait must be 60..3600 seconds")
    require(isinstance(c["elasticsearch_requests"], list), "requests must be a list")
    for key in ("monitoring_namespace", "monitoring_release"):
        require(isinstance(c["elasticsearch_" + key], str) and DNS.fullmatch(c["elasticsearch_" + key]), "invalid monitoring reference")
    require(c["elasticsearch_state"] != "absent" or not c["elasticsearch_requests"], "uninstall must not carry operation requests")
    seen = set()
    for req in c["elasticsearch_requests"]:
        validate_request(c, req)
        require(req["id"] not in seen, "duplicate change ID")
        seen.add(req["id"])
    return c


OPERATION_FIELDS = {
    "repository": {"name", "endpoint", "bucket", "base_path", "region", "path_style_access"},
    "snapshot": {"repository", "snapshot", "indices", "include_global_state", "feature_states"},
    "restore": {"repository", "snapshot", "indices", "feature_states"},
    "slm": {"name", "policy"}, "role": {"name", "definition"},
    "user": {"name", "password_secret", "roles"}, "reload-secure-settings": set(),
}


def validate_request(c, req):
    """Validate the complete input before sysctl, ECK or business mutations."""
    def require(ok, message):
        if not ok:
            raise ValueError(message)
    require(isinstance(req, dict), "operation must be an object")
    require(isinstance(req.get("id"), str) and len(req["id"]) <= 40 and DNS.fullmatch(req["id"]), "operation needs a stable DNS change ID")
    kind = req.get("type")
    require(kind in OPERATION_FIELDS, "unsupported operation type")
    require(set(req) <= OPERATION_FIELDS[kind] | {"id", "type"}, "unsupported operation field")
    def scan(value):
        if isinstance(value, dict):
            require(not any(str(k).lower() in {"password", "access_key", "secret_key", "api_key", "authorization"} for k in value), "operation credentials must reference a Secret")
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
    scan(req)
    for key in ("name", "repository", "snapshot"):
        if key in OPERATION_FIELDS[kind]:
            require(isinstance(req.get(key), str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", req[key]), "invalid operation identifier: " + key)
    if kind == "repository":
        endpoint = urlsplit(req.get("endpoint", ""))
        require(endpoint.scheme == "https" and endpoint.hostname and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment and endpoint.path in ("", "/"), "S3 requires an HTTPS endpoint without inline credentials")
        require(all(isinstance(req.get(k), str) and req[k] for k in ("bucket", "base_path", "region")), "S3 bucket, region and unique base_path are required")
        require(not req["base_path"].startswith("/") and all(part not in ("", ".", "..") for part in req["base_path"].split("/")), "invalid repository base_path")
        path = req["base_path"].split("/")
        reserved = ["elasticsearch", "logging"]
        require(path[:len(reserved)] != reserved and reserved[:len(path)] != path,
                "repository path overlaps the reserved EFK backup tree")
        flag(req.get("path_style_access", False))
        require(c["elasticsearch_s3_secret"], "S3 repository requires secureSettings credentials")
    if kind in ("snapshot", "restore"):
        indices = req.get("indices")
        require(isinstance(indices, list) and indices and len(indices) == len(set(indices)) and all(isinstance(i, str) and len(i) <= 255 and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", i) for i in indices), "explicit non-wildcard index list required")
        features = req.get("feature_states", ["none"])
        require(isinstance(features, list) and features and all(isinstance(f, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", f) for f in features), "invalid feature_states")
        require(len(features) == len(set(features)) and ("none" not in features or features == ["none"]), "none must be the only feature state and feature names must be unique")
        require("none" not in features or features == ["none"], "none cannot be mixed with feature states")
        if kind == "snapshot":
            flag(req.get("include_global_state", False))
        else:
            require(c["elasticsearch_repository_role"] == "reader", "restore requires an isolated readonly target")
            require("security" not in features or c["elasticsearch_file_realm_secret"] and c["elasticsearch_admin_secret"], "security restore requires an independent file-realm administrator")
    if kind in ("snapshot", "slm"):
        require(c["elasticsearch_repository_role"] == "writer", "readonly target cannot write snapshots or enable SLM")
    if kind == "slm":
        policy = req.get("policy")
        require(isinstance(policy, dict) and set(policy) == {"schedule", "name", "repository", "config", "retention"}, "SLM policy needs schedule, name, repository, config and retention")
        require(all(isinstance(policy[k], str) and policy[k] for k in ("schedule", "name", "repository")), "invalid SLM schedule, name or repository")
        require(isinstance(policy["config"], dict) and isinstance(policy["retention"], dict) and policy["retention"], "SLM config and retention required")
        require(set(policy["config"]) <= {"indices", "include_global_state", "feature_states"}, "unsupported SLM config field")
        retention = policy["retention"]
        require(set(retention) <= {"expire_after", "min_count", "max_count"}, "unsupported SLM retention field")
        for key in ("min_count", "max_count"):
            if key in retention:
                require(type(retention[key]) is int and retention[key] >= (1 if key == "max_count" else 0), "invalid SLM retention count")
        require(retention.get("min_count", 0) <= retention.get("max_count", float("inf")), "SLM min_count exceeds max_count")
        if "expire_after" in retention:
            require(isinstance(retention["expire_after"], str) and re.fullmatch(r"[1-9][0-9]*(ms|s|m|h|d)", retention["expire_after"]), "invalid SLM retention duration")
        # Apply the same index, feature-state and boolean boundaries to SLM.
        validate_request(c, {"id": req["id"], "type": "snapshot", "repository": policy["repository"], "snapshot": "slm-validation", **policy["config"]})
    if kind == "role":
        require(isinstance(req.get("definition"), dict) and req["definition"], "role definition required")
    if kind == "user":
        require(isinstance(req.get("password_secret"), str) and DNS.fullmatch(req["password_secret"]), "password Secret reference required")
        require(isinstance(req.get("roles"), list) and req["roles"] and all(isinstance(r, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", r) for r in req["roles"]), "explicit roles required")


class API:
    def __init__(self, kubeconfig, *, field_manager="kubeauto-elasticsearch"):
        self.command = ["kubectl", "--kubeconfig=" + kubeconfig]
        self.field_manager = field_manager

    def run(self, *args, body=None, timeout=120):
        result = subprocess.run(self.command + list(args), input=body, text=True,
                                capture_output=True, timeout=timeout)
        if result.returncode:
            raise ValueError("Kubernetes operation failed: " + " ".join(args[:3]) + ": " + result.stderr.strip())
        return result.stdout

    def get(self, kind, name="", namespace=""):
        args = (["-n", namespace] if namespace else []) + ["get", kind]
        if name:
            args.append(name)
        elif not namespace and kind not in ("crd", "nodes", "node", "pv", "storageclass", "namespace", "namespaces"):
            args.append("--all-namespaces")
        raw = self.run(*args, "--ignore-not-found", "-o", "json")
        return json.loads(raw) if raw.strip() else None if name else {"items": []}

    def apply(self, objects):
        return self.run("apply", "--server-side", "--field-manager=" + self.field_manager, "-f", "-",
                        body=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))


def converge(c, api, manifest, *, dry_run):
    """Validate first; update ECK's atomic nodeSets with optimistic locking.

    ECK's typed annotation updates can normalize podTemplate and take ownership
    of this entire atomic list. SSA must not force unrelated or foreign fields.
    """
    desired = json.loads(manifest.read_text())
    clusters = [obj for obj in desired['items'] if obj['kind'] == 'Elasticsearch']
    if len(clusters) != 1 or clusters[0]['metadata'] != meta(c, c['elasticsearch_name']):
        raise ValueError('rendered Elasticsearch identity differs from configuration')
    cluster = clusters[0]
    ns, name = c['elasticsearch_namespace'], c['elasticsearch_name']
    raw = api.run('-n', ns, 'get', 'elasticsearch', name, '--ignore-not-found', '-o', 'json', '--show-managed-fields')
    live = json.loads(raw) if raw.strip() else None
    patch = None
    probe = json.loads(json.dumps(desired))
    if live:
        if not owned_resource(c, live):
            raise ValueError('foreign Elasticsearch resource')
        if live['spec']['nodeSets'] != cluster['spec']['nodeSets']:
            owners = [entry for entry in live['metadata'].get('managedFields', [])
                      if entry.get('fieldsV1', {}).get('f:spec') and not entry.get('subresource')]
            if not any('f:nodeSets' in entry['fieldsV1']['f:spec'] for entry in owners) or any(
                    entry.get('manager') not in ('kubeauto-elasticsearch', 'elastic-operator') for entry in owners):
                raise ValueError('atomic nodeSets has unproven or foreign field ownership')
            # Keep controller-only defaults. Omitted fields previously owned by
            # this product are removed, as in SSA. Couple version/image changes
            # with nodeSets in one CR update, never an intermediate rollout.
            replacement = dict(live['spec'])
            for entry in owners:
                if entry['manager'] == 'kubeauto-elasticsearch':
                    for field in entry['fieldsV1']['f:spec']:
                        if field.startswith('f:') and field[2:] not in cluster['spec']:
                            replacement.pop(field[2:], None)
            replacement.update(cluster['spec'])
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': live['metadata']['uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': live['metadata']['resourceVersion']},
                     {'op': 'test', 'path': '/spec', 'value': live['spec']},
                     {'op': 'replace', 'path': '/spec', 'value': replacement}]
            next(obj for obj in probe['items'] if obj['kind'] == 'Elasticsearch')['spec']['nodeSets'] = live['spec']['nodeSets']
    apply = ('apply', '--server-side', '--field-manager=kubeauto-elasticsearch', '-f', '-')
    # No writes precede both independent server validations. A foreign owner on
    # another field still causes the normal SSA validation to refuse the change.
    api.run(*apply, '--dry-run=server', body=json.dumps(probe))
    if patch:
        command = ('-n', ns, 'patch', 'elasticsearch', name, '--type=json',
                   '--field-manager=kubeauto-elasticsearch', '-p', json.dumps(patch))
        api.run(*command, '--dry-run=server')
    if dry_run:
        return
    if patch:
        api.run(*command)
    api.run(*apply, '--dry-run=none', body=json.dumps(desired))


def owned(obj):
    return all(obj.get("metadata", {}).get("labels", {}).get(k) == v for k, v in LABELS.items())


def verify_http_certificate(c, secret):
    """Check the supplied chain, service SANs and key before changing ECK."""
    with tempfile.TemporaryDirectory(prefix="kubeauto-es-certificate-") as directory:
        files = {}
        for key in ("tls.crt", "tls.key", "ca.crt"):
            path = Path(directory) / key
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(base64.b64decode(secret["data"][key], validate=True))
            files[key] = str(path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(files["tls.crt"], files["tls.key"])
        for service in (c["elasticsearch_name"] + "-client", c["elasticsearch_name"] + "-es-http"):
            hostname = service + "." + c["elasticsearch_namespace"] + ".svc"
            result = subprocess.run(["openssl", "verify", "-CAfile", files["ca.crt"], "-untrusted", files["tls.crt"],
                                     "-verify_hostname", hostname, files["tls.crt"]], capture_output=True, timeout=15)
            if result.returncode:
                raise ValueError("HTTP certificate chain, validity or service SAN validation failed")


def instance(c):
    return hashlib.sha256((c["elasticsearch_namespace"] + "/" + c["elasticsearch_name"]).encode()).hexdigest()[:16]


def owned_resource(c, obj):
    return owned(obj) and obj.get("metadata", {}).get("labels", {}).get("kubeauto.io/elasticsearch-instance") == instance(c)


def auxiliary(c):
    ns, name = c["elasticsearch_namespace"], c["elasticsearch_name"]
    yield "service", name + "-client", ns
    yield "deployment", name + "-metrics", ns
    yield "service", name + "-metrics", ns
    yield "networkpolicy", name + "-access", ns
    yield "kibana", name, ns
    for kind in ("servicemonitor", "prometheusrule"):
        yield kind, ns + "-" + name + "-metrics", c["elasticsearch_monitoring_namespace"]


def existing_auxiliary(c, api):
    crds = {"kibana": "kibanas.kibana.k8s.elastic.co", "servicemonitor": "servicemonitors.monitoring.coreos.com", "prometheusrule": "prometheusrules.monitoring.coreos.com"}
    for kind, name, ns in auxiliary(c):
        if kind in crds and not api.get("crd", crds[kind]):
            continue
        obj = api.get(kind, name, ns)
        if obj:
            if not owned_resource(c, obj):
                raise ValueError("foreign product resource: " + kind + "/" + name)
            yield kind, name, ns


def delete_owned(c, api, candidates):
    # Validate every deletion before removing the first resource.
    pending = []
    for kind, name, ns in list(existing_auxiliary(c, api)):
        if (kind, name, ns) in candidates:
            obj = api.get(kind, name, ns)
            if not obj or not owned_resource(c, obj):
                raise ValueError("resource changed before retirement")
            retirement_identity(obj)
            pending.append((kind, name, ns, obj))
    for kind, name, ns, obj in pending:
        delete_checked(api, kind, name, ns, obj)


def retirement_identity(obj):
    metadata = obj["metadata"]
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise ValueError("retirement requires the current UID and resourceVersion")
    return {key: metadata[key] for key in ("uid", "resourceVersion")}


def delete_checked(api, kind, name, namespace, obj):
    """The API server must reject a replacement or concurrent object change."""
    routes = {
        "elasticsearch": ("/apis/elasticsearch.k8s.elastic.co/v1", "elasticsearches"),
        "kibana": ("/apis/kibana.k8s.elastic.co/v1", "kibanas"),
        "deployment": ("/apis/apps/v1", "deployments"),
        "networkpolicy": ("/apis/networking.k8s.io/v1", "networkpolicies"),
        "servicemonitor": ("/apis/monitoring.coreos.com/v1", "servicemonitors"),
        "prometheusrule": ("/apis/monitoring.coreos.com/v1", "prometheusrules"),
        "service": ("/api/v1", "services"),
    }
    prefix, plural = routes[kind]
    path = prefix + "/namespaces/" + namespace + "/" + plural + "/" + name
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": retirement_identity(obj)}
    api.run("delete", "--raw", path, "-f", "-", body=json.dumps(options))
    api.run("-n", namespace, "wait", "--for=delete", kind + "/" + name, "--timeout=10m", timeout=660)


def preflight(c, api):
    ns, name = c["elasticsearch_namespace"], c["elasticsearch_name"]
    namespace = api.get("namespace", ns)
    if namespace and not owned(namespace):
        raise ValueError("foreign business namespace; automatic adoption is forbidden")
    current = api.get("elasticsearch", name, ns) if api.get("crd", "elasticsearches.elasticsearch.k8s.elastic.co") else None
    list(existing_auxiliary(c, api))
    if current:
        if not owned_resource(c, current):
            raise ValueError("foreign Elasticsearch CR")
        old = current["spec"]
        if tuple(map(int, old["version"].split("."))) > tuple(map(int, c["elasticsearch_ver"].split("."))):
            raise ValueError("in-place Elasticsearch downgrade is forbidden; restore a compatible snapshot into an empty old-version cluster")
        previous = {s["name"]: s for s in old["nodeSets"]}
        desired = next(o for o in resources(c) if o["kind"] == "Elasticsearch")["spec"]["nodeSets"]
        if set(previous) != {s["name"] for s in desired}:
            raise ValueError("in-place topology conversion is forbidden; use an independent migration target")
        for nodeset in desired:
            oldset = previous[nodeset["name"]]
            if nodeset["count"] < oldset["count"]:
                raise ValueError("scale-in deletes ECK PVCs; explicit evacuation and separately approved retirement required")
            claim = nodeset["volumeClaimTemplates"][0]["spec"]
            oldclaim = oldset["volumeClaimTemplates"][0]["spec"]
            if (claim["storageClassName"] != oldclaim["storageClassName"] or
                    quantity(claim["resources"]["requests"]["storage"]) < quantity(oldclaim["resources"]["requests"]["storage"])):
                raise ValueError("PVC class changes and shrink are forbidden")
    ledger = api.get("configmap", name + "-identity", ns) if namespace else None
    if ledger and not owned_resource(c, ledger):
        raise ValueError("foreign Elasticsearch identity ledger")
    retained = json.loads(ledger.get("data", {}).get("pvc_uids", "{}")) if ledger else {}
    if ledger:
        ceiling = ledger.get("data", {}).get("version_ceiling")
        if not ceiling or tuple(map(int, ceiling.split("."))) > tuple(map(int, c["elasticsearch_ver"].split("."))):
            raise ValueError("retained data forbids an in-place version downgrade")
        if retained and ledger["data"].get("topology") != c["elasticsearch_topology"]:
            raise ValueError("retained PVCs cannot be reused for topology conversion")
    sets = (current or {}).get("spec", {}).get("nodeSets", [])
    allowed_claims = {f"elasticsearch-data-{name}-es-{group['name']}-{i}" for group in sets for i in range(group["count"])}
    if namespace:
        sts = (api.get("statefulset", namespace=ns) or {}).get("items", [])
        owner_uids = {o["metadata"]["uid"] for o in sts if current and any(r.get("uid") == current["metadata"]["uid"] for r in o["metadata"].get("ownerReferences", []))}
        if current:
            owner_uids.add(current["metadata"]["uid"])
        live_claims = set()
        for pod in (api.get("pods", namespace=ns) or {}).get("items", []):
            if any(r.get("uid") in owner_uids for r in pod["metadata"].get("ownerReferences", [])):
                live_claims.update(v["persistentVolumeClaim"]["claimName"] for v in pod["spec"].get("volumes", []) if "persistentVolumeClaim" in v)
        for claim in (api.get("pvc", namespace=ns) or {}).get("items", []):
            m = claim["metadata"]
            live_owner = m["name"] in allowed_claims and (any(r.get("uid") in owner_uids for r in m.get("ownerReferences", [])) or m["name"] in live_claims)
            retained_owner = retained.get(m["name"]) == m["uid"]
            if m.get("labels", {}).get("elasticsearch.k8s.elastic.co/cluster-name") != name or not (live_owner or retained_owner):
                raise ValueError("PVC ownership is unproven; label-only adoption is forbidden")
            pv = api.get("pv", claim.get("spec", {}).get("volumeName", "")) if claim.get("spec", {}).get("volumeName") else None
            ref = (pv or {}).get("spec", {}).get("claimRef", {})
            if pv and (ref.get("uid") != m["uid"] or ref.get("namespace") != ns or ref.get("name") != m["name"]):
                raise ValueError("PVC/PV binding identity differs")
    for req in c["elasticsearch_requests"]:
        if req["type"] == "repository":
            reserve_repository(c, api, req, commit=False)
        record = api.get("configmap", name + "-request-" + req["id"], ns) if namespace else None
        fingerprint = hashlib.sha256(json.dumps(req, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if record and (not owned_resource(c, record) or record.get("data", {}).get("fingerprint") != fingerprint):
            raise ValueError("change ID is already bound to different input")
        if record and (not ledger or not ledger.get('data', {}).get('cluster_uuid') or
                       record.get('data', {}).get('cluster_uuid') != ledger['data']['cluster_uuid']):
            raise ValueError("request journal belongs to a different data cluster")
        if record and req["type"] == "restore" and record.get("data", {}).get("phase") == "running":
            raise ValueError("interrupted restore has an unknown result; inspect the target before any new change")
        if req["type"] == "user":
            secret = api.get("secret", req["password_secret"], ns)
            if not secret or not secret.get("data", {}).get("password"):
                raise ValueError("user password Secret is missing")
    if c["elasticsearch_state"] == "absent":
        return
    sc = api.get("storageclass", c["elasticsearch_storage_class"])
    if not sc or sc.get("provisioner") in ("kubernetes.io/no-provisioner", "rancher.io/local-path"):
        raise ValueError("a CSI filesystem StorageClass supporting recovery and expansion is required")
    if not sc.get("allowVolumeExpansion"):
        raise ValueError("StorageClass must explicitly support expansion")
    for secret, keys in ((c["elasticsearch_http_tls_secret"], {"tls.crt", "tls.key", "ca.crt"}),
                         (c["elasticsearch_s3_secret"], {"s3.client.default.access_key", "s3.client.default.secret_key"}),
                         (c["elasticsearch_exporter_secret"], {"username", "password"})):
        if secret:
            obj = api.get("secret", secret, ns)
            if not obj or not keys <= set(obj.get("data", {})):
                raise ValueError("missing referenced Secret keys: " + secret)
    if c["elasticsearch_http_tls_secret"]:
        verify_http_certificate(c, api.get("secret", c["elasticsearch_http_tls_secret"], ns))
    for key, required in (("admin_secret", {"username", "password"}), ("file_realm_secret", {"users", "users_roles"})):
        if c["elasticsearch_" + key]:
            obj = api.get("secret", c["elasticsearch_" + key], ns)
            if not obj or not required <= set(obj.get("data", {})):
                raise ValueError("missing referenced identity Secret keys")
    if flag(c["elasticsearch_exporter_enabled"]):
        for crd in ("servicemonitors.monitoring.coreos.com", "prometheusrules.monitoring.coreos.com"):
            if not api.get("crd", crd):
                raise ValueError("Prometheus Operator CRDs must be installed before enabling the exporter")
        if not api.get("namespace", c["elasticsearch_monitoring_namespace"]):
            raise ValueError("monitoring namespace must exist before enabling the exporter")
        secret = api.get("secret", c["elasticsearch_exporter_secret"], ns)
        user = base64.b64decode(secret["data"]["username"], validate=True).decode()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", user) or user == "elastic":
            raise ValueError("exporter requires a dedicated non-administrative username")
    if c["elasticsearch_s3_ca_configmap"]:
        obj = api.get("configmap", c["elasticsearch_s3_ca_configmap"], ns)
        if not obj or "ca.crt" not in obj.get("data", {}):
            raise ValueError("S3 CA ConfigMap must contain ca.crt")
    selected = c["elasticsearch_data_nodes"] + c["elasticsearch_master_nodes"]
    reserved = {node: {"cpu": 0, "memory": 0} for node in selected}
    for pod in (api.get("pods", namespace="") or {}).get("items", []):
        host = pod.get("spec", {}).get("nodeName")
        if host not in reserved or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        if pod["metadata"].get("namespace") == ns and pod["metadata"].get("labels", {}).get("elasticsearch.k8s.elastic.co/cluster-name") == name:
            continue
        for resource in reserved[host]:
            normal = sum(quantity(o.get("resources", {}).get("requests", {}).get(resource, 0)) for o in pod["spec"].get("containers", []))
            init = max((quantity(o.get("resources", {}).get("requests", {}).get(resource, 0)) for o in pod["spec"].get("initContainers", [])), default=0)
            reserved[host][resource] += max(normal, init) + quantity(pod["spec"].get("overhead", {}).get(resource, 0))
    for node_name in selected:
        node = api.get("node", node_name)
        if (not node or node.get("spec", {}).get("unschedulable") or
                not any(s["type"] == "Ready" and s["status"] == "True" for s in node.get("status", {}).get("conditions", []))):
            raise ValueError("selected node is missing, unready or cordoned: " + node_name)
        tolerated = {"node-role.kubernetes.io/master", "node-role.kubernetes.io/control-plane"} if flag(c["elasticsearch_tolerate_control_plane"]) else set()
        if any(t.get("effect") in ("NoSchedule", "NoExecute") and t.get("key") not in tolerated for t in node.get("spec", {}).get("taints", [])):
            raise ValueError("selected node has an untolerated scheduling taint: " + node_name)
        group = "data" if node_name in c["elasticsearch_data_nodes"] else "master"
        for resource in ("cpu", "memory"):
            if quantity(node["status"]["allocatable"][resource]) - reserved[node_name][resource] < quantity(c[f"elasticsearch_{group}_{resource}"]):
                raise ValueError("selected node allocatable below Pod request: " + node_name)


def meta(c, name, **extra):
    labels = dict(LABELS)
    labels["kubeauto.io/elasticsearch-instance"] = instance(c)
    return {"name": name, "namespace": c["elasticsearch_namespace"], "labels": labels, **extra}


def resources(c, ca_revision=""):
    ns, name, version = c["elasticsearch_namespace"], c["elasticsearch_name"], c["elasticsearch_ver"]
    prefix = "registry.talkschool.cn:5000/brinnatt/"
    image = prefix + ("elasticsearch-smartcn:9.5.6-kubeauto.1" if flag(c["elasticsearch_smartcn_enabled"]) else "elasticsearch:" + version)
    sets = []
    for group in (["master", "data"] if c["elasticsearch_topology"] == "standard" else ["data"]):
        nodes = c[f"elasticsearch_{group}_nodes"]
        roles = ["master"] if group == "master" else ["data", "ingest"]
        if c["elasticsearch_topology"] == "compact":
            roles.insert(0, "master")
        container = {"name": "elasticsearch", "resources": {
            "requests": {"cpu": c[f"elasticsearch_{group}_cpu"], "memory": c[f"elasticsearch_{group}_memory"]},
            "limits": {"memory": c[f"elasticsearch_{group}_memory"]}}}
        if group == "data":
            heap = c["elasticsearch_data_heap"]
            container["env"] = [{"name": "ES_JAVA_OPTS", "value": f"-Xms{heap} -Xmx{heap}"}]
        pod = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}
                                  for node in nodes]}},
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname",
                "labelSelector": {"matchLabels": {"elasticsearch.k8s.elastic.co/cluster-name": name,
                                                   "elasticsearch.k8s.elastic.co/statefulset-name": f"{name}-es-{group}"}}}]}},
            "containers": [container]}
        if flag(c["elasticsearch_tolerate_control_plane"]):
            pod["tolerations"] = [{"key": key, "operator": "Exists", "effect": "NoSchedule"} for key in
                                  ("node-role.kubernetes.io/master", "node-role.kubernetes.io/control-plane")]
        if c["elasticsearch_s3_ca_configmap"]:
            # Keep all default JVM roots, then add the customer's S3 CA. The
            # truststore is mounted into the ES JVM, not merely the client Job.
            pod["volumes"] = [{"name": "s3-ca", "configMap": {"name": c["elasticsearch_s3_ca_configmap"]}},
                              {"name": "s3-truststore", "emptyDir": {}}]
            pod["initContainers"] = [{"name": "s3-truststore", "image": image,
                "resources": {},
                "command": ["/bin/bash", "-ec", "cp --remove-destination /usr/share/elasticsearch/jdk/lib/security/cacerts /trust/cacerts; "
                    "chmod 0644 /trust/cacerts; "
                    "awk '/BEGIN CERTIFICATE/ {n++} n {print > (\"/trust/ca-\" n \".pem\")}' /ca/ca.crt; "
                    "n=0; for cert in /trust/ca-*.pem; do n=$((n+1)); "
                    "/usr/share/elasticsearch/jdk/bin/keytool -importcert -noprompt -alias kubeauto-s3-$n "
                    "-keystore /trust/cacerts -storepass changeit -file \"$cert\"; done"],
                "volumeMounts": [{"name": "s3-ca", "mountPath": "/ca", "readOnly": True}, {"name": "s3-truststore", "mountPath": "/trust"}]}]
            container["volumeMounts"] = [{"name": "s3-truststore", "mountPath": "/usr/share/elasticsearch/config/s3-trust", "readOnly": True}]
            opts = next((v for v in container.get("env", []) if v["name"] == "ES_JAVA_OPTS"), None)
            if not opts:
                opts = {"name": "ES_JAVA_OPTS", "value": ""}
                container.setdefault("env", []).append(opts)
            opts["value"] += " -Djavax.net.ssl.trustStore=/usr/share/elasticsearch/config/s3-trust/cacerts -Djavax.net.ssl.trustStorePassword=changeit"
        sets.append({"name": group, "count": data_count(c) if group == "data" else len(nodes), "config": {"node.roles": roles, "node.store.allow_mmap": True},
                     "podTemplate": {"metadata": {"labels": dict(LABELS), **({"annotations": {"kubeauto.io/s3-ca-revision": ca_revision}} if ca_revision else {})}, "spec": pod},
                     "volumeClaimTemplates": [{"metadata": {"name": "elasticsearch-data"}, "spec": {
                         "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem", "storageClassName": c["elasticsearch_storage_class"],
                         "resources": {"requests": {"storage": c[f"elasticsearch_{group}_pvc_size"]}}}}]})
        # ES 9.5.6's own MinIO tests disable this one unsupported contention
        # probe (elastic/elasticsearch#135565). Preserve upstream defaults
        # unless the operator explicitly declares that backend limitation.
        if not flag(c.get("elasticsearch_repository_analysis_copy_during_write_contention", True)):
            sets[-1]["config"]["repositories.blobstore.testkit.analyze.copy_during_write_contention"] = False
    spec = {"version": version, "image": image, "volumeClaimDeletePolicy": "DeleteOnScaledownOnly", "nodeSets": sets,
            "http": {"service": {"spec": {"type": "ClusterIP"}}, "tls": {"certificate": {"secretName": ""}}},
            "updateStrategy": {"changeBudget": {"maxSurge": 1, "maxUnavailable": 1}}}
    if c["elasticsearch_http_tls_secret"]:
        spec["http"]["tls"] = {"certificate": {"secretName": c["elasticsearch_http_tls_secret"]}}
    if c["elasticsearch_s3_secret"]:
        spec["secureSettings"] = [{"secretName": c["elasticsearch_s3_secret"]}]
    if c["elasticsearch_file_realm_secret"]:
        spec["auth"] = {"fileRealm": [{"secretName": c["elasticsearch_file_realm_secret"]}]}
    objects = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": dict(LABELS)}},
               {"apiVersion": "elasticsearch.k8s.elastic.co/v1", "kind": "Elasticsearch", "metadata": meta(c, name), "spec": spec},
               {"apiVersion": "v1", "kind": "Service", "metadata": meta(c, name + "-client"), "spec": {
                   "ports": [{"name": "https", "port": 9200, "targetPort": 9200}], "selector": {
                       "elasticsearch.k8s.elastic.co/cluster-name": name, "elasticsearch.k8s.elastic.co/node-data": "true"}}}]
    # The additional Service DNS is placed in the generated HTTP certificate.
    spec["http"].setdefault("tls", {}).setdefault("selfSignedCertificate", {})["subjectAltNames"] = [
        {"dns": f"{name}-client.{ns}.svc"}, {"dns": f"{name}-client.{ns}.svc.cluster.local"}]
    if c["elasticsearch_http_tls_secret"]:
        spec["http"]["tls"].pop("selfSignedCertificate", None)
    if flag(c["elasticsearch_kibana_enabled"]):
        objects.append({"apiVersion": "kibana.k8s.elastic.co/v1", "kind": "Kibana", "metadata": meta(c, name), "spec": {
            "version": version, "image": prefix + "kibana:" + version, "count": 2,
            "elasticsearchRef": {"name": name}, "podTemplate": {"metadata": {"labels": dict(LABELS)}, "spec": {
                "containers": [{"name": "kibana", "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "2Gi"}}}],
                "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                    "topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": {"kibana.k8s.elastic.co/name": name}}}]}}}}}})
    return objects


def monitoring(c):
    ns, name = c["elasticsearch_namespace"], c["elasticsearch_name"]
    labels = dict(LABELS, **{"app.kubernetes.io/name": name + "-metrics"})
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(c, name + "-metrics"), "spec": {
        "replicas": 1, "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": labels}, "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534},
            "containers": [{"name": "exporter", "image": "registry.talkschool.cn:5000/brinnatt/elasticsearch-exporter:v1.11.0",
                "args": [f"--es.uri=https://{name}-client.{ns}.svc:9200", "--es.ca=/ca/ca.crt", "--es.all", "--es.indices", "--no-es.aliases", "--es.timeout=20s"],
                "env": [{"name": "ES_" + key.upper(), "valueFrom": {"secretKeyRef": {"name": c["elasticsearch_exporter_secret"], "key": key}}} for key in ("username", "password")],
                "ports": [{"name": "metrics", "containerPort": 9114}],
                "readinessProbe": {"httpGet": {"path": "/healthz", "port": "metrics"}, "initialDelaySeconds": 5},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                "volumeMounts": [{"name": "ca", "mountPath": "/ca", "readOnly": True}]}],
            "volumes": [{"name": "ca", "secret": {"secretName": c["elasticsearch_http_tls_secret"] or name + "-es-http-certs-public", "items": [{"key": "ca.crt" if c["elasticsearch_http_tls_secret"] else "tls.crt", "path": "ca.crt"}]}}]}}}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": meta(c, name + "-metrics"), "spec": {
        "selector": labels, "ports": [{"name": "metrics", "port": 9114, "targetPort": "metrics"}]}}
    monitor_meta = {**meta(c, ns + "-" + name + "-metrics"), "namespace": c["elasticsearch_monitoring_namespace"]}
    monitor_meta["labels"]["release"] = c["elasticsearch_monitoring_release"]
    sm = {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor", "metadata": monitor_meta, "spec": {
        "namespaceSelector": {"matchNames": [ns]}, "selector": {"matchLabels": meta(c, name)["labels"]},
        "endpoints": [{"port": "metrics", "interval": "60s", "scrapeTimeout": "30s"}]}}
    selector = f'namespace="{ns}",service="{name}-metrics"'
    rule = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule", "metadata": monitor_meta, "spec": {"groups": [{
        "name": "kubeauto-elasticsearch-" + name, "rules": [
            {"alert": "ElasticsearchClusterRed", "expr": f'elasticsearch_cluster_health_status{{{selector},color="red"}} == 1', "for": "1m", "labels": {"severity": "critical"}, "annotations": {"summary": "Elasticsearch primary shards unavailable"}},
            {"alert": "ElasticsearchUnassignedShards", "expr": f'elasticsearch_cluster_health_unassigned_shards{{{selector}}} > 0', "for": "5m", "labels": {"severity": "warning"}, "annotations": {"summary": "Elasticsearch shard allocation incomplete"}},
            {"alert": "ElasticsearchMetricsUnavailable", "expr": f'(up{{{selector}}} == 0) or absent(up{{{selector}}}) or absent(elasticsearch_cluster_health_status{{{selector}}})', "for": "2m", "labels": {"severity": "critical"}, "annotations": {"summary": "Elasticsearch metrics unavailable or absent"}}]}]}}
    return [deployment, service, sm, rule]


def network_policy(c):
    ns, name = c["elasticsearch_namespace"], c["elasticsearch_name"]
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(c, name + "-access"), "spec": {
        "podSelector": {"matchLabels": LABELS}, "policyTypes": ["Ingress"], "ingress": [
            {"from": [{"podSelector": {"matchLabels": LABELS}}], "ports": [{"protocol": "TCP", "port": 9300}, {"protocol": "TCP", "port": 9200}, {"protocol": "TCP", "port": 5601}]},
            {"from": [{"namespaceSelector": {"matchLabels": {"kubeauto.io/elasticsearch-client": "true"}}},
                      {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "elastic-system"}}}], "ports": [{"protocol": "TCP", "port": 9200}, {"protocol": "TCP", "port": 5601}]},
            {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": c["elasticsearch_monitoring_namespace"]}}}], "ports": [{"protocol": "TCP", "port": 9114}]}]}}


def enable_monitoring(c, api):
    if not flag(c["elasticsearch_exporter_enabled"]):
        delete_owned(c, api, {(kind, name, ns) for kind, name, ns in auxiliary(c) if "metrics" in name})
        return
    secret = api.get("secret", c["elasticsearch_exporter_secret"], c["elasticsearch_namespace"])
    username = base64.b64decode(secret["data"]["username"]).decode()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", username) or username == "elastic":
        raise ValueError("exporter requires a dedicated non-administrative username")
    with connection(c, api) as request:
        request("PUT", "/_security/role/kubeauto-metrics", {"cluster": ["monitor"], "indices": [{"names": ["*"], "privileges": ["monitor"]}]})
        request("PUT", "/_security/user/" + quote(username, safe=""), {"password": base64.b64decode(secret["data"]["password"]).decode(), "roles": ["kubeauto-metrics"]})
    objects = monitoring(c)
    objects[0]["spec"]["template"]["metadata"]["annotations"] = {"kubeauto.io/credentials-revision": secret["metadata"]["uid"] + "/" + secret["metadata"]["resourceVersion"]}
    trust = api.get("secret", c["elasticsearch_http_tls_secret"] or c["elasticsearch_name"] + "-es-http-certs-public", c["elasticsearch_namespace"])
    if not trust:
        raise ValueError("exporter HTTP trust Secret is missing")
    objects[0]["spec"]["template"]["metadata"]["annotations"]["kubeauto.io/http-trust-revision"] = trust["metadata"]["uid"] + "/" + trust["metadata"]["resourceVersion"]
    api.apply(objects)
    api.run("-n", c["elasticsearch_namespace"], "rollout", "status", "deployment/" + c["elasticsearch_name"] + "-metrics", "--timeout=5m", timeout=330)
    print("ELASTICSEARCH_MONITORING_READY")


def sysctl(c, api):
    for name in c["elasticsearch_data_nodes"] + c["elasticsearch_master_nodes"]:
        node = api.get("node", name)
        ips = [a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP"]
        if len(ips) != 1:
            raise ValueError("selected node has ambiguous InternalIP")
        script = "set -eu\n"
        if flag(c["elasticsearch_manage_sysctl"]):
            script += ('tmp=$(mktemp /etc/sysctl.d/.kubeauto-elasticsearch.XXXXXX)\n'
                       'trap \'rm -f "$tmp"\' EXIT\n'
                       'current=$(sysctl -n vm.max_map_count)\n'
                       'target=1048576\n[ "$current" -le "$target" ] || target="$current"\n'
                       'saved=$(sed -n "s/^vm.max_map_count=\\([0-9][0-9]*\\)$/\\1/p" /etc/sysctl.d/99-kubeauto-elasticsearch.conf 2>/dev/null || true)\n'
                       'if [ -n "$saved" ] && [ "$saved" -gt "$target" ]; then target="$saved"; fi\n'
                       'printf "vm.max_map_count=%s\\n" "$target" > "$tmp"\n'
                       'chmod 0644 "$tmp"\nmv "$tmp" /etc/sysctl.d/99-kubeauto-elasticsearch.conf\n'
                       'current=$(sysctl -n vm.max_map_count)\n'
                       '[ "$current" -ge "$target" ] || sysctl -w "vm.max_map_count=$target" >/dev/null\n')
        script += 'test "$(sysctl -n vm.max_map_count)" -ge 1048576\n'
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", c["elasticsearch_node_ssh_user"] + "@" + ips[0]]
        command += ["bash", "-s"] if c["elasticsearch_node_ssh_user"] == "root" else ["sudo", "-n", "bash", "-s"]
        result = subprocess.run(command, input=script, text=True, capture_output=True, timeout=45)
        if result.returncode:
            raise ValueError("mmap precondition failed on selected node: " + name)
        print("ELASTICSEARCH_MMAP_READY node=" + name, flush=True)


class TunnelHTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(socket.create_connection(("127.0.0.1", self.port), self.timeout), server_hostname=self.host)


def api_error_types(raw):
    """Expose causal types for diagnosis; never relay arbitrary error reasons."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return ""
    pending = [(payload.get("error"), 0)] if isinstance(payload, dict) else []
    types = []
    while pending and len(types) < 8:
        error, depth = pending.pop(0)
        if not isinstance(error, dict) or depth > 4:
            continue
        kind = error.get("type", "")
        if isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", kind) and kind not in types:
            types.append(kind)
        pending.append((error.get("caused_by"), depth + 1))
        causes = error.get("root_cause", [])
        if isinstance(causes, list):
            pending.extend((cause, depth + 1) for cause in causes[:8])
    return " types=" + ",".join(types) if types else ""


@contextmanager
def connection(c, api):
    ns, name = c["elasticsearch_namespace"], c["elasticsearch_name"]
    cert = api.get("secret", c["elasticsearch_http_tls_secret"] or name + "-es-http-certs-public", ns)
    admin = api.get("secret", c["elasticsearch_admin_secret"] or name + "-es-elastic-user", ns)
    if not cert or not admin:
        raise ValueError("ECK HTTP CA or administrative identity is not ready")
    context = ssl.create_default_context(cadata=base64.b64decode(cert["data"].get("ca.crt", cert["data"]["tls.crt"])).decode())
    identity = (base64.b64decode(admin["data"]["username"]) + b":" + base64.b64decode(admin["data"]["password"]) if c["elasticsearch_admin_secret"]
                else b"elastic:" + base64.b64decode(admin["data"]["elastic"]))
    auth = base64.b64encode(identity).decode()
    # Port 0 is selected by kubectl, avoiding a check-then-bind race.
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(api.command + ["-n", ns, "port-forward", "service/" + name + "-client", ":9200", "--address=127.0.0.1"],
                                   stdout=log, stderr=log, text=True)
        try:
            port = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None:
                log.seek(0)
                match = re.search(r"Forwarding from 127\.0\.0\.1:([0-9]+)", log.read())
                if match:
                    port = int(match[1])
                    break
                time.sleep(.2)
            if not port:
                raise ValueError("business Service port-forward did not become ready")

            def request(method, path, body=None):
                conn = TunnelHTTPS(f"{name}-client.{ns}.svc", port, timeout=60, context=context)
                try:
                    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                                 headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
                    response = conn.getresponse()
                    data = response.read()
                    if response.status >= 300:
                        raise ValueError(f"Elasticsearch API refused {method} {path.split('?')[0]} status={response.status}" + api_error_types(data))
                    return json.loads(data) if data else {}
                finally:
                    conn.close()
            yield request
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def health(c, api):
    deadline = time.monotonic() + c["elasticsearch_wait_seconds"]
    expected = member_count(c)
    while time.monotonic() < deadline:
        obj = api.get("elasticsearch", c["elasticsearch_name"], c["elasticsearch_namespace"])
        status = obj.get("status", {}) if obj else {}
        observed = status.get("observedGeneration")
        reconciled = status.get("phase") == "Ready" and any(
            condition.get("type") == "ReconciliationComplete" and condition.get("status") == "True"
            for condition in status.get("conditions", []))
        ready = obj and observed == obj["metadata"]["generation"] and reconciled and status.get("health") == "green" and status.get("availableNodes") == expected
        if ready:
            with connection(c, api) as request:
                data = request("GET", "/_cluster/health?wait_for_status=green&timeout=30s")
                if data.get("timed_out") or data["status"] != "green" or data["number_of_nodes"] != expected:
                    print("ELASTICSEARCH_WAIT_HEARTBEAT source=API health=" + str(data.get("status")) + " nodes=" + str(data.get("number_of_nodes")), flush=True)
                    time.sleep(5)
                    continue
                info = request("GET", "/")
                members = request("GET", "/_nodes")['nodes']
                if (info["version"]["number"] != c["elasticsearch_ver"] or len(members) != expected or
                        any(node['version'] != c["elasticsearch_ver"] for node in members.values())):
                    print("ELASTICSEARCH_WAIT_HEARTBEAT source=API reason=member-version-convergence", flush=True)
                    time.sleep(5)
                    continue
            ledger = api.get("configmap", c["elasticsearch_name"] + "-identity", c["elasticsearch_namespace"])
            if ledger and ledger.get("data", {}).get("cluster_uuid") and ledger["data"]["cluster_uuid"] != info["cluster_uuid"]:
                raise ValueError("cluster UUID differs from the retained data identity")
            claims = (api.get("pvc", namespace=c["elasticsearch_namespace"]) or {}).get("items", [])
            api.apply([{"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(c, c["elasticsearch_name"] + "-identity"), "data": {
                "cluster_uuid": info["cluster_uuid"], "version_ceiling": c["elasticsearch_ver"], "topology": c["elasticsearch_topology"], "pvc_uids": json.dumps({o["metadata"]["name"]: o["metadata"]["uid"] for o in claims}, sort_keys=True)}}])
            if not flag(c["elasticsearch_kibana_enabled"]):
                delete_owned(c, api, {("kibana", c["elasticsearch_name"], c["elasticsearch_namespace"])})
            else:
                api.run("-n", c["elasticsearch_namespace"], "wait", "--for=jsonpath={.status.health}=green", "kibana/" + c["elasticsearch_name"], "--timeout=10m", timeout=660)
            print("ELASTICSEARCH_HEALTH_PASS nodes=" + str(expected))
            return
        print("ELASTICSEARCH_WAIT_HEARTBEAT phase=" + str(status.get("phase")) + " reconciled=" + str(reconciled).lower() +
              " health=" + str(status.get("health")) + " nodes=" + str(status.get("availableNodes")), flush=True)
        time.sleep(10)
    raise ValueError("Elasticsearch current generation/data readiness timed out")


def absent(c, api):
    # Kubernetes DeleteOnScaledownOnly retains claims on whole-CR deletion.
    # Keep the namespace, Secrets, ConfigMaps and the shared ECK installation.
    ns, name = c["elasticsearch_namespace"], c["elasticsearch_name"]
    list(existing_auxiliary(c, api))
    obj = api.get("elasticsearch", name, ns) if api.get("crd", "elasticsearches.elasticsearch.k8s.elastic.co") else None
    if obj and not owned_resource(c, obj):
        raise ValueError("refusing to delete foreign Elasticsearch")
    if obj:
        retirement_identity(obj)
    delete_owned(c, api, set(auxiliary(c)))
    if obj:
        delete_checked(api, "elasticsearch", name, ns, obj)
    print("ELASTICSEARCH_ABSENT_PASS pvc=retained secrets=retained eck=retained")


def reserve_repository(c, api, req, *, commit, cluster_uuid=""):
    """Keep backup ownership after uninstall; serialize writers with API CAS.

    Bucket/path uniqueness is deliberately conservative across endpoints:
    different DNS aliases may address the same store. Readers never claim a
    writer lease. No credentials are stored in this platform ConfigMap.
    """
    if c["elasticsearch_repository_role"] != "writer":
        return
    name = "kubeauto-eck-repository-writers"
    ledger = api.get("configmap", name, "elastic-system")
    labels = {"app.kubernetes.io/managed-by": "kubeauto", "kubeauto.io/component": "eck"}
    if ledger and any(ledger["metadata"].get("labels", {}).get(k) != v for k, v in labels.items()):
        raise ValueError("foreign repository ownership ledger")
    claims = json.loads(ledger["data"]["claims"]) if ledger else []
    if not isinstance(claims, list):
        raise ValueError("invalid repository ownership ledger")
    namespace = api.get("namespace", c["elasticsearch_namespace"])
    if not namespace or not namespace["metadata"].get("uid"):
        raise ValueError("repository ownership requires an existing namespace identity")
    owner = {"namespace": c["elasticsearch_namespace"], "namespace_uid": namespace["metadata"]["uid"],
             "name": c["elasticsearch_name"], "repository": req["name"]}
    matching = None
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != set(owner) | {"bucket", "base_path", "cluster_uuid"}:
            raise ValueError("invalid repository ownership claim")
        if not all(isinstance(value, str) and value for value in claim.values()):
            raise ValueError("invalid repository ownership claim")
        same_owner = all(claim[key] == value for key, value in owner.items())
        if same_owner:
            if (claim["bucket"] != req["bucket"] or claim["base_path"] != req["base_path"] or
                    cluster_uuid and claim["cluster_uuid"] != cluster_uuid):
                raise ValueError("repository identity is already bound to another location or data cluster")
            matching = claim
        a, b = claim["base_path"].split("/"), req["base_path"].split("/")
        if claim["bucket"] == req["bucket"] and (a[:len(b)] == b or b[:len(a)] == a) and not same_owner:
            raise ValueError("repository path overlaps another writer's retained backup tree")
    if not commit or matching:
        return
    if not cluster_uuid or cluster_uuid == "_na_":
        raise ValueError("repository writer requires a verified data cluster identity")
    claims.append(dict(owner, bucket=req["bucket"], base_path=req["base_path"], cluster_uuid=cluster_uuid))
    obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": name, "namespace": "elastic-system", "labels": labels},
        "data": {"claims": json.dumps(claims, sort_keys=True)}}
    if ledger:
        for key in ("uid", "resourceVersion"):
            if not ledger["metadata"].get(key):
                raise ValueError("repository ownership update requires a current API identity")
            obj["metadata"][key] = ledger["metadata"][key]
    # A create race or stale resourceVersion refuses BEFORE any S3 write.
    # An uncertain failure retains the reservation rather than opening a
    # second writer against possibly existing backup objects.
    api.run("replace" if ledger else "create", "-f", "-", body=json.dumps(obj))


def verify_write_repository(c, api, request, req):
    name = req["repository"] if req["type"] == "snapshot" else req["policy"]["repository"]
    repo = request("GET", "/_snapshot/" + quote(name, safe=""))[name]
    settings = repo.get("settings", {})
    if repo.get("type") != "s3" or flag(settings.get("readonly", False)):
        raise ValueError("snapshot/SLM requires a registered writable S3 repository")
    declared = {key: settings[key] for key in ("bucket", "base_path", "endpoint", "region") if key in settings}
    declared.update(id=req["id"], type="repository", name=name)
    validate_request(c, declared)
    reserve_repository(c, api, declared, commit=True, cluster_uuid=request("GET", "/")["cluster_uuid"])


def verify_restore_repository(c, request, req):
    name = req["repository"]
    repo = request("GET", "/_snapshot/" + quote(name, safe=""))[name]
    settings = repo.get("settings", {})
    if repo.get("type") != "s3" or not flag(settings.get("readonly", False)):
        raise ValueError("restore requires a registered readonly S3 repository")
    declared = {key: settings[key] for key in ("bucket", "base_path", "endpoint", "region") if key in settings}
    declared.update(id=req["id"], type="repository", name=name)
    validate_request(c, declared)


def operation(c, api, request, req):
    """Run one named operation; never expose response credentials in logs."""
    kind = req["type"]

    def identifier(key):
        value = req.get(key, "")
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", value):
            raise ValueError("invalid operation identifier: " + key)
        return quote(value, safe="")

    if kind == "repository":
        endpoint = urlsplit(req.get("endpoint", ""))
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or
                endpoint.query or endpoint.fragment or endpoint.path not in ("", "/")):
            raise ValueError("S3 repository requires an independent HTTPS endpoint without inline credentials")
        if not req.get("bucket") or not req.get("base_path") or not req.get("region"):
            raise ValueError("S3 bucket, region and unique base_path are required")
        settings = {k: req[k] for k in ("bucket", "base_path", "region")}
        settings.update(endpoint=req["endpoint"], path_style_access=flag(req.get("path_style_access", False)),
                        readonly=c["elasticsearch_repository_role"] == "reader")
        repo = identifier("name")
        reserve_repository(c, api, req, commit=True, cluster_uuid=request("GET", "/")["cluster_uuid"])
        data = request("PUT", "/_snapshot/" + repo, {"type": "s3", "settings": settings})
        if not data.get("acknowledged"):
            raise ValueError("repository declaration was not acknowledged")
        verified = request("POST", "/_snapshot/" + repo + "/_verify")
        if not verified.get("nodes"):
            raise ValueError("repository verify returned no participating node")
        # Analysis writes probe objects and must never run against a DR reader.
        if c["elasticsearch_repository_role"] == "writer":
            request("POST", "/_snapshot/" + repo + "/_analyze?blob_count=10&max_blob_size=1mb&max_total_data_size=10mb")
    elif kind in ("snapshot", "restore"):
        repo, snapshot = identifier("repository"), identifier("snapshot")
        path = "/_snapshot/" + repo + "/" + snapshot
        if kind == "snapshot":
            if c["elasticsearch_repository_role"] != "writer":
                raise ValueError("readonly DR target cannot write snapshots")
            indices = req.get("indices", [])
            if not indices or not all(re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", i) for i in indices):
                raise ValueError("snapshot requires an explicit non-wildcard index list")
            # Existing snapshots are immutable. On restart poll the exact name;
            # do not treat name collision as permission to overwrite a backup.
            existing = request("GET", path + "?ignore_unavailable=true")
            matches = [s for s in existing.get("snapshots", []) if s["snapshot"] == req["snapshot"]]
            if not matches:
                request("PUT", path + "?wait_for_completion=false", {
                    "indices": ",".join(indices), "include_global_state": flag(req.get("include_global_state", False)),
                    "feature_states": req.get("feature_states", ["none"]),
                    "metadata": {"kubeauto_change_id": req["id"]}})
            elif matches[0].get("metadata", {}).get("kubeauto_change_id") != req["id"]:
                raise ValueError("snapshot name belongs to another request")
            deadline = time.monotonic() + c["elasticsearch_wait_seconds"]
            while time.monotonic() < deadline:
                data = request("GET", path)["snapshots"][0]
                if data["state"] == "SUCCESS":
                    if data.get("shards", {}).get("failed", 0) or not data.get("indices"):
                        raise ValueError("snapshot is empty or contains shard failures")
                    return {"snapshot_uuid": data["uuid"]}
                if data["state"] not in ("IN_PROGRESS", "STARTED"):
                    raise ValueError("snapshot did not succeed")
                print("ELASTICSEARCH_WAIT_HEARTBEAT operation=snapshot id=" + req["id"], flush=True)
                time.sleep(10)
            raise ValueError("snapshot completion timed out")
        else:
            if c["elasticsearch_repository_role"] != "reader":
                raise ValueError("restore requires an isolated target with a readonly repository")
            indices = req.get("indices", [])
            if not indices or not all(re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", i) for i in indices):
                raise ValueError("restore requires explicit source indices")
            features = req.get("feature_states", ["none"])
            if "security" in features and not (c["elasticsearch_file_realm_secret"] and c["elasticsearch_admin_secret"]):
                raise ValueError("security restore requires an independent file-realm administrator")
            if "security" in features:
                identity = request("GET", "/_security/_authenticate")
                if identity.get("authentication_realm", {}).get("type") != "file" or "superuser" not in identity.get("roles", []):
                    raise ValueError("security restore administrator must authenticate through the independent file realm with superuser privileges")
            existing = request("GET", "/_cat/indices?format=json&expand_wildcards=all")
            if any(row["index"] in indices for row in existing):
                raise ValueError("restore target is not empty; never delete or overwrite existing indices automatically")
            source = request("GET", path)["snapshots"][0]
            if source.get("state") != "SUCCESS" or not set(indices) <= set(source.get("indices", [])):
                raise ValueError("restore source is incomplete or lacks requested indices")
            feature_indices = set()
            if features != ["none"]:
                feature_states = {state["feature_name"]: state.get("indices", []) for state in source.get("feature_states", [])}
                if any(not feature_states.get(feature) for feature in features):
                    raise ValueError("restore snapshot lacks requested nonempty feature states")
                if any(not row["index"].startswith(".") for row in existing):
                    raise ValueError("feature-state recovery requires an isolated target without existing business indices")
                feature_indices = {name for feature in features for name in feature_states[feature]}
            restoring = set(indices) | feature_indices
            response = request("POST", path + "/_restore?wait_for_completion=false", {
                "indices": ",".join(indices), "include_global_state": False, "feature_states": features})
            if not response.get("accepted"):
                raise ValueError("restore was not accepted")
            deadline = time.monotonic() + c["elasticsearch_wait_seconds"]
            target = ",".join(quote(i, safe="") for i in sorted(restoring))
            while time.monotonic() < deadline:
                state = request("GET", "/_cluster/health/" + target + "?wait_for_status=green&timeout=10s")
                recovery = request("GET", "/" + target + "/_recovery?active_only=false")
                completed = set(recovery) == restoring and all(index.get("shards") and any(shard.get("primary") for shard in index["shards"]) for index in recovery.values()) and all(
                    shard.get("stage") == "DONE" and shard.get("source", {}).get("repository") == req["repository"] and
                    shard.get("source", {}).get("snapshot") == req["snapshot"]
                    for index in recovery.values() for shard in index.get("shards", []) if shard.get("primary"))
                if completed and not state.get("timed_out") and state.get("status") == "green":
                    receipt = {"snapshot_uuid": source["uuid"], "index_uuids": index_uuids(request, indices)}
                    if feature_indices:
                        receipt["feature_index_uuids"] = index_uuids(request, sorted(feature_indices))
                    return receipt
                print("ELASTICSEARCH_WAIT_HEARTBEAT operation=restore id=" + req["id"], flush=True)
                time.sleep(5)
            raise ValueError("restore completion timed out; preserve the journal and inspect the target")
    elif kind == "slm":
        if c["elasticsearch_repository_role"] != "writer":
            raise ValueError("readonly target cannot enable SLM")
        body = req.get("policy", {})
        if not all(body.get(k) for k in ("schedule", "name", "repository", "config", "retention")):
            raise ValueError("SLM policy needs schedule, name, repository, config and retention")
        request("PUT", "/_slm/policy/" + identifier("name"), body)
    elif kind == "role":
        request("PUT", "/_security/role/" + identifier("name"), req["definition"])
    elif kind == "user":
        secret = api.get("secret", req["password_secret"], c["elasticsearch_namespace"])
        if not secret or "password" not in secret.get("data", {}):
            raise ValueError("user password Secret is missing")
        request("PUT", "/_security/user/" + identifier("name"), {
            "password": base64.b64decode(secret["data"]["password"]).decode(), "roles": req["roles"]})
    elif kind == "reload-secure-settings":
        data = request("POST", "/_nodes/reload_secure_settings", {})
        expected = member_count(c)
        if (len(data.get('nodes', {})) != expected or data.get('_nodes', {}).get('successful') != expected or
                data.get('_nodes', {}).get('failed') != 0 or any(n.get("reload_exception") for n in data.get("nodes", {}).values())):
            raise ValueError("one or more nodes refused secure-settings reload")


def index_uuids(request, indices):
    if indices and all(name.startswith(".") for name in indices):
        # System data-stream backing indices reject direct settings access,
        # including file-realm superusers. Read only UUID metadata, then select
        # the exact indices declared by the snapshot's feature states.
        metadata = request("GET", "/_cluster/state/metadata?filter_path=metadata.indices.*.settings.index.uuid")
        available = metadata.get("metadata", {}).get("indices", {})
        data = {name: available[name] for name in indices if name in available}
    else:
        data = request("GET", "/" + ",".join(quote(i, safe="") for i in indices) + "/_settings?filter_path=*.settings.index.uuid")
    if set(data) != set(indices):
        raise ValueError("restored index set changed")
    result = {name: value.get("settings", {}).get("index", {}).get("uuid") for name, value in data.items()}
    if any(not isinstance(value, str) or not value for value in result.values()):
        raise ValueError("restored index UUID is missing")
    return result


def verify_receipt(request, req, receipt):
    source = request("GET", "/_snapshot/" + req["repository"] + "/" + req["snapshot"])["snapshots"][0]
    if source.get("state") != "SUCCESS" or source.get("uuid") != receipt.get("snapshot_uuid"):
        raise ValueError("completed request snapshot is absent, changed or incomplete")
    if req["type"] == "snapshot" and source.get("metadata", {}).get("kubeauto_change_id") != req["id"]:
        raise ValueError("completed snapshot request identity changed")
    if req["type"] == "restore" and index_uuids(request, req["indices"]) != receipt.get("index_uuids"):
        raise ValueError("completed restore target index identity changed")
    if req["type"] == "restore" and req.get("feature_states", ["none"]) != ["none"]:
        indices = {name for state in source.get("feature_states", []) if state["feature_name"] in req["feature_states"] for name in state.get("indices", [])}
        if not indices or index_uuids(request, sorted(indices)) != receipt.get("feature_index_uuids"):
            raise ValueError("completed restore target feature-state index identity changed")


def operations(c, api):
    ns = c["elasticsearch_namespace"]
    with connection(c, api) as request:
        for req in c["elasticsearch_requests"]:
            record_name = c["elasticsearch_name"] + "-request-" + req["id"]
            fingerprint = hashlib.sha256(json.dumps(req, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            record = api.get("configmap", record_name, ns)
            if record:
                if not owned_resource(c, record) or record.get("data", {}).get("fingerprint") != fingerprint:
                    raise ValueError("change ID is already bound to different input")
            cluster_uuid = request('GET', '/')['cluster_uuid']
            if record:
                if record.get('data', {}).get('cluster_uuid') != cluster_uuid:
                    raise ValueError("request journal belongs to a different data cluster")
            if req["type"] in ("snapshot", "slm"):
                verify_write_repository(c, api, request, req)
            elif req["type"] == "restore":
                verify_restore_repository(c, request, req)
            if record:
                if record["data"].get("phase") == "complete" and req["type"] in ("snapshot", "restore"):
                    verify_receipt(request, req, json.loads(record["data"].get("receipt", "{}")))
                    print("ELASTICSEARCH_REQUEST_REUSED id=" + req["id"], flush=True)
                    continue
                if record["data"].get("phase") == "running" and req["type"] == "restore":
                    raise ValueError("interrupted restore has an unknown result; inspect target data before using a new change ID")
            journal = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(c, record_name),
                       "data": {"fingerprint": fingerprint, "cluster_uuid": cluster_uuid, "phase": "running", "type": req["type"]}}
            api.apply([journal])
            receipt = operation(c, api, request, req)
            if receipt:
                journal["data"]["receipt"] = json.dumps(receipt, sort_keys=True)
            journal["data"]["phase"] = "complete"
            api.apply([journal])
            print("ELASTICSEARCH_REQUEST_PASS id=" + req["id"] + " type=" + req["type"], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "preflight", "namespace", "render", "apply", "sysctl", "health", "absent", "operations", "enable-monitoring", "network"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dry-run", choices=('server', 'none'), default='server')
    args = parser.parse_args()
    c = validate(json.loads(args.config.read_text()))
    if args.action == "validate":
        print("ELASTICSEARCH_CONFIG_PASS")
    elif args.action == "render":
        revision = ""
        if c["elasticsearch_s3_ca_configmap"]:
            if not args.kubeconfig:
                parser.error("S3 CA rendering requires --kubeconfig")
            ca = API(args.kubeconfig).get("configmap", c["elasticsearch_s3_ca_configmap"], c["elasticsearch_namespace"])
            if not ca or not ca.get("data", {}).get("ca.crt"):
                raise ValueError("S3 CA ConfigMap is missing")
            revision = hashlib.sha256(ca["data"]["ca.crt"].encode()).hexdigest()
        print(json.dumps({"apiVersion": "v1", "kind": "List", "items": resources(c, revision)}))
    else:
        if not args.kubeconfig:
            parser.error("--kubeconfig is required for cluster operations")
        api = API(args.kubeconfig)
        if args.action == "apply":
            if args.manifest is None:
                parser.error('--manifest is required for declaration convergence')
            converge(c, api, args.manifest, dry_run=args.dry_run == 'server')
        elif args.action == "preflight":
            preflight(c, api)
            print("ELASTICSEARCH_PREFLIGHT_PASS")
        elif args.action == "enable-monitoring":
            enable_monitoring(c, api)
        elif args.action == "namespace":
            existing = api.get("namespace", c["elasticsearch_namespace"])
            if existing and not owned(existing):
                raise ValueError("foreign business namespace")
            api.apply([resources(c)[0]])
            name = c["elasticsearch_name"] + "-identity"
            ledger = api.get("configmap", name, c["elasticsearch_namespace"])
            if ledger and not owned_resource(c, ledger):
                raise ValueError("foreign Elasticsearch identity ledger")
            data = dict(ledger.get("data", {})) if ledger else {"pvc_uids": "{}"}
            data.update(version_ceiling=c["elasticsearch_ver"], topology=c["elasticsearch_topology"])
            api.apply([{"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(c, name), "data": data}])
        elif args.action == "network":
            if flag(c["elasticsearch_network_policy_enabled"]):
                api.apply([network_policy(c)])
            else:
                obj = api.get("networkpolicy", c["elasticsearch_name"] + "-access", c["elasticsearch_namespace"])
                if obj:
                    if not owned(obj):
                        raise ValueError("foreign NetworkPolicy")
                    api.run("-n", c["elasticsearch_namespace"], "delete", "networkpolicy", obj["metadata"]["name"])
        else:
            globals()[args.action](c, api)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        print("ELASTICSEARCH_REFUSED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

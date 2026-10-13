#!/usr/bin/env python3
"""Read-only dual-registry identity gate. Uses skopeo's existing auth lookup."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import subprocess
import sys

IMAGES = (
    ("redis-operator", "0.27.0"), ("redis", "8.10.2"),
    ("redis-exporter", "v1.93.0"), ("redis-upgrade-fixture", "8.10.1"),
    ("redis-operator-upgrade-fixture", "0.26.0"),
)
PROTECTED_IMAGES = (("redis-operator", "0.27.0-kubeauto.2"),)
CLUSTER_IMAGES = (("redis", "8.10.2-kubeauto.1"),)


def identity(raw):
    manifest = json.loads(raw)
    platforms = {row.get("platform", {}).get("architecture"): row["digest"]
                 for row in manifest.get("manifests", [])
                 if row.get("platform", {}).get("os") == "linux"}
    if not {"amd64", "arm64"} <= platforms.keys():
        raise ValueError("both linux/amd64 and linux/arm64 are required")
    return "sha256:" + hashlib.sha256(raw).hexdigest(), platforms


def publication_records(evidence, pins=IMAGES):
    """Read the reviewed Actions evidence; never infer dual push from one registry."""
    if (evidence.get('schema') != 1 or evidence.get('workflow_conclusion') != 'success' or
            evidence.get('durable_rc') != 0 or evidence.get('failure_markers') != 0 or
            not re.fullmatch(r'[0-9a-f]{40}', evidence.get('head', ''))):
        raise ValueError('invalid-publication-evidence')
    run_id = evidence.get('run_id')
    if (not isinstance(run_id, int) or run_id <= 0 or evidence.get('run_url') !=
            f'https://github.com/brinnatt/kubeauto-ext-images-dockerfile/actions/runs/{run_id}'):
        raise ValueError('invalid-publication-provenance')
    archives = evidence.get('artifact_sha256', {})
    if not archives or any(not re.fullmatch(r'sha256:[0-9a-f]{64}', value)
                           for value in archives.values()):
        raise ValueError('missing-artifact-checksums')
    expected = {f'brinnatt/{name}:{tag}' for name, tag in pins}
    records = {}
    for record in evidence.get('images', []):
        key = record['key']
        if key in records or key not in expected or record.get('head') != evidence['head']:
            raise ValueError('publication-image-mismatch')
        if (record.get('dockerhub_ref') != key or record.get('talkedu_ref') !=
                'hub.talkedu.cn/kubeauto/' + key.removeprefix('brinnatt/')):
            raise ValueError('publication-registry-mismatch')
        if (set(record.get('platforms', {})) != {'linux/amd64', 'linux/arm64'} or
                not re.fullmatch(r'[0-9a-f]{64}', record.get('fingerprint', '')) or
                any(not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in
                    [record.get('digest', ''), *record.get('platforms', {}).values()])):
            raise ValueError('invalid-publication-manifests')
        records[key] = record
    if set(records) != expected:
        raise ValueError('publication-image-coverage-incomplete')
    return records


def inspect(image):
    result = subprocess.run(["skopeo", "inspect", "--raw", "docker://" + image],
                            capture_output=True, timeout=75)
    if result.returncode:
        # Never relay registry credentials or arbitrary stderr into evidence.
        error = result.stderr.lower()
        reason = ("authentication" if b"unauthorized" in error or b"denied" in error else
                  "unpublished" if b"manifest unknown" in error or b"name unknown" in error else
                  "registry-or-network")
        raise ValueError(reason)
    return identity(result.stdout)


def verify(pin, published=None):
    name, tag = pin
    refs = (f"hub.talkedu.cn/kubeauto/{name}:{tag}", f"docker.io/brinnatt/{name}:{tag}")
    records = []
    # Actions has read back both formal tags. China verifies the current local
    # delivery source against that immutable evidence without repeating Hub pulls.
    for ref in refs[:1] if published else refs:
        print(f"REDIS_ARTIFACT_CHECK image={ref}", flush=True)
        try:
            digest, platforms = inspect(ref)
            records.append((digest, platforms))
            print(json.dumps({"marker": "REDIS_ARTIFACT_MANIFEST", "image": ref,
                              "digest": digest, "platforms": platforms}), flush=True)
        except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
            reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            print(f"REDIS_ARTIFACT_FAIL image={ref} class=supply-chain reason={reason}", flush=True)
            return False
    if published:
        record = published[f'brinnatt/{name}:{tag}']
        records.append((record['digest'], {key.split('/')[1]: value
                                          for key, value in record['platforms'].items()}))
    if records[0] != records[1]:
        print(f"REDIS_ARTIFACT_FAIL image={name}:{tag} class=supply-chain reason=dual-push-mismatch", flush=True)
        return False
    return True


def main(pins=IMAGES):
    try:
        payload = os.environ.get('REDIS_PUBLISHED_EVIDENCE')
        published = publication_records(json.loads(payload), pins) if payload else None
    except (ValueError, KeyError, TypeError, AttributeError):
        print('REDIS_ARTIFACT_FAIL class=supply-chain reason=invalid-publication-evidence')
        return 1
    with ThreadPoolExecutor(max_workers=min(5, len(pins))) as pool:
        results = list(pool.map(lambda pin: verify(pin, published), pins))
    if not all(results):
        return 1
    print(f"REDIS_ARTIFACTS_PASS count={len(pins)} verification={'actions-and-current-talkedu' if published else 'current-dual-registry'}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ['--protected-operator'], ['--cluster-runtime']):
        sys.exit(2)
    pins = {'--protected-operator': PROTECTED_IMAGES, '--cluster-runtime': CLUSTER_IMAGES}
    sys.exit(main(pins[sys.argv[1]] if sys.argv[1:] else IMAGES))

#!/usr/bin/env python3
"""Verify every descriptor behind an OCI/Docker registry manifest.

The registry API identifies manifests, configs, and layers by the SHA-256 of
their exact response bytes.  Reading every object back after a push catches a
truncated or misrouted blob before Kubernetes/containerd consumes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request


MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def _read_and_hash(url: str, *, accept: str | None = None) -> tuple[bytes, str]:
    headers = {"Accept": accept} if accept else {}
    request = urllib.request.Request(url, headers=headers)
    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    with urllib.request.urlopen(request, timeout=30) as response:
        while chunk := response.read(1024 * 1024):
            chunks.append(chunk)
            hasher.update(chunk)
    return b"".join(chunks), f"sha256:{hasher.hexdigest()}"


def _verify_descriptor(base_url: str, repository: str, descriptor: dict) -> None:
    expected_digest = descriptor["digest"]
    expected_size = descriptor["size"]
    blob_url = f"{base_url}/v2/{repository}/blobs/{expected_digest}"
    payload, actual_digest = _read_and_hash(blob_url)
    if actual_digest != expected_digest or len(payload) != expected_size:
        raise RuntimeError(
            f"descriptor mismatch expected_digest={expected_digest} "
            f"actual_digest={actual_digest} expected_size={expected_size} "
            f"actual_size={len(payload)}"
        )
    print(f"BLOB_OK digest={expected_digest} size={expected_size}")


def verify(base_url: str, repository: str, reference: str) -> None:
    base_url = base_url.rstrip("/")
    quoted_reference = urllib.parse.quote(reference, safe=":@")
    manifest_url = f"{base_url}/v2/{repository}/manifests/{quoted_reference}"
    payload, manifest_digest = _read_and_hash(manifest_url, accept=MANIFEST_ACCEPT)
    manifest = json.loads(payload)
    print(f"MANIFEST_OK digest={manifest_digest} size={len(payload)}")

    descriptors = [manifest["config"], *manifest.get("layers", [])]
    for descriptor in descriptors:
        _verify_descriptor(base_url, repository, descriptor)
    print(
        f"REGISTRY_BLOB_INTEGRITY_PASS repository={repository} "
        f"reference={reference} descriptors={len(descriptors)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("repository")
    parser.add_argument("reference")
    args = parser.parse_args()
    verify(args.base_url, args.repository, args.reference)


if __name__ == "__main__":
    main()

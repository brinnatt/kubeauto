#!/usr/bin/env python3
"""Fixed-size SigV4 PUT probe for the Ceph regression gate."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree


PAYLOAD = b"kubeauto-ceph-s3-v1"
SIGNED_HEADERS = "content-type;host;x-amz-content-sha256;x-amz-date"


def signed_request(endpoint: str, bucket: str, key: str, access: str,
                   secret: str, now: datetime) -> Request:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise ValueError("endpoint must be an HTTP origin")
    if not re.fullmatch(r"[a-z0-9-]+", bucket) or not re.fullmatch(r"[a-z0-9-]+", key):
        raise ValueError("bucket and key must use the fixed regression name format")
    date = now.strftime("%Y%m%d")
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = f"/{quote(bucket, safe='')}/{quote(key, safe='')}"
    payload_hash = hashlib.sha256(PAYLOAD).hexdigest()
    headers = {
        "content-type": "application/octet-stream",
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": timestamp,
    }
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = (
        f"PUT\n{path}\n\n{canonical_headers}\n{SIGNED_HEADERS}\n{payload_hash}"
    )
    scope = f"{date}/us-east-1/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signing_key = ("AWS4" + secret).encode()
    for part in (date, "us-east-1", "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
        f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
    )
    return Request(f"{endpoint}{path}", data=PAYLOAD, headers=headers, method="PUT")


def error_code(body: bytes) -> str:
    try:
        code = ElementTree.fromstring(body).findtext(".//{*}Code") or "missing"
    except ElementTree.ParseError:
        return "invalid-xml"
    return code if re.fullmatch(r"[A-Za-z0-9]+", code) else "invalid-code"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    access, secret = os.environ.get("RGW_ACCESS", ""), os.environ.get("RGW_SECRET", "")
    if not access or not secret:
        print("RGW_S3_PUT_FAIL credentials=missing", file=sys.stderr)
        return 1
    request = signed_request(args.endpoint, args.bucket, args.key, access, secret,
                             datetime.now(timezone.utc))
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            status = response.status
    except HTTPError as exc:
        print(f"RGW_S3_PUT_FAIL http={exc.code} code={error_code(exc.read(4096))}",
              file=sys.stderr)
        return 1
    except URLError:
        print("RGW_S3_PUT_FAIL transport=unavailable", file=sys.stderr)
        return 1
    if status != 200:
        print(f"RGW_S3_PUT_FAIL http={status} code=unexpected-status", file=sys.stderr)
        return 1
    print("RGW_SIGV4_PUT_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

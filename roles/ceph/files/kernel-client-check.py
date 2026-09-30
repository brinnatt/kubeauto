#!/usr/bin/env python3
"""Read-only secure kernel-client qualification, including vendor backports."""

import gzip
import json
import lzma
import platform
import subprocess
import sys


def kernel_client_features(module_path):
    if module_path.endswith(".xz"):
        with lzma.open(module_path, "rb") as stream:
            contents = stream.read()
    elif module_path.endswith(".gz"):
        with gzip.open(module_path, "rb") as stream:
            contents = stream.read()
    elif module_path.endswith(".zst"):
        contents = subprocess.check_output(["zstd", "--decompress", "--stdout", module_path])
    else:
        with open(module_path, "rb") as stream:
            contents = stream.read()
    # Inspect the actual module: version numbers alone cannot qualify backports.
    return {
        "secure_msgr2": b"ms_mode=secure" in contents,
        "aes256k": all(symbol + b"\x00" in contents for symbol in (
            b"crypto_krb5_find_enctype", b"crypto_krb5_prepare_encryption",
        )),
    }


def main(key_type="aes256k"):
    if key_type not in ("aes256k", "aes"):
        raise ValueError("unsupported CephX client key type: %s" % key_type)
    release = platform.release()
    for module in ("libceph", "rbd", "ceph"):
        subprocess.check_call(["modprobe", "--dry-run", module])
    module_path = subprocess.check_output(
        ["modinfo", "--filename", "libceph"], universal_newlines=True
    ).strip()
    features = kernel_client_features(module_path)
    if not features["secure_msgr2"]:
        raise ValueError(
            "kernel %s lacks secure msgr2; install the distribution-supported "
            "updated/HWE kernel and reboot before enabling Ceph-CSI; "
            "security is not downgraded and Alpha RBD-NBD is not selected" % release
        )
    if key_type == "aes256k" and not features["aes256k"]:
        raise ValueError(
            "kernel %s lacks CephX aes256k required by the selected client key; "
            "Linux 7.0 introduced support, and vendor backports must be verified. "
            "Install a distribution-supported kernel with the backport and reboot; "
            "legacy AES keys are not enabled automatically" % release
        )
    print(json.dumps(dict(kernel=release, **features)))


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) == 2 else "aes256k")
    except (OSError, ValueError, subprocess.CalledProcessError, lzma.LZMAError) as error:
        print("CEPH_KERNEL_CLIENT_REJECT: %s" % error, file=sys.stderr)
        sys.exit(1)

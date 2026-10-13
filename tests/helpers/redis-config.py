#!/usr/bin/env python3
"""Read scalar Redis configuration without evaluating shell fragments."""
import re
import sys
from pathlib import Path
import yaml


def load(path):
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("configuration must be a mapping")
    if str(data.get("redis_install", "")).lower() not in ("yes", "true"):
        raise ValueError("Redis must be enabled in the product cluster configuration")
    if data.get("redis_mode") not in ("sentinel", "cluster"):
        raise ValueError("invalid Redis mode")
    for key in ("redis_namespace", "redis_operator_namespace", "redis_name"):
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", str(data.get(key, ""))):
            raise ValueError("invalid resource name: " + key)
    if not re.fullmatch(r"8\.[0-9]+\.[0-9]+", str(data.get("redis_ver", ""))):
        raise ValueError("redis_ver must be resolved by kubecli new, not a template placeholder")
    return data


def main():
    try:
        data = load(sys.argv[1])
        key = sys.argv[2]
        if key == "--check":
            print(f"REDIS_CONFIG_PASS namespace={data['redis_namespace']} name={data['redis_name']} mode={data['redis_mode']}")
            return 0
        if not re.fullmatch(r"redis_[a-z_]+", key) or key not in data:
            raise ValueError("unknown Redis configuration key")
        value = data[key]
        if not isinstance(value, (str, bool, int, float)) or "\n" in str(value) or "\r" in str(value):
            raise ValueError("configuration value must be a single scalar")
        print(str(value).lower() if isinstance(value, bool) else value)
        return 0
    except (IndexError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"REDIS_CONFIG_FAIL reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

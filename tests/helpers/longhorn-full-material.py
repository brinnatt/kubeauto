#!/usr/bin/env python3
"""Finite runtime-only paths for one fresh full Longhorn regression.

This selects material; each consumer retains its own acceptance assertions.
An explicitly selected run never falls back to committed historical evidence.
"""
import os
from pathlib import Path
import stat

ENV = 'LONGHORN_FULL_EVIDENCE_DIR'
FILES = {
    'tests/evidence/longhorn-v2-resources-qualification-20261020.json': 'v2-resources.json',
    'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json': 'v2-install.json',
    'tests/evidence/longhorn-v2-upgrade-lifecycle-20261021.json': 'v2-upgrade.json',
    'tests/evidence/longhorn-v2-dr-target-core-20261020.json': 'target-core.json',
    'tests/evidence/longhorn-v2-dr-target-install-qualification-20261020.json': 'target-install.json',
    'tests/evidence/longhorn-v2-backup-store-qualification-20261020.json': 'v2-backup-store.json',
    'tests/evidence/longhorn-v2-recurring-selector-qualification-20261020.json': 'v2-selector.json',
    'tests/evidence/longhorn-v2-dr-store-qualification-20261021.json': 'dr-store.json',
    'tests/evidence/longhorn-v2-dr-standby-qualification-20261021.json': 'dr-standby.json',
    'tests/evidence/longhorn-v2-dr-activation-qualification-20261021.json': 'dr-activation.json',
}


def active():
    return ENV in os.environ


def private(path, *, directory=False):
    metadata = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(metadata.st_mode) or metadata.st_uid != os.geteuid() or
            stat.S_IMODE(metadata.st_mode) & 0o077):
        raise ValueError('full material requires owned private non-symlink '+('directory' if directory else 'file'))


def directory(base):
    root = Path(base).resolve()
    value = os.environ[ENV]
    path = Path(value)
    if not value or not path.is_absolute() or '..' in path.parts or not path.is_relative_to(root/'logs'):
        raise ValueError('full material directory must be explicit and inside product logs')
    current = root
    for part in path.relative_to(root).parts:
        current = current/part
        if current.is_symlink():
            raise ValueError('full material directory crosses a symlink')
    private(path, directory=True)
    return path


def path(base, historical, *, optional=False):
    if historical not in FILES:
        raise ValueError('unregistered full material consumer')
    if not active():
        return Path(base)/historical
    result = directory(base)/FILES[historical]
    if optional and not result.exists() and not result.is_symlink():
        return result
    private(result)
    return result

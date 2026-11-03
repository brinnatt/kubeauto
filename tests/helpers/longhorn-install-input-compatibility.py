#!/usr/bin/env python3
"""Read-only reuse after the exact reviewed cache and Event ownership fixes.

The old plan and adoption evidence stay immutable. Only the reviewed helper
inputs may differ; their contract and new archived Event dependency stay pinned.
New adoption never uses this adapter. The runner freezes this helper as input.
"""
from pathlib import Path
import os

BASE = Path('/usr/local/kubeauto')
PLAN_SHA256 = 'f953daa0490a77e2559f9f228421c03b60b92fe348b11848d06a1d6339876a89'
CONTRACT = ('tests/unit/test_longhorn_monitoring_cache_reconcile.py', 'b8123408053b42b0c36e0ea02db6ed2be82a67595bab99f72c75389af9b4a5cf')
EVENT_RECOVERY = ('tests/evidence/longhorn-node-failure-cleanup-first-failure-20261022.json', 'dfe298d03d7d7c111f81c9657d0eb8f06f68c62214a1cb89d0147012d342db49')
INPUTS = {
    'tests/helpers/longhorn-lab.py': (
        'ce0dc7f776169f8bbf646ea86ff39d9f37e64c8b6261749e5aa6f1888e06742c',
        'ed237430bac9b2c0286fc2005081cba200fb7e51da64bf89e5955d05568e632b'),
    'tests/helpers/longhorn-v2-install.py': ('ae963a6f8eb4688e8c2a2d820c8fefb1f32593b1cb8698dfa8036de06cff13f4', '7afb928fe787e6df4e18ccf41b7e1107febb1e2f4c8df4acc0ebe153bd75b84d'),
    'tests/helpers/longhorn-v2-dr-target.py': ('e55d59daa0c19574d574e823a8789ff2b6b48580e712f32c51993a850732a48f', '5a8112f03c9b7e5abca1a112441ccecc422b7130772954be546cc9048e84fe1a'),
}


# A separately reviewed default .2 reader cohort; the original cohort stays intact.
CURRENT_INPUTS = {'tests/helpers/longhorn-lab.py': ('ce0dc7f776169f8bbf646ea86ff39d9f37e64c8b6261749e5aa6f1888e06742c',
                                   'ed237430bac9b2c0286fc2005081cba200fb7e51da64bf89e5955d05568e632b'),
 'tests/helpers/longhorn-v2-install.py': ('ae963a6f8eb4688e8c2a2d820c8fefb1f32593b1cb8698dfa8036de06cff13f4',
                                          'c6a9bf7a0755aad74ae2ed30381845fcfb1cf1f5364eb5d67307e017609feb2d'),
 'tests/helpers/longhorn-v2-dr-target-install.py': ('af065aa3959afe9158f9ef3f766e383dc59a58753ff4b069c1dc082b635c10e3',
                                                    '90647ef395f9b4be858f409c8fc5c1d76abfb3f157536a312cbd5f98d174b4b4'),
 'tests/helpers/longhorn-v2-upgrade-baseline.py': ('59b57a74d64624576feba26d155e336da8288b0c8ef4237852c7d06a18bd6d87',
                                                   'dcaef1fbd558cb9de9aedf8e0635ccf6a439a7151ca4f3541b9c9469023cfcdd')}
CURRENT_INPUTS['tests/helpers/longhorn-v2-dr-target.py'] = (
    'e55d59daa0c19574d574e823a8789ff2b6b48580e712f32c51993a850732a48f',
    '5a8112f03c9b7e5abca1a112441ccecc422b7130772954be546cc9048e84fe1a')
CURRENT_REVIEW = ('tests/evidence/longhorn-completed-default-reader-reuse-20261022.json', '7cb5eb5f79640cf05fac92631e677f5435040e3ed35c70a07a484c1009235396')


FILESYSTEM_INPUT = ('roles/longhorn/files/longhorn.py', ('685ce68c2240c931d06b9b8d608a0dcfaae09e1f67866f1941393669c6670282', '8a842e6260e307cb41d40154f8e0d2b96bd172efcf1be3a20c99116338cb92c1'))
FILESYSTEM_REVIEW = ('tests/evidence/longhorn-v2-cpu-isolation-v1-disk-impact-20261022.json', '30541f3eb959f1ce176e52050c557ccb33e0e5afadf0428a4e254fbc7e70aa29')


def input_current(name, original, current):
    return (original == current or INPUTS.get(name) == (original, current) or
            CURRENT_INPUTS.get(name) == (original, current) or
            (name == FILESYSTEM_INPUT[0] and FILESYSTEM_INPUT[1] == (original, current)))


def read_completed_plan(revision, profile):
    """Validate every original guard with the selected exact reviewed cohort."""
    digest = revision.digest
    if profile not in ('v2', 'v2-dr-target'):
        raise ValueError('installation reuse compatibility excludes new adoption and V1')
    if digest(BASE/revision.PLAN) != PLAN_SHA256:
        # Other exact plans remain supported; they receive no exception.
        return revision.read_plan(profile)
    selected = INPUTS
    install_name = 'tests/helpers/longhorn-v2-install.py'
    if digest(BASE/install_name) == CURRENT_INPUTS[install_name][1]:
        reference, expected = CURRENT_REVIEW
        if (not (BASE/reference).is_file() or (BASE/reference).is_symlink() or
                digest(BASE/reference) != expected):
            raise ValueError('completed installation compatibility input changed: '+install_name+
                             ' (default-reader review missing or changed)')
        if (any(name in os.environ for name in
                ('LONGHORN_FULL_EVIDENCE_DIR', 'LONGHORN_V2_RUNTIME_CONTINUATION')) or
                any((BASE/('tests/evidence/longhorn-'+engine+
                    '-im-revision-3-adopt-qualification-20261022.json')).exists() or
                    (BASE/('tests/evidence/longhorn-'+engine+
                    '-im-revision-3-adopt-qualification-20261022.json')).is_symlink()
                    for engine in ('v2', 'v2-dr-target'))):
            raise ValueError('completed .2 default-reader reuse excludes full/continuation and .3 adoption')
        selected = CURRENT_INPUTS
    name, pair = FILESYSTEM_INPUT
    if digest(BASE/name) == pair[1]:
        reference, expected = FILESYSTEM_REVIEW
        if digest(BASE/reference) != expected:
            raise ValueError('completed installation filesystem impact review changed')
        selected = dict(selected, **{name: pair})
    for name, (_, current) in selected.items():
        if digest(BASE/name) != current:
            raise ValueError('completed installation compatibility input changed: '+name)
    for name, expected in (CONTRACT, EVENT_RECOVERY):
        if digest(BASE/name) != expected:
            raise ValueError('completed installation compatibility dependency changed: '+name)

    def historical_digest(path):
        current = digest(path)  # Always enforce regular-file and symlink guards.
        relative = str(path.relative_to(BASE))
        pair = selected.get(relative)
        return pair[0] if pair and current == pair[1] else current

    revision.digest = historical_digest
    try:
        return revision.read_plan(profile)
    finally:
        revision.digest = digest

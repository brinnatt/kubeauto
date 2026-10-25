#!/usr/bin/env python3
"""Longhorn V1 host checks and identity-bound post-uninstall guard release."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def output(argv):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Longhorn host prerequisite failed: ' + argv[0])
    return result.stdout.strip()


def disk_receipt(path, minimum_gib, marker=None, *, cluster_uid='', node_uid=''):
    root = Path(path)
    if not root.is_dir() or root.resolve() != root or root.stat().st_dev == Path('/').stat().st_dev:
        raise ValueError('Longhorn requires an independent mounted filesystem, without symlinks/root fallback')
    if not all(re.fullmatch(r'[A-Za-z0-9-]+', value) for value in (cluster_uid, node_uid)):
        raise ValueError('Longhorn disk requires current cluster and node identities')
    data = json.loads(output(['findmnt', '--json', '--mountpoint', path, '-o', 'TARGET,SOURCE,FSTYPE,UUID,OPTIONS']))['filesystems']
    if len(data) != 1 or data[0]['target'] != path or data[0]['fstype'] not in ('ext4', 'xfs') or 'rw' not in data[0]['options'].split(',') or not data[0].get('uuid'):
        raise ValueError('Longhorn disk requires one writable ext4/xfs mount with stable UUID')
    disk = data[0]
    total, used, free = shutil.disk_usage(root)
    if total < minimum_gib * 1024**3 or free <= total // 4:
        raise ValueError('Longhorn disk is too small or lacks the 25 percent free-space floor')
    persistent = json.loads(output(['findmnt', '--fstab', '--evaluate', '--json', '--mountpoint', path, '-o', 'TARGET,SOURCE,OPTIONS']))['filesystems']
    if len(persistent) != 1 or persistent[0]['target'] != path or 'noauto' in persistent[0].get('options', '').split(','):
        raise ValueError('Longhorn disk must have one enabled persistent fstab mount')
    persistent_uuid = output(['blkid', '-p', '-s', 'UUID', '-o', 'value', persistent[0]['source']])
    if persistent_uuid != disk['uuid']:
        raise ValueError('Longhorn active and persistent filesystem identities differ')
    receipt = {'path': path, 'uuid': disk['uuid'], 'total_bytes': total, 'cluster_uid': cluster_uid, 'node_uid': node_uid}
    if marker and marker.is_symlink(): raise ValueError('Longhorn refuses a symlink filesystem receipt')
    if marker and marker.exists():
        old = json.loads(marker.read_text())
        if any(old.get(k) != receipt[k] for k in ('cluster_uid', 'node_uid')):
            raise ValueError('Longhorn filesystem receipt belongs to another or unknown cluster/node')
        if any(old.get(k) != receipt[k] for k in ('path', 'uuid')):
            raise ValueError('Longhorn disk identity changed; use the explicit disk replacement runbook')
    elif any(p.name != 'lost+found' for p in root.iterdir()):
        raise ValueError('Longhorn refuses unknown data on an unowned filesystem')
    return receipt


def clients():
    for command in ('iscsiadm', 'mount.nfs', 'cryptsetup', 'dmsetup', 'findmnt', 'blkid', 'lsblk'):
        if not shutil.which(command):
            raise ValueError('missing Longhorn client dependency: ' + command)
    # Longhorn 1.12.1 excludes this release: renamed session settings break
    # reading existing node records. Never rewrite host iSCSI/SAN state here.
    version = output(['iscsiadm', '--version'])
    if re.search(r'\bversion\s+(?:6\.)?2\.1\.12(?!\d)', version):
        raise ValueError('open-iscsi 2.1.12 is incompatible with Longhorn; use <=2.1.11 or >=2.1.13')
    if output(['systemctl', 'is-active', 'iscsid']) != 'active':
        raise ValueError('iscsid must be active')
    for module in ('iscsi_tcp', 'dm_crypt', 'nfs'):
        if not (Path('/sys/module') / module).exists():
            raise ValueError('missing Longhorn kernel module: ' + module)
    # Multipath may claim iSCSI devices. Never disable an existing SAN silently.
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'multipathd'], capture_output=True, timeout=30)
    if active.returncode == 0:
        raise ValueError('active multipathd requires an administrator-reviewed Longhorn device exclusion')


def release_mount_guard(path, marker, guard, *, cluster_uid='', node_uid='', check_only=False):
    """Release only our exact mount guard after the control-plane uninstall.

    The playbook first proves the absence of Longhorn CRDs/PVs. Filesystem,
    fstab, data, packages and shared client services remain administrator-owned.
    """
    for filename in (marker, guard):
        if filename.is_symlink() or (filename.exists() and not filename.is_file()):
            raise ValueError('Longhorn host release refuses a non-file receipt or guard')
    if not marker.exists() and not guard.exists():
        return False
    if not marker.exists():
        raise ValueError('Longhorn mount guard has no filesystem receipt')
    raw = marker.read_bytes()
    receipt = json.loads(raw)
    if not cluster_uid or not node_uid or receipt.get('cluster_uid') != cluster_uid or receipt.get('node_uid') != node_uid:
        raise ValueError('Longhorn host release refuses another or unknown cluster/node receipt')
    mounted = json.loads(output(['findmnt', '--json', '--mountpoint', path, '-o', 'TARGET,UUID']))['filesystems']
    if receipt.get('path') != path or not receipt.get('uuid') or mounted != [{'target': path, 'uuid': receipt['uuid']}]:
        raise ValueError('Longhorn filesystem identity differs during host release')
    expected = '[Unit]\nRequiresMountsFor=' + path + '\n[Service]\nExecStartPre=/usr/bin/mountpoint -q -- ' + path + '\n'
    if guard.exists() and guard.read_text() != expected:
        raise ValueError('Longhorn host release refuses a modified mount guard')
    if check_only:
        return True
    if marker.is_symlink() or guard.is_symlink() or marker.read_bytes() != raw:
        raise ValueError('Longhorn host receipt changed during release')
    if guard.exists():
        if guard.read_text() != expected:
            raise ValueError('Longhorn mount guard changed during release')
        guard.unlink()
    marker.unlink()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-path'); parser.add_argument('--minimum-gib', type=int, default=20)
    parser.add_argument('--marker', default='/etc/kubeauto/longhorn-disk.json')
    parser.add_argument('--release', choices=('check', 'apply'))
    parser.add_argument('--cluster-uid', default='')
    parser.add_argument('--node-uid', default='')
    parser.add_argument('--disk-only', action='store_true')
    args = parser.parse_args()
    if args.release:
        if not args.data_path:
            parser.error('--release requires --data-path')
        changed = release_mount_guard(args.data_path, Path(args.marker),
                    Path('/etc/systemd/system/kubelet.service.d/kubeauto-longhorn.conf'),
                    cluster_uid=args.cluster_uid, node_uid=args.node_uid,
                    check_only=args.release == 'check')
        print(json.dumps({'mount_guard_released': args.release == 'apply', 'changed': changed}))
        return
    if args.disk_only and not args.data_path:
        parser.error('--disk-only requires --data-path')
    if not args.disk_only:
        clients()
    receipt = disk_receipt(args.data_path, args.minimum_gib, Path(args.marker),
                           cluster_uid=args.cluster_uid, node_uid=args.node_uid) if args.data_path else {'client_ready': True}
    print(json.dumps(receipt, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print('LONGHORN_HOST_FAIL reason=' + str(error), file=sys.stderr)
        sys.exit(1)

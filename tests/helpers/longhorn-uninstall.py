"""Qualify product refusal before the official destructive uninstall hook."""
import copy
import json
import re
import secrets


def native_leases(lab):
    """Bind Manager-created metadata omitted by 1.12.1's deleteLease list.

    Only these two native factories are accepted in the original owned
    namespace. Unknown objects still fail the complete namespace graph check.
    Capture before the hook (or before a no-release recovery), then reject a
    replacement or a newly appearing lease before namespace deletion.
    """
    durations = {'longhorn-manager-webhook-lock': 20,
                 'longhorn-webhook-cert-rotation': 60}
    result = {}
    for obj in lab.get('leases.coordination.k8s.io', namespace='longhorn-system')['items']:
        meta = obj['metadata']; name = meta['name']
        if name not in durations:
            continue
        spec = obj.get('spec', {})
        if (obj.get('apiVersion') != 'coordination.k8s.io/v1' or obj.get('kind') != 'Lease' or
                meta.get('namespace') != 'longhorn-system' or not meta.get('uid') or
                meta.get('ownerReferences') or meta.get('deletionTimestamp') or
                meta.get('labels') or meta.get('annotations') or
                set(spec) != {'acquireTime', 'holderIdentity', 'leaseDurationSeconds', 'leaseTransitions', 'renewTime'} or
                # A startup leader's Lease can outlive its retired Node. The
                # original namespace and before/after Lease UID bind ownership.
                not isinstance(spec['holderIdentity'], str) or
                spec['holderIdentity'] and not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?', spec['holderIdentity']) or
                type(spec['leaseDurationSeconds']) is not int or spec['leaseDurationSeconds'] not in (1, durations[name]) or
                type(spec['leaseTransitions']) is not int or spec['leaseTransitions'] < 0 or
                any(not isinstance(spec[key], str) or not spec[key] for key in ('acquireTime', 'renewTime'))):
            raise ValueError('unrecognized native webhook lease identity or declaration')
        result['Lease/' + name] = meta['uid']
    return result


def recover(lab, relay_pid):
    """Finish native fixture recovery, then verify a retained external backup."""
    lab.recover_installation(relay_pid)
    store = lab.backup_store()
    if store.record.exists() or store.record.is_symlink():
        # A retained-backup receipt is mandatory. Never guess that an arbitrary
        # external backend is disposable merely because Longhorn is gone.
        verify_retained_backup(lab, phase='uninstalled')
        store.cleanup(after_uninstall=True)
        print('LONGHORN_UNINSTALL_RECOVERY_BACKUP_PRESERVED_PASS', flush=True)


def confirmation_state(lab):
    flag = lab.get('settings.longhorn.io', 'deleting-confirmation-flag', 'longhorn-system')
    if not flag or flag.get('value') != 'false' or lab.get('job', 'longhorn-uninstall', 'longhorn-system'):
        raise ValueError('uninstall refusal requires disabled deletion confirmation and no uninstall job')
    return flag


def verify_refusal(lab, journal, relay_pid, phase):
    if phase not in ('bound', 'released'):
        raise ValueError('unknown uninstall refusal phase')
    pv = lab.get('pv', journal['pv'])
    volume = lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')
    if phase == 'released':
        def detached():
            current = lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')
            if not current or current['metadata']['uid'] != journal['volume_uid']:
                raise ValueError('released refusal volume identity changed while detaching')
            return current if current.get('status', {}).get('state') == 'detached' and not current['spec'].get('nodeID') else None
        volume = lab.wait('uninstall-refusal-released-volume-detached', 180, detached)
    expected_phase = 'Bound' if phase == 'bound' else 'Released'
    if (not pv or pv['metadata']['uid'] != journal['pv_uid'] or
            pv.get('status', {}).get('phase') != expected_phase or
            pv.get('spec', {}).get('persistentVolumeReclaimPolicy') != 'Retain' or
            pv['spec'].get('claimRef', {}).get('uid') != journal['objects']['PersistentVolumeClaim/data'] or
            pv['spec'].get('csi', {}).get('volumeHandle') != journal['volume'] or
            not volume or volume['metadata']['uid'] != journal['volume_uid']):
        raise ValueError('uninstall refusal requires the original Retain PV and volume')
    original = (lab.DIRECTORY/'config.yml').read_bytes()
    before = lab.idempotence_snapshot(journal['namespace'])
    flag = confirmation_state(lab)
    for operation in ('uninstall', 'destroy'):
        if operation == 'uninstall':
            lab.run_requests(relay_pid, [], uninstall_id='refusal-' + secrets.token_hex(12),
                             expected_refusal='Longhorn uninstall refused: retained or bound PVs remain')
        else:
            lab.supervise_core(relay_pid, 'longhorn-destroy', expected_refusal=
                'Longhorn PVs remain; migrate/retain data and uninstall storage before destroy')
        current_pv = lab.get('pv', journal['pv'])
        current_volume = lab.get('volumes.longhorn.io', journal['volume'], 'longhorn-system')
        current_flag = lab.get('settings.longhorn.io', 'deleting-confirmation-flag', 'longhorn-system')
        if (not current_pv or current_pv['metadata']['uid'] != journal['pv_uid'] or
                current_pv['spec'] != pv['spec'] or
                not current_volume or current_volume['metadata']['uid'] != journal['volume_uid'] or
                current_volume['spec'] != volume['spec'] or
                not current_flag or current_flag['metadata']['uid'] != flag['metadata']['uid'] or
                current_flag.get('value') != flag['value'] or
                lab.get('job', 'longhorn-uninstall', 'longhorn-system') or
                (lab.DIRECTORY/'config.yml').read_bytes() != original or
                lab.idempotence_snapshot(journal['namespace']) != before):
            raise ValueError('refused destructive operation changed protected identities or declarations')
        print('LONGHORN_UNINSTALL_REFUSAL_OBSERVED phase=' + phase + ' operation=' + operation,
              flush=True)
    print('LONGHORN_UNINSTALL_REFUSAL_' + phase.upper() + '_PASS', flush=True)


def verify_retained_backup(lab, *, phase):
    """Read the actual backup configuration and every block, outside its source."""
    if phase not in ('connected', 'disconnected', 'source-removed', 'uninstalled'):
        raise ValueError('unknown external backup retention phase')
    store = lab.backup_store()
    path = store.directory / 'retained-backup.json'
    if path.is_symlink():
        raise ValueError('symlink external backup retention receipt')
    row = json.loads(path.read_text())
    state = lab.owner(); fixture = store.receipt()
    if (row['run_id'] != state['run_id'] or row['cluster_uid'] != state['cluster_uid'] or
            row['bucket'] != fixture['bucket']):
        raise ValueError('external backup retention belongs to another fixture')
    expected = row['object_receipt']
    if phase == 'uninstalled':
        if (state.get('native_uninstalled') is not True or
                lab.get('namespace', 'longhorn-system') or
                any(o['metadata']['name'].endswith('.longhorn.io') for o in lab.get('crds')['items'])):
            raise ValueError('post-uninstall backup verification requires proven native removal')
        lab.core_identity()
    if phase == 'source-removed' and (
            lab.get('volumes.longhorn.io', expected['volume'], 'longhorn-system') or
            any(p.get('spec', {}).get('csi', {}).get('volumeHandle') == expected['volume']
                for p in lab.get('pv')['items'])):
        raise ValueError('backup retention requires the source volume and referring PVs removed')
    observed = store.backup_receipt(expected['volume'], expected['backup'], expected['size'])
    volume_config = store.object_digest(row['volume_config_key'])
    if observed != expected or volume_config != row['volume_config_digest']:
        diagnostic = lab.DIRECTORY / ('longhorn-retention-readback-failure-' + secrets.token_hex(12) + '.json')
        lab.write_private(diagnostic, {'phase': phase, 'expected': row,
                                     'observed': observed, 'volume_config_digest': volume_config})
        raise ValueError('retained native backup configuration or referenced objects changed')
    print('LONGHORN_UNINSTALL_BACKUP_RETENTION_OBSERVED phase=' + phase + ' ' +
          json.dumps(row, sort_keys=True), flush=True)
    return row


def retain_backup(lab, journal):
    """Retire this test fixture's native target before ordinary data cleanup.

    An empty product backup URL intentionally does not clear an existing target.
    This is explicit fixture cleanup, not proof of a product disconnect action.
    """
    store = lab.backup_store(); fixture = store.receipt()
    backups = lab.load_helper('longhorn-backups.py')
    path = store.directory / 'retained-backup.json'
    if path.exists() or path.is_symlink() or len(journal.get('backups', {})) != 1:
        raise ValueError('backup retention requires one new full backup and no prior receipt')
    name, backup = next(iter(journal['backups'].items()))
    expected = backup.get('object_receipt', {})
    binding = fixture.get('product_binding', {})
    config = lab.yaml.safe_load((lab.DIRECTORY / 'config.yml').read_text())
    if (expected.get('volume') != journal['volume'] or expected.get('backup') != name or
            expected.get('size') != 2 * 1024**3 or expected.get('incremental') is not False or
            not expected.get('objects') or not backup.get('uid') or not binding or
            config.get('longhorn_backup_target') or config.get('longhorn_backup_secret') or
            config.get('longhorn_requests') or config.get('longhorn_action') != 'present'):
        raise ValueError('backup retention requires the original empty target declaration and verified full backup')
    resources = {kind: lab.get(kind, namespace='longhorn-system')['items']
                 for kind in backups.BACKUP_RESOURCES}
    native = resources['backups.longhorn.io']
    volumes = resources['backupvolumes.longhorn.io']
    volume = journal.get('backup_volume', {})
    target = lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
    if (len(native) != 1 or native[0]['metadata']['name'] != name or
            native[0]['metadata']['uid'] != backup['uid'] or
            len(volumes) != 1 or volumes[0]['metadata']['name'] != volume.get('name') or
            volumes[0]['metadata']['uid'] != volume.get('uid') or
            resources['backupbackingimages.longhorn.io'] or resources['systembackups.longhorn.io'] or
            not target or target['metadata']['uid'] != binding['target_uid'] or
            not target.get('status', {}).get('available') or
            backups.target_configuration(target['spec']) != backups.target_configuration(binding['fixture_spec'])):
        raise ValueError('backup retention refuses foreign native resources or a changed target')
    key = backups.volume_prefix(journal['volume']) + 'volume.cfg'
    row = {'run_id': lab.owner()['run_id'], 'cluster_uid': lab.owner()['cluster_uid'],
           'bucket': fixture['bucket'], 'source_volume_uid': journal['volume_uid'],
           'backup_uid': backup['uid'], 'target_uid': binding['target_uid'],
           'object_receipt': copy.deepcopy(expected), 'volume_config_key': key,
           'volume_config_digest': store.object_digest(key)}
    lab.write_private(path, row)
    verify_retained_backup(lab, phase='connected')
    original = dict(binding['original_spec'])
    if 'syncRequestedAt' in target['spec']:
        original['syncRequestedAt'] = target['spec']['syncRequestedAt']
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': binding['target_uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': target['metadata']['resourceVersion']},
             {'op': 'test', 'path': '/spec', 'value': target['spec']},
             {'op': 'replace', 'path': '/spec', 'value': original}]
    lab.kubectl('-n', 'longhorn-system', 'patch', 'backuptargets.longhorn.io', 'default',
                '--type=json', '--patch-file=/dev/stdin', stdin=json.dumps(patch))

    def disconnected():
        current = lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
        if (not current or current['metadata']['uid'] != binding['target_uid'] or
                backups.target_configuration(current['spec']) != backups.target_configuration(binding['original_spec'])):
            raise ValueError('backup target identity or declaration changed during disconnect')
        return not current.get('status', {}).get('available') and not any(
            lab.get(kind, namespace='longhorn-system')['items'] for kind in resources)

    lab.wait('uninstall-backup-target-disconnected', 600, disconnected)
    verify_retained_backup(lab, phase='disconnected')
    print('LONGHORN_UNINSTALL_BACKUP_TARGET_DISCONNECTED_PASS', flush=True)


def uninstall(lab, relay_pid):
    """Run nonempty guards, then empty-store native removal with real backups."""
    lab.business('uninstall_refusal', relay_pid)
    lab.backup_suite(relay_pid, 'uninstall_backup', uninstall=True)
    print('LONGHORN_UNINSTALL_BUSINESS_PASS', flush=True)

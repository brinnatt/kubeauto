#!/usr/bin/env python3
"""Accept only the real LONGHORN-52 identity transition, preserving old guards."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

import yaml

BASE = Path('/usr/local/kubeauto')
NS = 'longhorn-system'
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
PREVIOUS = 'tests/evidence/longhorn-v2-im-revision-2-adopt-qualification-20261021.json'
EVIDENCE = 'tests/evidence/longhorn-v2-upgrade-lifecycle-20261021.json'
TAG = 'v1.12.1-kubeauto.2'
POINTS = {'plain-before', 'encrypted-before', 'plain-after', 'encrypted-after'}
INPUTS = ('roles/longhorn/files/longhorn.py', 'roles/longhorn/files/block-check.py',
    'roles/longhorn/files/images.json', 'tests/helpers/longhorn-lab.py',
    'tests/helpers/longhorn-v2-lab.py', 'tests/helpers/longhorn-v2-core.py',
    'tests/helpers/longhorn-v2-install.py', 'tests/helpers/longhorn-upgrade-lifecycle.py',
    'tests/helpers/longhorn-v2-upgrade-prepare.py', 'tests/helpers/longhorn-v2-upgrade-lifecycle.sh',
    'tests/helpers/longhorn-v2-upgrade-baseline.py', 'tests/helpers/longhorn-v2-linked-lifecycle.py')


def target_tag():
    path = BASE/'roles/longhorn/files/images.json'
    value = json.loads(path.read_text())['1.12.1']['longhorn-instance-manager']
    if value not in (TAG, 'v1.12.1-kubeauto.3'):
        raise ValueError('upgrade baseline requires an explicitly supported immutable IM pin')
    return value


def adoption_path():
    return ('tests/evidence/longhorn-v2-im-revision-3-adopt-qualification-20261022.json'
            if target_tag() == 'v1.12.1-kubeauto.3' else PREVIOUS)


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def full_tag():
    return target_tag()


def load(name):
    spec = importlib.util.spec_from_file_location('upgrade_baseline_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('upgrade baseline requires a regular non-symlink file: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path):
    path = Path(path)
    return {'path': str(path.relative_to(BASE)), 'sha256': digest(path)}


def referenced(row):
    relative = Path(row['path'])
    if relative.is_absolute() or '..' in relative.parts: raise ValueError('upgrade baseline reference escapes the product directory')
    path = BASE/relative
    if not path.resolve().is_relative_to(BASE.resolve()) or digest(path) != row['sha256']:
        raise ValueError('upgrade baseline referenced bytes changed')
    return path


def fresh_previous():
    """Accept this run's real pinned initial install; never invent an adoption."""
    def initial_input_current(name, original):
        if digest(BASE/name) == original: return True
        if name == 'roles/longhorn/files/images.json':
            manager = load('longhorn-manager-adopt.py'); manager.BASE = BASE
            return manager.input_current(name, original)
        if name == 'tests/helpers/longhorn-v2-upgrade-baseline.py':
            revision = load('longhorn-im-revision-3-adopt.py'); revision.BASE = BASE
            return revision.gate_input_current(name, original)
        return False

    tag = full_tag()
    path = evidence_path('tests/evidence/longhorn-v2-aio-install-qualification-20261020.json')
    digest(path); row = json.loads(path.read_text())
    readback = row.get('independent_readback', {})
    baseline = readback.get('baseline', {})
    managers = readback.get('managers', {})
    if (row.get('status') != 'pass' or row.get('rc') != 0 or row.get('failure_markers') != 0 or
            row.get('durable') != {'exit': 0, 'finalized': 0} or
            row.get('signed_scope') != 'v2-aio-install-qualification' or
            row.get('version') != '1.12.1' or row.get('instance_manager_version') != tag or
            row.get('remote_input_verification', {}).get('mismatches') != 0 or
            any(not initial_input_current(name, row.get('frozen_inputs', {}).get(name)) for name in INPUTS) or
            not all(readback.get(key) is True for key in
                ('installation_healthy', 'empty_business', 'install_business_journal_absent')) or
            baseline.get('baseline_identity') != row.get('baseline_identity') or
            not row.get('baseline_identity') or
            set(row['baseline_identity']) != {'run_id', 'cluster_uid', 'namespace_uid', 'node_uids', 'raw_disks'} or
            baseline.get('native_block_receipt_sha256') != row.get('native_block_receipt_sha256') or
            not re.fullmatch('[0-9a-f]{64}', row.get('native_block_receipt_sha256', '')) or
            len(managers) != 3 or {m.get('node') for m in managers.values()} !=
                {'longhorn-node-'+address.rsplit('.', 1)[1] for address in NODES} or
            managers != {name: {key: m[key] for key in ('uid', 'node', 'image')}
                for name, m in baseline.get('managers', {}).items()} or
            any(not m.get('uid') or not m.get('image', '').endswith(':'+tag) for m in managers.values())):
        raise ValueError('fresh upgrade transition requires current independently accepted pinned initial installation')
    refs = row.get('receipt_refs', {})
    if set(refs) != {'log', 'exit', 'finalized'}:
        raise ValueError('fresh initial installation lacks original execution receipts')
    for key in ('exit', 'finalized'):
        if referenced(refs[key]).read_text().strip() != '0':
            raise ValueError('fresh initial installation execution was not finalized successfully')
    lines = referenced(refs['log']).read_text().splitlines()
    if any(re.match(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', line) for line in lines):
        raise ValueError('fresh initial installation raw log contains a failure marker')
    for marker in ('LONGHORN_V2_AIO_INSTALL_QUALIFICATION_PASS',
            'LONGHORN_V2_AIO_INSTALL_QUALIFICATION_SCOPED_CLEAN_PASS',
            'LONGHORN_INSTALL_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS'):
        if lines.count(marker) != 1:
            raise ValueError('fresh initial installation lacks a unique business/clean result')
    prefix = 'LONGHORN_FSYNC_REMOUNT_VERIFIED '
    observations = [line for line in lines if line.startswith(prefix)]
    expected = row.get('business', {})
    if (len(observations) != 1 or expected.get('bytes') != '9437184' or
            expected.get('replica_nodes') != ','.join('longhorn-node-'+a.rsplit('.', 1)[1] for a in NODES) or
            not re.fullmatch('[0-9a-f]{64}', expected.get('sha256', '')) or
            observations != [prefix+'bytes='+expected['bytes']+' sha256='+expected['sha256']+
                ' replica_nodes='+expected['replica_nodes']]):
        raise ValueError('fresh initial installation lacks its original fsync cross-node data result')
    return row


def previous():
    tag = target_tag()
    if full_material().active(): return fresh_previous()
    path = BASE/adoption_path(); digest(path); row = json.loads(path.read_text())
    readback = row.get('independent_readback', {})
    if (row.get('status') != 'pass' or row.get('rc') != 0 or row.get('failure_markers') != 0 or
            row.get('durable') != {'exit': 0, 'finalized': 0} or
            row.get('signed_scope') != ('v2-im-revision-3-adopt-qualification' if tag.endswith('.3') else 'v2-im-revision-2-adopt-qualification') or
            row.get('version') != '1.12.1' or row.get('instance_manager_version') != tag or
            row.get('remote_input_verification', {}).get('mismatches') != 0 or
            not all(readback.get(key) is True for key in ('installation_healthy', 'empty_business', 'adoption_journal_absent')) or
            readback.get('baseline', {}).get('baseline_identity') != row.get('baseline_identity') or
            not row.get('baseline_identity') or not readback.get('managers')):
        raise ValueError('upgrade transition requires independently accepted pinned adoption')
    if tag.endswith('.3'):
        revision = load('longhorn-im-revision-3-adopt.py'); revision.BASE = BASE
        original = json.loads((BASE/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').read_text())
        name = 'roles/longhorn/files/longhorn.py'
        if not revision.accepted_input_current(original, name, digest(BASE/name)):
            raise ValueError('upgrade transition requires completed exact .3 adoption and publication')
    return row


def capture(lab):
    """Read current physical/runtime identities; never grant acceptance by itself."""
    tag = target_tag()
    if tuple(lab.NODES) != NODES: raise ValueError('upgrade baseline requires the original three-source inventory')
    lab.core_identity(); lab.installation_health(); state = lab.owner()
    config_path = lab.DIRECTORY/'config.yml'; digest(config_path); config = yaml.safe_load(config_path.read_text())
    if (config.get('longhorn_version') != '1.12.1' or config.get('longhorn_data_engines') != ['v2'] or
            config.get('longhorn_install') != 'yes' or config.get('longhorn_action', 'present') != 'present' or
            config.get('longhorn_requests') or set(state.get('raw_disks', {})) != set(NODES)):
        raise ValueError('upgrade baseline requires a healthy idle pinned V2 profile')
    adopter = load('longhorn-v2-im-adopt.py'); adopter.empty(lab)
    target = lab.get('backuptargets.longhorn.io', 'default', NS)
    if not target or target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret'):
        raise ValueError('upgrade baseline retains an external backup consumer')
    for kind in ('backupvolumes.longhorn.io', 'backups.longhorn.io', 'systembackups.longhorn.io', 'systemrestores.longhorn.io'):
        if lab.get(kind, namespace=NS)['items']: raise ValueError('upgrade baseline retains data-protection objects')
    catalog = json.loads((BASE/'roles/longhorn/files/images.json').read_text())
    if catalog['1.12.1']['longhorn-instance-manager'] != tag: raise ValueError('upgrade baseline requires the published .2 image pin')
    image = config['longhorn_image_prefix']+'/longhorn-instance-manager:'+tag
    managers = adopter.managers(lab, image, engine='v2')
    for name, manager in managers.items():
        pod = lab.get('pod', name, NS)
        containers = (pod or {}).get('spec', {}).get('containers', [])
        statuses = (pod or {}).get('status', {}).get('containerStatuses', [])
        if (not pod or pod['metadata'].get('deletionTimestamp') or pod['spec'].get('nodeName') != manager['node'] or
                not any(ref.get('kind') == 'InstanceManager' and ref.get('uid') == manager['uid'] and ref.get('controller')
                    for ref in pod['metadata'].get('ownerReferences', [])) or
                len(containers) != 1 or containers[0].get('name') != 'instance-manager' or containers[0].get('image') != image or
                len(statuses) != 1 or not statuses[0].get('ready') or not statuses[0].get('state', {}).get('running') or
                not re.search(r'sha256:[0-9a-f]{64}$', statuses[0].get('imageID', ''))):
            raise ValueError('upgrade baseline IM runtime differs from its original native owner and image')
        manager.update(pod_uid=pod['metadata']['uid'], image_id=statuses[0]['imageID'])
    receipt_path = lab.DIRECTORY/'yml/longhorn/block-disks.json'; digest(receipt_path)
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get('cluster_uid') != state['cluster_uid'] or receipt.get('namespace_uid') != state['namespace_uid'] or
            set(receipt.get('nodes', {})) != set(NODES)):
        raise ValueError('upgrade baseline native block receipt belongs to another installation')
    for address, row in receipt['nodes'].items():
        native = lab.get('nodes.longhorn.io', row['name'], NS)
        if (not native or native['metadata']['uid'] != row['longhorn_node_uid'] or
                state['node_uids'].get(row['name']) != row['kubernetes_node_uid'] or len(row.get('disks', {})) != 1):
            raise ValueError('upgrade baseline native node identity changed')
        expected = state['raw_disks'][address]
        for name, disk in row['disks'].items():
            spec = native['spec']['disks'].get(name, {}); status = native['status']['diskStatus'].get(name, {})
            if (not disk.get('disk_uuid') or status.get('diskUUID') != disk['disk_uuid'] or
                    disk.get('path') != expected['path'] or disk.get('hardware_identity') != expected['identity'] or
                    disk.get('disk_driver') != 'aio' or spec.get('diskType') != 'block' or spec.get('diskDriver') != 'aio' or
                    spec.get('path') != disk['path']):
                raise ValueError('upgrade baseline raw hardware, driver or native UUID changed')
    upgrade = load('longhorn-upgrade-lifecycle.py')
    return {'baseline_identity': {key: state[key] for key in ('run_id', 'cluster_uid', 'namespace_uid', 'node_uids', 'raw_disks')},
        'native_block_receipt_sha256': digest(receipt_path), 'native_block_receipt': receipt,
        'config_sha256': digest(config_path), 'managers': managers, 'retained': upgrade.retained_identity(lab, {})}


def stable_installation(current, baseline):
    """Use the sole reviewed storage/runtime stability comparator."""
    return load('longhorn-v2-runtime-continuation.py').stable(current, baseline)


def current_baseline(lab, baseline, continuation_ref=None):
    import os
    current = capture(lab)
    if full_material().active():
        stable_installation(current, baseline)
        return baseline
    if continuation_ref is not None or 'LONGHORN_V2_RUNTIME_CONTINUATION' in os.environ:
        reader = load('longhorn-v2-runtime-continuation.py'); reader.BASE = BASE
        reader.material(continuation_ref); reader.stable(current, baseline)
        return baseline
    return current


def bind_previous(before, continuation_ref=None):
    if full_material().active():
        row = previous(); baseline = row['independent_readback']['baseline']
        stable_installation(before, baseline)
        return reference(evidence_path('tests/evidence/longhorn-v2-aio-install-qualification-20261020.json'))
    import os
    if continuation_ref is not None or 'LONGHORN_V2_RUNTIME_CONTINUATION' in os.environ:
        reader = load('longhorn-v2-runtime-continuation.py'); reader.BASE = BASE
        return reader.bind_before(before, continuation_ref)
    row = previous(); identity = before['baseline_identity']; baseline = row['independent_readback']['baseline']
    if (any(identity.get(key) != value for key, value in row['baseline_identity'].items()) or
            baseline.get('native_block_receipt_sha256') != before['native_block_receipt_sha256'] or
            {name: {key: manager[key] for key in ('uid', 'node', 'image')} for name, manager in before['managers'].items()} !=
            {name: {key: manager[key] for key in ('uid', 'node', 'image')}
                for name, manager in row['independent_readback']['managers'].items()}):
        raise ValueError('upgrade transition before-state differs from the accepted pinned installation')
    return reference(evidence_path('tests/evidence/longhorn-v2-aio-install-qualification-20261020.json')
        if full_material().active() else BASE/adoption_path())


def validate_transition(transition):
    tag = target_tag()
    before, after = transition['before'], transition['after']
    if transition['previous_evidence'] != bind_previous(before, transition['previous_evidence'].get('runtime_continuation')): raise ValueError('upgrade transition previous evidence SHA changed')
    old, new = before['baseline_identity'], after['baseline_identity']
    if (any(old[key] != new[key] for key in ('run_id', 'cluster_uid', 'node_uids', 'raw_disks')) or
            old['namespace_uid'] == new['namespace_uid'] or before['config_sha256'] != after['config_sha256']):
        raise ValueError('upgrade transition lost core/hardware/config identity or lacks a real namespace transition')
    prior, current = before['native_block_receipt'], after['native_block_receipt']
    if (prior.get('cluster_uid') != old['cluster_uid'] or current.get('cluster_uid') != new['cluster_uid'] or
            prior.get('namespace_uid') != old['namespace_uid'] or current.get('namespace_uid') != new['namespace_uid'] or
            {row['longhorn_node_uid'] for row in prior['nodes'].values()} &
            {row['longhorn_node_uid'] for row in current['nodes'].values()}):
        raise ValueError('upgrade transition reused native node identities or mismatched their namespaces')
    for address in NODES:
        a, b = prior['nodes'][address], current['nodes'][address]
        if (a['longhorn_node_uid'] == b['longhorn_node_uid'] or a['name'] != b['name'] or
                a['kubernetes_node_uid'] != b['kubernetes_node_uid'] or a['disks'] != b['disks']):
            raise ValueError('upgrade transition did not recreate native nodes on the same original raw lvstores')
    old_managers, new_managers = before['managers'], after['managers']
    expected_nodes = {'longhorn-node-'+address.rsplit('.', 1)[1] for address in NODES}
    for rows in (old_managers, new_managers):
        if (len(rows) != 3 or {m['node'] for m in rows.values()} != expected_nodes or
                any(not m['image'].endswith(':'+tag) or not m.get('pod_uid') or not m.get('image_id') for m in rows.values())):
            raise ValueError('upgrade transition manager image or exact original domain set differs')
    for key in ('uid', 'pod_uid'):
        if {m[key] for m in old_managers.values()} & {m[key] for m in new_managers.values()}:
            raise ValueError('upgrade transition retained an old installation manager identity')
    preparation = json.loads(referenced(transition['preparation']).read_text())
    result = json.loads(referenced(transition['upgrade_result']).read_text())
    if (preparation.get('stage') != 'complete' or preparation.get('baseline_before') != before or
            preparation.get('baseline_after') != after or preparation.get('previous_evidence') != transition['previous_evidence'] or
            preparation.get('source_namespace_uid') != new['namespace_uid'] or result.get('stage') != 'complete' or
            result.get('target_identity') != preparation['target_identity']['retained'] or
            preparation['source_core'] != {key: new[key] for key in ('run_id', 'cluster_uid', 'node_uids', 'raw_disks')}):
        raise ValueError('upgrade transition lacks its exact completed preparation, source and independent target result')
    # Keep the original pre-upgrade source receipt. The owning upgrade reader
    # admits only the pinned patch's two new CRDs, with every old UID intact.
    load('longhorn-upgrade-lifecycle.py').verify_retained(
        after['retained'], result.get('source_identity', {}), result.get('new_runtime', {}))
    legacy = result.get('legacy', {}); origin = legacy.get('legacy_origin', {})
    restores = result.get('restores', {})
    expected_origin = {'created_version': '1.12.0', 'upgraded_version': '1.12.1',
        'volume_name': legacy.get('volume', {}).get('metadata', {}).get('name'),
        'volume_uid': legacy.get('volume', {}).get('metadata', {}).get('uid'),
        'source_volume': legacy.get('source_volume'), 'source_volume_uid': legacy.get('source_volume_uid')}
    if (not legacy.get('refusal_verified') or not legacy.get('retired') or origin.get('created_version') != '1.12.0' or
            origin != expected_origin or any(not value for value in expected_origin.values()) or
            set(restores) != POINTS or len({r.get('volume_uid') for r in restores.values()}) != 4 or
            any(not r.get('remount') or not re.fullmatch('[0-9a-f]{64}', r.get('confirmed_sha256', '')) or
                not re.fullmatch('[0-9a-f]{64}', r.get('independent_write_sha256', '')) for r in restores.values())):
        raise ValueError('upgrade transition lacks real old legacy refusals or four independent protected restores')
    return after, result


def verify_execution(evidence, transition, result):
    refs = evidence['receipt_refs']
    if set(refs) != {'log', 'exit', 'finalized'}: raise ValueError('upgrade acceptance lacks exact execution receipt set')
    for key in ('exit', 'finalized'):
        if referenced(refs[key]).read_text().strip() != '0': raise ValueError('upgrade acceptance has nonzero durable exit/finalized state')
    lines = referenced(refs['log']).read_text().splitlines()
    if any(re.match(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)', line) for line in lines):
        raise ValueError('upgrade acceptance raw log contains a failure marker')
    for marker in ('LONGHORN_V2_UPGRADE_PASS', 'LONGHORN_V2_UPGRADE_GATE_PASS', 'LONGHORN_V2_UPGRADE_GATE_SCOPED_CLEAN_PASS',
            'LONGHORN_V2_LEGACY_LINKED_REFUSALS_PASS count=4 real_upgrade_fixture=true'):
        if lines.count(marker) != 1: raise ValueError('upgrade acceptance raw log lacks a unique terminal or legacy marker')
    for prefix, expected in (('LONGHORN_V2_UPGRADE_BASELINE_TRANSITION ', transition),
                             ('LONGHORN_V2_UPGRADE_LEGACY_VERIFIED ', result['legacy']['legacy_origin'])):
        matches = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
        if matches != [expected]: raise ValueError('upgrade acceptance log differs from its exact native transition or legacy origin')
    prefix = 'LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED '
    restores = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    if evidence.get('checkpoint_execution'):
        restores += verify_checkpoint(evidence, result, lines)
    if (len(restores) != 4 or {row['point'] for row in restores} != POINTS or
            any({key: value for key, value in row.items() if key != 'point'} != result['restores'][row['point']] for row in restores)):
        raise ValueError('upgrade acceptance log lacks its four exact independently verified restore results')


def verify_checkpoint(evidence, result, current_lines):
    """Bind a failed pre-legacy parent to its exact resumed rollout journal.

    The parent's real rc=1 and failure markers remain visible. Its two completed
    restore points never become a fabricated successful parent or replayed log.
    """
    checkpoint = evidence['checkpoint_execution']
    if set(checkpoint) != {'log', 'exit', 'finalized', 'journal', 'diagnostic', 'inputs'}:
        raise ValueError('upgrade checkpoint has an unexpected receipt set')
    for name in ('exit', 'finalized'):
        if referenced(checkpoint[name]).read_text().strip() != '1':
            raise ValueError('upgrade checkpoint must preserve its real failed durable status')
    diagnostic = referenced(checkpoint['diagnostic']).read_text()
    if ('in legacy_refusals' not in diagnostic or
            not diagnostic.rstrip().endswith('ValueError: legacy native admission returned the wrong result: cannot expand legacy linked-clone volumes')):
        raise ValueError('upgrade checkpoint is not the diagnosed legacy admission gate failure')
    journal_path = referenced(checkpoint['journal']); prior = json.loads(journal_path.read_text())
    if (result.get('resume_from_journal_sha256') != digest(journal_path) or
            prior.get('stage') != 'upgrade-starting' or prior.get('resume_from_journal_sha256') or
            not prior.get('id') or prior.get('old_runtime', {}).get('version') != '1.12.0' or
            prior.get('new_runtime', {}).get('version') != '1.12.1' or
            prior.get('successful_configuration', {}).get('longhorn_version') != '1.12.1' or
            prior.get('legacy', {}).get('created_version') != '1.12.0' or
            prior.get('legacy', {}).get('refusal_verified') or
            set(prior.get('restores', {})) != {'plain-before', 'encrypted-before'} or
            set(prior.get('points', {})) != {'plain-before', 'encrypted-before'} or
            any(prior.get(key) != result.get(key) for key in
                ('id', 'source_identity', 'target_identity', 'old_runtime', 'new_runtime', 'retained_before', 'system')) or
            any(prior['restores'][key] != result['restores'][key] or prior['points'][key] != result['points'][key]
                for key in ('plain-before', 'encrypted-before'))):
        raise ValueError('upgrade checkpoint does not bind the unchanged original rollout and restores')
    revision = load('longhorn-im-revision-3-adopt.py'); revision.BASE = BASE
    pins = json.loads(referenced(checkpoint['inputs']).read_text())
    for name in INPUTS:
        old = pins.get(name); actual = digest(BASE/name)
        if old != actual and not revision.gate_input_current(name, old):
            raise ValueError('upgrade checkpoint changed an unreviewed owning input: '+name)
    marker = 'LONGHORN_V2_UPGRADE_RESUMED_FROM_CHECKPOINT sha256='+digest(journal_path)
    if current_lines.count(marker) != 1:
        raise ValueError('upgrade continuation lacks its original journal handoff')
    lines = referenced(checkpoint['log']).read_text().splitlines()
    failures = [line for line in lines if line.startswith(
        'LONGHORN_FIRST_FAILURE phase=manager-engine-upgrade-and-isolated-recovery rc=1 diagnostics=')]
    if (lines.count('LONGHORN_GATE_EXIT rc=1') != 1 or
            len(failures) != 1 or
            any(line == 'LONGHORN_V2_UPGRADE_PASS' or line.startswith('LONGHORN_V2_UPGRADE_LEGACY_VERIFIED ') for line in lines)):
        raise ValueError('upgrade checkpoint original failure boundary changed')
    prefix = 'LONGHORN_V2_UPGRADE_ISOLATED_RESTORE_VERIFIED '
    restores = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    if (len(restores) != 2 or {row['point'] for row in restores} != {'plain-before', 'encrypted-before'} or
            any({key: value for key, value in row.items() if key != 'point'} != prior['restores'][row['point']]
                for row in restores)):
        raise ValueError('upgrade checkpoint lacks its two real old-version independent restores')
    return restores


def accepted_evidence():
    """One strict offline 52 reader; live consumers separately bind current state."""
    path = evidence_path(EVIDENCE); digest(path); row = json.loads(path.read_text())
    revision = load('longhorn-im-revision-3-adopt.py'); revision.BASE = BASE
    if (row.get('status') != 'pass' or row.get('rc') != 0 or row.get('failure_markers') != 0 or
            row.get('durable') != {'exit': 0, 'finalized': 0} or row.get('signed_scope') != 'v2-upgrade-lifecycle' or
            row.get('version') != '1.12.1' or row.get('remote_input_verification', {}).get('mismatches') != 0 or
            any(row.get('frozen_inputs', {}).get(name) != digest(BASE/name) and
                not revision.gate_input_current(name, row.get('frozen_inputs', {}).get(name)) for name in INPUTS)):
        raise ValueError('upgrade baseline requires accepted current frozen 52 evidence and durable rc=0')
    baseline, result = validate_transition(row['transition'])
    verify_execution(row, row['transition'], result)
    readback = row.get('independent_readback', {})
    if (not all(readback.get(key) is True for key in ('installation_healthy', 'empty_business', 'transition_journal_absent')) or
            readback.get('baseline') != baseline):
        raise ValueError('upgrade baseline current native/runtime identity differs from the independently accepted 52 transition')
    return row, baseline, result


def accepted(lab):
    """Preserve the existing live current-state check after offline acceptance."""
    row, baseline, _ = accepted_evidence()
    if current_baseline(lab, baseline, row['transition']['previous_evidence'].get('runtime_continuation')) != baseline:
        raise ValueError('upgrade baseline current native/runtime identity differs from the independently accepted 52 transition')
    print('LONGHORN_V2_UPGRADE_BASELINE_VERIFIED old_adoption_sha256='+row['transition']['previous_evidence']['sha256'], flush=True)
    return baseline

def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('mode', choices=('capture', 'accepted'))
    args = parser.parse_args(); install = load('longhorn-v2-install.py'); _, lab = install.fixture(); lab.require_lease()
    value = capture(lab) if args.mode == 'capture' else accepted(lab)
    print('LONGHORN_V2_UPGRADE_BASELINE_READBACK '+json.dumps(value, sort_keys=True), flush=True)


if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        traceback.print_exc(); sys.exit(1)

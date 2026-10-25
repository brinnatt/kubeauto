#!/usr/bin/env python3
"""LONGHORN-25: independent V1 topology on the four released same-subnet hosts.

The normal six-node V1 fixture is never changed. Three co-located control/data
nodes provide the original replicas; a fourth host exercises real add-node and
evacuation. All four are admitted by original retirement receipts and fresh
stable-device checks. No cross-subnet Kubernetes connection is required.
"""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import uuid

import yaml

BASE = Path('/usr/local/kubeauto')
CLUSTER = 'longhorn-node-gate'
NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')
RECEIVER = '192.168.47.149'


def load(filename):
    spec = importlib.util.spec_from_file_location('local25_'+filename.replace('-', '_'), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def base_fixture():
    profile = load('longhorn-v2-lab.py')
    lab = load('longhorn-lab.py')
    lab.NODES = lab.WORKERS = NODES
    lab.CLUSTER = CLUSTER
    lab.DIRECTORY = BASE/'clusters'/CLUSTER
    lab.STATE = lab.DIRECTORY/'longhorn-lab-owner.json'
    original_inventory = lab.inventory
    lab.inventory = lambda: original_inventory().replace('clusters/longhorn-gate', 'clusters/'+CLUSTER)
    # Same node addresses and proven Python transport as V2, separate sockets
    # and ownership. The focused run checks the new listeners before setup.
    lab.relay_probe = lambda node: profile.relay_probe(lab, node)
    lab.core_identity = lambda: load('longhorn-v2-core.py').core_identity(lab)
    return lab


def lifecycle_module():
    module = load('longhorn-node-lifecycle.py')
    module.HOST = RECEIVER
    module.NAME = 'longhorn-node-149'
    module.__file__ = str(Path(__file__).resolve())
    original_load = module.load
    def receiving_dependency(filename):
        dependency = original_load(filename)
        if filename == 'longhorn-v2-pool.py':
            dependency.HOSTS = (RECEIVER,)
        return dependency
    module.load = receiving_dependency
    original_fixture = module.fixture
    module.fixture = lambda: original_fixture(base_fixture())
    # Target retirement has its own real receipt schema and hashed logs.
    module.release_proof = load('longhorn-v2-disk-lifecycle.py').release_proof
    return module


def admit_released_disk(proof, collected, address, run_id):
    inv, assessment = collected['inventory'], collected['assessment']
    if collected['host'] != address or assessment['blockers'] or len(assessment['candidates']) != 1:
        raise ValueError('same-subnet host is not exclusively clean')
    candidate, original = assessment['candidates'][0], proof['original_raw_disk']
    if (proof['host'] != address or inv['machine_id'] != original['machine_id'] or
            candidate['identity'] != original['identity'] or candidate['size_bytes'] != original['size_bytes'] or
            original['path'] not in candidate['by_id'] or str(inv['hugepages_2mi']) != '0'):
        raise ValueError('same-subnet released disk, host or page pool changed')
    identity = str(uuid.uuid5(uuid.UUID(run_id), address))
    return {'kernel': '/dev/'+candidate['name'], 'device': original['path'],
        'identity': candidate['identity'], 'size': candidate['size_bytes'], 'uuid': identity,
        'machine_id': inv['machine_id'], 'path': '/data/kubeauto-longhorn', 'phase': 'planned',
        'fstab_line': 'UUID='+identity+' /data/kubeauto-longhorn ext4 defaults 0 2 # kubeauto-longhorn-lab:'+run_id}


def read_source_proof(path, address):
    # Reuse the original source receipt verifier with its exact host scope;
    # never infer a release from matrix status or an arbitrary resource name.
    module = load('longhorn-node-lifecycle.py')
    module.HOST = address
    return module.release_proof(path)


def configure(source_directory, target_proof):
    lab = base_fixture(); lab.require_lease()
    if lab.STATE.exists() or lab.STATE.is_symlink(): raise ValueError('same-subnet core cannot adopt an owner')
    for cluster in ('longhorn-v2-gate', 'longhorn-v2-dr-target'):
        if (BASE/'clusters'/cluster).exists(): raise ValueError('V2 owners must retire before same-subnet V1 admission')
    collected = {}; proofs = {}
    pool = load('longhorn-v2-pool.py')
    pool.HOSTS = (*NODES, RECEIVER)
    for address in NODES:
        path = Path(source_directory)/('longhorn-v2-source-released-'+address+'.json')
        proofs[address] = read_source_proof(path, address)
        collected[address] = pool.collect(address)
        admit_released_disk(proofs[address], collected[address], address, str(uuid.uuid4()))
    target = load('longhorn-v2-disk-lifecycle.py').release_proof(target_proof)
    admit_released_disk(target, pool.collect(RECEIVER), RECEIVER, str(uuid.uuid4()))
    lab.configure()
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_text())
    config.update(ENABLE_SETTING_HOSTNAME=True, longhorn_data_engines=['v1'])
    (lab.DIRECTORY/'config.yml').write_text(yaml.safe_dump(config, sort_keys=False))
    state = lab.owner()
    state.update(storage_nodes=list(NODES), released_source=proofs, target_proof=str(target_proof),
        planned_disks={address: admit_released_disk(proofs[address], collected[address], address, state['run_id'])
                       for address in NODES},
        raw_disks={address: dict(proofs[address]['original_raw_disk'],
            original_hostname=collected[address]['inventory']['hostname']) for address in NODES},
        local25_multipath={})
    lab.write_private(lab.STATE, state)
    print('LONGHORN_NODE_LOCAL_CORE_ADMITTED nodes=3 receiving='+RECEIVER, flush=True)


def prepare_storage(lab):
    lab.core_identity(); state = lab.owner()
    if state['disks'] or state.get('namespace_uid'): raise ValueError('same-subnet storage requires fresh core')
    install = load('longhorn-v2-install.py')
    # Admit all service identities before stopping the first unused service.
    if state['local25_multipath']: raise ValueError('same-subnet multipath is not an adoption entry')
    for address in NODES:
        state['local25_multipath'][address] = {'original': install.multipath(lab, address, 'probe'), 'phase': 'planned'}
    lab.write_private(lab.STATE, state)
    for address, entry in state['local25_multipath'].items():
        entry['phase'] = 'stop-intent'; lab.write_private(lab.STATE, state)
        entry['current'] = install.multipath(lab, address, 'stop', expected=entry['original'])
        entry['phase'] = 'stopped'; lab.write_private(lab.STATE, state)
    lab.capture_hosts()
    plan = lab.DIRECTORY/'node-local-disks.json'
    lab.write_private(plan, state['planned_disks'])
    lab.disk_plan = lambda raw, run_id: checked_disk_plan(lab, raw, run_id)
    lab.prepare_disks(plan)
    lab.prepare_credentials()
    print('LONGHORN_NODE_LOCAL_STORAGE_PREPARED', flush=True)


def checked_disk_plan(lab, raw, run_id):
    state = lab.owner()
    if run_id != state['run_id'] or json.loads(raw) != state['planned_disks']:
        raise ValueError('same-subnet disk input differs from admitted plan')
    return copy.deepcopy(state['planned_disks'])


def restore_multipath(lab):
    state = lab.owner(); install = load('longhorn-v2-install.py')
    if state['disks'] or state.get('namespace_uid'): raise ValueError('same-subnet storage must retire before service restoration')
    for address, entry in state['local25_multipath'].items():
        if entry['phase'] not in ('planned', 'stopped', 'restored'):
            raise ValueError('uncertain same-subnet multipath operation')
        entry['current'] = install.multipath(lab, address, 'restore',
            expected=entry.get('current', entry['original']), original=entry['original'])
        entry['phase'] = 'restored'; lab.write_private(lab.STATE, state)


def main():
    # Lifecycle commands retain the original parser, journaling and supervisor;
    # only their explicitly scoped fixture and receiving-host identities differ.
    actions = {'configure', 'core-setup', 'prepare-storage', 'storage-setup', 'retire-storage', 'retire-core'}
    if not any(action in sys.argv[1:] for action in actions):
        module = lifecycle_module()
        if len(sys.argv) == 2 and sys.argv[1] in ('relay-ensure', 'relay-maintain', 'relay-clean'):
            lab = module.fixture(); lab.require_lease(); lab.owner()
            {'relay-ensure': lab.relay_ensure, 'relay-maintain': lab.relay_maintain,
             'relay-clean': lab.relay_cleanup}[sys.argv[1]]()
        else: module.main()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=sorted(actions))
    parser.add_argument('--source-directory'); parser.add_argument('--target-proof')
    parser.add_argument('--relay-pid', type=int)
    args = parser.parse_args()
    if args.action == 'configure':
        if not args.source_directory or not args.target_proof: parser.error('both original release inputs required')
        configure(args.source_directory, args.target_proof); return
    lab = base_fixture(); lab.require_lease(); lab.owner()
    module = lifecycle_module()
    # The original product supervisor checks this wrapper's exact relay process.
    def supervise(pid, step, expected_refusal=None):
        if step != 'longhorn-setup': raise ValueError('unexpected same-subnet storage product step')
        return module.supervise(lab, pid, step, expected_refusal=expected_refusal)
    lab.supervise_core = supervise
    if args.action == 'core-setup':
        core = load('longhorn-v2-core.py'); core.__file__ = str(Path(__file__).resolve())
        core.supervise(lab, args.relay_pid, 'download')
        core.supervise(lab, args.relay_pid, 'setup')
        lab.core_identity()
    elif args.action == 'prepare-storage':
        lab.cache_artifacts()
        prepare_storage(lab)
    elif args.action == 'storage-setup': module.supervise(lab, args.relay_pid, 'longhorn-setup'); lab.installation_health()
    elif args.action == 'retire-storage':
        if (lab.DIRECTORY/module.RECORD).exists(): raise ValueError('receiving lifecycle must finalize before base retirement')
        if lab.owner().get('namespace_uid'): lab.recover_installation(args.relay_pid)
        elif lab.owner()['disks']: lab.cleanup_disks()
        if lab.owner().get('host_state'): lab.restore_hosts()
        restore_multipath(lab)
        print('LONGHORN_NODE_LOCAL_STORAGE_RETIRED', flush=True)
    elif args.action == 'retire-core': load('longhorn-v2-core.py').cleanup(lab)


if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, TypeError, OSError, TimeoutError, subprocess.SubprocessError) as error:
        print('LONGHORN_NODE_LOCAL_FAIL reason='+str(error), file=sys.stderr, flush=True)
        sys.exit(1)

#!/usr/bin/env python3
"""LONGHORN-51: native PDB refusal and planned V2 workload relocation.

The shared maintenance algorithm retains its V1 defaults. This explicit private
profile binds V2 class/engine, original raw disks and its own recovery journals.
It does not sign node expansion or the complete runtime-resource matrix.
"""
import argparse
from pathlib import Path
import signal

import yaml

from importlib.util import spec_from_file_location, module_from_spec


def load(name):
    spec = spec_from_file_location('v2_drain_'+name.replace('-', '_'), Path(__file__).with_name(name))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def profile(lab):
    config = yaml.safe_load((lab.DIRECTORY/'config.yml').read_bytes())
    if config.get('longhorn_data_engines') != ['v2'] or config.get('longhorn_requests'):
        raise ValueError('V2 planned drain requires its idle V2-only configuration')
    candidates = [c['name'] for c in config['longhorn_storage_classes']
        if c.get('data_engine') == 'v2' and c['replicas'] == 3 and c['reclaim_policy'] == 'Retain'
        and not c['encrypted'] and c.get('clone_mode', 'full-copy') == 'full-copy']
    if not candidates:
        raise ValueError('V2 planned drain has no explicit three-replica Retain class')
    storage = lab.get('storageclass', candidates[0])
    if (not storage or storage.get('provisioner') != 'driver.longhorn.io'
            or storage.get('parameters', {}).get('dataEngine') != 'v2'
            or storage.get('parameters', {}).get('numberOfReplicas') != '3'
            or storage.get('reclaimPolicy') != 'Retain'):
        raise ValueError('V2 planned drain class differs from product configuration')
    module = load('longhorn-maintenance.py')
    module.RECORD = 'longhorn-v2-maintenance-drain.json'
    module.BUSINESS = 'longhorn-v2-maintenance-drain-business.json'
    module.DATA_ENGINE = 'v2'; module.STORAGE_CLASS = candidates[0]
    prepare = module.prepare
    def prepare_v2(lab, row):
        business, create = prepare(lab, row)
        native = lab.get('volumes.longhorn.io', business['volume'], 'longhorn-system')
        if native['spec'].get('dataEngine') != 'v2':
            raise ValueError('planned V2 drain selected another volume engine')
        lab.wait('v2-maintenance-qualified-raw-replicas', 600, lambda: lab.healthy_replicas(business['volume']))
        return business, create
    module.prepare = prepare_v2
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('business', 'clean', 'finish'))
    parser.add_argument('--relay-pid', type=int, default=0)
    args = parser.parse_args()
    install = load('longhorn-v2-install.py'); _, lab = install.fixture(); lab.require_lease()
    module = profile(lab)
    if args.action == 'business':
        module.business(lab, args.relay_pid)
        print('LONGHORN_V2_MAINTENANCE_DRAIN_BUSINESS_PASS', flush=True)
    elif args.action == 'clean':
        module.clean(lab, args.relay_pid, failed=True)
    else:
        module.finish(lab)
        install.health(lab)
        print('LONGHORN_V2_MAINTENANCE_DRAIN_FIXTURES_CLEAN_PASS', flush=True)


if __name__ == '__main__':
    def interrupted(signum, frame): raise SystemExit(128+signum)
    signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
    main()

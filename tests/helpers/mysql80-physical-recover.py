#!/usr/bin/env python3
"""Resume one failed native-host branch through the original physical runner."""
import argparse
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('mysql80_recovery', Path(__file__).with_name('mysql80-physical-live.py'))
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def admit_failed_branch(directory, host):
    directory = directory.resolve()
    if directory.parent != (GATE.ROOT/'logs').resolve() or not directory.name.startswith('mysql80-full-'+host+'-'):
        raise ValueError('MYSQL80_RECOVERY_SCOPE_REFUSED')
    plan = json.loads((directory/'plan.json').read_text())
    if plan['stage'] != 'full' or plan['os'] != GATE.HOSTS[host][1] or plan['inputs'] != GATE.inputs():
        raise ValueError('MYSQL80_RECOVERY_INPUTS_REFUSED')
    for suffix in ('exit', 'finalized'):
        if (directory/('state.'+suffix)).read_text().strip() == '0':
            raise ValueError('MYSQL80_RECOVERY_NONFAILED_REFUSED')
    if 'MYSQL80_CAPACITY_REFUSED' not in (directory/'console.log').read_text():
        raise ValueError('MYSQL80_RECOVERY_CAUSE_REFUSED')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--failed-run', required=True, type=Path)
    parser.add_argument('--host', required=True, choices=sorted(GATE.HOSTS))
    args = parser.parse_args()
    admit_failed_branch(args.failed_run, args.host)
    images, runtime = GATE.admit('full')
    GATE.qualify()
    # The original launcher owns the shared control leases. This repair touches
    # only the failed native host, whose two remote locks are acquired by the
    # original remote() implementation. It never starts the healthy peer.
    record = GATE.launch_host(args.host, 'full', images, runtime)
    GATE.verify_host(record, 'full', args.host)
    print('MYSQL80_RECOVERY_HOST_PASS host='+args.host, flush=True)


if __name__ == '__main__':
    main()

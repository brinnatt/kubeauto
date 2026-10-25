#!/usr/bin/env python3
"""Exact .1 -> .2 IM adoption profile; reuse the accepted adoption lifecycle.

The reviewed plan authorizes exact input transitions, never arbitrary hash
exceptions. Historical .1 evidence is read-only. setup09 and the existing
fsync/remount/cleanup implementation remain the only mutation path.
"""
from contextlib import contextmanager
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import signal
import sys
import tempfile
import types

BASE = Path('/usr/local/kubeauto')
PLAN = 'tests/fixtures/longhorn-im-revision-2-plan.json'
OLD_TAG = 'v1.12.1-kubeauto.1'
NEW_TAG = 'v1.12.1-kubeauto.2'
DRIVER = 'tests/helpers/longhorn-im-revision-adopt.py'
ADOPTER = 'tests/helpers/longhorn-v2-im-adopt.py'
SOURCE = 'roles/longhorn/files/longhorn.py'
CATALOG = 'roles/longhorn/files/images.json'
ENGINES = ('v1', 'v2', 'v2-dr-target')
CORE_INPUTS = (SOURCE, CATALOG, 'roles/longhorn/files/block-check.py',
               'roles/longhorn/files/block-retire.py',
               'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py',
               'tests/helpers/longhorn-v2-core.py', 'tests/helpers/longhorn-v2-install.py',
               'tests/helpers/longhorn-v2-dr-target.py',
               'tests/helpers/longhorn-v2-dr-target-install.py',
               'tests/helpers/longhorn-v2-upgrade-baseline.py',
               'tests/helpers/longhorn-v2-im-adopt.sh', ADOPTER, DRIVER)


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('revision input must be a regular non-symlink file: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(row):
    path = Path(row['path'])
    if path.is_absolute() or '..' in path.parts or digest(BASE/path) != row['sha256']:
        raise ValueError('revision evidence reference differs from its frozen SHA256')
    return json.loads((BASE/path).read_text())


def read_plan(engine):
    if engine not in ENGINES:
        raise ValueError('unsupported IM revision profile')
    plan_sha = digest(BASE/PLAN)
    plan = json.loads((BASE/PLAN).read_text())
    if plan.get('schema') != 1 or plan.get('old_tag') != OLD_TAG or plan.get('new_tag') != NEW_TAG:
        raise ValueError('revision requires the reviewed exact .1 -> .2 plan')
    profile = plan['profiles'][engine]
    expected = 'tests/evidence/longhorn-'+engine+'-im-adopt-qualification-20261021.json'
    if profile['previous_evidence']['path'] != expected:
        raise ValueError('revision profile references another installation qualification')
    prior = reference(profile['previous_evidence'])
    readback = prior.get('independent_readback', {})
    if (prior.get('status') != 'pass' or prior.get('rc') != 0 or prior.get('failure_markers') != 0 or
            prior.get('durable') != {'exit': 0, 'finalized': 0} or
            prior.get('signed_scope') != engine+'-im-adopt-qualification' or
            prior.get('version') != '1.12.1' or prior.get('instance_manager_version') != OLD_TAG or
            prior.get('remote_input_verification', {}).get('mismatches') != 0 or
            not all(readback.get(k) is True for k in ('installation_healthy', 'empty_business', 'adoption_journal_absent')) or
            not prior.get('baseline_identity') or
            readback.get('baseline', {}).get('baseline_identity') != prior['baseline_identity'] or
            not readback.get('managers') or any(not row['image'].endswith(':'+OLD_TAG)
                for row in readback['managers'].values())):
        raise ValueError('revision requires the independently accepted .1 installation')
    inputs = profile['inputs']
    # Freeze all previously delivered product files as well as the exact helper
    # consumers. Newly added product files are explicit null-old entries.
    required = set(CORE_INPUTS) | {name for name in prior['frozen_inputs'] if name.startswith(
        ('roles/longhorn/', 'playbooks/', 'common/', 'controller/', 'model/', 'service/')) or
        name in ('conf/config.yml', 'kubecli.py', 'runtime_hook.py')}
    if not required <= set(inputs):
        raise ValueError('revision plan omits an owning product or lifecycle input')
    for name, row in inputs.items():
        path = Path(name)
        if (path.is_absolute() or '..' in path.parts or not row.get('source') or not row.get('impact') or
                row.get('old_sha256') != prior['frozen_inputs'].get(name) or
                digest(BASE/path) != row.get('sha256')):
            raise ValueError('revision plan input changed or has no exact provenance: '+name)
    catalog = json.loads((BASE/CATALOG).read_text())
    if catalog['1.12.1']['longhorn-instance-manager'] != NEW_TAG:
        raise ValueError('revision requires the published .2 product image catalog')
    publication = reference(plan['publication_evidence'])
    record = publication.get('publication_record', {})
    if (publication.get('status') != 'pass' or publication.get('durable_rc') != 0 or
            publication.get('failure_markers') != 0 or publication.get('selected_image_count') != 1 or
            publication.get('image') != 'brinnatt/longhorn-instance-manager:'+NEW_TAG or
            record.get('key') != publication['image'] or
            set(record.get('platforms', {})) != {'linux/amd64', 'linux/arm64'} or
            not record.get('digest')):
        raise ValueError('revision requires completed exact dual-architecture publication evidence')
    if digest(BASE/PLAN) != plan_sha:
        raise ValueError('revision plan changed during input verification')
    return plan, profile, prior, plan_sha


def qualification(engine):
    _, _, prior, _ = read_plan(engine)
    evidence = copy.deepcopy(prior)
    if engine != 'v1':
        evidence['native_block_receipt_sha256'] = prior['independent_readback']['baseline']['native_block_receipt_sha256']
    return evidence


def old_health(lab, original):
    """Run the frozen new checker against .1 in private temporary FILES only."""
    if hashlib.sha256(original.encode()).hexdigest() != digest(BASE/SOURCE):
        raise ValueError('revision old-health source differs from the frozen new checker')
    product = types.ModuleType('longhorn_revision_previous_health')
    product.__file__ = str(BASE/SOURCE)
    exec(compile(original, product.__file__, 'exec'), product.__dict__)
    catalog = json.loads((BASE/CATALOG).read_text())
    if catalog['1.12.1']['longhorn-instance-manager'] != NEW_TAG:
        raise ValueError('revision old-health requires its exact .2 catalog')
    catalog['1.12.1']['longhorn-instance-manager'] = OLD_TAG
    with tempfile.TemporaryDirectory(prefix='longhorn-revision-old-health-') as directory:
        product.FILES = Path(directory)
        (product.FILES/'images.json').write_text(json.dumps(catalog))
        config = json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text())
        config.setdefault('longhorn_client_nodes', list(lab.WORKERS))
        api = product.API(str(lab.DIRECTORY/'kubectl.kubeconfig'))
        if not product.healthy(config, api, product.node_map(config, api)):
            raise ValueError('revision original .1 installation is unhealthy')


def profile():
    spec = importlib.util.spec_from_file_location('longhorn_revision_adoption', BASE/ADOPTER)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    gate.BASE, gate.OLD_TAG, gate.NEW_TAG = BASE, OLD_TAG, NEW_TAG
    gate.RECORD = 'longhorn-v2-im-revision-2-adopt.json'
    gate.prefix = lambda engine: 'LONGHORN_'+engine.upper().replace('-', '_')+'_IM_REVISION_2_ADOPT'
    gate.qualification = qualification

    def previous_source(evidence):
        # qualification already proved every old/new input. No historical
        # checker is reconstructed from unrelated new production changes.
        return (BASE/SOURCE).read_text()
    gate.previous_source = previous_source
    gate.old_health = old_health
    base_adopt, base_finish, base_clean, base_verify = gate.adopt, gate.finish, gate.clean, gate.verify_log

    @contextmanager
    def journal_context(lab, engine):
        plan, selected, _, sha = read_plan(engine)
        transition = {'plan': {'path': PLAN, 'sha256': sha},
            'previous_evidence': selected['previous_evidence'],
            'publication_evidence': plan['publication_evidence'], 'old_tag': OLD_TAG, 'new_tag': NEW_TAG}
        original_write = lab.write_private
        path = lab.DIRECTORY/gate.RECORD.replace('v2', engine)
        def write(target, row):
            if target == path:
                row.update(transition=transition, plan_sha256=sha,
                    frozen_inputs={name: entry['sha256'] for name, entry in selected['inputs'].items()})
            original_write(target, row)
        lab.write_private = write
        try:
            yield transition
        finally:
            lab.write_private = original_write

    def adopt(install, lab, relay_pid, engine='v2'):
        with journal_context(lab, engine):
            base_adopt(install, lab, relay_pid, engine)
    gate.adopt = adopt

    def finish(install, lab, engine='v2'):
        with journal_context(lab, engine) as transition:
            path = lab.DIRECTORY/gate.RECORD.replace('v2', engine)
            digest(path)
            journal = json.loads(path.read_text())
            selected = read_plan(engine)[1]
            if (journal.get('transition') != transition or journal.get('plan_sha256') != transition['plan']['sha256'] or
                    journal.get('frozen_inputs') != {name: row['sha256'] for name, row in selected['inputs'].items()}):
                raise ValueError('revision finalization plan, previous receipt or frozen inputs changed')
            base_finish(install, lab, engine)
    gate.finish = finish

    def clean(lab, relay_pid, engine='v2'):
        path = lab.DIRECTORY/gate.RECORD.replace('v2', engine)
        if path.exists() or path.is_symlink():
            with journal_context(lab, engine) as transition:
                digest(path)
                row = json.loads(path.read_text())
                if row.get('transition') != transition:
                    raise ValueError('revision cleanup refuses another transition journal')
                base_clean(lab, relay_pid, engine)
    gate.clean = clean

    def verify_log(log, engine='v2'):
        base_verify(log, engine)
        marker = gate.prefix(engine)+'_RESULT '
        result = json.loads(next(line[len(marker):] for line in log.splitlines() if line.startswith(marker)))
        plan, selected, _, sha = read_plan(engine)
        expected = {'plan': {'path': PLAN, 'sha256': sha}, 'previous_evidence': selected['previous_evidence'],
            'publication_evidence': plan['publication_evidence'], 'old_tag': OLD_TAG, 'new_tag': NEW_TAG}
        if (result.get('transition') != expected or result.get('plan_sha256') != sha or
                result.get('frozen_inputs') != {name: row['sha256'] for name, row in selected['inputs'].items()}):
            raise ValueError('revision log omits its exact plan and previous receipt chain')
    gate.verify_log = verify_log
    return gate


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


def readback_fixture(engine):
    gate = profile()
    name = {'v1': 'longhorn-lab.py', 'v2': 'longhorn-v2-install.py',
            'v2-dr-target': 'longhorn-v2-dr-target-install.py'}[engine]
    spec = importlib.util.spec_from_file_location('revision_readback_fixture', BASE/'tests/helpers'/name)
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    if engine == 'v1':
        lab = install
        sys.modules[lab.__name__] = lab
        install = types.SimpleNamespace(health=lambda lab: lab.installation_health())
    else:
        _, lab = install.fixture()
    return gate, install, lab


def readback(engine, log):
    """Separate-process health receipt before a sequential batch can mutate it."""
    gate, install, lab = readback_fixture(engine)
    lab.require_lease()
    raw = log.read_bytes()
    text = raw.decode()
    gate.verify_log(text, engine)
    result = json.loads(next(line.split(' ', 1)[1] for line in text.splitlines()
                             if line.startswith(gate.prefix(engine)+'_RESULT ')))
    install.health(lab)
    baseline = gate.baseline(lab, qualification(engine), engine)
    config = json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text())
    managers = gate.managers(lab, config['longhorn_image_prefix']+'/longhorn-instance-manager:'+NEW_TAG,
                             engine=engine)
    gate.empty(lab)
    journal = lab.DIRECTORY/gate.RECORD.replace('v2', engine)
    if journal.exists() or journal.is_symlink():
        raise ValueError('revision independent readback retains its adoption journal')
    if baseline != result['before'] or managers != result['new_managers']:
        raise ValueError('revision independent readback differs from the completed product adoption')
    _, selected, _, plan_sha = read_plan(engine)
    if (plan_sha != result['plan_sha256'] or result['frozen_inputs'] !=
            {name: row['sha256'] for name, row in selected['inputs'].items()}):
        raise ValueError('revision independent readback inputs changed during observation')
    row = {'installation_healthy': True, 'empty_business': True, 'adoption_journal_absent': True,
           'baseline': baseline, 'managers': managers, 'business_log_sha256': hashlib.sha256(raw).hexdigest(),
           'plan_sha256': result['plan_sha256'], 'frozen_inputs': result['frozen_inputs']}
    print(gate.prefix(engine)+'_INDEPENDENT_READBACK '+json.dumps(row, sort_keys=True), flush=True)
    return row


def main():
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    if sys.argv[1:2] == ['readback']:
        parser = argparse.ArgumentParser(description='Independent completed revision readback')
        parser.add_argument('mode', choices=('readback',))
        parser.add_argument('--engine', choices=ENGINES, required=True)
        parser.add_argument('--log', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        args = parser.parse_args()
        if args.output.parent != args.log.parent or args.output.name != 'independent-readback.json':
            parser.error('readback output must be the new private receipt beside the business log')
        row = readback(args.engine, args.log)
        with args.output.open('x') as stream:
            args.output.chmod(0o600)
            stream.write(json.dumps(row, sort_keys=True)+'\n')
    else:
        profile().main()


if __name__ == '__main__':
    main()

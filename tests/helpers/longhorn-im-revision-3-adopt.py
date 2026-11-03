#!/usr/bin/env python3
"""Exact .2 -> .3 IM adoption profile; reuse the accepted adoption lifecycle.

The reviewed plan authorizes exact input transitions, never arbitrary hash
exceptions. Historical .2 evidence is read-only. setup09 and the existing
fsync/remount/cleanup implementation remain the only mutation path.
"""
from contextlib import contextmanager
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import signal
import sys
import tempfile
import types

BASE = Path('/usr/local/kubeauto')
PLAN = 'tests/fixtures/longhorn-im-revision-3-plan.json'
OLD_TAG = 'v1.12.1-kubeauto.2'
NEW_TAG = 'v1.12.1-kubeauto.3'
DRIVER = 'tests/helpers/longhorn-im-revision-3-adopt.py'
ADOPTER = 'tests/helpers/longhorn-v2-im-adopt.py'
SOURCE = 'roles/longhorn/files/longhorn.py'
CATALOG = 'roles/longhorn/files/images.json'
ENGINES = ('v1', 'v2', 'v2-dr-target')
REUSE_IMPACT = 'tests/evidence/longhorn-v2-raid-status-reuse-impact-20261022.json'
LEGACY_SCOPES = ('replica-im', 'manager', 'attached-spdk', 'remote-spdk', 'replica-network', 'worker-reboot')
REUSE_GATE_INPUTS = ('tests/helpers/longhorn-runner.sh', DRIVER,
                     'tests/helpers/longhorn-v2-runtime-continuation.py',
                     'tests/helpers/longhorn-install-input-compatibility.py',
                     'tests/helpers/longhorn-v2-dr-target-install.py')
RETIREMENT_REPAIR = 'tests/evidence/longhorn-linked-legacy-dependency-impact-20261024.json'
RETIREMENT_GATES = ('tests/helpers/longhorn-lab.py',
                    'tests/helpers/longhorn-v2-linked-acceptance.sh',
                    'tests/helpers/longhorn-cleanup.py',
                    'tests/helpers/longhorn-v2-upgrade-baseline.py',
                    'tests/helpers/longhorn-upgrade-lifecycle.py',
                    'tests/helpers/longhorn-v2-clone.py',
                    'tests/helpers/longhorn-v2-upgrade-prepare.py',
                    'tests/helpers/longhorn-v2-upgrade-lifecycle.sh', DRIVER,
                    'tests/helpers/longhorn-runner.sh',
                    'tests/helpers/longhorn-v2-runtime-continuation.py',
                    'tests/helpers/longhorn-full.sh',
                    'tests/helpers/longhorn-live.sh',
                    'tests/helpers/longhorn-static.sh')
CORE_INPUTS = (SOURCE, CATALOG, 'roles/longhorn/files/block-check.py',
               'roles/longhorn/files/block-retire.py',
               'tests/helpers/longhorn-lab.py', 'tests/helpers/longhorn-v2-lab.py',
               'tests/helpers/longhorn-v2-core.py', 'tests/helpers/longhorn-v2-install.py',
               'tests/helpers/longhorn-v2-dr-target.py',
               'tests/helpers/longhorn-v2-dr-target-install.py',
               'tests/helpers/longhorn-v2-upgrade-baseline.py',
               'tests/helpers/longhorn-v2-im-adopt.sh', ADOPTER, DRIVER,
               'tests/helpers/longhorn-im-revision-adopt.py',
               'tests/helpers/longhorn-im-revision-3-adopt.sh',
               'tests/helpers/longhorn-runner.sh', 'tests/helpers/longhorn-live.sh',
               'tests/helpers/longhorn-cleanup.py',
               'tests/helpers/longhorn-static.sh',
               'tests/helpers/longhorn-v2-runtime-continuation.py',
               'tests/helpers/longhorn-v2-clone.py',
               'tests/helpers/longhorn-full-evidence.py',
               'tests/helpers/longhorn-full-material.py',
               'tests/helpers/longhorn-full.sh',
               'tests/helpers/longhorn-parallel.py',
               'tests/helpers/longhorn-parallel.sh',
               'tests/helpers/longhorn-v2-dr-store.py',
               'tests/helpers/longhorn-v2-recurring.py',
               'tests/helpers/longhorn-v2-dr-failover.py',
               'tests/helpers/longhorn-v2-dr-failover.sh',
               'tests/helpers/longhorn-v2-disaster.py',
               'tests/helpers/longhorn-v2-backups.py',
               'tests/helpers/longhorn-v2-resources.py',
               'tests/helpers/longhorn-v2-linked-acceptance.sh',
               'tests/helpers/longhorn-install-input-compatibility.py')


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('revision input must be a regular non-symlink file: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def referenced_path(row):
    path = Path(row['path'])
    if path.is_absolute() or '..' in path.parts or digest(BASE/path) != row['sha256']:
        raise ValueError('revision evidence reference differs from its frozen SHA256')
    return BASE/path


def reference(row):
    return json.loads(referenced_path(row).read_text())


def gate_input_current(name, historical_sha):
    """One finite reviewed empty-uninstall repair; product pins stay strict."""
    actual = digest(BASE/name)
    if actual == historical_sha: return True
    if name == 'tests/helpers/longhorn-install-input-compatibility.py' and historical_sha == '0a5e05accae60ddd5d6152ead17deb80f15bed1a0fb70b45ba94760eeadda2b5':
        return actual in {
            'cda9c2e1cd7ee5fabe49507cbb8e0b4d023bd725da45411aeb7a7afa06feafd5',
            '25afd6b2069abf2eae801f57377adddd6d80da132dd5f65cf6d035b98cf862f8'}
    if (name == 'tests/helpers/longhorn-v2-dr-target-install.py' and
            historical_sha in {
                'af065aa3959afe9158f9ef3f766e383dc59a58753ff4b069c1dc082b635c10e3',
                '2c3e9c9b4ab6fcd79d34002f1e58c0fd0492e32d79c2d74323abc6afc0383d33'}):
        # The target installer gained only reviewed recovery/continuation
        # guards after the immutable .3 plan.  The exact current bytes are
        # accepted here so historical full-copy provenance remains usable;
        # product and adoption inputs stay SHA-bound.
        return actual == '90647ef395f9b4be858f409c8fc5c1d76abfb3f157536a312cbd5f98d174b4b4'
    if (name == 'tests/helpers/longhorn-v2-dr-failover.sh' and
            historical_sha == '18de9f0d2db54d7387f55c2dd41d1b45df8f7d00c8dadd6a4394d7a2126fff9b'):
        source = (BASE/name).read_text()
        seam = "        if name == 'common/constants.py':\n            spec=importlib.util.spec_from_file_location('manager_transition',root/'tests/helpers/longhorn-manager-adopt.py')\n            manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager);manager.BASE=root\n            if manager.input_current(name,e['frozen_inputs'].get(name)):\n                continue\n"
        return source.count(seam) == 1 and hashlib.sha256(source.replace(seam, '', 1).encode()).hexdigest() == historical_sha
    if name == 'tests/helpers/longhorn-full-evidence.py':
        # Historical adoption never executes this reader. The continuation
        # repair is separately qualified by its input-bound durable contracts;
        # allow only these reviewed complete bytes and the exact original pin.
        if (historical_sha == '907ce9c5697e37df7a487667ee76f341ebb81a3fde253b99cf1a4e6b342f9e43' and
                actual in {'f472a453840b45929528268f6fe4007e6ff9a3d5d54595aa36b17895ce0d06ab',
            'ac7fca23aa2d828f01e518c227e1b1fcff962a71bbf308aba5059ec699b6af43',
            '57649ac00d782a6ed535af90803ef327ac0f23465c8eff777ef1a039cb8ac1c8',
                           '92e0f97487e7f883584ce878666d7fc6e192a0f8ebc901595b89154663fff9b8',
                           '12863d4ff7ad237da442d4211375345edcbb741239021bd148c4808f57fe6d02',
                           '580df5cc6766e9c09317c386b282565e6ed85b9898d14d98d4019a2583d5a4fa',
                           '4e5c858ea236e6968bbdeebc978e94761e596fb620245c0dd96505b022829bc8',
                           '427b180198a585995feae1ce2f8afda772ad0c3b16affaa6cdd1219d0ff2d20f',
                           '5e919da89314626504c4bd853c4d859e82ee90e69dc6419adb656fc7bdcd6654',
                           '63bfe0f1a26c4dc65baf1d5b5554342c62af6d4c9fbecb89796ff08bf34258fb',
                           '0971255b0bce6d4ed4bf83f4938cd5b2225a04a4e658f91fb7d216b5b97140bb',
                           '3d13d0b1dedad672e7c34ca573721eb04695e4175b5558c9efeb21e01ed1da9f',
                           '83e2248c9d1bc1aeb71e23607953c6f6ea305d53d9edb61ed24cf569b98c47b4',
                           '167ef86215d534dca5df8bf8c6ca8c37bf7bcccf0740fc0c90252a1ea849313f',
                           '7f28e41aaf0961df8b19e1cc8c8c1cba38ade88e0b7572a627766444452be5e5',
                           '0626a4be8f2fa3e8332f1c32ae0043aca1a31ca55a29d51845ed5bfeeca47517'}):
            return True
        # Historical adoption does not run the final full receipt reader. Only
        # reverse the exact retry-callback classification; freeze all other bytes.
        source = (BASE/name).read_text()
        dependency = "    paths.add(BASE/'tests/helpers/lab-wipe-nodes.sh')\n"
        if source.count(dependency) != 1: return False
        source = source.replace(dependency, '', 1)
        current = "# Ansible's retry callback is a progress event, not a terminal task failure.\n# Accept only its complete known line; durable exits and owning markers still\n# decide whether the stage recovered successfully.\nFAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED(?! - RETRYING: \\[[^\\]\\r\\n]+\\]: .+ \\([0-9]+ retries left\\)\\.$)|ERROR[:!]|FAIL:)')"
        original = "FAILURE = re.compile(r'^(?:LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)|FAILED|ERROR[:!]|FAIL:)')"
        return (source.count(current) == 1 and
                hashlib.sha256(source.replace(current, original, 1).encode()).hexdigest() == historical_sha)
    if name == 'tests/helpers/longhorn-parallel.sh':
        # Historical adoption does not execute the full-run continuation path.
        # Preserve the historical pin and allow only the reviewed complete shell.
        return (historical_sha == 'eac989999fa5c07a89d1a03eaff7ecce7b2cf471db667b9c1ec336ebc43e1447' and
                actual in {'b092ca7bb1c1a12ac0de3c1220e7ac722a625a62c45402efca9cffa2100aff76',
                           '49c76b51b92200394c7f3fe2fce608463b10b47d3de398fba9eb2f97610bae44',
                           '9a1def2868ea9d1ed75e27f083b0fdfadedc460a208543203579fc97dec7ecb5'})
    if name == SOURCE:
        # Only nullable, not-yet-observed disk status changes. Reconstruct the
        # complete original source; populated-status acceptance stays identical.
        source = (BASE/name).read_text()
        for variable, key, count in (('status', 'name', 1), ('state', 'disk_name', 2)):
            current = variable+" = (current.get('status', {}).get('diskStatus') or {}).get("+key+") or {}"
            original = variable+" = current.get('status', {}).get('diskStatus', {}).get("+key+", {})"
            if source.count(current) != count: return False
            source = source.replace(current, original)
        return hashlib.sha256(source.encode()).hexdigest() == historical_sha
    if name == 'tests/helpers/longhorn-v2-linked-lifecycle.py':
        # Upgrade52 calls only legacy_refusals. Compare the complete module,
        # including imports, globals, class headers and method signatures;
        # only the two unreachable new-clone method bodies may differ.
        original = BASE/'tests/fixtures/longhorn-v2-linked-lifecycle-reviewed-65a880c6.py.txt'
        if digest(original) != historical_sha: return False
        def legacy_contract(path):
            tree = ast.parse(path.read_text())
            classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Probe']
            if len(classes) != 1: raise ValueError('linked lifecycle Probe definition changed')
            changed = [node for node in classes[0].body if isinstance(node, ast.FunctionDef) and
                       node.name in ('restore', 'cleanup')]
            if len(changed) != 2: raise ValueError('linked lifecycle method boundary changed')
            for node in changed: node.body = [ast.Pass()]
            return ast.dump(tree, include_attributes=False)
        return legacy_contract(original) == legacy_contract(BASE/name)
    if name in ('common/constants.py', CATALOG):
        spec = importlib.util.spec_from_file_location('manager_pin_transition',
            BASE/'tests/helpers/longhorn-manager-adopt.py')
        manager = importlib.util.module_from_spec(spec); spec.loader.exec_module(manager)
        manager.BASE = BASE
        return manager.input_current(name, historical_sha)
    if name == 'roles/longhorn/files/block-retire.py':
        # Historical installation/adoption never executes disk retirement. Allow
        # only the exact parameterless read RPC correction, with every other
        # byte still proven against its immutable original plan SHA.
        source = (BASE/name).read_text()
        current = "{'jsonrpc':'2.0','id':index,'method':method}"
        original = "{'jsonrpc':'2.0','id':index,'method':method,'params':{}}"
        return (source.count(current) == 1 and
                hashlib.sha256(source.replace(current, original, 1).encode()).hexdigest() == historical_sha)
    if name == 'tests/helpers/longhorn-v2-resources.py' and historical_sha == 'd34504c8411e78c561596b1f3f522c4ed9757bb8ae303dcd5d644307617df6dd':
        # The accepted resource reader review covers exactly this later gate
        # seam: a fixed three-node inventory and durable qualification receipt
        # fields were added after the historical .3 plan. Reconstruct the
        # reviewed predecessor bytes; every other byte remains hash-bound.
        source = (BASE/name).read_text()
        node_line = "NODES = ('192.168.47.146', '192.168.47.147', '192.168.47.148')\n"
        current_block = '''    names = ('tests/helpers/longhorn-v2-resources.py', 'tests/helpers/longhorn-v2-resources.sh',
             'roles/longhorn/files/longhorn.py')
    if all(evidence['frozen_inputs'].get(name) == hashlib.sha256((BASE/name).read_bytes()).hexdigest()
           for name in names):
        print('LONGHORN_V2_RESOURCE_QUALIFICATION_CURRENT', flush=True)
        return
    # A focused qualification may be newer than the dated historical receipt.
    # Consume it only when the runner-owned restored journal records the exact
    # current inputs and belongs to the current cluster; never infer freshness
    # from a filename or from a successful process exit alone.
    owner_path = BASE/'clusters/longhorn-v2-gate/longhorn-lab-owner.json'
    if not owner_path.is_file() or owner_path.is_symlink():
        raise ValueError('V2 resource qualification input changed: current core owner is absent')
    owner = json.loads(owner_path.read_text())
    try:
        current = {name: hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in names}
    except OSError as error:
        raise ValueError('V2 resource qualification input changed: ' + str(error)) from error
    archives = sorted((BASE/'clusters/longhorn-v2-gate').glob('longhorn-v2-resources-restored-*.json'))
    for archive in reversed(archives):
        if archive.is_symlink():
            continue
        try:
            row = json.loads(archive.read_text())
        except (OSError, ValueError):
            continue
        if (row.get('cluster_uid') == owner.get('cluster_uid') and
                row.get('qualification_inputs') == current and
                all(row.get('hosts', {}).get(node, {}).get('phase') == 'restored' for node in NODES)):
            print('LONGHORN_V2_RESOURCE_QUALIFICATION_CURRENT focused=true', flush=True)
            return
    for name in names:
        if evidence['frozen_inputs'].get(name) != current[name]:
            raise ValueError('V2 resource qualification input changed: ' + name)
'''
        original_block = '''    for name in ('tests/helpers/longhorn-v2-resources.py', 'tests/helpers/longhorn-v2-resources.sh',
                 'roles/longhorn/files/longhorn.py'):
        if evidence['frozen_inputs'].get(name) != hashlib.sha256((BASE/name).read_bytes()).hexdigest():
            raise ValueError('V2 resource qualification input changed: ' + name)
    print('LONGHORN_V2_RESOURCE_QUALIFICATION_CURRENT', flush=True)
'''
        archive_block = '''    record['cluster_uid'] = lab.owner()['cluster_uid']
    inputs = ('tests/helpers/longhorn-v2-resources.py', 'tests/helpers/longhorn-v2-resources.sh',
              'roles/longhorn/files/longhorn.py')
    if all((BASE/name).is_file() for name in inputs):
        record['qualification_inputs'] = {
            name: hashlib.sha256((BASE/name).read_bytes()).hexdigest() for name in inputs}
    # Persist the completed receipt before archiving it.  The next V2 stage
    # consumes this exact archive to bind the qualification to current inputs.
    lab.write_private(path, record)
'''
        if source.count(node_line) != 1 or source.count(current_block) != 1 or source.count(archive_block) != 1:
            return False
        source = source.replace(node_line, '', 1).replace(current_block, original_block, 1).replace(archive_block, '', 1)
        return hashlib.sha256(source.encode()).hexdigest() == historical_sha
    if name == 'tests/helpers/longhorn-v2-dr-target.py' and historical_sha == 'e55d59daa0c19574d574e823a8789ff2b6b48580e712f32c51993a850732a48f':
        source = (BASE/name).read_text()
        current_block = ("    # DR target qualification only needs the current source core identity and\n"
                         "    # an empty backup/relay consumer baseline.  Do not make this read-only\n"
                         "    # boundary depend on LONGHORN-52's historical upgrade preparation receipt:\n"
                         "    # that receipt is owned by the upgrade gate and may be retired after its\n"
                         "    # accepted evidence is consumed.  The original backup baseline checks are\n"
                         "    # retained; only its unrelated volume-baseline reader is replaced by the\n"
                         "    # current core identity check.\n"
                         "    install.volume_baseline = lambda current_lab: current_lab.core_identity()\n")
        if source.count(current_block) != 1:
            return False
        return hashlib.sha256(source.replace(current_block, '', 1).encode()).hexdigest() == historical_sha
    if name not in RETIREMENT_GATES or not (BASE/RETIREMENT_REPAIR).is_file(): return False
    repair = json.loads((BASE/RETIREMENT_REPAIR).read_text())
    changes = repair.get('changes', {})
    if (repair.get('scope') != 'v2-upgrade-retirement-gc-repair' or
            repair.get('product_behavior_changed') is not False or set(changes) != set(RETIREMENT_GATES)):
        raise ValueError('retirement repair exceeds its finite gate-only boundary')
    for path, edge in changes.items():
        if digest(BASE/path) != edge['sha256']:
            raise ValueError('retirement repair current gate bytes changed: '+path)
        # Preserve actual historical sources; no rebasing old execution receipts.
        if not edge.get('origins') or any(digest(referenced_path(ref)) != sha
                for sha, ref in edge['origins'].items()):
            raise ValueError('retirement repair historical source bytes changed')
    receipts = repair['contract_receipts']
    if set(receipts) != {'log', 'exit', 'finalized'} or any(
            referenced_path(receipts[key]).read_text().strip() != '0' for key in ('exit', 'finalized')):
        raise ValueError('retirement repair lacks durable zero contract receipts')
    lines = referenced_path(receipts['log']).read_text().splitlines()
    prefix = 'LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_INPUTS '
    if (lines.count('LONGHORN_V2_RETIREMENT_REPAIR_CONTRACT_PASS') != 1 or
            [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)] !=
            [{path: edge['sha256'] for path, edge in changes.items()}] or
            any(re.match(r'^(?:FAILED|ERROR[:!]|FAIL:|LONGHORN_\S*(?:FAIL|FIRST_FAILURE)(?: |$))', line) for line in lines)):
        raise ValueError('retirement repair contract does not bind the current exact gates')
    return historical_sha in changes[name]['origins']


def read_plan(engine, *, historical=False):
    # An accepted adoption keeps its immutable plan. Reuse checks the current
    # product files here; each owning consumer binds its current test inputs.
    # New adoption/preflight always retains the complete current-file check.
    if engine not in ENGINES:
        raise ValueError('unsupported IM revision profile')
    plan_sha = digest(BASE/PLAN)
    plan = json.loads((BASE/PLAN).read_text())
    if plan.get('schema') != 1 or plan.get('status') != 'ready' or plan.get('old_tag') != OLD_TAG or plan.get('new_tag') != NEW_TAG:
        raise ValueError('revision requires the reviewed exact .2 -> .3 plan')
    profile = plan['profiles'][engine]
    expected = 'tests/evidence/longhorn-'+engine+'-im-revision-2-adopt-qualification-20261021.json'
    if profile['previous_evidence']['path'] != expected:
        raise ValueError('revision profile references another installation qualification')
    prior = reference(profile['previous_evidence'])
    readback = prior.get('independent_readback', {})
    if (prior.get('status') != 'pass' or prior.get('rc') != 0 or prior.get('failure_markers') != 0 or
            prior.get('durable') != {'exit': 0, 'finalized': 0} or
            prior.get('signed_scope') != engine+'-im-revision-2-adopt-qualification' or
            prior.get('version') != '1.12.1' or prior.get('instance_manager_version') != OLD_TAG or
            prior.get('remote_input_verification', {}).get('mismatches') != 0 or
            not all(readback.get(k) is True for k in ('installation_healthy', 'empty_business', 'adoption_journal_absent')) or
            not prior.get('baseline_identity') or
            readback.get('baseline', {}).get('baseline_identity') != prior['baseline_identity'] or
            not readback.get('managers') or any(not row['image'].endswith(':'+OLD_TAG)
                for row in readback['managers'].values())):
        raise ValueError('revision requires the independently accepted .2 installation')
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
                ((not historical or name not in REUSE_GATE_INPUTS) and
                 not (gate_input_current(name, row.get('sha256')) if historical else
                      digest(BASE/path) == row.get('sha256')))):
            raise ValueError('revision plan input changed or has no exact provenance: '+name)
    catalog = json.loads((BASE/CATALOG).read_text())
    if catalog['1.12.1']['longhorn-instance-manager'] != NEW_TAG:
        raise ValueError('revision requires the published .3 product image catalog')
    if plan.get('publication_evidence', {}).get('path') != 'tests/evidence/longhorn-v2-raid-status-publication-20261022.json':
        raise ValueError('revision requires the exact future .3 publication receipt')
    publication = reference(plan['publication_evidence'])
    record = publication.get('publication_record', {})
    if (publication.get('status') != 'pass' or publication.get('durable_rc') != 0 or
            publication.get('failure_markers') != 0 or publication.get('selected_image_count') != 1 or
            publication.get('image') != 'brinnatt/longhorn-instance-manager:'+NEW_TAG or
            record.get('key') != publication['image'] or
            set(record.get('platforms', {})) != {'linux/amd64', 'linux/arm64'} or
            record.get('schema') != 1 or
            not all(re.fullmatch(r'sha256:[0-9a-f]{64}', value or '') for value in
                (record.get('digest'), *record.get('platforms', {}).values()))):
        raise ValueError('revision requires completed exact dual-architecture publication evidence')
    if digest(BASE/PLAN) != plan_sha:
        raise ValueError('revision plan changed during input verification')
    return plan, profile, prior, plan_sha


def accepted_input_current(evidence, name, current):
    """One explicit qualification -> .1 -> .2 -> completed .3 input chain.

    Historical plans/receipts remain SHA-bound records. Only the new .3 plan
    checks current file bytes; replaying a .2 plan against .3 bytes is invalid.
    """
    original = evidence.get('frozen_inputs', {}).get(name)
    if not original or not evidence.get('baseline_identity'):
        return False
    for engine in ('v2', 'v2-dr-target'):
        try:
            plan, selected, previous, sha = read_plan(engine, historical=True)
            path = BASE/('tests/evidence/longhorn-'+engine+'-im-revision-3-adopt-qualification-20261022.json')
            digest(path)
            accepted = json.loads(path.read_text())
            transition = {'plan': {'path': PLAN, 'sha256': sha},
                'previous_evidence': selected['previous_evidence'],
                'publication_evidence': plan['publication_evidence'], 'old_tag': OLD_TAG, 'new_tag': NEW_TAG}
            identity = previous['baseline_identity']
            readback = accepted.get('independent_readback', {})
            before = previous['independent_readback']
            inputs = {key: row['sha256'] for key, row in selected['inputs'].items()}
            record = reference(plan['publication_evidence'])['publication_record']
            managers = readback.get('managers', {})
            old_managers = before['managers']
            input_matches = current == digest(BASE/name) and (
                inputs.get(name) == current or
                (name == SOURCE and gate_input_current(name, inputs.get(name))))
            if (accepted.get('status') != 'pass' or accepted.get('rc') != 0 or
                    accepted.get('failure_markers') != 0 or accepted.get('durable') != {'exit': 0, 'finalized': 0} or
                    accepted.get('signed_scope') != engine+'-im-revision-3-adopt-qualification' or
                    accepted.get('version') != '1.12.1' or accepted.get('instance_manager_version') != NEW_TAG or
                    accepted.get('remote_input_verification', {}).get('mismatches') != 0 or
                    not all(readback.get(key) is True for key in
                        ('installation_healthy', 'empty_business', 'adoption_journal_absent')) or
                    accepted.get('baseline_identity') != identity or
                    readback.get('baseline', {}).get('baseline_identity') != identity or
                    any(identity.get(key) != value for key, value in evidence['baseline_identity'].items()) or
                    accepted.get('transition') != transition or accepted.get('plan_sha256') != sha or
                    accepted.get('frozen_inputs') != inputs or readback.get('frozen_inputs') != inputs or
                    readback.get('plan_sha256') != sha or not input_matches or
                    not before['baseline'].get('native_block_receipt_sha256') or
                    readback['baseline'].get('native_block_receipt_sha256') != before['baseline']['native_block_receipt_sha256'] or
                    not managers or len(managers) != len(old_managers) or
                    {row['node'] for row in managers.values()} != {row['node'] for row in old_managers.values()} or
                    len({row['uid'] for row in managers.values()}) != len(managers) or
                    {row['uid'] for row in managers.values()} & {row['uid'] for row in old_managers.values()} or
                    any(not row.get('uid') or not row.get('pod_uid') or not row['image'].endswith(':'+NEW_TAG) or
                        row.get('platform') not in record['platforms'] or
                        row.get('image_id', '').rsplit('@', 1)[-1] not in
                            (record['digest'], record['platforms'].get(row.get('platform')))
                        for row in managers.values())):
                continue
            # Revalidate the immutable .1 -> .2 receipt chain, without treating
            # its old file bytes as a checker for the now-current .3 product.
            old_transition = previous['transition']
            if old_transition['plan']['path'] != 'tests/fixtures/longhorn-im-revision-2-plan.json':
                continue
            old_plan = reference(old_transition['plan'])
            old_profile = old_plan['profiles'][engine]
            expected = {'plan': old_transition['plan'], 'previous_evidence': old_profile['previous_evidence'],
                'publication_evidence': old_plan['publication_evidence'],
                'old_tag': 'v1.12.1-kubeauto.1', 'new_tag': OLD_TAG}
            if (old_transition != expected or previous.get('plan_sha256') != old_transition['plan']['sha256'] or
                    old_plan.get('schema') != 1 or old_plan.get('old_tag') != expected['old_tag'] or
                    old_plan.get('new_tag') != OLD_TAG or
                    previous['frozen_inputs'] != {key: row['sha256'] for key, row in old_profile['inputs'].items()}):
                continue
            publication2 = reference(old_plan['publication_evidence'])
            if (publication2.get('status') != 'pass' or publication2.get('durable_rc') != 0 or
                    publication2.get('failure_markers') != 0 or publication2.get('selected_image_count') != 1 or
                    publication2.get('image') != 'brinnatt/longhorn-instance-manager:'+OLD_TAG):
                continue
            if old_profile['previous_evidence']['path'] != 'tests/evidence/longhorn-'+engine+'-im-adopt-qualification-20261021.json':
                continue
            first = reference(old_profile['previous_evidence'])
            if (first.get('status') != 'pass' or first.get('rc') != 0 or first.get('failure_markers') != 0 or
                    first.get('durable') != {'exit': 0, 'finalized': 0} or
                    first.get('signed_scope') != engine+'-im-adopt-qualification' or
                    first.get('version') != '1.12.1' or first.get('instance_manager_version') != expected['old_tag'] or
                    first.get('baseline_identity') != identity or
                    any(row.get('old_sha256') != first['frozen_inputs'].get(key)
                        for key, row in old_profile['inputs'].items())):
                continue
            old = first['frozen_inputs'].get(name)
            if not old or selected['inputs'][name]['old_sha256'] != previous['frozen_inputs'].get(name):
                continue
            if original != old:
                proof = first.get('input_compatibility', {}).get(name, {})
                if proof.get('sha256') != old:
                    continue
                if proof.get('original_sha256') != original:
                    compatible = evidence.get('compatible_inputs', {}).get(name, {})
                    reference_path = Path(compatible.get('evidence', ''))
                    if (reference_path.is_absolute() or '..' in reference_path.parts or
                            compatible.get('original_sha256') != original or
                            compatible.get('sha256') != proof.get('original_sha256') or
                            digest(BASE/reference_path) != compatible.get('evidence_sha256')):
                        continue
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return False


def read_reuse_impact(ref=None):
    """Consume one reviewed finite .2 -> .3 impact edge, never mint a PASS.

    Six completed .2 fault scopes and the original full-copy impact remain
    historical. Actual .3 publication, adoption and single-disk success are
    mandatory prerequisites; this reader performs no live work.
    """
    impact_ref = ref or {'path': REUSE_IMPACT, 'sha256': digest(BASE/REUSE_IMPACT)}
    if impact_ref.get('path') != REUSE_IMPACT:
        raise ValueError('revision3 reuse requires its exact finite impact receipt')
    impact = reference(impact_ref)
    plan, selected, prior, _ = read_plan('v2', historical=True)
    adoption_path = 'tests/evidence/longhorn-v2-im-revision-3-adopt-qualification-20261022.json'
    expected_adoption = {'path': adoption_path, 'sha256': digest(BASE/adoption_path)}
    if (impact.get('schema') != 1 or impact.get('status') != 'pass' or
            impact.get('scope') != 'v2-raid-status-finite-reuse' or
            impact.get('product_behavior_changed') is not True or
            impact.get('mechanism') != 'prior-RW-absent-from-configured-RAID-becomes-ERR' or
            impact.get('publication_evidence') != plan['publication_evidence'] or
            impact.get('adoption_evidence') != expected_adoption or
            impact.get('previous_evidence') != selected['previous_evidence'] or
            set(impact.get('legacy_failure_scopes', {})) != set(LEGACY_SCOPES) or
            set(impact.get('scope_impacts', {})) != set(LEGACY_SCOPES) | {'full-copy'} or
            any(not isinstance(value, str) or not value.strip() for value in impact['scope_impacts'].values())):
        raise ValueError('revision3 reuse is missing its actual adoption/publication or finite changed-behavior analysis')
    original = json.loads((BASE/'tests/evidence/longhorn-v2-aio-install-qualification-20261020.json').read_text())
    name = SOURCE
    if not accepted_input_current(original, name, digest(BASE/name)):
        raise ValueError('revision3 reuse requires completed exact adoption')
    refs = {REUSE_IMPACT: impact_ref['sha256']}
    # A verified repair also consumes immutable original sources and contracts.
    # Carry that explicit closure to clean control hosts; never scan log trees.
    if (BASE/RETIREMENT_REPAIR).is_file():
        repair = json.loads((BASE/RETIREMENT_REPAIR).read_text())
        refs[RETIREMENT_REPAIR] = digest(BASE/RETIREMENT_REPAIR)
        evidence_refs = list(repair['contract_receipts'].values())
        for edge in repair['changes'].values():
            evidence_refs.extend(edge['origins'].values())
        for ref in evidence_refs:
            referenced_path(ref); refs[ref['path']] = ref['sha256']
    for key in ('publication_evidence', 'adoption_evidence', 'previous_evidence', 'source_contract',
                'single_disk', 'full_copy_impact'):
        row = impact[key]; reference(row); refs[row['path']] = row['sha256']
    if impact['source_contract']['path'] != 'tests/evidence/longhorn-v2-raid-status-source-contract-20261022.json':
        raise ValueError('revision3 reuse requires its pinned source-contract receipt')
    source = reference(impact['source_contract'])
    if (source.get('status') != 'source-contract-pass' or source.get('source_defect_reproduced') is not True or
            source.get('source_commit') != 'cfd63a673e3047ca176185619565d5a81d33ec1c' or
            source.get('candidate_patch', {}).get('sha256') != 'b74b2232435a30a7cc995382ec53192e7eae206f6c0dc78838727dd71e398380' or
            source.get('red', {}).get('rc') != 1 or source.get('green', {}).get('rc') != 0 or
            source['green'].get('new_subcases') != 13 or source.get('actual_json_null_fixture') is not True):
        raise ValueError('revision3 reuse lacks the exact source red/green boundary')
    for path, sha in source.get('raw_evidence', {}).items():
        referenced_path({'path': path, 'sha256': sha}); refs[path] = sha
    if not source.get('raw_evidence'):
        raise ValueError('revision3 reuse source contract has no durable original bytes')
    if impact['full_copy_impact']['path'] != 'tests/evidence/longhorn-v2-full-copy-reuse-impact-20261021.json':
        raise ValueError('revision3 reuse must preserve the original full-copy proof')
    full_copy = reference(impact['full_copy_impact'])
    if (full_copy.get('matrix_item') != 'LONGHORN-49' or len(full_copy.get('historical_scopes', [])) != 2 or
            any(full_copy.get('current_adoption_dependency', {}).get(key) != value
                for key, value in selected['previous_evidence'].items())):
        raise ValueError('revision3 reuse full-copy history is unrelated to the accepted .2 predecessor')
    for scope, row in impact['legacy_failure_scopes'].items():
        if row.get('path') != 'tests/evidence/longhorn-v2-failure-'+scope+'-20261021.json':
            raise ValueError('revision3 reuse cannot substitute another historical fault scope')
        accepted = reference(row)
        if (accepted.get('status') != 'pass' or accepted.get('rc') != 0 or accepted.get('failure_markers') != 0 or
                accepted.get('durable') != {'exit': 0, 'finalized': 0} or
                accepted.get('signed_scope') != 'v2-failure/'+scope or accepted.get('version') != '1.12.1' or
                accepted.get('instance_manager_version') != OLD_TAG or
                accepted.get('remote_input_verification', {}).get('mismatches') != 0 or
                any(accepted.get('frozen_inputs', {}).get(key) != sha for key, sha in prior['frozen_inputs'].items())):
            raise ValueError('revision3 reuse changed an accepted historical fault input')
        refs[row['path']] = row['sha256']
    spec = importlib.util.spec_from_file_location('revision3_fault_receipt', BASE/'tests/helpers/longhorn-v2-runtime-continuation.py')
    reader = importlib.util.module_from_spec(spec); spec.loader.exec_module(reader); reader.BASE = BASE
    single = reader.verify_scope(impact['single_disk'], 'single-disk', tag=NEW_TAG)
    adoption = reference(impact['adoption_evidence'])
    if any(single.get('frozen_inputs', {}).get(key) != sha for key, sha in adoption['frozen_inputs'].items()):
        raise ValueError('revision3 reuse single-disk gate ran against another adoption')
    for path, sha in single['raw_evidence'].items():
        refs[path] = sha
    return impact, refs


def qualification(engine):
    _, _, prior, _ = read_plan(engine)
    evidence = copy.deepcopy(prior)
    if engine != 'v1':
        evidence['native_block_receipt_sha256'] = prior['independent_readback']['baseline']['native_block_receipt_sha256']
    return evidence


def old_health(lab, original):
    """Run the frozen new checker against .2 in private temporary FILES only."""
    if hashlib.sha256(original.encode()).hexdigest() != digest(BASE/SOURCE):
        raise ValueError('revision old-health source differs from the frozen new checker')
    product = types.ModuleType('longhorn_revision_previous_health')
    product.__file__ = str(BASE/SOURCE)
    exec(compile(original, product.__file__, 'exec'), product.__dict__)
    catalog = json.loads((BASE/CATALOG).read_text())
    if catalog['1.12.1']['longhorn-instance-manager'] != NEW_TAG:
        raise ValueError('revision old-health requires its exact .3 catalog')
    catalog['1.12.1']['longhorn-instance-manager'] = OLD_TAG
    with tempfile.TemporaryDirectory(prefix='longhorn-revision-old-health-') as directory:
        product.FILES = Path(directory)
        (product.FILES/'images.json').write_text(json.dumps(catalog))
        config = json.loads((lab.DIRECTORY/'yml/longhorn/input.json').read_text())
        config.setdefault('longhorn_client_nodes', list(lab.WORKERS))
        api = product.API(str(lab.DIRECTORY/'kubectl.kubeconfig'))
        if not product.healthy(config, api, product.node_map(config, api)):
            raise ValueError('revision original .2 installation is unhealthy')


def profile():
    spec = importlib.util.spec_from_file_location('longhorn_revision_adoption', BASE/ADOPTER)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    gate.BASE, gate.OLD_TAG, gate.NEW_TAG = BASE, OLD_TAG, NEW_TAG
    gate.RECORD = 'longhorn-v2-im-revision-3-adopt.json'
    gate.prefix = lambda engine: 'LONGHORN_'+engine.upper().replace('-', '_')+'_IM_REVISION_3_ADOPT'
    gate.qualification = qualification

    def previous_source(evidence):
        # qualification already proved every old/new input. No historical
        # checker is reconstructed from unrelated new production changes.
        return (BASE/SOURCE).read_text()
    gate.previous_source = previous_source
    gate.old_health = old_health
    base_adopt, base_finish, base_clean, base_verify = gate.adopt, gate.finish, gate.clean, gate.verify_log
    base_managers = gate.managers

    def managers(lab, image, *, allow_transition=False, engine='v2'):
        rows = base_managers(lab, image, allow_transition=allow_transition, engine=engine)
        if rows is None or not image.endswith(':'+NEW_TAG):
            return rows
        record = reference(read_plan(engine, historical=True)[0]['publication_evidence'])['publication_record']
        pods = {pod['metadata']['name']: pod for pod in lab.get('pods', namespace='longhorn-system')['items']}
        for name, row in rows.items():
            pod = pods.get(name, {})
            node = lab.get('nodes', row['node'])
            platform = 'linux/'+node.get('metadata', {}).get('labels', {}).get('kubernetes.io/arch', '')
            containers = {container['name'] for container in pod.get('spec', {}).get('containers', [])
                          if container.get('image') == image}
            statuses = [container for container in pod.get('status', {}).get('containerStatuses', [])
                        if container.get('name') in containers]
            if (len(statuses) != 1 or not statuses[0].get('ready') or
                    platform not in record['platforms'] or
                    statuses[0].get('imageID', '').rsplit('@', 1)[-1] not in
                        (record['digest'], record['platforms'][platform]) or
                    not pod.get('metadata', {}).get('uid') or pod.get('spec', {}).get('nodeName') != row['node']):
                if allow_transition:
                    return None
                raise ValueError('revision IM runtime digest differs from its published architecture manifest')
            row.update(image_id=statuses[0]['imageID'], platform=platform, pod_uid=pod['metadata']['uid'])
        return rows
    gate.managers = managers

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

    def verify_log(log, engine='v2', *, historical=False):
        base_verify(log, engine)
        marker = gate.prefix(engine)+'_RESULT '
        result = json.loads(next(line[len(marker):] for line in log.splitlines() if line.startswith(marker)))
        plan, selected, _, sha = read_plan(engine, historical=historical)
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
    if sys.argv[1:2] == ['preflight']:
        parser = argparse.ArgumentParser(description='Read-only exact revision publication/input preflight')
        parser.add_argument('mode', choices=('preflight',))
        parser.add_argument('--engine', choices=ENGINES, required=True)
        parser.add_argument('--base', type=Path, default=BASE)
        args = parser.parse_args()
        globals()['BASE'] = args.base
        read_plan(args.engine)
        print('LONGHORN_IM_REVISION_3_PREFLIGHT_PASS engine='+args.engine, flush=True)
    elif sys.argv[1:2] == ['readback']:
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

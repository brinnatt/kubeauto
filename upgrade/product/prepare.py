"""Prepare an exclusive source boundary without changing cluster resources.

The recovery snapshot is fetched over the existing etcd TLS identity.  A local
restore verifies the snapshot's embedded checksum without starting a server or
touching any member's data directory.  This verifies a recovery input; it is
not a claim that an application backup or a disaster-recovery drill passed.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

from upgrade.product import coordinator
from upgrade.product.continuity import Continuity, NAME
from upgrade.product.kubernetes_preflight import Refused, mapping, query, write_receipt
from upgrade.product.next_etcd import EtcdPatch, PINS_PATH


def command(argv, *, timeout=180):
    try:
        result = subprocess.run([str(arg) for arg in argv], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('recovery command unavailable or timed out; source remains unchanged') from None
    if result.returncode:
        raise Refused(f'recovery command refused (rc={result.returncode}); source remains unchanged')
    return result.stdout


def continuity_contract(binary, config, server, request):
    """Bind customer-supplied expectations to existing API object identities."""
    required = {'namespace', 'service', 'pvc', 'client_selector', 'ledger_sha256'}
    if (not isinstance(request, dict) or set(request) != required or
            any(not isinstance(value, str) or not value for value in request.values()) or
            any(not NAME.fullmatch(request[key]) for key in ['namespace', 'service', 'pvc']) or
            len(request['client_selector']) > 512 or
            any(ord(char) < 32 for char in request['client_selector']) or
            not re.fullmatch('[a-f0-9]{64}', request['ledger_sha256'])):
        raise Refused('continuity request requires namespace, service, pvc, client_selector and ledger_sha256')
    def get(args):
        return mapping(query(binary, config, server, args))
    namespace = get(['get', 'namespace', request['namespace'], '-o', 'json'])
    service = get(['-n', request['namespace'], 'get', 'service', request['service'], '-o', 'json'])
    pvc = get(['-n', request['namespace'], 'get', 'pvc', request['pvc'], '-o', 'json'])
    volume = pvc.get('spec', {}).get('volumeName')
    if not isinstance(volume, str) or not NAME.fullmatch(volume):
        raise Refused('continuity PVC is not bound to an existing named volume')
    pv = get(['get', 'pv', volume, '-o', 'json'])
    contract = {**request, 'pv': volume,
                **{key + '_uid': value.get('metadata', {}).get('uid')
                   for key, value in [('namespace', namespace), ('service', service),
                                      ('pvc', pvc), ('pv', pv)]}}
    Continuity(binary, config, server, contract)()
    return contract


def recovery_point(manager, cluster, variables, observed, state):
    """Save a fresh live snapshot and verify an isolated offline restoration."""
    etcd = EtcdPatch(manager, cluster, variables, observed, state)
    binary = manager.extra_bin_dir / 'etcdutl'
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    expected = json.loads(PINS_PATH.read_text())['etcd'].get(arch, {}).get('etcdutl')
    if (not expected or binary.is_symlink() or not binary.is_file() or
            not os.access(binary, os.X_OK) or coordinator._sha256(binary) != expected):
        raise Refused('etcdutl does not match the admitted official recovery binary')
    snapshot = state / 'snapshot.db'
    # snapshot save requires exactly one endpoint, unlike membership observation.
    argv = [arg for arg in etcd.argv if not arg.startswith(('--endpoints=', '--command-timeout='))]
    command([*argv, '--command-timeout=180s', '--endpoints=' + etcd.endpoints[0],
             'snapshot', 'save', snapshot])
    if snapshot.is_symlink() or not snapshot.is_file():
        raise Refused('etcd did not produce a regular snapshot')
    snapshot.chmod(0o600)
    digest = coordinator._sha256(snapshot)
    status = mapping(command([binary, 'snapshot', 'status', snapshot, '--write-out=json']))
    if (type(status.get('revision')) is not int or
            status['revision'] < etcd.boundary['revision'] or
            type(status.get('totalKey')) is not int or status['totalKey'] <= 0):
        raise Refused('snapshot revision or contents differ from the observed source boundary')
    with tempfile.TemporaryDirectory(prefix='restore-check-', dir=state) as directory:
        command([binary, 'snapshot', 'restore', snapshot, '--data-dir', Path(directory) / 'member'])
    if coordinator._sha256(snapshot) != digest:
        raise Refused('snapshot bytes changed during offline verification')
    return {'path': str(snapshot), 'sha256': digest, 'status': status,
            'etcd_cluster_id': etcd.boundary['cluster_id'],
            'offline_restore_verified': True}


def prepare_next_hop(manager, cluster, continuity_path, *, state_root=Path('/var/tmp')):
    """Create a private boundary exclusively; existing boundaries are never replaced."""
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', cluster):
        raise Refused('invalid cluster name')
    manager._validate_for_setup(cluster)
    state = state_root / ('kubeauto-upgrade-' + cluster)
    lease, receipt_path = Path(str(state) + '.lease'), Path(str(state) + '.receipt.json')
    if any(path.exists() or path.is_symlink() for path in [state, lease, receipt_path]):
        raise Refused('upgrade boundary already exists; inspect it and resume instead of preparing again')
    try:
        request = json.loads(Path(continuity_path).read_text())
    except (OSError, ValueError):
        raise Refused('continuity request must be readable JSON') from None
    descriptor = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    owns_state = False
    accepted = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from upgrade.product.hop import SOURCE, TARGET, RUN_ID
        os.write(descriptor, (RUN_ID+'\n').encode())
        os.fsync(descriptor)
        state.mkdir(mode=0o700)
        owns_state = True
        config = manager.clusters_dir / cluster / 'config.yml'
        variables = manager._yaml_to_dict(config)
        if str(variables.get('K8S_VER', '')).removeprefix('v') != SOURCE.removeprefix('v'):
            raise Refused('preparation requires the exact source configuration '+SOURCE)
        variables.update(K8S_VER=TARGET.removeprefix('v'), UPGRADE_SOURCE_VERSION=SOURCE,
            UPGRADE_EXPECT_MASTER_UNSCHEDULABLE=True, UPGRADE_STATE_DIRECTORY=str(state))
        binary = manager.kube_bin_dir / 'kubectl'
        kubeconfig = manager.clusters_dir / cluster / 'kubectl.kubeconfig'
        observed = coordinator._target_observer(manager, cluster, variables, binary,
                                                kubeconfig, state / 'source-observation.json')
        result = manager._run_playbook(cluster, Path(__file__).with_name('next-preconditions.yml'),
                                       extra_vars=variables)
        if type(result.rc) is not int or result.rc != 0:
            raise Refused('platform preparation refused before cluster mutation')
        result = manager._run_playbook(cluster, Path(__file__).with_name('api-auth-observe.yml'),
                                       extra_vars=variables)
        if type(result.rc) is not int or result.rc != 0:
            raise Refused('API authentication preparation refused before cluster mutation')
        from upgrade.product.next_api import admit as admit_api
        for backend in observed['api_backends']:
            admit_api(binary, kubeconfig, backend['server'], target=TARGET)
        contract = continuity_contract(binary, kubeconfig, observed['api_backends'][0]['server'], request)
        recovery = recovery_point(manager, cluster, variables, observed, state)
        Continuity(binary, kubeconfig, observed['api_backends'][0]['server'], contract)()
        # Freeze the config and observation before publishing the acceptance receipt.
        from upgrade.product.next_config import configuration
        configuration(config, state)
        write_receipt(state / 'source-observation.json', observed)
        receipt = {'schema_version': 1, 'source': SOURCE, 'target': TARGET,
            'cluster': cluster, 'mutation': 'none-after-setup',
            'cluster_identity': observed['cluster_identity'], 'nodes': observed['nodes'],
            'continuity': contract, 'recovery_point': recovery,
            'source_observation_sha256': coordinator._sha256(state / 'source-observation.json')}
        write_receipt(receipt_path, receipt)
        accepted = True
        print('UPGRADE_PREPARE_PASS', flush=True)
        return receipt
    finally:
        if not accepted:
            # These paths were created exclusively by this attempt. Never remove
            # an existing boundary, foreign journal or any cluster resource.
            if owns_state:
                shutil.rmtree(state)
            lease.unlink(missing_ok=True)
        os.close(descriptor)

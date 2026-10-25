#!/usr/bin/env python3
"""Independent target consumer of the source-owned external TLS backup fixture.

The target connects directly to the off-cluster API, with its own credential and
UID journal. It never owns, removes or reconfigures the shared server/container.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

BASE = Path('/usr/local/kubeauto')


def full_material():
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location('longhorn_full_material', Path(__file__).with_name('longhorn-full-material.py'))
    module = module_from_spec(spec); spec.loader.exec_module(module)
    return module


def evidence_path(historical, *, optional=False):
    return full_material().path(BASE, historical, optional=optional)


def load(name):
    spec = importlib.util.spec_from_file_location('dr_store_'+name.replace('-', '_'), BASE/'tests/helpers'/name)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fixture():
    backup = load('longhorn-v2-backups.py')
    source_install, source, backups = backup.fixture()
    target_install = load('longhorn-v2-dr-target-install.py')
    target, lab = target_install.fixture()
    return backup, source_install, source, backups, target_install, target, lab


def target_baseline(install, lab, backups):
    evidence = json.loads((evidence_path('tests/evidence/longhorn-v2-dr-target-install-qualification-20261020.json')).read_text())
    if evidence['status'] != 'pass' or evidence['rc'] or evidence['failure_markers']:
        raise ValueError('DR target requires its completed storage installation prerequisite')
    for name in ('roles/longhorn/files/longhorn.py', 'tests/helpers/longhorn-v2-dr-target.py',
                 'tests/helpers/longhorn-v2-dr-target-install.py', 'tests/helpers/longhorn-lab.py'):
        if evidence['frozen_inputs'].get(name) != hashlib.sha256((BASE/name).read_bytes()).hexdigest():
            raise ValueError('DR target installation dependency changed: '+name)
    state = lab.owner()
    if any(state.get(key) != value for key, value in evidence['baseline_identity'].items()):
        raise ValueError('DR target installation identity changed')
    receipt = lab.DIRECTORY/'yml/longhorn/block-disks.json'
    if receipt.is_symlink() or hashlib.sha256(receipt.read_bytes()).hexdigest() != evidence['native_block_receipt_sha256']:
        raise ValueError('DR target original block receipt changed')
    install.health(lab)
    target = lab.get('backuptargets.longhorn.io', 'default', 'longhorn-system')
    if (any((lab.DIRECTORY/name).exists() or (lab.DIRECTORY/name).is_symlink() for name in
            ('longhorn-install-business.json','longhorn-request-input.json','longhorn-dr-backup-consumer.json')) or
            lab.get('volumes.longhorn.io', namespace='longhorn-system')['items'] or
            any(n['metadata']['name'].startswith('longhorn-business-') for n in lab.get('namespaces')['items']) or
            any(obj['metadata'].get('labels',{}).get('kubeauto.io/longhorn-backup-run')
                for kind in ('pods','secrets') for obj in lab.get(kind,namespace='longhorn-system')['items']) or
            any(lab.get(kind, namespace='longhorn-system')['items'] for kind in backups.BACKUP_RESOURCES) or
            any(p.get('spec', {}).get('csi', {}).get('driver') == 'driver.longhorn.io' for p in lab.get('pv')['items']) or
            not target or target['spec'].get('backupTargetURL') or target['spec'].get('credentialSecret')):
        raise ValueError('DR target baseline retains business or external-store consumers')
    identity = {key: state[key] for key in ('run_id','cluster_uid','namespace_uid','node_uids','raw_disks')}
    identity['config_sha256'] = hashlib.sha256((lab.DIRECTORY/'config.yml').read_bytes()).hexdigest()
    print('LONGHORN_V2_DR_TARGET_STORE_BASELINE sha256='+hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(), flush=True)


def tls_probe_script():
    return '''import json,ssl,sys,urllib.error,urllib.request
row=json.load(sys.stdin)
def open_with(ca):
 context=ssl.create_default_context(cadata=ca)
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
 with opener.open(row['url'],timeout=5) as response:assert response.status==200
open_with(row['ca'])
try:open_with(row['wrong_ca'])
except urllib.error.URLError as error:
 assert isinstance(error.reason,ssl.SSLCertVerificationError), 'wrong CA failed for an unrelated reason'
else:raise AssertionError('wrong CA was accepted')
print('LONGHORN_V2_DR_TARGET_DIRECT_TLS_PASS valid_ca=true wrong_ca_refused=true source_transport=false')
'''


class Consumer:
    """Reuse only the existing product-binding operations, not server lifecycle."""
    def __init__(self, lab, store, backups):
        self.lab = lab; self.store = store; self.backups = backups
        self.record = lab.DIRECTORY/'longhorn-dr-backup-consumer.json'
        # Binding methods consume server.env and its CA privately. They never
        # transfer that file or log credentials. The endpoint is the external
        # server's approved address, independent of the source transport Pod.
        self.directory = store.directory
        self.product_endpoint = store.endpoint

    def initialize(self):
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('DR target consumer journal already exists')
        source = self.store.receipt(); state = self.lab.owner()
        obj = self.store.inspect(source)
        if not obj or not obj['State']['Running']:
            raise ValueError('DR target requires its exact running source-owned backup server')
        if source['run_id'] == state['run_id']:
            raise ValueError('source and target fixture owners are not independent')
        row = {'run_id': state['run_id'], 'cluster_uid': state['cluster_uid'],
               'namespace_uid': state['namespace_uid'], 'name': source['name'], 'bucket': source['bucket'],
               'source_run_id': source['run_id'], 'container_id': source['container_id'],
               'endpoint': self.product_endpoint, 'source_cluster_uid': self.store.lab.owner()['cluster_uid']}
        self.lab.write_private(self.record, row)
        self.receipt()

    def receipt(self):
        if self.record.is_symlink():
            raise ValueError('symlink DR target consumer journal')
        row = json.loads(self.record.read_text()); source = self.store.receipt(); state = self.lab.owner()
        if (any(row[key] != state[key] for key in ('run_id','cluster_uid','namespace_uid')) or
                row['source_run_id'] != source['run_id'] or row['container_id'] != source['container_id'] or
                row['name'] != source['name'] or row['bucket'] != source['bucket'] or
                row['source_cluster_uid'] != self.store.lab.owner()['cluster_uid'] or
                row['cluster_uid'] == row['source_cluster_uid'] or row['endpoint'] != self.store.endpoint or
                self.product_endpoint != row['endpoint']):
            raise ValueError('DR target consumer or shared server identity changed')
        return row

    def product_configuration(self):
        return self.backups.Store.product_configuration(self)

    def restore_product(self):
        # A target Volume can still restore from this server even if its backup
        # discovery CRs have disappeared. Keep the server until volumes retire.
        self.receipt()
        if self.lab.get('volumes.longhorn.io', namespace='longhorn-system')['items']:
            raise ValueError('DR target volumes must retire before shared-store disconnect')
        # The binding operation also rejects backup CRs and verifies the
        # target/Secret UID and original spec before removing only that Secret.
        self.backups.Store.restore_product(self)

    def qualify(self, pid):
        row = self.receipt()
        ca = (self.directory/'cas/ca.crt').read_text()
        result = self.lab.remote('192.168.47.149', ['python3','-c',tls_probe_script()], stdin=json.dumps({
            'ca': ca, 'wrong_ca': self.lab.tls_fixture()['ca.crt'],
            'url': self.product_endpoint+'/minio/health/live'}))
        expected = 'LONGHORN_V2_DR_TARGET_DIRECT_TLS_PASS valid_ca=true wrong_ca_refused=true source_transport=false'
        if result.stdout.strip() != expected:
            raise ValueError('DR target direct external TLS probe lacks its exact result')
        print(expected, flush=True)
        configuration = self.product_configuration()
        self.lab.run_requests(pid, [], backup_configuration=configuration)
        row = self.receipt(); binding = row['product_binding']
        current = self.lab.get('backuptargets.longhorn.io','default','longhorn-system')
        secret = self.lab.get('secret',binding['secret_name'],'longhorn-system')
        if (not current or current['metadata']['uid'] != binding['target_uid'] or
                self.backups.target_configuration(current['spec']) != self.backups.target_configuration(binding['fixture_spec']) or
                not current.get('status',{}).get('available') or
                any(c.get('type')=='Unavailable' and c.get('status')=='True' for c in (current.get('status',{}).get('conditions') or [])) or
                not secret or secret['metadata']['uid'] != binding['secret_uid'] or
                hashlib.sha256(json.dumps(secret['data'],sort_keys=True).encode()).hexdigest() != binding['secret_digest']):
            raise ValueError('DR target product backup target did not converge to its exact shared store')
        print('LONGHORN_V2_DR_TARGET_SHARED_STORE_VERIFIED target_available=true independent_endpoint=true', flush=True)

    def cleanup(self):
        if not self.record.exists() and not self.record.is_symlink():
            return
        self.receipt(); self.restore_product(); row = self.receipt()
        if row.get('product_binding'):
            raise ValueError('DR target retains its shared-store product binding')
        self.record.unlink()
        print('LONGHORN_V2_DR_TARGET_STORE_CONSUMER_CLEAN_PASS shared_server=retained', flush=True)


def cleanup(source, lab, backups):
    store = source.backup_store()
    consumer = Consumer(lab, store, backups)
    # Failure in target recovery deliberately stops before shared server removal.
    lab.restore_request_input()
    consumer.cleanup()
    store.cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('target-baseline','qualify','cleanup'))
    parser.add_argument('--target-relay-pid',type=int,default=0)
    args=parser.parse_args()
    backup, source_install, source, backups, install, target, lab = fixture()
    lab.require_lease()
    if args.mode=='target-baseline':target_baseline(install,lab,backups)
    elif args.mode=='cleanup':cleanup(source,lab,backups)
    else:
        store=source.backup_store()
        with source.scoped_cleanup(lambda:cleanup(source,lab,backups)):
            store.prepare();store.qualify()
            store.cleanup_transport()
            if store.receipt().get('transport') or store.transport_listener() is not None:
                raise ValueError('DR target qualification requires source backup transport removed')
            print('LONGHORN_V2_DR_SOURCE_BACKUP_TRANSPORT_ABSENT shared_server=retained', flush=True)
            consumer=Consumer(lab,store,backups);consumer.initialize();consumer.qualify(args.target_relay_pid)


if __name__=='__main__':
    try:main()
    except Exception as error:
        _,_,source,_,_,_,_=fixture();source.record_failure(error)
        print('LONGHORN_V2_DR_STORE_FAIL reason='+type(error).__name__,file=sys.stderr)
        sys.exit(1)

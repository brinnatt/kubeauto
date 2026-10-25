#!/usr/bin/env python3
"""Bounded V2 SPDK, remote-worker and replica transport faults.

The existing product-created volume, fsync acknowledgement ledger and fenced
bare-Pod recovery path remain authoritative. No raw-device writes, V1 process
RPCs, force deletions or speculative salvage are performed here.
"""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
from urllib.parse import urlsplit

NS = 'longhorn-system'
RECORD = 'longhorn-install-business.json'
KEY = 'v2_runtime_fault'
VARIANTS = ('attached-spdk', 'remote-spdk', 'replica-network', 'worker-reboot')


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def host_script():
    # pidfds prevent a recycled PID from receiving the signal. The original
    # cgroup, executable, command line and start tick are rechecked after open.
    return '''import ipaddress,json,os,re,signal,subprocess,sys
from pathlib import Path
r=json.load(sys.stdin)
def host():
 return {'machine_id':Path('/etc/machine-id').read_text().strip(),
         'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
def process(pid):
 p=Path('/proc')/str(pid)
 try:
  cgroup=(p/'cgroup').read_text();exe=os.readlink(p/'exe')
  token=r['pod_uid'];match=token in cgroup or token.replace('-','_') in cgroup
  if not match or Path(exe).name!='spdk_tgt':return None
  return {'pid':pid,'start':(p/'stat').read_text().rsplit(')',1)[1].split()[19],
          'exe':exe,'cmdline':(p/'cmdline').read_bytes().hex(),'cgroup':cgroup}
 except (FileNotFoundError,ProcessLookupError):return None
def probe():
 rows=[v for p in Path('/proc').iterdir() if p.name.isdigit() and (v:=process(int(p.name)))]
 if len(rows)>1:raise ValueError('ambiguous owned SPDK target')
 return dict(host(),process=rows[0] if rows else None)
action=r['action']
if action=='probe':
 if not re.fullmatch('[a-f0-9-]{36}',r['pod_uid']):raise ValueError('invalid original Pod UID')
 print(json.dumps(probe(),sort_keys=True))
elif action=='kill':
 before=probe()
 if before!=r['expected'] or not before['process']:raise ValueError('SPDK target identity changed before signal')
 fd=os.pidfd_open(before['process']['pid'])
 try:
  if probe()!=before:raise ValueError('SPDK target changed after pidfd acquisition')
  signal.pidfd_send_signal(fd,signal.SIGKILL)
 finally:os.close(fd)
 print(json.dumps(dict(before,signal='SIGKILL'),sort_keys=True))
elif action=='host':print(json.dumps(host(),sort_keys=True))
elif action=='reboot':
 if host()!=r['expected']:raise ValueError('worker identity changed before reboot')
 subprocess.run(['systemctl','reboot'],check=True,timeout=20)
 print(json.dumps({'requested':True,'original':r['expected']},sort_keys=True))
elif action=='rules':
 if host()!=r['expected']:raise ValueError('transport fault host identity changed')
 comment=r['comment']
 if not re.fullmatch('kubeauto-longhorn-v2-[a-f0-9]{24}',comment):raise ValueError('invalid owned fault comment')
 src=str(ipaddress.IPv4Address(r['source']));dst=str(ipaddress.IPv4Address(r['destination']))
 port=r['port']
 if type(port)!=int or not 1024<=port<=65535 or src==dst:raise ValueError('invalid replica endpoint')
 if type(r.get('host_network'))!=bool:raise ValueError('transport fault lacks original IM network mode')
 chains=('OUTPUT','INPUT') if r['host_network'] else ('FORWARD','FORWARD')
 rules=[[chains[0],'-p','tcp','-s',src+'/32','-d',dst+'/32','--dport',str(port)],
        [chains[1],'-p','tcp','-s',dst+'/32','-d',src+'/32','--sport',str(port)]]
 present=[]
 for rule in rules:
  tail=rule+['-m','comment','--comment',comment,'-j','DROP']
  base=['iptables','-w','5','-t','filter']
  result=subprocess.run(base+['-C']+tail,capture_output=True,text=True,timeout=15)
  if result.returncode not in (0,1):raise ValueError('cannot verify owned transport rule')
  if r['operation']=='add':
   if result.returncode==0:raise ValueError('transport fault rule collision; do not adopt')
   subprocess.run(base+['-I',tail[0],'1']+tail[1:],check=True,timeout=15)
  elif r['operation']=='remove':
   if result.returncode==0:subprocess.run(base+['-D']+tail,check=True,timeout=15)
  elif r['operation']!='probe':raise ValueError('invalid transport rule operation')
  observed=subprocess.run(base+['-C']+tail,capture_output=True,text=True,timeout=15)
  if observed.returncode not in (0,1):raise ValueError('cannot read transport rule after operation')
  present.append(observed.returncode==0)
 print(json.dumps({'host':host(),'present':present,'chains':list(chains)},sort_keys=True))
else:raise ValueError('unsupported runtime fault action')
'''


def host_call(lab, address, request, *, check=True):
    if address not in lab.WORKERS:
        raise ValueError('runtime fault refuses an unleased worker address')
    return lab.remote(address, ['python3', '-c', host_script()], stdin=json.dumps(request),
                      timeout=45, check=check)


def probe(lab, address, pod_uid=None, *, unavailable=False):
    request = {'action': 'host'} if pod_uid is None else {'action': 'probe', 'pod_uid': pod_uid}
    try:
        response = host_call(lab, address, request, check=not unavailable)
    except TimeoutError:
        if unavailable:
            return None
        raise
    if unavailable and response.returncode == 255:
        return None
    if unavailable and response.returncode:
        raise ValueError('runtime recovery host probe failed outside the expected SSH outage')
    row = json.loads(response.stdout)
    if row['machine_id'] != lab.owner()['raw_disks'][address]['machine_id']:
        raise ValueError('runtime fault host differs from leased raw-disk owner')
    return row


def address(lab, node):
    choices = [ip for ip in lab.WORKERS if node == 'longhorn-node-'+ip.rsplit('.', 1)[1]]
    if len(choices) != 1:
        raise ValueError('runtime fault node is outside the leased workers')
    return choices[0]


def native_consumers(lab, runtime):
    """Account for cached native instances as well as Kubernetes CRs."""
    mapping = {'engines.longhorn.io': 'instanceEngines',
               'enginefrontends.longhorn.io': 'instanceEngineFrontends',
               'replicas.longhorn.io': 'instanceReplicas'}
    for name, original in runtime['managers'].items():
        current = lab.get('instancemanagers.longhorn.io', name, NS)
        if (not current or current['metadata']['uid'] != original['uid'] or current['spec'] != original['spec']):
            raise ValueError('native consumer observation changed the original IM identity')
        state = current.get('status', {})
        if any(state.get(key) for key in ('instanceShards', 'instanceShardGroups', 'backingImages')):
            raise ValueError('runtime fault refuses other SPDK consumers')
        for kind, key in mapping.items():
            expected = {instance for instance, row in runtime['instances'][kind].items() if row['manager'] == name}
            actual = state.get(key) or {}
            if (set(actual) != expected or any(row.get('spec', {}).get('name') != instance or
                    row.get('status', {}).get('state') != 'running' for instance, row in actual.items())):
                raise ValueError('runtime fault native maps contain foreign or unready instances')


def established_socket(raw, source, destination, port):
    """Read actual engine connections inside the owning IM's network namespace."""
    def endpoint(value):
        ip, number = value.split(':')
        return socket.inet_ntoa(bytes.fromhex(ip)[::-1]), int(number, 16)
    for line in raw.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[3] == '01':
            local, remote = endpoint(fields[1]), endpoint(fields[2])
            if local[0] == source and remote == (destination, port):
                return True
    return False


def network_endpoints(lab, runtime, engine_name, engine, replica):
    """Bind native addresses to original IM Pod UIDs, independently of SSH IPs."""
    source_name = runtime['instances']['engines.longhorn.io'][engine_name]['manager']
    destination_name = replica.get('status', {}).get('instanceManagerName')
    common = load('longhorn-v2-control-faults.py')
    result = {}
    for side, name in (('source', source_name), ('destination', destination_name)):
        old = runtime['managers'].get(name)
        pod = lab.get('pod', name, NS) if old else None
        if (not old or not common.pod_ready(pod) or pod['metadata']['uid'] != old['pod_uid'] or
                not common.owner(pod, 'InstanceManager', old['uid']) or
                pod['spec'].get('nodeName') != old['spec']['nodeID']):
            raise ValueError('transport fault original IM network namespace changed')
        result[side] = {'manager': name, 'pod_uid': old['pod_uid'],
            'ip': str(ipaddress.IPv4Address(pod['status']['podIP'])),
            'host_network': bool(pod['spec'].get('hostNetwork'))}
    for side, native in (('source', engine), ('destination', replica)):
        state = native.get('status', {})
        if (state.get('storageIP') or state.get('ip')) != result[side]['ip']:
            raise ValueError('native data address differs from original IM network namespace')
    if result['source']['ip'] == result['destination']['ip']:
        raise ValueError('replica transport fault requires distinct network endpoints')
    return result


def boundary(lab, journal, variant):
    if variant not in VARIANTS:
        raise ValueError('unsupported V2 runtime fault')
    runtime = load('longhorn-v2-manager-loss.py').runtime(lab, journal)
    native_consumers(lab, runtime)
    common = load('longhorn-v2-control-faults.py')
    _, remote = common.boundary(lab, journal)
    node = runtime['client_node'] if variant == 'attached-spdk' else remote['parent']['node']
    names = [name for name, row in runtime['managers'].items() if row['spec']['nodeID'] == node]
    if len(names) != 1:
        raise ValueError('runtime fault has no unique owning instance manager')
    name = names[0]
    parent = runtime['managers'][name]
    host = address(lab, node)
    observed = probe(lab, host, parent['pod_uid'] if variant.endswith('spdk') else None)
    if variant.endswith('spdk') and not observed.get('process'):
        raise ValueError('runtime fault original SPDK target is missing')
    proof = {'case': variant, 'run_id': lab.owner()['run_id'], 'cluster_uid': lab.owner()['cluster_uid'], 'runtime': runtime, 'node': node, 'address': host,
             'manager': name, 'original_host': observed,
             'engine': {'node': runtime['client_node']},
             'client_uid': journal['objects']['Pod/writer'],
             'remount_requested_at': lab.get('volumes.longhorn.io', journal['volume'], NS).get('status', {}).get('remountRequestedAt', '')}
    if variant == 'replica-network':
        engine_name = next(iter(runtime['instances']['engines.longhorn.io']))
        engine = lab.get('engines.longhorn.io', engine_name, NS)
        endpoint = engine.get('status', {}).get('currentReplicaAddressMap', {}).get(remote['replica'])
        if not endpoint:
            raise ValueError('transport fault lacks the original native replica endpoint')
        parsed = urlsplit(endpoint if '://' in endpoint else 'tcp://'+endpoint)
        if parsed.scheme != 'tcp' or parsed.path or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('transport fault refuses an ambiguous native TCP endpoint')
        replica = lab.get('replicas.longhorn.io', remote['replica'], NS)
        endpoints = network_endpoints(lab, runtime, engine_name, engine, replica)
        if (str(ipaddress.IPv4Address(parsed.hostname)) != endpoints['destination']['ip'] or
                not parsed.port or not 1024 <= parsed.port <= 65535 or
                replica['metadata']['uid'] != remote['replica_uid'] or
                replica.get('status', {}).get('port') != parsed.port):
            raise ValueError('transport fault endpoint differs from original replica native IP/port')
        source_host = address(lab, runtime['client_node'])
        sockets = lab.kubectl('-n', NS, 'exec', endpoints['source']['manager'], '-c', 'instance-manager', '--',
            'cat', '/proc/net/tcp', timeout=30).stdout
        if not established_socket(sockets, endpoints['source']['ip'], endpoints['destination']['ip'], parsed.port):
            raise ValueError('transport fault lacks the actual established native data connection')
        proof.update(address=source_host, original_host=probe(lab, source_host), replica=remote['replica'],
                     replica_uid=remote['replica_uid'], engine_name=engine_name,
                     engine_uid=engine['metadata']['uid'], source=endpoints['source']['ip'],
                     destination=endpoints['destination']['ip'], endpoints=endpoints,
                     host_network=endpoints['source']['host_network'], port=parsed.port,
                     comment='kubeauto-longhorn-v2-'+hashlib.sha256(
                         (lab.owner()['run_id']+journal['volume_uid']).encode()).hexdigest()[:24])
    return proof


def persist(lab, journal, proof):
    journal[KEY] = proof
    lab.write_private(lab.DIRECTORY/RECORD, journal)


def rule_request(proof, operation):
    return dict(action='rules', expected=proof['original_host'], operation=operation,
                **{k: proof[k] for k in ('comment', 'source', 'destination', 'port', 'host_network')})


def restore_transport(lab, journal, proof):
    row = json.loads(host_call(lab, proof['address'], rule_request(proof, 'remove')).stdout)
    if row['present'] != [False, False]:
        raise ValueError('owned replica transport rules remain; retain fault journal')
    proof['transport_restored'] = True
    persist(lab, journal, proof)


def inject_case(lab, journal, variant, *, relay_pid=0):
    proof = boundary(lab, journal, variant)
    if boundary(lab, journal, variant) != proof:
        raise ValueError('runtime fault boundary changed before injection')
    if KEY in journal:
        raise ValueError('prior runtime fault requires recovery; do not replay')
    # Persist before all potentially ambiguous remote writes, including a lost
    # SSH response. Recovery never resends a kill or a reboot.
    persist(lab, journal, proof)
    if variant.endswith('spdk'):
        row = json.loads(host_call(lab, proof['address'], {'action': 'kill',
            'pod_uid': proof['runtime']['managers'][proof['manager']]['pod_uid'],
            'expected': proof['original_host']}).stdout)
        if row != dict(proof['original_host'], signal='SIGKILL'):
            raise ValueError('SPDK signal acknowledgement differs; retain original intent')
    elif variant == 'worker-reboot':
        load('longhorn-v2-boot-fixture.py').arm(lab, journal, proof)
        load('longhorn-v2-reboot-relay.py').pause(lab, journal, proof, relay_pid)
        host_call(lab, proof['address'], {'action': 'reboot', 'expected': proof['original_host']}, check=False)
    else:
        absent = json.loads(host_call(lab, proof['address'], rule_request(proof, 'probe')).stdout)
        if absent['present'] != [False, False]:
            raise ValueError('owned transport comment already exists; retain journal without adoption')
        proof['rules_absent_before'] = True
        persist(lab, journal, proof)
        row = json.loads(host_call(lab, proof['address'], rule_request(proof, 'add')).stdout)
        if row['present'] != [True, True]:
            raise ValueError('replica transport injection did not install both bounded rules')
        def degraded():
            engine = lab.get('engines.longhorn.io', proof['engine_name'], NS)
            if not engine or engine['metadata']['uid'] != proof['engine_uid']:
                raise ValueError('transport fault engine identity changed')
            mode = engine.get('status', {}).get('replicaModeMap', {}).get(proof['replica'])
            if mode != 'ERR':
                return False
            proof['observed_replica_mode'] = mode
            persist(lab, journal, proof)
            return True
        lab.wait('v2-transport-original-replica-degraded', 180, degraded)
    print('LONGHORN_V2_RUNTIME_FAULT_INJECTED '+json.dumps(proof, sort_keys=True), flush=True)
    return proof


def component_restored(lab, journal, proof):
    current = None
    if proof['case'] == 'worker-reboot':
        # SSH can return before kubelet. Restore the measured boot allocation
        # and its ownership ledgers before polling any native IM/replica state.
        current = probe(lab, proof['address'], unavailable=True)
        if current is None or current['boot_id'] == proof['original_host']['boot_id']:
            return False
        if current['machine_id'] != proof['original_host']['machine_id']:
            raise ValueError('runtime fault recovery changed physical host')
        resources = load('longhorn-v2-resources.py')
        if resources.probe(lab, proof['address'])['kubelet_active'] != 'active':
            load('longhorn-v2-boot-fixture.py').check_inactive_kubelet(lab, journal, proof)
            return False
        if not load('longhorn-v2-boot-fixture.py').complete(lab, journal, proof):
            return False
        load('longhorn-v2-reboot-relay.py').resume(lab, journal, proof)
    common = load('longhorn-v2-control-faults.py')
    old = proof['runtime']['managers'][proof['manager']]
    manager = lab.get('instancemanagers.longhorn.io', proof['manager'], NS)
    pod = lab.get('pod', proof['manager'], NS)
    if (not manager or manager['metadata']['uid'] != old['uid'] or manager['spec'] != old['spec']):
        raise ValueError('runtime fault original IM identity/configuration changed')
    if (not common.pod_ready(pod) or manager.get('status', {}).get('currentState') != 'running'):
        return False
    if (not common.owner(pod, 'InstanceManager', old['uid']) or pod['spec'].get('nodeName') != proof['node'] or
            len(pod['spec']['containers']) != 1 or pod['spec']['containers'][0].get('image') != old['spec']['image']):
        raise ValueError('runtime fault replacement Pod owner, node or pinned image changed')
    if current is None:
        current = probe(lab, proof['address'], pod['metadata']['uid'] if proof['case'].endswith('spdk') else None)
    if current is None:
        return False
    if current['machine_id'] != proof['original_host']['machine_id']:
        raise ValueError('runtime fault recovery changed physical host')
    if proof['case'] == 'worker-reboot':
        if current['boot_id'] == proof['original_host']['boot_id']:
            return False
    elif current['boot_id'] != proof['original_host']['boot_id']:
        raise ValueError('runtime fault host unexpectedly rebooted')
    if proof['case'].endswith('spdk') and (not current.get('process') or current['process'] == proof['original_host']['process']):
        return False
    if not lab.healthy_replicas(journal['volume']) or not lab.replica_topology(journal, 3):
        return False
    return {'host': current, 'pod_uid': pod['metadata']['uid'], 'replicas': lab.replica_topology(journal, 3)}


def validate_intent(lab, journal, proof):
    state = lab.owner()
    if (proof.get('case') not in VARIANTS or proof.get('run_id') != state['run_id'] or
            proof.get('cluster_uid') != state['cluster_uid'] or
            proof.get('runtime', {}).get('volume_uid') != journal['volume_uid'] or
            proof['address'] not in lab.WORKERS or
            proof['original_host']['machine_id'] != state['raw_disks'][proof['address']]['machine_id'] or
            proof['runtime']['managers'][proof['manager']]['spec']['nodeID'] != proof['node']):
        raise ValueError('runtime fault intent differs from the leased original volume/host')
    if proof['case'] == 'replica-network':
        expected = 'kubeauto-longhorn-v2-'+hashlib.sha256((state['run_id']+journal['volume_uid']).encode()).hexdigest()[:24]
        validate_transport(proof)
        if (proof['comment'] != expected or proof['address'] != address(lab, proof['runtime']['client_node']) or
                proof['engine_uid'] != proof['runtime']['instances']['engines.longhorn.io'][proof['engine_name']]['uid']):
            raise ValueError('runtime fault transport intent exceeds its owned endpoint')
    elif proof['address'] != address(lab, proof['node']):
        raise ValueError('runtime fault address differs from its original IM node')


def validate_transport(proof):
    """Validate the frozen network identities without requiring live old Pods."""
    endpoints = proof.get('endpoints', {})
    for side in ('source', 'destination'):
        row = endpoints.get(side, {})
        original = proof['runtime']['managers'].get(row.get('manager'), {})
        if (not original or row.get('pod_uid') != original.get('pod_uid') or
                not row.get('ip') or str(ipaddress.IPv4Address(row['ip'])) != row['ip'] or
                type(row.get('host_network')) is not bool):
            raise ValueError('transport fault lacks its original IM namespace identity')
    if (proof['source'] != endpoints['source']['ip'] or proof['destination'] != endpoints['destination']['ip'] or
            proof['source'] == proof['destination'] or proof['host_network'] != endpoints['source']['host_network'] or
            endpoints['source']['manager'] != proof['runtime']['instances']['engines.longhorn.io'][proof['engine_name']]['manager'] or
            endpoints['destination']['manager'] != proof['manager'] or
            proof['runtime']['managers'][endpoints['source']['manager']]['spec']['nodeID'] != proof['runtime']['client_node'] or
            type(proof['port']) is not int or not 1024 <= proof['port'] <= 65535):
        raise ValueError('runtime fault transport intent exceeds its owned endpoint')


def recovered(lab, journal, proof, *, emit=True):
    validate_intent(lab, journal, proof)
    if proof['case'] == 'replica-network' and not proof.get('transport_restored'):
        if not proof.get('rules_absent_before'):
            raise ValueError('transport intent lacks original absence proof; manual diagnosis required')
        restore_transport(lab, journal, proof)
    observed = component_restored(lab, journal, proof)
    if not observed:
        return False
    proof['recovered'] = observed
    persist(lab, journal, proof)
    if emit:
        print('LONGHORN_V2_RUNTIME_FAULT_RECOVERED '+json.dumps(
            {'case': proof['case'], 'volume_uid': journal['volume_uid'],
             'transport_restored': proof.get('transport_restored'), **observed}, sort_keys=True), flush=True)
    return True


def cleanup(lab, original, record=None, **kwargs):
    record = record or lab.DIRECTORY/RECORD
    if record.is_symlink():
        raise ValueError('runtime fault refuses a symlink journal')
    if record.exists():
        journal = json.loads(record.read_text())
        proof = journal.get(KEY)
        if proof and sys.exc_info()[0] is not None:
            try:
                load('longhorn-v2-manager-loss.py').diagnostics(lab, journal)
            except Exception as error:
                print('LONGHORN_PRIVATE_DIAGNOSTIC_UNAVAILABLE type='+type(error).__name__, flush=True)
        if proof:
            validate_intent(lab, journal, proof)
            if proof['case'] == 'worker-reboot':
                load('longhorn-v2-reboot-relay.py').resume(lab, journal, proof)
            lab.check_business_graph(journal)
            if proof['case'] == 'worker-reboot':
                # Restore a known old fixture stop before native polling;
                # service recovery alone never signs the interrupted case.
                load('longhorn-v2-boot-fixture.py').recover_kubelet(lab, journal, proof)
            # Restore our exact transport rules even if observation timed out.
            # Never remove rules whose original absence was not established.
            if proof['case'] == 'replica-network' and not proof.get('transport_restored'):
                if not proof.get('rules_absent_before'):
                    raise ValueError('uncertain transport ownership; retain fault journal')
                restore_transport(lab, journal, proof)
            if not proof.get('recovered'):
                lab.wait('v2-runtime-restore-before-business-cleanup', 1200,
                         lambda: recovered(lab, journal, proof, emit=False))
            if not proof.get('recovered'):
                raise ValueError('runtime recovery lacks durable native proof; retain journal')
            # A normal remount can detach the business volume. Verify the
            # recovered IM and installation without requiring an attached I/O path.
            old = proof['runtime']['managers'][proof['manager']]
            manager = lab.get('instancemanagers.longhorn.io', proof['manager'], NS)
            pod = lab.get('pod', proof['manager'], NS)
            common = load('longhorn-v2-control-faults.py')
            if (not manager or manager['metadata']['uid'] != old['uid'] or manager['spec'] != old['spec'] or
                    not common.pod_ready(pod) or not common.owner(pod, 'InstanceManager', old['uid'])):
                raise ValueError('runtime component changed after recovery; retain journal')
            lab.installation_health()
            if proof['case'] == 'worker-reboot':
                load('longhorn-v2-boot-fixture.py').clean(lab, journal, proof)
            print('LONGHORN_V2_RUNTIME_FAULT_RESTORED_BEFORE_CLEANUP', flush=True)
    return original(record, **kwargs)


def read_only_recovery(lab, journal, proof, rows):
    """Official bare-Pod recovery after unexpected detach and writer EROFS.

    The default verifier still requires the original writer to resume. This
    explicit V2 hook never treats a native Ready state as application recovery.
    """
    if not rows or rows[-1].get('type') != 'retry' or rows[-1].get('errno') != 'EROFS':
        return None
    lab.check_business_graph(journal)
    volume = lab.get('volumes.longhorn.io', journal['volume'], NS)
    pod = lab.get('pod', 'writer', journal['namespace'])
    remount = (volume or {}).get('status', {}).get('remountRequestedAt', '')
    if (not volume or volume['metadata']['uid'] != journal['volume_uid'] or
            volume['spec'].get('dataEngine') != 'v2' or not remount or
            remount == proof['remount_requested_at'] or
            not pod or pod['metadata']['uid'] != proof['client_uid'] or
            proof['client_uid'] != journal['objects']['Pod/writer'] or
            pod['metadata'].get('ownerReferences') or pod['metadata'].get('deletionTimestamp') or
            pod['spec'].get('nodeName') != proof['runtime']['client_node'] or
            [v.get('persistentVolumeClaim', {}).get('claimName') for v in pod['spec'].get('volumes', [])
             if 'persistentVolumeClaim' in v] != ['data']):
        raise ValueError('read-only recovery lacks original bare client and native unexpected-detach proof')
    code = 'import json,os;print(json.dumps({"read_only":bool(os.statvfs("/data").f_flag & os.ST_RDONLY)}))'
    mount = json.loads(lab.kubectl('-n', journal['namespace'], 'exec', 'writer', '--', 'python3', '-c', code, timeout=30).stdout)
    # The original writer's pwrite/fsync error is the application failure.
    # A separate exec observes the current mount flags, not that write's state.
    # Preserve the observation without making ST_RDONLY an extra prerequisite
    # for the documented recovery of an unexpectedly detached bare Pod.
    if set(mount) != {'read_only'} or type(mount['read_only']) is not bool:
        raise ValueError('EROFS recovery lacks the actual filesystem flag observation')
    return {'reason': 'unexpected-detach-read-only', 'volume_uid': journal['volume_uid'],
            'original_pod_uid': proof['client_uid'], 'before_remount': proof['remount_requested_at'],
            'after_remount': remount, 'mount_read_only': mount['read_only'], 'last_retry': rows[-1]}


def business(lab, relay_pid, variant):
    if variant not in VARIANTS:
        raise ValueError('unsupported runtime fault business case')
    original, original_cleanup = lab.load_helper, lab.cleanup_business
    control = original('longhorn-control-faults.py')
    verify = control.verify
    control.verify = lambda current, journal, create, native: verify(
        current, journal, create, native, read_only_recovery=read_only_recovery)
    native_variant = 'engine' if variant == 'attached-spdk' else 'instance-manager'
    control.inject = lambda current, journal, ignored: inject_case(current, journal, variant, relay_pid=relay_pid)
    control.recovered = recovered
    lab.load_helper = lambda name: control if name == 'longhorn-control-faults.py' else original(name)
    lab.cleanup_business = lambda record=None, **kw: cleanup(lab, original_cleanup, record, **kw)
    try:
        lab.run_requests(relay_pid, [])
        lab.business('control_failure', relay_pid=relay_pid, fault_variant=native_variant)
    finally:
        try:
            if variant == 'worker-reboot':
                load('longhorn-v2-reboot-relay.py').resume_current(lab)
        finally:
            lab.load_helper, lab.cleanup_business = original, original_cleanup
    print('LONGHORN_V2_RUNTIME_FAULT_BUSINESS_PASS case='+variant, flush=True)


def verify_log(log, variant):
    """Bind acknowledgements and recovery to this exact fault, not Pod health."""
    def one(prefix):
        rows = [json.loads(line.split(' ', 1)[1]) for line in log.splitlines() if line.startswith(prefix+' ')]
        if len(rows) != 1:
            raise ValueError('runtime fault requires one complete observation: '+prefix)
        return rows[0]
    if variant not in VARIANTS:
        raise ValueError('unknown runtime fault log scope')
    proof = one('LONGHORN_V2_RUNTIME_FAULT_INJECTED')
    recovery = one('LONGHORN_V2_RUNTIME_FAULT_RECOVERED')
    result = one('LONGHORN_CONTROL_FAULT_RESULT')
    ledger = one('LONGHORN_CONTROL_FAULT_ACKNOWLEDGEMENTS')
    remount = one('LONGHORN_CONTROL_FAULT_REMOUNT_VERIFIED')
    engine = variant == 'attached-spdk'
    expected_variant = 'engine' if engine else 'instance-manager'
    readonly = result.get('read_only_recovery')
    recreated = engine or readonly is not None
    mode = ('documented-read-only-bare-pod-recreation' if readonly is not None else
            'documented-bare-pod-recreation' if engine else 'native-component-recreation-original-client')
    if readonly is not None:
        if (engine or readonly.get('reason') != 'unexpected-detach-read-only' or
                readonly.get('volume_uid') != proof['runtime']['volume_uid'] or
                not proof.get('client_uid') or readonly.get('original_pod_uid') != proof['client_uid'] or
                readonly.get('before_remount') != proof.get('remount_requested_at') or
                not readonly.get('after_remount') or readonly['after_remount'] == readonly['before_remount'] or
                type(readonly.get('mount_read_only')) is not bool or
                not readonly.get('replacement_pod_uid') or readonly['replacement_pod_uid'] == readonly['original_pod_uid'] or
                readonly.get('last_retry', {}).get('type') != 'retry' or
                readonly['last_retry'].get('errno') != 'EROFS' or readonly['last_retry'] not in result.get('retries', [])):
            raise ValueError('runtime read-only recovery lacks its exact unexpected-detach and fenced client proof')
    if (proof.get('case') != variant or recovery.get('case') != variant or
            recovery.get('volume_uid') != proof.get('runtime', {}).get('volume_uid') or
            result.get('variant') != expected_variant or result.get('recovery_mode') != mode or
            not isinstance(result.get('retries'), list) or not 0 <= result.get('recovery_s', -1) <= 1200 or
            result.get('client_rc') != 0 and not recreated):
        raise ValueError('runtime fault ledger/client recovery scope differs')
    old, current = proof['original_host'], recovery['host']
    if current['machine_id'] != old['machine_id']:
        raise ValueError('runtime recovery changed the original machine')
    if variant == 'worker-reboot':
        if current['boot_id'] == old['boot_id']:
            raise ValueError('worker reboot lacks an actual new boot identity')
    elif current['boot_id'] != old['boot_id']:
        raise ValueError('non-node runtime fault unexpectedly rebooted the worker')
    if variant.endswith('spdk') and (not current.get('process') or current['process'] == old['process']):
        raise ValueError('SPDK recovery lacks a new native target identity')
    if variant == 'replica-network' and (not proof.get('rules_absent_before') or
            proof.get('observed_replica_mode') != 'ERR' or recovery.get('transport_restored') is not True):
        raise ValueError('network fault lacks owned rules and observed native replica degradation')
    if variant == 'replica-network':
        validate_transport(proof)
    replicas = recovery['replicas']
    if (len(replicas) != 3 or len({r['node'] for r in replicas.values()}) != 3 or
            len({r.get('uid') for r in replicas.values() if r.get('uid')}) != 3):
        raise ValueError('runtime recovery lacks healthy three-node replicas')
    if (ledger.get('client') != ('recovered-writer' if recreated else 'writer') or
            not ledger['token'].endswith(':'+expected_variant) or
            ledger['fresh']['token'] != ledger['token']+':recovered' or
            ledger['fresh'].get('path') != '/data/control-recovered-ledger'):
        raise ValueError('runtime fault original and fresh client ledgers differ')
    faults, control = load('longhorn-faults.py'), load('longhorn-control-faults.py')
    for key, records in (('confirmed', ledger), ('fresh_confirmed', ledger['fresh'])):
        checked = control.confirmed(records['records'], records['token'], faults)['summary']
        if records['summary'] != checked or result[key] != checked:
            raise ValueError('runtime fault fsync acknowledgement prefix differs')
    if remount != {'original': result['confirmed'], 'recovered': result['fresh_confirmed']}:
        raise ValueError('runtime fault cross-node remount does not verify both ledgers')
    required = ('LONGHORN_CONTROL_FAILURE_BUSINESS_PASS', 'LONGHORN_BUSINESS_FIXTURES_CLEAN_PASS',
                'LONGHORN_V2_RUNTIME_FAULT_RESTORED_BEFORE_CLEANUP',
                'LONGHORN_V2_RUNTIME_FAULT_BUSINESS_PASS case='+variant)
    if any(log.splitlines().count(marker) != 1 for marker in required):
        raise ValueError('runtime fault lacks its scoped cleanup and business exit')
    if variant == 'worker-reboot':
        paused = re.findall(r'^LONGHORN_V2_REBOOT_RELAY_PAUSED pid=(\d+)$', log, re.M)
        resumed = re.findall(r'^LONGHORN_V2_REBOOT_RELAY_RESUMED pid=(\d+) elapsed=(\d+)$', log, re.M)
        observer = proof.get('reboot_relay', {})
        if (len(paused) != 1 or len(resumed) != 1 or paused[0] != resumed[0][0] or
                int(paused[0]) <= 0 or int(resumed[0][1]) > 1200 or
                observer.get('pid') != int(paused[0]) or observer.get('phase') != 'paused'):
            raise ValueError('worker reboot lacks its original Registry observer pause/resume proof')
        armed = re.findall(r'^LONGHORN_V2_REBOOT_FIXTURE_ARMED node=(\S+) id=(\S+)$', log, re.M)
        restored = re.findall(r'^LONGHORN_V2_REBOOT_FIXTURE_RESTORED node=(\S+) id=(\S+)$', log, re.M)
        if (len(armed) != 1 or armed != restored or armed[0][0] != proof['address'] or
                log.splitlines().count('LONGHORN_V2_REBOOT_FIXTURE_SCOPED_CLEAN_PASS') != 1):
            raise ValueError('worker reboot lacks its exact boot fixture restoration and cleanup')
    matches = re.findall(r'^LONGHORN_FSYNC_REMOUNT_VERIFIED bytes=(\d+) sha256=([a-f0-9]{64}) replica_nodes=(\S+)$', log, re.M)
    token = ledger['token'].removesuffix(':'+expected_variant)
    if (len(matches) != 1 or not token or int(matches[0][0]) != len(token.encode())*262144 or
            matches[0][1] != hashlib.sha256(token.encode()*262144).hexdigest() or
            set(matches[0][2].split(',')) != {r['node'] for r in proof['runtime']['replicas'].values()}):
        raise ValueError('runtime fault original payload cross-node readback differs')
    if re.search(r'^LONGHORN_[A-Z0-9_]*(?:FAIL|FIRST_FAILURE)(?: |$)', log, re.M):
        raise ValueError('runtime fault log contains a failure marker')
    return {'case': variant, 'recovery_s': result['recovery_s'], 'client_rc': result['client_rc'],
            'retries': result['retries'], 'confirmed': result['confirmed'], 'fresh_confirmed': result['fresh_confirmed']}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('business', 'verify', 'cleanup'))
    parser.add_argument('--case', choices=VARIANTS)
    parser.add_argument('--relay-pid', type=int, default=0)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    if args.mode == 'verify':
        if not args.log or not args.case:
            parser.error('verify requires --log and --case')
        verify_log(args.log.read_text(), args.case)
        return
    if args.mode == 'business' and (args.relay_pid <= 0 or not args.case):
        parser.error('business requires its live relay and --case')
    _, lab = load('longhorn-v2-install.py').fixture()
    lab.require_lease()
    if args.mode == 'cleanup':
        cleanup(lab, lab.cleanup_business, relay_pid=args.relay_pid)
    else:
        business(lab, args.relay_pid, args.case)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import tempfile
        import traceback
        with tempfile.NamedTemporaryFile('w', prefix='longhorn-v2-runtime-fault-', suffix='.txt',
                                          dir='/var/tmp', delete=False) as stream:
            traceback.print_exc(file=stream)
            path = stream.name
        print('LONGHORN_V2_RUNTIME_FAULT_FAIL diagnostic='+path, flush=True)
        sys.exit(1)

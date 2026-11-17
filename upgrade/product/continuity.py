"""Read the admitted workload ledger through its existing Service and PVC.

The fixture/workload owner creates data and records identities before admission.
This product check neither creates objects nor rewrites the expected ledger.
"""
import hashlib
import re
import time
from upgrade.product.kubernetes_preflight import Refused, mapping, query

NAME = re.compile(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?')


class IntegrityRefused(Refused):
    """An identity or data mismatch is permanent for this admitted boundary."""


class Continuity:
    def __init__(self, kubectl, kubeconfig, server, contract):
        required = {'namespace', 'namespace_uid', 'service', 'service_uid', 'client_selector', 'pvc',
                    'pvc_uid', 'pv', 'pv_uid', 'ledger_sha256'}
        if (not isinstance(contract, dict) or set(contract) != required or
                any(not isinstance(v, str) or not v for v in contract.values()) or
                any(not NAME.fullmatch(contract[k]) for k in ['namespace','service','pvc','pv']) or
                len(contract['client_selector']) > 512 or
                any(ord(char) < 32 for char in contract['client_selector']) or
                not re.fullmatch('[a-f0-9]{64}', contract['ledger_sha256'])):
            raise Refused('complete admitted Service/PVC/ledger continuity contract required')
        self.binary, self.config, self.server = kubectl, kubeconfig, server
        self.contract = dict(contract)

    def get(self, args):
        return mapping(query(self.binary, self.config, self.server, args))

    def once(self):
        c = self.contract
        namespace = self.get(['get', 'namespace', c['namespace'], '-o', 'json'])
        if namespace.get('metadata', {}).get('uid') != c['namespace_uid']:
            raise IntegrityRefused('continuity namespace identity changed')
        service = self.get(['-n', c['namespace'], 'get', 'service', c['service'], '-o', 'json'])
        if service.get('metadata', {}).get('uid') != c['service_uid']:
            raise IntegrityRefused('continuity Service identity changed')
        pvc = self.get(['-n', c['namespace'], 'get', 'pvc', c['pvc'], '-o', 'json'])
        pv = self.get(['get', 'pv', c['pv'], '-o', 'json'])
        if (pvc.get('metadata', {}).get('uid') != c['pvc_uid'] or
                pvc.get('status', {}).get('phase') != 'Bound' or
                pvc.get('spec', {}).get('volumeName') != c['pv'] or
                pv.get('metadata', {}).get('uid') != c['pv_uid'] or
                pv.get('spec', {}).get('claimRef', {}).get('uid') != c['pvc_uid']):
            raise IntegrityRefused('continuity volume or claim identity changed')
        pods = self.get(['-n', c['namespace'], 'get', 'pods', '-l', c['client_selector'], '-o', 'json'])
        ready = [p for p in pods.get('items', []) if
                 not p.get('metadata', {}).get('deletionTimestamp') and
                 p.get('status', {}).get('phase') == 'Running' and
                 [x.get('status') for x in p.get('status', {}).get('conditions', [])
                  if x.get('type') == 'Ready'] == ['True']]
        if len(ready) != 1:
            raise Refused('continuity client is not uniquely Ready')
        raw = query(self.binary, self.config, self.server,
            ['-n', c['namespace'], 'exec', ready[0]['metadata']['name'], '--',
             'wget', '-T', '10', '-qO-', 'http://'+c['service']+':8080/ledger'])
        if hashlib.sha256(raw.encode()).hexdigest() != c['ledger_sha256']:
            raise IntegrityRefused('continuity Service returned a different persistent ledger')

    def __call__(self):
        deadline = time.monotonic()+120
        while True:
            try:
                self.once()
                return
            except IntegrityRefused:
                raise
            except Refused:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

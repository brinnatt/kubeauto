"""Finish only installed DNS/metrics consumers through their existing image stages."""
from upgrade.product.dns_stage import DNSStage, CORE_OBJECTS, CACHE_OBJECTS
from upgrade.product.metrics_stage import MetricsStage, OBJECTS
from upgrade.product.next_platform import snapshot
from upgrade.product.kubernetes_preflight import Refused


class AddonStages:
    def __init__(self, binary, kubeconfig, source, variables, state, traffic, continuity):
        self.source,self.variables,self.state=source,variables,state
        self.binary,self.kubeconfig=binary,kubeconfig
        self.traffic,self.continuity=traffic,continuity
        nodes={n['name']:n['uid'] for n in source['nodes']}
        self.transport=DNSStage(binary,kubeconfig,[b['server'] for b in source['api_backends']],nodes,
            target_version=source['target'],
            originally_schedulable={n['name']:n.get('originally_schedulable',True) for n in source['nodes']})
        self.steps=[]
        if variables.get('dns_install')=='yes':
            self.steps.append('addons/dns')
            refs=CORE_OBJECTS | (CACHE_OBJECTS if variables.get('ENABLE_LOCAL_DNS_CACHE') is True else set())
            self.dns=snapshot(state/'dns-source.json',lambda:{
                ref:self.transport.read(['get',ref,'-o','json']) for ref in sorted(refs)})
        if variables.get('metricsserver_install')=='yes':
            self.steps.append('addons/metrics')
            self.metrics=snapshot(state/'metrics-source.json',lambda:{
                ref:self.transport.read(['get',ref,'-o','json']) for ref in sorted(OBJECTS)})

    def business_pods(self):
        ns=self.continuity['namespace']
        objects=self.transport.read(['-n',ns,'get','pods','-l',self.continuity['client_selector'],'-o','json'])
        pods={ns+'/'+p['metadata']['name']:{'uid':p['metadata']['uid'],
            'node':p['spec']['nodeName'],'containers':[c['name'] for c in p['spec']['containers']]}
            for p in objects.get('items',[]) if not p['metadata'].get('deletionTimestamp')}
        if len(pods)!=1:raise Refused('one admitted business client required for live metrics')
        return pods

    def run(self,journal):
        for step in self.steps:
            # Reverify accepted target consumers and the business ledger on a
            # completed invocation; never rewrite the saved source objects.
            completed=step in journal.data['completed']
            if not completed:journal.begin(step)
            try:
                if step=='addons/dns':
                    self.transport.run(self.dns,lambda ref:self.traffic())
                else:
                    pods=snapshot(self.state/'metrics-business-pods.json',self.business_pods)
                    stage=MetricsStage(self.binary,self.kubeconfig,self.source,pods)
                    stage.run(self.metrics)
                self.traffic()
            except BaseException as error:
                if not completed:journal.failed(step,getattr(error,'rc',1),'kubernetes')
                raise
            if not completed:journal.complete(step)

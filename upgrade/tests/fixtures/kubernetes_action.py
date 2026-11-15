"""Offline external-operation adapter for real Kubernetes/CRI playbook handoffs.

Actual Ansible expressions, includes, retries and assertions remain in control.
Every unmodelled operation fails; no connection or host mutation is available.
"""
import fcntl
import json
import os
from pathlib import Path
import re

from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        root = Path(os.environ['UPGRADE_KUBERNETES_TEST_ROOT'])
        # Ansible forks one action process per host. Lock the whole read/modify/
        # write transaction on a separate stable file, including observations.
        with (root/'state.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self.run_locked(root, task_vars)

    def run_locked(self, root, task_vars):
        state = json.loads((root/'state.json').read_text())
        module, values = self._task.args['module'], self._task.args['values']
        name, host = self._task.get_name(), task_vars['inventory_hostname']
        trace = {'name': name, 'module': module, 'host': host, 'args': values}
        with (root/'trace.jsonl').open('a') as out:
            out.write(json.dumps(trace)+'\n')
        result = {'changed': False}
        if state.get('fail') == name + ':' + host:
            return {'failed': True, 'msg': 'fixture first failure', 'rc': 44}
        node = state['nodes'].get(host)
        stdout = None
        if module == 'shell':
            body = values['_raw_params']
            match = re.search(r'systemctl is-active --quiet ([a-z-]+)', body)
            if not match or '/proc/$upgrade_' not in body or '/exe" --version' not in body:
                return {'failed': True, 'msg': 'unmodelled process observation'}
            service = match[1]
            if service in ['kubelet', 'kube-proxy'] and node:
                stdout = 'Kubernetes '+node[service]
            elif service in ['kube-controller-manager', 'kube-scheduler']:
                stdout = 'Kubernetes '+state['controllers'][host][service]
            elif service == 'cri-dockerd' and node:
                stdout = 'cri-dockerd '+node['adapter']+' (fixture)'
        elif module == 'stat':
            path = values['path']
            digest = task_vars['UPGRADE_DOCKER_PRESERVED_FILES'].get(path)
            if path == task_vars['UPGRADE_CRI_DOCKERD']['path']:
                digest = task_vars['UPGRADE_CRI_DOCKERD']['sha256']
            elif path == '/fixture/bin/cri-dockerd' and node:
                digest = node['adapter_hash']
            if digest is None:
                return {'failed': True, 'msg': 'unmodelled stat'}
            result['stat'] = {'isreg': True, 'islnk': False, 'checksum': digest}
        elif module == 'copy':
            binary = Path(values['dest']).name
            if values['dest'] != '/fixture/bin/'+binary or binary not in {
                    'kube-apiserver', 'kube-controller-manager', 'kube-scheduler',
                    'kubelet', 'kube-proxy', 'cri-dockerd'}:
                return {'failed': True, 'msg': 'unmodelled copy'}
            if binary == 'cri-dockerd':
                digest = task_vars['UPGRADE_CRI_DOCKERD']['sha256']
                result['changed'] = node['adapter_hash'] != digest
                node['adapter_hash'] = digest
            else:
                state.setdefault('staged', {}).setdefault(host, []).append(binary)
        elif module == 'systemd_service':
            service = values['name']
            if values.get('state') != 'restarted':
                return {'failed': True, 'msg': 'unmodelled service operation'}
            if service == 'cri-dockerd':
                node['adapter'] = '0.4.10'
            elif service not in state.get('staged', {}).get(host, []):
                return {'failed': True, 'msg': 'restart without staged binary'}
            elif service == 'kube-apiserver':
                state['apis'][host] = 'v1.34.13'
            elif service in ['kube-controller-manager', 'kube-scheduler']:
                state['controllers'][host][service] = 'v1.34.13'
            elif service in ['kubelet', 'kube-proxy']:
                node[service] = 'v1.34.13'
            else:
                return {'failed': True, 'msg': 'unmodelled service'}
        elif module == 'command':
            argv = values['argv']
            if Path(argv[0]).name == 'kubectl':
                if '--raw=/version' in argv:
                    server = next(v.split('=', 1)[1] for v in argv if v.startswith('--server='))
                    backend = server.removeprefix('https://').split(':')[0]
                    stdout = json.dumps({'gitVersion': state['apis'][backend]})
                elif '--raw=/readyz' in argv:
                    stdout = 'ok'
                elif '--raw=/api/v1' in argv:
                    stdout = json.dumps({'resources': [{'name': 'pods/eviction', 'kind': 'Eviction',
                                                        'group': 'policy', 'version': 'v1'}]})
                elif 'node' in argv:
                    nodename = argv[argv.index('node')+1]
                    observed = next(n for n in state['nodes'].values() if n['name'] == nodename)
                    stdout = json.dumps({'metadata': {'name': nodename, 'uid': observed['uid']},
                        'spec': {'unschedulable': observed['cordoned']},
                        'status': {'nodeInfo': {'kubeletVersion': observed['kubelet']},
                                   'conditions': [{'type': 'Ready', 'status': 'True'}]}})
                elif 'drain' in argv or 'uncordon' in argv:
                    verb = 'drain' if 'drain' in argv else 'uncordon'
                    nodename = argv[argv.index(verb)+1]
                    observed = next(n for n in state['nodes'].values() if n['name'] == nodename)
                    observed['cordoned'] = verb == 'drain'
                    stdout = verb+' complete'
            elif argv == ['systemctl', 'show', 'docker', '--property=MainPID', '--value']:
                stdout = '1234'
            elif argv[:3] == ['/fixture/bin/docker', 'info', '--format']:
                stdout = json.dumps({'ServerVersion': '28.5.2', 'CgroupDriver': 'systemd',
                                     'DockerRootDir': '/fixture/docker-data'})
            elif argv[0] == '/fixture/bin/crictl':
                if 'info' in argv:
                    stdout = json.dumps({'status': {'conditions': [{'type': k, 'status': True}
                        for k in ['RuntimeReady', 'NetworkReady']]}})
                elif 'version' in argv:
                    stdout = json.dumps({'runtimeName': 'docker', 'runtimeVersion': '28.5.2',
                                         'runtimeApiVersion': 'v1'})
                elif 'pods' in argv:
                    stdout = json.dumps({'items': [{'id': host+'-sandbox'}]})
        else:
            return {'failed': True, 'msg': 'unmodelled module'}
        if module in ['shell', 'command']:
            if stdout is None:
                return {'failed': True, 'msg': 'unmodelled command'}
            result.update(rc=0, stdout=stdout, stdout_lines=stdout.splitlines(), stderr='')
        (root/'state.json').write_text(json.dumps(state))
        return result

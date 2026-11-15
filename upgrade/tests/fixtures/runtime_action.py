"""Recording Ansible action used only by offline runtime-stage contracts.

No connection, executable or service operation is performed. Real playbook
assertions, conditions, includes and loops run in Ansible around this adapter.
Unmodelled operations fail instead of manufacturing a success response.
"""
import json
import os
from pathlib import Path
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        root = Path(os.environ['UPGRADE_RUNTIME_TEST_ROOT'])
        state = json.loads((root/'state.json').read_text())
        args = self._task.args
        module, values = args['module'], args['values']
        name = self._task.get_name()
        target = task_vars.get('upgrade_runtime_target')
        trace = {'name': name, 'module': module, 'target': target, 'args': values}
        with (root/'trace.jsonl').open('a') as output:
            output.write(json.dumps(trace)+'\n')
        if state.get('fail') in [name, name+':'+str(target)]:
            return {'failed': True, 'msg': 'fixture first failure', 'rc': 43}
        result = {'changed': False}
        if module == 'stat':
            path = values['path']
            filename = Path(path).name
            if filename == 'config.toml':
                digest = state['config_hash']
            elif filename == '.kubeauto-contract-1.toml':
                digest = state.get('staged_config_hash', task_vars['UPGRADE_CONTAINERD_CONFIG']['sha256'])
            elif filename == 'containerd.service':
                digest = state['unit_hash']
            elif path.startswith('/fixture/bin/'):
                digest = state['disk_hashes'][filename]
            else:
                return {'failed': True, 'msg': 'unmodelled stat path'}
            result['stat'] = {'isreg': True, 'islnk': False, 'checksum': digest}
        elif module == 'copy':
            destination = values['dest']
            if destination.startswith('/fixture/bin/'):
                state['disk_hashes'][Path(destination).name] = task_vars['UPGRADE_CONTAINERD_BUNDLES'][target]['sha256'][Path(destination).name]
            elif destination == '/etc/containerd/config.toml':
                state['config_hash'] = task_vars['UPGRADE_CONTAINERD_CONFIG']['sha256']
            elif destination != '/etc/containerd/.kubeauto-contract-1.toml':
                return {'failed': True, 'msg': 'unmodelled copy path'}
        elif module == 'systemd_service':
            if values != {'name': 'containerd', 'state': 'restarted'}:
                return {'failed': True, 'msg': 'unmodelled service mutation'}
            state['version'] = target
        elif module == 'file':
            if values != {'path': '/etc/containerd/.kubeauto-contract-1.toml', 'state': 'absent'}:
                return {'failed': True, 'msg': 'unmodelled cleanup scope'}
        elif module == 'command':
            argv = values['argv']
            stdout = None
            if 'kubectl' in argv[0] and 'drain' in argv:
                state['cordoned'] = True
                stdout = 'node/node-one drained'
            elif 'kubectl' in argv[0] and argv[-4:-2] == ['node', 'node-one']:
                stdout = json.dumps({'metadata': {'uid': state.get('uid', 'uid-one'), 'name': 'node-one'},
                                     'spec': {'unschedulable': state.get('cordoned', True)}})
            elif argv[:3] == ['systemctl', 'show', 'containerd']:
                stdout = 'Delegate=yes\nKillMode='+state.get('kill_mode', 'process')
            elif argv[0] == '/fixture/bin/runc' and argv[1:] == ['--version']:
                stdout = 'runc version 1.5.2\ncommit: fixture'
            elif argv[0] == '/fixture/bin/containerd' and argv[-2:] == ['config', 'dump']:
                stdout = 'version = 3'  # decoding is separately tested with actual official binaries
            elif argv[0] == '/fixture/bin/crictl':
                if 'version' in argv:
                    stdout = ('Version:  0.1.0\nRuntimeName:  containerd\n'
                              f'RuntimeVersion:  v{state["version"]}\nRuntimeApiVersion:  v1\n')
                elif 'info' in argv:
                    stdout = json.dumps({'status': {'conditions': [
                        {'type': k, 'status': True} for k in ['RuntimeReady', 'NetworkReady']]}})
                elif 'pods' in argv:
                    stdout = json.dumps({'items': [{'id': state.get('sandbox', 'retained-one')}]})
                    if state.get('lose_sandbox') and target:
                        stdout = '{"items": []}'
            if stdout is None:
                return {'failed': True, 'msg': 'unmodelled command'}
            result.update(rc=0, stdout=stdout, stdout_lines=stdout.splitlines(), stderr='')
        else:
            return {'failed': True, 'msg': 'unmodelled action'}
        (root/'state.json').write_text(json.dumps(state))
        return result

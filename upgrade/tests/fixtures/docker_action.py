"""Offline Docker-stage recording adapter; no executable or connection is used."""
import json
import os
from pathlib import Path
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        root = Path(os.environ['UPGRADE_RUNTIME_TEST_ROOT'])
        state = json.loads((root/'state.json').read_text())
        module, values = self._task.args['module'], self._task.args['values']
        name = self._task.get_name()
        with (root/'trace.jsonl').open('a') as output:
            output.write(json.dumps({'name': name, 'module': module, 'args': values})+'\n')
        if state.get('fail') == name:
            return {'failed': True, 'msg': 'fixture first failure', 'rc': 44}
        result = {'changed': False}
        if module == 'stat':
            path = values['path']
            digest = state['preserved'].get(path)
            if path == '/fixture/bin/cri-dockerd':
                digest = state['adapter_hash']
            elif path == task_vars['UPGRADE_CRI_DOCKERD']['path']:
                digest = task_vars['UPGRADE_CRI_DOCKERD']['sha256']
            if digest is None:
                return {'failed': True, 'msg': 'unmodelled stat path'}
            result['stat'] = {'isreg': True, 'islnk': False, 'checksum': digest}
        elif module == 'copy':
            if values['dest'] != '/fixture/bin/cri-dockerd':
                return {'failed': True, 'msg': 'unmodelled copy scope'}
            digest = task_vars['UPGRADE_CRI_DOCKERD']['sha256']
            result['changed'] = state['adapter_hash'] != digest
            state['adapter_hash'] = digest
        elif module == 'systemd_service':
            if values != {'name': 'cri-dockerd', 'state': 'restarted'}:
                return {'failed': True, 'msg': 'unmodelled service scope'}
            state['adapter'] = '0.4.10'
            if state.get('restart_engine'):
                state['engine_pid'] += 1
        elif module == 'shell':
            body = values['_raw_params']
            if 'systemctl is-active --quiet cri-dockerd' not in body or '"/proc/$upgrade_adapter_pid/exe" --version' not in body:
                return {'failed': True, 'msg': 'unmodelled shell operation'}
            stdout = 'cri-dockerd '+state['adapter']+' (fixture)'
            result.update(rc=0, stdout=stdout, stdout_lines=stdout.splitlines(), stderr='')
        elif module == 'command':
            argv = values['argv']; stdout = None
            if 'kubectl' in argv[0] and 'drain' in argv:
                state['cordoned'] = True; stdout = 'node/node-one drained'
            elif 'kubectl' in argv[0] and argv[-4:-2] == ['node', 'node-one']:
                stdout = json.dumps({'metadata': {'name': 'node-one', 'uid': state.get('uid', 'uid-one')},
                                     'spec': {'unschedulable': state.get('cordoned', True)}})
            elif argv == ['systemctl', 'show', 'docker', '--property=MainPID', '--value']:
                stdout = str(state['engine_pid'])
            elif argv[:3] == ['/fixture/bin/docker', 'info', '--format']:
                if argv[3] != '{{json .}}':
                    return {'failed': True, 'msg': 'unmodelled Docker info format'}
                stdout = json.dumps({'ServerVersion': state.get('engine', '28.5.2'),
                                     'CgroupDriver': state.get('driver', 'systemd'), 'DockerRootDir': '/fixture/docker-data'})
            elif argv[0] == '/fixture/bin/crictl':
                if argv[1] != '--runtime-endpoint=unix:///var/run/cri-dockerd.sock':
                    return {'failed': True, 'msg': 'unmodelled CRI endpoint'}
                if 'info' in argv:
                    stdout = json.dumps({'status': {'conditions': [
                        {'type': k, 'status': True} for k in ['RuntimeReady', 'NetworkReady']]}})
                elif 'pods' in argv:
                    stdout = json.dumps({'items': [] if state.get('lose_sandbox') and state['adapter'] == '0.4.10' else [{'id': 'retained-one'}]})
                elif 'version' in argv:
                    stdout = json.dumps({'runtimeName': 'docker', 'runtimeVersion': state.get('engine', '28.5.2'), 'runtimeApiVersion': 'v1'})
            if stdout is None:
                return {'failed': True, 'msg': 'unmodelled command'}
            result.update(rc=0, stdout=stdout, stdout_lines=stdout.splitlines(), stderr='')
        else:
            return {'failed': True, 'msg': 'unmodelled action'}
        (root/'state.json').write_text(json.dumps(state))
        return result

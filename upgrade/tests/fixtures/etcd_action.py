"""Offline etcd action adapter; real assertions run without a connection."""
import json
import os
from pathlib import Path
from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        root = Path(os.environ['UPGRADE_ETCD_TEST_ROOT'])
        state = json.loads((root/'state.json').read_text())
        module, values = self._task.args['module'], self._task.args['values']
        host, name = task_vars['inventory_hostname'], self._task.get_name()
        with (root/'trace.jsonl').open('a') as stream:
            stream.write(json.dumps({'host': host, 'name': name, 'module': module,
                                     'args': values})+'\n')
        result = {'changed': False}
        if module == 'stat':
            path = values['path']
            if path == '/fixture/etcdctl':
                sha = state.get('client_hash', task_vars['UPGRADE_ETCDCTL_SHA256'])
            elif path == '/etc/systemd/system/etcd.service':
                sha = state['unit_hash'][host]
            elif path == '/fixture/bin/etcd':
                sha = state['disk_hash'][host]
            else:
                targets = [t for t in task_vars['UPGRADE_ETCD_TARGETS'].values() if t['path'] == path]
                if not targets or len({target['sha256'] for target in targets}) != 1:
                    return {'failed': True, 'msg': 'unmodelled stat'}
                sha = state.get('artifact_hash', targets[0]['sha256'])
            result['stat'] = {'isreg': True, 'executable': True, 'checksum': sha}
        elif module == 'copy':
            target = task_vars['UPGRADE_ETCD_TARGETS'][host]
            if values != {'src': target['path'], 'dest': '/fixture/bin/etcd', 'mode': '0755'}:
                return {'failed': True, 'msg': 'unmodelled copy'}
            result['changed'] = state['disk_hash'][host] != target['sha256']
            state['disk_hash'][host] = target['sha256']
        elif module == 'systemd_service':
            if values != {'name': 'etcd', 'state': 'restarted'}:
                return {'failed': True, 'msg': 'unmodelled restart'}
            state['version'][host] = state.get('restart_version', '3.6.16')
            state['restarted'].append(host)
            if state.get('regress_completed') and len(state['restarted']) == 3:
                state['version'][state['restarted'][0]] = '3.6.4'
            if state.get('unit_drift'):
                state['unit_hash'][host] = 'f'*64
        elif module == 'shell':
            if ('systemctl is-active --quiet etcd' not in values['_raw_params'] or
                    '"/proc/$upgrade_pid/exe" --version' not in values['_raw_params']):
                return {'failed': True, 'msg': 'unmodelled shell'}
            stdout = 'etcd Version: '+state.get('process_version', state['version'][host])+'\nGit SHA: fixture'
            result.update(rc=0, stdout=stdout, stdout_lines=stdout.splitlines(), stderr='')
        elif module == 'command':
            argv = values['argv']
            if argv == ['systemctl', 'show', '--property=FragmentPath', '--value', 'etcd']:
                data = '/etc/systemd/system/etcd.service'
            elif argv == ['systemctl', 'show', '--property=DropInPaths', '--value', 'etcd']:
                data = state.get('dropins', '')
            elif argv[0] == '/fixture/etcdctl':
                expected = ['/fixture/etcdctl',
                    '--endpoints='+','.join(task_vars['UPGRADE_ETCD_ENDPOINTS']),
                    '--cacert=/fixture/ssl/ca.pem', '--cert=/fixture/ssl/etcd.pem',
                    '--key=/fixture/ssl/etcd-key.pem', '--dial-timeout=5s', '--command-timeout=15s']
                if argv[:7] != expected:
                    return {'failed': True, 'msg': 'unmodelled client TLS or endpoint scope'}
                members = state['members']
                if argv[7:] == ['member', 'list', '--write-out=json']:
                    if state.get('fail_after_restart') and state['restarted']:
                        return {'failed': True, 'rc': 45, 'msg': 'fixture first endpoint failure'}
                    data = {'members': members}
                elif argv[7:] == ['endpoint', 'health', '--write-out=json']:
                    data = [{'endpoint': endpoint, 'health': not state.get('unhealthy')}
                            for endpoint in task_vars['UPGRADE_ETCD_ENDPOINTS']]
                elif argv[7:] == ['endpoint', 'status', '--write-out=json']:
                    data = []
                    for member_host, version in state['version'].items():
                        member = next(m for m in members if m['clientURLs'] == ['https://'+member_host+':2379'])
                        data.append({'Endpoint': member['clientURLs'][0], 'Status': {
                            'header': {'cluster_id': state.get('cluster_id', 101),
                                       'member_id': member['ID'], 'revision': state.get('revision', 100)},
                            'version': version, 'leader': state.get('leader', 1),
                            'errors': state.get('errors', []), 'isLearner': member.get('isLearner', False)}})
                else:
                    return {'failed': True, 'msg': 'unmodelled etcdctl operation'}
            else:
                return {'failed': True, 'msg': 'unmodelled command'}
            stdout = data if isinstance(data, str) else json.dumps(data)
            result.update(rc=0, stdout=stdout, stdout_lines=stdout.splitlines(), stderr='')
        else:
            return {'failed': True, 'msg': 'unmodelled module'}
        (root/'state.json').write_text(json.dumps(state))
        return result

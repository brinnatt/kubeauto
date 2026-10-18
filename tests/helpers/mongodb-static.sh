#!/usr/bin/env bash
# No SSH, image pull, source synchronization or Kubernetes mutation.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="${MONGODB_PYTHON:-$ROOT/.venv/bin/python}"
HELM="${MONGODB_HELM:-helm}"
ANSIBLE="${MONGODB_ANSIBLE_PLAYBOOK:-ansible-playbook}"
PROMTOOL="${MONGODB_PROMTOOL:-promtool}"
work="$(mktemp -d /tmp/kubeauto-mongodb-static.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
export PYTHONPATH="$ROOT" PYTHONPYCACHEPREFIX="$work/cache" ANSIBLE_ROLES_PATH="$ROOT/roles"
for file in tests/helpers/mongodb-*.sh tests/run_enterprise_regression.sh; do bash -n "$file"; done
"$PY" -m py_compile roles/cluster-addon/files/mongodb.py tests/helpers/validate_mongodb_matrix.py tests/helpers/mongodb-platform.py tests/helpers/mongodb-lab-storage.py tests/helpers/mongodb-lab.py tests/helpers/mongodb-scenarios.py tests/helpers/mongodb-registry-relay.py tests/helpers/mongodb-backups.py tests/helpers/mongodb-sharding.py
"$PY" tests/helpers/validate_mongodb_matrix.py tests/mongodb-test-matrix.yaml
"$PY" -m unittest tests.unit.test_mongodb_delivery tests.unit.test_mongodb_matrix tests.unit.test_mongodb_platform tests.unit.test_mongodb_storage tests.unit.test_mongodb_lab tests.unit.test_mongodb_scenarios tests.unit.test_mongodb_backups tests.unit.test_mongodb_sharding tests.unit.test_six_repo_version_sync -v
echo MONGODB_VALIDATION_PASS
"$PY" - "$work" <<'PY'
import json,sys
import importlib.util
from pathlib import Path
from tests.unit.test_mongodb_delivery import configuration,render
work=Path(sys.argv[1]);config=configuration()
spec=importlib.util.spec_from_file_location('mongodb_static_lab',Path('tests/helpers/mongodb-lab.py'))
lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
scenario_spec=importlib.util.spec_from_file_location('mongodb_static_scenarios',Path('tests/helpers/mongodb-scenarios.py'))
scenarios=importlib.util.module_from_spec(scenario_spec);scenario_spec.loader.exec_module(scenarios)
import yaml
workers={'mongodb-node-'+ip.rsplit('.',1)[1]:ip for ip in lab.NODES[3:]}
monitoring=scenarios.observability_objects('mongodb-observe-'+'a'*16,workers,lab.NAME,10)
configmap=next(o for o in monitoring if o['kind']=='ConfigMap')
work.joinpath('monitoring-prometheus.yml').write_text(configmap['data']['prometheus.yml'])
work.joinpath('monitoring-rules.yml').write_text(configmap['data']['alerts.yml'])
# These samples prove actual rule semantics only; live telemetry/faults remain M34.
health='mongodb_members_health{job="mongodb",member_idx="member-0",instance="primary"}'
volume='{namespace="mongodb-gate",persistentvolumeclaim="mongod-data-mongodb-gate-data-rs0-0"}'
series=[
 {'series':health,'values':'1 1 0 0 0 1 1 1'},
 {'series':health.replace('primary','self'),'values':'1x7'},
 {'series':'mongodb_members_optimeDate{job="mongodb",member_idx="member-0"}','values':'100000 100000 120000 120000 120000 120000 120000 120000'},
 {'series':'mongodb_members_optimeDate{job="mongodb",member_idx="member-1"}','values':'100000 100000 100000 100000 100000 120000 120000 120000'},
 {'series':'kubelet_volume_stats_available_bytes'+volume,'values':'50 50 20 20 20 50 50 50'},
 {'series':'kubelet_volume_stats_capacity_bytes'+volume,'values':'100x7'},
 {'series':'kube_customresource_mongodb_backup_completed{cluster="mongodb-gate-data"}','values':'0 0 0 0 0 25 25 25'}]
checks=[]
for alert in ('MongoDBMemberUnavailable','MongoDBReplicationLag','MongoDBVolumeLowSpace','MongoDBBackupStale'):
 for when in ('5s','20s','30s'):
  labels={'member_idx':'member-0'} if alert=='MongoDBMemberUnavailable' else {}
  checks.append({'eval_time':when,'alertname':alert,'exp_alerts':[{'exp_labels':labels}] if when=='20s' else []})
suite={'rule_files':[str(work/'monitoring-rules.yml')],'evaluation_interval':'5s',
       'tests':[{'interval':'5s','input_series':series,'alert_rule_test':checks}]}
work.joinpath('monitoring-tests.yml').write_text(yaml.safe_dump(suite))
for deployment in ('mongodb-gate','mongodb-restore-logical','mongodb-restore-physical','mongodb-sharding-gate','mongodb-ownership-gate'):
 work.joinpath(deployment+'.hosts').write_text(lab.inventory(deployment))
work.joinpath('inventory.yml').write_text('''- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.assert:
        that: cluster_dir == base_dir + '/clusters/' + expected_deployment
''')
config.update(base_dir=str(Path.cwd()),cluster_dir=str(work/'cluster'))
work.joinpath('values.yaml').write_text(json.dumps(render(config,'operator-values')[0]))
trusted = config | {'mongodb_operator_trust': {'refs': [{'name': 'mongodb-backup-ca', 'key': 'ca.crt'}], 'sha256': 'a'*64}}
work.joinpath('trusted-values.yaml').write_text(json.dumps(render(trusted,'operator-values')[0]))
old=config | {'mongodb_operator_ver':'1.23.0','mongodb_operator_chart_ver':'1.23.1'}
work.joinpath('upgrade-values.yaml').write_text(json.dumps(render(old,'operator-values')[0]))
work.joinpath('vars.json').write_text(json.dumps(config))
work.joinpath('disabled.json').write_text(json.dumps({'mongodb_install':'no'}))
work.joinpath('render.yml').write_text('''- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.template:
        src: "{{ base_dir }}/roles/cluster-addon/templates/mongodb/cluster.yaml.j2"
        dest: "{{ cluster_dir }}-rendered.yaml"
        mode: "0600"
''')
PY
promtool_version="$("$PROMTOOL" --version)"
[[ "$promtool_version" == *"version 3.13.1 "* ]] || { echo 'MongoDB static requires pinned promtool 3.13.1' >&2; exit 1; }
"$PROMTOOL" check config --syntax-only "$work/monitoring-prometheus.yml"
"$PROMTOOL" check rules "$work/monitoring-rules.yml"
"$PROMTOOL" test rules "$work/monitoring-tests.yml"
echo MONGODB_MONITORING_RULE_CONTRACT_PASS actual-promql=fire-and-resolve
for deployment in mongodb-gate mongodb-restore-logical mongodb-restore-physical mongodb-sharding-gate mongodb-ownership-gate; do
  "$ANSIBLE" -i "$work/$deployment.hosts" -c local "$work/inventory.yml" \
    -e "expected_deployment=$deployment" > "$work/$deployment-inventory.log"
done
echo MONGODB_INVENTORY_DIRECTORY_CONTRACT_PASS
"$HELM" template mongodb-operator roles/cluster-addon/files/psmdb-operator-1.23.2.tgz \
  --namespace mongodb --values "$work/values.yaml" > "$work/operator.yaml"
"$HELM" template mongodb-operator roles/cluster-addon/files/psmdb-operator-1.23.1.tgz \
  --namespace mongodb --values "$work/upgrade-values.yaml" > "$work/upgrade-operator.yaml"
"$HELM" template mongodb-operator roles/cluster-addon/files/psmdb-operator-1.23.2.tgz \
  --namespace mongodb --values "$work/trusted-values.yaml" \
  --post-renderer "$PY" --post-renderer-args "$ROOT/roles/cluster-addon/files/mongodb.py" \
  --post-renderer-args operator-render --post-renderer-args=--config --post-renderer-args "$work/vars.json" \
  --post-renderer-args=--manifest --post-renderer-args "$work/trusted-values.yaml" > "$work/trusted-operator.yaml"
"$PY" - "$work/trusted-operator.yaml" <<'PY'
import sys,yaml
from tests.unit.test_mongodb_delivery import check_operator_render
objects=[o for o in yaml.safe_load_all(open(sys.argv[1])) if o]
check_operator_render(objects,'mongodb')
deployment=next(o for o in objects if o['kind']=='Deployment')
spec=deployment['spec']['template']['spec']
assert spec['volumes']==[{'name':'kubeauto-backup-ca','projected':{'defaultMode':292,'sources':[
    {'secret':{'name':'mongodb-backup-ca','items':[{'key':'ca.crt','path':
        __import__('hashlib').sha256(b'mongodb-backup-ca/ca.crt').hexdigest()+'.crt'}]}}]}}]
assert any(e['name']=='SSL_CERT_DIR' for e in spec['containers'][0]['env'])
print('MONGODB_OPERATOR_CA_HELM_RENDER_PASS official-chart=unchanged ca-only=projected')
PY
"$PY" tests/helpers/mongodb-lab-storage.py render --base "$ROOT" > "$work/storage-values.json"
"$HELM" template mongodb-lab-longhorn tests/fixtures/longhorn-1.10.1.tgz \
  --namespace mongodb-lab-storage --values "$work/storage-values.json" > "$work/storage.yaml"
"$PY" - "$work/storage.yaml" <<'PY'
import sys,yaml
objects=[o for o in yaml.safe_load_all(open(sys.argv[1])) if o]
assert all(o['metadata'].get('namespace') in (None,'mongodb-lab-storage') for o in objects)
controllers=[o for o in objects if o['kind'] in ('Deployment','DaemonSet')]
assert controllers
for obj in controllers:
 spec=obj['spec']['template']['spec']
 assert spec['nodeSelector']=={'kubeauto.io/mongodb-storage':'true'}
 assert all(c['image'].startswith('registry.talkschool.cn:5000/mongodb-lab/brinnatt/') for c in spec['containers'])
print('MONGODB_STORAGE_HELM_RENDER_PASS')
PY
"$PY" - "$work/operator.yaml" "$work/upgrade-operator.yaml" <<'PY'
import sys,yaml
from tests.unit.test_mongodb_delivery import check_operator_render
for path,version in zip(sys.argv[1:],('1.23.1','1.23.0')):
 objects=[o for o in yaml.safe_load_all(open(path)) if o]
 deploy=next(o for o in objects if o['kind']=='Deployment')
 assert deploy['metadata']['name']=='mongodb-operator'
 assert deploy['spec']['replicas']==1
 c=deploy['spec']['template']['spec']['containers'][0]
 assert c['image'].endswith('/brinnatt/percona-server-mongodb-operator:'+version)
 check_operator_render(objects,'mongodb')
print('MONGODB_OFFICIAL_HELM_RENDER_PASS namespace-scope=true')
PY
"$ANSIBLE" -i localhost, -c local playbooks/07.cluster-addon.yml --syntax-check --tags mongodb
"$ANSIBLE" -i localhost, -c local playbooks/07.cluster-addon.yml --list-tasks --tags mongodb > "$work/tasks.txt"
"$PY" - "$work/tasks.txt" <<'PY'
import re,sys
rows=[r for r in open(sys.argv[1]) if 'cluster-addon :' in r]
assert rows and all('mongodb' in re.search(r'TAGS: \[(.*)\]',r).group(1).split(', ') for r in rows)
print('MONGODB_ISOLATED_TAGS_PASS')
PY
"$ANSIBLE" -i localhost, -c local "$work/render.yml" -e "@$work/vars.json" \
  -e "ansible_python_interpreter=$PY" > "$work/render.log"
"$PY" - "$work/cluster-rendered.yaml" <<'PY'
import sys,yaml
from tests.unit.test_mongodb_delivery import configuration,render
actual=yaml.safe_load(open(sys.argv[1]));expected=render(configuration())[0]
assert actual==expected
print('MONGODB_ANSIBLE_TEMPLATE_CONTRACT_PASS')
PY
"$ANSIBLE" -i localhost, -c local playbooks/07.cluster-addon.yml --check --tags mongodb \
  -e "@$work/disabled.json" -e "ansible_python_interpreter=$PY" > "$work/disabled.log"
"$PY" - "$work/disabled.log" <<'PY'
import re,sys
text=open(sys.argv[1]).read()
assert re.search(r'changed=0\s+unreachable=0\s+failed=0',text),text
assert 'MONGODB_DATA_PLANE_READY' not in text
print('MONGODB_DISABLED_PASS changed=0')
PY
echo MONGODB_STATIC_PASS

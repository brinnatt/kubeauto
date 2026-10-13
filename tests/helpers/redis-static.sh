#!/usr/bin/env bash
# Cheapest Redis gate: no SSH, source sync, image pull or cluster mutation.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PY="${REDIS_PYTHON:-$ROOT/.venv/bin/python}"
HELM="${REDIS_HELM:-helm}"
ANSIBLE="${REDIS_ANSIBLE_PLAYBOOK:-ansible-playbook}"
work="$(mktemp -d /tmp/kubeauto-redis-static.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
export PYTHONPATH="$ROOT"
export PYTHONPYCACHEPREFIX="$work/cache"
export ANSIBLE_ROLES_PATH="$ROOT/roles"
echo 'REDIS_STAGE_BEGIN stage=static-syntax'
for file in tests/helpers/redis-*.sh tests/run_enterprise_regression.sh; do bash -n "$file"; done
bash -n roles/cluster-addon/files/redis-operator-rbac.sh
"$PY" -m py_compile roles/cluster-addon/files/redis-{health,snapshot,crd,operator-rbac}.py tests/helpers/redis-{config,artifacts,lab-config,lab-storage,focused-gate,idempotence-probe,sentinel-probe,rotation-gate,backup-gate,performance-gate,observability-gate,capacity-gate,cluster-gate,cluster-client-gate,cluster-snapshot-gate,cluster-process,cluster-failure-gate,cluster-rebalance-gate,cluster-probe,dns-probe,evidence,stability-gate,crd-upgrade-gate,platform-gate,maintenance-gate,upgrade-gate,cluster-upgrade-gate,cleanup-gate,runtime-module-gate,full-evidence}.py
"$PY" tests/helpers/validate_redis_matrix.py tests/redis-test-matrix.yaml
echo 'REDIS_STAGE_BEGIN stage=static-contracts'
"$PY" -m unittest tests.unit.test_redis_delivery tests.unit.test_redis_operator_rbac tests.unit.test_redis_focused_gate tests.unit.test_redis_rotation_gate tests.unit.test_redis_cluster_snapshot_gate tests.unit.test_redis_backup_gate tests.unit.test_redis_performance_gate tests.unit.test_redis_observability_gate tests.unit.test_redis_capacity_gate tests.unit.test_redis_cluster_gate tests.unit.test_redis_cluster_client_gate tests.unit.test_redis_cluster_process tests.unit.test_redis_cluster_failure_gate tests.unit.test_redis_cluster_rebalance_gate tests.unit.test_redis_evidence tests.unit.test_redis_stability_gate tests.unit.test_redis_crd_upgrade tests.unit.test_redis_platform_gate tests.unit.test_redis_maintenance_gate tests.unit.test_redis_upgrade_gate tests.unit.test_redis_cluster_upgrade_gate tests.unit.test_redis_cleanup_gate tests.unit.test_redis_lab_clean_gate tests.unit.test_redis_full_gate tests.unit.test_redis_full_evidence tests.unit.test_six_repo_version_sync tests.unit.test_middleware_documentation -v
echo 'REDIS_STAGE_BEGIN stage=official-chart-render'
"$PY" - "$work" <<'PY'
import sys
from pathlib import Path
import yaml
from jinja2 import Environment, StrictUndefined
work = Path(sys.argv[1])
env = Environment(undefined=StrictUndefined)
template = Path('roles/cluster-addon/templates/redis/operator-values.yaml.j2').read_text()
work.joinpath('operator-values.yaml').write_text(env.from_string(template).render(
    redis_operator_ver='0.27.0', redis_operator_image_ver='0.27.0-kubeauto.2', redis_namespace='redis', redis_operator_replicas=2,
    redis_acl_secret='', redis_maxmemory_percent=0))
work.joinpath('legacy-operator-values.yaml').write_text(env.from_string(template).render(
    redis_operator_ver='0.26.0', redis_operator_image_ver='0.26.0', redis_namespace='redis',
    redis_operator_replicas=2, redis_acl_secret='', redis_maxmemory_percent=0))
work.joinpath('disabled.yaml').write_text(yaml.safe_dump({'redis_install': 'no'}))
PY
REDIS_POST_RENDER_PYTHON="$PY" "$HELM" template redis-operator roles/cluster-addon/files/redis-operator-0.27.0.tgz \
  --namespace redis-operator --values "$work/operator-values.yaml" \
  --post-renderer "$ROOT/roles/cluster-addon/files/redis-operator-rbac.sh" > "$work/operator.yaml"
"$PY" - "$work/operator.yaml" <<'PY'
import sys,yaml
docs = list(yaml.safe_load_all(open(sys.argv[1])))
deploy = next(d for d in docs if d and d['kind']=='Deployment')
assert deploy['spec']['replicas']==2
assert deploy['spec']['selector']['matchLabels']=={'name': 'redis-operator'}
assert all(deploy['spec']['template']['metadata']['labels'].get(k)==v
           for k,v in deploy['spec']['selector']['matchLabels'].items())
container = deploy['spec']['template']['spec']['containers'][0]
gates = next(e['value'] for e in container['env'] if e['name']=='FEATURE_GATES')
assert 'GenerateConfigInInitContainer=true' in gates
assert container['image']=='registry.talkschool.cn:5000/brinnatt/redis-operator:0.27.0-kubeauto.2'
role = next(d for d in docs if d['kind']=='ClusterRole' and d['metadata']['name']=='redis-operator')
assert [r for r in role['rules'] if 'nodes' in r.get('resources', [])] == [
    {'apiGroups':[''], 'resources':['nodes'], 'verbs':['list']}]
print('REDIS_OPERATOR_NODE_DISCOVERY_RENDER_PASS permission=list-only ownership=helm')
print(f'REDIS_HELM_RENDER_PASS resources={len(docs)}')
PY
"$HELM" template redis-operator tests/fixtures/redis-operator-0.26.0.tgz \
  --namespace redis-operator --values "$work/legacy-operator-values.yaml" > "$work/legacy-operator.yaml"
"$PY" - "$work/legacy-operator.yaml" <<'PY'
import sys,yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
deployment = next(d for d in docs if d['kind']=='Deployment')
container = deployment['spec']['template']['spec']['containers'][0]
gates = [e['value'] for e in container['env'] if e['name']=='FEATURE_GATES']
assert gates == ['GenerateConfigInInitContainer=true'], gates
assert container['image']=='registry.talkschool.cn:5000/brinnatt/redis-operator:0.26.0'
assert deployment['spec']['replicas']==2
print('REDIS_LEGACY_HELM_RENDER_PASS registered-feature-gates=exact')
PY
"$PY" tests/helpers/redis-lab-storage.py render --base "$ROOT" > "$work/storage-values.json"
"$HELM" template redis-lab-longhorn tests/fixtures/longhorn-1.10.1.tgz \
  --namespace redis-lab-storage --kube-version 1.33.6 --values "$work/storage-values.json" > "$work/storage.yaml"
"$PY" - "$work/storage.yaml" <<'PY'
import sys,yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
assert all(d['metadata'].get('namespace','redis-lab-storage')=='redis-lab-storage' for d in docs)
controllers = [d for d in docs if d['kind'] in ('Deployment','DaemonSet')]
assert any(d['metadata']['name']=='longhorn-driver-deployer' for d in controllers)
for doc in controllers:
    assert doc['spec']['template']['spec']['nodeSelector']=={'kubeauto.io/redis-storage':'true'}
    for c in doc['spec']['template']['spec']['containers']:
        assert c['image'].startswith('registry.talkschool.cn:5000/redis-lab/brinnatt/')
print(f'REDIS_STORAGE_HELM_RENDER_PASS resources={len(docs)}')
PY
echo 'REDIS_STAGE_BEGIN stage=ansible-entry-contract'
"$ANSIBLE" -i localhost, -c local playbooks/07.cluster-addon.yml --syntax-check --tags redis
"$ANSIBLE" -i localhost, -c local playbooks/07.cluster-addon.yml --list-tasks --tags redis > "$work/tasks.txt"
"$PY" - "$work/tasks.txt" <<'PY'
import re,sys
tasks=[s for s in open(sys.argv[1]) if 'cluster-addon :' in s]
assert tasks and all('redis' in re.search(r'TAGS: \[(.*)\]',s).group(1).split(', ') for s in tasks)
print(f'REDIS_ISOLATED_TASKS_PASS count={len(tasks)}')
PY
"$ANSIBLE" -i localhost, -c local playbooks/07.cluster-addon.yml --check --tags redis \
  --extra-vars "@$work/disabled.yaml" > "$work/disabled.log"
"$PY" - "$work/disabled.log" <<'PY'
import re,sys
log=open(sys.argv[1]).read()
assert re.search(r'changed=0\s+unreachable=0\s+failed=0',log),log
assert 'REDIS_DATA_PLANE_READY' not in log
print('REDIS_DISABLED_CHECK_PASS changed=0')
PY
echo REDIS_STATIC_GATE_PASS

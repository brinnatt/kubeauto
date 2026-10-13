#!/usr/bin/env python3
"""Helm post-renderer for the pinned Cluster failure-domain admission revision."""
import sys

import yaml


def node_discovery(documents):
    deployments = [doc for doc in documents if doc.get('kind') == 'Deployment'
                   and doc.get('metadata', {}).get('name') == 'redis-operator']
    if len(deployments) != 1:
        raise ValueError('expected one pinned Redis Operator Deployment')
    deployment = deployments[0]
    containers = deployment['spec']['template']['spec']['containers']
    operators = [container for container in containers if container['name'] == 'redis-operator']
    if len(operators) != 1:
        raise ValueError('expected one Redis Operator container')
    if operators[0]['image'].rsplit(':', 1)[-1] != '0.27.0-kubeauto.2':
        return documents
    roles = [doc for doc in documents if doc.get('kind') == 'ClusterRole'
             and doc.get('metadata', {}).get('name') == 'redis-operator']
    bindings = [doc for doc in documents if doc.get('kind') == 'ClusterRoleBinding'
                and doc.get('metadata', {}).get('name') == 'redis-operator']
    if len(roles) != 1 or len(bindings) != 1:
        raise ValueError('failure-domain admission requires the pinned cluster RBAC scope')
    namespace = deployment['metadata']['namespace']
    if (bindings[0]['roleRef'] != {'apiGroup': 'rbac.authorization.k8s.io',
                                  'kind': 'ClusterRole', 'name': 'redis-operator'} or
            bindings[0]['subjects'] != [{'kind': 'ServiceAccount', 'name': 'redis-operator',
                                         'namespace': namespace}]):
        raise ValueError('unexpected Redis Operator RBAC binding')
    rule = {'apiGroups': [''], 'resources': ['nodes'], 'verbs': ['list']}
    node_rules = [item for item in roles[0]['rules'] if 'nodes' in item.get('resources', [])]
    if node_rules and node_rules != [rule]:
        raise ValueError('unexpected Redis Operator node permissions')
    if not node_rules:
        roles[0]['rules'].append(rule)
    return documents


def main():
    try:
        documents = [doc for doc in yaml.safe_load_all(sys.stdin) if doc]
        if not all(isinstance(doc, dict) for doc in documents):
            raise ValueError('invalid rendered Kubernetes document')
        result = node_discovery(documents)
    except (ValueError, KeyError, TypeError, yaml.YAMLError):
        print('Redis Operator post-render rejected an unexpected pinned Chart contract', file=sys.stderr)
        return 1
    yaml.safe_dump_all(result, sys.stdout, sort_keys=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

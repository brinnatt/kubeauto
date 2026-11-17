"""Local etcd wire fixture; v3.32 node startup creates the missing BGP default."""
import base64
import json
from pathlib import Path
import sys

root = Path(sys.argv[0]).parents[1]
path = root/'datastore.json'
data = json.loads(path.read_text()) if path.exists() else {
    '/calico/resources/v3/projectcalico.org/ippools/default-ipv4-ippool':
        {'spec': {'cidr': '10.244.0.0/16'}}}
node = json.loads((root/'state.json').read_text())['objects']['daemonset/calico-node']
image = node['spec']['template']['spec']['containers'][0]['image']
key = '/calico/resources/v3/projectcalico.org/bgpconfigurations/default'
if image.endswith(':v3.32.4') and key not in data:
    data[key] = {'kind': 'BGPConfiguration', 'apiVersion': 'projectcalico.org/v3',
                 'metadata': {'name': 'default', 'uid': 'generated-bgp-uid',
                              'creationTimestamp': '2026-11-16T13:10:17Z'},
                 'spec': {'logSeverityScreen': 'Info',
                          'serviceLoadBalancerAggregation': 'Enabled'}}
path.write_text(json.dumps(data))
prefix = sys.argv[sys.argv.index('get')+1]
print(json.dumps({'kvs': [
    {'key': base64.b64encode(key.encode()).decode(),
     'value': base64.b64encode(json.dumps(value).encode()).decode()}
    for key, value in sorted(data.items()) if key.startswith(prefix)]}))

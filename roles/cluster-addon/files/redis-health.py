#!/usr/bin/env python3
"""Read-only Redis convergence check for the pinned OpsTree resource model."""
import argparse
import json
import subprocess
import time

AUTH_CLI_PREFIX = [
    'sh', '-c',
    'REDISCLI_AUTH="${REDISCLI_AUTH:-${REDIS_PASSWORD:-}}"; '
    'if [ -n "${REDISCLI_AUTH:-}" ]; then '
    'REDISCLI_AUTH="$(printf %s "$REDISCLI_AUTH" | tr -d "\\r\\n")"; '
    'export REDISCLI_AUTH; fi; exec redis-cli "$@"', 'redis-cli',
]


def ready(pod):
    return (not pod["metadata"].get("deletionTimestamp") and
            pod.get("status", {}).get("phase") == "Running" and
            any(c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", [])))


def check_resources(pods, claims, expected, topology):
    by_role = {}
    for pod in pods:
        role = pod["metadata"].get("labels", {}).get("role")
        if role in expected:
            by_role.setdefault(role, []).append(pod)
    for role, count in expected.items():
        members = by_role.get(role, [])
        if len(members) != count or not all(ready(p) for p in members):
            raise ValueError(f"role={role} expected={count} ready={sum(ready(p) for p in members)}")
        if len({p["spec"]["nodeName"] for p in members}) < min(3, count):
            raise ValueError(f"role={role} has fewer than three scheduled nodes")
        domains = {topology.get(p["spec"]["nodeName"]) for p in members}
        if None in domains or len(domains) < min(3, count):
            raise ValueError(f"role={role} has insufficient labelled failure domains")
        for pod in members:
            attached = []
            for volume in pod["spec"].get("volumes", []):
                claim = volume.get("persistentVolumeClaim", {}).get("claimName")
                if claim:
                    attached.append(claim)
                if claim and claims.get(claim) != "Bound":
                    raise ValueError(f"PVC={claim} is not Bound")
            if role != 'sentinel' and not attached:
                raise ValueError(f"Pod={pod['metadata']['name']} has no persistent data claim")
    return by_role


def info(text):
    return dict(line.split(":", 1) for line in text.splitlines()
                if ":" in line and not line.startswith("#"))


def primary_addresses(pod, namespace, cluster_domain):
    name = pod['metadata']['name']
    hostname = pod['spec'].get('hostname', name)
    subdomain = pod['spec'].get('subdomain')
    addresses = {pod['status']['podIP'], name}
    if subdomain:
        addresses.add(f'{hostname}.{subdomain}.{namespace}.svc.{cluster_domain}')
    return addresses


def sentinel_endpoint_matches(reply, primary, namespace, cluster_domain):
    return (len(reply) == 2 and reply[1] == '6379' and
            reply[0] in primary_addresses(primary, namespace, cluster_domain))


def cluster_members(text, pods, topology, shards, replicas):
    """Validate the actual Redis replication graph, independently of Pod labels."""
    by_ip = {p['status']['podIP']: p for p in pods}
    members = {}
    covered = set()
    for line in text.splitlines():
        row = line.split()
        if len(row) < 8 or row[0] in members:
            raise ValueError('invalid or duplicate Cluster member')
        flags = set(row[2].split(','))
        if flags & {'fail', 'fail?', 'handshake', 'noaddr'} or row[7] != 'connected':
            raise ValueError('Cluster member is disconnected or failing')
        host = row[1].split(',')[0].split('@')[0].rsplit(':', 1)[0]
        if host not in by_ip:
            raise ValueError('Cluster member does not belong to the declared Pods')
        pod = by_ip[host]
        slots = []
        for token in row[8:]:
            if token.startswith('['):
                raise ValueError('Cluster slots are still migrating')
            parts = token.split('-')
            low, high = int(parts[0]), int(parts[-1])
            if len(parts) > 2 or not 0 <= low <= high <= 16383:
                raise ValueError('invalid Cluster slot range')
            slots.extend(range(low, high + 1))
        is_primary = 'master' in flags
        if is_primary == ('slave' in flags) or (slots and not is_primary):
            raise ValueError('invalid Cluster role or replica owns slots')
        if covered.intersection(slots) or len(set(slots)) != len(slots):
            raise ValueError('duplicate Cluster slot ownership')
        covered.update(slots)
        members[row[0]] = {'primary': is_primary, 'parent': row[3],
                          'pod': pod['metadata']['name'],
                          'domain': topology[pod['spec']['nodeName']], 'slots': tuple(slots)}
    primaries = {node_id: member for node_id, member in members.items() if member['primary']}
    if (len(members) != len(pods) or len(primaries) != shards or
            len({m['pod'] for m in members.values()}) != len(pods) or len(covered) != 16384):
        raise ValueError('Cluster membership or complete slot coverage is missing')
    for node_id, primary in primaries.items():
        followers = [m for m in members.values() if not m['primary'] and m['parent'] == node_id]
        if len(followers) != replicas or not primary['slots']:
            raise ValueError('Cluster primary replica count or slots are incomplete')
        if len({m['domain'] for m in [primary, *followers]}) < 2:
            raise ValueError('Cluster primary and all its replicas share a failure domain')
    if any(not m['primary'] and m['parent'] not in primaries for m in members.values()):
        raise ValueError('Cluster replica references an unknown primary')
    return members


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--mode", choices=("sentinel", "cluster"), required=True)
    parser.add_argument("--replication-replicas", type=int, default=3)
    parser.add_argument("--shards", type=int, default=3)
    parser.add_argument("--replicas-per-shard", type=int, default=2)
    parser.add_argument("--topology-key", default="kubernetes.io/hostname")
    parser.add_argument('--service-dns-domain', default='cluster.local')
    parser.add_argument("--tls", action=argparse.BooleanOptionalAction)
    parser.add_argument("--wait", type=int, default=600)
    args = parser.parse_args()
    kubectl = ["kubectl", "--kubeconfig=" + args.kubeconfig]
    containers = {}

    def run(argv):
        result = subprocess.run(kubectl + argv, capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise ValueError("Kubernetes API/exec request failed; inspect events and controller logs")
        return result.stdout.strip()

    def get(kind, *extra):
        return json.loads(run(["get", kind, *extra, "-o", "json"]))

    def cli(pod, port, *command):
        tls = (["--tls", "--cacert", "/tls/ca.crt", "--cert", "/tls/tls.crt",
                "--key", "/tls/tls.key"] if args.tls else [])
        result = run(["-n", args.namespace, "exec", pod, "-c", containers[pod], "--",
                      *AUTH_CLI_PREFIX, "-e", "--raw", "--no-auth-warning", *tls,
                      "-p", str(port), *command])
        if result.startswith(("ERR", "NOAUTH", "WRONGPASS", "NOREPLICAS", "CLUSTERDOWN")):
            raise ValueError("Redis command rejected during health verification")
        return result

    expected = ({"replication": args.replication_replicas, "sentinel": 3}
                if args.mode == "sentinel" else
                {"leader": args.shards, "follower": args.shards * args.replicas_per_shard})
    start = time.monotonic()
    while True:
        try:
            ns = ["-n", args.namespace]
            pods = get("pods", *ns, "-l", "kubeauto.io/component=redis")["items"]
            pods = [p for p in pods if p["metadata"].get("labels", {}).get("app") in
                    (args.name, args.name + "-sentinel", args.name + "-sentinel-sentinel",
                     args.name + "-leader", args.name + "-follower")]
            containers = {p["metadata"]["name"]: next(ref["name"] for ref in
                          p["metadata"].get("ownerReferences", []) if ref["kind"] == "StatefulSet")
                          for p in pods}
            claims = {p["metadata"]["name"]: p.get("status", {}).get("phase")
                      for p in get("pvc", *ns)["items"]}
            topology = {n["metadata"]["name"]: n["metadata"].get("labels", {}).get(args.topology_key)
                        for n in get("nodes")["items"]}
            by_role = check_resources(pods, claims, expected, topology)
            if args.mode == "sentinel":
                masters = []
                replication = {}
                for pod in by_role["replication"]:
                    name = pod["metadata"]["name"]
                    status = info(cli(name, 6379, "INFO", "replication"))
                    replication[name] = status
                    if status.get("role") == "master":
                        if int(status.get("connected_slaves", "0")) != args.replication_replicas - 1:
                            raise ValueError("primary has not connected all expected replicas")
                        masters.append(name)
                    elif status.get("role") != "slave" or status.get("master_link_status") != "up":
                        raise ValueError("replica link is not up")
                if len(masters) != 1:
                    raise ValueError("replication does not have exactly one primary")
                master = get("redisreplication", args.name, *ns).get("status", {}).get("masterNode")
                if master != masters[0]:
                    raise ValueError("controller primary and Redis role disagree")
                primary = next(p for p in by_role['replication'] if p['metadata']['name'] == master)
                for name, status in replication.items():
                    if name != master and (status.get('master_host') not in primary_addresses(
                            primary, args.namespace, args.service_dns_domain) or
                                           status.get('master_port') != '6379' or
                                           status.get('master_sync_in_progress') != '0'):
                        raise ValueError('replica has not synchronized with the sole primary')
                for pod in by_role["sentinel"]:
                    if not cli(pod["metadata"]["name"], 26379, "SENTINEL", "CKQUORUM", "mymaster").startswith("OK"):
                        raise ValueError("Sentinel cannot authorize a failover")
                    discovered = cli(pod['metadata']['name'], 26379, 'SENTINEL',
                                     'get-master-addr-by-name', 'mymaster').splitlines()
                    if not sentinel_endpoint_matches(discovered, primary, args.namespace, args.service_dns_domain):
                        raise ValueError('Sentinel discovery disagrees with the sole primary')
                endpoints = get("endpoints", args.name + "-master", *ns)
                addresses = [a for s in endpoints.get("subsets", []) for a in s.get("addresses", [])]
                if len(addresses) != 1 or addresses[0].get("targetRef", {}).get("name") != masters[0]:
                    raise ValueError("write Service endpoint does not select the sole primary")
            else:
                status = get("rediscluster", args.name, *ns).get("status", {})
                if status.get("state") != "Ready":
                    raise ValueError("RedisCluster has not reached Ready")
                view = None
                for pod in pods:
                    state = info(cli(pod["metadata"]["name"], 6379, "CLUSTER", "INFO"))
                    if (state.get("cluster_state") != "ok" or state.get("cluster_slots_ok") != "16384" or
                            state.get("cluster_slots_fail") != "0" or state.get("cluster_slots_pfail") != "0" or
                            state.get("cluster_size") != str(args.shards) or
                            state.get("cluster_known_nodes") != str(sum(expected.values()))):
                        raise ValueError("Cluster state, slots or membership is incomplete")
                    members = cluster_members(cli(pod['metadata']['name'], 6379, 'CLUSTER', 'NODES'),
                                              pods, topology, args.shards, args.replicas_per_shard)
                    if view is not None and view != members:
                        raise ValueError('Cluster members disagree on roles, replicas or slots')
                    view = members
            print(f"REDIS_CONTROL_PLANE_READY mode={args.mode}")
            print(f"REDIS_DATA_PLANE_READY mode={args.mode} members={sum(expected.values())}")
            return 0
        except (ValueError, KeyError, StopIteration, OSError, subprocess.TimeoutExpired) as exc:
            elapsed = int(time.monotonic() - start)
            print(f"REDIS_WAIT_HEARTBEAT elapsed={elapsed} mode={args.mode} state={exc}", flush=True)
            if elapsed >= args.wait:
                print(f"REDIS_HEALTH_FAIL mode={args.mode} reason=convergence-timeout")
                return 1
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())

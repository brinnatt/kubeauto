#!/usr/bin/env bash
# Product-owned RWX integration and leader replacement, on the Rocky 9.8 lease.
verify_nfs_production() (
  set -euo pipefail
  local cluster="$1" cfg="$2" namespace=kubeauto-nfs-production
  local run="${3:?owned run ID required}" owns_namespace=0 owns_server=0
  local evidence old_uid new_uid old_pod
  evidence=$(umask 077; mktemp -d /var/tmp/kubeauto-nfs-production.XXXXXX)
  server_fixture() {
    "${SSH[@]}" root@192.168.122.238 "python3 - $1 '$run'" \
      <"$BASE/tests/helpers/nfs-production-fixture.py"
  }
  finish_nfs() {
    local initial_rc=$? cleanup_rc=0
    trap - EXIT
    if (( initial_rc != 0 )); then
      kubectl -n "$namespace" get pods,pvc,events -o wide >"$evidence/business-state.log" 2>&1 || true
      kubectl -n kube-system describe pods -l app=nfs-client-provisioner >"$evidence/provisioner-describe.log" 2>&1 || true
      kubectl -n kube-system logs deployment/nfs-client-provisioner --tail=200 >"$evidence/provisioner.log" 2>&1 || true
    fi
    if (( owns_namespace )); then
      kubectl delete namespace "$namespace" --wait=true --timeout=180s || cleanup_rc=1
      local attempt volumes_clean=0
      for attempt in $(seq 1 24); do
        if kubectl get pv -o json | python3 -c \
          'import json,sys; assert not any(v.get("spec",{}).get("claimRef",{}).get("namespace")==sys.argv[1] for v in json.load(sys.stdin)["items"])' \
          "$namespace"; then volumes_clean=1; break; fi
        sleep 5
      done
      (( volumes_clean )) || cleanup_rc=1
    fi
    # Keep the export until all clients have unmounted and all claims are gone.
    if (( owns_server && cleanup_rc == 0 )); then
      kubectl -n kube-system delete deployment nfs-client-provisioner --ignore-not-found \
        --cascade=foreground --wait=true --timeout=120s || cleanup_rc=1
      if (( cleanup_rc == 0 )); then server_fixture cleanup || cleanup_rc=1; fi
    fi
    sha256sum "$evidence"/* >"$evidence/SHA256SUMS" 2>/dev/null || true
    chmod 0600 "$evidence"/* 2>/dev/null || true
    if (( initial_rc == 0 && cleanup_rc == 0 )); then
      echo "NFS_PRODUCTION_SCOPED_CLEAN_PASS evidence=$evidence"
    else
      echo "NFS_PRODUCTION_DIAGNOSTICS directory=$evidence"
      exit 1
    fi
  }
  trap finish_nfs EXIT
  if kubectl get namespace "$namespace" >/dev/null 2>&1; then exit 1; fi
  if kubectl -n kube-system get deployment nfs-client-provisioner >/dev/null 2>&1; then exit 1; fi
  owns_server=1
  server_fixture prepare
  kubecli download -E nfs-provisioner </dev/null
  set_cfg "$cfg" nfs_provisioner_install '"yes"'
  set_cfg "$cfg" nfs_provisioner_namespace '"kube-system"'
  set_cfg "$cfg" nfs_server '"192.168.122.238"'
  set_cfg "$cfg" nfs_path '"/var/tmp/kubeauto-nfs-production-data"'
  kubecli setup "$cluster" 07 </dev/null
  cp "$WORK/yml/nfs-provisioner/nfs-provisioner.yaml" "$evidence/rendered.yaml"
  kubectl -n kube-system rollout status deployment/nfs-client-provisioner --timeout=180s
  kubectl create namespace "$namespace"
  owns_namespace=1
  create_clients() {
    local suffix="$1" number node
    cat <<YAML | kubectl -n "$namespace" apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: rwx-$suffix}
spec:
  accessModes: [ReadWriteMany]
  storageClassName: managed-nfs-storage
  resources: {requests: {storage: 1Gi}}
YAML
    for number in 1 2; do
      node="rocky9-worker$number"
      cat <<YAML | kubectl -n "$namespace" apply -f -
apiVersion: v1
kind: Pod
metadata: {name: client-$suffix-$number}
spec:
  nodeSelector: {kubernetes.io/hostname: $node}
  containers:
  - name: client
    image: registry.talkschool.cn:5000/brinnatt/json-mock:v1.3.1
    command: [node, -e, 'setInterval(() => {}, 1000)']
    volumeMounts: [{name: shared, mountPath: /data}]
  volumes: [{name: shared, persistentVolumeClaim: {claimName: rwx-$suffix}}]
YAML
    done
    kubectl -n "$namespace" wait --for=condition=Ready "pod/client-$suffix-1" "pod/client-$suffix-2" --timeout=240s
  }
  write_data() {
    kubectl -n "$namespace" exec "client-$1-1" -- node -e \
      'const fs=require("fs"); const fd=fs.openSync("/data/payload","w"); fs.writeSync(fd,process.argv[1]); fs.fsyncSync(fd); fs.closeSync(fd);' "$run-$1"
  }
  read_data() {
    local suffix="$1" actual
    actual=$(kubectl -n "$namespace" exec "client-$suffix-2" -- node -e \
      'process.stdout.write(require("fs").readFileSync("/data/payload"))')
    [[ "$actual" == "$run-$suffix" ]]
    echo "NFS_RWX_FSYNC_READBACK_PASS volume=$suffix writer=rocky9-worker1 reader=rocky9-worker2"
  }
  create_clients original
  write_data original
  read_data original
  old_pod=$(kubectl -n kube-system get pods -l app=nfs-client-provisioner -o jsonpath='{.items[0].metadata.name}')
  old_uid=$(kubectl -n kube-system get pod "$old_pod" -o jsonpath='{.metadata.uid}')
  kubectl -n kube-system logs "$old_pod" >"$evidence/original-leader.log"
  kubectl -n kube-system delete pod "$old_pod" --wait=true --timeout=120s
  kubectl -n kube-system rollout status deployment/nfs-client-provisioner --timeout=180s
  new_uid=$(kubectl -n kube-system get pods -l app=nfs-client-provisioner -o jsonpath='{.items[0].metadata.uid}')
  [[ -n "$old_uid" && -n "$new_uid" && "$old_uid" != "$new_uid" ]]
  create_clients replacement
  write_data replacement
  read_data replacement
  read_data original
  kubectl -n kube-system logs deployment/nfs-client-provisioner >"$evidence/replacement-leader.log"
  grep -q 'Started provisioner controller' "$evidence/replacement-leader.log"
  echo NFS_LEADER_REPLACEMENT_BUSINESS_PASS
  echo NFS_PRODUCTION_FOCUSED_PASS
)

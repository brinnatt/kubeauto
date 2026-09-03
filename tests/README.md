# Enterprise regression handbook

`tests/` is the executable delivery knowledge base for kubeauto. It captures the reusable lessons from the Cursor-era regression work and the 2026-07-25 through 2026-07-29 Codex full-chain run. The source of truth is executable evidence, not a narrative claim or an old log.

## Acceptance contract

A delivery run is complete only when all of the following are true:

- the affected unit and contract tests pass;
- the current live scenario emits its documented success marker;
- the durable wrapper records `rc=0`;
- the supervisor reports zero failure markers;
- `enterprise-test-matrix.yaml` has no affected pending or failed item;
- the six-repository compatibility tests pass;
- final cleanup emits `LAB_CLEAN_VERIFY_PASS`.

Before any enterprise gate starts, the runner executes
`helpers/validate-test-matrix.py`. This parses the YAML and recalculates every
Tier total/status count and the overall `N/N` declaration from the item
details. It rejects stale summaries, duplicate IDs, missing/unknown statuses,
or any non-pass item; a delivery gate cannot emit PASS while this check fails.

Do not mark a matrix item pass from historical evidence. A Pod merely existing or showing `Running` is insufficient when the component has multiple containers, asynchronous custom resources, readiness conditions, data-path checks, or a required business read/write operation.

For a newly created Kubernetes cluster, Node/Pod `Ready` is also insufficient.
The fixed live gate must deploy an application, satisfy its readiness probe,
resolve a Service through cluster DNS, complete an HTTP write/read through the
ClusterIP data path, and emit `KUBERNETES_PRODUCTION_SMOKE_PASS`.  The reusable
implementation is `helpers/kubernetes-production-smoke.sh`.

## Laboratory topology

| Role | Address | Account | Contract |
| --- | --- | --- | --- |
| Development only | 192.168.47.129 | brinnatt | Edit and sync only; never a test node |
| Jumper | 192.168.47.130 | root | Rocky 8.10, Ansible 2.16 control path |
| Workers | 192.168.47.131-133 | root | Rocky 8.10, small memory, reserved disabled |
| Masters | 192.168.47.134-136 | root | Rocky 8.10, small memory, reserved disabled |
| Debian node | 192.168.47.128 | brinnatt + sudo | Debian compatibility |
| Reserved/runtime | 192.168.47.137 | root | Rocky 8.10, 8 CPU / about 16 GiB currently provisioned; live 2 CPU + 4 GiB reservation gate |
| AIO/control/reserved | 192.168.47.138 | ubuntu + sudo | Ubuntu AIO and primary regression control |
| Ansible compatibility | 192.168.47.141 | root | Anolis OS 23.3 clean-snapshot control path |
| Ansible compatibility | 192.168.47.142 | root | openEuler 22.03 LTS-SP4 clean-snapshot control path |
| Ansible compatibility | 192.168.47.143 | root | openSUSE Leap 16.0 clean-snapshot control path |

The current external load-balancer VIP is `192.168.47.250:8443`. Addresses 140 and 147 belong to retired kubeauto lab layouts and must not appear in active regression configuration. Addresses 141-143 are dedicated compatibility controls and must not be silently reused as ordinary Kubernetes nodes. A negative unit-test fixture may contain a retired address only to prove that it is removed.

The production reserved-resource sizing baseline remains 16 CPU / 32 GiB. The
smaller 137 lab host validates the effective 2 CPU + 4 GiB Allocatable delta and
cgroup placement; it must not be reported as proof that the lab itself has 32 GiB.

SSH keys are the primary authentication path. The scripts use `BatchMode=yes` for SSH/SCP/rsync. A snapshot restore may remove the installed key on 141-143; the fixed runner invokes `tests/helpers/lab-ssh-bootstrap.sh`, which accepts the already-authorized credential only through the runtime `LAB_SSH_PASSWORD` environment variable and then returns to `BatchMode=yes`. Do not copy that credential into files, command output or retained logs.

## One top-level runner

Run all lab work from the development host through:

```bash
bash tests/run_enterprise_regression.sh --all-delivery
```

`--all-delivery` is the unattended final-signoff sequence. It composes the
already focused and cleanup-safe modes in this order: jumper, nerdctl, Docker,
Kubernetes patch upgrade, the delivery-gaps full chain (which includes the
authoritative core topology regression), the Rocky 8 customer-binary build,
cross-distribution Ansible gates, and the nine Tier3 frozen-tool gates. A failed
sub-mode stops the sequence after that mode's mandatory cleanup.

Useful fixed modes:

```bash
bash tests/run_enterprise_regression.sh --status
bash tests/run_enterprise_regression.sh --follow
bash tests/run_enterprise_regression.sh
bash tests/run_enterprise_regression.sh --aio-only
bash tests/run_enterprise_regression.sh --jumper-only
bash tests/run_enterprise_regression.sh --nerdctl-only
bash tests/run_enterprise_regression.sh --docker-only
bash tests/run_enterprise_regression.sh --registry-reboot-only
bash tests/run_enterprise_regression.sh --upgrade-only
bash tests/run_enterprise_regression.sh --gaps-only
bash tests/run_enterprise_regression.sh --diagnose-gaps-last
bash tests/run_enterprise_regression.sh --ansible-os-probe
bash tests/run_enterprise_regression.sh --ansible-anolis-probe
bash tests/run_enterprise_regression.sh --ansible-anolis-container-probe
bash tests/run_enterprise_regression.sh --ansible-os-only
bash tests/run_enterprise_regression.sh --build-rocky8-kubecli
bash tests/run_enterprise_regression.sh --tier3-tools-only
bash tests/run_enterprise_regression.sh --mysql-only
bash tests/run_enterprise_regression.sh --mysql-status
bash tests/run_enterprise_regression.sh --mysql-follow
bash tests/run_enterprise_regression.sh --kafka-only
bash tests/run_enterprise_regression.sh --kafka-status
bash tests/run_enterprise_regression.sh --kafka-follow
bash tests/run_enterprise_regression.sh --kafka-clean-only
```

`--mysql-only` owns the independent Percona PXC matrix in
`mysql-test-matrix.yaml`. It uses the dedicated six-node MySQL laboratory,
does not enter the delivered core topology or `--all-delivery`, and always
performs scoped PXC cleanup and verification before and after the run.

`--kafka-only` owns the independent Strimzi/Kafka matrix in
`kafka-test-matrix.yaml`. It uses the approved six-node Kafka laboratory,
does not enter the delivered core topology, PXC branch, or `--all-delivery`,
and always performs Kafka-owned cleanup and verification before and after the
run. Use `--kafka-status` and `--kafka-follow` for durable state and live logs;
use `--kafka-cancel` only to stop the Kafka-owned durable job.

The runner is authoritative because it centralizes SSH, source synchronization, remote launch, durable PID/exit state, foreground log streaming, heartbeat, silent-stall diagnostics, final markers, and cleanup. Do not replace it with a sequence of manually approved SSH commands.

## Required lifecycle

For every stateful run:

1. Stop a stale durable job that owns the same hosts.
2. Run `bash tests/helpers/lab-wipe-nodes.sh`.
3. Run `bash tests/helpers/lab-wipe-nodes.sh --verify` and require `LAB_CLEAN_VERIFY_PASS`.
4. Sync current source to the relevant control host.
5. Launch through `run-durable-gate.sh` and the top-level supervisor.
6. Stream every remote log line to the foreground console.
7. Emit a heartbeat every 30 seconds. After 60 seconds with no new log bytes, print durable state and a process-tree diagnostic.
8. On success, failure, lost SSH, or interrupted monitoring, wipe and verify again before another scenario.

The remote workload must survive a local SSH/tail disconnect. The `.pid` and `.exit` state files are authoritative; a missing exit record is a failed/lost run, not a pass.

## Failure and root-cause protocol

When a gate fails:

1. Preserve the exact command, current log tail, PID/exit state and cluster events.
2. Determine whether the failure is in the gate, environment, registry/artifact path, runtime, Kubernetes, controller reconciliation, or product code.
3. Consult the official source and documentation for the pinned version. Prefer vendored official charts/CRDs/source when external access is limited.
4. Reproduce from a clean baseline and change one causal layer at a time.
5. Add a focused unit/contract test for the proven root cause.
6. Run the focused live gate, then the required full chain.
7. If the attempted fix does not pass, remove the speculative change and clean its data before continuing.

Examples captured by the current gates include registry HTTP readiness instead of container state, SHA256 validation and atomic restaging of Docker runtime artifacts, asynchronous RocketMQ Broker reconciliation, real LVM/NFS read/write, official Nacos schema import with external MySQL, MinIO Tenant health, and 9/9 executed network-check jobs.

## Test-gate change protocol

Test helpers, runners, fixtures, diagnostics, templates and matrix validators
are delivery code.  They must be validated before they are allowed to consume a
clean lab.  Preserve every acceptance scenario, but execute the following
evidence ladder in order:

1. Classify the failure and change as product, test gate, environment,
   supply-chain, runtime, Kubernetes/controller, or a proven combination.
   Name the owning matrix scenario, product entry point, terminal marker and
   smallest disproof command.
2. Pass the changed artifact's lowest-cost checks first: syntax/import checks,
   focused unit and contract tests, structured-data parsing, render checks, and
   resource contracts for names, namespaces, selectors, ports, images,
   ownership, markers and durable exit state.
3. Add a deterministic regression test for a proven test-gate defect, then use
   the narrowest fixed-runner branch that executes the affected product path.
   A static or focused failure blocks the full middleware gate.
4. Only a green focused gate unlocks one clean full middleware regression.
   The runner decides whether a prior clean boundary can be reused; stale
   processes, clusters, registries, port-forwards or test-owned resources force
   normal cleanup and `LAB_CLEAN_VERIFY_PASS` first.
5. On a live failure, capture the first failed command, durable exit state,
   rendered inputs, resource state, Pod descriptions/events and controller logs
   before changing code.  Abort after a failed prerequisite rather than running
   unrelated later stages to obtain secondary failures.

For each live launch, the log must identify the completed ladder level, the
evidence that unlocked it and the next permitted level.  Direct Helm, kubectl
or SSH remains diagnostic-only unless the test has already exercised the same
behavior through the documented `config.yml` and `kubecli` product entry point.
This protocol is an efficiency control, not a coverage waiver: matrix status,
durable `rc=0`, zero failure markers and final cleanup remain mandatory.

## China image-source contract

For a Docker Hub-origin fixture in an already-delivered path, preserve its explicit, reviewable fallback list. The existing gates try the pinned `docker.sparkcr.cn/<upstream-image>` accelerator before direct Docker Hub, retain the applicable TalkEdu and Huawei candidates, and keep the upstream reference last. A mirror-discovery page such as `https://status.anye.xyz/` is for operator review only; never consume its current recommendations dynamically in a delivery run.

An accelerator response is not evidence that the fixture is usable. The gate must complete pull, inspect the exact image, tag and push it into the local registry, then verify the expected registry tag/manifest before deploying a workload. Pin the real upstream tag throughout the source list, local-registry tag and Pod manifest; never retag a different patch release to impersonate the requested version.

New middleware branches do not persist dynamically discovered public accelerators. When an isolated middleware test requires temporary acceleration, inject the reviewed source through a non-persisted runtime parameter, verify the checksum or manifest digest, and remove the override after the run.

## Script map

Authoritative orchestration:

- `run_enterprise_regression.sh`: development-host owner of the full workflow and all supported modes; `--all-delivery` is the single final-signoff command.
- `run_unit_tests.sh`: isolated unit suite; writes logs to a temporary directory.
- `helpers/run-durable-gate.sh`: remote PID/exit wrapper.
- `helpers/lab-wipe-nodes.sh`: destructive lab cleanup plus independent verification.
- `helpers/sync-kubeauto.sh`: controlled source synchronization.
- `helpers/lab-control-ssh-bootstrap.sh`: key-only control-to-node access bootstrap; it transfers only the control public key and never persists a fallback password or copies a private key.

Current live gates:

- `helpers/regression-full.sh`: core topology, lifecycle, CNI, addon and reserved baseline.
- `helpers/regression-jumper.sh`: jumper/Ansible 2.16 path.
- `helpers/nerdctl-gate.sh`: containerd/nerdctl contract.
- `helpers/delivery-docker-gate.sh`: Docker, buildx, Compose, cri-dockerd and artifact recovery.
- `helpers/registry-reboot-gate.sh`: local Registry restart policy, real control-host reboot recovery and persisted-manifest integrity.
- `helpers/delivery-upgrade-smoke.sh`: real Kubernetes patch transition.
- `helpers/delivery-gaps-fullchain.sh` and `helpers/delivery-gap-retest.sh`: remaining storage, messaging, registry, observability and network workload gates.
- `helpers/build-tools-rocky8.sh` and `helpers/tier3-tools-gate.sh`: build every frozen tool on glibc 2.28, execute the exact outputs on real Rocky 8, and remove scoped build/runtime residue.

Recovery and historical phase scripts are retained because they encode useful focused diagnostics and safe resume points. They are not alternate top-level sign-off commands. Before reusing one, verify that its preconditions still match the matrix and run it through the same durable supervision and cleanup contract.

Retained focused/recovery assets:

- registry and image preparation: `bootstrap-brinnatt-mirrors.sh`, `registry-ready-gate.sh`, `registry_blob_integrity.py`;
- storage fixtures: `prep-node-lvm-loop.sh`, `prep-nfs-server.sh`, `mk-cluster-133.sh`;
- reserved-resource focus: `reserved-4g-gate.sh`, `rocky-reserved-gate.sh`, `run-reserved-fullchain.sh`, `verify-node-reserved.sh`;
- phase/resume diagnosis: `regression-phase3.sh`, `regression-remainder.sh`, `regression-resume.sh`, `regression-resume-g2.sh`, `regression-resume-g3.sh`, `resume-from-g8.sh`, `resume-g8b-only.sh`;
- superseded launch/smoke paths retained for logic reference: `regression.sh`, `start-full-regression.sh`, `delivery-gap-smoke.sh`.

The last group must not be used for final sign-off: it predates parts of the current durable supervisor and full cleanup contract. Reuse useful checks by migrating them into the authoritative runner/gates with unit coverage.

## Matrix state transitions

Before implementation, set affected entries to `pending`. During work, record commands and exact evidence, but do not declare pass. After the clean live run and final cleanup, update:

- the item status and evidence;
- its execution-group status;
- coverage totals;
- `meta.updated`, `meta.regression_log` and `meta.regression_result`;
- the overall PASS/PENDING/FAIL assessment.

Validate YAML parsing and count every `status: pass`, `status: pending`, and `status: fail`. Tier3 scope-defined skips remain explicit and are not silently counted as pass.

## Logs and retained knowledge

Runtime logs belong under `logs/` and are ignored by Git. They are transient diagnostic evidence and may include host-specific or obsolete data. Do not commit them.

When a log teaches a reusable lesson, convert that lesson into one or more of:

- a unit or contract test;
- a bounded helper/gate;
- an enterprise-matrix evidence entry;
- this handbook or another maintained document.

Then remove the raw log when it is no longer needed for the active investigation.

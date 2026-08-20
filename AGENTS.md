# Kubeauto delivery contract

These instructions apply to the entire repository.

## Start here

Before changing code or running a lab test, read:

1. `tests/README.md`
2. `tests/enterprise-test-matrix.yaml`
3. The relevant production code and unit/contract tests
4. The matching official documentation and source for the pinned component version

Inspect all six sibling repositories under `/home/brinnatt/projects` before and after a change. Preserve unrelated user changes.

## Non-negotiable delivery rules

- Treat the six repositories as one release unit. Audit constants, image tags, Dockerfiles, GitHub Actions, TalkEdu private-registry push, Docker Hub push, and fallback order after every relevant change.
- Prefer `hub.talkedu.cn` and Huawei mirrors for the China delivery path; retain Docker Hub/upstream fallbacks. Downloads must be checksum-verified and atomically replaced so partial files cannot be accepted.
- Preserve the pinned accelerator and fallback configuration in already-delivered code unless a bug is proven with current evidence. For a newly added middleware branch, public accelerators and proxy mirrors are test-time, runtime-only aids because they may disappear or be blocked without notice; never persist a dynamically discovered accelerator in that branch's production code, CI, documentation or default configuration. Inject temporary test sources through non-persisted runtime parameters and verify checksums or manifest digests before use.
- Use the smallest readable, maintainable, Pythonic change consistent with upstream design.
- Reproduce from a verified clean lab before calling a failure a product bug. Separate test-gate, environment, supply-chain, runtime, Kubernetes, controller, and product failures with evidence.
- A failed attempted fix is not a new baseline. Remove its speculative code and lab residue before continuing.
- Change affected matrix entries to pending while work is in progress. Mark them pass only after a current clean run produces auditable evidence. Historical logs never substitute for a current run.

## Fixed lab authority

- Development only: `192.168.47.129` (`brinnatt`); never use it as a test node.
- Jumper: `192.168.47.130` (`root`, Rocky Linux 8.10).
- Workers: `192.168.47.131-133` (`root`, Rocky Linux 8.10).
- Masters: `192.168.47.134-136` (`root`, Rocky Linux 8.10).
- Debian: `192.168.47.128` (`brinnatt`, passwordless sudo).
- Reserved-resource host: `192.168.47.137` (`root`, Rocky Linux 8.10, 8 CPU / about 16 GiB as currently provisioned).
- Ubuntu all-in-one/control/large-memory host: `192.168.47.138` (`ubuntu`, passwordless sudo).
- Anolis compatibility control: `192.168.47.141` (`root`, verified Anolis OS 23.3).
- openEuler compatibility control: `192.168.47.142` (`root`, verified openEuler 22.03 LTS-SP4).
- openSUSE compatibility control: `192.168.47.143` (`root`, verified openSUSE Leap 16.0).
- Disable reserved resources on small-memory 131-136. Exercise the real 2 CPU + 4 GiB reservation behavior only on 137/138; keep the customer sizing baseline (16 CPU / 32 GiB) distinct from the smaller lab capacity.
- Use key authentication with `BatchMode=yes`. Do not copy the existing lab fallback credential into new files or print it in logs/output.
- Snapshot restores may remove SSH keys on all test hosts. Restore access through the fixed lab bootstrap helper using the runtime-only `LAB_SSH_PASSWORD`; never persist the fallback credential.

## Authoritative commands

- Unit tests: `bash tests/run_unit_tests.sh`
- Full autonomous delivery sign-off: `bash tests/run_enterprise_regression.sh --all-delivery`
- Core topology regression: `bash tests/run_enterprise_regression.sh`
- Live status: `bash tests/run_enterprise_regression.sh --status`
- Live log: `bash tests/run_enterprise_regression.sh --follow`
- Ansible OS compatibility probe/gate: `bash tests/run_enterprise_regression.sh --ansible-os-probe` / `--ansible-os-only`
- Cleanup: `bash tests/helpers/lab-wipe-nodes.sh`
- Cleanup verification: `bash tests/helpers/lab-wipe-nodes.sh --verify`

Use the fixed runner instead of ad-hoc SSH command sequences. It owns source sync, durable remote state, foreground streaming, heartbeat, diagnostics, cleanup, and evidence collection.

Every stateful scenario requires clean verification before and after execution. Success requires the expected terminal marker, durable `rc=0`, zero failure markers, updated matrix evidence, and final `LAB_CLEAN_VERIFY_PASS`.

Do not commit runtime logs, PID files, Python caches, remote cluster directories, or registry data. Preserve reusable knowledge in tests, helpers, the matrix, or documentation.

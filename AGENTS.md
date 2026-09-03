# Kubeauto delivery contract

These instructions apply to the entire repository.

## Start here

Before changing code or running a lab test, read:

1. `tests/README.md`
2. `tests/enterprise-test-matrix.yaml`
3. The relevant production code and unit/contract tests
4. The matching official documentation and source for the pinned component version

For a new middleware branch, also read and follow `docs/middleware/delivery-playbook.md`.

Inspect all six sibling repositories under `/home/brinnatt/projects` before and after a change. Preserve unrelated user changes.

## Non-negotiable delivery rules

- Treat the six repositories as one release unit. Audit constants, image tags, Dockerfiles, GitHub Actions, TalkEdu private-registry push, Docker Hub push, and fallback order after every relevant change.
- For every future feature branch that depends on external images, make `kubeauto-ext-images-dockerfile` an artifact prerequisite rather than discovering images during live testing. Inventory production, upgrade, rollback, backup, performance and test-infrastructure images; reuse an existing exact pin or add an official-source, version-pinned Dockerfile under the owning functional directory; register it in the dual-push CI matrix; pass the catalog validator; and verify the TalkEdu and Docker Hub manifests/digests after publication before starting the normal live gate. Prefer the TalkEdu copy in China. Treat a dynamic public mirror only as a temporary runtime bridge before fixed artifacts are published; never make repeated public downloads the normal test path.
- Prefer `hub.talkedu.cn` and Huawei mirrors for the China delivery path; retain Docker Hub/upstream fallbacks. Downloads must be checksum-verified and atomically replaced so partial files cannot be accepted.
- Preserve the pinned accelerator and fallback configuration in already-delivered code unless a bug is proven with current evidence. For a newly added middleware branch, public accelerators and proxy mirrors are test-time, runtime-only aids because they may disappear or be blocked without notice; never persist a dynamically discovered accelerator in that branch's production code, CI, documentation or default configuration. Inject temporary test sources through non-persisted runtime parameters and verify checksums or manifest digests before use.
- Use the smallest readable, maintainable, Pythonic change consistent with upstream design.
- Reproduce from a verified clean lab before calling a failure a product bug. Separate test-gate, environment, supply-chain, runtime, Kubernetes, controller, and product failures with evidence.
- A failed attempted fix is not a new baseline. Remove its speculative code and lab residue before continuing.
- Change affected matrix entries to pending while work is in progress. Mark them pass only after a current clean run produces auditable evidence. Historical logs never substitute for a current run.
- Customer-facing middleware documentation covers exactly four content types: user manual, operations manual, technical whitepaper and development manual. Keep the technical whitepaper and development manual separate; combine the user and operations content into one `operations-manual.md` when the component follows the established PXC style. Integrate official references into the applicable whitepaper or development manual. Do not deliver proposal/review drafts, retrospectives, project-status narratives, internal test chronology, standalone source indexes or an extra README as customer documentation. Write from the customer's product, task and operational perspective with formal terminology, continuous executable main paths and clearly quoted exception/rollback/risk branches.

## Test-engineering and execution-efficiency contract

Test helpers, runners, templates, matrix validators and diagnostics are production
delivery code. A test-gate bug that consumes a clean lab run is a delivery defect,
not an acceptable testing cost. Preserve 100% acceptance coverage; reduce waste by
ordering evidence correctly, never by skipping scenarios.

- Before any stateful or full-chain run, every changed test artifact must pass its
  cheapest relevant contract checks: shell syntax, YAML/JSON parsing, template
  rendering, resource name/namespace/ownership assertions, marker/exit-state
  assertions and focused unit tests. A static contract failure blocks live work.
- New or changed assertions must first run in the narrowest focused branch that
  proves the assertion against its owning product path. Only after that branch is
  green may the fixed runner start one clean full middleware regression. Do not
  restart a full lab merely to discover syntax, quoting, resource-name, namespace,
  port-forward, selector or marker mistakes.
- Exercise customer behavior through the product entry point (`config.yml` plus
  the documented `kubecli` workflow). Direct Helm, kubectl or SSH may provide
  diagnostics, fixtures or independent API checks, but cannot replace evidence
  that the product configuration rendered and converged correctly.
- On every failed gate, preserve its first failure, durable state and diagnostics;
  classify it as product, test-gate, environment, supply-chain, runtime or
  Kubernetes/controller failure before changing code. A test-gate fix requires a
  regression unit/contract test for the exact cause before any rerun. Change one
  causal layer at a time and remove rejected attempts and lab residue.
- Use the runner's durable status, log and focused modes to reuse a validated
  clean boundary. Do not duplicate completed setup or repeat unrelated scenarios.
  A complete clean regression remains mandatory after accepted changes, with its
  terminal marker, durable `rc=0`, zero failure markers, current matrix evidence
  and final `LAB_CLEAN_VERIFY_PASS`.
- Never optimize by downgrading coverage, accepting partial evidence, marking
  matrix items early or treating a running Pod as a business result. The only
  acceptable efficiency gain is preventing invalid test code from reaching an
  expensive live environment.

### Mandatory test-gate change ladder

This ladder is mandatory whenever a helper, runner, fixture, test template,
diagnostic, matrix validator or test assertion changes.  It applies equally to
new middleware and maintenance of delivered branches.

1. Classify the proposed change before running it: product code, test-gate code,
   environment/supply-chain, or a combination.  Record the owning scenario, the
   exact product entry point, the expected success marker, and the smallest
   command that can disprove the assumption.  Do not describe a test-gate
   failure as a product failure without that proof.
2. Complete the static preflight appropriate to every changed file before any
   remote mutation: shell syntax and strict-mode control flow; Python import and
   focused unit tests; YAML/JSON parsing; Ansible syntax; Helm/template render;
   resource name, namespace, selector, port, image, ownership, marker and exit
   contracts.  Add or update a deterministic unit/contract test for the exact
   defect before its next live execution.
3. Run the narrowest fixed-runner branch that reaches the changed assertion.
   Its setup, cleanup and diagnostics remain runner-owned.  A failed preflight
   or focused gate blocks a full middleware run; fix the single proven causal
   layer, re-run its contract, then re-run only that focused branch.
4. Start exactly one clean full middleware regression only after the affected
   focused gate is green.  Reuse a verified clean boundary only when the runner
   proves there is no stale process, cluster, registry data, port-forward or
   test-owned resource; otherwise perform the normal cleanup and verification.
5. After a full-chain failure, collect the first failing command, durable exit
   state, rendered inputs, owned-resource state, Pod descriptions/events and
   relevant controller logs before editing.  Stop the scenario at a failed
   prerequisite: never spend the remaining stages of an invalid run merely to
   collect secondary failures.

Every live-launch update must state the completed ladder level, the evidence
that unlocked it, and the next permitted level.  No test-only shortcut may
replace the product entry point, and no direct command may silently mutate a
customer path merely to make a test convenient.  The full acceptance contract
still requires current clean evidence for every affected matrix item; this
ladder removes invalid executions, not customer coverage.

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

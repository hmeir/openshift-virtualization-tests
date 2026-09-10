# Handover — OCP/EUS upgrade pipeline hang (CNV-92725)

**Audience:** an agent/engineer picking this up cold. Everything you need to reproduce, confirm, and fix.

## TL;DR

EUS/OCP upgrade Jenkins jobs hang for hours during the OCP control-plane rolling restart.
Root cause is **not** the sampler timeout being too short and **not** (per the console) the test pod being
evicted. It is: a Kubernetes API GET blocks forever because there is **no client-side read timeout**, and
`TimeoutSampler` (and ocp-resources' own inner retry) can only enforce their timeout *between* iterations —
never during a blocked call. So the timeout never fires and the job hangs until manually aborted.

## Evidence (already gathered)

- **Jenkins `test-upgrade-cnv-eus-4.16` #33** — `ABORTED`, ~8.6h. Last log 23:08:44 (polling `clusterversion`,
  state `Partial`), then ~8h of silence, then `Aborted by Miroslav Sedlak` at 07:04. **No `TimeoutExpiredError`**,
  no "Timeout reached while upgrading OCP" log, no agent/channel-disconnect. Pytest ran teardown on SIGINT at
  abort → the process was **alive and blocked**, not killed at 23:08. OCP upgrade itself completed ~00:36.
- **Jenkins #34** — a *different*, clean failure: ODF stuck at `4.14.36-rhodf`, `_get_updated_odf_csv` correctly
  timed out after 20min. (Not the hang; don't conflate.)
- **Related ticket CNV-94352** — same #33; attributes the hang to the test pod running on master nodes and being
  evicted. That is a real fragility but its causal mechanism is contradicted by the SIGINT-teardown evidence
  above. Node placement alone can't fix it: the control plane restarts regardless of where the observer runs.

## Root cause — exact code path (ocp-resources)

Package: `openshift-python-wrapper` (`ocp_resources`), in `.venv/.../ocp_resources/resource.py`.

- `Resource.instance` (resource.py:1237, namespaced variant :1715) already wraps the GET in a retry:
  `retry_cluster_exceptions(func=_instance)` — an inner `TimeoutSampler` (10s window, 1s sleep) with
  `DEFAULT_CLUSTER_RETRY_EXCEPTIONS = {MaxRetryError: [], ServerTimeoutError: [], ProtocolError: []}`.
  **The machinery to survive a dropped connection is already present.**
- BUT `_instance()` calls `self.api.get(name=self.name)` with **no `_request_timeout`**, and
  `get_client()` (resource.py:245) builds a bare `kubernetes.client.Configuration()` with **no socket/read
  timeout**. urllib3's default read timeout is `None` → a half-open socket read blocks forever → **no exception
  is ever raised** → the retry never triggers.
- Our caller: `wait_for_cluster_version_state_and_version` in
  `tests/install_upgrade_operators/product_upgrade/utils.py` — `TimeoutSampler(wait_timeout=TIMEOUT_180MIN,
  sleep=10, ...)`, no `exceptions_dict` (so it defaults to retry-everything). Its 3h budget also never fires,
  same reason.

It is **resource-agnostic**: any `.instance` / `.api.get` hangs the same way. No upgrade needed to reproduce.

## The fix

- **A — upstream in ocp-resources (best, highest leverage):** thread a default `_request_timeout=(connect, read)`
  into `.instance`/`.get` so `self.api.get(..., _request_timeout=(c, r))`. The existing
  `DEFAULT_CLUSTER_RETRY_EXCEPTIONS` then catches the raised `ReadTimeout`/`MaxRetryError` and every caller is
  protected. `openshift-python-wrapper` is RedHatQE-maintained → a PR is realistic.
- **B — local, immediate unblock (our repo):** `.instance` is a property and takes no args, so bypass it in
  `_cluster_version_state_and_version` and call
  `cluster_version.api.get(name=cluster_version.name, _request_timeout=(10, 60))` (or a thin helper). The dead
  read then raises → our sampler retries within the 3h budget.
- Recommendation: **do B now, file A upstream, drop B when A ships.**
- Note: the "scope the exceptions_dict" idea is moot at the ocp-resources layer — its default already lists the
  right transient exceptions; the missing ingredient was the *timeout that makes them fire*.

## Reproduction harness

`tasks/sampler-hang-repro/repro.py` — self-contained, one process. It:
1. Stands up a **pausable TCP relay** in front of the real apiserver (raw byte forward; mTLS/token both work
   because TLS is end-to-end). `freeze()` holds sockets **open** but stops forwarding → the exact half-open
   condition. (It must NOT close sockets on freeze — a close/RST would raise immediately and the retry would
   recover, falsely refuting the theory.)
2. Runs three tests against `Namespace/default`:
   - **A** freeze; `ns.instance` must **HANG** past the inner 10s retry (watchdog 30s) → proves the retry can't help.
   - **B** freeze; `ns.api.get(_request_timeout=(5,5))` must **RAISE in ~5s** → proves the fix.
   - **C** freeze; a `TimeoutSampler` around the timed GET; unfreeze after 15s; must **RECOVER** → proves the
     full timeout→raise→retry→succeed chain.
3. Prints `VERDICT: A=.. B=.. C=..` and `THEORY CONFIRMED` when A∧B∧C.

### How to run

```bash
# 1. connect to the cluster (uses the cluster-login skill flow)
#    target cluster for this investigation: c01-hmeir50
source .cluster-env                       # created by cluster-login
oc whoami                                  # sanity

# 2. run the harness
uv run python tasks/sampler-hang-repro/repro.py
```

### Expected output (theory holds)

```
VERDICT: A(reproduce)=True  B(fix-raises)=True  C(recovery)=True
THEORY CONFIRMED
```

If **A=False** (didn't hang): most likely the relay is closing sockets on freeze (RST/EOF) instead of holding
them open — check `_pump`/`freeze`. If **B raised but A also "raised"** rather than hung, same cause.
If discovery hangs before warm-up completes, ensure the first `namespace.instance` runs while **unfrozen**.

## Pointers

- Code under test: `tests/install_upgrade_operators/product_upgrade/utils.py`
  → `wait_for_cluster_version_state_and_version`, `verify_upgrade_ocp`.
- ocp-resources: `.venv/.../ocp_resources/resource.py` (`instance` :1237/:1715, `retry_cluster_exceptions` :1155,
  `get_client` :196); `.venv/.../ocp_resources/utils/constants.py` (`DEFAULT_CLUSTER_RETRY_EXCEPTIONS`).
- Jira: CNV-92725 (this Story, "Investigate why OCP/EUS pipelines hang"); related CNV-94352; VMEDO-761 (separate).
- Jenkins: `test-upgrade-cnv-eus-4.16` #33 (hang) and #34 (ODF timeout).

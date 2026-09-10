# CNV-94727 — Findings (diagnostics on 4.22.6, no upgrade performed)

**Card:** https://redhat.atlassian.net/browse/CNV-94727 — *Operator upgrade fails with hco-operator crashloop* (Blocker, ASSIGNED)
**Cluster inspected:** OCP `5.0.0-ec.6`, CNV `kubevirt-hyperconverged-operator.v4.22.6` (pre-upgrade baseline)

## Root cause

The HCO CRD storage version flips **`v1beta1` → `v1`** during the CNV 4.22→5.0 upgrade.
`spec.featureGates` is a **map/object** in v1beta1 but a **list of `{name,state}`** in v1
(both schema types confirmed in `01-crd.yaml`). When the 5.0 operator re-stores the CR in v1
format, the stored/defaulted featureGates **map** serializes incorrectly (empty object `{}`, or
`[{},{},{}]` with empty names); the 5.0 mutating webhook then can't parse the old object:

> `failed to store the HyperConverged CR in v1 format; ... admission webhook`
> `"mutate-hyperconverged-hco.kubevirt.io" denied the request: failed to parse the old HyperConverged`

Because operator + mutating/validating webhooks upgrade together with **`failurePolicy: Fail`**
(confirmed in `05-webhooks.txt` for both v1 and v1beta1 webhooks), the new pods crashloop **while
admission is blocked** — a deadlock. The CR cannot be corrected without deleting the webhook
configurations.

## `deployOVS` annotation — ruled out

Not the trigger. It only appears in Sarah's reproducer spec. This cluster has **no annotations at
all** (`03-cr-v1beta1-view.yaml`) yet still carries the vulnerable populated featureGates map.

## Why the HCO "looks different" from the card

`oc get hyperconverged` returns the **v1 view** — nested `deployment` / `security` /
`virtualization` / `workloadSources`, with `featureGates` absent — because v1 is `served=true`.
The card's reproducer spec is the flat **v1beta1 view**. Same object, two API projections:

| View | featureGates | file |
|---|---|---|
| stored raw (v1beta1) | populated map, 12 gates | `02-cr-raw-stored-v1beta1.json` |
| v1beta1 | populated map, 12 gates | `03-cr-v1beta1-view.yaml` |
| v1 | **absent** (list, unset) | `04-cr-v1-view.yaml` |

## Vulnerable ingredients confirmed present (pre-upgrade)

- `status.storedVersions = ["v1beta1"]` — the flip to v1 has not happened yet.
- CRD versions: `v1 served=true storage=false`, `v1beta1 served=true storage=true`.
- featureGates stored as a **populated 12-entry map** (schema default) — exactly the shape that
  PR #4493's `recoverBadFeatureGates` does **not** handle (it is guarded by `len(fgsObj)==0`).
- Conversion strategy: `Webhook`; both v1 and v1beta1 admission webhooks have `failurePolicy: Fail`.

## Non-destructive conversion probe (server-side dry-run)

Round-tripping the CR through both the v1 and v1beta1 APIs with `--dry-run=server` was **accepted**
in both cases (`08-probe-v1-dryrun.txt`, `09-probe-v1beta1-dryrun.txt`).

**Interpretation:** the stored data is *not* intrinsically invalid, and the **4.22 build's** v1
webhook + conversion handle it cleanly. The failure is specific to the **5.0 build's re-store path**
(operator startup re-storing the CR in v1 during the storage flip) plus the 5.0 mutating webhook's
stricter old-object parse. **Therefore the crash cannot be reproduced on 4.22.6 without the 5.0
bits** — a real upgrade on a throwaway cluster is required to capture the crash logs.

## Fix status

PR #4493 (`recoverBadFeatureGates`, `pkg/webhooks/mutator/hyperConvergedMutator.go`) only handles
the empty-object case under `len==0`. Per Sarah Bennert (2026-08-25) it still fails on
`v5.0.0.rhel9-59` for **populated** maps and on the **conversion read-path**. Card reopened to
ASSIGNED.

## Next step to unblock the assignee

Run `capture-during-upgrade.sh` on a throwaway 4.22.x cluster, starting it **before** the CNV
Subscription upgrade and leaving it through the crash. That yields the pre/during operator+webhook
logs and the live CR watch across the storage flip — the exact artifacts Nahshon requested.

## Artifact index

| File | Contents |
|---|---|
| `00-versions.txt` | OCP/CNV versions, hco-operator & hco-webhook images |
| `01-crd.yaml` | Full HyperConverged CRD (both schemas, conversion, storedVersions) |
| `02-cr-raw-stored-v1beta1.json` | Raw stored CR bytes (v1beta1) |
| `03-cr-v1beta1-view.yaml` | CR in v1beta1 view (populated featureGates map) |
| `04-cr-v1-view.yaml` | CR in v1 view (nested; featureGates absent) |
| `05-webhooks.txt` / `05-webhooks-full.yaml` | Webhook failurePolicy / matchPolicy / apiVersions |
| `06-hco-operator.log` / `07-hco-webhook.log` | Current (clean) 4.22 logs, baseline |
| `08-probe-v1-dryrun.txt` / `09-probe-v1beta1-dryrun.txt` | Non-destructive conversion probes (accepted) |
| `capture-during-upgrade.sh` | Staged smoking-gun capture for a real upgrade |

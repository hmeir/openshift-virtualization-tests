#!/usr/bin/env bash
# CNV-94727 — smoking-gun capture for the 4.22 -> 5.0 HCO upgrade crashloop.
#
# Run this on a THROWAWAY 4.22.x cluster BEFORE starting the CNV Subscription
# upgrade to 5.0, and leave it running THROUGH the crash. It captures exactly
# what Nahshon (assignee) asked for on the card: operator+webhook logs streamed
# across the version flip, plus a live watch of the HCO CR as it is re-stored.
#
# Usage:  ./capture-during-upgrade.sh [output_dir]
set -euo pipefail

NS="openshift-cnv"
OUT="${1:-cnv-94727-upgrade-capture}"
mkdir -p "$OUT"

echo "[*] Capturing pre-upgrade baseline into $OUT ..."
oc get csv -n "$NS" > "$OUT/pre-csv.txt" 2>&1 || true
oc get crd hyperconvergeds.hco.kubevirt.io -o yaml > "$OUT/pre-crd.yaml" 2>&1 || true
oc get --raw "/apis/hco.kubevirt.io/v1beta1/namespaces/$NS/hyperconvergeds/kubevirt-hyperconverged" \
  > "$OUT/pre-cr-raw-v1beta1.json" 2>&1 || true

echo "[*] Starting background streams (operator, webhook, CR watch)."
echo "    Start the CNV upgrade now. Ctrl-C here once hco pods crashloop."

oc logs -n "$NS" -f --prefix=true -l "name=hyperconverged-cluster-operator" \
  > "$OUT/hco-operator.log" 2>&1 &
PID_OP=$!
oc logs -n "$NS" -f --prefix=true -l "name=hyperconverged-cluster-webhook" \
  > "$OUT/hco-webhook.log" 2>&1 &
PID_WH=$!
oc get -n "$NS" hyperconvergeds.v1beta1.hco.kubevirt.io kubevirt-hyperconverged -o yaml -w \
  > "$OUT/hco-cr-watch.yaml" 2>&1 &
PID_CR=$!

cleanup() {
  echo "[*] Stopping streams; grabbing final state + storage flip evidence ..."
  kill "$PID_OP" "$PID_WH" "$PID_CR" 2>/dev/null || true
  oc get pods -n "$NS" -l "name in (hyperconverged-cluster-operator,hyperconverged-cluster-webhook)" \
    > "$OUT/post-pods.txt" 2>&1 || true
  # --previous may fail after multiple restarts; capture best-effort
  oc logs -n "$NS" --prefix=true -l "name=hyperconverged-cluster-webhook" --previous \
    > "$OUT/hco-webhook-previous.log" 2>&1 || true
  oc get crd hyperconvergeds.hco.kubevirt.io \
    -o jsonpath='{.status.storedVersions}{"\n"}{range .spec.versions[*]}{.name}{" served="}{.served}{" storage="}{.storage}{"\n"}{end}' \
    > "$OUT/post-crd-versions.txt" 2>&1 || true
  echo "[*] Done. Artifacts in $OUT/"
}
trap cleanup INT TERM

wait

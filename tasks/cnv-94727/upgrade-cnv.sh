#!/bin/bash

# Static Configuration
CNV_NAMESPACE="openshift-cnv"
MARKETPLACE_NAMESPACE="openshift-marketplace"

# Default Values
TARGET_CHANNEL="stable"
TARGET_IMAGE=""
TARGET_VERSION=""

# Usage Instructions
usage() {
  echo "Usage: $0 -i <target_image> -v <target_version> [-c <target_channel>]"
  echo "Example: $0 -i brew.registry.redhat.io/rh-osbs/iib:1116287 -v 4.20.8"
  echo ""
  echo "Options:"
  echo "  -i  Target index image (Required)"
  echo "  -v  Target version (Required, e.g., 4.20.8)"
  echo "  -c  Target channel (Optional, default: stable)"
  exit 1
}

# Parse Command Line Arguments
while getopts "i:v:c:h" opt; do
  case ${opt} in
    i ) TARGET_IMAGE=$OPTARG ;;
    v ) TARGET_VERSION=$OPTARG ;;
    c ) TARGET_CHANNEL=$OPTARG ;;
    h ) usage ;;
    * ) usage ;;
  esac
done

# Validate Required Arguments
if [ -z "${TARGET_IMAGE}" ] || [ -z "${TARGET_VERSION}" ]; then
  echo "Error: Target image (-i) and target version (-v) are required."
  usage
fi

# Dynamically set the CSV based on the provided version
TARGET_CSV="kubevirt-hyperconverged-operator.v${TARGET_VERSION}"

echo "Starting CNV upgrade to ${TARGET_VERSION} (CSV: ${TARGET_CSV}) on channel '${TARGET_CHANNEL}'..."

# 1. Patch CatalogSource
echo "--> Patching CatalogSource..."
oc patch catsrc hco-catalogsource -n "${MARKETPLACE_NAMESPACE}" --type='merge' \
  -p "{\"spec\":{\"image\":\"${TARGET_IMAGE}\"}}"

# 2. Patch Subscription
echo "--> Patching Subscription..."
oc patch sub hco-operatorhub -n "${CNV_NAMESPACE}" --type='merge' \
  -p "{\"spec\":{\"channel\":\"${TARGET_CHANNEL}\"}}"

# 3. Discovery Loop
echo "--> Waiting for InstallPlan for ${TARGET_CSV} to be created..."
INSTALL_PLAN=""
while [ -z "$INSTALL_PLAN" ]; do
  INSTALL_PLAN=$(oc get installplan -n "${CNV_NAMESPACE}" -o jsonpath="{.items[?(@.spec.clusterServiceVersionNames[0]=='${TARGET_CSV}')].metadata.name}")
  [ -z "$INSTALL_PLAN" ] && sleep 5
done
echo "--> Found InstallPlan: ${INSTALL_PLAN}"

# 4. Approve InstallPlan
echo "--> Approving InstallPlan..."
oc patch installplan "${INSTALL_PLAN}" -n "${CNV_NAMESPACE}" --type='merge' \
  -p '{"spec":{"approved":true}}'

# 5. Wait for Upgrade
echo "--> Waiting for HCO to reach version ${TARGET_VERSION}..."
# This command waits until the JSON path equals the target version
oc wait hco kubevirt-hyperconverged -n "${CNV_NAMESPACE}" \
  --for=jsonpath='{.status.versions[?(@.name=="operator")].version}'="${TARGET_VERSION}" \
  --timeout=15m

echo ""
echo "SUCCESS: CNV upgrade complete."

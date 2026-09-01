import json
import logging
import re
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError
from ocp_resources.cdi import CDI
from ocp_resources.custom_resource_definition import CustomResourceDefinition
from ocp_resources.data_source import DataSource
from ocp_resources.hyperconverged import HyperConverged
from ocp_resources.kubevirt import KubeVirt
from ocp_resources.namespace import Namespace
from ocp_resources.network_addons_config import NetworkAddonsConfig
from ocp_resources.resource import Resource, ResourceEditor
from ocp_resources.ssp import SSP
from pytest_testconfig import py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

import utilities.infra
from utilities.constants.hco import (
    APPLICATION_AWARE_CONFIG_ENABLE_KEY,
    APPLICATION_AWARE_CONFIG_KEY,
    DEFAULT_HCO_CONDITIONS,
    DEPLOYMENT_KEY,
    ENABLE_COMMON_BOOT_IMAGE_IMPORT,
    EXPECTED_STATUS_CONDITIONS,
    FEATURE_GATE_DISABLED_STATE,
    FEATURE_GATE_ENABLED_STATE,
    FEATURE_GATE_PHASE_ALPHA,
    FEATURE_GATE_PHASE_BETA,
    FEATURE_GATES,
    HCO_SUBSCRIPTION,
    HYPERCONVERGED_CRD_NAME,
    IMAGE_CRON_STR,
    INFRA_KEY,
    NODE_PLACEMENTS_KEY,
    SSP_CR_COMMON_TEMPLATES_LIST_KEY_NAME,
    WORKLOAD_KEY,
    WORKLOAD_SOURCES_KEY,
)
from utilities.constants.storage import StorageClassNames
from utilities.constants.timeouts import (
    TIMEOUT_2MIN,
    TIMEOUT_4MIN,
    TIMEOUT_5MIN,
    TIMEOUT_5SEC,
    TIMEOUT_10MIN,
    TIMEOUT_30MIN,
)
from utilities.ssp import (
    wait_for_at_least_one_auto_update_data_import_cron,
    wait_for_deleted_data_import_crons,
    wait_for_ssp_conditions,
)
from utilities.storage import verify_boot_sources_reimported

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from ocp_resources.data_import_cron import DataImportCron

LOGGER = logging.getLogger(__name__)

# The featureGates CRD description opens with a phase-legend preamble ("* alpha: ...", "* beta: ...")
# before this header, then lists the gates. Everything after the header is the gate list.
_FG_LIST_HEADER = "Feature-Gate list:"
# Matches "* gateName: <text> Phase: <Phase>" bullet entries in the gate list.
_FG_PHASE_RE = re.compile(r"\*\s+(\w+):\s.*?Phase:\s+(\w+)", re.DOTALL)

DEFAULT_HCO_PROGRESSING_CONDITIONS = {
    Resource.Condition.PROGRESSING: Resource.Condition.Status.TRUE,
}
HCO_JSONPATCH_ANNOTATION_COMPONENT_DICT = {
    "kubevirt": {
        "api_group_prefix": "kubevirt",
        "config": "configuration/",
    },
    "cdi": {
        "api_group_prefix": "containerizeddataimporter",
        "config": "config/",
    },
    "cnao": {
        "api_group_prefix": "networkaddonsconfigs",
    },
    "ssp": {
        "api_group_prefix": "ssp",
    },
}


class ResourceEditorValidateHCOReconcile(ResourceEditor):
    def __init__(
        self,
        admin_client,
        hco_namespace="openshift-cnv",
        consecutive_checks_count=3,
        list_resource_reconcile=None,
        wait_for_reconcile_post_update=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.admin_client = admin_client
        self.hco_namespace = Namespace(client=self.admin_client, name=hco_namespace)
        self.wait_for_reconcile_post_update = wait_for_reconcile_post_update
        self._consecutive_checks_count = consecutive_checks_count
        self.list_resource_reconcile = list_resource_reconcile or []
        LOGGER.info(f"Patches: {self.patches}")

    def update(self, backup_resources=False):
        super().update(backup_resources=backup_resources)
        if self.wait_for_reconcile_post_update:
            wait_for_hco_conditions(
                admin_client=self.admin_client,
                hco_namespace=self.hco_namespace,
                consecutive_checks_count=self._consecutive_checks_count,
                list_dependent_crs_to_check=self.list_resource_reconcile,
            )

    def restore(self):
        super().restore()
        wait_for_hco_conditions(
            admin_client=self.admin_client,
            hco_namespace=self.hco_namespace,
            consecutive_checks_count=self._consecutive_checks_count,
            list_dependent_crs_to_check=self.list_resource_reconcile,
        )


def feature_gates_patch(**gates: bool) -> dict[str, Any]:
    """Build a merge-patch dict for ``spec.featureGates`` from ``name=enabled`` kwargs.

    A merge patch replaces the whole list, so pass every gate that must be present in one call. An
    enabled gate omits ``state`` (Enabled is the default); a disabled gate sets it explicitly.

    For standalone gate changes prefer :func:`set_hco_feature_gates`, which reads-modifies-writes so
    it does not drop pre-existing gates. Use this builder when composing the feature-gate fragment
    into a larger spec patch.

    Args:
        **gates: Mapping of gate name to desired enabled state.

    Returns:
        A ``{"spec": {"featureGates": [...]}}`` dict for ``ResourceEditorValidateHCOReconcile``.
    """
    return {
        "spec": {
            FEATURE_GATES: [
                {"name": name} if enabled else {"name": name, "state": FEATURE_GATE_DISABLED_STATE}
                for name, enabled in gates.items()
            ]
        }
    }


@contextmanager
def set_hco_feature_gates(
    admin_client: DynamicClient,
    hco_resource: HyperConverged,
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    list_resource_reconcile: list[type[Resource]] | None = None,
) -> Iterator[None]:
    """Safely set feature gates on the HCO CR, preserving existing entries, reverting on exit.

    v1 ``spec.featureGates`` is a list and a merge patch REPLACES it, so setting one gate with a
    bare patch would wipe the others. This reads the current list, merges the requested
    enable/disable deltas into it, and writes the full merged list. On exit the resource editor
    restores the original list — which REMOVES the entries added here rather than writing their
    defaults (writing a default would leave a pinned entry behind; see
    ``docs/featuregates/HCO_V1_BEHAVIOR_EXPLAINED.md`` §5/§8).

    Args:
        admin_client: Cluster admin dynamic client.
        hco_resource: The HyperConverged resource to patch.
        enable: Gate names to set enabled.
        disable: Gate names to set disabled.
        list_resource_reconcile: Operand resource kinds to wait for after the change; defaults to
            ``[KubeVirt]`` (where feature gates propagate).

    Yields:
        None, once the merged gates are applied and HCO has reconciled.
    """
    deltas = {name: True for name in enable or []}
    deltas.update({name: False for name in disable or []})
    current = [dict(entry) for entry in hco_resource.instance.spec.get(FEATURE_GATES, [])]
    merged = [entry for entry in current if entry["name"] not in deltas]
    merged.extend(
        {"name": name} if enabled else {"name": name, "state": FEATURE_GATE_DISABLED_STATE}
        for name, enabled in deltas.items()
    )
    with ResourceEditorValidateHCOReconcile(
        admin_client=admin_client,
        patches={hco_resource: {"spec": {FEATURE_GATES: merged}}},
        list_resource_reconcile=list_resource_reconcile or [KubeVirt],
        wait_for_reconcile_post_update=True,
    ):
        yield


def is_feature_gate_enabled(hco_resource: HyperConverged, name: str, fg_phases: dict[str, str]) -> bool:
    """Return whether a feature gate is effectively enabled on the HCO CR.

    A gate present in ``spec.featureGates`` reads its own ``state``. An absent gate sits at its
    lifecycle-phase default (Beta = Enabled, Alpha = Disabled).

    Args:
        hco_resource: The HyperConverged resource to read.
        name: Feature gate name.
        fg_phases: Gate-name → lifecycle-phase map (see :func:`parse_hco_fg_phases`).

    Returns:
        ``True`` if the gate is effectively enabled.

    Raises:
        ValueError: If the gate is absent and its phase is neither Alpha nor Beta (e.g. a
            deprecated gate that may default on). The phase heuristic cannot resolve those — read
            the gate's dedicated spec field instead (see HCO_V1_BEHAVIOR_EXPLAINED.md §6).
    """
    for entry in hco_resource.instance.spec.get(FEATURE_GATES, []):
        if entry["name"] == name:
            return entry.get("state", FEATURE_GATE_ENABLED_STATE) == FEATURE_GATE_ENABLED_STATE
    phase = fg_phases[name]
    if phase not in (FEATURE_GATE_PHASE_ALPHA, FEATURE_GATE_PHASE_BETA):
        raise ValueError(
            f"Cannot infer the state of unset feature gate {name!r} from phase {phase!r}; "
            f"read its dedicated spec field instead (see HCO_V1_BEHAVIOR_EXPLAINED.md §6)."
        )
    return phase == FEATURE_GATE_PHASE_BETA


def parse_hco_fg_phases(admin_client: DynamicClient) -> dict[str, str]:
    """Discover feature-gate lifecycle phases from the v1 HCO CRD.

    Parses the free-text ``spec.featureGates`` description of the v1 CRD version into a
    ``{gate_name: phase}`` map (phases lower-cased). Avoids a hand-maintained phase table, which
    would churn every release.

    Args:
        admin_client: Cluster admin dynamic client.

    Returns:
        Mapping of feature-gate name to lifecycle phase (e.g. ``{"downwardMetrics": "alpha"}``).

    Raises:
        ValueError: If no phases are parsed (the CRD description format likely changed) — fail loud
            rather than silently returning an empty map.
    """
    crd = CustomResourceDefinition(client=admin_client, name=HYPERCONVERGED_CRD_NAME)
    versions = crd.instance.to_dict()["spec"]["versions"]
    v1_version = next(version for version in versions if version["name"] == Resource.ApiVersion.V1)
    description = v1_version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["featureGates"].get(
        "description", ""
    )
    # Drop the phase-legend preamble so its "* alpha:/beta:/GA:/deprecated:" bullets are not parsed
    # as gates. If the header is absent (format change), fall back to the whole description and let
    # the empty-result guard below fail loud.
    gate_list = description.split(_FG_LIST_HEADER, 1)[-1] if _FG_LIST_HEADER in description else ""
    phases = dict(_FG_PHASE_RE.findall(gate_list))
    if not phases:
        raise ValueError(
            "Failed to parse feature gate phases from the HCO CRD; the featureGates description format may have changed"
        )
    return {name: phase.lower() for name, phase in phases.items()}


def common_boot_image_import_enabled(hco_resource: HyperConverged) -> bool:
    """Whether ``spec.workloadSources.enableCommonBootImageImport`` is enabled on the HCO CR."""
    return hco_resource.instance.spec.get(WORKLOAD_SOURCES_KEY, {}).get(ENABLE_COMMON_BOOT_IMAGE_IMPORT)


def wait_for_hco_conditions(
    admin_client,
    hco_namespace,
    expected_conditions=None,
    wait_timeout=TIMEOUT_10MIN,
    sleep=5,
    consecutive_checks_count=3,
    condition_key1="type",
    condition_key2="status",
    list_dependent_crs_to_check=None,
):
    """Checking HCO conditions.

    If list_dependent_crs_to_check information is passed, we would wait for them to
    stabilize first, before checking hco.status.conditions. Please note, EXPECTED_STATUS_CONDITIONS defines what all
    CRs can be checked currently. Any new CRs and associated default conditions need to be added in
    EXPECTED_STATUS_CONDITIONS in order for option list_dependent_crs_to_check to work as expected.
    """
    if list_dependent_crs_to_check:
        LOGGER.info(f"Waiting for {len(list_dependent_crs_to_check)} CRs managed by HCO to reconcile: ")
        for resource in list_dependent_crs_to_check:
            utilities.infra.wait_for_consistent_resource_conditions(
                dynamic_client=admin_client,
                namespace=getattr(resource, "namespace", None),
                resource_kind=resource,
                expected_conditions=EXPECTED_STATUS_CONDITIONS[resource],
                consecutive_checks_count=consecutive_checks_count,
            )
    utilities.infra.wait_for_consistent_resource_conditions(
        dynamic_client=admin_client,
        namespace=hco_namespace.name,
        expected_conditions=expected_conditions or DEFAULT_HCO_CONDITIONS,
        resource_kind=HyperConverged,
        condition_key1=condition_key1,
        condition_key2=condition_key2,
        total_timeout=wait_timeout,
        polling_interval=sleep,
        consecutive_checks_count=consecutive_checks_count,
    )


def wait_for_ds(ds):
    LOGGER.info(f"Waiting for daemonset {ds.name} to be up to date.")
    samples = TimeoutSampler(
        wait_timeout=TIMEOUT_4MIN,
        sleep=5,
        func=lambda: ds.instance.to_dict(),
    )
    try:
        for sample in samples:
            status = sample.get("status")
            metadata = sample.get("metadata")
            if metadata.get("generation") == status.get("observedGeneration") and (
                status.get("desiredNumberScheduled")
                == status.get("currentNumberScheduled")
                == status.get("updatedNumberScheduled")
            ):
                break
    except TimeoutExpiredError:
        LOGGER.error(f"Timeout waiting for daemonset {ds.name} to be up to date.")
        raise


def wait_for_dp(dp):
    LOGGER.info(f"Waiting for deployment {dp.name} to be up to date.")
    samples = TimeoutSampler(
        wait_timeout=TIMEOUT_4MIN,
        sleep=5,
        func=lambda: dp.instance.to_dict(),
    )
    try:
        for sample in samples:
            status = sample.get("status")
            metadata = sample.get("metadata")
            if metadata.get("generation") == status.get("observedGeneration") and status.get("replicas") == status.get(
                "updatedReplicas",
            ):
                break
    except TimeoutExpiredError:
        LOGGER.error(f"Timeout waiting for deployment {dp.name} to be up to date.")
        raise


def apply_np_changes(
    admin_client,
    hco,
    hco_namespace,
    infra_placement=None,
    workloads_placement=None,
    exclude_deployments=None,
):
    node_placements = hco.instance.to_dict()["spec"].get(DEPLOYMENT_KEY, {}).get(NODE_PLACEMENTS_KEY, {})
    current_infra = node_placements.get(INFRA_KEY)
    current_workloads = node_placements.get(WORKLOAD_KEY)
    target_infra = infra_placement if infra_placement is not None else current_infra
    target_workloads = workloads_placement if workloads_placement is not None else current_workloads
    if target_workloads != current_workloads or target_infra != current_infra:
        patch = {
            "spec": {
                DEPLOYMENT_KEY: {
                    NODE_PLACEMENTS_KEY: {
                        INFRA_KEY: target_infra or None,
                        WORKLOAD_KEY: target_workloads or None,
                    }
                }
            }
        }
        LOGGER.info(f"Updating HCO with node placement. {patch}")
        editor = ResourceEditor(patches={hco: patch})
        editor.update(backup_resources=False)
        wait_for_hco_post_update_stable_state(
            admin_client=admin_client,
            hco_namespace=hco_namespace,
            exclude_deployments=exclude_deployments,
        )
    else:
        LOGGER.info("No actual changes to node placement configuration, skipping")


def wait_for_hco_post_update_stable_state(admin_client, hco_namespace, exclude_deployments=None):
    """Waits for hco to reach stable state post hco update

    Args:
        admin_client (DynamicClient): Dynamic client object
        hco_namespace (Namespace): Namespace object
        exclude_deployments (list): List of deployment names to exclude from verification

    """
    exclude_deployments = exclude_deployments or []

    LOGGER.info("Waiting for all HCO conditions to detect that it's back to a stable configuration.")
    wait_for_hco_conditions(
        admin_client=admin_client,
        hco_namespace=hco_namespace,
        consecutive_checks_count=6,
        list_dependent_crs_to_check=[CDI, NetworkAddonsConfig, KubeVirt],
    )
    # unfortunately at this time we are not really done:
    # HCO propagated the change to components operators that propagated it
    # to their operands (deployments and daemonsets)
    # so all the CNV operators reports progressing=False and even HCO reports progressing=False
    # but deployment and daemonsets controllers has still to kill and restart pods.
    # with the following lines we can wait for all the deployment and daemonsets in
    # openshift-cnv namespace to be back to uptodate status.
    # The main issue is that if we check it too fast, we can even check before
    # deployment and daemonsets controller report uptodate=false.
    # We have also to compare the observedGeneration with the generation number
    # to be sure that the relevant controller already updated the status
    for ds in utilities.infra.get_daemonsets(admin_client=admin_client, namespace=hco_namespace.name):
        # We need to skip checking "hostpath-provisioner" daemonset, since it is not managed by HCO CR
        if not ds.name.startswith(StorageClassNames.HOSTPATH):
            wait_for_ds(ds=ds)
    for deployment in utilities.infra.get_deployments(
        admin_client=admin_client,
        namespace=hco_namespace.name,
    ):
        if deployment.name not in exclude_deployments:
            wait_for_dp(dp=deployment)
        else:
            LOGGER.info(f"Skipping deployment {deployment.name} verification as it is excluded: {exclude_deployments}.")
    utilities.infra.wait_for_pods_running(
        admin_client=admin_client,
        namespace=hco_namespace,
        number_of_consecutive_checks=3,
        filter_pods_by_name=IMAGE_CRON_STR,
    )


def add_labels_to_nodes(nodes, node_labels):
    """Adds given labels to a list of nodes

    Args:
        nodes (list): list of nodes
        node_labels (dict): dictionary of labels to be applied

    Returns:
        dictionary with information on labels applied for all the nodes and associated resource editors for the same

    """
    node_resources = {}
    for index, node in enumerate(nodes, start=1):
        labels = {key: f"{value}{index}" for key, value in node_labels.items()}
        node_resource = ResourceEditor(patches={node: {"metadata": {"labels": labels}}})
        node_resource.update(backup_resources=True)
        node_resources[node_resource] = {"node": node.name, "labels": labels}
    return node_resources


def get_hco_spec(admin_client, hco_namespace):
    return utilities.infra.get_hyperconverged_resource(
        client=admin_client,
        hco_ns_name=hco_namespace.name,
    ).instance.to_dict()["spec"]


def get_installed_hco_csv(admin_client, hco_namespace):
    cnv_subscription = utilities.infra.get_subscription(
        admin_client=admin_client,
        namespace=hco_namespace.name,
        subscription_name=py_config["hco_subscription"] or HCO_SUBSCRIPTION,
    )
    return utilities.infra.get_csv_by_name(
        csv_name=cnv_subscription.instance.status.installedCSV,
        admin_client=admin_client,
        namespace=hco_namespace.name,
    )


def get_hco_version(client, hco_ns_name):
    """Get current hco version

    Args:
        client (DynamicClient): Dynamic client object
        hco_ns_name (str): hco namespace name

    Returns:
        str: hyperconverged operator version

    """
    return (
        utilities.infra
        .get_hyperconverged_resource(client=client, hco_ns_name=hco_ns_name)
        .instance.status.versions[0]
        .version
    )


def wait_for_hco_version(client, hco_ns_name, cnv_version):
    """Wait for hco version to get updated.

    Args:
        client (DynamicClient): Dynamic client object
        hco_ns_name (str): hco namespace name
        cnv_version (str): cnv version string that should match with current cnv version

    Returns:
        str: hco version string

    Raises:
        TimeoutExpiredError: if hco resource is not updated with expected version string

    """
    samples = TimeoutSampler(
        wait_timeout=TIMEOUT_30MIN,
        sleep=5,
        func=get_hco_version,
        client=client,
        hco_ns_name=hco_ns_name,
    )
    sample = None
    try:
        for sample in samples:
            if sample and sample == cnv_version:
                LOGGER.info(f"HCO version updated to {cnv_version}")
                return sample
    except TimeoutExpiredError:
        LOGGER.error(f"Expected HCO version: {cnv_version}, actual hco version: {sample}")
        raise


def disable_common_boot_image_import_hco_spec(
    admin_client: DynamicClient,
    hco_resource: HyperConverged,
    golden_images_namespace: Namespace,
    golden_images_data_import_crons: list[DataImportCron],
    exclude_data_source_names: Collection[str] | None = None,
) -> Iterator[None]:
    if common_boot_image_import_enabled(hco_resource=hco_resource):
        update_common_boot_image_import_spec(
            hco_resource=hco_resource,
            enable=False,
        )
        wait_for_deleted_data_import_crons(data_import_crons=golden_images_data_import_crons)
        yield
        # Always enable enableCommonBootImageImport spec after test execution
        enable_common_boot_image_import_spec_wait_for_data_import_cron(
            hco_resource=hco_resource,
            admin_client=admin_client,
            namespace=golden_images_namespace,
            exclude_data_source_names=exclude_data_source_names,
        )
    else:
        yield


def enable_common_boot_image_import_spec_wait_for_data_import_cron(
    hco_resource: HyperConverged,
    admin_client: DynamicClient,
    namespace: Namespace,
    exclude_data_source_names: Collection[str] | None = None,
) -> None:
    hco_namespace = Namespace(client=admin_client, name=hco_resource.namespace)
    update_common_boot_image_import_spec(
        hco_resource=hco_resource,
        enable=True,
    )
    wait_for_at_least_one_auto_update_data_import_cron(admin_client=admin_client, namespace=namespace)
    wait_for_ssp_conditions(admin_client=admin_client, hco_namespace=hco_namespace)
    wait_for_hco_conditions(admin_client=admin_client, hco_namespace=hco_namespace)
    assert verify_boot_sources_reimported(
        admin_client=admin_client,
        namespace=namespace.name,
        consecutive_checks_count=1,
        exclude_data_source_names=exclude_data_source_names,
    )


def update_common_boot_image_import_spec(hco_resource, enable):
    def _wait_for_spec_update(_hco_resource, _enable):
        LOGGER.info(f"Wait for HCO {ENABLE_COMMON_BOOT_IMAGE_IMPORT} spec to be set to {_enable}.")
        try:
            for sample in TimeoutSampler(
                wait_timeout=TIMEOUT_2MIN,
                sleep=5,
                func=lambda: common_boot_image_import_enabled(hco_resource=_hco_resource) == _enable,
            ):
                if sample:
                    return
        except TimeoutExpiredError:
            LOGGER.error(f"{ENABLE_COMMON_BOOT_IMAGE_IMPORT} was not updated to {_enable}")
            raise

    editor = ResourceEditor(
        patches={hco_resource: {"spec": {WORKLOAD_SOURCES_KEY: {ENABLE_COMMON_BOOT_IMAGE_IMPORT: enable}}}},
    )
    editor.update(backup_resources=True)
    _wait_for_spec_update(_hco_resource=hco_resource, _enable=enable)


def get_hco_namespace(admin_client, namespace="openshift-cnv"):
    hco_ns = Namespace(client=admin_client, name=namespace)
    if hco_ns.exists:
        return hco_ns
    raise ResourceNotFoundError(f"Namespace: {namespace} not found.")


def get_json_patch_annotation_values(component, path, value=None, op="add"):
    component_dict = HCO_JSONPATCH_ANNOTATION_COMPONENT_DICT[component]
    return {
        f"{component_dict['api_group_prefix']}.{Resource.ApiGroup.KUBEVIRT_IO}/jsonpatch": json.dumps([
            {
                "op": op,
                "path": f"/spec/{component_dict.get('config', '')}{path}",
                "value": value,
            },
        ]),
    }


def hco_cr_jsonpatch_annotations_dict(component, path, value=None, op="add"):
    # https://github.com/kubevirt/hyperconverged-cluster-operator/blob/main/docs/cluster-configuration.md#jsonpatch-annotations
    return {
        "metadata": {
            "annotations": get_json_patch_annotation_values(component=component, path=path, value=value, op=op),
        },
    }


@contextmanager
def update_hco_annotations(
    admin_client,
    resource,
    path,
    value=None,
    overwrite_patches=False,
    component="kubevirt",
    op="add",
    resource_list=None,
):
    """Update jsonpatch annotation in HCO CR.

    Args:
        admin_client (DynamicClient): Kubernetes admin client
        resource (HyperConverged): HCO resource object
        path (str): key path in KubeVirt CR
        value (any): key value
        overwrite_patches (bool): if True - overwrites existing jsonpatch annotation/s
        component (str): component getting json patched
        op (str): operation string
        resource_list(list): list of resources that we should wait for reconciliation after restore

    """
    if not resource_list:
        resource_list = [KubeVirt]
    jsonpatch_key = (
        f"{HCO_JSONPATCH_ANNOTATION_COMPONENT_DICT[component]['api_group_prefix']}."
        f"{Resource.ApiGroup.KUBEVIRT_IO}/jsonpatch"
    )
    resource_existing_jsonpatch_annotation = resource.instance.metadata.get("annotations", {}).get(jsonpatch_key)
    hco_config_jsonpath_dict = hco_cr_jsonpatch_annotations_dict(
        component=component,
        path=path,
        value=value,
        op=op,
    )
    # Avoid overwriting existing jsonpatch annotations
    # example:
    # '[{"op": "add", "path": "/spec/configuration/machineType", "value": "pc-q35-rhel8.4.0"},
    # {"op": "add", "path": "/spec/configuration/cpuModel", "value": "Haswell-noTSX"}]]'
    if resource_existing_jsonpatch_annotation and not overwrite_patches:
        try:
            existing_patches = json.loads(resource_existing_jsonpatch_annotation)
        except json.JSONDecodeError, TypeError:
            LOGGER.warning(
                f"Existing jsonpatch annotation for key {jsonpatch_key!r} is not valid JSON "
                f"({resource_existing_jsonpatch_annotation!r}); ignoring and overwriting.",
            )
            existing_patches = None
        if isinstance(existing_patches, list) and existing_patches:
            hco_annotations_dict = hco_config_jsonpath_dict["metadata"]["annotations"]
            hco_annotations_dict[jsonpatch_key] = json.dumps(
                existing_patches + json.loads(hco_annotations_dict[jsonpatch_key])
            )
        elif existing_patches is not None and not isinstance(existing_patches, list):
            LOGGER.warning(
                f"Existing jsonpatch annotation for key {jsonpatch_key!r} is not a list "
                f"(got {type(existing_patches).__name__!r}); ignoring and overwriting.",
            )

    with ResourceEditorValidateHCOReconcile(
        admin_client=admin_client,
        patches={resource: hco_config_jsonpath_dict},
        list_resource_reconcile=resource_list,
        wait_for_reconcile_post_update=True,
    ):
        yield


def is_hco_tainted(admin_client, hco_namespace):
    hco = utilities.infra.get_hyperconverged_resource(
        client=admin_client,
        hco_ns_name=hco_namespace,
    )
    return [condition for condition in hco.instance.status.conditions if condition["type"] == "TaintedConfiguration"]


def wait_for_auto_boot_config_stabilization(admin_client, hco_namespace):
    wait_for_ssp_conditions(admin_client=admin_client, hco_namespace=hco_namespace)
    wait_for_hco_conditions(admin_client=admin_client, hco_namespace=hco_namespace)


def update_hco_templates_spec(
    admin_client,
    hco_namespace,
    hyperconverged_resource,
    updated_template,
    custom_datasource_name=None,
    golden_images_namespace=None,
):
    with ResourceEditorValidateHCOReconcile(
        admin_client=admin_client,
        patches={
            hyperconverged_resource: {
                "spec": {WORKLOAD_SOURCES_KEY: {SSP_CR_COMMON_TEMPLATES_LIST_KEY_NAME: [updated_template]}}
            }
        },
        list_resource_reconcile=[SSP, CDI],
        wait_for_reconcile_post_update=True,
    ):
        wait_for_auto_boot_config_stabilization(admin_client=admin_client, hco_namespace=hco_namespace)
        yield updated_template
    # delete the datasource associated with custom template that was created earlier, as it won't be cleaned up
    # otherwise
    if custom_datasource_name:
        DataSource(
            client=admin_client,
            name=custom_datasource_name,
            namespace=golden_images_namespace.name,
        ).clean_up()


@contextmanager
def enabled_aaq_in_hco(client, hco_namespace, hyperconverged_resource, enable_acrq_support=False):
    application_aware_config = {APPLICATION_AWARE_CONFIG_ENABLE_KEY: True}
    if enable_acrq_support:
        application_aware_config["allowApplicationAwareClusterResourceQuota"] = True
    patches = {
        hyperconverged_resource: {"spec": {DEPLOYMENT_KEY: {APPLICATION_AWARE_CONFIG_KEY: application_aware_config}}}
    }

    with ResourceEditorValidateHCOReconcile(
        patches=patches,
        list_resource_reconcile=[KubeVirt],
        wait_for_reconcile_post_update=True,
        admin_client=client,
    ):
        yield
    # need to wait when all AAQ system pods removed
    samples = TimeoutSampler(
        wait_timeout=TIMEOUT_5MIN,
        sleep=TIMEOUT_5SEC,
        func=utilities.infra.get_pod_by_name_prefix,
        client=client,
        pod_prefix="aaq-(controller|server)",
        namespace=hco_namespace.name,
        get_all=True,
    )
    sample = None
    try:
        for sample in samples:
            if not sample:
                break
    except TimeoutExpiredError:
        LOGGER.error(f"Some AAQ pods still present: {sample}")
        raise
    except NotFoundError, ResourceNotFoundError:
        LOGGER.info("AAQ system PODs removed.")

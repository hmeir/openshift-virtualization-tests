"""HyperConverged (HCO) CR — v1 API knowledge.

Holds the v1-shaped ``HyperConverged`` subclass used across the automation, plus the v1
feature-gate behavior (list ``{name, state}`` encoding and lifecycle-phase defaults).

Spec reads and writes use the repo's plain idiom directly at the call sites — inline
``{"spec": {...}}`` patch dicts for ``ResourceEditor`` writes and ``instance.spec`` /
``instance.to_dict()["spec"]`` access for reads. The v1 group/field key names are exported here
as constants so call sites share one source of truth for the (restructured) v1 field names.

The v1 spec field paths (v1beta1 → v1 restructuring) are documented in
``docs/featuregates/hco_v1_api_field_mapping.md``.

A sibling ``HyperConvergedV1Beta1`` subclass and a shared base are intentionally NOT provided
yet — they are added only when dedicated v1beta1 tests need them (v1beta1 is served-only on
CNV 5.0 and dropped ~5.2).
"""

import re
from typing import TYPE_CHECKING, Any

from ocp_resources.custom_resource_definition import CustomResourceDefinition
from ocp_resources.hyperconverged import HyperConverged
from ocp_resources.resource import Resource

from utilities.constants.hco import FEATURE_GATES

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

# v1 spec group keys (the new v1 grouping — see hco_v1_api_field_mapping.md).
VIRTUALIZATION_KEY = "virtualization"
STORAGE_KEY = "storage"
SECURITY_KEY = "security"
DEPLOYMENT_KEY = "deployment"
WORKLOAD_SOURCES_KEY = "workloadSources"
NODE_PLACEMENTS_KEY = "nodePlacements"
INFRA_KEY = "infra"
WORKLOAD_KEY = "workload"  # singular in v1 (v1beta1 used the plural "workloads")
APPLICATION_AWARE_CONFIG_KEY = "applicationAwareConfig"
APPLICATION_AWARE_CONFIG_ENABLE_KEY = "enable"

# Virtualization-group field keys that are RESTRUCTURED in v1 (renamed or re-parented within the
# group). Leaf keys whose names are unchanged (e.g. liveMigrationConfig, permittedHostDevices) keep
# their existing constants/literals at the call sites; only their "virtualization" parent is added.
VIRTUAL_MACHINE_OPTIONS_KEY = "virtualMachineOptions"  # now holds defaultCPUModel (was a top-level field)
OBSOLETE_CPU_MODELS_KEY = "obsoleteCPUModels"  # renamed from v1beta1 "obsoleteCPUs"; value is now a []string

# Storage-group field keys restructured in v1.
# renamed from v1beta1 spec.resourceRequirements.storageWorkloads (the resourceRequirements wrapper is dropped).
WORKLOAD_RESOURCE_REQUIREMENTS_KEY = "workloadResourceRequirements"

# Feature-gate serialization (v1 list format) and lifecycle phase names.
FEATURE_GATE_ENABLED_STATE = "Enabled"
FEATURE_GATE_DISABLED_STATE = "Disabled"
FEATURE_GATE_PHASE_BETA = "beta"

HYPERCONVERGED_CRD_NAME = f"hyperconvergeds.{Resource.ApiGroup.HCO_KUBEVIRT_IO}"

# The featureGates CRD description opens with a phase-legend preamble ("* alpha: ...", "* beta: ...")
# before this header, then lists the gates. Everything after the header is the gate list.
_FG_LIST_HEADER = "Feature-Gate list:"
# Matches "* gateName: <text> Phase: <Phase>" bullet entries in the gate list.
_FG_PHASE_RE = re.compile(r"\*\s+(\w+):\s.*?Phase:\s+(\w+)", re.DOTALL)


class HyperConvergedV1(HyperConverged):
    """HCO v1 CR wrapper.

    ``api_version`` is left unset so the base class auto-negotiates the highest served version
    (``v1`` on CNV 5.0). ``kind`` stays ``HyperConverged`` for any subclass depth, so the
    correct CRD is targeted.
    """

    @staticmethod
    def feature_gates_patch(**gates: bool) -> dict[str, Any]:
        """Build a merge-patch dict for ``spec.featureGates`` from ``name=enabled`` kwargs.

        A merge patch replaces the whole list, so pass every gate that must be present in one
        call. An enabled gate omits ``state`` (Enabled is the default); a disabled gate sets it
        explicitly.

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

    def is_feature_gate_enabled(self, name: str, fg_phases: dict[str, str]) -> bool:
        """Return whether a feature gate is effectively enabled.

        A gate present in ``spec.featureGates`` reads its own ``state``. A gate that is absent
        sits at its lifecycle-phase default (Beta = Enabled, everything else = Disabled).

        Note:
            The unset-gate branch is a phase heuristic. It is correct for unset Alpha/Beta
            gates but WRONG for a deprecated gate that defaults on (e.g. ``disableMDevConfiguration``).
            Where the authoritative value of an unset deprecated gate matters, read the resolved
            v1beta1 feature-gate map instead (added with v1beta1 support).

        Args:
            name: Feature gate name.
            fg_phases: Gate-name → lifecycle-phase map (see :func:`parse_hco_fg_phases`).

        Returns:
            ``True`` if the gate is effectively enabled.
        """
        for entry in self.instance.to_dict()["spec"].get(FEATURE_GATES, []):
            if entry["name"] == name:
                return entry.get("state", FEATURE_GATE_ENABLED_STATE) == FEATURE_GATE_ENABLED_STATE
        return fg_phases[name] == FEATURE_GATE_PHASE_BETA


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
        ValueError: If no phases are parsed (the CRD description format likely changed) — fail
            loud rather than silently returning an empty map.
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

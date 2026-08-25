"""HyperConverged (HCO) CR — v1 API knowledge.

Holds the v1-shaped ``HyperConverged`` subclass used across the automation. All v1 spec
structure lives here so call sites stay version-agnostic:

- **Reads** go through :meth:`HyperConvergedV1.read_spec` (public escape hatch for a dynamic
  path) or, in later group PRs, per-field ``@property`` reads that delegate to it.
- **Writes** are patch-dict builders (:meth:`HyperConvergedV1.spec_patch`,
  :meth:`HyperConvergedV1.feature_gates_patch`) fed to the existing
  ``ResourceEditorValidateHCOReconcile`` — they are never applied here.

The v1 spec field paths (v1beta1 → v1 restructuring) are documented in
``docs/featuregates/hco_v1_api_field_mapping.md``. Only the field paths currently exercised by
the automation are declared in :class:`HyperConvergedV1.SpecPath`; more are added as each group
of call sites is migrated.

A sibling ``HyperConvergedV1Beta1`` subclass and a shared base are intentionally NOT provided
yet — they are added only when dedicated v1beta1 tests need them (v1beta1 is served-only on
CNV 5.0 and dropped ~5.2).
"""

import re
from typing import TYPE_CHECKING, Any

from ocp_resources.custom_resource_definition import CustomResourceDefinition
from ocp_resources.hyperconverged import HyperConverged
from ocp_resources.resource import Resource

from utilities.constants.hco import (
    ENABLE_COMMON_BOOT_IMAGE_IMPORT,
    FEATURE_GATES,
    SSP_CR_COMMON_TEMPLATES_LIST_KEY_NAME,
)

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

# v1 spec group keys (the new v1 grouping — see hco_v1_api_field_mapping.md).
DEPLOYMENT_KEY = "deployment"
WORKLOAD_SOURCES_KEY = "workloadSources"
NODE_PLACEMENTS_KEY = "nodePlacements"
INFRA_KEY = "infra"
WORKLOAD_KEY = "workload"  # singular in v1 (v1beta1 used the plural "workloads")
APPLICATION_AWARE_CONFIG_KEY = "applicationAwareConfig"
APPLICATION_AWARE_CONFIG_ENABLE_KEY = "enable"

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

    class SpecPath:
        """v1 ``spec.*`` key paths, as tuples composed from the module-level key constants.

        Only paths exercised by migrated call sites are declared; add entries as needed.
        """

        NODE_PLACEMENTS = (DEPLOYMENT_KEY, NODE_PLACEMENTS_KEY)
        NODE_PLACEMENT_INFRA = (DEPLOYMENT_KEY, NODE_PLACEMENTS_KEY, INFRA_KEY)
        NODE_PLACEMENT_WORKLOAD = (DEPLOYMENT_KEY, NODE_PLACEMENTS_KEY, WORKLOAD_KEY)
        ENABLE_COMMON_BOOT_IMAGE_IMPORT = (WORKLOAD_SOURCES_KEY, ENABLE_COMMON_BOOT_IMAGE_IMPORT)
        DATA_IMPORT_CRON_TEMPLATES = (WORKLOAD_SOURCES_KEY, SSP_CR_COMMON_TEMPLATES_LIST_KEY_NAME)
        APPLICATION_AWARE_CONFIG = (DEPLOYMENT_KEY, APPLICATION_AWARE_CONFIG_KEY)

    def read_spec(self, path: tuple[str, ...], default: Any = None) -> Any:
        """Read a nested value from the live ``spec`` by key path.

        Reads via ``self.instance`` so the value reflects current cluster state.

        Args:
            path: Sequence of nested keys under ``spec`` (e.g. ``SpecPath.NODE_PLACEMENT_INFRA``).
            default: Returned if any segment along the path is missing.

        Returns:
            The value at ``spec.<path>``, or ``default`` if the path does not exist.
        """
        node: Any = self.instance.to_dict()["spec"]
        for key in path:
            if not isinstance(node, dict):
                return default
            node = node.get(key, default)
        return node

    @staticmethod
    def spec_patch(path: tuple[str, ...], value: Any) -> dict[str, Any]:
        """Build a merge-patch dict setting ``spec.<path>`` to ``value``.

        Args:
            path: Sequence of nested keys under ``spec``.
            value: Value to place at the leaf of the path.

        Returns:
            A ``{"spec": {..nested..}}`` dict for ``ResourceEditorValidateHCOReconcile``.
        """
        node: Any = value
        for key in reversed(path):
            node = {key: node}
        return {"spec": node}

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
        for entry in self.read_spec(path=(FEATURE_GATES,), default=[]):
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

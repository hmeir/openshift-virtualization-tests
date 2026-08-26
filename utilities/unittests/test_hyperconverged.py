"""Unit tests for the utilities.hyperconverged module (HCO v1 wrapper)."""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add utilities to Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utilities.hyperconverged import (
    FEATURE_GATE_DISABLED_STATE,
    HyperConvergedV1,
    parse_hco_fg_phases,
)


def _hco_mock(spec: dict[str, Any]) -> MagicMock:
    """A HyperConvergedV1 stand-in whose live ``instance`` returns ``spec``.

    Avoids constructing a real resource (which would require cluster API discovery).
    """
    mock_self = MagicMock(spec=HyperConvergedV1)
    mock_self.instance.to_dict.return_value = {"spec": spec}
    return mock_self


class TestFeatureGatesPatch:
    """Test cases for HyperConvergedV1.feature_gates_patch"""

    def test_enabled_gate_omits_state(self):
        """An enabled gate is serialized without a state (Enabled is the default)"""
        assert HyperConvergedV1.feature_gates_patch(downwardMetrics=True) == {
            "spec": {"featureGates": [{"name": "downwardMetrics"}]}
        }

    def test_disabled_gate_sets_state(self):
        """A disabled gate serializes an explicit Disabled state"""
        assert HyperConvergedV1.feature_gates_patch(declarativeHotplugVolumes=False) == {
            "spec": {"featureGates": [{"name": "declarativeHotplugVolumes", "state": FEATURE_GATE_DISABLED_STATE}]}
        }

    def test_multiple_gates(self):
        """Multiple gates are emitted as a single replacement list"""
        result = HyperConvergedV1.feature_gates_patch(gateOn=True, gateOff=False)
        assert result["spec"]["featureGates"] == [
            {"name": "gateOn"},
            {"name": "gateOff", "state": FEATURE_GATE_DISABLED_STATE},
        ]


class TestIsFeatureGateEnabled:
    """Test cases for HyperConvergedV1.is_feature_gate_enabled"""

    def test_present_enabled(self):
        """A present gate with no explicit state reads as enabled"""
        mock_self = _hco_mock({"featureGates": [{"name": "gateA"}]})
        assert HyperConvergedV1.is_feature_gate_enabled(mock_self, name="gateA", fg_phases={}) is True

    def test_present_disabled(self):
        """A present gate with state Disabled reads as disabled"""
        mock_self = _hco_mock({"featureGates": [{"name": "gateB", "state": FEATURE_GATE_DISABLED_STATE}]})
        assert HyperConvergedV1.is_feature_gate_enabled(mock_self, name="gateB", fg_phases={}) is False

    def test_unset_beta_gate_enabled(self):
        """An unset Beta gate defaults to enabled"""
        mock_self = _hco_mock({})
        assert HyperConvergedV1.is_feature_gate_enabled(mock_self, name="gateC", fg_phases={"gateC": "beta"}) is True

    def test_unset_alpha_gate_disabled(self):
        """An unset Alpha gate defaults to disabled"""
        mock_self = _hco_mock({})
        assert HyperConvergedV1.is_feature_gate_enabled(mock_self, name="gateD", fg_phases={"gateD": "alpha"}) is False

    def test_unset_deprecated_gate_caveat(self):
        """Documented caveat: an unset deprecated gate is reported disabled even if it defaults on"""
        mock_self = _hco_mock({})
        assert (
            HyperConvergedV1.is_feature_gate_enabled(mock_self, name="gateE", fg_phases={"gateE": "deprecated"})
            is False
        )


class TestParseHcoFgPhases:
    """Test cases for parse_hco_fg_phases"""

    @staticmethod
    def _crd_with_description(description: str) -> MagicMock:
        crd = MagicMock()
        crd.instance.to_dict.return_value = {
            "spec": {
                "versions": [
                    {"name": "v1beta1", "schema": {}},
                    {
                        "name": "v1",
                        "schema": {
                            "openAPIV3Schema": {
                                "properties": {"spec": {"properties": {"featureGates": {"description": description}}}}
                            }
                        },
                    },
                ]
            }
        }
        return crd

    # Mirrors the real CRD description: a phase-legend preamble (whose "* alpha:/beta:/..." bullets
    # must NOT be parsed as gates) followed by the "Feature-Gate list:" header and the gate bullets.
    _REALISTIC_DESCRIPTION = (
        "FeatureGates is a set of optional feature gates. A feature gate may be in the following "
        "phases: * alpha: the feature is in dev-preview. It is disabled by default, but can be "
        "enabled. * beta: the feature gate is in tech-preview. It is enabled by default. * GA: the "
        "feature is graduated. * deprecated: the feature is deprecated. Feature-Gate list: "
        "* decentralizedLiveMigration: enables cross-cluster migration. Phase: beta "
        "* downwardMetrics: exposes metrics to the guest. Phase: Alpha "
        "* disableMDevConfiguration: deprecated gate. Phase: deprecated"
    )

    @patch("utilities.hyperconverged.CustomResourceDefinition")
    def test_parses_phases(self, mock_crd_class):
        """The gate list is parsed into a lower-cased phase map; the phase-legend preamble is ignored"""
        mock_crd_class.return_value = self._crd_with_description(self._REALISTIC_DESCRIPTION)

        result = parse_hco_fg_phases(admin_client=MagicMock())

        assert result == {
            "decentralizedLiveMigration": "beta",
            "downwardMetrics": "alpha",
            "disableMDevConfiguration": "deprecated",
        }
        # Preamble legend words must not leak in as gate names.
        assert not {"alpha", "beta", "GA", "deprecated"} & result.keys()

    @patch("utilities.hyperconverged.CustomResourceDefinition")
    def test_empty_description_raises(self, mock_crd_class):
        """An unparseable (empty) description fails loud rather than returning an empty map"""
        mock_crd_class.return_value = self._crd_with_description("")

        with pytest.raises(ValueError, match="Failed to parse feature gate phases"):
            parse_hco_fg_phases(admin_client=MagicMock())

    @patch("utilities.hyperconverged.CustomResourceDefinition")
    def test_missing_header_raises(self, mock_crd_class):
        """A description without the gate-list header fails loud (format change guard)"""
        mock_crd_class.return_value = self._crd_with_description(
            "* downwardMetrics: no header present here. Phase: Alpha"
        )

        with pytest.raises(ValueError, match="Failed to parse feature gate phases"):
            parse_hco_fg_phases(admin_client=MagicMock())


class TestSubclassKind:
    """Test that the v1 subclass still targets the HyperConverged CRD"""

    def test_kind_is_hyperconverged(self):
        """kind stays 'HyperConverged' for the subclass so the correct CRD is used"""
        # Skip cluster API-version discovery; kind is derived from the class hierarchy, not the cluster.
        with patch.object(HyperConvergedV1, "_set_api_version"):
            hco = HyperConvergedV1(name="hco", namespace="openshift-cnv", client=MagicMock())
        assert hco.kind == "HyperConverged"

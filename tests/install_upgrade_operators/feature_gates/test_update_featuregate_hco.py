import pytest

from tests.install_upgrade_operators.constants import MEDIATED_DEVICES_CONFIGURATION
from utilities.constants.hco import DISABLE_MDEV_CONFIGURATION
from utilities.hco import is_feature_gate_enabled, set_hco_feature_gates

pytestmark = [pytest.mark.s390x, pytest.mark.skip_must_gather_collection]


@pytest.fixture()
def updated_fg_hco(
    request,
    admin_client,
    hyperconverged_resource_scope_function,
):
    feature_gates = request.param["feature_gates"]
    with set_hco_feature_gates(
        admin_client=admin_client,
        hco_resource=hyperconverged_resource_scope_function,
        enable=[name for name, enabled in feature_gates.items() if enabled],
        disable=[name for name, enabled in feature_gates.items() if not enabled],
    ):
        yield


@pytest.mark.parametrize(
    "updated_fg_hco",
    [
        pytest.param(
            {"feature_gates": {DISABLE_MDEV_CONFIGURATION: True}},
            marks=pytest.mark.polarion("CNV-10091"),
            id="test_enable_fg_disable_mdev_config_hco",
        ),
    ],
    indirect=["updated_fg_hco"],
)
def test_enable_fg_hco(
    updated_fg_hco,
    hyperconverged_resource_scope_function,
    hco_fg_phases,
    kubevirt_resource,
):
    assert is_feature_gate_enabled(
        hco_resource=hyperconverged_resource_scope_function,
        name=DISABLE_MDEV_CONFIGURATION,
        fg_phases=hco_fg_phases,
    ), f"HCO featureGate {DISABLE_MDEV_CONFIGURATION} is not enabled"

    kubevirt_mdev_enabled = kubevirt_resource.instance.spec["configuration"][MEDIATED_DEVICES_CONFIGURATION]["enabled"]
    assert kubevirt_mdev_enabled is False, (
        f"KubeVirt {MEDIATED_DEVICES_CONFIGURATION}.enabled: {kubevirt_mdev_enabled}, expected: False"
    )

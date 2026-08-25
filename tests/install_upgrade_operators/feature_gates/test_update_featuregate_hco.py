import pytest
from ocp_resources.kubevirt import KubeVirt

from tests.install_upgrade_operators.constants import MEDIATED_DEVICES_CONFIGURATION
from utilities.constants.hco import DISABLE_MDEV_CONFIGURATION
from utilities.hco import ResourceEditorValidateHCOReconcile

pytestmark = [pytest.mark.s390x, pytest.mark.skip_must_gather_collection]


@pytest.fixture()
def updated_fg_hco(
    request,
    admin_client,
    hyperconverged_resource_scope_function,
):
    with ResourceEditorValidateHCOReconcile(
        admin_client=admin_client,
        patches={
            hyperconverged_resource_scope_function: hyperconverged_resource_scope_function.feature_gates_patch(
                **request.param["feature_gates"]
            )
        },
        list_resource_reconcile=[KubeVirt],
        wait_for_reconcile_post_update=True,
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
    assert hyperconverged_resource_scope_function.is_feature_gate_enabled(
        name=DISABLE_MDEV_CONFIGURATION, fg_phases=hco_fg_phases
    ), f"HCO featureGate {DISABLE_MDEV_CONFIGURATION} is not enabled"

    kubevirt_mdev_enabled = kubevirt_resource.instance.spec["configuration"][MEDIATED_DEVICES_CONFIGURATION]["enabled"]
    assert kubevirt_mdev_enabled is False, (
        f"KubeVirt {MEDIATED_DEVICES_CONFIGURATION}.enabled: {kubevirt_mdev_enabled}, expected: False"
    )

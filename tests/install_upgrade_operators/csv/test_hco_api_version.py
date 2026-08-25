import pytest
from ocp_resources.resource import Resource

pytestmark = [pytest.mark.sno, pytest.mark.s390x, pytest.mark.skip_must_gather_collection]


@pytest.mark.polarion("CNV-5832")
def test_hyperconverged_cr_api_version(hyperconverged_resource_scope_function):
    """
    This test will check the Hyperconverged CR's api_version is v1 (the served+stored version on CNV 5.0)
    """
    expected_api_version = f"{Resource.ApiGroup.HCO_KUBEVIRT_IO}/{Resource.ApiVersion.V1}"
    actual_api_version = hyperconverged_resource_scope_function.instance.apiVersion
    assert actual_api_version == expected_api_version, (
        f"HCO apiVersion is {actual_api_version}, expected {expected_api_version}"
    )

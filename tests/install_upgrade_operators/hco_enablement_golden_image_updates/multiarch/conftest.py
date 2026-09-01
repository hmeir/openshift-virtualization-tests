import logging

import pytest
from ocp_resources.cdi import CDI
from ocp_resources.kubevirt import KubeVirt
from ocp_resources.network_addons_config import NetworkAddonsConfig
from ocp_resources.ssp import SSP

from tests.install_upgrade_operators.constants import ENABLE_MULTI_ARCH_BOOT_IMAGE_IMPORT
from tests.install_upgrade_operators.hco_enablement_golden_image_updates.multiarch.utils import (
    CUSTOM_MULTIARCH_DATASOURCE_NAME,
    MULTIARCH_MANAGED_CRS,
)
from utilities.constants.cluster import KUBERNETES_ARCH_LABEL
from utilities.constants.hco import DEPLOYMENT_KEY, NODE_PLACEMENTS_KEY, WORKLOAD_KEY, WORKLOAD_SOURCES_KEY
from utilities.hco import ResourceEditorValidateHCOReconcile, update_hco_templates_spec
from utilities.virt import get_hyperconverged_kubevirt

LOGGER = logging.getLogger(__name__)


@pytest.fixture(scope="class")
def disabled_multiarch_feature_gate(admin_client, hyperconverged_resource_scope_class):
    with ResourceEditorValidateHCOReconcile(
        admin_client=admin_client,
        patches={
            hyperconverged_resource_scope_class: {
                "spec": {WORKLOAD_SOURCES_KEY: {ENABLE_MULTI_ARCH_BOOT_IMAGE_IMPORT: False}}
            }
        },
        list_resource_reconcile=MULTIARCH_MANAGED_CRS,
        wait_for_reconcile_post_update=True,
    ):
        yield


@pytest.fixture(scope="class")
def enabled_multiarch_feature_gate(admin_client, hyperconverged_resource_scope_class):
    multiarch_enabled = (
        hyperconverged_resource_scope_class.instance
        .to_dict()["spec"]
        .get(WORKLOAD_SOURCES_KEY, {})
        .get(ENABLE_MULTI_ARCH_BOOT_IMAGE_IMPORT)
    )
    if multiarch_enabled:
        LOGGER.info("Multiarch boot image import is already enabled")
        yield
    else:
        with ResourceEditorValidateHCOReconcile(
            admin_client=admin_client,
            patches={
                hyperconverged_resource_scope_class: {
                    "spec": {WORKLOAD_SOURCES_KEY: {ENABLE_MULTI_ARCH_BOOT_IMAGE_IMPORT: True}}
                }
            },
            list_resource_reconcile=MULTIARCH_MANAGED_CRS,
            wait_for_reconcile_post_update=True,
        ):
            yield


@pytest.fixture(scope="class")
def kubevirt_default_architecture(admin_client, hco_namespace):
    # TODO: Migrate to HCO CR once defaultArchitecture is exposed there.
    # Currently only available on KubeVirt CR status. See:
    # https://github.com/kubevirt/hyperconverged-cluster-operator/pull/4329
    return get_hyperconverged_kubevirt(
        admin_client=admin_client,
        hco_namespace=hco_namespace,
    ).instance.status.defaultArchitecture


@pytest.fixture()
def single_arch_node_placement(admin_client, workers_architectures, hyperconverged_resource_scope_function):
    single_arch = min(workers_architectures)
    LOGGER.info(f"Restricting workloads nodePlacement to single architecture: {single_arch}")
    placement = {"nodePlacement": {"nodeSelector": {KUBERNETES_ARCH_LABEL: single_arch}}}
    with ResourceEditorValidateHCOReconcile(
        admin_client=admin_client,
        patches={
            hyperconverged_resource_scope_function: {
                "spec": {DEPLOYMENT_KEY: {NODE_PLACEMENTS_KEY: {WORKLOAD_KEY: placement}}}
            }
        },
        list_resource_reconcile=[SSP, KubeVirt, CDI, NetworkAddonsConfig],
        wait_for_reconcile_post_update=True,
    ):
        yield


@pytest.fixture()
def hco_with_custom_template(
    request,
    admin_client,
    hco_namespace,
    golden_images_namespace,
    hyperconverged_resource_scope_function,
    hyperconverged_status_templates_scope_function,
):
    yield from update_hco_templates_spec(
        admin_client=admin_client,
        hco_namespace=hco_namespace,
        hyperconverged_resource=hyperconverged_resource_scope_function,
        updated_template=request.param(common_templates=hyperconverged_status_templates_scope_function),
        custom_datasource_name=CUSTOM_MULTIARCH_DATASOURCE_NAME,
        golden_images_namespace=golden_images_namespace,
    )

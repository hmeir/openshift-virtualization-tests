from collections.abc import Callable
from functools import cache
from subprocess import TimeoutExpired

from kubernetes.dynamic import DynamicClient
from ocp_resources.node import Node
from ocp_resources.resource import get_client
from pyhelper_utils.shell import run_command
from timeout_sampler import TimeoutSampler


def filter_schedulable_nodes(
    nodes: list[Node],
    cpu_arch: str | list[str] | None,
    taint_check: Callable[[Node], bool],
) -> list[Node]:
    """Filter cluster nodes to those marked schedulable by KubeVirt.

    Args:
        nodes: All cluster nodes to filter.
        cpu_arch: Architecture filter — a single arch string, list of archs, or None for no filter.
        taint_check: Callable returning True if a node has a disqualifying taint.

    Returns:
        list[Node]: Nodes that are schedulable, untainted, kubelet-ready, and match the arch filter.
    """
    cpu_archs = [cpu_arch] if isinstance(cpu_arch, str) else cpu_arch
    return [
        node
        for node in nodes
        if node.labels.get("kubevirt.io/schedulable") == "true"
        and not node.instance.spec.unschedulable
        and not taint_check(node)
        and node.kubelet_ready
        and (not cpu_archs or node.labels.get("kubernetes.io/arch") in cpu_archs)
    ]


@cache
def cache_admin_client() -> DynamicClient:
    """Get admin_client once and reuse it

    This usage of this function is limited ONLY in places where `client` cannot be passed as an argument.
    For example: in pytest native fixtures in conftest.py.

    Returns:
        DynamicClient: admin_client

    """

    return get_client()


def get_oc_whoami_username(*, wait_timeout: int = 30, sleep: int = 3):
    """Return the current OpenShift CLI user by running ``oc whoami``.

    Each attempt runs ``oc whoami`` in a subprocess with a time limit, then retries
    on error or hang until a non-empty username is returned or ``wait_timeout`` is
    reached.

    Args:
        wait_timeout: Maximum time in seconds to keep retrying ``oc whoami``.
        sleep: Seconds to wait between attempts.

    Returns:
        The authenticated user name (stdout from ``oc whoami``, stripped of whitespace).

    Raises:
        TimeoutExpiredError: If no successful non-empty result is obtained before
            ``wait_timeout`` elapses.
    """

    def _whoami() -> str:
        did_succeed, stdout, _ = run_command(
            command=["oc", "whoami"],
            capture_output=True,
            check=False,
            timeout=sleep,
        )
        return stdout.strip() if did_succeed else ""

    for result in TimeoutSampler(
        wait_timeout=wait_timeout,
        sleep=sleep,
        func=_whoami,
        exceptions_dict={TimeoutExpired: []},
    ):
        if result:
            return result

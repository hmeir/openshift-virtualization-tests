# Generated using Claude cli

"""Unit tests for cluster module"""

from unittest.mock import MagicMock, patch

import pytest
from timeout_sampler import TimeoutExpiredError

from utilities.cluster import cache_admin_client, filter_schedulable_nodes, get_oc_whoami_username


class TestFilterSchedulableNodes:
    """Test filter_schedulable_nodes for all multiarch/cpu-arch scenarios (1-3).

    Scenarios 4-7 (homogeneous cluster + --cpu-arch) are validation-level errors
    tested in TestValidateCpuArchParams in test_pytest_utils.py.
    """

    @staticmethod
    def _make_node(arch: str, schedulable: bool = True, unschedulable: bool = False, ready: bool = True) -> MagicMock:
        node = MagicMock()
        node.labels = {
            "kubevirt.io/schedulable": "true" if schedulable else "false",
            "kubernetes.io/arch": arch,
        }
        node.instance.spec.unschedulable = unschedulable
        node.kubelet_ready = ready
        return node

    @staticmethod
    def _no_taint(node: MagicMock) -> bool:
        return False

    def test_single_arch_filter_returns_only_matching_nodes(self):
        """Scenario 1 & 2: cpu_arch string filters nodes to that architecture only."""
        amd_node = self._make_node(arch="amd64")
        arm_node = self._make_node(arch="arm64")

        amd_result = filter_schedulable_nodes(nodes=[amd_node, arm_node], cpu_arch="amd64", taint_check=self._no_taint)
        assert [node.labels["kubernetes.io/arch"] for node in amd_result] == ["amd64"], (
            "Expected only amd64 node when cpu_arch='amd64'"
        )

        arm_result = filter_schedulable_nodes(nodes=[amd_node, arm_node], cpu_arch="arm64", taint_check=self._no_taint)
        assert [node.labels["kubernetes.io/arch"] for node in arm_result] == ["arm64"], (
            "Expected only arm64 node when cpu_arch='arm64'"
        )

    def test_list_arch_filter_returns_all_matching_nodes(self):
        """Scenario 3: cpu_arch list returns nodes from all specified architectures."""
        amd_node = self._make_node(arch="amd64")
        arm_node = self._make_node(arch="arm64")

        result = filter_schedulable_nodes(
            nodes=[amd_node, arm_node], cpu_arch=["amd64", "arm64"], taint_check=self._no_taint
        )
        assert len(result) == 2, "Expected both nodes when cpu_arch=['amd64', 'arm64']"
        assert {node.labels["kubernetes.io/arch"] for node in result} == {"amd64", "arm64"}

    def test_no_arch_filter_returns_all_schedulable_nodes(self):
        """cpu_arch=None returns all schedulable nodes regardless of architecture."""
        amd_node = self._make_node(arch="amd64")
        arm_node = self._make_node(arch="arm64")

        result = filter_schedulable_nodes(nodes=[amd_node, arm_node], cpu_arch=None, taint_check=self._no_taint)
        assert len(result) == 2, "Expected all nodes when cpu_arch=None"

    def test_non_schedulable_node_excluded(self):
        """Nodes without kubevirt.io/schedulable=true are excluded."""
        node = self._make_node(arch="amd64", schedulable=False)
        result = filter_schedulable_nodes(nodes=[node], cpu_arch=None, taint_check=self._no_taint)
        assert result == [], "Non-schedulable node must be excluded"

    def test_tainted_node_excluded(self):
        """Nodes for which taint_check returns True are excluded."""
        node = self._make_node(arch="amd64")
        result = filter_schedulable_nodes(nodes=[node], cpu_arch=None, taint_check=lambda _: True)
        assert result == [], "Tainted node must be excluded"

    def test_not_ready_node_excluded(self):
        """Nodes with kubelet_ready=False are excluded."""
        node = self._make_node(arch="amd64", ready=False)
        result = filter_schedulable_nodes(nodes=[node], cpu_arch=None, taint_check=self._no_taint)
        assert result == [], "Non-ready node must be excluded"


class TestCacheAdminClient:
    """Test cases for cache_admin_client function"""

    @patch("utilities.cluster.get_client")
    def test_cache_admin_client_returns_client(self, mock_get_client):
        """Test that cache_admin_client returns a DynamicClient"""
        # Clear the cache before testing
        cache_admin_client.cache_clear()

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        result = cache_admin_client()

        assert result == mock_client
        mock_get_client.assert_called_once()

    @patch("utilities.cluster.get_client")
    def test_cache_admin_client_caches_result(self, mock_get_client):
        """Test that cache_admin_client caches the client on repeated calls"""
        # Clear the cache before testing
        cache_admin_client.cache_clear()

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Call multiple times
        result1 = cache_admin_client()
        result2 = cache_admin_client()
        result3 = cache_admin_client()

        # All results should be the same cached object
        assert result1 is result2
        assert result2 is result3
        # get_client should only be called once due to caching
        mock_get_client.assert_called_once()

    @patch("utilities.cluster.get_client")
    def test_cache_admin_client_cache_clear(self, mock_get_client):
        """Test that cache can be cleared and get_client is called again"""
        # Clear the cache before testing
        cache_admin_client.cache_clear()

        mock_client1 = MagicMock()
        mock_client2 = MagicMock()
        mock_get_client.side_effect = [mock_client1, mock_client2]

        # First call
        result1 = cache_admin_client()
        assert result1 == mock_client1

        # Clear cache
        cache_admin_client.cache_clear()

        # Second call should get new client
        result2 = cache_admin_client()
        assert result2 == mock_client2

        # get_client should be called twice
        assert mock_get_client.call_count == 2

    @patch("utilities.cluster.get_client")
    def test_cache_admin_client_cache_info(self, mock_get_client):
        """Test that cache_info tracks cache statistics"""
        # Clear the cache before testing
        cache_admin_client.cache_clear()

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Check initial cache info
        info = cache_admin_client.cache_info()
        assert info.hits == 0
        assert info.misses == 0

        # First call - cache miss
        cache_admin_client()
        info = cache_admin_client.cache_info()
        assert info.hits == 0
        assert info.misses == 1

        # Second call - cache hit
        cache_admin_client()
        info = cache_admin_client.cache_info()
        assert info.hits == 1
        assert info.misses == 1

        # Third call - another cache hit
        cache_admin_client()
        info = cache_admin_client.cache_info()
        assert info.hits == 2
        assert info.misses == 1


class TestGetOcWhoamiUsername:
    """Test cases for get_oc_whoami_username function"""

    @patch("utilities.cluster.TimeoutSampler")
    def test_returns_username_on_first_attempt(self, mock_sampler):
        """Test that username is returned immediately when oc whoami succeeds"""
        mock_sampler.return_value = ["system:admin"]

        result = get_oc_whoami_username()

        assert result == "system:admin"

    @patch("utilities.cluster.TimeoutSampler")
    def test_returns_username_after_retry(self, mock_sampler):
        """Test that username is returned after an initial empty result"""
        mock_sampler.return_value = ["", "system:admin"]

        result = get_oc_whoami_username()

        assert result == "system:admin"

    @patch("utilities.cluster.TimeoutSampler")
    def test_raises_on_timeout(self, mock_sampler):
        """Test that TimeoutExpiredError propagates when oc whoami never succeeds"""
        mock_sampler.side_effect = TimeoutExpiredError("Timeout")

        with pytest.raises(TimeoutExpiredError):
            get_oc_whoami_username()

    @patch("utilities.cluster.TimeoutSampler")
    def test_sampler_called_with_correct_args(self, mock_sampler):
        """Test that TimeoutSampler is constructed with the expected parameters"""
        mock_sampler.return_value = ["kubeadmin"]

        get_oc_whoami_username(wait_timeout=60, sleep=5)

        call_kwargs = mock_sampler.call_args[1]
        assert call_kwargs["wait_timeout"] == 60
        assert call_kwargs["sleep"] == 5

    @patch("utilities.cluster.TimeoutSampler")
    @patch("utilities.cluster.run_command")
    def test_whoami_inner_function_success(self, mock_run_command, mock_sampler):
        """Test that _whoami calls run_command and returns stripped stdout on success"""
        mock_run_command.return_value = (True, "system:admin\n", "")

        def call_func_once(*args, **kwargs):
            return [kwargs["func"]()]

        mock_sampler.side_effect = call_func_once

        result = get_oc_whoami_username()

        assert result == "system:admin"
        mock_run_command.assert_called_once_with(
            command=["oc", "whoami"],
            capture_output=True,
            check=False,
            timeout=3,
        )

    @patch("utilities.cluster.TimeoutSampler")
    @patch("utilities.cluster.run_command")
    def test_whoami_inner_function_failure_returns_empty(self, mock_run_command, mock_sampler):
        """Test that _whoami returns empty string when run_command fails, then retries"""
        mock_run_command.side_effect = [(False, "", "error"), (True, "kubeadmin\n", "")]

        call_count = 0

        def call_func_twice(*args, **kwargs):
            nonlocal call_count
            results = []
            for _ in range(2):
                results.append(kwargs["func"]())
            call_count = len(results)
            return results

        mock_sampler.side_effect = call_func_twice

        result = get_oc_whoami_username()

        assert result == "kubeadmin"
        assert mock_run_command.call_count == 2

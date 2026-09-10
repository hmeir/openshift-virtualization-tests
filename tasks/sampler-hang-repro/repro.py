#!/usr/bin/env python
"""Reproduce (and verify the fix for) the TimeoutSampler / ocp-resources upgrade hang.

THEORY (CNV-92725):
    `Resource.instance` in ocp-resources does `self.api.get(name=...)` with NO
    client-side `_request_timeout`, and `get_client()` builds a
    `kubernetes.client.Configuration()` with NO socket/read timeout. urllib3's
    default read timeout is therefore None -> a half-open connection (peer gone,
    no RST/FIN) makes the socket read block FOREVER. Neither ocp-resources'
    inner 10s `retry_cluster_exceptions` nor an outer 3h TimeoutSampler can fire,
    because both only evaluate their timeout BETWEEN iterations, never during a
    blocked call. => the EUS upgrade job hangs during the apiserver rollout.

WHAT THIS SCRIPT DOES (no cluster upgrade required, resource-agnostic):
    Stands up a pausable TCP relay in front of the real apiserver. When "frozen"
    it holds sockets open but stops forwarding bytes -> exactly the half-open
    condition. It then runs three tests against the `Namespace/default` resource:

      TEST A  reproduce hang : `ns.instance` while frozen must HANG (> inner 10s
                               retry window) -> proves the retry can't save it.
      TEST B  fix raises     : `ns.api.get(..., _request_timeout=(5, 5))` while
                               frozen must RAISE in ~5s instead of hanging.
      TEST C  end-to-end      : a TimeoutSampler wrapping the timed GET must
                               RECOVER once the relay is unfrozen mid-flight.

CRITICAL: freeze must NOT close the sockets. A close/RST would raise
ConnectionError immediately and the retry would recover -> that would falsely
REFUTE the theory. The relay only stops forwarding; it never closes on freeze.

Run:
    source .cluster-env && uv run python tasks/sampler-hang-repro/repro.py
"""

from __future__ import annotations

import os
import socket
import threading
import time
import warnings
from urllib.parse import urlparse

import urllib3
import yaml
from ocp_resources.namespace import Namespace
from ocp_resources.resource import get_client
from timeout_sampler import TimeoutSampler

warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

INNER_RETRY_WINDOW_SEC = 10  # ocp-resources retry_cluster_exceptions wait_timeout
HANG_WATCHDOG_SEC = 30  # > inner retry window, so a 30s hang proves the retry didn't help
REQUEST_TIMEOUT = (5, 5)  # (connect, read) for the fixed call
FREEZE_SETTLE_SEC = 0.5  # > pump recv timeout: let the freeze fully take effect before the measured call


class PausableProxy:
    """Raw TCP relay localhost:<port> <-> target. `freeze()` holds sockets open but stops forwarding."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.frozen = threading.Event()
        self._stop = False
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(50)
        self.listen_port = self._server.getsockname()[1]

    def start(self) -> "PausableProxy":
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                downstream, _ = self._server.accept()
            except OSError:
                break
            try:
                upstream = socket.create_connection(self.target, timeout=10)
            except OSError:
                downstream.close()
                continue
            threading.Thread(target=self._pump, args=(downstream, upstream), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, downstream), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        src.settimeout(0.1)  # short, so a freeze takes effect fast (a localhost round-trip can't slip through)
        while not self._stop:
            if self.frozen.is_set():
                time.sleep(0.05)
                continue  # hold both sockets OPEN, read nothing -> peer's read blocks (half-open)
            try:
                data = src.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break  # real EOF -> tear down (only happens when NOT frozen)
            # If a freeze began between recv and send, HOLD the bytes until unfrozen: the peer's read
            # blocks now, and the stream stays intact so TEST C can recover cleanly (lossless freeze).
            while self.frozen.is_set() and not self._stop:
                time.sleep(0.05)
            try:
                dst.sendall(data)
            except OSError:
                break
        for sock in (src, dst):
            try:
                sock.close()
            except OSError:
                pass

    def freeze(self) -> None:
        self.frozen.set()

    def unfreeze(self) -> None:
        self.frozen.clear()


def _current_server_url(kubeconfig_path: str) -> str:
    with open(kubeconfig_path) as file_handle:
        cfg = yaml.safe_load(file_handle)
    current_context = cfg.get("current-context")
    cluster_name = next(
        context["context"]["cluster"] for context in cfg["contexts"] if context["name"] == current_context
    )
    return next(cluster["cluster"]["server"] for cluster in cfg["clusters"] if cluster["name"] == cluster_name)


def _patched_kubeconfig(kubeconfig_path: str, proxy_port: int) -> str:
    """Copy the kubeconfig, redirect every cluster server through the local proxy, drop TLS verification."""
    with open(kubeconfig_path) as file_handle:
        cfg = yaml.safe_load(file_handle)
    for cluster in cfg["clusters"]:
        cluster["cluster"]["server"] = f"https://127.0.0.1:{proxy_port}"
        cluster["cluster"]["insecure-skip-tls-verify"] = True
        cluster["cluster"].pop("certificate-authority", None)
        cluster["cluster"].pop("certificate-authority-data", None)
    patched_path = "/tmp/repro-sampler-hang-kubeconfig.yaml"
    with open(patched_path, "w") as file_handle:
        yaml.safe_dump(cfg, file_handle)
    return patched_path


def _run_with_watchdog(func, watchdog_sec: int) -> tuple[dict, threading.Thread]:
    result: dict = {}

    def _runner() -> None:
        started = time.time()
        try:
            result["value"] = func()
        except BaseException as exception:  # noqa: BLE001 - scratch harness; we want the raw type
            result["error"] = f"{type(exception).__name__}: {exception}"
        finally:
            result["elapsed"] = round(time.time() - started, 2)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(watchdog_sec)
    result["hung"] = thread.is_alive()
    return result, thread


def main() -> int:
    kubeconfig_path = os.environ.get("KUBECONFIG")
    if not kubeconfig_path or not os.path.isfile(kubeconfig_path):
        print("ERROR: KUBECONFIG is not set / file missing. Run: source .cluster-env")
        return 2

    server_url = _current_server_url(kubeconfig_path)
    parsed = urlparse(server_url)
    target_host, target_port = parsed.hostname, parsed.port or 443
    print(f"Real apiserver: {server_url}  ->  proxying via 127.0.0.1")

    proxy = PausableProxy(target_host=target_host, target_port=target_port).start()
    print(f"Pausable proxy listening on 127.0.0.1:{proxy.listen_port}")

    client = get_client(config_file=_patched_kubeconfig(kubeconfig_path, proxy.listen_port), verify_ssl=False)
    namespace = Namespace(client=client, name="default")

    # Warm up: trigger discovery + establish a pooled keepalive connection BEFORE freezing.
    print(f"Warm-up: namespace/default exists = {namespace.instance.metadata.name!r}\n")

    # --- TEST A: reproduce the hang -------------------------------------------------
    print(f"[A] freeze; call .instance (has inner {INNER_RETRY_WINDOW_SEC}s retry); watchdog {HANG_WATCHDOG_SEC}s ...")
    proxy.freeze()
    time.sleep(FREEZE_SETTLE_SEC)
    result_a, _ = _run_with_watchdog(lambda: namespace.instance, HANG_WATCHDOG_SEC)
    proxy.unfreeze()
    time.sleep(2)
    a_pass = result_a.get("hung") is True
    print(f"    -> {result_a}  ::  {'PASS (hung, retry did NOT save it)' if a_pass else 'FAIL (did not hang)'}\n")

    # --- TEST B: the fix makes it raise instead of hang ----------------------------
    print(f"[B] freeze; call .api.get(_request_timeout={REQUEST_TIMEOUT}); watchdog {HANG_WATCHDOG_SEC}s ...")
    proxy.freeze()
    time.sleep(FREEZE_SETTLE_SEC)
    result_b, _ = _run_with_watchdog(
        lambda: namespace.api.get(name="default", _request_timeout=REQUEST_TIMEOUT), HANG_WATCHDOG_SEC
    )
    proxy.unfreeze()
    time.sleep(2)
    error_b = result_b.get("error", "")
    b_pass = (not result_b.get("hung")) and ("timeout" in error_b.lower() or "timed out" in error_b.lower())
    print(f"    -> {result_b}  ::  {'PASS (raised timeout instead of hanging)' if b_pass else 'FAIL (hung or non-timeout)'}\n")

    # --- TEST C: end-to-end recovery via TimeoutSampler ----------------------------
    print("[C] freeze; run TimeoutSampler(fixed GET); unfreeze after 15s; expect RECOVERY ...")
    result_c: dict = {}

    def _sampler_run() -> None:
        started = time.time()
        try:
            for sample in TimeoutSampler(
                wait_timeout=90,
                sleep=3,
                func=lambda: namespace.api.get(name="default", _request_timeout=REQUEST_TIMEOUT),
                exceptions_dict={Exception: []},
                print_log=False,
            ):
                if sample:
                    result_c["value"] = "recovered"
                    break
        except BaseException as exception:  # noqa: BLE001
            result_c["error"] = f"{type(exception).__name__}: {exception}"
        finally:
            result_c["elapsed"] = round(time.time() - started, 2)

    proxy.freeze()
    time.sleep(FREEZE_SETTLE_SEC)
    sampler_thread = threading.Thread(target=_sampler_run, daemon=True)
    sampler_thread.start()
    time.sleep(15)  # stay frozen: the fixed GET keeps timing out and the sampler keeps retrying
    proxy.unfreeze()  # now the retry can succeed
    sampler_thread.join(40)
    result_c["hung"] = sampler_thread.is_alive()
    c_pass = result_c.get("value") == "recovered"
    print(f"    -> {result_c}  ::  {'PASS (recovered after unfreeze)' if c_pass else 'FAIL'}\n")

    overall = a_pass and b_pass and c_pass
    print("=" * 72)
    print(f"VERDICT: A(reproduce)={a_pass}  B(fix-raises)={b_pass}  C(recovery)={c_pass}")
    print(f"THEORY {'CONFIRMED' if overall else 'NOT fully confirmed - inspect output above'}")
    print("=" * 72)
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit local-only pytest runner policy (load with -p thermal_offline)."""

import asyncio
import socket

import httpx
import pytest


@pytest.fixture(autouse=True)
def sandbox_event_loop(monkeypatch):
    """Restricted sendto also denies asyncio's UNIX self-pipe wakeup.

    Bound selector sleep in that environment; this is still in-memory ASGI,
    and does not enable any socket connection or change application code.
    """
    left, right = socket.socketpair()
    try:
        left.send(b"x")
    except PermissionError:
        run_forever = asyncio.BaseEventLoop.run_forever

        def polling(loop):
            active = True

            def tick():
                if active:
                    loop.call_later(0.005, tick)

            handle = loop.call_later(0.005, tick)
            try:
                return run_forever(loop)
            finally:
                active = False
                handle.cancel()

        monkeypatch.setattr(asyncio.BaseEventLoop, "run_forever", polling)
    finally:
        left.close()
        right.close()


def pytest_collection_modifyitems(items):
    for item in items:
        fixtures = set(item.fixturenames)
        if fixtures & {"agent_http", "fix1", "bridge_live"}:
            item.add_marker(
                pytest.mark.skip(reason="本轮禁止开启 agent；既有启用用例未执行")
            )
        if item.originalname in {
            "test_hard_supervisor_bounds_actual_http_transport_phases",
            "test_local_tcp_transmits_exact_received_bytes_and_scoped_surrogate",
        }:
            item.add_marker(
                pytest.mark.skip(reason="本轮只允许内存／MockTransport，禁止 TCP")
            )


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded(original):
        def call(sock, address):
            if sock.family in {socket.AF_INET, socket.AF_INET6}:
                pytest.fail("Real IP network is forbidden in this offline test run")
            return original(sock, address)

        return call

    def forbidden(*args, **kwargs):
        pytest.fail("Use MockTransport or ASGITransport; real HTTP is forbidden")

    monkeypatch.setattr(socket.socket, "connect", guarded(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(connect_ex))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)

import socket
import threading
import time
from unittest import mock

import pytest
import proxy_tool as pt
from tests.test_load_and_stability import EchoServer, eventually


@pytest.mark.parametrize('kind', ['http', 'relay', 'socks'])
def test_start_blocks_self_loop_but_not_other_local_proxy(kind):
    address = ('127.0.0.1', 17281)
    server = (pt.HttpServer(address[1], pt.UpstreamPool([address])) if kind == 'http'
              else (pt.RelayServer if kind == 'relay' else pt.SocksServer)(address[1], address))
    with pytest.raises(OSError, match='监听端口'):
        server.start()
    assert not server._on
    assert pt.validate_upstreams([('127.0.0.1', 17282)], (17281,)) is None
    assert pt.validate_upstreams([('localhost', 17281)], (17281,))


def test_resolved_alias_cannot_connect_to_own_listener():
    server = pt.HttpServer(0, pt.UpstreamPool([]))
    server.start()
    try:
        with mock.patch.object(pt, '_resolve_addresses', return_value=[('127.0.0.1', server.listen_port)]), \
                mock.patch.object(pt.socket, 'create_connection') as dial:
            with pytest.raises(OSError, match='自循环'):
                pt._dial_connection(('alias.invalid', server.listen_port), .1)
            dial.assert_not_called()
    finally:
        server.stop()


def test_dns_wait_is_bounded_and_does_not_create_per_request_threads():
    entered, release = threading.Event(), threading.Event()
    def resolve(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 1))]
    try:
        with mock.patch.object(pt.socket, 'getaddrinfo', side_effect=resolve):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                pt._dial_connection(('slow.invalid', 1), .08)
            assert time.monotonic() - started < .5
            assert entered.is_set()
    finally:
        release.set()
        assert eventually(lambda: pt._DNS_QUEUE.unfinished_tasks == 0)


def test_multiple_resolved_addresses_share_one_dial_budget():
    now = [0.0]
    def dial(*args, **kwargs):
        now[0] += kwargs['timeout']
        raise TimeoutError()
    with mock.patch.object(pt, '_resolve_addresses', return_value=[('192.0.2.1', 1), ('192.0.2.2', 1)]), \
            mock.patch.object(pt.time, 'monotonic', side_effect=lambda: now[0]), \
            mock.patch.object(pt.socket, 'create_connection', side_effect=dial) as connect:
        with pytest.raises(TimeoutError):
            pt._dial_connection(('alias.invalid', 1), 2)
        assert now[0] == 2
        assert connect.call_count == 1


def test_same_host_dns_is_shared_and_failed_dial_invalidates_cache():
    entered, release = threading.Event(), threading.Event()
    results = []
    def resolve(*args, **kwargs):
        entered.set()
        release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 1))]
    def caller():
        results.append(pt._resolve_addresses('shared.invalid', 1, time.monotonic() + 2))
    with mock.patch.object(pt.socket, 'getaddrinfo', side_effect=resolve) as dns:
        threads = [threading.Thread(target=caller) for _ in range(8)]
        for thread in threads:
            thread.start()
        assert entered.wait(1)
        release.set()
        for thread in threads:
            thread.join(2)
        assert len(results) == 8 and dns.call_count == 1
        with mock.patch.object(pt.socket, 'create_connection', side_effect=ConnectionRefusedError):
            with pytest.raises(ConnectionRefusedError):
                pt._dial_connection(('shared.invalid', 1), .1)
        assert ('shared.invalid', 1) not in pt._DNS_CACHE


def test_pool_total_budget_stops_sequential_timeouts_and_releases_leases():
    pool = pt.UpstreamPool([(f'192.0.2.{i}', 7890) for i in range(1, 5)])
    server = pt.HttpServer(0, pool)
    server._on = True
    now = [0.0]
    def dial(*args, **kwargs):
        now[0] += kwargs['timeout']
        raise TimeoutError()
    with mock.patch.object(pt.time, 'monotonic', side_effect=lambda: now[0]), \
            mock.patch.object(pt, '_dial_connection', side_effect=dial) as connect:
        server._pump(mock.Mock())
        assert now[0] == pt.TOTAL_DIAL_TIMEOUT_SECS
        assert connect.call_count == 2
    assert not pool._leases and not pool._loads
    assert not server._live


def test_socks_connect_response_can_take_longer_than_lan_dial():
    server = pt.SocksServer(0, ('127.0.0.1', 1))
    upstream, client = mock.Mock(), mock.Mock()
    now = [0.0]
    def receive(size):
        now[0] += 4
        return b'HTTP/1.1 200 OK\r\n\r\n'
    upstream.recv.side_effect = receive
    with mock.patch.object(pt.time, 'monotonic', side_effect=lambda: now[0]), \
            mock.patch.object(pt, '_dial_connection', return_value=upstream):
        assert server._dial_upstream(client, 'example.com:443') is upstream
    assert upstream.settimeout.call_args_list[0].args[0] == pt.UPSTREAM_CONNECT_TIMEOUT_SECS


def test_listener_recovers_and_keeps_existing_tunnel_alive():
    echo = EchoServer()
    server = pt.HttpServer(0, pt.UpstreamPool([echo.address]))
    server.RETRY_DELAYS = (.01, .02, .03)
    server.start()
    try:
        with socket.create_connection(('127.0.0.1', server.listen_port), timeout=2) as old_client:
            old_client.sendall(b'before')
            assert old_client.recv(6) == b'before'
            old_listener = server._srv
            old_listener.close()
            assert eventually(lambda: server._srv is not None and server._srv is not old_listener)
            assert server.listener_state == 'running'
            old_client.sendall(b'after')
            assert old_client.recv(5) == b'after'
            with socket.create_connection(('127.0.0.1', server.listen_port), timeout=2) as fresh:
                fresh.sendall(b'new')
                assert fresh.recv(3) == b'new'
    finally:
        server.stop()
        echo.close()


def test_listener_retries_are_bounded_and_stop_cancels_recovery():
    server = pt.HttpServer(0, pt.UpstreamPool([]))
    server.RETRY_DELAYS = (.01, .01, .01)
    server.start()
    try:
        with mock.patch.object(server, '_open_listener', side_effect=OSError('busy')) as retry:
            server._srv.close()
            assert eventually(lambda: server.listener_state == 'failed')
            assert retry.call_count == 3
            assert not server._on and server._srv is None
    finally:
        server.stop()
    server.RETRY_DELAYS = (.2, .2, .2)
    server.start()
    server._srv.close()
    assert eventually(lambda: server.listener_state == 'recovering')
    server.stop()
    time.sleep(.25)
    assert server.listener_state == 'stopped' and server._srv is None


def test_shared_capacity_preserves_local_slot_and_returns_all_quotas():
    release = threading.Event()
    shared = pt.RelayServer(0, ('127.0.0.1', 1))
    local = pt.HttpServer(0, pt.UpstreamPool([]))
    clients = [mock.Mock() for _ in range(3)]
    for client in clients:
        client.getpeername.return_value = ('192.0.2.10', 1234)
    for server in (shared, local):
        server._on = True
        server._srv = object()
        server._pump = lambda client: release.wait(3)
    slots = threading.BoundedSemaphore(2)
    with mock.patch.object(pt, '_FORWARD_SLOTS', slots), \
            mock.patch.object(pt, '_SHARED_SLOTS', threading.BoundedSemaphore(1)):
        try:
            pt._start_client_worker(shared, clients[0], shared._srv)
            pt._start_client_worker(shared, clients[1], shared._srv)
            pt._start_client_worker(local, clients[2], local._srv)
            clients[1].close.assert_called_once()
            clients[0].close.assert_not_called()
            clients[2].close.assert_not_called()
            assert shared.rejected_conns == 1
        finally:
            release.set()
            assert eventually(lambda: not shared._live and not local._live)
            assert slots.acquire(timeout=1) and slots.acquire(timeout=1)
            slots.release(); slots.release()
    assert not pt._SHARED_CLIENTS


@pytest.mark.parametrize('state, expected', [('failed', '入口故障'), ('recovering', '正在自动恢复')])
def test_ui_reports_listener_fault_without_changing_system_proxy(state, expected):
    app = pt.ProxyApp.__new__(pt.ProxyApp)
    app.entry_port = 7890
    app._entry = mock.Mock(listener_state=state)
    app._pool_addrs = lambda: [('192.0.2.1', 7890)]
    app._current_preset = lambda: {'type': 'single'}
    app.status_var, app.status_label, app.btn = mock.Mock(), mock.Mock(), mock.Mock()
    app._entry_status_text = mock.Mock()
    with mock.patch.object(pt, 'get_system_proxy', return_value=(True, '127.0.0.1:7890')), \
            mock.patch.object(pt, 'disable_system_proxy') as disable:
        app.refresh_status()
        assert expected in app.status_var.set.call_args.args[0]
        assert app.status_label.config.call_args.kwargs['foreground'] == '#c62828'
        disable.assert_not_called()

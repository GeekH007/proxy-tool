import os
import sys
import socket
import threading
import unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


class TestOptimization(unittest.TestCase):
    def test_recovered_upstream_returns_to_rotation(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        for _ in range(3):
            pool.report_fail(a)
        pool.report_ok(a)
        self.assertEqual(pool.order(), [a, b])

    def test_stop_during_dial_cleans_stats_and_sockets(self):
        pool = pt.UpstreamPool([('a', 1)])
        server = pt.HttpServer(0, pool)
        server._on = True
        client, upstream = mock.Mock(), mock.Mock()
        def dial(*args, **kwargs):
            server.stop()
            return upstream
        with mock.patch.object(pt, '_dial_connection', side_effect=dial):
            server._pump(client)
        self.assertEqual(server.active_conns, 0)
        self.assertEqual(server.stats[('a', 1)]['conns'], 0)
        self.assertEqual(server._live, set())
        client.close.assert_called()
        upstream.close.assert_called()

    def test_transfer_failure_unblocks_both_directions(self):
        src, dst = mock.Mock(), mock.Mock()
        src.recv_into.side_effect = ConnectionResetError()
        pt._pipe_sockets(src, dst, mock.Mock())
        src.shutdown.assert_called_with(socket.SHUT_RDWR)
        self.assertIn(mock.call(socket.SHUT_RDWR), dst.shutdown.call_args_list)

    def test_half_close_preserves_large_delayed_response(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        payload = bytes(range(256)) * 4096
        failures = []
        def serve():
            try:
                with listener.accept()[0] as upstream:
                    upstream.settimeout(5)
                    request = bytearray()
                    while True:
                        data = upstream.recv(65536)
                        if not data:
                            break
                        request.extend(data)
                    self.assertEqual(request, b'request')
                    upstream.sendall(payload)
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        server = pt.HttpServer(0, pt.UpstreamPool([listener.getsockname()]))
        server.start()
        try:
            with socket.create_connection(server._srv.getsockname(), timeout=5) as client:
                client.sendall(b'request')
                client.shutdown(socket.SHUT_WR)
                response = bytearray()
                while True:
                    data = client.recv(65536)
                    if not data:
                        break
                    response.extend(data)
                self.assertEqual(response, payload)
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
        finally:
            server.stop()
            listener.close()

class TestFailoverOptimization(unittest.TestCase):
    def test_first_failure_temporarily_prefers_other_upstream(self):
        now = [100.0]
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b], clock=lambda: now[0])
        pool.report_fail(a)
        self.assertEqual(pool.order(), [b, a])
        now[0] += pool.RETRY_SECS + 0.1
        pool.order()
        self.assertEqual(pool.order(), [a, b])

    def test_concurrent_recovery_has_only_one_probe(self):
        a = ('a', 1)
        pool = pt.UpstreamPool([a])
        pool.report_fail(a)
        barrier = threading.Barrier(16)
        results = []
        lock = threading.Lock()
        def attempt():
            barrier.wait(timeout=5)
            token = pool.begin_attempt(a)
            with lock:
                results.append(token)
        workers = [threading.Thread(target=attempt) for _ in range(16)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        tokens = [token for token in results if token is not None]
        self.assertEqual(len(results), 16)
        self.assertEqual(len(tokens), 1)
        pool.finish_attempt(a, tokens[0], True)
        self.assertIsNotNone(pool.begin_attempt(a))
        self.assertIsNotNone(pool.begin_attempt(a))

    def test_failed_probe_releases_reservation(self):
        a = ('a', 1)
        pool = pt.UpstreamPool([a])
        pool.report_fail(a)
        token = pool.begin_attempt(a)
        pool.finish_attempt(a, token, False)
        self.assertIsNotNone(pool.begin_attempt(a))

    def test_old_preset_dial_cannot_poison_new_pool(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        old = pool.begin_attempt(a)
        pool.set([a, b])
        pool.finish_attempt(a, old, False)
        self.assertEqual(pool.order(), [a, b])

    def test_duplicate_upstreams_dial_only_once(self):
        pool = pt.UpstreamPool([('a', 1), ('a', 1)])
        self.assertEqual(pool.order(), [('a', 1)])

    def test_healthy_inflight_result_does_not_release_another_probe(self):
        a = ('a', 1)
        pool = pt.UpstreamPool([a])
        old = pool.begin_attempt(a)
        pool.report_fail(a)
        probe = pool.begin_attempt(a)
        pool.finish_attempt(a, old, False)
        self.assertIsNone(pool.begin_attempt(a))
        pool.finish_attempt(a, probe, True)
        self.assertIsNotNone(pool.begin_attempt(a))

    def test_entry_skips_node_with_recovery_probe_in_progress(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        pool.report_fail(a)
        probe = pool.begin_attempt(a)
        server = pt.HttpServer(0, pool)
        server._on = True
        client, upstream = mock.Mock(), mock.Mock()
        with mock.patch.object(pt, '_dial_connection', return_value=upstream) as dial:
            with mock.patch.object(server, '_pipe'):
                server._pump(client)
        dial.assert_called_once_with(b, timeout=pt.DIAL_TIMEOUT_SECS)
        self.assertEqual(server.active_conns, 0)
        self.assertEqual(server._live, set())
        pool.finish_attempt(a, probe, True)

    def test_entry_falls_back_to_degraded_node_if_healthy_node_fails(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        pool.report_fail(a)
        server = pt.HttpServer(0, pool)
        server._on = True
        client, upstream = mock.Mock(), mock.Mock()
        with mock.patch.object(pt, '_dial_connection', side_effect=[OSError(), upstream]) as dial:
            with mock.patch.object(server, '_pipe'):
                server._pump(client)
        self.assertEqual([call.args[0] for call in dial.call_args_list], [b, a])
        self.assertEqual(server.active_conns, 0)
        self.assertEqual(server._live, set())
        self.assertEqual(pool.order()[0], a)

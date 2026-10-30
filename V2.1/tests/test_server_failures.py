"""Regression coverage for listener startup and SOCKS handshake failure paths."""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


class TestListenerStartupFailure(unittest.TestCase):
    def test_accept_thread_failure_closes_listener_and_resets_state(self):
        factories = (
            lambda: pt.HttpServer(0, pt.UpstreamPool([])),
            lambda: pt.RelayServer(0, ('127.0.0.1', 1)),
            lambda: pt.SocksServer(0, ('127.0.0.1', 1)),
        )
        for factory in factories:
            server = factory()
            with self.subTest(server=type(server).__name__):
                listener = mock.Mock()
                with mock.patch.object(pt.socket, 'socket', return_value=listener):
                    with mock.patch.object(pt.threading.Thread, 'start',
                                           side_effect=RuntimeError('cannot start new thread')):
                        with self.assertRaises(RuntimeError):
                            server.start()
                self.assertFalse(server._on)
                self.assertIsNone(server._srv)
                listener.close.assert_called_once()


class TestSocksFailureCleanup(unittest.TestCase):
    def setUp(self):
        self.server = pt.SocksServer(0, ('127.0.0.1', 1))

    def test_client_reset_during_first_tunnel_bytes_closes_upstream(self):
        client, upstream = mock.Mock(), mock.Mock()
        upstream.recv.return_value = b'HTTP/1.1 200 Connection Established\r\n\r\nfirst bytes'
        client.sendall.side_effect = [None, ConnectionResetError('client closed')]
        with mock.patch.object(pt.socket, 'create_connection', return_value=upstream):
            with self.assertRaises(ConnectionResetError):
                self.server._dial_upstream(client, 'example.com:443')
        upstream.close.assert_called_once()

    def test_exact_status_code_and_http_version_are_required(self):
        for status in (b'HTTP/1.1 502 upstream said 200 unavailable',
                       b'HTTP/1.1 2000 not a status', b'garbage 200 OK'):
            with self.subTest(status=status):
                upstream = mock.Mock()
                upstream.recv.return_value = status + b'\r\n\r\n'
                with mock.patch.object(pt.socket, 'create_connection', return_value=upstream):
                    with self.assertRaises(pt._SocksError):
                        self.server._dial_upstream(mock.Mock(), 'example.com:443')
                upstream.close.assert_called_once()

    def test_valid_connect_keeps_tunnel_bytes_and_returns_socket(self):
        for version in (b'HTTP/1.0', b'HTTP/1.1'):
            with self.subTest(version=version):
                client, upstream = mock.Mock(), mock.Mock()
                upstream.recv.return_value = version + b' 200 OK\r\n\r\npayload'
                with mock.patch.object(pt.socket, 'create_connection', return_value=upstream):
                    result = self.server._dial_upstream(client, 'example.com:443')
                self.assertIs(result, upstream)
                upstream.close.assert_not_called()
                self.assertEqual(client.sendall.call_args_list,
                                 [mock.call(bytes((5, 0, 0, 1)) + b'\x00' * 6),
                                  mock.call(b'payload')])


class TestHandshakeDeadlines(unittest.TestCase):
    def test_fragmented_client_fields_share_one_budget(self):
        server = pt.SocksServer(0, ('127.0.0.1', 1))
        client = mock.Mock()
        now = [0.0]
        fragments = iter((b'\x05\x01', b'\x00', b'\x05\x01\x00\x03',
                          b'\x01', b'x', b'\x01\xbb'))
        def receive(_size):
            now[0] += .25
            return next(fragments)
        client.recv.side_effect = receive
        with mock.patch.object(pt.time, 'monotonic', side_effect=lambda: now[0]):
            with mock.patch.object(pt.SocksServer, 'HANDSHAKE_TIMEOUT', 1.0):
                with self.assertRaises(TimeoutError):
                    server._handshake(client)
        self.assertEqual(client.recv.call_count, 4)
        self.assertEqual(now[0], 1.0)

    def test_fragmented_field_does_not_restart_read_budget(self):
        client = mock.Mock()
        now = [0.0]
        def receive(_size):
            now[0] += .5
            return b'x'
        client.recv.side_effect = receive
        with mock.patch.object(pt.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError):
                pt.SocksServer._recv_exact(client, 3, deadline=1.0)
        self.assertEqual(client.recv.call_count, 2)
        self.assertEqual(client.settimeout.call_args_list, [mock.call(1.0), mock.call(.5)])

    def test_upstream_header_has_its_own_finite_budget(self):
        server = pt.SocksServer(0, ('127.0.0.1', 1))
        client, upstream = mock.Mock(), mock.Mock()
        now = [0.0]
        def dial(*_args, **_kwargs):
            now[0] += 2.0
            return upstream
        chunks = iter((b'HTTP/1.1 ', b'200 OK\r\n\r\n'))
        def receive(_size):
            now[0] += 1.0
            return next(chunks)
        upstream.recv.side_effect = receive
        with mock.patch.object(pt.time, 'monotonic', side_effect=lambda: now[0]):
            with mock.patch.object(pt, 'UPSTREAM_CONNECT_TIMEOUT_SECS', 1.0):
                with mock.patch.object(pt.socket, 'create_connection', side_effect=dial):
                    with self.assertRaises(TimeoutError):
                        server._dial_upstream(client, 'example.com:443')
        self.assertEqual(upstream.recv.call_count, 1)
        upstream.close.assert_called_once()

    def test_successful_socks_tunnel_removes_handshake_timeouts(self):
        server = pt.SocksServer(0, ('127.0.0.1', 1))
        server._on = True
        client, upstream = mock.Mock(), mock.Mock()
        client.getpeername.return_value = ('127.0.0.1', 12345)
        with mock.patch.object(server, '_handshake', return_value='example.com:443'):
            with mock.patch.object(server, '_dial_upstream', return_value=upstream):
                with mock.patch.object(server, '_pipe'):
                    server._pump(client)
        client.settimeout.assert_any_call(None)
        upstream.settimeout.assert_any_call(None)
        self.assertEqual(server.active_conns, 0)
        self.assertEqual(server._live, set())


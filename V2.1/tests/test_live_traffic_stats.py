"""Live byte accounting across all gateways, using real TCP sockets."""
from contextlib import contextmanager
import os
import socket
import struct
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def read_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(min(65536, count - len(data)))
        if not chunk:
            raise AssertionError(f'Unexpected EOF: {len(data)} / {count}')
        data.extend(chunk)
    return bytes(data)


class EchoUpstream:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.05)
        self.address = self.listener.getsockname()
        self.stopped = threading.Event()
        self.clients = set()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                self.clients.add(client)
            threading.Thread(target=self.serve, args=(client,), daemon=True).start()

    def serve(self, client):
        try:
            client.settimeout(5)
            first = client.recv(65536)
            if first.startswith(b'CONNECT '):
                while b'\r\n\r\n' not in first:
                    chunk = client.recv(65536)
                    if not chunk:
                        return
                    first += chunk
                _, first = first.split(b'\r\n\r\n', 1)
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            if first:
                client.sendall(first)
            while data := client.recv(65536):
                client.sendall(data)
        except OSError:
            pass
        finally:
            client.close()
            with self.lock:
                self.clients.discard(client)

    def reset_clients(self):
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            # Force an RST rather than an orderly half-close.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                              struct.pack('HH' if os.name == 'nt' else 'ii', 1, 0))
            client.close()

    def close(self):
        self.stopped.set()
        self.listener.close()
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        self.thread.join(1)


@contextmanager
def connected_gateway(kind):
    upstream = EchoUpstream()
    if kind == 'http':
        server = pt.HttpServer(0, pt.UpstreamPool([upstream.address]))
    elif kind == 'relay':
        server = pt.RelayServer(0, upstream.address)
    else:
        server = pt.SocksServer(0, upstream.address)
    client = None
    server.start()
    try:
        client = socket.create_connection(('127.0.0.1', server._srv.getsockname()[1]), timeout=3)
        if kind == 'socks':
            client.sendall(b'\x05\x01\x00')
            assert read_exact(client, 2) == b'\x05\x00'
            client.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01\x00\x50')
            assert read_exact(client, 10)[:2] == b'\x05\x00'
        yield server, client, upstream
    finally:
        if client is not None:
            client.close()
        server.stop()
        eventually(lambda: server.active_conns == 0)
        upstream.close()


def totals(snapshot, kind):
    if kind == 'http':
        return tuple(sum(row[index] for row in snapshot[1].values()) for index in (1, 2))
    device_totals = tuple(sum(row[index] for row in snapshot[1]) for index in (3, 4))
    assert device_totals == snapshot[2], (device_totals, snapshot[2])
    return snapshot[2]


def committed_totals(server, kind):
    with server._lock:
        if kind == 'http':
            return tuple(sum(row[key] for row in server.stats.values()) for key in ('bd', 'bu'))
        return server.bytes_down, server.bytes_up


class TestLiveTrafficStats(unittest.TestCase):
    def test_small_persistent_traffic_is_visible_before_eof(self):
        for kind in ('http', 'relay', 'socks'):
            with self.subTest(gateway=kind), connected_gateway(kind) as (server, client, _):
                payload = b'live small message'
                client.sendall(payload)
                self.assertEqual(read_exact(client, len(payload)), payload)
                self.assertTrue(eventually(lambda: totals(server.snapshot(), kind) == (len(payload),) * 2))
                previous = server.snapshot()
                self.assertEqual(previous[0], 1)
                self.assertEqual(committed_totals(server, kind), (0, 0))
                extra = b'second message'
                client.sendall(extra)
                self.assertEqual(read_exact(client, len(extra)), extra)
                size = len(payload) + len(extra)
                self.assertTrue(eventually(lambda: totals(server.snapshot(), kind) == (size, size)))
                self.assertEqual(totals(previous, kind), (len(payload),) * 2)
                self.assertEqual(len(server._pending), 2)

    def test_batch_handoff_has_no_double_count(self):
        for kind in ('http', 'relay', 'socks'):
            with self.subTest(gateway=kind):
                reached, release = threading.Event(), threading.Event()
                arrivals = []
                lock = threading.Lock()
                real_pipe = pt._pipe_sockets
                def paused_pipe(src, dst, flush, progress=None):
                    def paused_flush(count):
                        if count >= 262144:
                            with lock:
                                arrivals.append(count)
                                if len(arrivals) == 2:
                                    reached.set()
                            if not release.wait(3):
                                raise AssertionError('Flush test barrier timed out')
                        flush(count)
                    return real_pipe(src, dst, paused_flush, progress)
                with mock.patch.object(pt, '_pipe_sockets', side_effect=paused_pipe):
                    with connected_gateway(kind) as (server, client, _):
                        try:
                            payload = b'x' * 262144
                            client.sendall(payload)
                            self.assertEqual(read_exact(client, len(payload)), payload)
                            self.assertTrue(reached.wait(3))
                            self.assertEqual(committed_totals(server, kind), (0, 0))
                            self.assertEqual(totals(server.snapshot(), kind), (len(payload),) * 2)
                            release.set()
                            self.assertTrue(eventually(lambda: committed_totals(server, kind) == (len(payload),) * 2))
                            self.assertEqual(totals(server.snapshot(), kind), (len(payload),) * 2)
                            # New tail bytes after the batch must be added only once.
                            client.sendall(b'tail')
                            self.assertEqual(read_exact(client, 4), b'tail')
                            self.assertTrue(eventually(lambda: totals(server.snapshot(), kind) == (len(payload) + 4,) * 2))
                        finally:
                            release.set()

    def test_stop_flushes_tail_and_removes_pending_records(self):
        for kind in ('http', 'relay', 'socks'):
            with self.subTest(gateway=kind), connected_gateway(kind) as (server, client, _):
                client.sendall(b'stop')
                self.assertEqual(read_exact(client, 4), b'stop')
                self.assertTrue(eventually(lambda: totals(server.snapshot(), kind) == (4, 4)))
                server.stop()
                self.assertTrue(eventually(lambda: server.active_conns == 0 and not server._pending))
                self.assertEqual(committed_totals(server, kind), (4, 4))
                self.assertEqual(totals(server.snapshot(), kind), (4, 4))
                self.assertEqual(server._live, set())

    def test_upstream_reset_flushes_tail_and_removes_pending_records(self):
        for kind in ('http', 'relay', 'socks'):
            with self.subTest(gateway=kind), connected_gateway(kind) as (server, client, upstream):
                client.sendall(b'reset')
                self.assertEqual(read_exact(client, 5), b'reset')
                self.assertTrue(eventually(lambda: totals(server.snapshot(), kind) == (5, 5)))
                upstream.reset_clients()
                try:
                    self.assertEqual(client.recv(1), b'')
                except OSError:
                    pass
                client.close()
                self.assertTrue(eventually(lambda: server.active_conns == 0 and not server._pending))
                self.assertEqual(committed_totals(server, kind), (5, 5))
                self.assertEqual(server._live, set())

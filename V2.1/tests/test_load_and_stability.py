import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return predicate()


class TestLoadBalancing(unittest.TestCase):
    def test_busy_upstream_gets_no_extra_work_when_other_is_idle(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        first_up, first_token = pool.acquire_attempt(set())
        self.assertEqual(first_up, a)
        pool.finish_attempt(a, first_token, True)
        # Slow A remains busy while B completes several sequential downloads.
        for _ in range(5):
            up, token = pool.acquire_attempt(set())
            self.assertEqual(up, b)
            pool.finish_attempt(up, token, True)
            pool.release_attempt(token)
        pool.release_attempt(first_token)
        self.assertEqual(pool._loads, {})
        self.assertEqual(pool._leases, {})

    def test_parallel_selection_reserves_load_atomically(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        barrier = threading.Barrier(32)
        results = []
        lock = threading.Lock()
        def select():
            barrier.wait(timeout=5)
            selected = pool.acquire_attempt(set())
            with lock:
                results.append(selected)
        workers = [threading.Thread(target=select) for _ in range(32)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        self.assertEqual(len(results), 32)
        self.assertEqual([up for up, _ in results].count(a), 16)
        self.assertEqual([up for up, _ in results].count(b), 16)
        for up, token in results:
            pool.finish_attempt(up, token, False)
            pool.release_attempt(token)  # Idempotent after a failed dial.
        self.assertEqual(pool._loads, {})

    def test_active_load_survives_preset_change_and_old_completion(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a, b])
        token = pool.begin_attempt(a)
        pool.finish_attempt(a, token, True)
        pool.set([a, b])
        self.assertEqual(pool.order()[0], b)
        pool.release_attempt(token)
        pool.release_attempt(token)
        self.assertEqual(pool._loads, {})

    def test_failed_dial_and_stopped_entry_release_load(self):
        pool = pt.UpstreamPool([('a', 1), ('b', 2)])
        server = pt.HttpServer(0, pool)
        server._on = True
        with mock.patch.object(pt, '_dial_connection', side_effect=OSError()):
            server._pump(mock.Mock())
        self.assertEqual(pool._loads, {})
        self.assertEqual(pool._leases, {})
        self.assertEqual(server._live, set())


class EchoServer:
    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.address = self.socket.getsockname()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.echo, args=(conn,), daemon=True).start()

    @staticmethod
    def echo(conn):
        with conn:
            conn.settimeout(8)
            try:
                while data := conn.recv(65536):
                    conn.sendall(data)
            except OSError:
                pass

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(1)


class TestConnectionStability(unittest.TestCase):
    def test_full_capacity_keeps_existing_connection_then_accepts_after_release(self):
        upstream = EchoServer()
        pool = pt.UpstreamPool([upstream.address])
        server = pt.HttpServer(0, pool)
        slots = threading.BoundedSemaphore(1)
        clients = []
        with mock.patch.object(pt, '_FORWARD_SLOTS', slots):
            server.start()
            try:
                address = server._srv.getsockname()
                first = socket.create_connection(address, timeout=3)
                clients.append(first)
                first.sendall(b'one')
                self.assertEqual(first.recv(3), b'one')
                second = socket.create_connection(address, timeout=3)
                clients.append(second)
                try:
                    self.assertEqual(second.recv(1), b'')
                except ConnectionResetError:
                    pass
                self.assertEqual(server.rejected_conns, 1)
                first.sendall(b'two')
                self.assertEqual(first.recv(3), b'two')
                first.close()
                self.assertTrue(eventually(lambda: server.active_conns == 0 and not server._live))
                # Wait for the worker's final slot release, not just its counters.
                self.assertTrue(slots.acquire(timeout=3))
                slots.release()
                third = socket.create_connection(address, timeout=3)
                clients.append(third)
                third.sendall(b'three')
                self.assertEqual(third.recv(5), b'three')
            finally:
                for client in clients:
                    client.close()
                server.stop()
                upstream.close()
                self.assertTrue(eventually(lambda: not server._live and not pool._loads))

    def test_thread_start_failure_releases_capacity_and_client(self):
        server = pt.HttpServer(0, pt.UpstreamPool([]))
        server._on = True
        listener = server._srv = object()
        slots = threading.BoundedSemaphore(1)
        client = mock.Mock()
        with mock.patch.object(pt, '_FORWARD_SLOTS', slots):
            with mock.patch.object(pt.threading.Thread, 'start', side_effect=RuntimeError()):
                pt._start_client_worker(server, client, listener)
        client.close.assert_called_once()
        self.assertEqual(server._live, set())
        self.assertTrue(slots.acquire(blocking=False))

    def test_old_listener_cannot_start_worker_after_restart(self):
        server = pt.HttpServer(0, pt.UpstreamPool([]))
        server._on = True
        server._srv = object()
        client = mock.Mock()
        with mock.patch.object(server, '_pump') as pump:
            pt._start_client_worker(server, client, object())
        pump.assert_not_called()
        client.close.assert_called_once()

    def test_close_error_during_rejection_still_releases_slot(self):
        server = pt.HttpServer(0, pt.UpstreamPool([]))
        slots = threading.BoundedSemaphore(1)
        client = mock.Mock()
        client.close.side_effect = OSError('socket already invalid')
        with mock.patch.object(pt, '_FORWARD_SLOTS', slots):
            pt._start_client_worker(server, client, object())
        self.assertTrue(slots.acquire(blocking=False))

    def test_idle_tunnel_survives_dial_timeout(self):
        upstream = EchoServer()
        server = pt.HttpServer(0, pt.UpstreamPool([upstream.address]))
        server.start()
        try:
            with socket.create_connection(server._srv.getsockname(), timeout=5) as client:
                client.sendall(b'first')
                self.assertEqual(client.recv(5), b'first')
                time.sleep(pt.DIAL_TIMEOUT_SECS + .2)
                client.sendall(b'after-idle')
                self.assertEqual(client.recv(10), b'after-idle')
        finally:
            server.stop()
            upstream.close()

    def test_socket_option_failure_does_not_skip_keepalive(self):
        sock = mock.Mock()
        sock.setsockopt.side_effect = [OSError(), None, None, None]
        pt._tune_sock(sock)
        self.assertIn(mock.call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                      sock.setsockopt.call_args_list)
        sock.settimeout.assert_not_called()

    def test_real_socket_has_keepalive_enabled(self):
        with socket.socket() as sock:
            pt._tune_sock(sock)
            self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)

    def test_slow_reader_can_be_stopped_without_lingering_workers(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(5)
        ended = threading.Event()
        def send():
            try:
                with listener.accept()[0] as upstream:
                    upstream.settimeout(5)
                    while True:
                        upstream.sendall(b'x' * 65536)
            except OSError:
                pass
            finally:
                ended.set()
        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        pool = pt.UpstreamPool([listener.getsockname()])
        server = pt.HttpServer(0, pool)
        server.start()
        try:
            with socket.create_connection(server._srv.getsockname(), timeout=3) as client:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                self.assertTrue(client.recv(1))
                time.sleep(.2)
                server.stop()
                self.assertTrue(eventually(lambda: server.active_conns == 0 and not server._live))
                self.assertEqual(pool._loads, {})
                self.assertTrue(ended.wait(5))
        finally:
            server.stop()
            listener.close()

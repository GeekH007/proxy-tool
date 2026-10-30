"""Bounded diagnostics, privacy and network failure isolation; all files are temporary."""

import json
import os
import queue
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy_tool as pt


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        pt.close_diagnostics()
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.log_dir = os.path.join(self.temporary, "logs")
        self.addCleanup(pt.close_diagnostics)

    def start(self):
        self.assertTrue(pt.init_diagnostics(self.log_dir))
        return pt._DIAGNOSTICS

    def records(self, limit=200):
        return [json.loads(line) for line in pt.read_recent_diagnostics(limit).splitlines()]

    def test_without_explicit_init_logging_has_no_files_or_threads(self):
        with mock.patch.object(pt, "_DiagnosticLog") as constructor:
            self.assertFalse(pt._diagnostic("app_start"))
            self.assertEqual(pt.read_recent_diagnostics(), "")
            self.assertFalse(pt.diagnostics_status()["enabled"])
            with self.assertRaises(OSError):
                pt.export_diagnostics(os.path.join(self.temporary, "unused.zip"))
        constructor.assert_not_called()
        self.assertEqual(os.listdir(self.temporary), [])

    def test_only_allowlisted_event_fields_and_error_codes_are_recorded(self):
        self.start()
        secret = "private-password https://account:token@example.test/private?q=secret"
        self.assertTrue(pt._diagnostic(
            "health_transition", upstream=("192.0.2.10", 7890),
            error=OSError(13, secret), state="degraded", stage="probe",
            elapsed_ms=45, failures=2, successes=0, password=secret,
            url=secret, target=secret, request=secret, headers={"Authorization": secret}))
        record, = self.records()
        self.assertEqual(record["upstream"], ["192.0.2.10", 7890])
        self.assertEqual(record["state"], "degraded")
        self.assertEqual(record["errno"], 13)
        self.assertEqual(record["error"], "io")
        self.assertEqual(record["elapsed_ms"], 45)
        self.assertTrue(set(record).isdisjoint({"password", "url", "target", "request", "headers"}))
        self.assertNotIn(secret, json.dumps(record))

    def test_url_credentials_and_unrecognized_text_are_dropped(self):
        self.start()
        self.assertTrue(pt._diagnostic("dial_failed", upstream=("user:password@host", 80),
                                       stage="https://private.test", state="password",
                                       elapsed_ms=float("nan"), count="secret"))
        record, = self.records()
        self.assertEqual(set(record), {"time", "event"})
        self.assertFalse(pt._diagnostic("GET https://private.test?password=secret"))

    def test_health_probe_errors_keep_fixed_target_labels_and_safe_error_details(self):
        self.start()
        failures = (
            pt._ConnectivityFailure([
                ("Google HTTPS", TimeoutError("secret target URL")),
                ("Gstatic HTTPS", pt._ProbeHttpError(403)),
            ]),
            pt._ConnectivityFailure([
                ("Google HTTPS", ssl.SSLCertVerificationError(1, "secret certificate details")),
                ("Gstatic HTTPS", OSError("Tunnel connection failed: 407 secret credentials")),
            ]),
        )
        for error in failures:
            self.assertTrue(pt._diagnostic("health_transition", error=error, state="degraded"))
        records = self.records()
        self.assertEqual(records[0]["checks"], [
            {"target": "google_https", "error": "timeout"},
            {"target": "gstatic_https", "error": "http", "status": 403},
        ])
        self.assertEqual(records[1]["checks"], [
            {"target": "google_https", "error": "certificate", "errno": 1},
            {"target": "gstatic_https", "error": "tunnel", "status": 407},
        ])
        destination = os.path.join(self.temporary, "health.zip")
        pt.export_diagnostics(destination)
        with zipfile.ZipFile(destination) as archive:
            text = archive.read("diagnostics.jsonl").decode("utf-8")
        self.assertNotIn("secret", text)
        self.assertEqual([json.loads(line) for line in text.splitlines()], records)

    def test_unrecognized_probe_target_and_extra_check_fields_are_not_exported(self):
        self.start()
        pt._diagnostic("health_transition", checks=[
            {"target": "https://private.test", "error": "timeout", "password": "secret"},
            {"target": "google_https", "error": "http", "status": 403, "password": "secret"},
        ])
        record, = self.records()
        self.assertEqual(record["checks"], [{"target": "google_https", "error": "http", "status": 403}])
        self.assertNotIn("secret", json.dumps(record))

    def test_rotation_bounds_disk_usage_and_keeps_latest_events(self):
        with mock.patch.object(pt, "_DIAGNOSTIC_MAX_BYTES", 512):
            self.start()
            for number in range(80):
                self.assertTrue(pt._diagnostic("rotation_test", count=number))
            records = self.records()
            names = os.listdir(self.log_dir)
            self.assertEqual(set(names), {"diagnostics.jsonl", "diagnostics.jsonl.1",
                                          "diagnostics.jsonl.2", "diagnostics.jsonl.3"})
            self.assertTrue(all(os.path.getsize(os.path.join(self.log_dir, name)) <= 512
                                for name in names))
            self.assertEqual(records[-1]["count"], 79)
            self.assertLess(len(records), 80)

    def test_read_recent_is_ordered_and_obeys_limit(self):
        self.start()
        for number in range(12):
            pt._diagnostic("recent_test", count=number)
        self.assertEqual([record["count"] for record in self.records(3)], [9, 10, 11])

    def test_export_flushes_queued_events_and_excludes_configuration(self):
        self.start()
        for number in range(20):
            self.assertTrue(pt._diagnostic("export_test", count=number))
        with open(os.path.join(self.log_dir, "proxy_config.json"), "w") as dst:
            json.dump({"socks_pass": "never-export-this-password"}, dst)
        destination = os.path.join(self.temporary, "diagnostics.zip")
        self.assertEqual(pt.export_diagnostics(destination), destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(archive.namelist(), ["diagnostics.jsonl"])
            text = archive.read("diagnostics.jsonl").decode("utf-8")
        self.assertEqual([json.loads(line)["count"] for line in text.splitlines()], list(range(20)))
        self.assertNotIn("never-export-this-password", text)

    def test_export_sanitizes_old_or_tampered_log_lines(self):
        self.start()
        pt._diagnostic("app_start")
        self.records()  # Drain the write queue before appending a simulated old log line.
        with open(os.path.join(self.log_dir, "diagnostics.jsonl"), "a", encoding="utf-8") as dst:
            dst.write(json.dumps({"time": "2026-10-26T00:00:00Z", "event": "old_event",
                                  "upstream": ["https://user:secret@private.test", 80],
                                  "password": "never-export", "error": "sensitive exception text",
                                  "stage": "dial", "count": 3}) + "\n")
            dst.write("invalid log with password=never-export\n")
        destination = os.path.join(self.temporary, "clean.zip")
        pt.export_diagnostics(destination)
        with zipfile.ZipFile(destination) as archive:
            text = archive.read("diagnostics.jsonl").decode("utf-8")
        self.assertNotIn("never-export", text)
        self.assertNotIn("sensitive exception", text)
        self.assertNotIn("private.test", text)
        old = [json.loads(line) for line in text.splitlines() if "old_event" in line][0]
        self.assertEqual(set(old), {"time", "event", "stage", "count"})

    def test_full_queue_drops_events_without_waiting_for_disk(self):
        with mock.patch.object(pt, "_DIAGNOSTIC_QUEUE_SIZE", 4):
            current = self.start()
        entered, release = threading.Event(), threading.Event()
        original_emit = current.handler.emit

        def stalled_emit(record):
            entered.set()
            release.wait(5)
            original_emit(record)

        try:
            with mock.patch.object(current.handler, "emit", side_effect=stalled_emit):
                self.assertTrue(pt._diagnostic("first_event"))
                self.assertTrue(entered.wait(2))
                for _ in range(4):
                    self.assertTrue(pt._diagnostic("queued_event"))
                started = time.perf_counter()
                self.assertFalse(pt._diagnostic("dropped_event"))
                self.assertLess(time.perf_counter() - started, .5)
                self.assertEqual(current.queue.qsize(), 4)
        finally:
            release.set()
        pt.close_diagnostics()
        self.assertFalse(current.thread.is_alive())

    def test_log_creation_failure_is_safe_and_does_not_expose_os_error(self):
        with mock.patch.object(pt.os, "makedirs", side_effect=PermissionError("secret path")):
            self.assertFalse(pt.init_diagnostics(self.log_dir))
        self.assertFalse(pt._diagnostic("app_start"))
        status = pt.diagnostics_status()
        self.assertFalse(status["enabled"])
        self.assertTrue(status["error"])
        self.assertNotIn("secret path", status["error"])

    def test_write_failure_keeps_logger_thread_alive_and_error_is_safe(self):
        current = self.start()
        with mock.patch.object(current.handler, "emit", side_effect=OSError("secret disk detail")):
            self.assertTrue(pt._diagnostic("disk_failure_test"))
            self.records()
        self.assertTrue(current.thread.is_alive())
        self.assertTrue(pt.diagnostics_status()["error"])
        self.assertNotIn("secret disk detail", pt.diagnostics_status()["error"])

    def test_real_refused_dials_from_all_three_servers_are_written(self):
        self.start()
        # Keep a localhost port reserved but not listening: real dialing fails,
        # without depending on external networking or racing another listener.
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            address = unavailable.getsockname()
            servers = (
                ("http", pt.HttpServer(0, pt.UpstreamPool([address])), "upstream_dial_failed"),
                ("relay", pt.RelayServer(0, address), "upstream_dial_failed"),
                ("socks", pt.SocksServer(0, address), "socks_session_failed"),
            )
            for name, server, event in servers:
                with self.subTest(server=name):
                    server._on = True
                    client = mock.Mock()
                    client.getpeername.return_value = ("127.0.0.1", 12345)
                    if name == "socks":
                        # Only isolate the client handshake; upstream dialing is real.
                        with mock.patch.object(server, "_handshake", return_value="private-target.test:443"):
                            server._pump(client)
                    else:
                        server._pump(client)
                    entries = [record for record in self.records()
                               if record["event"] == event and record.get("server") == name]
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(entries[0]["upstream"], list(address))
                    self.assertEqual(entries[0]["error"], "refused")
                    self.assertIsInstance(entries[0]["errno"], int)
                    self.assertGreaterEqual(entries[0]["elapsed_ms"], 0)
                    self.assertNotIn("private-target.test", json.dumps(entries))
                    self.assertEqual(server.active_conns, 0)

    def test_real_socket_read_and_write_errors_are_written_without_payload(self):
        self.start()
        closed = socket.socket()
        closed.close()
        with socket.socket() as destination:
            pt._pipe_sockets(closed, destination, mock.Mock())
        source, peer = socket.socketpair()
        with source, peer:
            peer.sendall(b"secret request payload https://private-target.test")
            pt._pipe_sockets(source, closed, mock.Mock())
        records = [record for record in self.records() if record["event"] == "pipe_failed"]
        self.assertEqual([record["stage"] for record in records], ["recv", "send"])
        self.assertTrue(all(record["error"] == "io" for record in records))
        self.assertTrue(all(isinstance(record["errno"], int) for record in records))
        self.assertNotIn("secret", json.dumps(records))
        self.assertNotIn("private-target.test", json.dumps(records))

    def test_successful_failover_writes_failed_and_selected_upstreams(self):
        self.start()
        first, second = ("192.0.2.1", 80), ("192.0.2.2", 80)
        server = pt.HttpServer(0, pt.UpstreamPool([first, second]))
        server._on = True
        with mock.patch.object(pt.socket, "create_connection",
                               side_effect=[ConnectionRefusedError(10061, "secret"), mock.Mock()]), \
                mock.patch.object(server, "_pipe"):
            server._pump(mock.Mock())
        records = self.records()
        failure, = [record for record in records if record["event"] == "upstream_dial_failed"]
        selected, = [record for record in records if record["event"] == "upstream_failover"]
        self.assertEqual(failure["upstream"], list(first))
        self.assertEqual(selected["upstream"], list(second))
        self.assertEqual(selected["attempts"], 2)

    def test_connection_capacity_rejection_is_written(self):
        self.start()
        server = pt.RelayServer(0, ("127.0.0.1", 7890))
        client = mock.Mock()
        with mock.patch.object(pt, "_FORWARD_SLOTS", threading.BoundedSemaphore(0)):
            pt._start_client_worker(server, client, None)
        record, = self.records()
        self.assertEqual(record["event"], "connection_rejected")
        self.assertEqual(record["rejected"], 1)
        client.close.assert_called_once()

    def test_failed_logger_cannot_interrupt_upstream_failover(self):
        current = self.start()
        pool = pt.UpstreamPool([("192.0.2.1", 80), ("192.0.2.2", 80)])
        server = pt.HttpServer(0, pool)
        server._on = True
        client, upstream = mock.Mock(), mock.Mock()
        with mock.patch.object(current.queue, "put_nowait", side_effect=RuntimeError("queue failed")), \
                mock.patch.object(pt.socket, "create_connection",
                                  side_effect=[ConnectionRefusedError(10061, "private"), upstream]) as dial, \
                mock.patch.object(server, "_pipe") as pipe:
            server._pump(client)
        self.assertEqual(dial.call_count, 2)
        self.assertEqual(pipe.call_count, 2)
        self.assertEqual(server.active_conns, 0)
        self.assertEqual(server._live, set())
        self.assertEqual(pool._loads, {})

    def test_pipe_failure_still_shuts_sockets_when_diagnostics_fail(self):
        current = self.start()
        source, destination = mock.Mock(), mock.Mock()
        source.recv_into.side_effect = ConnectionResetError(10054, "private target")
        with mock.patch.object(current.queue, "put_nowait", side_effect=queue.Full):
            pt._pipe_sockets(source, destination, mock.Mock())
        source.shutdown.assert_called_with(socket.SHUT_RDWR)
        self.assertIn(mock.call(socket.SHUT_RDWR), destination.shutdown.call_args_list)
        self.assertIn(mock.call(socket.SHUT_WR), destination.shutdown.call_args_list)

    def test_close_drains_pending_records_releases_file_and_is_idempotent(self):
        current = self.start()
        for number in range(10):
            self.assertTrue(pt._diagnostic("shutdown_test", count=number))
        pt.close_diagnostics()
        pt.close_diagnostics()
        self.assertFalse(current.thread.is_alive())
        self.assertFalse(pt.diagnostics_status()["enabled"])
        self.assertIsNone(current.handler.stream)
        with open(os.path.join(self.log_dir, "diagnostics.jsonl"), encoding="utf-8") as src:
            self.assertEqual(len(src.readlines()), 10)
        self.assertFalse(pt._diagnostic("after_close"))

    def test_export_failure_uses_safe_message_and_removes_temporary_archive(self):
        self.start()
        pt._diagnostic("app_start")
        with mock.patch.object(pt.os, "replace", side_effect=OSError("secret filesystem path")):
            with self.assertRaisesRegex(OSError, "诊断日志导出失败") as result:
                pt.export_diagnostics(os.path.join(self.temporary, "report.zip"))
        self.assertNotIn("secret filesystem path", str(result.exception))
        self.assertEqual(os.listdir(self.temporary), ["logs"])


if __name__ == "__main__":
    unittest.main()

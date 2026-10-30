import json
import os
import socket
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

from tests.test_core import _AppTest
import proxy_tool as pt


class TestDiagnosticChecks(unittest.TestCase):
    def run_plan(self, plan, connection=None):
        with mock.patch.object(pt.socket, 'create_connection', return_value=mock.MagicMock()), \
                mock.patch.object(pt.http.client, 'HTTPSConnection', return_value=connection) as factory:
            report = pt._run_diagnostic_checks(plan, threading.Event())
        return report, factory

    def test_https_stages_and_response_failure_fallback(self):
        conn = mock.MagicMock()
        first, second = mock.MagicMock(), mock.MagicMock()
        first.__enter__.return_value.status = 403
        second.__enter__.return_value.status = 204
        conn.getresponse.side_effect = [first, second]
        report, factory = self.run_plan([{'label': '上游', 'address': ('proxy', 80)}], conn)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(conn.close.call_count, 2)
        self.assertTrue(any(row['detail'] == 'HTTP403' for row in report['results']))
        self.assertEqual(report['results'][-1]['status'], 'passed')
        self.assertIn('HTTPS 响应', report['results'][-1]['stage'])

    def test_tcp_failure_prevents_https_and_retains_stage(self):
        with mock.patch.object(pt.socket, 'create_connection', side_effect=ConnectionRefusedError()), \
                mock.patch.object(pt.http.client, 'HTTPSConnection') as https:
            report = pt._run_diagnostic_checks([{'label': '上游', 'address': ('proxy', 80)}], threading.Event())
        https.assert_not_called()
        self.assertEqual(report['results'][0]['stage'], 'TCP 端口')
        self.assertEqual(report['results'][0]['status'], 'failed')

    def test_disabled_and_cancelled_plans_do_not_probe(self):
        with mock.patch.object(pt.socket, 'create_connection') as connect:
            event = threading.Event()
            plan = [{'label': '上游', 'address': ('proxy', 80), 'disabled': True}]
            report = pt._run_diagnostic_checks(plan, event)
            self.assertEqual(report['results'][0]['status'], 'skipped')
            event.set()
            self.assertEqual(pt._run_diagnostic_checks(plan, event)['status'], 'cancelled')
        connect.assert_not_called()

    def test_unused_local_port_response_is_warning_not_service_success(self):
        report, factory = self.run_plan([{'label': '入口', 'address': ('127.0.0.1', 1234), 'running': False}])
        self.assertEqual(report['results'][0]['status'], 'warning')
        factory.assert_not_called()

    def test_real_local_listener_does_not_claim_remote_socks_access(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            report = pt._run_diagnostic_checks([{'label': 'SOCKS', 'address': listener.getsockname(),
                                                'https': False}], threading.Event())
        self.assertEqual(report['results'][0]['status'], 'passed')
        self.assertEqual(report['results'][1]['status'], 'skipped')
        self.assertIn('未测试认证', report['results'][1]['detail'])

    def test_cancel_between_stages_closes_connection(self):
        event = threading.Event()
        conn = mock.MagicMock()
        conn.connect.side_effect = event.set
        with mock.patch.object(pt.socket, 'create_connection', return_value=mock.MagicMock()), \
                mock.patch.object(pt.http.client, 'HTTPSConnection', return_value=conn):
            report = pt._run_diagnostic_checks([{'label': '上游', 'address': ('proxy', 80)}], event)
        self.assertEqual(report['status'], 'cancelled')
        conn.close.assert_called_once()
        conn.request.assert_not_called()

    def test_export_works_without_logs_and_includes_structured_and_readable_report(self):
        report = pt._run_diagnostic_checks([], threading.Event())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(pt, '_DIAGNOSTICS', None):
            path = os.path.join(directory, 'report.zip')
            pt._export_diagnostic_report(path, report)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {'report.json', 'report.txt'})
                self.assertEqual(json.loads(archive.read('report.json'))['logs'], 'unavailable')
                self.assertIn('仅包含诊断结果', archive.read('report.txt').decode())


class TestDiagnosticWindow(_AppTest):
    def test_snapshot_plan_includes_disabled_nodes_and_running_actual_port(self):
        self.app.pool_box.selection_set('0')
        self.app.toggle_upstream()
        self.app._entry = mock.Mock(listen_port=5678)
        plan = self.app._diagnosis_plan()
        self.assertTrue(plan[0]['disabled'])
        self.assertEqual(plan[2]['address'], ('127.0.0.1', 5678))
        self.assertFalse(plan[-1]['https'])

    def test_diagnostic_window_finishes_on_ui_queue_and_fits_screen(self):
        report = pt._run_diagnostic_checks([], threading.Event())
        with mock.patch.object(pt, '_run_diagnostic_checks', return_value=report):
            self.app.open_diagnosis()
            self.app._ui_queue.get(timeout=3)()
        self.root.update_idletasks()
        win = self.app._diagnosis_win
        self.assertFalse(self.app._diagnosis_busy)
        self.assertLessEqual(win.winfo_reqwidth(), win.winfo_screenwidth())
        self.assertLessEqual(win.winfo_reqheight(), win.winfo_screenheight() - 64)

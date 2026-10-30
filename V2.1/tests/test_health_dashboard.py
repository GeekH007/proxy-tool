"""Health and diagnostics UI integration; no external probes or system changes."""
import threading
from unittest import mock

from tests.test_core import _AppTest
import proxy_tool as pt


class TestHealthDashboard(_AppTest):
    def test_background_status_uses_all_rows_and_preserves_disabled_label(self):
        self.app.pool_box.selection_set('0')
        self.assertTrue(self.app.toggle_upstream())
        up = ('2.2.2.2', 80)
        token = self.app._pool.begin_health_probe(up)
        self.app._pool.finish_health_probe(up, token, True, 135)
        self.app._refresh_health_status()
        self.assertEqual(self.app.pool_box.set('0', 'test'), '已停用')
        self.assertEqual(self.app.pool_box.set('1', 'test'), '✓ 135 ms')
        self.assertIn('1 正常', self.app.health_summary_var.get())

    def test_new_manual_result_survives_older_background_result(self):
        up = ('1.1.1.1', 80)
        token = self.app._pool.begin_health_probe(up)
        self.app._pool.finish_health_probe(up, token, True, 10)
        self.app._show_test_result(self.app._test_gen, '1.1.1.1:80', True, 90, 1)
        self.app._refresh_health_status()
        self.assertEqual(self.app.pool_box.set('0', 'test'), '✓ 90 ms')
        token = self.app._pool.begin_health_probe(up)
        self.app._pool.finish_health_probe(up, token, True, 20)
        self.app._refresh_health_status()
        self.assertEqual(self.app.pool_box.set('0', 'test'), '✓ 20 ms')

    def test_remark_edit_does_not_forgive_degraded_upstream(self):
        up = ('1.1.1.1', 80)
        for _ in range(3):
            token = self.app._pool.begin_health_probe(up)
            self.app._pool.finish_health_probe(up, token, False, TimeoutError())
        self.assertTrue(self.app._edit_upstream_at(0, up[0], '80', '办公室电脑'))
        self.assertEqual(self.app._pool.health_snapshot()[up]['state'], 'degraded')

    def test_details_distinguish_https_probe_and_tcp_connectivity(self):
        up = ('1.1.1.1', 80)
        for _ in range(3):
            token = self.app._pool.begin_health_probe(up)
            self.app._pool.finish_health_probe(up, token, False, TimeoutError())
        with mock.patch.object(pt.messagebox, 'showinfo') as show:
            self.app.show_test_details()
        detail = show.call_args.args[1]
        self.assertIn('HTTPS：已降级', detail)
        self.assertIn('TCP 拨号', detail)
        self.assertIn('连续 3 次失败', detail)

    def test_quit_stops_monitor_and_local_entry(self):
        self.app._entry = mock.Mock()
        with mock.patch.object(self.app._health_monitor, 'stop') as stop:
            self.app.quit_app()
        stop.assert_called_once()
        self.app._entry.stop.assert_called_once()

    def test_entry_start_thread_failure_does_not_leave_broken_server_or_set_proxy(self):
        failed = mock.Mock()
        failed.start.side_effect = RuntimeError('cannot start thread')
        with mock.patch.object(pt, 'HttpServer', return_value=failed), \
                mock.patch.object(pt, 'set_system_proxy') as enable, \
                mock.patch.object(pt.messagebox, 'showerror') as show:
            self.app.apply(True)
        self.assertIsNone(self.app._entry)
        enable.assert_not_called()
        show.assert_called_once()

    def test_socks_start_thread_failure_rolls_back_started_relay(self):
        relay, socks = mock.Mock(), mock.Mock()
        socks.start.side_effect = RuntimeError('cannot start thread')
        self.app._fw_pending = True
        with mock.patch.object(pt, 'RelayServer', return_value=relay), \
                mock.patch.object(pt, 'SocksServer', return_value=socks):
            self.app._start_share(('1.1.1.1', 80), '127.0.0.1')
        relay.stop.assert_called_once()
        self.assertIsNone(self.app._relay)
        self.assertIsNone(self.app._socks)
        self.assertFalse(self.app._fw_pending)

    def _run_pending_ui_callback(self):
        callback = self.app._ui_queue.get(timeout=3)
        callback()

    def test_log_read_and_export_use_background_tasks(self):
        read_entered, release_read = threading.Event(), threading.Event()
        self.addCleanup(release_read.set)

        def read_records(_limit):
            read_entered.set()
            release_read.wait(3)
            return '{"event":"upstream_dial_failed"}'

        with mock.patch.object(pt, 'diagnostics_status', return_value={
                'enabled': True, 'directory': 'temporary-logs', 'error': ''}), \
                mock.patch.object(pt, 'read_recent_diagnostics', side_effect=read_records), \
                mock.patch.object(pt.filedialog, 'asksaveasfilename', return_value='chosen.zip'), \
                mock.patch.object(pt, 'export_diagnostics', return_value='chosen.zip') as export, \
                mock.patch.object(pt.messagebox, 'showinfo') as show:
            self.app.open_diagnostics()
            self.assertTrue(read_entered.wait(1))
            win = self.app._diagnostics_win
            descendants = []
            pending = [win]
            while pending:
                widget = pending.pop()
                descendants.append(widget)
                pending.extend(widget.winfo_children())
            button = next(w for w in descendants if isinstance(w, pt.ttk.Button)
                          and w.cget('text') == '导出日志')
            self.assertTrue(button.instate(['disabled']))
            release_read.set()
            self._run_pending_ui_callback()
            display = next(w for w in descendants if isinstance(w, pt.tk.Text))
            self.assertIn('upstream_dial_failed', display.get('1.0', 'end'))
            button.invoke()
            self._run_pending_ui_callback()
            export.assert_called_once_with('chosen.zip')
            self.assertEqual(show.call_args.args[0], '导出完成')

    def test_log_window_can_close_while_read_is_pending(self):
        ready = threading.Event()
        self.addCleanup(ready.set)
        with mock.patch.object(pt, 'read_recent_diagnostics', side_effect=lambda _: ready.wait(3)):
            self.app.open_diagnostics()
            self.app._diagnostics_win.destroy()
            ready.set()
            self._run_pending_ui_callback()

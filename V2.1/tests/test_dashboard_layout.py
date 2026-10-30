"""Geometry checks for visible controls; no snapshots or system-proxy changes."""
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from unittest import mock

from tests.test_core import _AppTest
import proxy_tool as pt


class TestDashboardLayout(_AppTest):
    EXAMPLE_ROW = ('生产主链路…', '255.255.255.255:65535', '999.9 MB/s', '999.9 MB/s', '256', '✗ 测试未通')

    def _show(self):
        self.root.geometry('+20+20')
        self.root.deiconify()
        self.root.update()

    def _assert_fits(self, window):
        self.root.update_idletasks()
        self.assertLessEqual(window.winfo_reqwidth(), window.winfo_screenwidth())
        # Reserve space for title bar and taskbar, rather than allowing clipped chrome.
        self.assertLessEqual(window.winfo_reqheight(), window.winfo_screenheight() - 64)

        def check(parent):
            children = [child for child in parent.winfo_children()
                        if child.winfo_ismapped() and not isinstance(child, tk.Toplevel)]
            for child in children:
                x, y = child.winfo_x(), child.winfo_y()
                right, bottom = x + child.winfo_width(), y + child.winfo_height()
                self.assertGreaterEqual(x, -1, str(child))
                self.assertGreaterEqual(y, -1, str(child))
                self.assertLessEqual(right, parent.winfo_width() + 1, str(child))
                self.assertLessEqual(bottom, parent.winfo_height() + 1, str(child))
                check(child)
            for index, first in enumerate(children):
                for second in children[index + 1:]:
                    horizontal = min(first.winfo_x() + first.winfo_width(),
                                     second.winfo_x() + second.winfo_width()) - max(
                                         first.winfo_x(), second.winfo_x())
                    vertical = min(first.winfo_y() + first.winfo_height(),
                                   second.winfo_y() + second.winfo_height()) - max(
                                       first.winfo_y(), second.winfo_y())
                    self.assertFalse(horizontal > 1 and vertical > 1,
                                     f'Overlapping controls: {first} and {second}')
        check(window)

    def test_single_and_multi_controls_fit(self):
        self._show()
        for name in ('s', 'm'):
            with self.subTest(preset=name):
                self.app.preset_var.set(name)
                self.app._apply_form()
                self.root.update()
                self._assert_fits(self.root)

    def test_metrics_and_pool_columns_fit(self):
        self._show()
        self.app.metric_down.set('999.9 MB/s')
        self.app.metric_up.set('999.9 MB/s')
        self.app.metric_conns.set('256')
        self.root.update()
        self._assert_fits(self.root)
        self._assert_pool_readable()

    def _assert_pool_readable(self):
        """Default cells and headings need room for text plus ordinary cell padding."""
        table = self.app.pool_box
        item = table.get_children()[0]
        table.item(item, values=self.EXAMPLE_ROW)
        self.root.update_idletasks()
        widths = sum(table.column(column, 'width') for column in table['columns'])
        self.assertLessEqual(widths, table.winfo_width())
        style = ttk.Style(self.root)
        table_font = tkfont.Font(self.root, font=style.lookup('Treeview', 'font'))
        heading_font = tkfont.Font(self.root, font=style.lookup('Treeview.Heading', 'font'))
        for column, example in zip(table['columns'], self.EXAMPLE_ROW):
            self.assertLessEqual(table_font.measure(example) + 24, table.column(column, 'width'),
                                 f'{column} cannot display {example}')
            self.assertLessEqual(heading_font.measure(table.heading(column, 'text')) + 24,
                                 table.column(column, 'width'), f'{column} heading is cramped')
            cell = table.bbox(item, column)
            self.assertTrue(cell, f'{column} is not visible at default horizontal position')
            x, _, width, _ = cell
            self.assertGreaterEqual(x, 0, column)
            self.assertLessEqual(x + width, table.winfo_width(), f'{column} is clipped off the right edge')

    def test_minimum_window_width_keeps_all_proxy_columns_readable(self):
        self._show()
        self.assertTrue(self.root.resizable()[0], 'Main window must allow horizontal resizing')
        minimum_width, _ = self.root.minsize()
        self.assertGreater(minimum_width, 1, 'A meaningful minimum width protects the table')
        self.root.geometry(f'1x{self.root.winfo_height()}+20+20')
        self.root.update()
        self.assertGreaterEqual(self.root.winfo_width(), minimum_width)
        self._assert_pool_readable()
        self._assert_fits(self.root)

    def test_widening_window_expands_proxy_table(self):
        self._show()
        initial_window = self.root.winfo_width()
        initial_table = self.app.pool_box.winfo_width()
        target_width = min(initial_window + 160, self.root.winfo_screenwidth() - 64)
        self.assertGreater(target_width, initial_window)
        self.root.geometry(f'{target_width}x{self.root.winfo_height()}+20+20')
        self.root.update()
        self.assertGreater(self.app.pool_box.winfo_width(), initial_table)
        self._assert_pool_readable()
        self._assert_fits(self.root)

    def test_preset_and_settings_dialogs_fit(self):
        self._show()
        self.app._open_new_preset_dialog()
        self.root.update()
        preset = [child for child in self.root.winfo_children()
                  if isinstance(child, tk.Toplevel)][-1]
        self._assert_fits(preset)
        preset.destroy()
        self.app.open_settings()
        self.root.update()
        self._assert_fits(self.app._settings_win)
        self.app._settings_win.destroy()

    def test_verbose_test_error_does_not_push_controls_off_screen(self):
        self.app.preset_var.set('s')
        self.app._apply_form()
        self._show()
        reason = pt._ConnectivityFailure([
            ('Google HTTPS', OSError('upstream TLS failure: ' + 'certificate detail ' * 40)),
            ('Gstatic HTTPS', OSError('upstream TLS failure: ' + 'certificate detail ' * 40)),
        ])
        self.app._show_test_result(self.app._test_gen, '1.1.1.1:80', False, reason, 1)
        self.root.update()
        self._assert_fits(self.root)

    def test_maximum_note_and_health_summary_fit_at_minimum_width(self):
        self._show()
        note = '这是一个恰好十六个汉字的测试备注'
        self.app.preset_var.set('s')
        self.app._apply_form()
        self.assertTrue(self.app._edit_upstream_at(0, '1.1.1.1', '80', note))
        self.root.update()
        self._assert_fits(self.root)
        self.app.preset_var.set('m')
        self.app._apply_form()
        self.app.health_summary_var.set('HTTPS：255 正常 · 255 待复查 · 255 降级 · 255 恢复中 · 255 待检测')
        self.root.geometry(f'{self.root.minsize()[0]}x{self.root.winfo_reqheight()}')
        self.root.update()
        self._assert_fits(self.root)
        labels = []
        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Label):
                    labels.append(child)
                walk(child)
        walk(self.root)
        for variable in (self.app.health_summary_var, self.app.mode_var):
            label = next(label for label in labels if str(label.cget('textvariable')) == str(variable))
            self.assertTrue(label.winfo_ismapped())
            self.assertGreaterEqual(label.winfo_width(), label.winfo_reqwidth())

    def test_upstream_edit_dialog_shows_full_note_without_clipping(self):
        self._show()
        note = '这是一个恰好十六个汉字的测试备注'
        self.app._edit_upstream_at(0, '255.255.255.255', '65535', note)
        self.app.pool_box.selection_set('0')
        self.app.edit_upstream()
        self.root.update()
        window = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)][-1]
        self._assert_fits(window)
        entries = [child for child in window.winfo_children() if isinstance(child, ttk.Entry)]
        self.assertEqual(entries[2].get(), note)
        text_font = tkfont.Font(self.root, font=ttk.Style(self.root).lookup('TEntry', 'font'))
        self.assertLessEqual(text_font.measure(note) + 20, entries[2].winfo_width())
        window.destroy()

    def test_diagnostics_window_fits_with_long_path_and_many_mock_records(self):
        self._show()
        text = '2026-10-26 simulated diagnostic record\n' * 200
        status = {'enabled': True, 'directory': 'D:/diagnostics/' + 'long-subdirectory/' * 15}
        with mock.patch.object(pt, 'diagnostics_status', return_value=status):
            with mock.patch.object(pt, 'read_recent_diagnostics', return_value=text):
                self.app.open_diagnostics()
                self.app._ui_queue.get(timeout=3)()  # Deliver the isolated worker's UI callback.
                self.root.update()
        window = self.app._diagnostics_win
        self._assert_fits(window)
        displays = []
        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Text):
                    displays.append(child)
                walk(child)
        walk(window)
        self.assertEqual(displays[0].get('1.0', 'end-1c'), text)
        self.assertEqual(str(displays[0]['state']), 'disabled')
        window.destroy()

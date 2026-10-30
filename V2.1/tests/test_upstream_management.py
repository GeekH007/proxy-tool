import copy
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_core import _AppTest
from tests.test_load_and_stability import EchoServer, eventually
import proxy_tool as pt


class TestUpstreamMetadataConfig(unittest.TestCase):
    def test_legacy_presets_do_not_gain_empty_metadata(self):
        preset = pt._normalize({'presets': [{'name': 'old', 'ip': 'proxy', 'port': '80'}]})['presets'][0]
        self.assertEqual(preset, {'name': 'old', 'type': 'single', 'upstreams': [['proxy', '80']]})

    def test_metadata_keys_are_canonical_and_invalid_entries_discarded(self):
        source = {'presets': [{'name': 'm', 'upstreams': [['ProxyA', '80'], ['b', '81']],
                              'upstream_options': {'PROXYA:0080': {'name': '办公主线', 'enabled': False},
                                                   'unknown:80': {'name': 'stale'}, 'invalid': False}}]}
        preset = pt._normalize(source)['presets'][0]
        self.assertEqual(preset['upstream_options'], {'proxya:80': {'name': '办公主线', 'enabled': False}})
        self.assertEqual(pt._enabled_upstreams(preset), [('b', 81)])

    def test_all_disabled_stays_disabled_until_user_enables_a_node(self):
        source = {'presets': [{'name': 's', 'upstreams': [['a', '80']],
                              'upstream_options': {'a:80': {'name': '维护中', 'enabled': False}}}]}
        preset = pt._normalize(source)['presets'][0]
        self.assertEqual(pt._enabled_upstreams(preset), [])
        self.assertFalse(pt._upstream_options(preset, 'a', 80)['enabled'])

    def test_metadata_round_trips_without_losing_disabled_address(self):
        presets = [{'name': 'm', 'type': 'multi', 'upstreams': [['a', '80'], ['b', '80']],
                    'upstream_options': {'a:80': {'name': '备机', 'enabled': False}}}]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory, 'config.json'))
            pt.save_config('m', presets, custom=path)
            restored = pt.load_config(path)
        self.assertEqual(restored['presets'], presets)

    def test_case_and_zero_padded_port_duplicates_normalize_to_one(self):
        data = {'presets': [{'name': 'm', 'upstreams': [['Proxy', '80'], ['proxy', '0080']]}]}
        self.assertEqual(len(pt._normalize(data)['presets'][0]['upstreams']), 1)


class TestUpstreamManagement(_AppTest):
    def test_imported_self_loop_can_be_disabled_but_cannot_be_reenabled(self):
        self.app._current_preset()['upstreams'][0] = ['127.0.0.1', str(self.app.entry_port)]
        self.app._refresh_pool_box()
        self._select(0)
        self.assertTrue(self.app.toggle_upstream())
        self.assertEqual(self.app._pool_addrs(), [('2.2.2.2', 80)])
        with mock.patch.object(pt.messagebox, 'showwarning') as warning:
            self.assertFalse(self.app.toggle_upstream())
            warning.assert_called_once()

    def _select(self, index):
        self.app.pool_box.selection_set(str(index))
        self.app._update_upstream_actions()

    def test_pause_persists_all_rows_and_shifts_shared_new_connections(self):
        self.app._relay = mock.Mock(upstream=('1.1.1.1', 80))
        self.app._socks = mock.Mock(upstream=('1.1.1.1', 80))
        self._select(0)
        self.assertTrue(self.app.toggle_upstream())
        self.assertEqual(self.app._pool_addrs(), [('2.2.2.2', 80)])
        self.assertEqual(self.app._pool_addrs(include_disabled=True), [('1.1.1.1', 80), ('2.2.2.2', 80)])
        self.assertEqual(self.app._pool.order(), [('2.2.2.2', 80)])
        self.assertEqual(self.app._relay.upstream, ('2.2.2.2', 80))
        self.assertEqual(self.app._socks.upstream, ('2.2.2.2', 80))
        self.app._relay.stop.assert_not_called()
        saved = next(p for p in self._read_disk()['presets'] if p['name'] == 'm')
        self.assertEqual(len(saved['upstreams']), 2)
        self.assertFalse(saved['upstream_options']['1.1.1.1:80']['enabled'])
        self.assertEqual(self.app.pool_box.set('0', 'test'), '已停用')
        self.assertEqual(self.app.toggle_up_btn['text'], '启用')

    def test_last_enabled_cannot_be_paused_or_removed(self):
        self._select(0)
        self.assertTrue(self.app.toggle_upstream())
        before = copy.deepcopy(self.app.presets)
        self._select(1)
        with mock.patch.object(pt.messagebox, 'showinfo'):
            self.assertFalse(self.app.toggle_upstream())
            self.app.remove_upstream()
        self.assertEqual(self.app.presets, before)
        self.assertEqual(self.app._pool_addrs(), [('2.2.2.2', 80)])

    def test_edit_preserves_disabled_state_and_moves_note_to_new_address(self):
        self._select(0)
        self.app.toggle_upstream()
        self.assertTrue(self.app._edit_upstream_at(0, 'proxy.local', '008080', '办公备用链路'))
        preset = self.app._current_preset()
        self.assertEqual(preset['upstreams'][0], ['proxy.local', '8080'])
        self.assertEqual(preset['upstream_options']['proxy.local:8080'],
                         {'name': '办公备用链路', 'enabled': False})
        self.assertNotIn('1.1.1.1:80', preset['upstream_options'])
        self.assertEqual(self.app._pool_addrs(), [('2.2.2.2', 80)])
        self.assertEqual(self.app.pool_box.set('0', 'name'), '办公备用链路')

    def test_duplicate_edit_and_overlong_note_are_rejected(self):
        before = copy.deepcopy(self.app.presets)
        with mock.patch.object(pt.messagebox, 'showwarning'):
            self.assertFalse(self.app._edit_upstream_at(0, '2.2.2.2', '0080', 'duplicate'))
            self.assertFalse(self.app._edit_upstream_at(0, '1.1.1.1', '80', '长' * 17))
        self.assertEqual(self.app.presets, before)

    def test_save_failure_rolls_back_address_note_pause_and_pool(self):
        before = copy.deepcopy(self.app.presets)
        disk_before = self._read_disk()
        self._select(0)
        with mock.patch.object(pt, 'save_config', side_effect=PermissionError('read only')):
            with mock.patch.object(pt.messagebox, 'showerror'):
                self.assertFalse(self.app._edit_upstream_at(0, 'new.proxy', '8080', 'new note'))
                self.assertFalse(self.app.toggle_upstream())
        self.assertEqual(self.app.presets, before)
        self.assertEqual(self._read_disk(), disk_before)
        self.assertEqual(set(self.app._pool.order()), {('1.1.1.1', 80), ('2.2.2.2', 80)})
        self.assertEqual(self.app.pool_box.set('0', 'addr'), '1.1.1.1:80')

    def test_draft_edit_and_pause_do_not_write_or_change_live_pool(self):
        disk_before = self._read_disk()
        live_before = set(self.app._pool.order())
        self.app._enter_add_mode('multi', 'draft')
        for address in ('a:80', 'b:80'):
            self.app.add_var.set(address)
            self.app.add_upstream()
        self.assertTrue(self.app._edit_upstream_at(0, 'a', '80', '草稿备注'))
        self._select(0)
        self.assertTrue(self.app.toggle_upstream())
        self.assertEqual(self._read_disk(), disk_before)
        self.assertEqual(set(self.app._pool.order()), live_before)
        self.assertFalse(pt._upstream_options(self.app._draft, 'a', 80)['enabled'])
        self.app.on_add_or_save()
        saved = next(p for p in self._read_disk()['presets'] if p['name'] == 'draft')
        self.assertEqual(saved['upstream_options']['a:80'], {'name': '草稿备注', 'enabled': False})
        self.assertEqual(len(saved['upstreams']), 2)

    def test_row_statistics_and_test_result_use_full_row_indexes(self):
        self._select(0)
        self.app.toggle_upstream()
        self.app._entry = mock.Mock(listen_port=self.app.entry_port)
        self.app._entry.snapshot.return_value = (5, {('1.1.1.1', 80): (3, 100, 100),
                                                    ('2.2.2.2', 80): (2, 200, 200)})
        self.app._entry_status_text()
        self.assertEqual(self.app.pool_box.set('0', 'conns'), '3')
        self.assertEqual(self.app.pool_box.set('1', 'conns'), '2')
        self.app._show_test_result(self.app._test_gen, '2.2.2.2:80', True, 42, 1)
        self.assertEqual(self.app.pool_box.set('0', 'test'), '已停用')
        self.assertEqual(self.app.pool_box.set('1', 'test'), '✓ 42 ms')

    def test_draft_cannot_be_saved_with_only_disabled_nodes(self):
        before = self._read_disk()
        self.app._enter_add_mode('multi', 'maintenance')
        for address in ('a:80', 'b:80'):
            self.app.add_var.set(address)
            self.app.add_upstream()
        self._select(0)
        self.app.toggle_upstream()
        self._select(1)
        self.app.remove_upstream()  # Draft may be incomplete while being edited.
        with mock.patch.object(pt.messagebox, 'showwarning') as warning:
            self.app.on_add_or_save()
        warning.assert_called_once()
        self.assertTrue(self.app._adding)
        self.assertEqual(self._read_disk(), before)

    def test_disabled_single_preset_can_be_reenabled(self):
        self.app.preset_var.set('s')
        preset = self.app._current_preset()
        pt._set_upstream_options(preset, '1.1.1.1', 80, '维护', False)
        self.app._apply_form()
        self.app._refresh_pool_box()
        self.assertEqual(self.app.ip_var.get(), '1.1.1.1')
        self.assertEqual(self.app.single_enable_btn.winfo_manager(), 'pack')
        self.assertTrue(self.app.toggle_upstream())
        self.assertEqual(self.app._pool_addrs(), [('1.1.1.1', 80)])
        self.assertEqual(self.app.single_enable_btn.winfo_manager(), '')

    def test_note_dialog_shows_complete_sixteen_character_name(self):
        note = '这是一个恰好十六个汉字的测试备注'
        self.assertEqual(len(note), 16)
        self.assertTrue(self.app._edit_upstream_at(0, '1.1.1.1', '80', note))
        self._select(0)
        self.assertTrue(self.app.pool_box.set('0', 'name').endswith('…'))
        self.app.edit_upstream()
        import tkinter as tk
        from tkinter import ttk
        window = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)][-1]
        entries = [child for child in window.winfo_children() if isinstance(child, ttk.Entry)]
        self.assertEqual(entries[2].get(), note)
        window.destroy()


class TestPauseKeepsExistingTunnel(unittest.TestCase):
    def test_pool_change_routes_new_connections_without_closing_old_one(self):
        first, second = EchoServer(), EchoServer()
        pool = pt.UpstreamPool([first.address, second.address])
        server = pt.HttpServer(0, pool)
        server.start()
        try:
            address = server._srv.getsockname()
            with socket.create_connection(address, timeout=5) as existing:
                existing.sendall(b'before')
                self.assertEqual(existing.recv(6), b'before')
                pool.set([second.address])
                existing.sendall(b'after')
                self.assertEqual(existing.recv(5), b'after')
                with socket.create_connection(address, timeout=5) as fresh:
                    fresh.sendall(b'new')
                    self.assertEqual(fresh.recv(3), b'new')
                    _, stats = server.snapshot()
                    self.assertGreaterEqual(stats[first.address][0], 1)
                    self.assertGreaterEqual(stats[second.address][0], 1)
        finally:
            server.stop()
            first.close()
            second.close()
        self.assertTrue(eventually(lambda: server.active_conns == 0))

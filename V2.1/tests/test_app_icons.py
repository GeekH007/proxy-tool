"""External application icons and tray ownership, without adding a tray icon."""

import ctypes
import os
import shutil
import struct
import sys
import tempfile
import unittest
from contextlib import ExitStack, chdir
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy_tool as pt
from tests.test_core import _AppTest


def png_size(payload):
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("Icon asset is not a PNG")
    return struct.unpack(">II", payload[16:24])


class TestExternalIcons(unittest.TestCase):
    def test_all_requested_png_sizes_are_in_script_assets_directory(self):
        self.assertEqual(pt._ASSET_DIR, os.path.join(pt._BASE_DIR, "assets"))
        self.assertEqual(set(pt._ICON_SIZES), {16, 32, 48, 256})
        for size in pt._ICON_SIZES:
            with self.subTest(size=size):
                with open(os.path.join(pt._ASSET_DIR, f"lan-proxy-{size}.png"), "rb") as src:
                    self.assertEqual(png_size(src.read()), (size, size))

    def test_intermediate_dpi_uses_larger_source_and_preserves_full_handle(self):
        handle = 0x100000123 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x12345678
        received = []

        def create_icon(data, length, is_icon, version, width, height, flags):
            # Copy while the source buffer is alive; never inspect a freed pointer.
            received.append((ctypes.string_at(data, length), is_icon, version,
                             width, height, flags))
            return handle

        with mock.patch.object(pt.user32, "GetSystemMetrics", return_value=24), \
                mock.patch.object(pt.user32, "CreateIconFromResourceEx",
                                  side_effect=create_icon), \
                mock.patch.object(pt.user32, "LoadIconW") as fallback:
            self.assertEqual(pt._load_tray_icon(), (handle, True))
        self.assertEqual(len(received), 1)
        payload, is_icon, version, width, height, flags = received[0]
        self.assertEqual(png_size(payload), (32, 32))
        self.assertTrue(is_icon)
        self.assertEqual((version, width, height, flags), (0x00030000, 24, 24, 0))
        fallback.assert_not_called()

    def test_native_creation_failure_returns_shared_system_icon(self):
        with mock.patch.object(pt.user32, "GetSystemMetrics", return_value=16), \
                mock.patch.object(pt.user32, "CreateIconFromResourceEx", return_value=0), \
                mock.patch.object(pt.user32, "LoadIconW", return_value=123) as fallback:
            self.assertEqual(pt._load_tray_icon(), (123, False))
        fallback.assert_called_once_with(None, 32512)

    def test_missing_asset_directory_returns_shared_system_icon(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(pt, "_ASSET_DIR", os.path.join(temporary, "missing")), \
                mock.patch.object(pt.user32, "GetSystemMetrics", return_value=16), \
                mock.patch.object(pt.user32, "LoadIconW", return_value=123) as fallback:
            self.assertEqual(pt._load_tray_icon(), (123, False))
        fallback.assert_called_once_with(None, 32512)

    def test_invalid_assets_return_shared_system_icon(self):
        with tempfile.TemporaryDirectory() as temporary:
            for size in pt._ICON_SIZES:
                with open(os.path.join(temporary, f"lan-proxy-{size}.png"), "wb") as dst:
                    dst.write(b"not a valid PNG")
            with mock.patch.object(pt, "_ASSET_DIR", temporary), \
                    mock.patch.object(pt.user32, "GetSystemMetrics", return_value=16), \
                    mock.patch.object(pt.user32, "LoadIconW", return_value=123) as fallback:
                self.assertEqual(pt._load_tray_icon(), (123, False))
            fallback.assert_called_once_with(None, 32512)

    def test_missing_and_corrupt_preferred_assets_try_another_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            shutil.copyfile(os.path.join(pt._ASSET_DIR, "lan-proxy-48.png"),
                            os.path.join(temporary, "lan-proxy-48.png"))
            with open(os.path.join(temporary, "lan-proxy-32.png"), "wb") as dst:
                dst.write(b"not a valid PNG")
            with mock.patch.object(pt, "_ASSET_DIR", temporary), \
                    mock.patch.object(pt.user32, "GetSystemMetrics", return_value=16):
                handle, owned = pt._load_tray_icon()
                try:
                    self.assertTrue(handle)
                    self.assertTrue(owned, "Valid 48px icon should survive missing/corrupt alternatives")
                finally:
                    if owned and handle:
                        self.assertTrue(pt.user32.DestroyIcon(handle))

    def test_loading_does_not_depend_on_current_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary, chdir(temporary), \
                mock.patch.object(pt.user32, "GetSystemMetrics", return_value=16):
            handle, owned = pt._load_tray_icon()
            try:
                self.assertTrue(handle)
                self.assertTrue(owned, "Icon should load from script assets, independent of cwd")
            finally:
                if owned and handle:
                    self.assertTrue(pt.user32.DestroyIcon(handle))

    def test_windows_can_decode_every_asset_size(self):
        # A real native decode catches unsupported PNG encodings; no window or
        # notification-area entry is created. Isolate each size so a corrupt
        # asset cannot silently pass by falling back to another valid size.
        for size in (16, 32, 48, 256):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temporary:
                name = f"lan-proxy-{size}.png"
                shutil.copyfile(os.path.join(pt._ASSET_DIR, name), os.path.join(temporary, name))
                with mock.patch.object(pt, "_ASSET_DIR", temporary), \
                        mock.patch.object(pt.user32, "GetSystemMetrics", return_value=size):
                    handle, owned = pt._load_tray_icon()
                    try:
                        self.assertTrue(handle)
                        self.assertTrue(owned, "Custom PNG failed; loader used the fallback")
                    finally:
                        if owned and handle:
                            self.assertTrue(pt.user32.DestroyIcon(handle))


class TestTrayIconLifetime(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.icon = 0x100000123 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x12345678
        self.window = self.icon + 1
        self.instance = 0x777
        self.taskbar_message = 0xC001
        self.api = {}
        values = {
            "RegisterClassW": 1,
            "RegisterWindowMessageW": self.taskbar_message,
            "CreateWindowExW": self.window,
            "DestroyWindow": True,
            "UnregisterClassW": True,
            "DestroyIcon": True,
            "DefWindowProcW": 0,
        }
        for name, result in values.items():
            self.api[name] = self.stack.enter_context(
                mock.patch.object(pt.user32, name, return_value=result))
        self.stack.enter_context(mock.patch.object(pt.kernel32, "GetModuleHandleW",
                                                  return_value=self.instance))
        self.notify = self.stack.enter_context(
            mock.patch.object(pt.shell32, "Shell_NotifyIconW", return_value=True))
        self.load_icon = self.stack.enter_context(
            mock.patch.object(pt, "_load_tray_icon", return_value=(self.icon, True)))

    def make_tray(self, owned=True):
        self.load_icon.return_value = (self.icon, owned)
        tray = pt.TrayIcon(mock.Mock(), mock.Mock())
        self.addCleanup(tray.close)
        return tray

    def test_remove_hides_and_show_reuses_owned_icon(self):
        tray = self.make_tray()
        self.assertEqual(tray.hwnd, self.window)
        self.assertEqual(tray._nid.hIcon, self.icon)
        self.assertTrue(tray.show("ready"))
        tray.remove()
        self.api["DestroyIcon"].assert_not_called()
        self.api["DestroyWindow"].assert_not_called()
        self.assertTrue(tray.show("ready"))
        self.assertEqual([call.args[0] for call in self.notify.call_args_list],
                         [pt.NIM_ADD, pt.NIM_DELETE, pt.NIM_ADD])
        self.load_icon.assert_called_once_with()

    def test_explorer_restart_reuses_existing_icon(self):
        tray = self.make_tray()
        tray.show("ready")
        self.notify.reset_mock()
        tray._dispatch(self.window, self.taskbar_message, 0, 0)
        self.assertEqual(self.notify.call_args.args[0], pt.NIM_ADD)
        self.assertEqual(tray._nid.hIcon, self.icon)
        self.load_icon.assert_called_once_with()
        self.api["DestroyIcon"].assert_not_called()

    def test_close_deletes_window_before_destroying_owned_icon_and_is_final(self):
        tray = self.make_tray()
        tray.show("ready")
        sequence = mock.Mock()
        sequence.attach_mock(self.notify, "notify")
        for name in ("DestroyWindow", "UnregisterClassW", "DestroyIcon"):
            sequence.attach_mock(self.api[name], name)
        tray.close()
        tray.close()
        self.assertEqual([call[0] for call in sequence.mock_calls],
                         ["notify", "DestroyWindow", "UnregisterClassW", "DestroyIcon"])
        self.assertEqual(self.notify.call_args.args[0], pt.NIM_DELETE)
        self.api["DestroyWindow"].assert_called_once_with(self.window)
        self.api["UnregisterClassW"].assert_called_once()
        self.api["DestroyIcon"].assert_called_once_with(self.icon)
        self.notify.reset_mock()
        self.assertFalse(tray.show("must stay closed"))
        tray._dispatch(self.window, self.taskbar_message, 0, 0)
        self.notify.assert_not_called()

    def test_close_after_remove_still_frees_owned_icon(self):
        tray = self.make_tray()
        tray.show("ready")
        tray.remove()
        self.notify.reset_mock()
        tray.close()
        self.notify.assert_not_called()
        self.api["DestroyIcon"].assert_called_once_with(self.icon)

    def test_shared_fallback_icon_is_never_destroyed(self):
        tray = self.make_tray(owned=False)
        tray.show("ready")
        tray.close()
        self.api["DestroyWindow"].assert_called_once_with(self.window)
        self.api["UnregisterClassW"].assert_called_once()
        self.api["DestroyIcon"].assert_not_called()

    def test_window_creation_failure_unregisters_class(self):
        self.api["CreateWindowExW"].return_value = 0
        with self.assertRaises(OSError):
            pt.TrayIcon(mock.Mock(), mock.Mock())
        self.api["UnregisterClassW"].assert_called_once()
        self.notify.assert_not_called()


class TestWindowIcons(_AppTest):
    def test_tk_keeps_all_window_icon_sizes_and_header_logo_alive(self):
        self.assertEqual({(image.width(), image.height())
                          for image in self.app._window_icons},
                         {(16, 16), (32, 32), (48, 48), (256, 256)})
        self.assertEqual((self.app._logo_image.width(), self.app._logo_image.height()),
                         (48, 48))


class TestWindowIconsFromOtherDirectory(TestWindowIcons):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(chdir(temporary))
        super().setUp()


class _WindowIconsWithAssetFixture(_AppTest):
    """Build the real Tk app against temporary assets; never alter shipped files."""

    VALID_SIZES = ()
    CORRUPT_SIZES = ()

    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        for size in self.VALID_SIZES:
            name = f"lan-proxy-{size}.png"
            shutil.copyfile(os.path.join(pt._ASSET_DIR, name), os.path.join(temporary, name))
        for size in self.CORRUPT_SIZES:
            with open(os.path.join(temporary, f"lan-proxy-{size}.png"), "wb") as dst:
                dst.write(b"not a valid PNG")
        self.enterContext(mock.patch.object(pt, "_ASSET_DIR", temporary))
        super().setUp()

    def assert_text_logo_fallback(self):
        self.assertEqual(self.app._window_icons, [])
        self.assertIsNone(self.app._logo_image)
        pending = [self.root]
        labels = []
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, pt.ttk.Label):
                labels.append(widget.cget("text"))
        self.assertIn("LP", labels)


class TestWindowIconsWithoutPreferredSize(_WindowIconsWithAssetFixture):
    VALID_SIZES = (16, 32, 256)

    def test_missing_48px_logo_uses_nearest_available_size(self):
        self.assertEqual({image.width() for image in self.app._window_icons}, {16, 32, 256})
        self.assertEqual((self.app._logo_image.width(), self.app._logo_image.height()), (32, 32))


class TestWindowIconsWithPartialDamage(_WindowIconsWithAssetFixture):
    VALID_SIZES = (256,)
    CORRUPT_SIZES = (32, 48)

    def test_invalid_files_are_skipped_and_large_header_logo_is_reduced(self):
        self.assertEqual({image.width() for image in self.app._window_icons}, {256})
        self.assertGreater(self.app._logo_image.width(), 0)
        self.assertLessEqual(self.app._logo_image.width(), 48)
        self.assertEqual(self.app._logo_image.width(), self.app._logo_image.height())


class TestWindowIconsAllMissing(_WindowIconsWithAssetFixture):
    def test_missing_icons_preserve_app_startup_and_text_logo(self):
        self.assert_text_logo_fallback()


class TestWindowIconsAllCorrupt(_WindowIconsWithAssetFixture):
    CORRUPT_SIZES = (16, 32, 48, 256)

    def test_corrupt_icons_preserve_app_startup_and_text_logo(self):
        self.assert_text_logo_fallback()


if __name__ == "__main__":
    unittest.main()

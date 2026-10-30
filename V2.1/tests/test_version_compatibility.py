from unittest import mock

import proxy_tool as pt


def test_existing_autostart_survives_version_change_and_remains_switchable():
    old_name = '系统代理工具 V2.0'
    values = {old_name: 'old startup command', 'UnrelatedApp': 'keep'}

    def query(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], pt.winreg.REG_SZ

    def delete(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    def write(_key, name, _reserved, _kind, command):
        values[name] = command

    with mock.patch.object(pt.winreg, 'OpenKey', return_value=mock.MagicMock()), \
            mock.patch.object(pt.winreg, 'QueryValueEx', side_effect=query), \
            mock.patch.object(pt.winreg, 'DeleteValue', side_effect=delete), \
            mock.patch.object(pt.winreg, 'SetValueEx', side_effect=write), \
            mock.patch.object(pt, '_autostart_exe', return_value=r'C:\Python\pythonw.exe'):
        assert pt.get_autostart()
        pt.set_autostart(False)
        assert not pt.get_autostart()
        pt.set_autostart(True)
        assert pt.get_autostart()
        assert set(values) == {old_name, 'UnrelatedApp'}
        assert values['UnrelatedApp'] == 'keep'
        assert 'proxy_tool.py' in values[old_name]


def test_existing_v20_window_is_found_when_new_title_is_absent():
    kernel, user = mock.Mock(), mock.Mock()
    kernel.CreateMutexW.return_value = 99
    user.FindWindowW.side_effect = [0, 1234]
    with mock.patch.object(pt, 'kernel32', kernel), mock.patch.object(pt, 'user32', user), \
            mock.patch.object(pt.ctypes, 'get_last_error', return_value=183):
        assert pt.acquire_single_instance() is False
    assert user.FindWindowW.call_args_list == [
        mock.call(None, pt.APP_TITLE), mock.call(None, '系统代理工具 V2.0')]
    user.ShowWindow.assert_called_once_with(1234, 9)
    kernel.CloseHandle.assert_called_once_with(99)

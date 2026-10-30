"""Safe visual preview: example data, temporary config, no system proxy changes."""
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_tool as pt

with tempfile.TemporaryDirectory() as folder:
    config = str(Path(folder) / 'config.json')
    Path(config).write_text(json.dumps({
        'current': '办公网络 · 双上游', 'share': False, 'presets': [
            {'name': '办公网络 · 双上游', 'type': 'multi',
             'upstreams': [['192.168.1.73', '7890'], ['192.168.1.91', '7890']]},
            {'name': '单代理', 'type': 'single', 'upstreams': [['192.168.1.73', '7890']]},
        ]}, ensure_ascii=False), encoding='utf-8')
    patches = [
        mock.patch.object(pt, 'CONFIG_FILE', config),
        mock.patch.object(pt, 'SETTINGS_FILE', str(Path(folder) / 'settings.json')),
        mock.patch.object(pt, 'OLD_SETTINGS_FILE', str(Path(folder) / 'missing.json')),
        mock.patch.object(pt, 'get_system_proxy', return_value=(False, '')),
        mock.patch.object(pt, 'get_autostart', return_value=False),
        mock.patch.object(pt, 'set_autostart'),
        mock.patch.object(pt, '_primary_lan_ip', return_value='192.168.1.130'),
        mock.patch.object(pt, 'TrayIcon'),
        mock.patch.object(pt.ProxyApp, '_restore_entry'),
        mock.patch.object(pt.ProxyApp, '_start_health_monitor'),
        mock.patch.object(pt.ProxyApp, '_auto_refresh'),
        mock.patch.object(pt.ProxyApp, 'apply'),
        mock.patch.object(pt.ProxyApp, 'toggle_share'),
        mock.patch.object(pt.ProxyApp, 'test_connectivity'),
    ]
    for patch in patches:
        patch.start()
    root = tk.Tk()
    app = pt.ProxyApp(root)
    root.title(f'{pt.APP_VERSION} · 界面预览（示例数据）')
    root.protocol('WM_DELETE_WINDOW', root.destroy)
    if '--single' in sys.argv:
        app.preset_var.set('单代理')
        app._apply_form()
    else:
        app.status_var.set('● 已开启  127.0.0.1:7890 · 2 台上游')
        app.btn.configure(text='关闭代理', style='On.TButton')
        app.metric_down.set('8.4 MB/s')
        app.metric_up.set('256 KB/s')
        app.metric_conns.set('12')
        app.pool_box.item('0', values=('办公室电脑', '192.168.1.73:7890', '5.2 MB/s', '160 KB/s', '7', '✓ 126 ms'))
        app.pool_box.item('1', values=('备用电脑', '192.168.1.91:7890', '3.2 MB/s', '96 KB/s', '5', '✓ 158 ms'))
        app.health_summary_var.set('HTTPS：2 正常')
    root.geometry('+180+80')
    def show_preview():
        root.withdraw()
        root.deiconify()
        root.lift()
    root.after(250, show_preview)
    root.after(240000, root.destroy)
    try:
        root.mainloop()
    finally:
        for patch in reversed(patches):
            patch.stop()

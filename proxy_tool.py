# -*- coding: utf-8 -*-
"""Windows 系统代理小工具：小窗口控制代理开关，设置 IP 和端口，自动记住配置。"""
import ctypes
from ctypes import wintypes
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.request
import winreg

PROXY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or _BASE_DIR, "ProxyTool")
CONFIG_FILE = os.path.join(CONFIG_DIR, "proxy_config.json")  # 默认配置位置（可在设置里改）
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")    # 应用设置（固定放这，不跟随配置路径）
LEGACY_CONFIG_FILE = os.path.join(_BASE_DIR, "proxy_config.json")  # 旧版配置位置，读到即迁移
DEFAULT_IP = "127.0.0.1"
DEFAULT_PORT = "7890"
DEFAULT_SHARE_PORT = 7891
APP_TITLE = "系统代理工具"
MUTEX_NAME = "MyXm_ProxyTool_SingleInstance"
TEST_URL = "http://www.gstatic.com/generate_204"
REFRESH_INTERVAL_MS = 2000
ACTIVE_WINDOW_SEC = 600   # “近10分钟活跃设备”的统计窗口
DEVICE_TTL_SEC = 3600     # 设备记录保留时长，超时清理

_mutex_handle = None  # 持有互斥量句柄防止被回收，进程退出时系统自动释放

# 单实例/托盘/前台窗口用的 Win32 句柄；use_last_error 让 WinError 带上真实错误码
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------- 系统代理（注册表） ----------

def _refresh():
    """广播设置已更改，让浏览器等立即生效"""
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
    wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)


def set_system_proxy(server: str, override: str = "localhost;127.*;<local>"):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        try:  # 若之前开了"使用设置脚本"(PAC)，需关掉，否则手动代理不生效
            winreg.DeleteValue(key, "AutoConfigURL")
        except FileNotFoundError:
            pass
    _refresh()


def disable_system_proxy():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    _refresh()


def get_system_proxy():
    """返回 (是否开启, 代理服务器字符串)，以注册表实际状态为准"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROXY_KEY) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
            return bool(enable), server
    except OSError:
        return False, ""


# ---------- 开机自启（HKCU Run 键，用 pythonw 全路径直接启动脚本） ----------

def get_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_TITLE)
            return True
    except OSError:
        return False


def _autostart_exe() -> str:
    """自启命令用的 pythonw：优先取当前解释器同目录的，PATH 里第一个 pythonw
    未必是跑本程序的这个，之后卸载/改动 PATH 命中的那个 Python 自启就悄悄失效"""
    sibling = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(sibling):
        return sibling
    exe = shutil.which("pythonw") or shutil.which("pyw")
    if not exe:
        raise OSError("未找到 pythonw，请先安装 Python 并勾选加入 PATH")
    return exe


def set_autostart(enable: bool):
    if enable:
        script = os.path.join(_BASE_DIR, "proxy_tool.py")
        cmd = f'"{_autostart_exe()}" "{script}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_TITLE, 0, winreg.REG_SZ, cmd)
    else:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, APP_TITLE)
            except FileNotFoundError:
                pass


# ---------- 单实例保护 ----------

def acquire_single_instance() -> bool:
    """用命名互斥量防止重复启动；已有实例时把它的窗口拉到前台并返回 False"""
    global _mutex_handle
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS：已有实例在跑
        kernel32.CloseHandle(handle)
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            if not user32.SetForegroundWindow(hwnd):  # 抢不到前台（被系统拦下）
                flash_window(hwnd)
        return False
    _mutex_handle = handle
    return True


# ---------- 前台窗口兜底：抢不到就闪任务栏 ----------

FLASHW_ALL = 0x3        # 闪标题栏 + 任务栏按钮
FLASHW_TIMERNOFG = 0xC  # 一直闪到窗口到前台为止


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD)]


def flash_window(hwnd) -> bool:
    """SetForegroundWindow 被系统拦下时，闪任务栏按钮提醒用户窗口在等它"""
    info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd,
                       FLASHW_ALL | FLASHW_TIMERNOFG, 5, 0)
    return bool(user32.FlashWindowEx(ctypes.byref(info)))


# ---------- 托盘图标（ctypes 自实现，不引第三方）。
# tkinter 拿不到窗口过程，所以自建一个隐藏窗口接收托盘回调和 TaskbarCreated
# 广播；消息由 Tk 主循环的消息泵分发，回调必然落在主线程——但那是 WndProc
# 重入点，里面绝不能碰 tkinter（重入 Tcl 会 Fatal Error 秒崩进程），
# 只能入队或做纯 Win32 调用。 ----------

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
WM_TRAY_CALLBACK = 0x8000 + 1  # WM_APP+1：托盘鼠标事件回调消息
WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x202, 0x203
WM_RBUTTONUP, WM_CONTEXTMENU = 0x205, 0x7B


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", wintypes.INT), ("cbWndExtra", wintypes.INT),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]
user32.DefWindowProcW.restype = wintypes.LPARAM
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.LoadIconW.restype = ctypes.c_void_p
user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]


class TrayIcon:
    """系统托盘图标：左键/双击回调 on_restore，右键回调 on_menu。
    两个回调都运行在 Tk 消息泵的 WndProc 重入点里，**绝不能在里面直接调
    tkinter**（重入 Tcl 会 Fatal Python Error 秒退进程），只能入队或做纯 Win32 操作。"""

    def __init__(self, on_restore, on_menu):
        self._on_restore = on_restore
        self._on_menu = on_menu
        self._added = False
        self._tip = ""  # 当前 tooltip，show 用它判断是否真要发 NIM_MODIFY
        self._wndproc = _WNDPROC(self._dispatch)  # 必须保存引用，被回收会崩
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = f"ProxyToolTray{id(self):x}"  # 每实例一个类名，避免重注册冲突
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        # 必须在建窗前注册：WndProc 从 WM_NCCREATE 起就会被回调
        self._msg_taskbar = user32.RegisterWindowMessageW("TaskbarCreated")
        # 用普通隐藏窗口而不是 message-only：后者收不到广播，而 explorer
        # 重启时的 TaskbarCreated 广播正是图标被清掉后重新添加的唯一信号
        self._hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "", 0, 0, 0, 0, 0,
            None, None, wc.hInstance, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid = _NOTIFYICONDATAW()
        self._nid.cbSize = ctypes.sizeof(self._nid)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._nid.uCallbackMessage = WM_TRAY_CALLBACK
        self._nid.hIcon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION

    @property
    def hwnd(self):
        return self._hwnd

    def _dispatch(self, hwnd, msg, wp, lp):
        # 整体兜底：消息处理里任何异常都不能抛出回调——返回 0 会让
        # CreateWindowExW 之类的调用中途失败（WM_NCCREATE 必须走 DefWindowProc）
        try:
            if msg == WM_TRAY_CALLBACK:
                if lp in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._on_restore()
                elif lp in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._on_menu()
            elif self._msg_taskbar and msg == self._msg_taskbar:
                # explorer 重启过，图标被系统清了：重新添加（纯 Win32，可在回调里做）
                self._added = False
                self.show(self._tip)
        except Exception:
            traceback.print_exc()  # 回调出错别拖垮消息处理
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def show(self, tip: str) -> bool:
        """首次调用添加图标，之后 tooltip 有变化才发 NIM_MODIFY"""
        tip = tip[:127]  # szTip 容量 128，留 1 个 NUL
        if self._added and self._tip == tip:
            return True
        self._tip = tip
        self._nid.szTip = tip
        if not self._added:
            # 失败（比如托盘还没就绪）不抛，下次 show 会自动重试添加
            self._added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)))
        elif shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid)):
            pass
        else:
            # NIM_MODIFY 失败说明图标已经不在了（双保险，正常走 TaskbarCreated）
            self._added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)))
        return self._added

    def balloon(self, title: str, text: str):
        """弹一条系统通知（Win11 下显示为 toast）"""
        if not self._added:
            return
        nid = self._nid
        flags = nid.uFlags
        nid.uFlags = flags | NIF_INFO
        nid.szInfo = text
        nid.szInfoTitle = title
        nid.dwInfoFlags = 1  # NIIF_INFO：带信息图标
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        nid.uFlags = flags  # 撤掉 NIF_INFO，之后改 tooltip 不会重复弹气泡

    def remove(self):
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._added = False


# ---------- 局域网共享（透明 TCP 转发到上游代理） ----------

def _primary_lan_ip():
    """本机对外的局域网 IP（其他设备应填这个）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _rule_name(port: int) -> str:
    return f"ProxyTool LAN Share {port}"


def firewall_rule_exists(port: int) -> bool:
    try:
        # timeout：防火墙服务坏了会让 netsh 卡死，不设上限会把共享流程永久卡住；
        # CREATE_NO_WINDOW：pythonw 没控制台，不加这个 netsh 会自己弹黑框闪一下
        out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                              f"name={_rule_name(port)}"], capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
    except subprocess.TimeoutExpired:
        return False  # 卡死按“没规则”处理：让用户重开一次，也别锁死 _fw_pending
    return out.returncode == 0


def add_firewall_rule_elevated(port: int) -> bool:
    """弹 UAC 提权加放行规则，只对本机所在子网开放"""
    params = (f'advfirewall firewall add rule name="{_rule_name(port)}" '
              f"dir=in action=allow protocol=TCP localport={port} remoteip=localsubnet")
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0) > 32


class RelayServer:
    """把 0.0.0.0:listen_port 收到的 TCP 连接原样搬给上游代理（不解协议）"""

    def __init__(self, listen_port: int, upstream: tuple):
        self.listen_port = listen_port
        self.upstream = upstream  # (ip, port)，可随时替换，对新连接生效
        self._srv = None
        self._on = False
        self._lock = threading.Lock()
        self.active_conns = 0  # 当前 TCP 连接总数
        self.devices = {}      # ip -> {"conns": 当前连接数, "last": 最后活跃时刻}
        self._live = set()     # 活跃连接对 (client, up)，stop 时强制断开用

    def start(self):
        srv = socket.socket()
        try:
            srv.bind(("0.0.0.0", self.listen_port))
            srv.listen(16)
        except OSError:
            srv.close()
            raise
        self._srv, self._on = srv, True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self._on = False
        if self._srv:
            self._srv.close()
            self._srv = None
        # 已建立的连接也要掐断，否则客户端的长连接（keep-alive/HTTP2）会一直活着；
        # _live 里还有正在拨上游的半截连接（单元素元组），它们的客户端同样要掐
        with self._lock:
            pairs = list(self._live)
        for pair in pairs:
            for s in pair:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

    def _accept_loop(self):
        srv = self._srv  # 局部快照：stop() 会把 _srv 置 None，别在循环里解引用它
        while self._on and srv is not None:
            try:
                client, _ = srv.accept()
            except OSError:
                break  # stop() 关闭监听后会走到这
            threading.Thread(target=self._pump, args=(client,), daemon=True).start()

    def _pump(self, client):
        try:
            ip = client.getpeername()[0]
        except OSError:
            ip = "未知"
        with self._lock:
            dev = self.devices.setdefault(ip, {"conns": 0, "last": time.monotonic()})
            dev["conns"] += 1
            self.active_conns += 1
        try:
            t_pending = (client,)
            with self._lock:
                self._live.add(t_pending)  # 拨上游前就登记，stop() 才掐得到在途连接
            try:
                up = socket.create_connection(self.upstream, timeout=5)
            except OSError:
                client.close()  # 上游连不上，别让客户端 socket 干等
                return
            finally:
                with self._lock:
                    self._live.discard(t_pending)
            up.settimeout(None)  # 否则上游 keep-alive 空闲会被 5 秒超时打断
            pair = (client, up)
            with self._lock:
                self._live.add(pair)
                stopped = not self._on  # stop() 若恰在拨号期间跑过，这条是漏网的
            if stopped:
                client.close()
                up.close()
                return
            t1 = threading.Thread(target=self._pipe, args=(client, up), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(up, client), daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()
            with self._lock:
                self._live.discard(pair)
            client.close(); up.close()
        finally:
            with self._lock:
                dev["conns"] -= 1
                dev["last"] = time.monotonic()
                self.active_conns -= 1

    def snapshot(self):
        """界面轮询用：返回 (当前连接总数, [(ip, 当前连接数, 距最后活跃秒数), ...])"""
        now = time.monotonic()
        with self._lock:
            if self.devices:
                self.devices = {ip: d for ip, d in self.devices.items()
                                if d["conns"] > 0 or now - d["last"] < DEVICE_TTL_SEC}
            devs = sorted(((ip, d["conns"], int(now - d["last"]))
                           for ip, d in self.devices.items()),
                          key=lambda x: -x[1])
            return self.active_conns, devs

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)  # 半关，让对面把剩余数据收完
            except OSError:
                pass


# ---------- 应用设置（共享端口、配置文件路径） ----------

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    port = data.get("share_port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        port = DEFAULT_SHARE_PORT
    path = data.get("config_path")
    path = str(path).strip() if isinstance(path, str) else ""
    return {"share_port": port, "config_path": path}


def save_settings(settings: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)  # 原子替换，写一半断电也不会损坏


# ---------- 配置读写（多套预设，兼容旧版单配置格式） ----------

def _normalize(data) -> dict:
    """把任意版本的配置统一成 {"current": 名称, "share": bool, "presets": [...]}"""
    if not isinstance(data, dict):
        data = {}
    presets = [{"name": str(p.get("name") or "未命名"),
                "ip": str(p.get("ip") or DEFAULT_IP),
                "port": str(p.get("port") or DEFAULT_PORT)}
               for p in (data.get("presets") or []) if isinstance(p, dict)]
    if not presets:  # 旧版 {ip, port} 单配置格式，或文件损坏
        presets = [{"name": "默认", "ip": str(data.get("ip") or DEFAULT_IP),
                    "port": str(data.get("port") or DEFAULT_PORT)}]
    current = str(data.get("current") or presets[0]["name"])
    if current not in [p["name"] for p in presets]:
        current = presets[0]["name"]
    return {"current": current, "share": bool(data.get("share")), "presets": presets}


def _config_candidates(custom: str) -> list:
    """读取配置的候选位置，按优先级：自定义路径 > %APPDATA% 默认 > 项目目录旧位置"""
    paths = ([custom] if custom else []) + [CONFIG_FILE, LEGACY_CONFIG_FILE]
    seen, out = set(), []
    for p in paths:
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def load_config(custom: str = "") -> dict:
    target = custom or CONFIG_FILE
    # 目标位置已有文件（哪怕解析失败）就绝不迁移覆盖：文件损坏时保留现场，让用户能修
    target_exists = os.path.exists(target)
    for path in _config_candidates(custom):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = _normalize(json.load(f))
        except (OSError, ValueError):
            continue
        if not target_exists and os.path.normcase(path) != os.path.normcase(target):
            # 从低优先级位置读到 → 迁到目标位置，下次起只读目标
            try:
                save_config(cfg["current"], cfg["presets"], cfg["share"], custom)
            except OSError:
                pass  # 迁移失败别拦启动；之后保存时会重试并在界面报错
        return cfg
    return _normalize(None)


def save_config(current: str, presets: list, share: bool = False, custom: str = ""):
    path = custom or CONFIG_FILE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"current": current, "share": share, "presets": presets},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替换，写一半断电也不会损坏配置


# ---------- 界面 ----------

class ProxyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Return>", lambda _e: self.apply(True))  # 回车 = 应用并开启

        self.settings = load_settings()
        self.share_port = self.settings["share_port"]
        self._settings_win = None
        self._clients_win = None
        self._tray = None          # TrayIcon，创建成功后赋值；失败则无托盘
        self._tray_hinted = False  # "已藏到托盘"的气泡每次运行只提示一次
        self._fw_pending = False  # 正在等 UAC/防火墙规则写入，期间忽略重复点击
        self._ui_queue = queue.Queue()  # 工作线程→主线程的 UI 任务队列
        self._pump_ui_queue()
        cfg = load_config(self.settings["config_path"])
        self.presets = cfg["presets"]
        self.preset_var = tk.StringVar(value=cfg["current"])
        self._relay = None          # 运行中的 RelayServer
        self._share_cfg = cfg["share"]  # 共享开关的持久状态（跨启动恢复用）
        self._lan_hint = _primary_lan_ip() or "本机IP"
        pad = {"padx": 12, "pady": 6}
        frm = tk.Frame(root)
        frm.pack(padx=4, pady=4)

        tk.Label(frm, text="预设").grid(row=0, column=0, sticky="e", **pad)
        preset_frm = tk.Frame(frm)
        preset_frm.grid(row=0, column=1, sticky="w", **pad)
        self.preset_box = ttk.Combobox(preset_frm, textvariable=self.preset_var, width=16,
                                       values=[p["name"] for p in self.presets])
        self.preset_box.pack(side="left")
        self.preset_box.bind("<<ComboboxSelected>>", self.on_preset_selected)
        tk.Button(preset_frm, text="删除", width=3, command=self.delete_preset).pack(side="left", padx=(4, 0))

        tk.Label(frm, text="代理地址").grid(row=1, column=0, sticky="e", **pad)
        self.ip_var = tk.StringVar(value=self._current_preset()["ip"])
        tk.Entry(frm, textvariable=self.ip_var, width=22).grid(row=1, column=1, sticky="w", **pad)

        tk.Label(frm, text="代理端口").grid(row=2, column=0, sticky="e", **pad)
        self.port_var = tk.StringVar(value=self._current_preset()["port"])
        self.port_entry = tk.Entry(frm, textvariable=self.port_var, width=22)
        self.port_entry.grid(row=2, column=1, sticky="w", **pad)
        self.port_var.trace_add("write", lambda *_: self._validate_port())  # 即时校验
        self._validate_port()

        self.status_var = tk.StringVar()
        tk.Label(frm, textvariable=self.status_var, fg="#555").grid(
            row=3, column=0, columnspan=2, **pad)

        self.btn = tk.Button(frm, width=18, command=self.toggle)
        self.btn.grid(row=4, column=0, columnspan=2, ipady=4, padx=12, pady=(2, 4))

        self.test_btn = tk.Button(frm, text="测试连通", width=8, command=self.test_connectivity)
        self.test_btn.grid(row=5, column=0, sticky="w", padx=(12, 4), pady=(0, 6))
        self.test_result_var = tk.StringVar()
        self.test_result_label = tk.Label(frm, textvariable=self.test_result_var, fg="#555")
        self.test_result_label.grid(row=5, column=1, sticky="w", padx=(0, 12), pady=(0, 6))

        self.autostart_var = tk.BooleanVar(value=get_autostart())
        tk.Checkbutton(frm, text="开机自动启动", variable=self.autostart_var,
                       command=self.toggle_autostart).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        tk.Label(frm, text="局域网共享").grid(row=7, column=0, sticky="e", **pad)
        share_frm = tk.Frame(frm)
        share_frm.grid(row=7, column=1, sticky="w", **pad)
        self.share_btn = tk.Button(share_frm, text="开启共享", width=8, command=self.toggle_share)
        self.share_btn.pack(side="left")
        tk.Button(share_frm, text="设置…", width=6, command=self.open_settings).pack(side="left", padx=(6, 0))
        tk.Button(share_frm, text="客户端", width=6, command=self.open_clients).pack(side="left", padx=(6, 0))
        self.share_status_var = tk.StringVar()
        self.share_status_label = tk.Label(frm, textvariable=self.share_status_var, fg="#555",
                                           justify="left", anchor="w")
        self.share_status_label.grid(row=8, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10))
        self._set_share_status(self._share_off_text())

        self._auto_refresh()  # 每 2 秒重读注册表，窗口状态始终跟系统一致
        if self._share_cfg:  # 上次关窗时共享还开着 → 这次自动恢复
            self.root.after(400, self._restore_share)

        # 托盘：点 X 藏进通知区继续跑（代理/共享都不断），右键菜单可真正退出
        try:
            self._tray = TrayIcon(self._tray_restore, self._tray_menu)
            self._update_tray_tip()
        except Exception:
            traceback.print_exc()  # 托盘失败只影响常驻提示，不影响主功能
            self._tray = None

    # ---- 预设 ----

    def _current_preset(self) -> dict:
        for p in self.presets:
            if p["name"] == self.preset_var.get():
                return p
        return self.presets[0]

    def on_preset_selected(self, _event=None):
        p = self._current_preset()
        self.ip_var.set(p["ip"])
        self.port_var.set(p["port"])
        self._sync_relay_upstream()

    def delete_preset(self):
        if len(self.presets) <= 1:
            messagebox.showinfo("提示", "至少要保留一个预设", parent=self.root)
            return
        name = self.preset_var.get()
        rest = [p for p in self.presets if p["name"] != name]
        if len(rest) == len(self.presets):  # 输入框里是不存在的名字
            messagebox.showinfo("提示", f"没有名为「{name}」的预设", parent=self.root)
            return
        self.presets = rest
        first = self.presets[0]
        self.preset_var.set(first["name"])
        self.ip_var.set(first["ip"])
        self.port_var.set(first["port"])
        self._sync_preset_box()
        self._sync_relay_upstream()  # 共享运行中也要跟上切换后的地址
        self.save_preset()

    def save_preset(self):
        """把当前填写的地址端口存进当前名字的预设（名字是新的则新建）"""
        ip, port = self.ip_var.get().strip(), self.port_var.get().strip()
        name = self.preset_var.get().strip()
        if not name:
            name = f"{ip}:{port}" if ip or port else "默认"
        for p in self.presets:
            if p["name"] == name:
                p["ip"], p["port"] = ip, port
                break
        else:
            self.presets.append({"name": name, "ip": ip, "port": port})
            self._sync_preset_box()
        self.preset_var.set(name)
        try:
            save_config(name, self.presets, self._share_cfg, self.settings["config_path"])
        except OSError as e:
            messagebox.showerror("错误", f"保存配置失败：\n{e}", parent=self.root)

    def _sync_preset_box(self):
        self.preset_box.config(values=[p["name"] for p in self.presets])

    # ---- 行为 ----

    def _auto_refresh(self):
        self.refresh_status()
        self._refresh_share_status()
        self._update_tray_tip()
        self.root.after(REFRESH_INTERVAL_MS, self._auto_refresh)

    def _ui(self, fn):
        """后台线程把 UI 操作投递回主线程执行。
        本机的 Tcl 是非线程安全构建，工作线程直接 root.after 会抛
        RuntimeError，所以只能入队，由主线程的轮询器取出执行。"""
        self._ui_queue.put(fn)

    def _pump_ui_queue(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                traceback.print_exc()  # 单个任务出错别拖垮轮询；行为等同 Tk 默认
        self.root.after(50, self._pump_ui_queue)

    def _refresh_share_status(self):
        """共享运行中：状态行显示设备/连接计数（设备按来源 IP 去重，近10分钟活跃）"""
        if not self._relay:
            return
        conns, devs = self._relay.snapshot()
        n_dev = sum(1 for _, _, sec in devs if sec <= ACTIVE_WINDOW_SEC)
        up = self._relay.upstream
        tail = f"近10分钟设备 {n_dev} 台，当前连接 {conns} 个" if devs or conns else "暂无设备连接"
        self._set_share_status(
            f"✓ 共享中：其他设备把代理填成 {self._lan_hint}:{self.share_port}\n"
            f"转发到 {up[0]}:{up[1]} ｜ {tail}", color="#1a7f37")

    def refresh_status(self):
        self.enabled, server = get_system_proxy()
        if self.enabled:
            self.status_var.set(f"当前状态：已开启（{server}）")
            self.btn.config(text="关闭代理", bg="#ffd7d7")
        else:
            self.status_var.set("当前状态：已关闭")
            self.btn.config(text="开启代理", bg="#d7f0d7")

    @staticmethod
    def _parse_port(text: str):
        """端口文本 -> int 或 None。只认 ASCII 数字：'²' 这类 isdigit() 为真的字符会让 int() 炸"""
        text = text.strip()
        if text.isascii() and text.isdecimal():
            port = int(text)
            if 1 <= port <= 65535:
                return port
        return None

    def _validate_port(self):
        port = self.port_var.get().strip()
        ok = not port or self._parse_port(port) is not None  # 空=还没填，不算错
        self.port_entry.config(bg="#ffffff" if ok else "#ffdcdc")

    @staticmethod
    def _valid_host(host: str) -> bool:
        """代理地址格式检查：IPv4 或主机名都合法，拦掉逗号/空格/协议前缀/
        误填进 IP 栏的“IP:端口”这类手误——乱值写进注册表会断整个系统上网"""
        if not host or len(host) > 253:
            return False
        for lb in host.rstrip(".").split("."):
            if not lb.isascii() or not lb or len(lb) > 63:
                return False
            if not all(c.isalnum() or c == "-" for c in lb):
                return False
            if not (lb[0].isalnum() and lb[-1].isalnum()):
                return False
        return True

    def _valid_inputs(self) -> bool:
        ip = self.ip_var.get().strip()
        if (ip and self._valid_host(ip)
                and self._parse_port(self.port_var.get()) is not None):
            return True
        messagebox.showwarning("提示", "请填写正确的代理地址（IP 或主机名）和端口（1-65535）",
                               parent=self.root)
        return False

    def _read_upstream(self):
        ip = self.ip_var.get().strip()
        port = self._parse_port(self.port_var.get())
        if ip and port and self._valid_host(ip):
            return (ip, port)
        return None

    def _sync_relay_upstream(self):
        """转发目标始终跟随当前填写的代理，新连接即生效，无需重启共享"""
        up = self._read_upstream()
        if up and self._relay:
            self._relay.upstream = up

    def toggle(self):
        self.apply(not self.enabled)

    def apply(self, enable: bool):
        ip = self.ip_var.get().strip()
        port = self.port_var.get().strip()
        if enable and not self._valid_inputs():
            return
        try:
            if enable:
                set_system_proxy(f"{ip}:{port}")
            else:
                disable_system_proxy()
        except OSError as e:
            messagebox.showerror("错误", f"操作失败：\n{e}", parent=self.root)
            return
        self.save_preset()  # 记住这次填的地址端口
        self._sync_relay_upstream()
        self.refresh_status()

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
        except OSError as e:
            self.autostart_var.set(not self.autostart_var.get())  # 回退勾选状态
            messagebox.showerror("错误", f"设置开机自启失败：\n{e}", parent=self.root)

    def test_connectivity(self):
        """走当前填写的代理请求 generate_204，后台线程执行避免卡界面"""
        if not self._valid_inputs():
            return
        ip, port = self.ip_var.get().strip(), int(self.port_var.get().strip())
        self.test_btn.config(state="disabled", text="测试中…")
        self.test_result_var.set("正在测试…")
        self.test_result_label.config(fg="#555")
        threading.Thread(target=self._test_worker, args=(ip, port), daemon=True).start()

    def _test_worker(self, ip: str, port: int):
        proxies = {"http": f"http://{ip}:{port}", "https": f"http://{ip}:{port}"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        start = time.perf_counter()
        try:
            opener.open(TEST_URL, timeout=3)
        except Exception as e:  # 超时、拒绝连接等统一按失败展示
            reason = getattr(e, "reason", None) or e
            text = str(reason)
            if "timed out" in text.lower():
                text = "3 秒无响应（超时）"
            self._ui(lambda: self._show_test_result(False, text))
        else:
            ms = round((time.perf_counter() - start) * 1000)
            self._ui(lambda: self._show_test_result(True, f"{ms} ms"))

    def _show_test_result(self, ok: bool, info: str):
        self.test_btn.config(state="normal", text="测试连通")
        if ok:
            self.test_result_var.set(f"✓ 连通（{info}）")
            self.test_result_label.config(fg="#1a7f37")
        else:
            self.test_result_var.set(f"✗ 失败：{info}")
            self.test_result_label.config(fg="#c62828")

    # ---- 局域网共享 ----

    def _share_off_text(self, port=None) -> str:
        return f"未开启：开启后其他设备把代理填成 {self._lan_hint}:{port or self.share_port}"

    def _set_share_status(self, text: str, color: str = "#555"):
        self.share_status_var.set(text)
        self.share_status_label.config(fg=color)

    def toggle_share(self):
        if self._relay:
            self._relay.stop()
            self._relay = None
            self.share_btn.config(text="开启共享")
            self._share_cfg = False
            self.save_preset()
            self._set_share_status(self._share_off_text())
            return
        up = self._read_upstream()
        if not up:
            messagebox.showwarning("提示", "请先填写正确的代理 IP 和端口", parent=self.root)
            return
        lan = _primary_lan_ip()
        if not lan:
            messagebox.showerror("错误", "无法获取本机局域网 IP", parent=self.root)
            return
        if self._fw_pending:  # 检查规则/等 UAC 进行中，忽略重复点击
            return
        self._fw_pending = True
        self._set_share_status("正在检查防火墙规则…")
        threading.Thread(target=self._check_fw_then_start, args=(up, lan), daemon=True).start()

    def _restart_fw_check(self, lan):
        """检查/等 UAC 期间共享端口被改了：旧结果作废，用新端口重来一轮"""
        up2 = self._read_upstream()
        if up2 is None:
            self._fw_pending = False
            self._set_share_status("✗ 代理地址无效，未开启共享", color="#c62828")
            return
        self._set_share_status("正在检查防火墙规则…")
        threading.Thread(target=self._check_fw_then_start, args=(up2, lan), daemon=True).start()

    def _check_fw_then_start(self, up, lan):
        """后台查防火墙规则（netsh 会卡主线程），查完回主线程决定弹 UAC 还是直接开"""
        port = self.share_port
        has_rule = firewall_rule_exists(port)

        def proceed():
            if port != self.share_port:  # 检查期间用户在设置里改了端口：旧结果作废
                self._restart_fw_check(lan)
                return
            if has_rule:
                self._fw_pending = False
                self._start_share(up, lan)
                return
            if not messagebox.askyesno(
                    "局域网共享",
                    f"首次开启需要在防火墙放行 TCP 端口 {port}\n"
                    "（会弹出管理员确认窗口，请选“是”；只对本机所在子网放行）。\n现在添加吗？",
                    parent=self.root):
                self._fw_pending = False
                self._set_share_status(self._share_off_text(port))
                return
            self._set_share_status("正在添加防火墙规则…请在弹出的窗口点“是”")
            threading.Thread(target=self._fw_then_start, args=(up, lan, port), daemon=True).start()

        self._ui(proceed)

    def _fw_then_start(self, up, lan, port):
        ok = add_firewall_rule_elevated(port)
        rule_ok = False
        for _ in range(8):  # netsh 是异步的，等规则真正写入（最多约 3 秒）
            if not ok:  # UAC 被取消/失败，别再白跑 8 轮 netsh
                break
            rule_ok = firewall_rule_exists(port)
            if rule_ok:
                break
            time.sleep(0.4)

        def finish():
            if port != self.share_port:
                # 等 UAC/写规则期间端口又被改了：这条规则已对不上号，用新端口重来一轮
                self._restart_fw_check(lan)
                return
            self._fw_pending = False
            if rule_ok:  # 结果后台已查好，别在主线程再跑一次 netsh（会卡界面）
                self._start_share(up, lan)
            else:
                self._set_share_status(
                    "✗ 防火墙规则没加上（取消了 UAC 或失败），未开启共享", color="#c62828")

        self._ui(finish)

    def _start_share(self, up, lan):
        try:
            relay = RelayServer(self.share_port, up)
            relay.start()
        except OSError as e:
            self._fw_pending = False  # 失败要解锁，否则之后点开启都被"进行中"挡住
            self._set_share_status(f"✗ 开启失败：{e}", color="#c62828")
            return
        self._relay = relay
        self._lan_hint = lan or self._lan_hint  # 用刚探测到的 IP，别用过期缓存
        self.share_btn.config(text="停止共享")
        self._share_cfg = True
        self.save_preset()
        self._refresh_share_status()

    def _restore_share(self):
        if self._relay:  # 400ms 恢复窗口里用户已手动开启，别再走恢复
            return
        up = self._read_upstream()
        if not up:
            self._set_share_status(
                "上次开启了共享，但代理地址无效，没有自动恢复，请手动开启一次",
                color="#c62828")
            return
        self._fw_pending = True
        threading.Thread(target=self._restore_worker, args=(up,), daemon=True).start()

    def _restore_worker(self, up):
        port = self.share_port
        has_rule = firewall_rule_exists(port)

        def finish():
            self._fw_pending = False
            if self._relay:  # 恢复期间用户已手动开启，再开就撞端口（假报错）
                return
            if port != self.share_port:  # 期间改了端口，这条检查结果已过时
                self._set_share_status(
                    "共享端口已改动，没有自动恢复，请重新点“开启共享”", color="#c62828")
                return
            if has_rule:
                self._start_share(up, self._lan_hint)
            else:
                self._set_share_status(
                    "上次开启了共享，但这次没有自动恢复\n（防火墙规则缺失），请手动开启一次",
                    color="#c62828")

        self._ui(finish)

    # ---- 设置页面 ----

    def open_settings(self):
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("设置")
        win.resizable(False, False)
        win.transient(self.root)
        frm = tk.Frame(win)
        frm.pack(padx=14, pady=10)
        pad = {"padx": 8, "pady": 5}

        tk.Label(frm, text="共享监听端口").grid(row=0, column=0, sticky="e", **pad)
        port_var = tk.StringVar(value=str(self.share_port))
        tk.Entry(frm, textvariable=port_var, width=12).grid(row=0, column=1, sticky="w", **pad)

        tk.Label(frm, text="配置文件路径").grid(row=1, column=0, sticky="e", **pad)
        path_var = tk.StringVar(value=self.settings["config_path"] or CONFIG_FILE)
        tk.Entry(frm, textvariable=path_var, width=36).grid(row=1, column=1, sticky="w", **pad)

        def browse():
            picked = filedialog.asksaveasfilename(
                parent=win, title="选择配置文件位置", defaultextension=".json",
                initialfile=os.path.basename(path_var.get()) or "proxy_config.json")
            if picked:
                path_var.set(os.path.normpath(picked))

        tk.Button(frm, text="浏览…", command=browse).grid(row=1, column=2, padx=(0, 8))

        tk.Label(frm, text="切换路径时会把当前配置复制到新位置；\n改了共享端口后需重新点一次“开启共享”。",
                 fg="#888", justify="left").grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        btns = tk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, pady=(2, 8))

        def on_save():
            port = self._parse_port(port_var.get())
            if port is None:
                messagebox.showwarning("提示", "共享端口需为 1-65535 的数字", parent=win)
                return
            new_path = self._normalize_config_path(path_var.get())
            old_path = self.settings["config_path"] or CONFIG_FILE
            changed = os.path.normcase(new_path) != os.path.normcase(old_path)
            if changed and os.path.exists(new_path):
                if not messagebox.askyesno("提示", "新路径已有配置文件，用当前配置覆盖它？", parent=win):
                    return
            port_changed = port != self.share_port
            new_settings = {"share_port": port,
                            "config_path": new_path if changed else old_path}
            try:
                save_settings(new_settings)
            except OSError as e:
                messagebox.showerror("错误", f"保存设置失败：\n{e}", parent=win)
                return  # 写盘失败就别动内存状态，免得界面和文件分叉
            self.share_port = port
            self.settings = new_settings
            if changed:
                self.save_preset()  # 立刻把当前配置写到新位置
            if port_changed:
                if self._relay:  # 端口变了，旧监听作废
                    self._relay.stop()
                    self._relay = None
                    self.share_btn.config(text="开启共享")
                    self._share_cfg = False  # 共享已停，别把"开着"残留进配置
                    self.save_preset()
                    self._set_share_status(
                        f"共享端口已改为 {self.share_port}，请重新点“开启共享”", color="#c62828")
                else:
                    self._set_share_status(self._share_off_text())
            win.destroy()

        tk.Button(btns, text="保存", width=8, command=on_save).pack(side="left", padx=6)
        tk.Button(btns, text="取消", width=8, command=win.destroy).pack(side="left")
        win.bind("<Return>", lambda _e: on_save())  # 回车=保存，Esc=取消
        win.bind("<Escape>", lambda _e: win.destroy())
        self._center_over(win)  # 摆到主窗口中央再锁模态
        win.grab_set()

    @staticmethod
    def _normalize_config_path(text: str) -> str:
        path = text.strip()
        if not path:
            path = CONFIG_FILE  # 清空=回到默认位置；abspath("") 会落到 CWD（自启时是 System32）
        # 转绝对路径：开机自启时 CWD 是 System32，相对路径会把配置写错地方
        return os.path.abspath(os.path.normpath(os.path.expandvars(os.path.expanduser(path))))

    def _center_in(self, win, x, y, w, h):
        """把 win 摆到矩形 (x, y, w, h) 中央。
        geometry(+x+y) 定位的是窗口外框，winfo_root* 量的是客户区，
        两者差一个边框+标题栏的固定值：先放一次量出差值，再修正一步。"""
        win.update_idletasks()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        dx, dy = (w - ww) // 2, (h - wh) // 2
        win.geometry(f"+{x + dx}+{y + dy}")
        win.update_idletasks()
        ex = win.winfo_rootx() - x - dx
        ey = win.winfo_rooty() - y - dy
        if ex or ey:
            win.geometry(f"+{x + dx - ex}+{y + dy - ey}")

    def _center_over(self, win):
        self._center_in(win, self.root.winfo_rootx(), self.root.winfo_rooty(),
                        self.root.winfo_width(), self.root.winfo_height())

    def open_clients(self):
        """共享客户端列表小窗：每台设备的 IP、当前连接数、最后活跃时间"""
        if self._clients_win is not None and self._clients_win.winfo_exists():
            self._clients_win.lift()
            return
        win = tk.Toplevel(self.root)
        self._clients_win = win
        win.title("共享客户端")
        win.resizable(False, False)
        win.transient(self.root)
        tv = ttk.Treeview(win, columns=("ip", "conns", "last"), show="headings", height=8)
        for col, text, width in (("ip", "设备 IP", 150), ("conns", "当前连接", 80),
                                 ("last", "最后活跃", 90)):
            tv.heading(col, text=text)
            tv.column(col, width=width, anchor="w" if col == "ip" else "center")
        tv.pack(padx=10, pady=(10, 4))
        summary = tk.Label(win, text="", fg="#555")
        summary.pack(padx=10, pady=(0, 10))

        def refresh():
            if not win.winfo_exists():
                return
            tv.delete(*tv.get_children())
            if self._relay:
                conns, devs = self._relay.snapshot()
                for ip, c, sec in devs:
                    tv.insert("", "end", values=(ip, c, f"{sec} 秒前"))
                n_dev = sum(1 for _, _, sec in devs if sec <= ACTIVE_WINDOW_SEC)
                summary.config(text=f"当前连接 {conns} 个 ｜ 近10分钟设备 {n_dev} 台")
            else:
                summary.config(text="共享未开启")
            win.after(REFRESH_INTERVAL_MS, refresh)

        refresh()
        self._center_over(win)

    # ---- 托盘 ----

    def _make_tray_tip(self) -> str:
        state = "代理开" if self.enabled else "代理关"
        if self._relay:
            state += "，共享中"
        return f"{APP_TITLE}：{state}\n左键打开主窗口，右键菜单可退出"

    def _update_tray_tip(self):
        if not self._tray:
            return
        try:
            self._tray.show(self._make_tray_tip())  # show 内部只在变化时发消息
        except Exception:
            traceback.print_exc()

    def _tray_restore(self):
        # 本方法被托盘 WndProc 回调调用，而那个回调是 Tk 消息泵的重入点，
        # 在里面直接碰 tkinter 会重入 Tcl、进程当场 Fatal Error——先入队
        self._ui(self._do_tray_restore)

    def _do_tray_restore(self):
        self.root.deiconify()
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        if not user32.SetForegroundWindow(hwnd):  # 抢不到前台就闪任务栏
            flash_window(hwnd)

    def _tray_menu(self):
        self._ui(self._show_tray_menu)  # 同上：菜单的创建/弹出必须回主循环做

    def _show_tray_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="打开主窗口", command=self._tray_restore)
        menu.add_separator()
        # 退出只 quit 不 destroy：tk_popup 阻塞期间 Tcl 定时器照跑，在菜单循环里
        # destroy 会踩烂收场；菜单选完自然关闭，mainloop 退出后由 __main__ 收尸
        menu.add_command(label="退出", command=self.quit_app)
        pos = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        try:
            # 前台必须给“拥有菜单”的主窗口；给别的隐藏窗口菜单会一点就消失。
            # 事后 WM_NULL 配对，菜单才能正常收场
            user32.SetForegroundWindow(hwnd)
            menu.tk_popup(pos.x, pos.y)
        finally:
            menu.grab_release()
            menu.destroy()  # 每次右键都新建，用完即毁，别积攒孤儿 Tcl 窗口
            user32.PostMessageW(hwnd, 0, 0, 0)  # WM_NULL

    def on_close(self):
        """点窗口 X：藏到托盘继续跑，代理和共享都不断。
        托盘不可用（没建起来/图标一直没加上）时藏了就再也找不回，只能真退出"""
        if self._tray is None or not self._tray._added:
            self.quit_app()
            return
        self.root.withdraw()
        if not self._tray_hinted:
            self._tray_hinted = True
            try:
                self._tray.balloon(APP_TITLE, "已最小化到托盘，代理和共享仍在运行。\n右键托盘图标可退出。")
            except Exception:
                traceback.print_exc()

    def quit_app(self):
        """真正退出（托盘菜单的“退出”）：停转发、存配置、摘掉托盘图标。
        只 quit 不 destroy——destroy . 销毁窗口但**不会终止 mainloop**，进程会
        空转残活（实测踩过）；mainloop 由 quit 终止，窗口由 __main__ 统一销毁"""
        if self._relay:
            self._relay.stop()  # 只停转发；共享开关状态保留，下次启动自动恢复
        self.save_preset()
        if self._tray:
            try:
                self._tray.remove()
            except Exception:
                traceback.print_exc()
        self.root.quit()


if __name__ == "__main__":
    if not acquire_single_instance():  # 已有实例，其窗口已被拉到前台
        raise SystemExit(0)
    try:  # 高分屏下界面更清晰
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = ProxyApp(root)
    # 主窗口摆到屏幕中央，与弹窗共用同一套“放一次→量差值→修正一步”逻辑
    app._center_in(root, 0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    root.mainloop()
    root.destroy()  # quit_app 只 quit 不 destroy，mainloop 退出后在这里统一收尸

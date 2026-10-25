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
# V2 独立目录：V1 读不懂 upstreams 格式，若共用 ProxyTool 目录，V1 一退出就会把
# 全部预设覆写成默认值——两个版本各守各的配置，互不见面
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or _BASE_DIR, "ProxyToolV2")
CONFIG_FILE = os.path.join(CONFIG_DIR, "proxy_config.json")  # 默认配置位置（可在设置里改）
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")    # 应用设置（固定放这，不跟随配置路径）
_OLD_DIR = os.path.join(os.environ.get("APPDATA") or _BASE_DIR, "ProxyTool")
OLD_CONFIG_FILE = os.path.join(_OLD_DIR, "proxy_config.json")    # V1 配置，V2 首启读一次迁走
OLD_SETTINGS_FILE = os.path.join(_OLD_DIR, "settings.json")      # V1 设置，带上旧共享端口/认证
LEGACY_CONFIG_FILE = os.path.join(_BASE_DIR, "proxy_config.json")  # 更早期配置位置，读到即迁移
DEFAULT_IP = "127.0.0.1"
DEFAULT_PORT = "7890"
DEFAULT_SHARE_PORT = 7891
DEFAULT_SOCKS_PORT = 7892  # SOCKS5 通道端口（局域网共享的第二条通道）
DEFAULT_ENTRY_PORT = 7890  # 本机入口端口（多代理分流，系统代理指向它）；被 Clash 等占用时设置里可改
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
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN, TPM_RETURNCMD = 0x2, 0x20, 0x100


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
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = wintypes.BOOL  # TPM_RETURNCMD 时返回的是命令 id
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.DWORD, wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [wintypes.HMENU]


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

    def show_menu(self, entries):
        """弹原生托盘菜单（TrackPopupMenu，资源管理器同款），阻塞至菜单关闭，
        返回被点中项的回调（未选中返回 None）。**必须在主线程调用**（内部
        _ui 入队后到达，调用点即安全点，返回的回调可直接执行）。
        entries: [(label, callback), ...]，label 为 "-" 画分隔线。
        不用 Tk 菜单：tk_popup 在托盘场景真实点击不派发选中（实测玄学），
        原生菜单的派发由 Windows 内部完成，不经过 Tcl。"""
        hmenu = user32.CreatePopupMenu()
        for i, (label, _cb) in enumerate(entries):
            if label == "-":
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(hmenu, MF_STRING, i + 1, label)
        pos = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        # 前台必须给拥有菜单的窗口，事后 WM_NULL 配对收场（经典托盘菜单三件套）
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                                    pos.x, pos.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, 0, 0, 0)  # WM_NULL
        user32.DestroyMenu(hmenu)
        return entries[cmd - 1][1] if 0 < cmd <= len(entries) else None


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


def _rule_name(port: int, socks: bool = False) -> str:
    return f"ProxyTool LAN {'SOCKS' if socks else 'Share'} {port}"


def firewall_rule_exists(port: int, socks: bool = False) -> bool:
    try:
        # timeout：防火墙服务坏了会让 netsh 卡死，不设上限会把共享流程永久卡住；
        # CREATE_NO_WINDOW：pythonw 没控制台，不加这个 netsh 会自己弹黑框闪一下
        out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                              f"name={_rule_name(port, socks)}"], capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
    except subprocess.TimeoutExpired:
        return False  # 卡死按“没规则”处理：让用户重开一次，也别锁死 _fw_pending
    return out.returncode == 0


def add_firewall_rule_elevated(rules) -> bool:
    """弹 UAC 提权加放行规则（只对本机所在子网开放）。
    rules: [(port, socks)，...] 多条规则在同一个提权进程里 & 串联——UAC 只弹一次"""
    cmds = (f'netsh advfirewall firewall add rule name="{_rule_name(p, s)}" '
            f"dir=in action=allow protocol=TCP localport={p} remoteip=localsubnet"
            for p, s in rules)
    return ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", "/c " + " & ".join(cmds), None, 0) > 32


# ---------- 上游池（多代理分流） ----------

class UpstreamPool:
    """多上游选择器：round-robin 按连接轮询，连续失败的上游短暂熔断殿后兜底。

    order() 推进轮询指针并给出本次连接的尝试顺序：健康上游按轮转序在前，
    熔断中的（连续失败 >= BAN_AFTER 且未过 BAN_SECS）殿后——不作首选但仍在列，
    全部熔断时它们仍是最后的希望。
    """

    BAN_AFTER = 3   # 连续失败次数达到即熔断
    BAN_SECS = 30   # 熔断时长：期间不作首选（兜底仍可试到）

    def __init__(self, upstreams, clock=time.monotonic):
        self._clock = clock  # 注入时间源，测试里用假钟避免真等 30 秒
        self._lock = threading.Lock()
        self._ups = []
        self._idx = 0      # 轮询指针：order() 推进
        self._fails = {}   # up -> 连续失败次数
        self._banned = {}  # up -> 禁止至（clock 值）
        self.set(upstreams)

    def set(self, upstreams):
        """整体替换池（切预设/改池用），指针与失败计数一并重置"""
        with self._lock:
            self._ups = list(upstreams)
            self._idx = 0
            self._fails = {}
            self._banned = {}

    def order(self):
        """本次连接的尝试顺序：健康的轮转序在前、熔断者殿后；空池返回 []"""
        with self._lock:
            if not self._ups:
                return []
            n = len(self._ups)
            start = self._idx
            self._idx = (self._idx + 1) % n
            rot = [self._ups[(start + i) % n] for i in range(n)]
            now = self._clock()
            healthy = [u for u in rot if now >= self._banned.get(u, 0)]
            banned = [u for u in rot if now < self._banned.get(u, 0)]
            return healthy + banned

    def report_fail(self, up):
        with self._lock:
            self._fails[up] = self._fails.get(up, 0) + 1
            if self._fails[up] >= self.BAN_AFTER:
                self._banned[up] = self._clock() + self.BAN_SECS

    def report_ok(self, up):
        with self._lock:
            self._fails.pop(up, None)


class RelayServer:
    """把 0.0.0.0:listen_port 收到的 TCP 连接原样搬给上游代理（不解协议）"""

    def __init__(self, listen_port: int, upstream: tuple):
        self.listen_port = listen_port
        self.upstream = upstream  # (ip, port)，可随时替换，对新连接生效
        self._srv = None
        self._on = False
        self._lock = threading.Lock()
        self.active_conns = 0  # 当前 TCP 连接总数
        self.devices = {}      # ip -> {"conns": 连接数, "last": 最后活跃, "bd"/"bu": 累计下行/上行字节}
        self._live = set()     # 活跃连接对 (client, up)，stop 时强制断开用
        self.bytes_down = 0    # 累计下行字节（上游→客户端），锁内更新
        self.bytes_up = 0      # 累计上行字节（客户端→上游）

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
            dev = self.devices.setdefault(ip, {"conns": 0, "last": time.monotonic(), "bd": 0, "bu": 0})
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
            t1 = threading.Thread(target=self._pipe, args=(client, up, dev, False), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(up, client, dev, True), daemon=True)
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
        """界面轮询用：(连接总数, [(ip, 连接数, 距活跃秒, 累计下行, 累计上行), ...],
        (累计下行字节, 累计上行字节))"""
        now = time.monotonic()
        with self._lock:
            if self.devices:
                self.devices = {ip: d for ip, d in self.devices.items()
                                if d["conns"] > 0 or now - d["last"] < DEVICE_TTL_SEC}
            devs = sorted(((ip, d["conns"], int(now - d["last"]), d["bd"], d["bu"])
                           for ip, d in self.devices.items()),
                          key=lambda x: -x[1])
            return self.active_conns, devs, (self.bytes_down, self.bytes_up)

    def _pipe(self, src, dst, dev, down: bool):
        """双向搬运字节；down=True 表示 dst 是客户端（下行）。字节计数拿锁入账"""
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
                n = len(data)
                with self._lock:
                    if down:
                        self.bytes_down += n
                        dev["bd"] += n
                    else:
                        self.bytes_up += n
                        dev["bu"] += n
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)  # 半关，让对面把剩余数据收完
            except OSError:
                pass


# ---------- SOCKS5 网关（桥接到 HTTP 上游代理） ----------

class _SocksError(Exception):
    """SOCKS5 会话失败：rep=回给客户端的错误码，消息给人看（状态行）。
    rep=None 表示握手阶段已回过应答（如 0xFF），_pump 里别再补发"""

    def __init__(self, rep, msg: str):
        super().__init__(msg)
        self.rep = rep


class SocksServer:
    """0.0.0.0:listen_port 收 SOCKS5（仅 CONNECT），桥接到 HTTP 上游代理。
    上游只认 HTTP 代理时的协议转换网关：对上游发 CONNECT，200 后双向搬字节。
    启停/记账/管道结构与 RelayServer 同款。"""

    HANDSHAKE_TIMEOUT = 5  # 握手+拨上游限时；隧道接通后两边都清掉

    def __init__(self, listen_port: int, upstream: tuple, user: str = "", password: str = ""):
        self.listen_port = listen_port
        self.upstream = upstream  # (ip, port)，可随时替换，对新连接生效
        self.user = user.strip()
        self.password = password
        self.last_error = ""  # 最近一次 SOCKS 会话失败原因（带时刻，状态行显示）
        self._srv = None
        self._on = False
        self._lock = threading.Lock()
        self.active_conns = 0
        self.devices = {}
        self._live = set()
        self.bytes_down = 0    # 累计下行字节（上游→客户端），锁内更新
        self.bytes_up = 0      # 累计上行字节（客户端→上游）

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
        # 同 RelayServer：已建立的/握手中的连接都掐断，keep-alive 别想赖着
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
        srv = self._srv  # 局部快照：stop() 会把 _srv 置 None
        while self._on and srv is not None:
            try:
                client, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._pump, args=(client,), daemon=True).start()

    def snapshot(self):
        """同 RelayServer.snapshot：(连接总数, 设备列表, (累计下行, 累计上行))"""
        now = time.monotonic()
        with self._lock:
            if self.devices:
                self.devices = {ip: d for ip, d in self.devices.items()
                                if d["conns"] > 0 or now - d["last"] < DEVICE_TTL_SEC}
            devs = sorted(((ip, d["conns"], int(now - d["last"]), d["bd"], d["bu"])
                           for ip, d in self.devices.items()),
                          key=lambda x: -x[1])
            return self.active_conns, devs, (self.bytes_down, self.bytes_up)

    def _note_error(self, e: _SocksError):
        self.last_error = f"{time.strftime('%H:%M')} {e}"

    @staticmethod
    def _recv_exact(sock, n: int) -> bytes:
        """SOCKS 握手按字段长度读全——TCP 分包下一次 recv 可能只到半截"""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("客户端提前断开")
            buf += chunk
        return bytes(buf)

    @staticmethod
    def _reply(client, rep: int):
        """VER=5 REP RSV ATYP=IPv4 BND=0.0.0.0:0"""
        try:
            client.sendall(bytes((5, rep, 0, 1)) + b"\x00" * 6)
        except OSError:
            pass

    def _handshake(self, client) -> str:
        """认证 + 读 CONNECT 请求，返回 "host:port"。失败抛 _SocksError"""
        ver, nmethods = self._recv_exact(client, 2)
        if ver != 5:
            raise _SocksError(1, "不是 SOCKS5 握手")
        methods = self._recv_exact(client, nmethods)
        if self.user:  # RFC 1929 用户名密码认证（设置里留空 = 无认证）
            if 2 not in methods:
                self._reply(client, 0xFF)
                raise _SocksError(None, "客户端不支持用户名密码认证")
            client.sendall(b"\x05\x02")
            if self._recv_exact(client, 1)[0] != 1:
                raise _SocksError(1, "认证子协商版本错误")
            ulen = self._recv_exact(client, 1)[0]
            uname = self._recv_exact(client, ulen).decode("utf-8", "replace")
            plen = self._recv_exact(client, 1)[0]
            passwd = self._recv_exact(client, plen).decode("utf-8", "replace")
            ok = uname == self.user and passwd == self.password
            client.sendall(b"\x01" + (b"\x00" if ok else b"\x01"))
            if not ok:
                raise _SocksError(1, "用户名或密码错误")
        else:
            if 0 not in methods:
                self._reply(client, 0xFF)
                raise _SocksError(None, "客户端不支持无认证方式")
            client.sendall(b"\x05\x00")
        ver, cmd, _rsv, atyp = self._recv_exact(client, 4)
        if ver != 5:
            raise _SocksError(1, "请求版本错误")
        if cmd != 1:
            raise _SocksError(7, "仅支持 CONNECT（不支持 BIND/UDP 转发）")
        if atyp == 1:
            host = socket.inet_ntoa(self._recv_exact(client, 4))
        elif atyp == 3:  # 域名不在本地解析，原样交给上游（顺带规避本地 DNS 污染）
            n = self._recv_exact(client, 1)[0]
            host = self._recv_exact(client, n).decode("ascii", "replace")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, self._recv_exact(client, 16))
        else:
            raise _SocksError(8, "地址类型不支持")
        port = int.from_bytes(self._recv_exact(client, 2), "big")
        if ":" in host:  # IPv6 字面量进 CONNECT 请求行必须加方括号，否则上游解析不了
            host = f"[{host}]"
        return f"{host}:{port}"

    def _dial_upstream(self, client, target: str):
        """对 HTTP 上游发 CONNECT，200 后回 SOCKS 成功，返回已接通的上游 socket"""
        up = socket.create_connection(self.upstream, timeout=self.HANDSHAKE_TIMEOUT)
        try:
            up.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                if len(resp) > 8192:
                    raise _SocksError(1, "上游响应头异常")
                chunk = up.recv(4096)
                if not chunk:
                    raise _SocksError(1, "上游提前断开")
                resp += chunk
            status = resp.split(b"\r\n", 1)[0]
            if b" 200" not in status:
                raise _SocksError(5, f"上游拒绝 CONNECT：{status.decode('latin-1', 'replace')}")
        except Exception:
            up.close()
            raise
        leftover = resp.split(b"\r\n\r\n", 1)[1]
        self._reply(client, 0)  # 隧道通了才告诉客户端成功
        if leftover:  # 少见：上游把隧道首包捎在响应里，补交给客户端
            client.sendall(leftover)
        return up

    def _pump(self, client):
        try:
            ip = client.getpeername()[0]
        except OSError:
            ip = "未知"
        with self._lock:
            dev = self.devices.setdefault(ip, {"conns": 0, "last": time.monotonic(), "bd": 0, "bu": 0})
            dev["conns"] += 1
            self.active_conns += 1
        up = None
        try:
            t_pending = (client,)
            with self._lock:
                self._live.add(t_pending)  # 握手期间就登记，stop() 才掐得到
            try:
                try:
                    client.settimeout(self.HANDSHAKE_TIMEOUT)  # 握手限时，防半开连接挂死线程
                    target = self._handshake(client)
                    up = self._dial_upstream(client, target)
                except _SocksError as e:
                    self._note_error(e)
                    if e.rep is not None:
                        self._reply(client, e.rep)
                    return
                except (OSError, ConnectionError) as e:
                    self._note_error(_SocksError(1, str(e) or "连接中断"))
                    self._reply(client, 1)
                    return
                up.settimeout(None)      # 同 RelayServer：超时不得残留到长连接
                client.settimeout(None)
                pair = (client, up)
                with self._lock:
                    self._live.add(pair)
                    stopped = not self._on
                if stopped:
                    return
                t1 = threading.Thread(target=self._pipe, args=(client, up, dev, False), daemon=True)
                t2 = threading.Thread(target=self._pipe, args=(up, client, dev, True), daemon=True)
                t1.start(); t2.start()
                t1.join(); t2.join()
                with self._lock:
                    self._live.discard(pair)
            finally:
                with self._lock:
                    self._live.discard(t_pending)
        finally:
            for s in (client, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
            with self._lock:
                dev["conns"] -= 1
                dev["last"] = time.monotonic()
                self.active_conns -= 1

    def _pipe(self, src, dst, dev, down: bool):  # 同 RelayServer._pipe，含字节计数
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
                n = len(data)
                with self._lock:
                    if down:
                        self.bytes_down += n
                        dev["bd"] += n
                    else:
                        self.bytes_up += n
                        dev["bu"] += n
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def _merge_devs(a, b):
    """两路共享的设备表合并：连接数与累计字节相加，活跃时间取更近的"""
    m = {ip: [c, sec, bd, bu] for ip, c, sec, bd, bu in a}
    for ip, c, sec, bd, bu in b:
        if ip in m:
            d = m[ip]
            d[0] += c
            d[1] = min(d[1], sec)
            d[2] += bd
            d[3] += bu
        else:
            m[ip] = [c, sec, bd, bu]
    return [(ip, c, s, bd, bu) for ip, (c, s, bd, bu) in m.items()]


def _fmt_speed(n: float) -> str:
    """字节/秒 → 自动换档的人类可读速率（B/s、KB/s、MB/s、GB/s）"""
    if n <= 0:
        return "0 B/s"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if n < 1024 or unit == "GB/s":
            return f"{n:.0f} {unit}" if unit == "B/s" else f"{n:.1f} {unit}"
        n /= 1024


# ---------- 本机入口（多代理分流） ----------

class HttpServer:
    """127.0.0.1:listen_port 的本机代理入口：不解协议，按池逐台拨上游，接通后
    双向搬字节——CONNECT 与明文 GET 都是上游认识的字节流，零解析即全兼容。
    系统代理指向这里，流量才经过本工具，分流/故障切换才有可能。"""

    def __init__(self, listen_port: int, pool: UpstreamPool):
        self.listen_port = listen_port
        self.pool = pool   # 换池对外部生效（set 后新连接用新池），运行中可热切
        self._srv = None
        self._on = False
        self._lock = threading.Lock()
        self.active_conns = 0   # 当前 TCP 连接总数
        self.stats = {}         # 上游 -> {"conns","bd","bu"}，锁内更新；换池后旧条目保留供 UI 显示
        self._live = set()      # 活跃连接对 (client, up)，stop 时强制断开用

    def start(self):
        srv = socket.socket()
        try:
            srv.bind(("127.0.0.1", self.listen_port))  # 只服务本机；绑 0.0.0.0 等于给局域网开免费通道
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
        # 已建立的连接也要掐断（同 RelayServer：keep-alive 长连接不会自己退）
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
                break
            threading.Thread(target=self._pump, args=(client,), daemon=True).start()

    def _pump(self, client):
        with self._lock:
            self.active_conns += 1
        try:
            t_pending = (client,)
            with self._lock:
                self._live.add(t_pending)  # 拨上游前就登记，stop() 才掐得到在途连接
            try:
                up = None
                for cand in self.pool.order():
                    try:
                        up = socket.create_connection(cand, timeout=5)
                    except OSError:
                        self.pool.report_fail(cand)
                        continue
                    self.pool.report_ok(cand)
                    break
                if up is None:
                    client.close()  # 池里全连不上，别让客户端 socket 干等
                    return
            finally:
                with self._lock:
                    self._live.discard(t_pending)
            up.settimeout(None)  # create_connection 的 timeout 会残留到 recv，掐断 keep-alive
            pair = (client, up)
            with self._lock:
                self._live.add(pair)
                stopped = not self._on
                st = self.stats.setdefault(cand, {"conns": 0, "bd": 0, "bu": 0})
                st["conns"] += 1
            if stopped:
                client.close()
                up.close()
                return
            t1 = threading.Thread(target=self._pipe, args=(client, up, st, False), daemon=True)
            t2 = threading.Thread(target=self._pipe, args=(up, client, st, True), daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()
            with self._lock:
                self._live.discard(pair)
                st["conns"] -= 1
            client.close(); up.close()
        finally:
            with self._lock:
                self.active_conns -= 1

    def snapshot(self):
        """界面轮询用：(连接总数, {上游: (连接数, 累计下行, 累计上行)})"""
        with self._lock:
            return self.active_conns, {u: (st["conns"], st["bd"], st["bu"])
                                       for u, st in self.stats.items()}

    def _pipe(self, src, dst, st, down: bool):
        """同 RelayServer._pipe，改按上游入账；down=True 表示 dst 是客户端（下行）"""
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
                n = len(data)
                with self._lock:
                    if down:
                        st["bd"] += n
                    else:
                        st["bu"] += n
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass


# ---------- 应用设置（共享端口、SOCKS5、配置文件路径） ----------

def _read_json(path: str):
    """读 JSON 文件；打不开/坏了返回 None（调用方自己给默认）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_settings() -> dict:
    data = _read_json(SETTINGS_FILE)
    if data is None and not os.path.exists(SETTINGS_FILE):
        # V2 settings 还没写过 → 首启带上 V1 的共享端口/SOCKS 认证（entry_port 等新键走默认）
        data = _read_json(OLD_SETTINGS_FILE)
    if not isinstance(data, dict):
        data = {}
    port = data.get("share_port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        port = DEFAULT_SHARE_PORT
    socks_port = data.get("socks_port")
    if not isinstance(socks_port, int) or not 1 <= socks_port <= 65535:
        socks_port = DEFAULT_SOCKS_PORT
    entry_port = data.get("entry_port")
    if not isinstance(entry_port, int) or not 1 <= entry_port <= 65535:
        entry_port = DEFAULT_ENTRY_PORT
    user = data.get("socks_user")
    user = user.strip() if isinstance(user, str) else ""
    password = data.get("socks_pass")
    password = password if isinstance(password, str) else ""
    path = data.get("config_path")
    path = str(path).strip() if isinstance(path, str) else ""
    return {"share_port": port, "socks_port": socks_port, "entry_port": entry_port,
            "socks_user": user, "socks_pass": password, "config_path": path}


def save_settings(settings: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)  # 原子替换，写一半断电也不会损坏


# ---------- 配置读写（多套预设，兼容旧版单配置格式） ----------

def _valid_host_str(host: str) -> bool:
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


def _valid_upstream(ip: str, port: str) -> bool:
    """上游条目校验：地址合法 + 端口 1-65535（配置文件可能被手改，进池前把好关）"""
    if not (port.isascii() and port.isdecimal()):
        return False
    return _valid_host_str(ip) and 1 <= int(port) <= 65535


def _to_addr_pairs(upstreams: list) -> list:
    """配置里的 [[ip, 端口], ...]（字符串对）→ [(ip, int(端口)), ...]，非法条目滤掉"""
    out = []
    for u in upstreams:
        if isinstance(u, (list, tuple)) and len(u) == 2:
            ip, port = str(u[0]).strip(), str(u[1]).strip()
            if _valid_upstream(ip, port):
                out.append((ip, int(port)))
    return out


def validate_upstreams(pairs: list):
    """开代理前的池门槛校验：可用返回 None，否则返回人话错误（空池/坏条目都拦）"""
    if not pairs:
        return "上游池为空，请先添加至少一台上游"
    for ip, port in pairs:
        if not _valid_upstream(ip, str(port)):
            return f"上游 {ip}:{port} 格式不对（地址需 IP/主机名，端口 1-65535）"
    return None


def _parse_entry_addr(server: str):
    """注册表代理值 → 本机入口端口；非 127.0.0.1:端口 形态返回 None"""
    if not server.startswith("127.0.0.1:"):
        return None
    try:
        return int(server.split(":", 1)[1])
    except ValueError:
        return None


def _normalize(data) -> dict:
    """把任意版本的配置统一成 {"current": 名称, "share": bool,
    "presets": [{"name": 名, "type": "single"|"multi", "upstreams": [[ip, 端口], ...]}]}。
    旧版 {ip, port} 预设/顶层单配置自动迁移成单元素上游组；非法条目整条丢弃。
    type 缺失/非法按台数推断（1→single，≥2→multi）；type=single 却有 ≥2 台时
    改判 multi——绝不截断丢数据。"""
    if not isinstance(data, dict):
        data = {}
    presets = []
    for p in (data.get("presets") or []):
        if not isinstance(p, dict):
            continue
        ups = p.get("upstreams") if isinstance(p.get("upstreams"), list) else None
        if ups is None:  # 旧版 {ip, port} 单地址预设 → 迁移成单元素组
            ups = [[p.get("ip") or DEFAULT_IP, p.get("port") or DEFAULT_PORT]]
        entries = []
        for u in ups:
            if isinstance(u, (list, tuple)) and len(u) == 2:
                ip, port = str(u[0]).strip(), str(u[1]).strip()
                if _valid_upstream(ip, port):
                    entries.append([ip, port])
        if entries:
            ptype = p.get("type")
            if ptype not in ("single", "multi"):
                ptype = "single" if len(entries) == 1 else "multi"
            elif ptype == "single" and len(entries) >= 2:
                ptype = "multi"
            presets.append({"name": str(p.get("name") or "未命名"),
                            "type": ptype, "upstreams": entries})
    if not presets:  # 旧版顶层 {ip, port} 单配置，或文件损坏
        ip, port = str(data.get("ip") or DEFAULT_IP), str(data.get("port") or DEFAULT_PORT)
        default = [[ip, port]] if _valid_upstream(ip, port) else [[DEFAULT_IP, DEFAULT_PORT]]
        presets = [{"name": "默认", "type": "single", "upstreams": default}]
    current = str(data.get("current") or presets[0]["name"])
    if current not in [p["name"] for p in presets]:
        current = presets[0]["name"]
    return {"current": current, "share": bool(data.get("share")), "presets": presets}


def _config_candidates(custom: str) -> list:
    """读取配置的候选位置，按优先级：自定义路径 > V2 默认 > V1 目录（首启迁移源）> 项目目录旧位置"""
    paths = ([custom] if custom else []) + [CONFIG_FILE, OLD_CONFIG_FILE, LEGACY_CONFIG_FILE]
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


# 连接失败原因的人话映射（errno / Windows socket 错误码）
_CONN_ERR_HINTS = {
    10061: "连不上代理（代理没开，或地址/端口不对）",
    10060: "3 秒无响应（超时）",
    11001: "代理主机名解析不了（检查地址是否填对）",
    10054: "连接被重置（端口不对或被拦截）",
}


def _friendly_conn_error(reason) -> str:
    code = getattr(reason, "errno", None) or getattr(reason, "winerror", None)
    hint = _CONN_ERR_HINTS.get(code)
    if hint:
        return hint
    if "timed out" in str(reason).lower():  # socket.timeout 可能不带错误码
        return _CONN_ERR_HINTS[10060]
    return str(reason)


# 测试失败进池表/状态行的精简短词（2-4 字，固定列宽装得下）
_CONN_ERR_SHORT = {10061: "连不上", 10060: "超时", 11001: "解析失败", 10054: "被重置"}


def _short_conn_error(reason) -> str:
    """连接失败原因 → 精简短词；完整人话用 _friendly_conn_error（单代理状态框用）"""
    code = getattr(reason, "errno", None) or getattr(reason, "winerror", None)
    if code in _CONN_ERR_SHORT:
        return _CONN_ERR_SHORT[code]
    if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
        return "超时"
    return "失败"


def parse_upstream_line(text: str):
    """'IP:端口' 单行解析：按最后一个冒号拆（IPv6 字面量维持现有校验不支持）；
    任一环节不合法返回 None"""
    parts = text.strip().rsplit(":", 1)
    if len(parts) == 2 and _valid_upstream(parts[0].strip(), parts[1].strip()):
        return parts[0].strip(), parts[1].strip()
    return None


# ---------- 界面 ----------

class ProxyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        # 试验：全 ttk + clam 主题——Tk 内置主题里唯一尊重底色上标的（vista 忽略按钮
        # background），统一控件风格；代价是整体观感非 Windows 原生
        style = ttk.Style(root)
        style.theme_use("clam")
        self._bg = style.lookup("TFrame", "background")
        root.configure(background=self._bg)
        # 大按钮状态底色 + 悬停/按下微调（开关代理）
        style.configure("On.TButton", background="#ffd7d7")
        style.map("On.TButton", background=[("active", "#ffd0d0"), ("pressed", "#ffc4c4")])
        style.configure("Off.TButton", background="#d7f0d7")
        style.map("Off.TButton", background=[("active", "#cdeacd"), ("pressed", "#c0e3c0")])
        # 输入框校验红框
        style.configure("Invalid.TEntry", fieldbackground="#ffdcdc")
        root.title(APP_TITLE)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Escape>", lambda _e: self._cancel_add_mode() if self._adding else None)

        self.settings = load_settings()
        self.share_port = self.settings["share_port"]
        self.socks_port = self.settings["socks_port"]
        self._settings_win = None
        self._clients_win = None
        self._tray = None          # TrayIcon，创建成功后赋值；失败则无托盘
        self._tray_hinted = False  # "已藏到托盘"的气泡每次运行只提示一次
        self._fw_pending = False  # 正在等 UAC/防火墙规则写入，期间忽略重复点击
        self._test_gen = 0        # 测试轮次：地址变更后旧结果（含在途）作废
        self._adding = False      # 「新增」模式：填新预设期间，开关/共享/删除暂禁用
        self._ui_queue = queue.Queue()  # 工作线程→主线程的 UI 任务队列
        self._pump_ui_queue()
        cfg = load_config(self.settings["config_path"])
        self.presets = cfg["presets"]
        self.preset_var = tk.StringVar(value=cfg["current"])
        self._relay = None          # 运行中的 RelayServer（HTTP 透明转发）
        self._socks = None          # 运行中的 SocksServer（SOCKS5→HTTP 网关）
        self._share_cfg = cfg["share"]  # 共享开关的持久状态（跨启动恢复用）
        self._lan_hint = _primary_lan_ip() or "本机IP"
        self._bytes_prev = (0.0, 0, 0)   # 共享速率差分采样 (monotonic, 累计下行, 累计上行)
        self._dev_bytes_prev = {}        # 客户端列表 per-IP 速率采样 ip -> (t, 下行, 上行)
        self.entry_port = self.settings["entry_port"]
        self._pool = UpstreamPool(self._pool_addrs())  # 当前预设的上游组
        self._entry = None               # 运行中的 HttpServer（本机入口，多代理分流）
        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(root)
        frm.pack(padx=4, pady=4)

        ttk.Label(frm, text="预设").grid(row=0, column=0, sticky="e", **pad)
        preset_frm = ttk.Frame(frm)
        preset_frm.grid(row=0, column=1, sticky="w", **pad)
        self.preset_box = ttk.Combobox(preset_frm, textvariable=self.preset_var, width=16,
                                       values=[p["name"] for p in self.presets])
        self.preset_box.pack(side="left")
        self.preset_box.bind("<<ComboboxSelected>>", self.on_preset_selected)
        # padding 压到与输入框同高（clam 默认 padding=5 会让按钮比 23px 的 Combobox 胖一圈）
        self.save_preset_btn = ttk.Button(preset_frm, text="新增", width=6, padding=(5, 0),
                                          command=self.on_add_or_save)
        self.save_preset_btn.pack(side="left", padx=(4, 0))
        self.delete_preset_btn = ttk.Button(preset_frm, text="删除", width=4, padding=(5, 0),
                                            command=self.delete_preset)
        self.delete_preset_btn.pack(side="left", padx=(4, 0))

        # ---- 单/多两种形态占同一 grid 单元，切预设即切形态 ----
        # 单形态（V1 式）：地址端口两格直接编辑当前预设的那台上游（Task 4 接保存）
        single_frm = ttk.Frame(frm)
        single_frm.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(single_frm, text="上游地址").grid(row=0, column=0, sticky="e", **pad)
        self.ip_var = tk.StringVar()
        self.ip_entry = ttk.Entry(single_frm, textvariable=self.ip_var, width=22)
        self.ip_entry.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(single_frm, text="上游端口").grid(row=1, column=0, sticky="e", **pad)
        port_frm = ttk.Frame(single_frm)
        port_frm.grid(row=1, column=1, sticky="w", **pad)
        self.port_var = tk.StringVar()
        self.port_entry = ttk.Entry(port_frm, textvariable=self.port_var, width=15)
        self.port_entry.pack(side="left")
        # 回车：新增模式=存预设；平时单形态的编辑保存由 Task 4 接入
        for ent in (self.ip_entry, self.port_entry):
            ent.bind("<Return>", lambda _e: self.on_add_or_save() if self._adding else None)
        self.ip_var.trace_add("write", self._on_addr_changed)    # 编辑即校验+作废旧测试结果
        self.port_var.trace_add("write", self._on_addr_changed)

        # 多形态：单行 IP:端口 回车即入池；池表兼作状态表（速率/连接/测试分列）
        multi_frm = ttk.Frame(frm)
        multi_frm.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(multi_frm, text="添加上游").grid(row=0, column=0, sticky="e", **pad)
        add_frm = ttk.Frame(multi_frm)
        add_frm.grid(row=0, column=1, sticky="w", **pad)
        self.add_var = tk.StringVar()
        self.add_entry = ttk.Entry(add_frm, textvariable=self.add_var, width=22)
        self.add_entry.pack(side="left")
        self.add_entry.bind("<Return>", lambda _e: self.add_upstream())
        self.add_up_btn = ttk.Button(add_frm, text="添加", width=6, padding=(5, 0),
                                     command=self.add_upstream)
        self.add_up_btn.pack(side="left", padx=(4, 0))
        pool_frm = ttk.Frame(multi_frm)
        pool_frm.grid(row=1, column=1, sticky="w", **pad)
        self.pool_box = ttk.Treeview(pool_frm,
                                      columns=("addr", "down", "up", "conns", "test"),
                                      show="headings", height=4, selectmode="browse")
        for col, text, w, anchor in (("addr", "上游", 185, "w"), ("down", "↓", 62, "e"),
                                     ("up", "↑", 62, "e"), ("conns", "连接", 40, "e"),
                                     ("test", "测试", 70, "w")):
            self.pool_box.heading(col, text=text)
            self.pool_box.column(col, width=w, anchor=anchor, stretch=False)
        self.pool_box.pack(side="left")
        ttk.Button(pool_frm, text="移除", width=6, padding=(5, 0),
                   command=self.remove_upstream).pack(side="left", padx=(6, 0), anchor="n")
        self.single_frm, self.multi_frm = single_frm, multi_frm

        self.status_var = tk.StringVar()
        self.status_label = ttk.Label(frm, textvariable=self.status_var, foreground="#555")
        self.status_label.grid(row=4, column=0, columnspan=2, **pad)

        # 单形态速率行（入口运行中 2 秒差分，永不换行）；多形态的每台速率在池表列里
        self.entry_speed_var = tk.StringVar()
        self.entry_speed_label = ttk.Label(frm, textvariable=self.entry_speed_var,
                                           foreground="#555")
        self.entry_speed_label.grid(row=5, column=0, columnspan=2, sticky="w",
                                    padx=12, pady=(0, 2))
        self._up_bytes_prev = {}   # 上游 -> (t, 下行, 上行) 差分采样

        self.btn = ttk.Button(frm, width=18, command=self.toggle)
        self.btn.grid(row=6, column=0, columnspan=2, ipady=4, padx=12, pady=(2, 4))

        self.test_btn = ttk.Button(frm, text="测试连通", width=8, command=self.test_connectivity)
        self.test_btn.grid(row=7, column=0, sticky="w", padx=(12, 4), pady=(0, 6))
        self.test_result_var = tk.StringVar()
        self.test_result_label = ttk.Label(frm, textvariable=self.test_result_var, foreground="#555",
                                          wraplength=300)  # 长错误文本折行，别撑宽定宽窗口
        self.test_result_label.grid(row=7, column=1, sticky="w", padx=(0, 12), pady=(0, 6))
        self._on_addr_changed()  # 初次按载入的配置刷红框（配置 JSON 可能被手改坏）
        self._apply_form()       # 形态跟随当前预设（内部 set 再触发一次 trace，无害）
        self._refresh_pool_box()
        self._restore_entry()

        ttk.Separator(frm).grid(row=8, column=0, columnspan=2,
                                sticky="ew", padx=12, pady=(2, 2))  # 主功能/辅助设置分区

        self.autostart_var = tk.BooleanVar(value=get_autostart())
        ttk.Checkbutton(frm, text="开机自动启动", variable=self.autostart_var,
                       command=self.toggle_autostart).grid(
            row=9, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 6))

        ttk.Label(frm, text="局域网共享").grid(row=10, column=0, sticky="e", **pad)
        share_frm = ttk.Frame(frm)
        share_frm.grid(row=10, column=1, sticky="w", **pad)
        self.share_btn = ttk.Button(share_frm, text="开启共享", width=8, command=self.toggle_share)
        self.share_btn.pack(side="left")
        ttk.Button(share_frm, text="设置", width=4, command=self.open_settings).pack(side="left", padx=(6, 0))
        ttk.Button(share_frm, text="客户端", width=6, command=self.open_clients).pack(side="left", padx=(6, 0))
        self.share_status_var = tk.StringVar()
        self.share_status_label = ttk.Label(frm, textvariable=self.share_status_var, foreground="#555",
                                           justify="left", anchor="w",
                                           wraplength=300)  # 共享中的双通道长文案折行，别撑宽窗口
        self.share_status_label.grid(row=11, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10))
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
        if self._adding:  # 新增模式里改选预设 = 放弃新增，恢复按钮
            self._exit_add_mode(restore=False)
        self._apply_form()
        self._refresh_pool_box()
        self._sync_relay_upstream()

    def _apply_form(self):
        """界面形态跟随当前预设类型：单=两格直接编辑，多=单行添加+池表。
        切形态时窗口高度变化是预期行为（布局变化），流量刷新不再撑动窗口。"""
        single = self._current_preset()["type"] == "single"
        if single:
            self.single_frm.grid()
            self.multi_frm.grid_remove()
            self.entry_speed_label.grid()
            ip, port = (self._pool_addrs() or [("", "")])[0]
            self.ip_var.set(ip)          # set 会触发 trace：红框复位、旧测试结果作废
            self.port_var.set(str(port))
        else:
            self.single_frm.grid_remove()
            self.multi_frm.grid()
            self.entry_speed_label.grid_remove()
            self.entry_speed_var.set("")
            self.ip_var.set("")          # 多形态两格隐藏，清空防编辑语义混用
            self.port_var.set("")
        self.add_var.set("")

    def _restore_entry(self):
        """开机自启后注册表还指着本机入口：把入口拉起来；池坏或端口被占就摘注册表，
        绝不挂着"已开启"的名头让全机流量指向没人听的端口"""
        enabled, server = get_system_proxy()
        if not enabled or _parse_entry_addr(server) != self.entry_port:
            return
        addrs = self._pool_addrs()
        if validate_upstreams(addrs):
            disable_system_proxy()  # 池空/坏：入口没法有意义地跑，先恢复直连
            return
        try:
            self._pool.set(addrs)
            self._entry = HttpServer(self.entry_port, self._pool)
            self._entry.start()
        except OSError:
            self._entry = None
            disable_system_proxy()

    # ---- 上游池 ----

    def _refresh_pool_box(self):
        """池表跟随当前预设重画；iid 用索引，移除时好对位"""
        self.pool_box.delete(*self.pool_box.get_children())
        for i, (ip, port) in enumerate(self._pool_addrs()):
            self.pool_box.insert("", "end", iid=str(i),
                                 values=(f"{ip}:{port}", "", "", "", ""))

    def add_upstream(self):
        """把单行输入框的 IP:端口 加进当前预设的池（重复的不加），入口热切新连接生效"""
        parsed = parse_upstream_line(self.add_var.get())
        if not parsed:
            messagebox.showwarning("提示", "格式：IP:端口（如 192.130.30.73:7890）",
                                   parent=self.root)
            return
        ip, port = parsed
        cur = self._current_preset()
        if [ip, port] not in cur["upstreams"]:
            cur["upstreams"].append([ip, port])
        self.add_var.set("")
        self._test_gen += 1          # 池变了，旧测试结果作废
        self.test_result_var.set("")
        self._refresh_pool_box()
        self._sync_pools()
        self.save_preset()

    def remove_upstream(self):
        """移除池中选中的一台；最后一家不许删（删空就没法分流了）"""
        sel = self.pool_box.selection()
        if not sel:
            return
        cur = self._current_preset()
        if len(cur["upstreams"]) <= 1:
            messagebox.showinfo("提示", "至少要保留一台上游", parent=self.root)
            return
        del cur["upstreams"][int(sel[0])]
        self._test_gen += 1
        self.test_result_var.set("")
        # 在途的逐台测试被作废时按钮可能永远停在"测试中…"——gen 变化的源头顺手恢复它
        self.test_btn.config(state="normal", text="测试连通")
        self._refresh_pool_box()
        self._sync_pools()
        self.save_preset()

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
        self.ip_var.set("")
        self.port_var.set("")
        self._sync_preset_box()
        self._refresh_pool_box()
        self._sync_relay_upstream()  # 共享运行中也要跟上切换后的地址
        self.save_preset()

    def save_preset(self, ups=None) -> bool:
        """把上游组存进当前名字的预设（名字是新的则新建）；ups 缺省=当前池。返回是否落盘成功"""
        if ups is None:
            ups = [[ip, str(port)] for ip, port in self._pool_addrs()]
        name = self.preset_var.get().strip()
        if not name:
            first = ups[0] if ups else ["", ""]
            name = f"{first[0]}:{first[1]}" if first[0] else "默认"
        for p in self.presets:
            if p["name"] == name:
                p["upstreams"] = ups
                break
        else:
            self.presets.append({"name": name,
                                 "type": "single" if len(ups) == 1 else "multi",
                                 "upstreams": ups})
            self._sync_preset_box()
        self.preset_var.set(name)
        try:
            save_config(name, self.presets, self._share_cfg, self.settings["config_path"])
        except OSError as e:
            messagebox.showerror("错误", f"保存配置失败：\n{e}", parent=self.root)
            return False
        return True

    def on_add_or_save(self):
        """按钮双态：常态「新增」进入新增模式；新增模式「存预设」校验+落盘并退出"""
        if self._adding:
            if self._valid_inputs():
                ip, port = self.ip_var.get().strip(), self.port_var.get().strip()
                if self.save_preset([[ip, port]]):  # 新预设从输入框这一台起步
                    self._refresh_pool_box()
                    self._exit_add_mode(restore=False)
        else:
            self._enter_add_mode()

    def _enter_add_mode(self):
        self._adding = True
        self._add_backup = (self.preset_var.get(), self.ip_var.get(), self.port_var.get())
        self.preset_var.set("")  # 名字也清空（留空保存时自动用 ip:端口 当名字）
        self.ip_var.set("")
        self.port_var.set("")    # 置空即触发 trace：红框复位、旧测试结果作废
        for w in (self.btn, self.share_btn, self.add_up_btn):
            w.config(state="disabled")  # 半成品地址不能应用/共享，也不能加进别家预设的池
        # 「删除」临时变「取消」：不新增按钮，布局零变化（删除在新增模式里本来就不可用）
        self.delete_preset_btn.config(text="取消", command=self._cancel_add_mode)
        self.save_preset_btn.config(text="存预设")
        self.ip_entry.focus_set()

    def _exit_add_mode(self, restore: bool):
        self._adding = False
        self.save_preset_btn.config(text="新增")
        self.delete_preset_btn.config(text="删除", command=self.delete_preset)
        for w in (self.btn, self.share_btn, self.add_up_btn):
            w.config(state="normal")
        if restore:  # 取消/退出：还原进来之前的样子，别把半成品留下
            name, ip, port = self._add_backup
            self.preset_var.set(name)
            self.ip_var.set(ip)
            self.port_var.set(port)
        self._sync_relay_upstream()

    def _cancel_add_mode(self):
        self._exit_add_mode(restore=True)

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

    def _share_snapshot(self):
        """HTTP 转发 + SOCKS5 两路合并的 (连接总数, 设备列表, (累计下行字节, 累计上行字节))"""
        conns, devs, total = self._relay.snapshot() if self._relay else (0, [], (0, 0))
        if self._socks:
            c2, d2, t2 = self._socks.snapshot()
            devs = _merge_devs(devs, d2)
            conns += c2
            total = (total[0] + t2[0], total[1] + t2[1])
        devs.sort(key=lambda x: -x[1])
        return conns, devs, total

    def _speed_text(self, total) -> str:
        """按与上次 2 秒采样的差值算速率；首采样或共享重启（计数清零）返回空"""
        now = time.monotonic()
        pt, pd, pu = self._bytes_prev
        self._bytes_prev = (now, total[0], total[1])
        dt = now - pt
        if pt == 0.0 or dt <= 0 or total[0] < pd or total[1] < pu:
            return ""
        return f"↓ {_fmt_speed(max(0, total[0] - pd) / dt)} ↑ {_fmt_speed(max(0, total[1] - pu) / dt)}"

    def _refresh_share_status(self):
        """共享运行中：状态行显示双通道地址 + 设备/连接/速率 + 最近 SOCKS 失败"""
        if not self._relay:
            return
        conns, devs, total = self._share_snapshot()
        n_dev = sum(1 for d in devs if d[2] <= ACTIVE_WINDOW_SEC)
        up = self._relay.upstream
        tail = f"近10分钟设备 {n_dev} 台，当前连接 {conns} 个" if devs or conns else "暂无设备连接"
        speed = self._speed_text(total)
        if speed:
            tail += f" ｜ {speed}"
        auth = f"账号 {self.settings['socks_user']}" if self.settings["socks_user"] else "无认证"
        text = (f"✓ 共享中\n"
                f"HTTP: {self._lan_hint}:{self.share_port}\n"
                f"SOCKS5: {self._lan_hint}:{self.socks_port}（{auth}）\n"
                f"转发到 {up[0]}:{up[1]} ｜ {tail}")
        if self._socks and self._socks.last_error:
            text += f"\n最近 SOCKS 失败：{self._socks.last_error}"
        self._set_share_status(text, color="#1a7f37")

    def refresh_status(self):
        self.enabled, server = get_system_proxy()
        if self.enabled:
            if server.startswith("127.0.0.1:"):  # 指向本机入口
                n = len(self._pool_addrs())
                server = (f"本机入口 {server}" if self._current_preset()["type"] == "single"
                          else f"本机入口 {server}（{n} 台上游）")
            self.status_var.set(f"● 已开启  {server}")
            self.status_label.config(foreground="#1a7f37")
            self.btn.config(text="关闭代理", style="On.TButton")
        else:
            self.status_var.set("● 已关闭")
            self.status_label.config(foreground="#555")
            self.btn.config(text="开启代理", style="Off.TButton")
        self._entry_status_text()

    def _entry_status_text(self):
        """入口速率/连接分形态展示：多形态写池表各列（按列 set，不覆测试列），
        单形态写一行速率；入口没跑就清空"""
        if not self._entry:
            self.entry_speed_var.set("")
            return
        if self._entry.listen_port != self.entry_port:
            return  # 「端口已改待重开」的红字提示别被 2 秒轮询顶掉
        _, stats = self._entry.snapshot()
        now = time.monotonic()
        single = self._current_preset()["type"] == "single"
        addrs = self._pool_addrs()
        iid_of = {up: str(i) for i, up in enumerate(addrs)}
        first = addrs[0] if addrs else None
        for up, (c, bd, bu) in stats.items():
            prev = self._up_bytes_prev.get(up)
            self._up_bytes_prev[up] = (now, bd, bu)
            d_text = u_text = ""
            if prev and bd >= prev[1] and bu >= prev[2] and now > prev[0]:
                dt = now - prev[0]
                d_text = _fmt_speed((bd - prev[1]) / dt)
                u_text = _fmt_speed((bu - prev[2]) / dt)
            if single:
                if up == first:  # 单形态只关心那一台
                    parts = [t for t in (f"↓ {d_text}" if d_text else "",
                                         f"↑ {u_text}" if u_text else "",
                                         f"{c}连" if c else "") if t]
                    self.entry_speed_var.set("  ".join(parts))
            else:
                iid = iid_of.get(up)
                if iid and self.pool_box.exists(iid):
                    self.pool_box.set(iid, "down", d_text)
                    self.pool_box.set(iid, "up", u_text)
                    self.pool_box.set(iid, "conns", str(c) if c else "")

    @staticmethod
    def _parse_port(text: str):
        """端口文本 -> int 或 None。只认 ASCII 数字：'²' 这类 isdigit() 为真的字符会让 int() 炸"""
        text = text.strip()
        if text.isascii() and text.isdecimal():
            port = int(text)
            if 1 <= port <= 65535:
                return port
        return None

    def _on_addr_changed(self, *_):
        """地址/端口被编辑（含切预设的联动写入）：即时红框校验 + 旧测试结果作废"""
        ip, port = self.ip_var.get().strip(), self.port_var.get().strip()
        ip_ok = not ip or self._valid_host(ip)  # 空=还没填，不算错
        port_ok = not port or self._parse_port(port) is not None
        self.ip_entry.config(style="TEntry" if ip_ok else "Invalid.TEntry")
        self.port_entry.config(style="TEntry" if port_ok else "Invalid.TEntry")
        self._test_gen += 1
        self.test_result_var.set("")
        # 在途的逐台测试被作废时按钮可能永远停在"测试中…"——gen 变化的源头顺手恢复它
        self.test_btn.config(state="normal", text="测试连通")

    @staticmethod
    def _valid_host(host: str) -> bool:
        """代理地址格式检查（校验逻辑已提取到模块级 _valid_host_str 共用）"""
        return _valid_host_str(host)

    def _valid_inputs(self) -> bool:
        ip = self.ip_var.get().strip()
        if (ip and self._valid_host(ip)
                and self._parse_port(self.port_var.get()) is not None):
            return True
        messagebox.showwarning("提示", "请填写正确的代理地址（IP 或主机名）和端口（1-65535）",
                               parent=self.root)
        return False

    def _pool_addrs(self) -> list:
        """当前预设的上游组 → [(ip, int(端口)), ...]"""
        return _to_addr_pairs(self._current_preset()["upstreams"])

    def _read_upstream(self):
        """共享通道跟随的上游 = 池[0]（与 V1 行为等价：单一确定目标）"""
        addrs = self._pool_addrs()
        return addrs[0] if addrs else None

    def _sync_pools(self):
        """入口池与共享上游始终跟随当前预设：入口热切对新连接生效，无需重启"""
        if self._entry:
            self._entry.pool.set(self._pool_addrs())
        up = self._read_upstream()
        if up and self._relay:
            self._relay.upstream = up
        if up and self._socks:
            self._socks.upstream = up

    def _sync_relay_upstream(self):
        """转发目标跟随逻辑已并入 _sync_pools；保留旧名给既有调用点"""
        self._sync_pools()

    def toggle(self):
        self.apply(not self.enabled)

    def apply(self, enable: bool):
        if enable:
            addrs = self._pool_addrs()
            err = validate_upstreams(addrs)  # 空池/坏条目都拦，注册表不写
            if err:
                messagebox.showwarning("提示", err, parent=self.root)
                return
            self._pool.set(addrs)
            if self._entry is None:  # 入口只在缺时起；改入口端口后重开一次开关生效
                try:
                    self._entry = HttpServer(self.entry_port, self._pool)
                    self._entry.start()
                except OSError as e:
                    self._entry = None
                    messagebox.showerror(
                        "错误", f"入口端口 {self.entry_port} 起不来（被其他程序占用？），"
                                f"可在设置的共享页改入口端口：\n{e}", parent=self.root)
                    return
            try:
                set_system_proxy(f"127.0.0.1:{self.entry_port}")
            except OSError as e:
                messagebox.showerror("错误", f"操作失败：\n{e}", parent=self.root)
                return
            self.save_preset()  # 记住这次用的上游组
        else:
            try:
                disable_system_proxy()
            except OSError as e:
                messagebox.showerror("错误", f"操作失败：\n{e}", parent=self.root)
                return
            if self._entry:  # 先摘注册表再停监听，断流窗口最小
                self._entry.stop()
                self._entry = None
        self._sync_pools()
        self.refresh_status()

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
        except OSError as e:
            self.autostart_var.set(not self.autostart_var.get())  # 回退勾选状态
            messagebox.showerror("错误", f"设置开机自启失败：\n{e}", parent=self.root)

    def test_connectivity(self):
        """对池内每台上游逐台请求 generate_204，分列显示结果"""
        addrs = self._pool_addrs()
        if not addrs:
            messagebox.showwarning("提示", "上游池为空，请先添加上游", parent=self.root)
            return
        self.test_btn.config(state="disabled", text="测试中…")
        self.test_result_var.set("正在测试…")
        self.test_result_label.config(foreground="#555")
        gen = self._test_gen
        self._test_results = {}   # "ip:port" -> (ok, 文本)，收齐 total 条才恢复按钮
        for ip, port in addrs:
            threading.Thread(target=self._test_worker, args=(ip, port, gen, len(addrs)),
                             daemon=True).start()

    def _test_worker(self, ip: str, port: int, gen: int, total: int):
        proxies = {"http": f"http://{ip}:{port}", "https": f"http://{ip}:{port}"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        start = time.perf_counter()
        try:
            opener.open(TEST_URL, timeout=3)
        except Exception as e:  # 超时、拒绝连接等统一按失败展示
            reason = getattr(e, "reason", None) or e
            ok, info = False, _friendly_conn_error(reason)
        else:
            ok, info = True, f"{round((time.perf_counter() - start) * 1000)} ms"
        self._ui(lambda: self._show_test_result(gen, f"{ip}:{port}", ok, info, total))

    def _show_test_result(self, gen: int, name: str, ok: bool, info: str, total: int):
        if gen != self._test_gen:  # 结果到达前池已被改动：丢弃（按钮由收齐的末条恢复）
            return
        self._test_results[name] = (ok, info)
        if len(self._test_results) < total:
            return
        self.test_btn.config(state="normal", text="测试连通")
        self.test_result_var.set(" ｜ ".join(f"{n} {'✓' if ok else '✗'} {t}"
                                             for n, (ok, t) in self._test_results.items()))
        self.test_result_label.config(foreground="#1a7f37"
                                      if all(ok for ok, _ in self._test_results.values())
                                      else "#c62828")

    # ---- 局域网共享 ----

    def _share_off_text(self, port=None) -> str:
        return (f"未开启：开启后其他设备把 HTTP 代理填成 {self._lan_hint}:{port or self.share_port}"
                f"，SOCKS5 填 {self._lan_hint}:{self.socks_port}")

    def _set_share_status(self, text: str, color: str = "#555"):
        self.share_status_var.set(text)
        self.share_status_label.config(foreground=color)

    def toggle_share(self):
        if self._relay or self._socks:
            if self._relay:
                self._relay.stop()
                self._relay = None
            if self._socks:
                self._socks.stop()
                self._socks = None
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
        port, socks_port = self.share_port, self.socks_port
        has_rule = (firewall_rule_exists(port)
                    and firewall_rule_exists(socks_port, socks=True))

        def proceed():
            if (port, socks_port) != (self.share_port, self.socks_port):  # 检查期间端口被改：作废重来
                self._restart_fw_check(lan)
                return
            if has_rule:
                self._fw_pending = False
                self._start_share(up, lan)
                return
            if not messagebox.askyesno(
                    "局域网共享",
                    f"首次开启需要在防火墙放行 TCP 端口 {port} 和 {socks_port}\n"
                    "（会弹出管理员确认窗口，请选“是”；只对本机所在子网放行）。\n现在添加吗？",
                    parent=self.root):
                self._fw_pending = False
                self._set_share_status(self._share_off_text(port))
                return
            self._set_share_status("正在添加防火墙规则…请在弹出的窗口点“是”")
            threading.Thread(target=self._fw_then_start,
                             args=(up, lan, port, socks_port), daemon=True).start()

        self._ui(proceed)

    def _fw_then_start(self, up, lan, port, socks_port):
        # 只加缺失的规则（netsh 同名重复添加会留重复条目）
        rules = [(p, s) for p, s in ((port, False), (socks_port, True))
                 if not firewall_rule_exists(p, socks=s)]
        ok = add_firewall_rule_elevated(rules) if rules else True
        rule_ok = False
        for _ in range(8):  # netsh 是异步的，等规则真正写入（最多约 3 秒）
            if not ok:  # UAC 被取消/失败，别再白跑 8 轮 netsh
                break
            rule_ok = (firewall_rule_exists(port)
                       and firewall_rule_exists(socks_port, socks=True))
            if rule_ok:
                break
            time.sleep(0.4)

        def finish():
            if (port, socks_port) != (self.share_port, self.socks_port):
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
        try:
            socks = SocksServer(self.socks_port, up,
                                self.settings["socks_user"], self.settings["socks_pass"])
            socks.start()
        except OSError as e:
            relay.stop()  # HTTP 通道起来了也得退回，两通道同进同退
            self._fw_pending = False
            self._set_share_status(f"✗ SOCKS5 端口 {self.socks_port} 开启失败：{e}", color="#c62828")
            return
        self._relay, self._socks = relay, socks
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
        port, socks_port = self.share_port, self.socks_port
        has_rule = (firewall_rule_exists(port)
                    and firewall_rule_exists(socks_port, socks=True))

        def finish():
            self._fw_pending = False
            if self._relay or self._socks:  # 恢复期间用户已手动开启，再开就撞端口（假报错）
                return
            if (port, socks_port) != (self.share_port, self.socks_port):  # 期间改了端口，检查已过时
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
        win.configure(background=self._bg)  # clam 底色，别让窗口边露白
        win.title("设置")
        win.resizable(False, False)
        win.transient(self.root)
        frm = ttk.Frame(win)
        frm.pack(padx=14, pady=10)
        pad = {"padx": 8, "pady": 5}

        ttk.Label(frm, text="入口端口").grid(row=0, column=0, sticky="e", **pad)
        entry_var = tk.StringVar(value=str(self.entry_port))
        ttk.Entry(frm, textvariable=entry_var, width=12).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(frm, text="共享监听端口").grid(row=1, column=0, sticky="e", **pad)
        port_var = tk.StringVar(value=str(self.share_port))
        ttk.Entry(frm, textvariable=port_var, width=12).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(frm, text="SOCKS5 端口").grid(row=2, column=0, sticky="e", **pad)
        socks_var = tk.StringVar(value=str(self.socks_port))
        ttk.Entry(frm, textvariable=socks_var, width=12).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(frm, text="SOCKS5 用户名").grid(row=3, column=0, sticky="e", **pad)
        user_var = tk.StringVar(value=self.settings["socks_user"])
        ttk.Entry(frm, textvariable=user_var, width=12).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(frm, text="SOCKS5 密码").grid(row=4, column=0, sticky="e", **pad)
        pass_var = tk.StringVar(value=self.settings["socks_pass"])
        ttk.Entry(frm, textvariable=pass_var, width=12, show="*").grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(frm, text="配置文件路径").grid(row=5, column=0, sticky="e", **pad)
        path_var = tk.StringVar(value=self.settings["config_path"] or CONFIG_FILE)
        ttk.Entry(frm, textvariable=path_var, width=36).grid(row=5, column=1, sticky="w", **pad)

        def browse():
            picked = filedialog.asksaveasfilename(
                parent=win, title="选择配置文件位置", defaultextension=".json",
                initialfile=os.path.basename(path_var.get()) or "proxy_config.json")
            if picked:
                path_var.set(os.path.normpath(picked))

        ttk.Button(frm, text="浏览", command=browse).grid(row=5, column=2, padx=(0, 8))

        ttk.Label(frm, text="入口端口被占用（如 Clash）时换一个；改了入口端口需重新点一次“开关代理”；\n切换路径时会把当前配置复制到新位置；\n改了任一端口或 SOCKS5 认证后需重新点一次“开启共享”；\nSOCKS5 用户名/密码留空 = 无认证。",
                 foreground="#888", justify="left").grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=3, pady=(2, 8))

        def on_save():
            entry_port = self._parse_port(entry_var.get())
            if entry_port is None:
                messagebox.showwarning("提示", "入口端口需为 1-65535 的数字", parent=win)
                return
            port = self._parse_port(port_var.get())
            if port is None:
                messagebox.showwarning("提示", "共享端口需为 1-65535 的数字", parent=win)
                return
            socks_port = self._parse_port(socks_var.get())
            if socks_port is None:
                messagebox.showwarning("提示", "SOCKS5 端口需为 1-65535 的数字", parent=win)
                return
            if socks_port == port:
                messagebox.showwarning("提示", "SOCKS5 端口不能和共享端口相同", parent=win)
                return
            if entry_port == port or entry_port == socks_port:
                messagebox.showwarning("提示", "入口端口不能和共享/SOCKS5 端口相同", parent=win)
                return
            new_path = self._normalize_config_path(path_var.get())
            old_path = self.settings["config_path"] or CONFIG_FILE
            changed = os.path.normcase(new_path) != os.path.normcase(old_path)
            if changed and os.path.exists(new_path):
                if not messagebox.askyesno("提示", "新路径已有配置文件，用当前配置覆盖它？", parent=win):
                    return
            port_changed = (port != self.share_port or socks_port != self.socks_port
                            or user_var.get().strip() != self.settings["socks_user"]
                            or pass_var.get() != self.settings["socks_pass"])
            new_settings = {"share_port": port, "socks_port": socks_port,
                            "entry_port": entry_port,
                            "socks_user": user_var.get().strip(),
                            "socks_pass": pass_var.get(),
                            "config_path": new_path if changed else old_path}
            try:
                save_settings(new_settings)
            except OSError as e:
                messagebox.showerror("错误", f"保存设置失败：\n{e}", parent=win)
                return  # 写盘失败就别动内存状态，免得界面和文件分叉
            self.share_port, self.socks_port = port, socks_port
            self.settings = new_settings
            if entry_port != self.entry_port:
                self.entry_port = entry_port
                if self._entry:  # 入口在跑：旧监听还占着旧端口，重开开关才切到新端口
                    self.test_result_var.set("入口端口已改，重新点一次开关代理生效")
                    self.test_result_label.config(foreground="#c62828")
            if changed:
                self.save_preset()  # 立刻把当前配置写到新位置
            if port_changed:
                if self._relay or self._socks:  # 端口/认证变了，旧监听作废
                    if self._relay:
                        self._relay.stop()
                        self._relay = None
                    if self._socks:
                        self._socks.stop()
                        self._socks = None
                    self.share_btn.config(text="开启共享")
                    self._share_cfg = False  # 共享已停，别把"开着"残留进配置
                    self.save_preset()
                    self._set_share_status(
                        "共享端口/SOCKS5 设置已改动，请重新点“开启共享”", color="#c62828")
                else:
                    self._set_share_status(self._share_off_text())
            win.destroy()

        ttk.Button(btns, text="保存", width=8, command=on_save).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", width=8, command=win.destroy).pack(side="left")
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
        """共享客户端列表小窗：每台设备的 IP、当前连接数、实时速率、最后活跃时间"""
        if self._clients_win is not None and self._clients_win.winfo_exists():
            self._clients_win.lift()
            return
        win = tk.Toplevel(self.root)
        self._clients_win = win
        win.configure(background=self._bg)  # clam 底色，别让窗口边露白
        win.title("共享客户端")
        win.resizable(False, False)
        win.transient(self.root)
        tv = ttk.Treeview(win, columns=("ip", "conns", "down", "up", "last"), show="headings", height=8)
        for col, text, width in (("ip", "设备 IP", 130), ("conns", "连接", 50),
                                 ("down", "↓ 速率", 85), ("up", "↑ 速率", 85),
                                 ("last", "最后活跃", 80)):
            tv.heading(col, text=text)
            tv.column(col, width=width, anchor="w" if col == "ip" else "center")
        tv.pack(padx=10, pady=(10, 4))
        summary = ttk.Label(win, text="", foreground="#555")
        summary.pack(padx=10, pady=(0, 10))

        def refresh():
            if not win.winfo_exists():
                return
            tv.delete(*tv.get_children())
            if self._relay or self._socks:
                conns, devs, _total = self._share_snapshot()
                now = time.monotonic()
                prev = self._dev_bytes_prev
                seen = set()
                for ip, c, sec, bd, bu in devs:
                    seen.add(ip)
                    old = prev.get(ip)
                    if old and bd >= old[1] and bu >= old[2] and now > old[0]:
                        down = (bd - old[1]) / (now - old[0])
                        ups = (bu - old[2]) / (now - old[0])
                    else:  # 首次见到，或共享重启后计数清零
                        down = ups = 0.0
                    prev[ip] = (now, bd, bu)
                    tv.insert("", "end", values=(ip, c, _fmt_speed(down), _fmt_speed(ups),
                                                 f"{sec} 秒前"))
                for ip in [k for k in prev if k not in seen]:
                    del prev[ip]
                n_dev = sum(1 for d in devs if d[2] <= ACTIVE_WINDOW_SEC)
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
        # 本方法在主线程安全点执行（_ui 入队到达），原生菜单的选中由 Windows
        # 派发，点中的回调直接在这里执行即可
        if self._tray is None:
            return
        cb = self._tray.show_menu([
            ("打开主窗口", self._do_tray_restore),
            ("-", None),
            ("退出", self.quit_app),
        ])
        if cb:
            cb()

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
        if self._adding:  # 新增模式里退出：先还原字段，别把半成品存成预设
            self._exit_add_mode(restore=True)
        if self._relay:
            self._relay.stop()  # 只停转发；共享开关状态保留，下次启动自动恢复
        if self._socks:
            self._socks.stop()
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

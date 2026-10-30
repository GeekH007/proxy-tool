# -*- coding: utf-8 -*-
"""Windows 系统代理小工具：小窗口控制代理开关，设置 IP 和端口，自动记住配置。"""
import ctypes
from ctypes import wintypes
import copy
import http.client
import ipaddress
import json
import logging
import logging.handlers
import math
import os
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import tempfile
import time
import traceback
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
import winreg
import zipfile
import weakref

PROXY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSET_DIR = os.path.join(_BASE_DIR, "assets")
_ICON_SIZES = (256, 48, 32, 16)
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
APP_VERSION = "V2.1"        # 稳定性增强版；显示版本与配置/自启标识分离
APP_TITLE = f"系统代理工具 {APP_VERSION}"
AUTOSTART_VALUE_NAME = "系统代理工具 V2.0"  # 保留历史 Run 项，升级不丢失自启状态
MUTEX_NAME = "MyXm_ProxyTool_SingleInstance"
TEST_TARGETS = (
    ("Google HTTPS", "www.google.com", "/generate_204"),
    ("Gstatic HTTPS", "www.gstatic.com", "/generate_204"),
)
TEST_TIMEOUT_SECS = 6  # 仅用于连通性探测，不改变转发拨号超时
DIAL_TIMEOUT_SECS = 3   # 拨上游限时：本机→上游 RTT 低，超时短促让故障切换不干等
TOTAL_DIAL_TIMEOUT_SECS = 6  # 整个上游选择过程的预算，含 DNS 和所有候选地址
UPSTREAM_CONNECT_TIMEOUT_SECS = 35  # 上游默认外网建连 30 秒，另留响应传输余量
PIPE_BUFFER_SIZE = 65536  # 单方向复用的搬运缓冲区；实测未支持继续增大
MAX_FORWARD_CONNECTIONS = 256  # 三个入口合计，限制线程/缓冲区占用
_FORWARD_SLOTS = threading.BoundedSemaphore(MAX_FORWARD_CONNECTIONS)
LOCAL_RESERVED_CONNECTIONS = 64
MAX_SHARED_PER_CLIENT = 96
_SHARED_SLOTS = threading.BoundedSemaphore(MAX_FORWARD_CONNECTIONS - LOCAL_RESERVED_CONNECTIONS)
_SHARED_CLIENTS = {}
_SHARED_LOCK = threading.Lock()
ACCEPT_BACKLOG = 128    # listen 积压：浏览器/多设备瞬时并发不丢 SYN 触发重传
REFRESH_INTERVAL_MS = 2000
ACTIVE_WINDOW_SEC = 600   # “近10分钟活跃设备”的统计窗口
DEVICE_TTL_SEC = 3600     # 设备记录保留时长，超时清理

_mutex_handle = None  # 持有互斥量句柄防止被回收，进程退出时系统自动释放


# ---------- 有界异步诊断；不记录请求、目标地址、认证信息或异常原文 ----------

_DIAGNOSTIC_MAX_BYTES = 2 * 1024 * 1024
_DIAGNOSTIC_BACKUPS = 3
_DIAGNOSTIC_QUEUE_SIZE = 2048
_DIAGNOSTICS = None
_DIAGNOSTICS_LOCK = threading.Lock()
_DIAGNOSTICS_ERROR = ""
_DIAGNOSTICS_DIRECTORY = ""
_DIAGNOSTIC_TEXT_FIELDS = {
    "stage": {"accept", "listen", "worker", "dial", "handshake", "connect",
              "recv", "send", "pipe", "probe", "health", "startup", "shutdown"},
    "server": {"http", "relay", "socks"},
    "direction": {"up", "down"},
    "result": {"ok", "failed", "recovered", "disabled"},
    "state": {"unknown", "healthy", "suspect", "degraded", "recovering"},
}
_DIAGNOSTIC_NUMBER_FIELDS = {
    "errno", "winerror", "elapsed_ms", "attempts", "active", "limit", "rejected",
    "count", "failures", "successes", "cooldown_ms", "bytes", "status", "rep", "dropped",
}
_DIAGNOSTIC_ERROR_TYPES = {"timeout", "refused", "reset", "dns", "io", "protocol", "internal",
                           "certificate", "tls", "http", "tunnel", "probe_failed"}
_DIAGNOSTIC_CHECK_LABELS = {"Google HTTPS": "google_https", "Gstatic HTTPS": "gstatic_https"}


def _diagnostic_clean_checks(checks):
    result = []
    if not isinstance(checks, list):
        return result
    for item in checks[:2]:
        if not isinstance(item, dict):
            continue
        label, kind = item.get("target"), item.get("error")
        if (not isinstance(label, str) or label not in _DIAGNOSTIC_CHECK_LABELS.values()
                or not isinstance(kind, str) or kind not in _DIAGNOSTIC_ERROR_TYPES):
            continue
        check = {"target": label, "error": kind}
        for key in ("errno", "winerror", "status"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 2 ** 31:
                if key != "status" or 100 <= value <= 599:
                    check[key] = value
        result.append(check)
    return result


def _diagnostic_error_fields(error, nested=False):
    """只提取类型/数字；CONNECT 的固定错误格式也只保留三位状态码。"""
    result = {"error": "internal"}
    if not nested and isinstance(error, globals().get("_ConnectivityFailure", ())):
        checks = []
        for label, cause in error.attempts[:2]:
            if isinstance(label, str) and label in _DIAGNOSTIC_CHECK_LABELS:
                checks.append({"target": _DIAGNOSTIC_CHECK_LABELS[label],
                               **_diagnostic_error_fields(cause, nested=True)})
        checks = _diagnostic_clean_checks(checks)
        kinds = {check["error"] for check in checks}
        result["error"] = next(iter(kinds)) if len(kinds) == 1 else "probe_failed"
        if checks:
            result["checks"] = checks
        return result
    for error_type, label in ((ssl.SSLCertVerificationError, "certificate"),
                              (ssl.SSLError, "tls"), (TimeoutError, "timeout"),
                              (ConnectionRefusedError, "refused"), (ConnectionResetError, "reset"),
                              (socket.gaierror, "dns"), (http.client.HTTPException, "protocol"),
                              (OSError, "io")):
        if isinstance(error, error_type):
            result["error"] = label
            break
    if isinstance(error, globals().get("_SocksError", ())):
        result["error"] = "protocol"
    if isinstance(error, globals().get("_ProbeHttpError", ())):
        result["error"] = "http"
        if isinstance(error.status, int) and 100 <= error.status <= 599:
            result["status"] = error.status
    if isinstance(error, OSError) and error.args and isinstance(error.args[0], str):
        prefix = "Tunnel connection failed: "
        if error.args[0].startswith(prefix):
            result["error"] = "tunnel"
            code = error.args[0][len(prefix):].split(" ", 1)[0]
            if len(code) == 3 and code.isascii() and code.isdigit() and 100 <= int(code) <= 599:
                result["status"] = int(code)
    for key in ("errno", "winerror"):
        value = getattr(error, key, None)
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) <= 2 ** 31:
            result[key] = value
            if result["error"] == "io":
                result["error"] = {10060: "timeout", 10061: "refused", 10054: "reset",
                                   11001: "dns"}.get(value, "io")
    return result


def _diagnostic_record(event, upstream=None, error=None, **fields):
    """明确白名单：调用者即使误传 URL、密码或异常正文也不会落盘。"""
    if not isinstance(event, str) or not 1 <= len(event) <= 48:
        return None
    if not all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in event):
        return None
    record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    if isinstance(upstream, (tuple, list)) and len(upstream) == 2:
        host, port = upstream
        if (isinstance(host, str) and 0 < len(host) <= 253 and host.isascii()
                and all(c.isalnum() or c in ".-:[]" for c in host)
                and isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
            record["upstream"] = [host, port]
    for key, values in _DIAGNOSTIC_TEXT_FIELDS.items():
        value = fields.get(key)
        if isinstance(value, str) and value in values:
            record[key] = value
    for key in _DIAGNOSTIC_NUMBER_FIELDS:
        value = fields.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and abs(value) <= 10 ** 18:
                record[key] = value
    checks = _diagnostic_clean_checks(fields.get("checks"))
    if checks:
        record["checks"] = checks
    if error is not None:
        record.update(_diagnostic_error_fields(error))
    return record


def _sanitize_diagnostic_lines(data):
    """导出前再次筛选旧文件，未知/损坏行及额外字段均不进入诊断包。"""
    output = []
    for line in data.splitlines():
        try:
            old = json.loads(line)
            if not isinstance(old, dict):
                continue
            record = _diagnostic_record(old.get("event"), old.get("upstream"),
                                        **{k: v for k, v in old.items()
                                           if k not in ("event", "upstream", "error")})
            if record is None:
                continue
            stamp = old.get("time", "")
            record["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
            if old.get("error") in _DIAGNOSTIC_ERROR_TYPES:
                record["error"] = old["error"]
            output.append(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
        except (ValueError, TypeError, OverflowError):
            continue
    return "\n".join(output) + ("\n" if output else "")


class _DiagnosticFileHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record):
        # logging 默认会把异常/消息打印到 stderr；这里保留安全状态，转发不受影响。
        self.failed = True


class _DiagnosticLog:
    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)
        self.handler = _DiagnosticFileHandler(
            os.path.join(self.directory, "diagnostics.jsonl"),
            maxBytes=_DIAGNOSTIC_MAX_BYTES, backupCount=_DIAGNOSTIC_BACKUPS,
            encoding="utf-8")
        self.handler.failed = False
        self.queue = queue.Queue(maxsize=_DIAGNOSTIC_QUEUE_SIZE)
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.accepting = True
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, name="proxy-diagnostics", daemon=True)
        try:
            self.thread.start()
        except Exception:
            self.handler.close()
            raise

    def enqueue(self, record):
        # 不等待日志线程/磁盘；短锁有竞争时宁可丢诊断，不能拖慢网络线程。
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if not self.accepting:
                return False
            try:
                self.queue.put_nowait(record)
                return True
            except queue.Full:
                self.dropped += 1
                return False
        finally:
            self.lock.release()

    def _snapshot(self):
        self.handler.flush()
        result = {}
        for index in range(_DIAGNOSTIC_BACKUPS, -1, -1):
            name = "diagnostics.jsonl" + (f".{index}" if index else "")
            path = os.path.join(self.directory, name)
            try:
                with open(path, "rb") as src:
                    src.seek(0, os.SEEK_END)
                    length = src.tell()
                    src.seek(max(0, length - _DIAGNOSTIC_MAX_BYTES))
                    data = src.read(_DIAGNOSTIC_MAX_BYTES)
                result[name] = _sanitize_diagnostic_lines(data.decode("utf-8", "replace"))
            except FileNotFoundError:
                continue
        return result

    def _run(self):
        try:
            while not self.stopped.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if isinstance(item, tuple):
                        ready, response = item
                        try:
                            response["files"] = self._snapshot()
                        except Exception:
                            response["failed"] = True
                        finally:
                            ready.set()
                    else:
                        message = json.dumps(item, ensure_ascii=True, separators=(",", ":"))
                        record = logging.LogRecord("proxy.diagnostics", logging.INFO, "", 0,
                                                   message, (), None)
                        self.handler.handle(record)
                except Exception:
                    self.handler.failed = True
                finally:
                    self.queue.task_done()
        finally:
            try:
                self.handler.close()
            except Exception:
                pass

    def snapshot(self):
        ready, response = threading.Event(), {}
        with self.lock:
            if not self.accepting:
                raise OSError("诊断日志未启用")
            try:
                self.queue.put_nowait((ready, response))
            except queue.Full:
                raise OSError("诊断队列繁忙，请稍后重试") from None
        if not ready.wait(5) or response.get("failed"):
            raise OSError("诊断日志读取失败")
        return response["files"]

    def close(self):
        with self.lock:
            self.accepting = False
            self.stopped.set()
        self.thread.join(timeout=3)


def init_diagnostics(log_dir=None):
    """显式启动；导入模块或创建测试窗口不会触碰用户日志目录。"""
    global _DIAGNOSTICS, _DIAGNOSTICS_ERROR, _DIAGNOSTICS_DIRECTORY
    with _DIAGNOSTICS_LOCK:
        if _DIAGNOSTICS is not None:
            return True
        try:
            directory = os.path.abspath(log_dir or os.path.join(CONFIG_DIR, "logs"))
            _DIAGNOSTICS_DIRECTORY = directory
            _DIAGNOSTICS = _DiagnosticLog(directory)
            _DIAGNOSTICS_ERROR = ""
            return True
        except Exception:
            _DIAGNOSTICS_ERROR = "无法启用诊断日志；代理功能可继续使用"
            return False


def close_diagnostics():
    global _DIAGNOSTICS
    with _DIAGNOSTICS_LOCK:
        current, _DIAGNOSTICS = _DIAGNOSTICS, None
        if current is not None:
            current.close()


def _diagnostic(event, upstream=None, error=None, **fields):
    current = _DIAGNOSTICS
    if current is None:
        return False
    try:
        record = _diagnostic_record(event, upstream, error, **fields)
        return bool(record is not None and current.enqueue(record))
    except Exception:
        return False


def diagnostics_status():
    current = _DIAGNOSTICS
    return {"enabled": bool(current and current.accepting and current.thread.is_alive()),
            "directory": current.directory if current else _DIAGNOSTICS_DIRECTORY,
            "error": ("诊断日志写入失败；代理功能可继续使用" if current and current.handler.failed
                      else _DIAGNOSTICS_ERROR)}


def read_recent_diagnostics(limit=200):
    current = _DIAGNOSTICS
    if current is None:
        return ""
    try:
        limit = max(1, min(int(limit), 2000))
        files = current.snapshot()
        return "\n".join("".join(files.values()).splitlines()[-limit:])
    except Exception:
        return ""


def export_diagnostics(destination):
    """单个写线程在队列边界取一致快照；ZIP 只包含已再次脱敏的诊断文件。"""
    current = _DIAGNOSTICS
    if current is None:
        raise OSError("诊断日志未启用")
    files = current.snapshot()
    destination = os.path.abspath(os.fspath(destination))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(destination), suffix=".tmp",
                                         delete=False) as tmp:
            temporary = tmp.name
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, text)
        os.replace(temporary, destination)
        return destination
    except Exception:
        raise OSError("诊断日志导出失败") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass

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
            winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
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
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, cmd)
    else:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


# ---------- 单实例保护 ----------

def acquire_single_instance() -> bool:
    """用命名互斥量防止重复启动；已有实例时把它的窗口拉到前台并返回 False"""
    global _mutex_handle
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS：已有实例在跑
        kernel32.CloseHandle(handle)
        hwnd = (user32.FindWindowW(None, APP_TITLE)
                or user32.FindWindowW(None, "系统代理工具 V2.0"))
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
user32.CreateIconFromResourceEx.restype = ctypes.c_void_p
user32.CreateIconFromResourceEx.argtypes = [ctypes.POINTER(ctypes.c_ubyte), wintypes.DWORD,
                                           wintypes.BOOL, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
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


def _load_tray_icon():
    """读取 assets PNG，生成适合系统托盘尺寸的 HICON；同时返回所有权。"""
    width = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
    height = user32.GetSystemMetrics(50) or 16  # SM_CYSMICON
    desired = max(width, height)
    # 优先最接近且不小于目标尺寸的图；缺失或损坏则尝试其他尺寸。
    sizes = sorted(_ICON_SIZES, key=lambda size: (size < desired, abs(size - desired)))
    for size in sizes:
        try:
            with open(os.path.join(_ASSET_DIR, f"lan-proxy-{size}.png"), "rb") as source:
                payload = source.read()
            if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                continue
            buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            handle = user32.CreateIconFromResourceEx(buffer, len(payload), True, 0x00030000,
                                                     width, height, 0)
            if handle:
                return handle, True
        except (OSError, ValueError):
            continue
    # 系统共享图标不能 DestroyIcon；自定义载荷失败也不影响代理与托盘功能。
    return user32.LoadIconW(None, 32512), False


class TrayIcon:
    """系统托盘图标：左键/双击回调 on_restore，右键回调 on_menu。
    两个回调都运行在 Tk 消息泵的 WndProc 重入点里，**绝不能在里面直接调
    tkinter**（重入 Tcl 会 Fatal Python Error 秒退进程），只能入队或做纯 Win32 操作。"""

    def __init__(self, on_restore, on_menu):
        self._on_restore = on_restore
        self._on_menu = on_menu
        self._added = False
        self._closed = False
        self._owned_icon = False
        self._class_registered = False
        self._tip = ""  # 当前 tooltip，show 用它判断是否真要发 NIM_MODIFY
        self._wndproc = _WNDPROC(self._dispatch)  # 必须保存引用，被回收会崩
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = f"ProxyToolTray{id(self):x}"  # 每实例一个类名，避免重注册冲突
        self._class_name = wc.lpszClassName
        self._hinstance = wc.hInstance
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._class_registered = True
        # 必须在建窗前注册：WndProc 从 WM_NCCREATE 起就会被回调
        self._msg_taskbar = user32.RegisterWindowMessageW("TaskbarCreated")
        # 用普通隐藏窗口而不是 message-only：后者收不到广播，而 explorer
        # 重启时的 TaskbarCreated 广播正是图标被清掉后重新添加的唯一信号
        self._hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "", 0, 0, 0, 0, 0,
            None, None, wc.hInstance, None)
        if not self._hwnd:
            error = ctypes.WinError(ctypes.get_last_error())
            user32.UnregisterClassW(self._class_name, self._hinstance)
            self._class_registered = False
            self._closed = True
            raise error
        self._nid = _NOTIFYICONDATAW()
        self._nid.cbSize = ctypes.sizeof(self._nid)
        self._nid.hWnd = self._hwnd
        self._nid.uID = 1
        self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._nid.uCallbackMessage = WM_TRAY_CALLBACK
        try:
            self._nid.hIcon, self._owned_icon = _load_tray_icon()
            if not self._nid.hIcon:
                raise OSError("无法创建托盘图标")
        except Exception:
            self.close()
            raise

    @property
    def hwnd(self):
        return self._hwnd

    def _dispatch(self, hwnd, msg, wp, lp):
        # 整体兜底：消息处理里任何异常都不能抛出回调——返回 0 会让
        # CreateWindowExW 之类的调用中途失败（WM_NCCREATE 必须走 DefWindowProc）
        try:
            if self._closed:
                return user32.DefWindowProcW(hwnd, msg, wp, lp)
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
        if self._closed:
            return False
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
        if self._closed or not self._added:
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
        """暂时隐藏，保留 HICON 以便再次 show 或资源管理器重启后使用。"""
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._added = False

    def close(self):
        """主线程中永久销毁；先撤托盘/消息窗口，再释放自有图标，重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        self.remove()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._class_registered:
            user32.UnregisterClassW(self._class_name, self._hinstance)
            self._class_registered = False
        if self._owned_icon and self._nid.hIcon:
            user32.DestroyIcon(self._nid.hIcon)
        self._owned_icon = False
        self._nid.hIcon = None

    def show_menu(self, entries):
        """弹原生托盘菜单（TrackPopupMenu，资源管理器同款），阻塞至菜单关闭，
        返回被点中项的回调（未选中返回 None）。**必须在主线程调用**（内部
        _ui 入队后到达，调用点即安全点，返回的回调可直接执行）。
        entries: [(label, callback), ...]，label 为 "-" 画分隔线。
        不用 Tk 菜单：tk_popup 在托盘场景真实点击不派发选中（实测玄学），
        原生菜单的派发由 Windows 内部完成，不经过 Tcl。"""
        if self._closed:
            return None
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
    """多上游选择器：健康节点优先选连接较少者，同负载时轮询。

    拨号中的连接也计入负载，选择与占位在同一把锁内完成。
    order() 推进轮询指针并给出本次连接的尝试顺序：健康上游在前，
    首次失败降级 RETRY_SECS，连续失败 >= BAN_AFTER 后降级 BAN_SECS；
    降级节点殿后——不作首选但仍在列，
    全部熔断时它们仍是最后的希望。
    """

    RETRY_SECS = 1  # 首次失败先短暂降级，避免后续请求继续首选故障节点
    BAN_AFTER = 3   # 连续失败次数达到即熔断
    BAN_SECS = 30   # 熔断时长：期间不作首选（兜底仍可试到）
    HEALTH_FAIL_AFTER = 3
    HEALTH_RECOVER_AFTER = 2

    def __init__(self, upstreams, clock=time.monotonic):
        self._clock = clock  # 注入时间源，测试里用假钟避免真等 30 秒
        self._lock = threading.Lock()
        self._ups = []
        self._idx = 0      # 轮询指针：order() 推进
        self._fails = {}   # up -> 连续失败次数
        self._banned = {}  # up -> 禁止至（clock 值）
        self._loads = {}   # up -> 拨号中 + 已建立的连接数（跨预设保留存量）
        self._leases = {}  # token -> up；连接结束时幂等释放
        self.set(upstreams)

    def set(self, upstreams, preserve_health=False):
        """替换地址并作废在途结果；可保留仍存在节点的 TCP/HTTPS 健康记录。"""
        with self._lock:
            replacements = list(dict.fromkeys(upstreams))
            if preserve_health and replacements == self._ups:
                return  # 备注等非地址编辑不重置检测间隔，也无需作废同一端点的探测。
            previous_health = getattr(self, "_https_health", {}) if preserve_health else {}
            previous_fails = self._fails if preserve_health else {}
            previous_banned = self._banned if preserve_health else {}
            self._generation = getattr(self, "_generation", 0) + 1
            self._probing = {}
            self._ups = replacements
            self._idx = 0
            self._fails = {up: previous_fails[up] for up in self._ups if up in previous_fails}
            self._banned = {up: previous_banned[up] for up in self._ups if up in previous_banned}
            self._health_inflight = {}
            self._https_health = {
                up: dict(previous_health[up]) if up in previous_health else {
                    "state": "unknown", "failures": 0, "successes": 0,
                    "last_checked": None, "latency_ms": None, "reason": "", "degraded": False}
                for up in self._ups}

    def _order_locked(self):
        if not self._ups:
            return []
        n = len(self._ups)
        start = self._idx
        self._idx = (self._idx + 1) % n
        rot = [self._ups[(start + i) % n] for i in range(n)]
        now = self._clock()
        # 稳定排序保留同负载的轮询顺序；正在恢复探测的节点由占位逻辑跳过。
        def priority(up):
            tcp_degraded = now < self._banned.get(up, 0)
            https_degraded = self._https_health[up]["degraded"]
            # HTTPS 降级只是殿后，不封死兜底流量；公共检测点异常时仍能访问其他网站。
            return (tcp_degraded or https_degraded, tcp_degraded, self._loads.get(up, 0))
        return sorted(rot, key=priority)

    def order(self):
        """健康、负载少的节点在前，同负载轮询；空池返回 []。"""
        with self._lock:
            return self._order_locked()

    def acquire_attempt(self, excluded):
        """原子选择并占位，避免并发连接读到同一份过时的负载。"""
        with self._lock:
            for up in self._order_locked():
                if up not in excluded:
                    token = self._begin_locked(up)
                    if token is not None:
                        return up, token
        return None

    def _report_fail_locked(self, up):
        self._fails[up] = self._fails.get(up, 0) + 1
        delay = self.BAN_SECS if self._fails[up] >= self.BAN_AFTER else self.RETRY_SECS
        self._banned[up] = self._clock() + delay

    def report_fail(self, up):
        with self._lock:
            if up in self._ups:
                self._report_fail_locked(up)

    def report_ok(self, up):
        with self._lock:
            self._fails.pop(up, None)
            self._banned.pop(up, None)

    def _begin_locked(self, up):
        if up not in self._ups or up in self._probing:
            return None
        token = (self._generation, object())
        if self._fails.get(up, 0):
            self._probing[up] = token
        self._leases[token] = up
        self._loads[up] = self._loads.get(up, 0) + 1
        return token

    def begin_attempt(self, up):
        """已失败节点只允许一个恢复探测；健康节点仍可并行拨号。"""
        with self._lock:
            return self._begin_locked(up)

    def _release_locked(self, token):
        up = self._leases.pop(token, None)
        if up is not None:
            self._loads[up] -= 1
            if self._loads[up] == 0:
                del self._loads[up]

    def release_attempt(self, token):
        with self._lock:
            self._release_locked(token)

    def finish_attempt(self, up, generation, success):
        with self._lock:
            if not success:
                self._release_locked(generation)
            # 切换预设后，旧拨号结果不得污染新池（即使两池包含同一地址）。
            if generation[0] != self._generation:
                return
            if self._probing.get(up) == generation:
                del self._probing[up]
            if success:
                self._fails.pop(up, None)
                self._banned.pop(up, None)
            else:
                self._report_fail_locked(up)

    def health_targets(self):
        """监测调度器获取本代地址；不推进流量轮询指针。"""
        with self._lock:
            return self._generation, tuple(self._ups)

    def begin_health_probe(self, up, generation=None):
        with self._lock:
            if (up not in self._https_health or up in self._health_inflight
                    or (generation is not None and generation != self._generation)):
                return None
            token = (self._generation, object())
            self._health_inflight[up] = token
            return token

    def cancel_health_probe(self, up, token):
        with self._lock:
            if self._health_inflight.get(up) == token:
                del self._health_inflight[up]

    def finish_health_probe(self, up, token, success, info):
        """只提交本代、本次完整多目标探测；TCP 成功不会调用或清除本状态。"""
        transition = None
        with self._lock:
            if token is None or self._health_inflight.get(up) != token:
                return False
            del self._health_inflight[up]
            health = self._https_health[up]
            previous = health["state"]
            health["last_checked"] = self._clock()
            if success:
                health["failures"] = 0
                health["successes"] += 1
                health["latency_ms"] = info if isinstance(info, (int, float)) else None
                health["reason"] = ""
                if health["degraded"] and health["successes"] < self.HEALTH_RECOVER_AFTER:
                    health["state"] = "recovering"
                else:
                    health["degraded"] = False
                    health["state"] = "healthy"
            else:
                health["successes"] = 0
                health["failures"] += 1
                health["latency_ms"] = None
                health["reason"] = _short_conn_error(info)
                if health["degraded"] or health["failures"] >= self.HEALTH_FAIL_AFTER:
                    health["degraded"] = True
                    health["state"] = "degraded"
                else:
                    health["state"] = "suspect"
            if health["state"] != previous:
                transition = (health["state"], health["failures"], health["successes"])
        if transition:
            diagnostic = globals().get("_diagnostic")
            if diagnostic is not None:
                try:
                    diagnostic("health_transition", upstream=up, error=None if success else info,
                               state=transition[0], failures=transition[1], successes=transition[2])
                except Exception:
                    pass  # 日志不能影响调度和状态提交。
        return True

    def health_snapshot(self):
        """上游状态副本；last_checked 使用单调时钟，HTTPS 与 TCP 故障分开展示。"""
        with self._lock:
            now = self._clock()
            return {up: {**health, "checking": up in self._health_inflight,
                         "tcp_failures": self._fails.get(up, 0),
                         "tcp_degraded": now < self._banned.get(up, 0)}
                    for up, health in self._https_health.items()}


class HealthMonitor:
    """最多两个工作线程直接领取到期节点；无任务队列，不随上游数创建线程。

    stop 不等待网络；本轮在途结果作废。start 可安全重启，尚未返回的旧工作线程
    仍占线程预算，不会再为同一地址并发探测。wake 只唤醒调度，不突破检测间隔。
    """

    MAX_WORKERS = 2

    def __init__(self, pool, probe=None, *, interval=30.0):
        self.pool = pool
        self._probe = probe
        self.interval = max(.01, float(interval))
        self._condition = threading.Condition()
        self._running = False
        self._epoch = 0
        self._threads = set()
        self._inflight = {}
        self._due = {}
        self._pool_generation = None

    def _ensure_workers_locked(self):
        failure = None
        while self._running and len(self._threads) < self.MAX_WORKERS:
            worker = None
            try:
                worker = threading.Thread(target=self._worker, daemon=True,
                                          name="ProxyHealthMonitor")
                self._threads.add(worker)
                worker.start()
            except RuntimeError as error:
                self._threads.discard(worker)
                failure = error
                diagnostic = globals().get("_diagnostic")
                if diagnostic is not None:
                    diagnostic("health_start_error", error=error)
                if not self._threads:
                    self._running = False
                break
        return failure

    def start(self):
        with self._condition:
            if not self._running:
                self._running = True
                self._epoch += 1
                self._pool_generation = None
                self._due = {}
            failure = self._ensure_workers_locked()
            self._condition.notify_all()
            if not self._threads:
                raise failure or RuntimeError("health monitor has no worker")

    def stop(self):
        with self._condition:
            self._running = False
            self._epoch += 1
            for up, (_epoch, token) in self._inflight.items():
                self.pool.cancel_health_probe(up, token)
            self._condition.notify_all()

    def wake(self):
        with self._condition:
            self._condition.notify_all()

    def _next_task_locked(self):
        generation, upstreams = self.pool.health_targets()
        now = time.monotonic()
        if generation != self._pool_generation:
            self._pool_generation = generation
            self._due = {up: self._due.get(up, now) for up in upstreams}
        for up in sorted(upstreams, key=lambda address: self._due.get(address, now)):
            if up in self._inflight or now < self._due.get(up, now):
                continue
            token = self.pool.begin_health_probe(up, generation)
            if token is not None:
                task = (self._epoch, token)
                self._inflight[up] = task
                # 先记下一次最早启动时间；即使成员编辑使本次结果作废，保留节点
                # 也不会在旧探测刚完成时立刻再发一次。有效完成后再按完成时间续期。
                self._due[up] = now + self.interval
                return up, task
        return None

    def _wait_seconds_locked(self):
        now = time.monotonic()
        due = [when for up, when in self._due.items() if up not in self._inflight]
        # 配置修改应调用 wake；有限等待也能发现漏掉的唤醒。
        return min(self.interval, max(.01, min(due) - now)) if due else self.interval

    def _worker(self):
        current = threading.current_thread()
        try:
            while True:
                with self._condition:
                    task = None
                    while self._running and task is None:
                        task = self._next_task_locked()
                        if task is None:
                            self._condition.wait(self._wait_seconds_locked())
                    if not self._running:
                        return
                up, (epoch, token) = task
                try:
                    probe = self._probe or _probe_upstream
                    success, info = probe(*up)
                except Exception as error:
                    success, info = False, error
                with self._condition:
                    if self._inflight.get(up) == (epoch, token):
                        del self._inflight[up]
                    if self._running and epoch == self._epoch:
                        self.pool.finish_health_probe(up, token, bool(success), info)
                        if token[0] == self._pool_generation:
                            self._due[up] = time.monotonic() + self.interval
                    else:
                        self.pool.cancel_health_probe(up, token)
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._threads.discard(current)
                # start 恰好与闲置线程退出交错时补齐；退休线程不会再领取工作。
                self._ensure_workers_locked()
                self._condition.notify_all()


def _tune_sock(sock):
    """独立设置各选项；保活探测失联对端，不给正常空闲长连接加读超时。"""
    for level, option, value in (
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024),
            (socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)):
        try:
            sock.setsockopt(level, option, value)
        except OSError:
            pass
    if hasattr(socket, "SIO_KEEPALIVE_VALS"):
        try:
            # Windows: 空闲 60 秒后开始探测，每 10 秒重试，由 TCP 判断失联。
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60000, 10000))
        except OSError:
            pass


_LISTENERS = weakref.WeakSet()
_LISTENERS_LOCK = threading.Lock()
_DNS_QUEUE = queue.Queue(maxsize=16)
_DNS_LOCK = threading.Lock()
_DNS_STARTED = False
_DNS_CACHE = {}  # 最多 128 个上游，成功结果短缓存；失败拨号立即失效
_DNS_INFLIGHT = {}


def _local_target(host, port, extra_ports=()):
    """只拦本工具的监听端口；其他本机代理仍可作为上游。"""
    with _LISTENERS_LOCK:
        ports = {s.listen_port for s in _LISTENERS if s._on}
    if int(port) not in ports.union(extra_ports):
        return False
    host = host.strip('[]').lower().rstrip('.')
    if host in ('localhost', socket.gethostname().lower().rstrip('.')):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # DNS 别名在后台解析后再检查，不阻塞界面。
    if address.is_loopback or address.is_unspecified or (
            isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped and
            address.ipv4_mapped.is_loopback):
        return True
    # UDP connect 仅查询本机路由，不发送数据，兼容多网卡上的本机地址。
    try:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((str(address), int(port)))
            return ipaddress.ip_address(probe.getsockname()[0]) == address
    except OSError:
        return False


def _resolve_addresses(host, port, deadline):
    """有界 DNS 工作者 + 同地址合并解析；网络慢时不为每个连接排一次 DNS。"""
    global _DNS_STARTED
    try:
        ipaddress.ip_address(host.strip('[]'))
        return [(host.strip('[]'), port)]
    except ValueError:
        pass
    key = (host.lower(), port)
    with _DNS_LOCK:
        cached = _DNS_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        _DNS_CACHE.pop(key, None)
        if not _DNS_STARTED:
            def resolver():
                while True:
                    lookup_key, job = _DNS_QUEUE.get()
                    result = job['result']
                    try:
                        if time.monotonic() < job['deadline']:
                            try:
                                result.extend(dict.fromkeys(
                                    (entry[4][0], lookup_key[1]) for entry in socket.getaddrinfo(
                                        *lookup_key, type=socket.SOCK_STREAM)))
                            except OSError as error:
                                result.append(error)
                    finally:
                        with _DNS_LOCK:
                            _DNS_INFLIGHT.pop(lookup_key, None)
                            if result and not isinstance(result[0], OSError):
                                if len(_DNS_CACHE) >= 128:
                                    _DNS_CACHE.pop(next(iter(_DNS_CACHE)))
                                _DNS_CACHE[lookup_key] = (time.monotonic() + 10, tuple(result))
                        job['event'].set()
                        _DNS_QUEUE.task_done()
            for index in range(2):
                try:
                    threading.Thread(target=resolver, daemon=True).start()
                except RuntimeError:
                    if not index:
                        raise
                    break  # 已创建的线程继续工作，避免下次重复创建更多线程。
            _DNS_STARTED = True
        job = _DNS_INFLIGHT.get(key)
        if job is None:
            job = {'event': threading.Event(), 'result': [], 'deadline': deadline}
            try:
                _DNS_QUEUE.put_nowait((key, job))
            except queue.Full:
                raise OSError('DNS 队列已满，请稍后重试') from None
            _DNS_INFLIGHT[key] = job
        else:
            job['deadline'] = max(job['deadline'], deadline)
    if not job['event'].wait(max(0, deadline - time.monotonic())):
        raise TimeoutError('DNS 解析超过连接预算')
    result = job['result']
    if result and isinstance(result[0], OSError):
        raise result[0]
    if not result:
        raise TimeoutError('没有在连接预算内解析到地址')
    return result


def _dial_connection(address, timeout):
    deadline = time.monotonic() + timeout
    host, port = address
    if _local_target(host, port):
        raise OSError('已阻止上游指向本工具监听端口的自循环')
    candidates = _resolve_addresses(host, port, deadline)
    last_error = None
    for target in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with _DNS_LOCK:
                _DNS_CACHE.pop((host.lower(), port), None)
            raise TimeoutError('上游建连总预算已用完')
        if _local_target(*target):
            raise OSError('已阻止解析到本工具监听端口的自循环')
        try:
            return socket.create_connection(target, timeout=remaining)
        except OSError as error:
            last_error = error
    with _DNS_LOCK:
        _DNS_CACHE.pop((host.lower(), port), None)
    raise last_error or OSError('上游没有可连接地址')


class _ManagedListener:
    """监听异常有限重建；不关闭已建立的连接，不修改系统代理设置。"""
    bind_host = '0.0.0.0'
    server_kind = 'relay'
    RETRY_DELAYS = (.25, 1.0, 2.0)

    def _init_listener(self):
        self.listener_state = 'stopped'
        self._lifecycle = threading.RLock()
        self._stop_accept = threading.Event()

    def _open_listener(self):
        srv = socket.socket()
        try:
            srv.bind((self.bind_host, self.listen_port))
            srv.listen(ACCEPT_BACKLOG)
            srv.settimeout(.5)  # 只作用于 accept；stop 能及时终止等待。
            actual = srv.getsockname()
            if isinstance(actual, tuple):
                self.listen_port = actual[1]
            return srv
        except OSError:
            srv.close()
            raise

    def start(self):
        with self._lifecycle:
            if self._on:
                return
            if hasattr(self, 'pool'):
                with self.pool._lock:
                    upstreams = list(self.pool._ups)
            else:
                upstreams = [self.upstream]
            for host, port in upstreams:
                if _local_target(host, port, (self.listen_port,)):
                    raise OSError('上游不能指向本工具的监听端口')
            try:
                srv = self._open_listener()
            except OSError as error:
                self.listener_state = 'failed'
                _diagnostic('listener_start_failed', error=error, server=self.server_kind, stage='listen')
                raise
            self._srv, self._on = srv, True
            self.listener_state = 'running'
            self._stop_accept = threading.Event()
            with _LISTENERS_LOCK:
                _LISTENERS.add(self)
            try:
                threading.Thread(target=self._accept_loop, daemon=True).start()
            except RuntimeError as error:
                _diagnostic('listener_start_failed', error=error, server=self.server_kind, stage='worker')
                self._srv, self._on = None, False
                self.listener_state = 'failed'
                srv.close()
                raise

    def stop(self):
        with self._lifecycle:
            self._on = False
            self.listener_state = 'stopped'
            self._stop_accept.set()
            srv, self._srv = self._srv, None
            if srv:
                srv.close()
        with self._lock:
            pairs = list(self._live)
        for pair in pairs:
            for sock in pair:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def _accept_loop(self):
        stop = self._stop_accept
        srv = self._srv
        failures = []
        while not stop.is_set() and self._on and self._srv is srv:
            try:
                client, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError as error:
                with self._lifecycle:
                    if stop.is_set() or self._srv is not srv:
                        return
                    self._srv = None
                    self.listener_state = 'recovering'
                    srv.close()
                _diagnostic('listener_failed', error=error, server=self.server_kind, stage='accept')
                while not stop.is_set():
                    now = time.monotonic()
                    failures = [stamp for stamp in failures if now - stamp < 60]
                    if len(failures) >= len(self.RETRY_DELAYS):
                        with self._lifecycle:
                            if self._stop_accept is stop:
                                self._on = False
                                self.listener_state = 'failed'
                        return
                    delay = self.RETRY_DELAYS[len(failures)]
                    failures.append(now)
                    if stop.wait(delay):
                        return
                    with self._lifecycle:
                        if stop.is_set():
                            return
                        try:
                            srv = self._open_listener()
                        except OSError as retry_error:
                            _diagnostic('listener_retry_failed', error=retry_error,
                                        server=self.server_kind, stage='listen')
                            continue
                        self._srv = srv
                        self.listener_state = 'running'
                    _diagnostic('listener_recovered', server=self.server_kind, stage='listen')
                    break
                continue
            client.settimeout(None)
            _tune_sock(client)
            _start_client_worker(self, client, srv)


def _start_client_worker(server, client, listener):
    """三入口共用并发上限，线程创建失败或停服时也归还槽位。"""
    def _close_client():
        try:
            client.close()
        except OSError:
            pass

    slots = _FORWARD_SLOTS
    shared_slots = _SHARED_SLOTS
    shared = not isinstance(server, HttpServer)
    ip = None
    if shared:
        try:
            peer = client.getpeername()
            ip = peer[0] if isinstance(peer, tuple) else 'unknown'
        except OSError:
            ip = 'unknown'
    shared_acquired = shared_slots.acquire(blocking=False) if shared else False
    with _SHARED_LOCK:
        client_acquired = shared_acquired and _SHARED_CLIENTS.get(ip, 0) < MAX_SHARED_PER_CLIENT
        if client_acquired:
            _SHARED_CLIENTS[ip] = _SHARED_CLIENTS.get(ip, 0) + 1
    acquired = (not shared or client_acquired) and slots.acquire(blocking=False)

    def release():
        if client_acquired:
            with _SHARED_LOCK:
                count = _SHARED_CLIENTS[ip] - 1
                if count:
                    _SHARED_CLIENTS[ip] = count
                else:
                    del _SHARED_CLIENTS[ip]
        if shared_acquired:
            shared_slots.release()
        if acquired:
            slots.release()

    if not acquired:
        release()
        with server._lock:
            server.rejected_conns += 1
        _diagnostic("connection_rejected", stage="worker", limit=MAX_FORWARD_CONNECTIONS,
                    rejected=server.rejected_conns)
        _close_client()
        return
    pending = (client,)
    with server._lock:
        stopped = not server._on or server._srv is not listener
        if not stopped:
            server._live.add(pending)  # 在线程启动前登记，stop 不会漏掉尚未执行的任务。
    if stopped:
        _close_client()
        release()
        return

    def run():
        try:
            with server._lock:
                current = server._on and server._srv is listener
            if current:
                server._pump(client)
        except Exception as error:
            _diagnostic("worker_failed", error=error, stage="worker")
            raise
        finally:
            try:
                _close_client()
            finally:
                with server._lock:
                    server._live.discard(pending)
                release()
    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError as error:
        _diagnostic("worker_start_failed", error=error, stage="worker")
        with server._lock:
            server._live.discard(pending)
            server.rejected_conns += 1
        _close_client()
        release()


def _pipe_sockets(src, dst, flush, progress=None):
    """双向搬运的公共实现（Relay/Socks/Http 三处共用）：recv→sendall 直到
    任一端断开，随后半关写端让对端收完尾包。flush(n) 每 256KB 批量回调一次
    （热路径不逐块抢统计锁），EOF/异常时冲掉尾量。可选 progress 是此管道
    累计发送/已入账计数槽，供快照即时显示批次间的小流量。"""
    acc = 0
    buffer = bytearray(PIPE_BUFFER_SIZE)
    view = memoryview(buffer)
    stage = "recv"
    try:
        while True:
            stage = "recv"
            size = src.recv_into(buffer)
            if not size:
                break
            stage = "send"
            dst.sendall(view[:size])
            acc += size
            if progress is not None:
                progress[0] += size
            if acc >= 262144:
                flush(acc)
                acc = 0
    except OSError as error:
        _diagnostic("pipe_failed", error=error, stage=stage)
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    finally:
        if acc:
            flush(acc)
        try:
            dst.shutdown(socket.SHUT_WR)  # 半关，让对面把剩余数据收完
        except OSError:
            pass


def _pending_bytes(pending):
    """调用者持有服务器锁；每个单写者槽只读一次，提交和移除不会穿插快照。"""
    deltas = {}
    for record, down, progress in pending.values():
        delta = deltas.setdefault(id(record), [0, 0])
        delta[0 if down else 1] += progress[0] - progress[1]
    return deltas


def _pipe_with_stats(server, src, dst, record, down, include_total=False):
    """每管道独立发布小流量进度；只有批量交接才取得服务器统计锁。"""
    token = object()
    progress = [0, 0]  # 单写者累计发送量 / 锁内已入账量
    with server._lock:
        server._pending[token] = (record, down, progress)

    def flush(n):
        with server._lock:
            record["bd" if down else "bu"] += n
            if include_total:
                if down:
                    server.bytes_down += n
                else:
                    server.bytes_up += n
            # 同一个管道线程是唯一写者；与累计值同锁交接，快照不会双算。
            progress[1] += n

    try:
        _pipe_sockets(src, dst, flush, progress)
    finally:
        with server._lock:
            server._pending.pop(token, None)


class RelayServer(_ManagedListener):
    """把 0.0.0.0:listen_port 收到的 TCP 连接原样搬给上游代理（不解协议）"""

    bind_host = '0.0.0.0'
    server_kind = 'relay'

    def __init__(self, listen_port: int, upstream: tuple):
        self._init_listener()
        self.listen_port = listen_port
        self.upstream = upstream  # (ip, port)，可随时替换，对新连接生效
        self._srv = None
        self._on = False
        self.rejected_conns = 0
        self._lock = threading.Lock()
        self.active_conns = 0  # 当前 TCP 连接总数
        self.devices = {}      # ip -> {"conns": 连接数, "last": 最后活跃, "bd"/"bu": 累计下行/上行字节}
        self._live = set()     # 活跃连接对 (client, up)，stop 时强制断开用
        self._pending = {}  # 管道 -> (计账记录, 下行标记, 单写者待入账字节槽)
        self.bytes_down = 0    # 累计下行字节（上游→客户端），锁内更新
        self.bytes_up = 0      # 累计上行字节（客户端→上游）


    def _pump(self, client):
        try:
            ip = client.getpeername()[0]
        except OSError:
            ip = "未知"
        with self._lock:
            dev = self.devices.setdefault(ip, {"conns": 0, "last": time.monotonic(), "bd": 0, "bu": 0})
            dev["conns"] += 1
            self.active_conns += 1
        up = pair = None
        t_pending = (client,)
        try:
            with self._lock:
                self._live.add(t_pending)  # 拨上游前就登记，stop() 才掐得到在途连接
            upstream = self.upstream
            started = time.perf_counter()
            try:
                up = _dial_connection(upstream, timeout=DIAL_TIMEOUT_SECS)
            except OSError as error:
                _diagnostic("upstream_dial_failed", upstream=upstream, error=error,
                            server="relay", stage="dial",
                            elapsed_ms=int((time.perf_counter() - started) * 1000))
                client.close()  # 上游连不上，别让客户端 socket 干等
                return
            finally:
                with self._lock:
                    self._live.discard(t_pending)
            up.settimeout(None)  # 否则上游 keep-alive 空闲会被 5 秒超时打断
            _tune_sock(up)
            pair = (client, up)
            with self._lock:
                self._live.add(pair)
                stopped = not self._on  # stop() 若恰在拨号期间跑过，这条是漏网的
            if stopped:
                client.close()
                up.close()
                return
            t1 = threading.Thread(target=self._pipe, args=(client, up, dev, False), daemon=True)
            t1.start()
            # 当前工作线程直接承担下行，每连接只需两个线程。
            try:
                self._pipe(up, client, dev, True)
            finally:
                t1.join()
        finally:
            for sock in (client, up):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            with self._lock:
                self._live.discard(t_pending)
                self._live.discard(pair)
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
            pending = _pending_bytes(self._pending)
            devs = sorted(((ip, d["conns"], int(now - d["last"]),
                            d["bd"] + pending.get(id(d), (0, 0))[0],
                            d["bu"] + pending.get(id(d), (0, 0))[1])
                           for ip, d in self.devices.items()),
                          key=lambda x: -x[1])
            total = (self.bytes_down + sum(delta[0] for delta in pending.values()),
                     self.bytes_up + sum(delta[1] for delta in pending.values()))
            return self.active_conns, devs, total

    def _pipe(self, src, dst, dev, down: bool):
        """实时快照包含小流量，累计计账仍按批次入锁。"""
        _pipe_with_stats(self, src, dst, dev, down, include_total=True)




# ---------- SOCKS5 网关（桥接到 HTTP 上游代理） ----------

class _SocksError(Exception):
    """SOCKS5 会话失败：rep=回给客户端的错误码，消息给人看（状态行）。
    rep=None 表示握手阶段已回过应答（如 0xFF），_pump 里别再补发"""

    def __init__(self, rep, msg: str):
        super().__init__(msg)
        self.rep = rep


def _set_deadline_timeout(sock, deadline):
    """把本次 I/O 的超时压到握手剩余预算，慢速分包不能无限续期。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("handshake timed out")
    sock.settimeout(remaining)


class SocksServer(_ManagedListener):
    """0.0.0.0:listen_port 收 SOCKS5（仅 CONNECT），桥接到 HTTP 上游代理。
    上游只认 HTTP 代理时的协议转换网关：对上游发 CONNECT，200 后双向搬字节。
    启停/记账/管道结构与 RelayServer 同款。"""

    HANDSHAKE_TIMEOUT = 5  # 客户端握手限时（拨上游另有 DIAL_TIMEOUT_SECS）；隧道接通后清掉

    bind_host = '0.0.0.0'
    server_kind = 'socks'

    def __init__(self, listen_port: int, upstream: tuple, user: str = "", password: str = ""):
        self._init_listener()
        self.listen_port = listen_port
        self.upstream = upstream  # (ip, port)，可随时替换，对新连接生效
        self.user = user.strip()
        self.password = password
        self.last_error = ""  # 最近一次 SOCKS 会话失败原因（带时刻，状态行显示）
        self._srv = None
        self._on = False
        self.rejected_conns = 0
        self._lock = threading.Lock()
        self.active_conns = 0
        self.devices = {}
        self._live = set()
        self._pending = {}  # 管道 -> (计账记录, 下行标记, 单写者待入账字节槽)
        self.bytes_down = 0    # 累计下行字节（上游→客户端），锁内更新
        self.bytes_up = 0      # 累计上行字节（客户端→上游）


    def snapshot(self):
        """同 RelayServer.snapshot：(连接总数, 设备列表, (累计下行, 累计上行))"""
        now = time.monotonic()
        with self._lock:
            if self.devices:
                self.devices = {ip: d for ip, d in self.devices.items()
                                if d["conns"] > 0 or now - d["last"] < DEVICE_TTL_SEC}
            pending = _pending_bytes(self._pending)
            devs = sorted(((ip, d["conns"], int(now - d["last"]),
                            d["bd"] + pending.get(id(d), (0, 0))[0],
                            d["bu"] + pending.get(id(d), (0, 0))[1])
                           for ip, d in self.devices.items()),
                          key=lambda x: -x[1])
            total = (self.bytes_down + sum(delta[0] for delta in pending.values()),
                     self.bytes_up + sum(delta[1] for delta in pending.values()))
            return self.active_conns, devs, total

    def _note_error(self, e: _SocksError):
        self.last_error = f"{time.strftime('%H:%M')} {e}"

    @staticmethod
    def _recv_exact(sock, n: int, deadline=None) -> bytes:
        """SOCKS 握手按字段长度读全，所有字段共享同一个截止时间。"""
        buf = bytearray()
        while len(buf) < n:
            if deadline is not None:
                _set_deadline_timeout(sock, deadline)
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
        deadline = time.monotonic() + self.HANDSHAKE_TIMEOUT

        def receive(size):
            return self._recv_exact(client, size, deadline)

        def send(data):
            _set_deadline_timeout(client, deadline)
            client.sendall(data)

        ver, nmethods = receive(2)
        if ver != 5:
            raise _SocksError(1, "不是 SOCKS5 握手")
        methods = receive(nmethods)
        if self.user:  # RFC 1929 用户名密码认证（设置里留空 = 无认证）
            if 2 not in methods:
                self._reply(client, 0xFF)
                raise _SocksError(None, "客户端不支持用户名密码认证")
            send(b"\x05\x02")
            if receive(1)[0] != 1:
                raise _SocksError(1, "认证子协商版本错误")
            ulen = receive(1)[0]
            uname = receive(ulen).decode("utf-8", "replace")
            plen = receive(1)[0]
            passwd = receive(plen).decode("utf-8", "replace")
            ok = uname == self.user and passwd == self.password
            send(b"\x01" + (b"\x00" if ok else b"\x01"))
            if not ok:
                raise _SocksError(1, "用户名或密码错误")
        else:
            if 0 not in methods:
                self._reply(client, 0xFF)
                raise _SocksError(None, "客户端不支持无认证方式")
            send(b"\x05\x00")
        ver, cmd, _rsv, atyp = receive(4)
        if ver != 5:
            raise _SocksError(1, "请求版本错误")
        if cmd != 1:
            raise _SocksError(7, "仅支持 CONNECT（不支持 BIND/UDP 转发）")
        if atyp == 1:
            host = socket.inet_ntoa(receive(4))
        elif atyp == 3:  # 域名不在本地解析，原样交给上游（顺带规避本地 DNS 污染）
            n = receive(1)[0]
            host = receive(n).decode("ascii", "replace")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, receive(16))
        else:
            raise _SocksError(8, "地址类型不支持")
        port = int.from_bytes(receive(2), "big")
        if ":" in host:  # IPv6 字面量进 CONNECT 请求行必须加方括号，否则上游解析不了
            host = f"[{host}]"
        return f"{host}:{port}"

    def _dial_upstream(self, client, target: str):
        """局域网拨号与外网 CONNECT 分开计时；碎片响应头共用外网预算。"""
        up = _dial_connection(self.upstream, timeout=DIAL_TIMEOUT_SECS)
        deadline = time.monotonic() + UPSTREAM_CONNECT_TIMEOUT_SECS
        _tune_sock(up)
        try:
            _set_deadline_timeout(up, deadline)
            up.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
            resp = b""
            while b"\r\n\r\n" not in resp:
                if len(resp) > 8192:
                    raise _SocksError(1, "上游响应头异常")
                _set_deadline_timeout(up, deadline)
                chunk = up.recv(4096)
                if not chunk:
                    raise _SocksError(1, "上游提前断开")
                resp += chunk
            status = resp.split(b"\r\n", 1)[0]
            parts = status.split(b" ", 2)
            if len(parts) < 2 or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1") or parts[1] != b"200":
                raise _SocksError(5, f"上游拒绝 CONNECT：{status.decode('latin-1', 'replace')}")
            leftover = resp.split(b"\r\n\r\n", 1)[1]
            # 客户端认证/请求阶段已经结束，别残留最后一个字段的极短读预算。
            client.settimeout(self.HANDSHAKE_TIMEOUT)
            self._reply(client, 0)
            if leftover:
                client.sendall(leftover)
            return up
        except Exception:
            try:
                up.close()
            except OSError:
                pass
            raise

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
        pair = None
        try:
            t_pending = (client,)
            with self._lock:
                self._live.add(t_pending)  # 握手期间就登记，stop() 才掐得到
            try:
                stage = "handshake"
                started = time.perf_counter()
                try:
                    client.settimeout(self.HANDSHAKE_TIMEOUT)  # 握手限时，防半开连接挂死线程
                    target = self._handshake(client)
                    stage = "connect"
                    up = self._dial_upstream(client, target)
                except _SocksError as e:
                    _diagnostic("socks_session_failed", upstream=self.upstream, error=e,
                                server="socks", stage=stage, rep=e.rep,
                                elapsed_ms=int((time.perf_counter() - started) * 1000))
                    self._note_error(e)
                    if e.rep is not None:
                        self._reply(client, e.rep)
                    return
                except (OSError, ConnectionError) as e:
                    _diagnostic("socks_session_failed", upstream=self.upstream, error=e,
                                server="socks", stage=stage,
                                elapsed_ms=int((time.perf_counter() - started) * 1000))
                    self._note_error(_SocksError(1, str(e) or "连接中断"))
                    self._reply(client, 1)
                    return
                up.settimeout(None)      # 同 RelayServer：超时不得残留到长连接
                _tune_sock(up)
                client.settimeout(None)
                pair = (client, up)
                with self._lock:
                    self._live.add(pair)
                    stopped = not self._on
                if stopped:
                    return
                t1 = threading.Thread(target=self._pipe, args=(client, up, dev, False), daemon=True)
                t1.start()
                # 当前工作线程直接承担下行，每连接只需两个线程。
                try:
                    self._pipe(up, client, dev, True)
                finally:
                    t1.join()
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
                self._live.discard(pair)
                dev["conns"] -= 1
                dev["last"] = time.monotonic()
                self.active_conns -= 1

    def _pipe(self, src, dst, dev, down: bool):
        """实时快照包含小流量，累计计账仍按批次入锁。"""
        _pipe_with_stats(self, src, dst, dev, down, include_total=True)


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

class HttpServer(_ManagedListener):
    """127.0.0.1:listen_port 的本机代理入口：不解协议，按池逐台拨上游，接通后
    双向搬字节——CONNECT 与明文 GET 都是上游认识的字节流，零解析即全兼容。
    系统代理指向这里，流量才经过本工具，分流/故障切换才有可能。"""

    bind_host = '127.0.0.1'
    server_kind = 'http'

    def __init__(self, listen_port: int, pool: UpstreamPool):
        self._init_listener()
        self.listen_port = listen_port
        self.pool = pool   # 换池对外部生效（set 后新连接用新池），运行中可热切
        self._srv = None
        self._on = False
        self.rejected_conns = 0
        self._lock = threading.Lock()
        self.active_conns = 0   # 当前 TCP 连接总数
        self.stats = {}         # 上游 -> {"conns","bd","bu"}，锁内更新；换池后旧条目保留供 UI 显示
        self._live = set()      # 活跃连接对 (client, up)，stop 时强制断开用
        self._pending = {}  # 管道 -> (计账记录, 下行标记, 单写者待入账字节槽)


    def _pump(self, client):
        with self._lock:
            self.active_conns += 1
        up = pair = st = generation = None
        t_pending = (client,)
        try:
            with self._lock:
                self._live.add(t_pending)  # 拨上游前就登记，stop() 才掐得到在途连接
            try:
                up = None
                tried = set()
                deadline = time.monotonic() + TOTAL_DIAL_TIMEOUT_SECS
                while self._on:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    attempt = self.pool.acquire_attempt(tried)
                    if attempt is None:
                        break
                    cand, generation = attempt
                    tried.add(cand)
                    success = False
                    started = time.perf_counter()
                    try:
                        up = _dial_connection(cand, timeout=min(DIAL_TIMEOUT_SECS, remaining))
                        success = True
                    except OSError as error:
                        _diagnostic("upstream_dial_failed", upstream=cand, error=error,
                                    server="http", stage="dial", attempts=len(tried),
                                    elapsed_ms=int((time.perf_counter() - started) * 1000))
                        continue
                    finally:
                        self.pool.finish_attempt(cand, generation, success)
                    break
                if up is None:
                    if self._on:
                        _diagnostic("upstream_unavailable", server="http", stage="dial",
                                    attempts=len(tried))
                    client.close()  # 池里全连不上，别让客户端 socket 干等
                    return
                if len(tried) > 1:
                    _diagnostic("upstream_failover", upstream=cand, server="http", stage="dial",
                                attempts=len(tried))
            finally:
                with self._lock:
                    self._live.discard(t_pending)
            up.settimeout(None)  # create_connection 的 timeout 会残留到 recv，掐断 keep-alive
            _tune_sock(up)
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
            t1.start()
            # 当前工作线程直接承担下行，每连接只需两个线程。
            try:
                self._pipe(up, client, st, True)
            finally:
                t1.join()
        finally:
            self.pool.release_attempt(generation)
            for sock in (client, up):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            with self._lock:
                self._live.discard(t_pending)
                self._live.discard(pair)
                if st is not None:
                    st["conns"] -= 1
                self.active_conns -= 1

    def snapshot(self):
        """界面轮询用：(连接总数, {上游: (连接数, 累计下行, 累计上行)})"""
        with self._lock:
            pending = _pending_bytes(self._pending)
            return self.active_conns, {
                u: (st["conns"], st["bd"] + pending.get(id(st), (0, 0))[0],
                    st["bu"] + pending.get(id(st), (0, 0))[1])
                for u, st in self.stats.items()}

    def _pipe(self, src, dst, st, down: bool):
        """按上游统计，批次间已发送的小流量由快照叠加。"""
        _pipe_with_stats(self, src, dst, st, down)


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


def _upstream_key(ip, port):
    """元数据按规范地址关联；端口补零、主机名大小写不会产生重复记录。"""
    return f"{str(ip).strip().lower()}:{int(port)}"


def _upstream_options(preset, ip, port):
    options = preset.get("upstream_options", {})
    value = options.get(_upstream_key(ip, port), {}) if isinstance(options, dict) else {}
    if not isinstance(value, dict):
        value = {}
    note = value.get("name", "")
    note = " ".join(note.split())[:16] if isinstance(note, str) else ""
    return {"name": note, "enabled": value.get("enabled", True) is not False}


def _set_upstream_options(preset, ip, port, note="", enabled=True):
    options = preset.setdefault("upstream_options", {})
    key = _upstream_key(ip, port)
    if note or not enabled:
        options[key] = {"name": note, "enabled": bool(enabled)}
    else:
        options.pop(key, None)
    if not options:
        preset.pop("upstream_options", None)


def _enabled_upstreams(preset):
    return [up for up in _to_addr_pairs(preset["upstreams"])
            if _upstream_options(preset, *up)["enabled"]]


def validate_upstreams(pairs: list, listener_ports=()):
    """开代理前的池门槛校验：可用返回 None，否则返回人话错误（空池/坏条目都拦）"""
    if not pairs:
        return "上游池为空，请先添加至少一台上游"
    for ip, port in pairs:
        if not _valid_upstream(ip, str(port)):
            return f"上游 {ip}:{port} 格式不对（地址需 IP/主机名，端口 1-65535）"
        if _local_target(ip, port, listener_ports):
            return f"上游 {ip}:{port} 指向本工具监听端口，会造成自循环，请使用其他代理端口"
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
        seen = set()
        for u in ups:
            if isinstance(u, (list, tuple)) and len(u) == 2:
                ip, port = str(u[0]).strip(), str(u[1]).strip()
                if _valid_upstream(ip, port) and _upstream_key(ip, port) not in seen:
                    entries.append([ip, port])
                    seen.add(_upstream_key(ip, port))
        if entries:
            ptype = p.get("type")
            if ptype not in ("single", "multi"):
                ptype = "single" if len(entries) == 1 else "multi"
            elif ptype == "single" and len(entries) >= 2:
                ptype = "multi"
            preset = {"name": str(p.get("name") or "未命名"), "type": ptype, "upstreams": entries}
            raw_options = p.get("upstream_options", {})
            canonical = {}
            if isinstance(raw_options, dict):
                for key, value in raw_options.items():
                    try:
                        host, port = str(key).rsplit(":", 1)
                        if _valid_upstream(host.strip(), port.strip()):
                            canonical[_upstream_key(host, port)] = value
                    except (ValueError, TypeError):
                        continue
            for ip, port in entries:
                option = _upstream_options({"upstream_options": canonical}, ip, port)
                _set_upstream_options(preset, ip, port, option["name"], option["enabled"])
            presets.append(preset)
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


def _run_diagnostic_checks(plan, cancel, progress=None):
    """独立诊断快照；不写配置、不改变健康分流，仅后台顺序探测。"""
    report = {"version": APP_VERSION, "started_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
              "status": "running", "results": [],
              "scope": "本机检查；共享监听成功不代表其他设备可达。耗时不是带宽测速。"}

    def record(target, stage, status, detail, started=None):
        row = {"target": target, "stage": stage, "status": status, "detail": detail,
               "time": time.strftime("%Y-%m-%d %H:%M:%S %z")}
        if started is not None:
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        report["results"].append(row)
        if progress:
            progress(copy.deepcopy(report))

    for item in plan:
        if cancel.is_set():
            break
        host, port = item["address"]
        target = f"{item['label']} ({host}:{port})"
        if item.get("disabled"):
            record(target, "配置", "skipped", "该上游已停用，未发起网络请求")
            continue
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=DIAL_TIMEOUT_SECS):
                pass
        except OSError as error:
            status = "failed" if item.get("running", True) else "skipped"
            record(target, "TCP 端口", status,
                   _short_conn_error(error) if status == "failed" else "服务未开启，端口未响应", started)
            continue
        if not item.get("running", True):
            record(target, "TCP 端口", "warning", "本工具服务未开启，但端口有响应；可能由其他程序占用", started)
            continue
        record(target, "TCP 端口", "passed", "端口可连接；尚未证明代理能访问外网", started)
        if not item.get("https", True):
            record(target, "SOCKS5", "skipped", "仅验证本机监听；未测试认证、转发及其他设备可达性")
            continue
        for target_name, site, path in TEST_TARGETS:
            if cancel.is_set():
                break
            connection = None
            phase = "CONNECT/TLS"
            started = time.monotonic()
            try:
                connection = http.client.HTTPSConnection(
                    host, port, timeout=TEST_TIMEOUT_SECS, context=ssl.create_default_context())
                connection.set_tunnel(site, port=443)
                connection.connect()
                record(target, f"{target_name} · {phase}", "passed", "隧道及证书校验通过", started)
                if cancel.is_set():
                    break
                phase = "HTTPS 响应"
                started = time.monotonic()
                connection.request("GET", path, headers={"Host": site, "Connection": "close",
                                                         "User-Agent": "ProxyTool/2.0 Diagnostics"})
                with connection.getresponse() as response:
                    if response.status != 204:
                        raise _ProbeHttpError(response.status)
                record(target, f"{target_name} · {phase}", "passed", "HTTP 204；该目标访问成功", started)
                break
            except (OSError, http.client.HTTPException, _ProbeHttpError) as error:
                record(target, f"{target_name} · {phase}", "failed", _short_conn_error(error), started)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
    report["status"] = "cancelled" if cancel.is_set() else "completed"
    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
    return report


def _format_diagnostic_report(report):
    labels = {"passed": "通过", "failed": "失败", "warning": "注意", "skipped": "未测试"}
    lines = ["代理诊断报告", f"开始：{report['started_at']}",
             "状态：" + {"running": "检查中", "completed": "检查完成", "cancelled": "已取消",
                        "error": "检查意外中止"}.get(report["status"], report["status"]), report["scope"],
             "单个目标失败后会复查另一目标；部分失败不代表整个代理不可用。", ""]
    for row in report["results"]:
        elapsed = f" · {row['elapsed_ms']} ms" if "elapsed_ms" in row else ""
        lines.append(f"[{labels[row['status']]}] {row['target']} / {row['stage']}{elapsed}\n  {row['detail']}")
    return "\n".join(lines)


def _export_diagnostic_report(destination, report):
    """只导出诊断结果和脱敏日志，不打包用户配置或凭据。"""
    report = copy.deepcopy(report)
    files = {}
    current = _DIAGNOSTICS
    try:
        if current is not None:
            files = current.snapshot()
        report["logs"] = "included" if files else "unavailable"
    except OSError:
        report["logs"] = "unavailable"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(destination)),
                                         suffix=".tmp", delete=False) as stream:
            temporary = stream.name
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("report.txt", _format_diagnostic_report(report) +
                             ("\n\n日志不可用，本包仅包含诊断结果。" if not files else ""))
            for name, content in files.items():
                archive.writestr("logs/" + name, content)
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class _ProbeHttpError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"测试站点返回 HTTP {status}（未得到预期的 204 响应）")


class _ConnectivityFailure(Exception):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("；".join(f"{name}：{error}" for name, error in attempts))


def _probe_upstream(ip, port):
    """逐台显式走 HTTP 代理的 CONNECT + TLS，不受系统代理/PAC/绕过列表干扰。

    验证证书且要求 204；一个测试站点失败后尝试另一个。失败仅代表本次探测
    未通过，不能据此断言整个代理池不可用。每次尝试都释放连接。
    """
    failures = []
    for name, host, path in TEST_TARGETS:
        connection = None
        start = time.perf_counter()
        try:
            connection = http.client.HTTPSConnection(
                ip, port, timeout=TEST_TIMEOUT_SECS, context=ssl.create_default_context())
            connection.set_tunnel(host, port=443)
            connection.request("GET", path, headers={
                "Host": host,
                "User-Agent": "ProxyTool/2.0 ConnectivityCheck",
                "Connection": "close",
            })
            with connection.getresponse() as response:
                if response.status != 204:
                    raise _ProbeHttpError(response.status)
            return True, round((time.perf_counter() - start) * 1000)
        except (OSError, http.client.HTTPException, _ProbeHttpError) as error:
            failures.append((name, error))
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
    return False, _ConnectivityFailure(failures)


# 连接失败原因的人话映射（errno / Windows socket 错误码）
_CONN_ERR_HINTS = {
    10061: "连不上代理（代理没开，或地址/端口不对）",
    10060: "连接或响应超时（无响应）",
    11001: "代理主机名解析不了（检查地址是否填对）",
    10054: "连接被重置（端口不对或被拦截）",
}


def _friendly_conn_error(reason) -> str:
    if isinstance(reason, _ConnectivityFailure):
        return "；".join(f"{name}：{_friendly_conn_error(error)}"
                        for name, error in reason.attempts)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"HTTPS 证书校验失败（检查系统时间、证书或上游 HTTPS 检查）：{reason}"
    if isinstance(reason, OSError) and "Tunnel connection failed:" in str(reason):
        return f"上游拒绝 HTTPS 隧道：{reason}"
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
    if isinstance(reason, _ConnectivityFailure):
        labels = list(dict.fromkeys(_short_conn_error(error) for _, error in reason.attempts))
        return labels[0] if len(labels) == 1 else "测试未通"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "证书错误"
    if isinstance(reason, _ProbeHttpError):
        return f"HTTP{reason.status}"
    if isinstance(reason, OSError) and "Tunnel connection failed:" in str(reason):
        return "隧道拒绝"
    if isinstance(reason, http.client.HTTPException):
        return "协议错误"
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


def _indicator_images(root, bg: str) -> dict:
    """自绘 20px 复选/单选指示器位图，替代主题指示器（clam 画叉、经典控件
    指示器绑死字体高度都不可控）；cb0/cb1=复选未选/选中，rb0/rb1=单选未选/选中"""
    size, border, blue, white = 20, "#707070", "#0078d4", "#ffffff"

    def seg_d(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def make(kind: str) -> tk.PhotoImage:
        img = tk.PhotoImage(master=root, width=size, height=size)
        rows = []
        for y in range(size):
            row = []
            for x in range(size):
                px, py = x + 0.5, y + 0.5
                d = math.hypot(px - 10, py - 10)
                c = bg                                  # 圈外填界面底色，视觉无缝
                if kind.startswith("rb"):               # 单选：2px 灰环 + 选中蓝点
                    if d <= 9.5:
                        c = border if d > 7.2 else (blue if kind == "rb1" and d <= 5.0 else white)
                else:                                    # 复选：2px 灰框，选中蓝底白勾
                    inb = 1.5 <= px <= 18.5 and 1.5 <= py <= 18.5
                    if inb:
                        c = border if kind == "cb0" and not (3.5 <= px <= 16.5 and 3.5 <= py <= 16.5) else (
                            blue if kind == "cb1" else white)
                        if kind == "cb1" and (seg_d(px, py, 5.5, 10.5, 8.8, 13.8) < 1.7
                                              or seg_d(px, py, 8.2, 13.2, 14.5, 6.2) < 1.7):
                            c = white
                row.append(c)
            rows.append("{" + " ".join(row) + "}")
        img.put(" ".join(rows), to=(0, 0, size, size))
        return img

    return {k: make(k) for k in ("cb0", "cb1", "rb0", "rb1")}

class ProxyApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        style = ttk.Style(root)
        style.theme_use("clam")
        self._bg = "#ffffff"
        self._shell_bg = "#f1f5f9"
        root.configure(background=self._shell_bg)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style.configure(".", font=("Microsoft YaHei UI", 10), background=self._bg,
                        foreground="#172b4d", bordercolor="#dbe3ef")
        style.configure("TFrame", background=self._bg)
        style.configure("Shell.TFrame", background=self._shell_bg)
        style.configure("Card.TFrame", background=self._bg, relief="flat")
        style.configure("TLabel", background=self._bg, foreground="#172b4d")
        style.configure("Title.TLabel", background=self._shell_bg,
                        font=("Microsoft YaHei UI", 19, "bold"), foreground="#12213a")
        style.configure("Subtitle.TLabel", background=self._shell_bg, foreground="#64748b",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Muted.TLabel", foreground="#64748b", font=("Microsoft YaHei UI", 9))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Metric.TLabel", font=("Segoe UI", 18, "bold"), foreground="#19345b")
        style.configure("Badge.TLabel", foreground="#2563eb", background="#eff6ff", padding=(9, 3),
                        font=("Microsoft YaHei UI", 9))
        style.configure("Logo.TLabel", foreground="white", background="#2563eb", padding=(12, 8),
                        font=("Segoe UI", 16, "bold"))
        style.configure("TButton", padding=(10, 7), background="#f8fafc", foreground="#334155",
                        borderwidth=1, focusthickness=0, relief="flat")
        style.map("TButton", background=[("disabled", "#f1f5f9"), ("pressed", "#dbeafe"),
                                          ("active", "#e8eef7")],
                  foreground=[("disabled", "#94a3b8")])
        for name in ("Off.TButton", "Primary.TButton"):
            style.configure(name, background="#2563eb", foreground="white", bordercolor="#2563eb",
                            padding=(14, 9), font=("Microsoft YaHei UI", 10, "bold"))
            style.map(name, background=[("disabled", "#cbd5e1"), ("pressed", "#1e40af"),
                                        ("active", "#1d4ed8")], foreground=[("disabled", "#f8fafc")])
        style.configure("On.TButton", background="#edf2f8", foreground="#334155", padding=(14, 9))
        style.map("On.TButton", background=[("active", "#fee2e2"), ("pressed", "#fecaca")],
                  foreground=[("active", "#b91c1c")])
        style.configure("TEntry", padding=(8, 7), fieldbackground="white", bordercolor="#dbe3ef")
        style.map("TEntry", bordercolor=[("focus", "#2563eb")])
        style.configure("TCombobox", padding=(8, 6), fieldbackground="white", arrowsize=14)
        style.map("TCombobox", fieldbackground=[("readonly", "white")],
                  selectbackground=[("readonly", "#eff6ff")], selectforeground=[("readonly", "#172b4d")])
        style.configure("Invalid.TEntry", fieldbackground="#fff1f2", bordercolor="#f87171")
        style.configure("Treeview", rowheight=28, background="white", fieldbackground="white",
                        font=("Microsoft YaHei UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", padding=(8, 9), background="#f1f5f9",
                        foreground="#64748b", font=("Microsoft YaHei UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#1e40af")])
        style.map("Treeview.Heading", background=[("active", "#e2e8f0")])
        # 指示器放大：自绘 20px 位图（勾/圆点）替换主题指示器，文字大小不受影响
        self._ind_imgs = _indicator_images(root, self._bg)
        style.element_create("BigCb.indicator", "image",
                             self._ind_imgs["cb0"], ("selected", self._ind_imgs["cb1"]))
        style.element_create("BigRb.indicator", "image",
                             self._ind_imgs["rb0"], ("selected", self._ind_imgs["rb1"]))
        for ctl, ind in (("Checkbutton", "BigCb"), ("Radiobutton", "BigRb")):
            style.layout(f"Big.T{ctl}",
                [(f"{ctl}.padding", {"sticky": "nswe", "children": [
                    (f"{ind}.indicator", {"side": "left", "sticky": ""}),
                    (f"{ctl}.focus", {"side": "left",
                                      "sticky": "w" if ctl == "Checkbutton" else "",
                                      "children": [(f"{ctl}.label", {"sticky": "nswe"})]})]})])
        icons = {}
        for size in _ICON_SIZES:
            try:
                with open(os.path.join(_ASSET_DIR, f"lan-proxy-{size}.png"), "rb") as source:
                    icons[size] = tk.PhotoImage(master=root, data=source.read())
            except (OSError, tk.TclError):
                continue  # 图标文件缺失或损坏不能阻止代理工具启动。
        self._window_icons = list(icons.values())  # 持有引用，避免 Tk 图片被回收。
        if self._window_icons:
            root.iconphoto(True, *self._window_icons)
            self._logo_image = icons[min(icons, key=lambda size: abs(size - 48))]
            scale = math.ceil(max(self._logo_image.width(), self._logo_image.height()) / 48)
            if scale > 1:
                self._logo_image = self._logo_image.subsample(scale, scale)
        else:
            self._logo_image = None
        root.title(APP_TITLE)
        root.resizable(True, False)
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
        self._test_results = {}   # "ip:port" -> (ok, ms|原因对象)，收齐 total 条才恢复按钮
        self._adding = False      # 「新增」模式：填新预设期间，开关/共享/删除暂禁用
        self._draft = None        # 新增模式的草稿预设 {"name","type","upstreams"}；存才落盘
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
        self._health_monitor = HealthMonitor(self._pool)
        self._health_start_error = False
        self._manual_test_times = {}
        self._diagnostics_win = None
        self._diagnosis_win = None
        self._diagnosis_busy = False
        self._diagnosis_cancel = threading.Event()
        self._entry = None               # 运行中的 HttpServer（本机入口，多代理分流）
        self._up_bytes_prev = {}
        self._metric_prev = None
        shell = ttk.Frame(root, style="Shell.TFrame", padding=(20, 12))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell, style="Shell.TFrame")
        header.pack(fill="x", pady=(0, 14))
        if self._logo_image is not None:
            ttk.Label(header, image=self._logo_image, style="Subtitle.TLabel").pack(
                side="left", padx=(0, 12))
        else:
            ttk.Label(header, text="LP", style="Logo.TLabel").pack(side="left", padx=(0, 12))
        heading = ttk.Frame(header, style="Shell.TFrame")
        heading.pack(side="left")
        ttk.Label(heading, text="局域网代理", style="Title.TLabel").pack(anchor="w")
        ttk.Label(heading, text="集中管理上游连接与局域网共享", style="Subtitle.TLabel").pack(anchor="w")
        ttk.Button(header, text="设置", width=5, command=self.open_settings).pack(side="right")
        ttk.Button(header, text="诊断", width=5, command=self.open_diagnosis).pack(side="right", padx=(0, 6))
        ttk.Button(header, text="日志", width=5, command=self.open_diagnostics).pack(
            side="right", padx=(0, 6))
        ttk.Label(header, text=APP_VERSION, style="Badge.TLabel").pack(side="right", padx=(8, 12))

        status_card = ttk.Frame(shell, style="Card.TFrame", padding=(18, 12))
        status_card.pack(fill="x", pady=(0, 10))
        status_card.columnconfigure(0, weight=1)
        ttk.Label(status_card, text="连接状态", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="● 代理未开启")
        self.status_label = ttk.Label(status_card, textvariable=self.status_var, style="Status.TLabel",
                                      wraplength=490)
        self.status_label.grid(row=1, column=0, sticky="w", pady=(4, 2))
        self.btn = ttk.Button(status_card, text="开启代理", style="Off.TButton", width=12, command=self.toggle)
        self.btn.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))
        metrics = ttk.Frame(status_card)
        metrics.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.metric_down = tk.StringVar(value="—")
        self.metric_up = tk.StringVar(value="—")
        self.metric_conns = tk.StringVar(value="0")
        for col, (title, variable) in enumerate((("下载速率", self.metric_down),
                                               ("上传速率", self.metric_up),
                                               ("当前连接", self.metric_conns))):
            metrics.columnconfigure(col, weight=1, uniform="metric")
            tile = ttk.Frame(metrics)
            tile.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 20, 0))
            ttk.Label(tile, text=title, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(tile, textvariable=variable, style="Metric.TLabel").pack(anchor="w")
        self.entry_speed_var = tk.StringVar()
        self.entry_speed_label = ttk.Label(status_card, textvariable=self.entry_speed_var, style="Muted.TLabel")
        self.entry_speed_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        config_card = ttk.Frame(shell, style="Card.TFrame", padding=(18, 12))
        config_card.pack(fill="x", pady=(0, 10))
        config_heading = ttk.Frame(config_card)
        config_heading.pack(fill="x", pady=(0, 8))
        ttk.Label(config_heading, text="上游配置", style="Section.TLabel").pack(side="left")
        self.health_summary_var = tk.StringVar(value="HTTPS：等待检测")
        ttk.Label(config_heading, textvariable=self.health_summary_var, style="Muted.TLabel").pack(
            side="left", padx=(12, 0))
        self.mode_var = tk.StringVar(value="多代理 · 按负载分流")
        ttk.Label(config_heading, textvariable=self.mode_var, style="Badge.TLabel").pack(side="right")
        frm = ttk.Frame(config_card)
        frm.pack(fill="x")
        frm.columnconfigure(1, weight=1)
        pad = {"padx": (0, 10), "pady": 6}
        ttk.Label(frm, text="当前预设", style="Muted.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self.preset_frm = ttk.Frame(frm)
        self.preset_frm.grid(row=0, column=1, sticky="ew", pady=6)
        self.preset_box = ttk.Combobox(self.preset_frm, textvariable=self.preset_var, width=28,
                                       values=[p["name"] for p in self.presets])
        self.preset_box.pack(side="left", fill="x", expand=True)
        self.preset_box.bind("<<ComboboxSelected>>", self.on_preset_selected)
        self.save_preset_btn = ttk.Button(self.preset_frm, text="新增", width=5, command=self.on_add_or_save)
        self.save_preset_btn.pack(side="left", padx=(8, 0))
        self.delete_preset_btn = ttk.Button(self.preset_frm, text="删除", width=4, command=self.delete_preset)
        self.delete_preset_btn.pack(side="left", padx=(6, 0))

        self.addr_lbl = ttk.Label(frm, text="上游地址", style="Muted.TLabel")
        self.addr_lbl.grid(row=1, column=0, sticky="w", **pad)
        self.ip_var = tk.StringVar()
        self.ip_entry = ttk.Entry(frm, textvariable=self.ip_var, width=32)
        self.ip_entry.grid(row=1, column=1, sticky="ew", pady=6)
        self.port_lbl = ttk.Label(frm, text="上游端口", style="Muted.TLabel")
        self.port_lbl.grid(row=2, column=0, sticky="w", **pad)
        self.port_frm = ttk.Frame(frm)
        self.port_frm.grid(row=2, column=1, sticky="ew", pady=6)
        self.port_var = tk.StringVar()
        self.port_entry = ttk.Entry(self.port_frm, textvariable=self.port_var, width=14)
        self.port_entry.pack(side="left")
        ttk.Label(self.port_frm, text="HTTP 代理端口", style="Muted.TLabel").pack(side="left", padx=12)
        ttk.Button(self.port_frm, text="备注 / 编辑", command=self.edit_upstream).pack(side="left")
        self.single_enable_btn = ttk.Button(self.port_frm, text="启用", width=4,
                                            command=self.toggle_upstream)
        self.single_note_var = tk.StringVar()
        ttk.Label(self.port_frm, textvariable=self.single_note_var, style="Muted.TLabel").pack(
            side="left", padx=(10, 0))
        for ent in (self.ip_entry, self.port_entry):
            ent.bind("<Return>", lambda _e: self.on_add_or_save() if self._adding else self._commit_single_edit())
            ent.bind("<FocusOut>", lambda _e: None if self._adding else self._commit_single_edit())
        self.ip_var.trace_add("write", self._on_addr_changed)
        self.port_var.trace_add("write", self._on_addr_changed)

        self.add_lbl = ttk.Label(frm, text="添加上游", style="Muted.TLabel")
        self.add_lbl.grid(row=1, column=0, sticky="w", **pad)
        self.add_frm = ttk.Frame(frm)
        self.add_frm.grid(row=1, column=1, sticky="ew", pady=6)
        self.add_var = tk.StringVar()
        self.add_entry = ttk.Entry(self.add_frm, textvariable=self.add_var, width=30)
        self.add_entry.pack(side="left", fill="x", expand=True)
        self.add_entry.bind("<Return>", lambda _e: self.add_upstream())
        self.add_up_btn = ttk.Button(self.add_frm, text="添加", width=5, command=self.add_upstream)
        self.add_up_btn.pack(side="left", padx=(8, 0))
        self.edit_up_btn = ttk.Button(self.add_frm, text="编辑", width=4, command=self.edit_upstream)
        self.edit_up_btn.pack(side="left", padx=(6, 0))
        self.toggle_up_btn = ttk.Button(self.add_frm, text="停用", width=4, command=self.toggle_upstream)
        self.toggle_up_btn.pack(side="left", padx=(6, 0))
        ttk.Button(self.add_frm, text="移除", width=4, command=self.remove_upstream).pack(
            side="left", padx=(6, 0))
        self.pool_frm = ttk.Frame(frm)
        self.pool_frm.grid(row=2, column=1, sticky="ew", pady=(6, 4))
        table = ttk.Frame(self.pool_frm)
        table.pack(side="left", fill="x", expand=True)
        self.pool_box = ttk.Treeview(table, columns=("name", "addr", "down", "up", "conns", "test"),
                                     show="headings", height=3, selectmode="browse")
        cell_font = tkfont.Font(root, font=style.lookup("Treeview", "font"))
        heading_font = tkfont.Font(root, font=style.lookup("Treeview.Heading", "font"))
        table_width = 0
        for col, text, width, anchor, sample in (
                ("name", "备注", 120, "w", "生产主链路…"),
                ("addr", "上游地址", 240, "w", "192.168.100.100:65535"),
                ("down", "下载", 112, "e", "999.9 MB/s"),
                ("up", "上传", 112, "e", "999.9 MB/s"),
                ("conns", "连接", 70, "e", "256"),
                ("test", "连通性", 132, "w", "✗ HTTPS异常")):
            width = max(width, cell_font.measure(sample) + 24, heading_font.measure(text) + 24)
            table_width += width
            self.pool_box.heading(col, text=text)
            self.pool_box.column(col, width=width, minwidth=width, anchor=anchor,
                                 stretch=(col == "addr"))
        self.pool_box.tag_configure("even", background="#f8fafc")
        self.pool_box.tag_configure("disabled", foreground="#94a3b8")
        self.pool_box.bind("<<TreeviewSelect>>", lambda _event: self._update_upstream_actions())
        self.pool_box.bind("<Double-1>", self._on_upstream_double_click)
        self.pool_box.pack(side="left", fill="x", expand=True)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.pool_box.yview)
        scrollbar.pack(side="right", fill="y")
        self.pool_box.configure(yscrollcommand=scrollbar.set)
        # 给表格完整列宽，模式切换也保持同宽；额外空间优先留给较长的上游地址。
        frm.columnconfigure(1, minsize=table_width + scrollbar.winfo_reqwidth() + 2)
        self._single_widgets = (self.addr_lbl, self.ip_entry, self.port_lbl, self.port_frm)
        self._multi_widgets = (self.add_lbl, self.add_frm, self.pool_frm)

        test_frm = ttk.Frame(frm)
        test_frm.grid(row=3, column=1, sticky="ew", pady=(8, 0))
        self.test_btn = ttk.Button(test_frm, text="测试连通", width=8, command=self.test_connectivity)
        self.test_btn.pack(side="left")
        self.test_details_btn = ttk.Button(test_frm, text="详情", width=4, command=self.show_test_details)
        self.test_details_btn.pack(side="left", padx=(6, 0))
        self.test_result_var = tk.StringVar()
        self.test_result_label = ttk.Label(test_frm, textvariable=self.test_result_var, style="Muted.TLabel",
                                          wraplength=280)
        self.test_result_label.pack(side="left", padx=(10, 0))

        share_card = ttk.Frame(shell, style="Card.TFrame", padding=(18, 12))
        share_card.pack(fill="x", pady=(0, 10))
        share_heading = ttk.Frame(share_card)
        share_heading.pack(fill="x")
        ttk.Label(share_heading, text="局域网共享", style="Section.TLabel").pack(side="left")
        ttk.Button(share_heading, text="客户端", width=6, command=self.open_clients).pack(side="right")
        self.share_btn = ttk.Button(share_heading, text="开启共享", width=8, command=self.toggle_share)
        self.share_btn.pack(side="right", padx=(0, 8))
        self.share_status_var = tk.StringVar()
        self.share_status_label = ttk.Label(share_card, textvariable=self.share_status_var, style="Muted.TLabel",
                                           justify="left", anchor="w", wraplength=640)
        self.share_status_label.pack(fill="x", pady=(10, 0))
        self._set_share_status(self._share_off_text())
        footer = ttk.Frame(shell, style="Shell.TFrame")
        footer.pack(fill="x")
        self.autostart_var = tk.BooleanVar(value=get_autostart())
        self.autostart_cb = ttk.Checkbutton(footer, text="开机自动启动", variable=self.autostart_var,
                                           command=self.toggle_autostart, style="Big.TCheckbutton")
        self.autostart_cb.pack(side="left")
        self.footer_hint = ttk.Label(footer, text="关闭窗口后在托盘中运行", style="Subtitle.TLabel")
        self.footer_hint.pack(side="right")
        self._on_addr_changed()
        self._apply_form()
        self._refresh_pool_box()
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), 1)
        self._restore_entry()
        self.refresh_status()

        self._start_health_monitor()
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
            self.footer_hint.configure(text="托盘不可用，关闭窗口将退出")

    # ---- 预设 ----

    def _current_preset(self) -> dict:
        if self._draft is not None:  # 新增模式：池/表跟随草稿，落盘前与 presets 隔离
            return self._draft
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
        两组控件共用主网格行列，切形态即整组显隐（标签列统一、起点对齐）；
        切形态时窗口高度变化是预期行为（布局变化），流量刷新不再撑动窗口。"""
        single = self._current_preset()["type"] == "single"
        self.mode_var.set("单代理" if single else "多代理 · 按负载分流")
        for w in self._single_widgets:
            (w.grid if single else w.grid_remove)()
        for w in self._multi_widgets:
            (w.grid_remove if single else w.grid)()
        if single:
            self.entry_speed_label.grid()
            ip, port = (self._pool_addrs(include_disabled=True) or [("", "")])[0]
            self.ip_var.set(ip)          # set 会触发 trace：红框复位、旧测试结果作废
            self.port_var.set(str(port))
            note = _upstream_options(self._current_preset(), ip, port)["name"] if ip else ""
            self.single_note_var.set(note)
            option = _upstream_options(self._current_preset(), ip, port) if ip else {"enabled": True}
            if not option["enabled"]:
                self.single_enable_btn.pack(side="left", padx=(8, 0))
            else:
                self.single_enable_btn.pack_forget()
        else:
            self.entry_speed_label.grid_remove()
            self.entry_speed_var.set("")
            self.ip_var.set("")          # 多形态两格隐藏，清空防编辑语义混用
            self.port_var.set("")
        self.add_var.set("")
        # 切换形态时按内容重算高度，避免池表被旧窗口尺寸截断。
        self.root.geometry("")

    def _restore_entry(self):
        """开机自启后注册表还指着本机入口：把入口拉起来；池坏或端口被占就摘注册表，
        绝不挂着"已开启"的名头让全机流量指向没人听的端口"""
        enabled, server = get_system_proxy()
        if not enabled or _parse_entry_addr(server) != self.entry_port:
            return
        addrs = self._pool_addrs()
        if validate_upstreams(addrs, (self.entry_port,)):
            disable_system_proxy()  # 池空/坏：入口没法有意义地跑，先恢复直连
            return
        try:
            self._pool.set(addrs, preserve_health=True)
            self._entry = HttpServer(self.entry_port, self._pool)
            self._entry.start()
        except (OSError, RuntimeError) as error:
            _diagnostic("entry_restore_failed", error=error, stage="startup", server="http")
            self._entry = None
            disable_system_proxy()

    # ---- 上游池 ----

    def _refresh_pool_box(self):
        """池表跟随当前预设重画；iid 用索引，移除时好对位"""
        selected = self.pool_box.selection()
        self.pool_box.delete(*self.pool_box.get_children())
        for i, (ip, port) in enumerate(self._pool_addrs(include_disabled=True)):
            option = _upstream_options(self._current_preset(), ip, port)
            note = option["name"]
            display_name = note if len(note) <= 8 else note[:7] + "…"
            tags = (("even",) if i % 2 == 0 else ()) + (() if option["enabled"] else ("disabled",))
            self.pool_box.insert("", "end", iid=str(i),
                                 values=(display_name, f"{ip}:{port}", "", "", "",
                                         "" if option["enabled"] else "已停用"), tags=tags)
        if selected and self.pool_box.exists(selected[0]):
            self.pool_box.selection_set(selected[0])
        self._update_upstream_actions()

    def _selected_upstream_index(self):
        if self._current_preset()["type"] == "single":
            return 0 if self._pool_addrs(include_disabled=True) else None
        selected = self.pool_box.selection()
        return int(selected[0]) if selected else None

    def _update_upstream_actions(self):
        index = self._selected_upstream_index()
        addrs = self._pool_addrs(include_disabled=True)
        valid = index is not None and index < len(addrs)
        self.edit_up_btn.config(state="normal" if valid else "disabled")
        enabled = _upstream_options(self._current_preset(), *addrs[index])["enabled"] if valid else True
        self.toggle_up_btn.config(state="normal" if valid else "disabled", text="停用" if enabled else "启用")

    def _commit_upstream_change(self, candidate):
        """先持久化再热切；失败时恢复预设，已有连接始终不受编辑影响。"""
        for host, port in _enabled_upstreams(candidate):
            if _local_target(host, port, (self.entry_port,)):
                messagebox.showwarning('提示', '上游不能指向本工具监听端口，请修改地址或端口', parent=self.root)
                return False
        if not self._adding and not _enabled_upstreams(candidate):
            messagebox.showinfo("提示", "至少要保留一台启用的上游", parent=self.root)
            return False
        current = self._current_preset()
        previous = copy.deepcopy(current)
        current.clear()
        current.update(candidate)
        if not self._adding and not self.save_preset():
            current.clear()
            current.update(previous)
            if current["type"] == "single":
                self._apply_form()
            self._refresh_pool_box()
            return False
        self._invalidate_test_results()
        self._refresh_pool_box()
        if self._current_preset()["type"] == "single":
            self._apply_form()
        if not self._adding:
            self._sync_pools()
        return True

    def _edit_upstream_at(self, index, ip, port, note):
        ip, port, note = str(ip).strip(), str(port).strip(), str(note).strip()
        if not _valid_upstream(ip, port):
            messagebox.showwarning("提示", "请填写正确的代理地址和端口（1-65535）", parent=self.root)
            return False
        if len(note) > 16 or any(ord(char) < 32 for char in note):
            messagebox.showwarning("提示", "备注最多 16 个字符，不能换行", parent=self.root)
            return False
        candidate = copy.deepcopy(self._current_preset())
        if not 0 <= index < len(candidate["upstreams"]):
            return False
        old = candidate["upstreams"][index]
        new_key = _upstream_key(ip, port)
        if any(i != index and _upstream_key(*up) == new_key
               for i, up in enumerate(candidate["upstreams"])):
            messagebox.showwarning("提示", "该上游地址已存在", parent=self.root)
            return False
        option = _upstream_options(candidate, *old)
        candidate.get("upstream_options", {}).pop(_upstream_key(*old), None)
        candidate["upstreams"][index] = [ip, str(int(port))]
        _set_upstream_options(candidate, ip, port, note, option["enabled"])
        return self._commit_upstream_change(candidate)

    def edit_upstream(self):
        index = self._selected_upstream_index()
        if index is None:
            return
        ip, port = self._pool_addrs(include_disabled=True)[index]
        option = _upstream_options(self._current_preset(), ip, port)
        win = tk.Toplevel(self.root)
        win.title("编辑上游")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()
        ip_var, port_var, note_var = tk.StringVar(value=ip), tk.StringVar(value=str(port)), tk.StringVar(value=option["name"])
        first = None
        for row, (label, variable) in enumerate((("地址", ip_var), ("端口", port_var), ("备注", note_var))):
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="e", padx=12, pady=8)
            entry = ttk.Entry(win, textvariable=variable, width=30)
            entry.grid(row=row, column=1, padx=12, pady=8)
            first = first or entry
        ttk.Label(win, text="备注最多 16 个字符；仅新连接使用修改后的地址。",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=2, padx=12, pady=(0, 8))
        def confirm():
            if self._edit_upstream_at(index, ip_var.get(), port_var.get(), note_var.get()):
                win.destroy()
        buttons = ttk.Frame(win)
        buttons.grid(row=4, column=0, columnspan=2, pady=(0, 12))
        ttk.Button(buttons, text="保存", command=confirm).pack(side="left", padx=6)
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="left", padx=6)
        win.bind("<Return>", lambda _event: confirm())
        win.bind("<Escape>", lambda _event: win.destroy())
        self._center_over(win)
        first.focus_set()

    def _on_upstream_double_click(self, event):
        row = self.pool_box.identify_row(event.y)
        if row:
            self.pool_box.selection_set(row)
            self.edit_upstream()

    def toggle_upstream(self):
        index = self._selected_upstream_index()
        if index is None:
            return False
        candidate = copy.deepcopy(self._current_preset())
        up = candidate["upstreams"][index]
        option = _upstream_options(candidate, *up)
        if option["enabled"] and len(_enabled_upstreams(candidate)) <= 1:
            messagebox.showinfo("提示", "至少要保留一台启用的上游", parent=self.root)
            return False
        _set_upstream_options(candidate, *up, option["name"], not option["enabled"])
        return self._commit_upstream_change(candidate)

    def _invalidate_test_results(self):
        """作废在途与既有测试结果（池/地址变了）：gen 变更的源头必须恢复按钮，
        否则一旦结果被丢弃收不回来，「测试连通」永远停在测试中"""
        self._test_gen += 1
        self._test_results = {}
        self._manual_test_times.clear()
        self.test_result_var.set("")
        self.test_btn.config(state="normal", text="测试连通")

    def _commit_single_edit(self):
        """单形态地址/端口直接编辑的落盘：回车或失焦触发；非法（红框在）或值未变不落盘"""
        cur = self._current_preset()
        if cur["type"] != "single":
            return  # 多形态下两格隐藏（如切换瞬间的 FocusOut），别把残留值写进池
        ip, port = self.ip_var.get().strip(), self.port_var.get().strip()
        if not _valid_upstream(ip, port):
            return  # 红框已在提示，静默不落盘
        if cur["upstreams"] and cur["upstreams"][0] == [ip, port]:
            return  # 值没变（每次点击都会来一发 FocusOut），别折腾
        note = _upstream_options(cur, *cur["upstreams"][0])["name"] if cur["upstreams"] else ""
        self._edit_upstream_at(0, ip, port, note)

    def add_upstream(self):
        """把单行输入框的 IP:端口 加进当前预设的池（重复的不加）；
        新增模式只进草稿，点「存预设」才落盘；常态则入口热切新连接生效"""
        parsed = parse_upstream_line(self.add_var.get())
        if not parsed:
            messagebox.showwarning("提示", "格式：IP:端口（如 192.130.30.73:7890）",
                                   parent=self.root)
            return
        ip, port = parsed
        candidate = copy.deepcopy(self._current_preset())
        if any(_upstream_key(*up) == _upstream_key(ip, port) for up in candidate["upstreams"]):
            messagebox.showinfo("提示", "该上游地址已存在", parent=self.root)
            return
        candidate["upstreams"].append([ip, str(int(port))])
        if self._commit_upstream_change(candidate):
            self.add_var.set("")

    def remove_upstream(self):
        """移除池中选中的一台；常态最后一家不许删（删空就没法分流了），
        新增模式的草稿里可以删到 0（存的时候拦「至少一台上游」）"""
        sel = self.pool_box.selection()
        if not sel:
            return
        candidate = copy.deepcopy(self._current_preset())
        removed = candidate["upstreams"].pop(int(sel[0]))
        candidate.get("upstream_options", {}).pop(_upstream_key(*removed), None)
        if not candidate.get("upstream_options"):
            candidate.pop("upstream_options", None)
        self._commit_upstream_change(candidate)

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
        self._apply_form()       # 形态/两格跟随新当前预设（此前只清两格，删完形态不切）
        self._sync_preset_box()
        self._refresh_pool_box()
        self._sync_relay_upstream()  # 共享运行中也要跟上切换后的地址
        self.save_preset()

    def save_preset(self, ups=None) -> bool:
        """把上游组存进当前名字的预设（名字是新的则新建）；ups 缺省=当前池。返回是否落盘成功"""
        if self._draft is not None and not any(p is self._draft for p in self.presets):
            return True   # 草稿隔离中：设置页保存/在途共享完成等旁路此刻不是落盘时机
        if ups is None:
            ups = [[ip, str(port)] for ip, port in self._pool_addrs(include_disabled=True)]
        name = self.preset_var.get().strip()
        if not name:
            first = ups[0] if ups else ["", ""]
            name = f"{first[0]}:{first[1]}" if first[0] else "默认"
        pending = copy.deepcopy(self.presets)
        for p in pending:
            if p["name"] == name:
                p["upstreams"] = ups
                break
        else:
            pending.append({"name": name,
                            "type": "single" if len(ups) == 1 else "multi",
                            "upstreams": ups})
        selected = next(p for p in pending if p['name'] == name)
        for host, port in _enabled_upstreams(selected):
            if _local_target(host, port, (self.entry_port,)):
                messagebox.showwarning('提示', '上游不能指向本工具监听端口，请修改地址或端口', parent=self.root)
                return False
        try:
            save_config(name, pending, self._share_cfg, self.settings["config_path"])
        except OSError as e:
            messagebox.showerror("错误", f"保存配置失败：\n{e}", parent=self.root)
            return False
        self.presets[:] = pending
        self.preset_var.set(name)
        self._sync_preset_box()
        return True

    def on_add_or_save(self):
        """按钮双态：常态「新增」弹窗选类型进新增模式；新增模式「存预设」校验+落盘并退出"""
        if self._adding:
            draft = self._draft
            if draft["type"] == "single":
                ip, port = self.ip_var.get().strip(), self.port_var.get().strip()
                if not _valid_upstream(ip, port):
                    messagebox.showwarning(
                        "提示", "请填写正确的代理地址（IP 或主机名）和端口（1-65535）",
                        parent=self.root)
                    return
                draft["upstreams"] = [[ip, port]]
            elif not draft["upstreams"]:
                messagebox.showwarning("提示", "至少添加一台上游", parent=self.root)
                return
            if not _enabled_upstreams(draft):
                messagebox.showwarning("提示", "至少要保留一台启用的上游", parent=self.root)
                return
            self.presets.append(draft)
            self._sync_preset_box()
            self.preset_var.set(draft["name"])
            if not self.save_preset():   # 落盘失败回滚内存，半成品不留
                self.presets.remove(draft)
                self._sync_preset_box()
                return
            self._exit_add_mode(restore=False)
            self._apply_form()
            self._refresh_pool_box()
        else:
            self._open_new_preset_dialog()

    def _open_new_preset_dialog(self):
        """新增预设第一站：选类型+名称；确定后才进入主界面的填写模式"""
        win = tk.Toplevel(self.root)
        win.title("新增预设")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        type_var = tk.StringVar(value="single")
        name_var = tk.StringVar()
        ttk.Label(win, text="类型").grid(row=0, column=0, sticky="e", padx=12, pady=8)
        type_frm = ttk.Frame(win)
        type_frm.grid(row=0, column=1, sticky="w", padx=12, pady=8)
        for txt, val in (("单代理（一个地址）", "single"),
                         ("多代理（上游池，按连接分流）", "multi")):
            ttk.Radiobutton(type_frm, text=txt, variable=type_var, value=val,
                            style="Big.TRadiobutton"
                            ).pack(side="left", padx=(8, 0) if val == "multi" else 0)
        ttk.Label(win, text="名称").grid(row=1, column=0, sticky="e", padx=12, pady=8)
        name_entry = ttk.Entry(win, textvariable=name_var, width=20)
        name_entry.grid(row=1, column=1, sticky="w", padx=12, pady=8)

        def confirm():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("提示", "请填写预设名称", parent=win)
                return
            if any(p["name"] == name for p in self.presets):
                messagebox.showwarning("提示", f"已有名为「{name}」的预设，请换个名字", parent=win)
                return
            win.destroy()
            self._enter_add_mode(type_var.get(), name)

        btn_frm = ttk.Frame(win)
        btn_frm.grid(row=2, column=0, columnspan=2, pady=(0, 10))
        ttk.Button(btn_frm, text="确定", command=confirm).pack(side="left", padx=6)
        ttk.Button(btn_frm, text="取消", command=win.destroy).pack(side="left", padx=6)
        name_entry.focus_set()
        win.bind("<Return>", lambda _e: confirm())
        win.bind("<Escape>", lambda _e: win.destroy())
        self._center_over(win)   # 居中于主窗口（其余弹窗同款），别冒在屏幕左上角

    def _enter_add_mode(self, ptype: str, name: str):
        """进入新增模式：草稿进内存（Esc/切预设全弃），点「存预设」才落盘"""
        self._adding = True
        self._add_backup = self.preset_var.get()
        self._draft = {"name": name, "type": ptype, "upstreams": []}
        # 下拉框实时显示草稿名（名称看得见去处；_current_preset 认 _draft 不认这个值）
        self.preset_var.set(name)
        self._invalidate_test_results()
        # 半成品不能开代理/共享；「添加」不禁——draft 的池就靠它和回车
        for w in (self.btn, self.share_btn):
            w.config(state="disabled")
        # 「删除」临时变「取消」：不新增按钮，布局零变化（删除在新增模式里本来就不可用）
        self.delete_preset_btn.config(text="取消", command=self._cancel_add_mode)
        self.save_preset_btn.config(text="存预设")
        self.ip_var.set("")
        self.port_var.set("")      # 置空即触发 trace：红框复位、旧测试结果作废
        self.add_var.set("")
        self._apply_form()         # 形态按 draft 类型（multi=空池表；single=空白两格）
        self._refresh_pool_box()
        (self.ip_entry if ptype == "single" else self.add_entry).focus_set()

    def _exit_add_mode(self, restore: bool):
        self._adding = False
        self._draft = None
        self.save_preset_btn.config(text="新增")
        self.delete_preset_btn.config(text="删除", command=self.delete_preset)
        for w in (self.btn, self.share_btn):
            w.config(state="normal")
        if restore:  # 取消/退出：回到进来之前的预设，草稿丢弃
            self.preset_var.set(self._add_backup)
            self._apply_form()
            self._refresh_pool_box()
        self._sync_pools()

    def _cancel_add_mode(self):
        self._exit_add_mode(restore=True)

    def _sync_preset_box(self):
        self.preset_box.config(values=[p["name"] for p in self.presets])

    # ---- 行为 ----

    def _start_health_monitor(self):
        try:
            self._health_monitor.start()
        except (OSError, RuntimeError) as error:
            self._health_start_error = True
            self.health_summary_var.set("HTTPS：后台检测未启动")
            _diagnostic("health_start_failed", error=error)

    def _refresh_health_status(self):
        if self._adding:
            self.health_summary_var.set("HTTPS：保存预设后检测")
            return
        if self._health_start_error:
            self.health_summary_var.set("HTTPS：后台检测未启动")
            return
        snapshots = self._pool.health_snapshot()
        counts = {state: 0 for state in ("unknown", "healthy", "suspect", "degraded", "recovering")}
        now = time.monotonic()
        enabled = set(self._pool_addrs())
        for i, up in enumerate(self._pool_addrs(include_disabled=True)):
            if up not in enabled:
                continue
            info = snapshots.get(up, {})
            state = info.get("state", "unknown")
            counts[state] = counts.get(state, 0) + 1
            iid = str(i)
            if not info.get("last_checked") or not self.pool_box.exists(iid):
                continue
            # 最新手动测试先保留；下一次更新的后台结果会自然接替它。
            manual_time = self._manual_test_times.get(f"{up[0]}:{up[1]}", 0)
            if manual_time > info["last_checked"] and now - manual_time < 30:
                continue
            if state == "healthy":
                text = f"✓ {info['latency_ms']} ms"
            elif state == "suspect":
                text = f"复查 {info['failures']}/3"
            elif state == "recovering":
                text = f"恢复 {info['successes']}/2"
            else:
                text = "降级 · " + (info.get("reason") or "检测失败")
            self.pool_box.set(iid, "test", text)
        parts = []
        for state, label in (("healthy", "正常"), ("suspect", "待复查"),
                             ("degraded", "降级"), ("recovering", "恢复中"), ("unknown", "待检测")):
            if counts[state]:
                parts.append(f"{counts[state]} {label}")
        self.health_summary_var.set("HTTPS：" + (" · ".join(parts) if parts else "等待检测"))

    def _diagnosis_plan(self):
        plan = [{"label": "上游", "address": up,
                 "disabled": not _upstream_options(self._current_preset(), *up)["enabled"]}
                for up in self._pool_addrs(include_disabled=True)]
        for label, server, configured_port, https in (
                ("本机代理入口", self._entry, self.entry_port, True),
                ("HTTP 共享", self._relay, self.share_port, True),
                ("SOCKS5 共享监听", self._socks, self.socks_port, False)):
            port = server.listen_port if server else configured_port
            plan.append({"label": label, "address": ("127.0.0.1", port),
                         "running": server is not None, "https": https})
        return plan

    def open_diagnosis(self):
        if self._diagnosis_win is not None and self._diagnosis_win.winfo_exists():
            self._diagnosis_win.lift()
            return
        if self._diagnosis_busy:
            messagebox.showinfo("诊断", "上次检查正在结束，请稍后重试。", parent=self.root)
            return
        win = self._diagnosis_win = tk.Toplevel(self.root)
        win.title("一键诊断")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="一键诊断", style="Section.TLabel").pack(anchor="w")
        ttk.Label(frame, text="按开始时的配置检查；配置或网络变化后可重新诊断。取消将在当前检查结束后生效。",
                  wraplength=680, style="Muted.TLabel").pack(anchor="w", pady=(6, 10))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        display = tk.Text(body, width=90, height=22, wrap="word", font=("Microsoft YaHei UI", 9),
                          background="#f8fafc", relief="flat", padx=8, pady=8, state="disabled")
        display.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(body, command=display.yview)
        bar.pack(side="right", fill="y")
        display.configure(yscrollcommand=bar.set)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        latest = None

        def render(report):
            nonlocal latest
            latest = report
            if win.winfo_exists():
                display.configure(state="normal")
                display.delete("1.0", "end")
                display.insert("1.0", _format_diagnostic_report(report))
                display.configure(state="disabled")
                display.see("end")

        def finish(report, error=False):
            self._diagnosis_busy = False
            if error:
                report = latest or {"started_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                                    "results": [], "scope": "检查意外中止，可查看日志后重试。"}
                report["status"] = "error"
            render(report)
            if win.winfo_exists():
                start_button.configure(state="normal")
                cancel_button.configure(state="disabled")
                export_button.configure(state="normal")

        def start():
            self._diagnosis_busy = True
            self._diagnosis_cancel = cancel = threading.Event()
            plan = self._diagnosis_plan()
            start_button.configure(state="disabled")
            export_button.configure(state="disabled")
            cancel_button.configure(state="normal")
            render({"started_at": time.strftime("%Y-%m-%d %H:%M:%S %z"), "status": "running",
                    "scope": "正在检查上游及本机监听；不会更改配置或分流状态。", "results": []})

            def work():
                try:
                    report = _run_diagnostic_checks(plan, cancel,
                        lambda value: self._ui(lambda: render(value)))
                except Exception as error:
                    _diagnostic("diagnosis_failed", error=error)
                    self._ui(lambda: finish(None, True))
                else:
                    self._ui(lambda: finish(report))
            try:
                threading.Thread(target=work, name="proxy-diagnosis", daemon=True).start()
            except RuntimeError as error:
                _diagnostic("diagnosis_failed", error=error)
                finish(None, True)

        def export():
            path = filedialog.asksaveasfilename(parent=win, title="导出诊断报告与日志",
                defaultextension=".zip", filetypes=[("ZIP 诊断包", "*.zip")],
                initialfile=time.strftime("proxy-report-%Y%m%d-%H%M%S.zip"))
            if not path:
                return
            report = copy.deepcopy(latest)
            start_button.configure(state="disabled")
            export_button.configure(state="disabled")

            def done(ok):
                if win.winfo_exists():
                    start_button.configure(state="normal")
                    export_button.configure(state="normal")
                    if ok:
                        messagebox.showinfo("导出完成", f"报告与可用日志已保存到：\n{path}", parent=win)
                    else:
                        messagebox.showerror("导出失败", "请检查保存位置是否可写后重试。", parent=win)

            def work():
                try:
                    _export_diagnostic_report(path, report)
                except Exception as error:
                    _diagnostic("report_export_failed", error=error)
                    self._ui(lambda: done(False))
                else:
                    self._ui(lambda: done(True))
            try:
                threading.Thread(target=work, name="proxy-report-export", daemon=True).start()
            except RuntimeError:
                done(False)

        start_button = ttk.Button(buttons, text="重新诊断", command=start)
        start_button.pack(side="left")
        cancel_button = ttk.Button(buttons, text="取消检查", command=lambda: self._diagnosis_cancel.set())
        cancel_button.pack(side="left", padx=6)
        export_button = ttk.Button(buttons, text="导出报告与日志", command=export)
        export_button.pack(side="right")
        def close():
            self._diagnosis_cancel.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        self._center_over(win)
        start()

    def open_diagnostics(self):
        if self._diagnostics_win is not None and self._diagnostics_win.winfo_exists():
            self._diagnostics_win.lift()
            return
        win = self._diagnostics_win = tk.Toplevel(self.root)
        win.title("故障日志")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        path_var = tk.StringVar()
        ttk.Label(frame, text="故障记录", style="Section.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=path_var, wraplength=680, style="Muted.TLabel").pack(
            anchor="w", pady=(6, 10))
        content = ttk.Frame(frame)
        content.pack(fill="both", expand=True)
        display = tk.Text(content, width=96, height=19, wrap="word", font=("Consolas", 9),
                          background="#f8fafc", foreground="#334155", relief="flat", padx=10, pady=10)
        display.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=display.yview)
        scrollbar.pack(side="right", fill="y")
        display.configure(yscrollcommand=scrollbar.set)
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="仅记录运行事件和错误；导出不包含配置、密码或请求内容。",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        busy = False

        def run_task(work, complete, failed):
            nonlocal busy
            if busy:
                return
            busy = True
            refresh_button.configure(state="disabled")
            export_button.configure(state="disabled")

            def finish(result, error):
                nonlocal busy
                busy = False
                if not win.winfo_exists():
                    return
                refresh_button.configure(state="normal")
                export_button.configure(state="normal")
                (failed if error else complete)(result)

            def work_in_background():
                try:
                    result = work()
                except Exception as error:
                    _diagnostic("diagnostics_action_failed", error=error)
                    self._ui(lambda: finish(None, True))
                else:
                    self._ui(lambda: finish(result, False))

            try:
                threading.Thread(target=work_in_background, name="proxy-log-view", daemon=True).start()
            except RuntimeError:
                finish(None, True)

        def show_records(records):
            display.configure(state="normal")
            display.delete("1.0", "end")
            display.insert("1.0", records or "暂无故障记录。")
            display.configure(state="disabled")
            display.see("end")

        def refresh():
            status = diagnostics_status()
            if status.get("error"):
                path_var.set(status["error"])
            elif status["enabled"]:
                path_var.set(f"保存位置：{status['directory']}（最多约 8 MiB，自动轮换）")
            else:
                path_var.set("日志尚未启用")
            run_task(lambda: read_recent_diagnostics(200), show_records,
                     lambda _result: path_var.set("日志读取失败，可稍后重试；代理仍可继续使用"))

        def export():
            destination = filedialog.asksaveasfilename(
                parent=win, title="导出故障日志", defaultextension=".zip",
                initialfile=time.strftime("proxy-diagnostics-%Y%m%d-%H%M%S.zip"),
                filetypes=[("ZIP 日志包", "*.zip")])
            if not destination:
                return
            run_task(lambda: export_diagnostics(destination),
                     lambda _result: messagebox.showinfo("导出完成", f"日志已保存到：\n{destination}", parent=win),
                     lambda _result: messagebox.showerror(
                         "导出失败", "无法导出日志，请检查保存位置是否可写或稍后重试。", parent=win))

        refresh_button = ttk.Button(footer, text="刷新", command=refresh)
        refresh_button.pack(side="left")
        export_button = ttk.Button(footer, text="导出日志", command=export, style="Primary.TButton")
        export_button.pack(side="right")
        refresh()
        win.bind("<Escape>", lambda _e: win.destroy())
        self._center_over(win)

    def _auto_refresh(self):
        self.refresh_status()
        self._refresh_share_status()
        self._refresh_health_status()
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
            except Exception as error:
                _diagnostic("ui_error", error=error)
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
        faults = []
        for label, listener in (('HTTP', self._relay), ('SOCKS5', self._socks)):
            state = getattr(listener, 'listener_state', 'running')
            if listener and state in ('recovering', 'failed'):
                faults.append(f"{label}：" + ('正在恢复监听' if state == 'recovering' else '监听故障，请停止后重新开启'))
        if faults:
            self._set_share_status('；'.join(faults), color='#c62828')
            return
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
        owned_entry = _parse_entry_addr(server) == self.entry_port
        if self.enabled:
            if server.startswith("127.0.0.1:"):  # 指向本机入口
                n = len(self._pool_addrs())
                server = (f"本机入口 {server}" if self._current_preset()["type"] == "single"
                          else f"本机入口 {server}（{n} 台上游）")
            self.status_var.set(f"● 已开启  {server}")
            self.status_label.config(foreground="#1a7f37")
            self.btn.config(text="关闭代理", style="On.TButton")
            if owned_entry:
                state = getattr(self._entry, 'listener_state', 'failed')
                if state != 'running':
                    detail = '监听中断，正在自动恢复' if state == 'recovering' else '本机入口故障，请关闭后重新开启'
                    self.status_var.set(f'● {detail}')
                    self.status_label.config(foreground='#c62828')
        else:
            self.status_var.set("● 已关闭")
            self.status_label.config(foreground="#555")
            self.btn.config(text="开启代理", style="Off.TButton")
        self._entry_status_text()

    def _update_metrics(self, active, stats):
        self.metric_conns.set(str(active))
        now = time.monotonic()
        totals = (sum(st[1] for st in stats.values()), sum(st[2] for st in stats.values()))
        previous = self._metric_prev
        self._metric_prev = (now, *totals) if self._entry else None
        if previous and now > previous[0] and totals[0] >= previous[1] and totals[1] >= previous[2]:
            dt = now - previous[0]
            self.metric_down.set(_fmt_speed((totals[0] - previous[1]) / dt))
            self.metric_up.set(_fmt_speed((totals[1] - previous[2]) / dt))
        else:
            self.metric_down.set("—")
            self.metric_up.set("—")

    def _entry_status_text(self):
        """入口速率/连接分形态展示：多形态写池表各列（按列 set，不覆测试列），
        单形态写一行速率；入口没跑就清空"""
        if not self._entry:
            self.entry_speed_var.set("")
            self._update_metrics(0, {})
            return
        if self._entry.listen_port != self.entry_port:
            return  # 「端口已改待重开」的红字提示别被 2 秒轮询顶掉
        active, stats = self._entry.snapshot()
        self._update_metrics(active, stats)
        now = time.monotonic()
        single = self._current_preset()["type"] == "single"
        addrs = self._pool_addrs(include_disabled=True)
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
        self._invalidate_test_results()

    @staticmethod
    def _valid_host(host: str) -> bool:
        """代理地址格式检查（校验逻辑已提取到模块级 _valid_host_str 共用）"""
        return _valid_host_str(host)

    def _pool_addrs(self, include_disabled=False) -> list:
        """转发仅取启用项；显示、保存和行号映射显式取全量。"""
        preset = self._current_preset()
        return (_to_addr_pairs(preset["upstreams"]) if include_disabled else _enabled_upstreams(preset))

    def _read_upstream(self):
        """共享通道跟随的上游 = 池[0]（与 V1 行为等价：单一确定目标）"""
        addrs = self._pool_addrs()
        return addrs[0] if addrs else None

    def _sync_pools(self):
        """入口池与共享上游始终跟随当前预设：入口热切对新连接生效，无需重启"""
        if self._adding:
            return  # 草稿编辑不能改变正在使用的预设和后台探测目标。
        addrs = self._pool_addrs()
        self._pool.set(addrs, preserve_health=True)
        if self._entry and self._entry.pool is not self._pool:
            self._entry.pool.set(addrs, preserve_health=True)
        up = self._read_upstream()
        if up and self._relay:
            self._relay.upstream = up
        if up and self._socks:
            self._socks.upstream = up
        monitor = getattr(self, "_health_monitor", None)
        if monitor is not None:
            monitor.wake()

    def _sync_relay_upstream(self):
        """转发目标跟随逻辑已并入 _sync_pools；保留旧名给既有调用点"""
        self._sync_pools()

    def toggle(self):
        self.apply(not self.enabled)

    def apply(self, enable: bool):
        if enable:
            addrs = self._pool_addrs()
            err = validate_upstreams(addrs, (self.entry_port,))  # 空池/坏条目都拦，注册表不写
            if err:
                messagebox.showwarning("提示", err, parent=self.root)
                return
            self._pool.set(addrs, preserve_health=True)
            if self._entry is None:  # 入口只在缺时起；改入口端口后重开一次开关生效
                try:
                    self._entry = HttpServer(self.entry_port, self._pool)
                    self._entry.start()
                except (OSError, RuntimeError) as e:
                    _diagnostic("entry_start_failed", error=e, stage="startup", server="http")
                    self._entry = None
                    messagebox.showerror(
                        "错误", f"入口端口 {self.entry_port} 启动失败，"
                                f"请检查端口占用和系统资源，可在设置里修改端口：\n{e}", parent=self.root)
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
        """各上游独立测试 HTTPS；失败时换站点复查，结果不修改上游池。"""
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
        try:
            ok, info = _probe_upstream(ip, port)
        except Exception as error:  # 未预期错误也回收按钮，并保留原始详情。
            ok, info = False, error
        self._ui(lambda: self._show_test_result(gen, f"{ip}:{port}", ok, info, total))

    def _show_test_result(self, gen: int, name: str, ok: bool, info, total: int):
        """结果分形态展示：多形态写池表 test 列（✓ 毫秒/✗ 短词），
        单形态写结果行（✗ 追加完整人话）"""
        if gen != self._test_gen:  # 结果到达前池已被改动：丢弃（按钮由收齐的末条恢复）
            return
        self._manual_test_times[name] = time.monotonic()
        self._test_results[name] = (ok, info)
        if len(self._test_results) < total:
            return
        self.test_btn.config(state="normal", text="测试连通")
        if self._current_preset()["type"] == "single":
            ok1, val = next(iter(self._test_results.values()))
            if ok1:
                self.test_result_var.set(f"✓ {val} ms")
                self.test_result_label.config(foreground="#1a7f37")
            else:
                self.test_result_var.set(
                    f"✗ {_short_conn_error(val)} · 查看详情")
                self.test_result_label.config(foreground="#c62828")
            return
        iid_of = {f"{ip}:{port}": str(i) for i, (ip, port) in enumerate(self._pool_addrs(include_disabled=True))}
        for n, (ok1, val) in self._test_results.items():
            iid = iid_of.get(n)
            if iid and self.pool_box.exists(iid):
                self.pool_box.set(iid, "test",
                                  f"✓ {val} ms" if ok1 else f"✗ {_short_conn_error(val)}")
        passed = sum(ok1 for ok1, _ in self._test_results.values())
        self.test_result_var.set(f"{passed}/{total} 上游通过；未通过请点详情" if passed < total else "")
        self.test_result_label.config(foreground="#9a6700" if passed else "#c62828")

    def show_test_details(self):
        if not self._test_results:
            text = "请先点击测试连通。"
        else:
            rows = ["逐台上游测试 HTTPS；失败时会换站点复查。",
                    "结果仅代表本次测试，浏览器可能使用池内其他上游。", ""]
            for name, (ok, info) in self._test_results.items():
                rows.append(f"{name}\n" + (f"通过，{info} ms（含 CONNECT 和 TLS）"
                                           if ok else _friendly_conn_error(info)))
            text = "\n\n".join(rows)
        snapshots = self._pool.health_snapshot()
        enabled = set(self._pool_addrs())
        automatic = []
        labels = {"unknown": "待检测", "healthy": "正常", "suspect": "等待复查",
                  "degraded": "已降级", "recovering": "恢复观察中"}
        for up in self._pool_addrs(include_disabled=True):
            name = f"{up[0]}:{up[1]}"
            if up not in enabled:
                automatic.append(f"{name}：已停用，已有连接可继续传输")
                continue
            info = snapshots.get(up, {})
            state = labels.get(info.get("state", "unknown"), "待检测")
            row = f"{name}\nHTTPS：{state}"
            if info.get("last_checked"):
                age = max(0, int(time.monotonic() - info["last_checked"]))
                row += f"（{age} 秒前）"
            if info.get("reason"):
                row += f"；{info['reason']}"
            if info.get("latency_ms") is not None:
                row += f"；最近成功 {info['latency_ms']} ms"
            failures = info.get("tcp_failures", 0)
            row += (f"\nTCP 拨号：连续失败 {failures} 次" if failures
                    else "\nTCP 拨号：尚未发现连续失败（不代表 HTTPS 一定可用）")
            automatic.append(row)
        if automatic:
            text += ("\n\n后台健康检测（每台约 30 秒一次）\n"
                     "连续 3 次失败降级，连续 2 次成功恢复；仅调整新连接。\n"
                     "所有上游均降级时仍允许兜底，避免检测站点故障造成全断。\n\n"
                     + "\n\n".join(automatic))
        messagebox.showinfo("连通性测试详情", text, parent=self.root)

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
        err = validate_upstreams([up], (self.entry_port, self.share_port, self.socks_port))
        if err:
            self._fw_pending = False
            self._set_share_status(f'✗ 开启失败：{err}', color='#c62828')
            return
        try:
            relay = RelayServer(self.share_port, up)
            relay.start()
        except (OSError, RuntimeError) as e:
            _diagnostic("share_start_failed", upstream=up, error=e, stage="startup", server="relay")
            self._fw_pending = False  # 失败要解锁，否则之后点开启都被"进行中"挡住
            self._set_share_status(f"✗ 开启失败：{e}", color="#c62828")
            return
        try:
            socks = SocksServer(self.socks_port, up,
                                self.settings["socks_user"], self.settings["socks_pass"])
            socks.start()
        except (OSError, RuntimeError) as e:
            _diagnostic("share_start_failed", upstream=up, error=e, stage="startup", server="socks")
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
        for col, text, width in (("ip", "设备 IP", 150), ("conns", "连接", 56),
                                 ("down", "↓ 速率", 100), ("up", "↑ 速率", 100),
                                 ("last", "最后活跃", 96)):
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
        self._health_monitor.stop()
        self._diagnosis_cancel.set()
        if self._adding:  # 新增模式里退出：先还原字段，别把半成品存成预设
            self._exit_add_mode(restore=True)
        if self._relay:
            self._relay.stop()  # 只停转发；共享开关状态保留，下次启动自动恢复
        if self._socks:
            self._socks.stop()
        if self._entry:
            self._entry.stop()
        self.save_preset()
        if self._tray:
            try:
                self._tray.close()
            except Exception:
                traceback.print_exc()
        self.root.quit()


if __name__ == "__main__":
    if not acquire_single_instance():  # 已有实例，其窗口已被拉到前台
        raise SystemExit(0)
    init_diagnostics()
    _diagnostic("app_start")
    original_thread_hook = threading.excepthook

    def report_thread_error(args):
        _diagnostic("thread_error", error=args.exc_value)
        original_thread_hook(args)

    threading.excepthook = report_thread_error
    try:  # 高分屏下界面更清晰
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = app = None
    try:
        root = tk.Tk()
        original_callback_hook = root.report_callback_exception

        def report_callback_error(kind, value, tb):
            _diagnostic("ui_error", error=value)
            original_callback_hook(kind, value, tb)

        root.report_callback_exception = report_callback_error
        app = ProxyApp(root)
        # 主窗口摆到屏幕中央，与弹窗共用同一套“放一次→量差值→修正一步”逻辑
        app._center_in(root, 0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        root.mainloop()
    except Exception as error:
        _diagnostic("app_error", error=error)
        raise
    finally:
        if app is not None:
            app._health_monitor.stop()
        if root is not None:
            root.destroy()
        _diagnostic("app_stop")
        close_diagnostics()

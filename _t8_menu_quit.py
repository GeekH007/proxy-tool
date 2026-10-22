# -*- coding: utf-8 -*-
"""托盘菜单退出的真实验证：真弹 tk_popup + WM_COMMAND 走菜单选中真实派发路径
（UIPI 挡合成点击，只能同进程 PostMessage），跑真 mainloop 验证 quit 能退出。
此前 update() 空转循环测不出"destroy 不终止 mainloop、进程残活"的 bug。"""
import ctypes
import os
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy_tool as pt

u = ctypes.windll.user32
WM_COMMAND, WM_CLOSE = 0x0111, 0x0010

root = tk.Tk()
app = pt.ProxyApp(root)
root.update_idletasks()
APP_HWND = u.GetParent(root.winfo_id()) or root.winfo_id()

real_quit = app.quit_app
app._quit_called = False
app._quit_at = 0.0
def wrapped_quit():
    app._quit_called = True
    app._quit_at = time.monotonic()
    real_quit()
app.quit_app = wrapped_quit

root.withdraw()
root.update()


def poster():
    time.sleep(0.5)
    menu_hwnd = u.FindWindowW("#32768", None)
    print("菜单窗口:", "有" if menu_hwnd else "无")
    for wid in range(1, 13):  # 遍历候选条目 id，模拟逐项点击（无关 id 无副作用）
        u.PostMessageW(APP_HWND, WM_COMMAND, wid, 0)
        time.sleep(0.05)
    # WM_COMMAND 只触发 invoke、不会让 TrackPopupMenu 返回；tk_popup 还阻塞着，
    # 必须关掉菜单它才能返回、mainloop 才能消费 quit 标志（真实点击选中会自动关）
    time.sleep(2.0)
    menu_hwnd = u.FindWindowW("#32768", None)
    print("兜底关菜单:", "有" if menu_hwnd else "菜单已不在")
    if menu_hwnd:
        u.PostMessageW(menu_hwnd, WM_CLOSE, 0, 0)


threading.Thread(target=poster, daemon=True).start()

t0 = time.monotonic()
root.after(10000, root.quit)  # 总兜底
app._tray_menu()  # 真实路径：入队 → _show_tray_menu → tk_popup
root.mainloop()   # 真循环：只有 root.quit() 能让它退出

print(f"mainloop 运行 {time.monotonic() - t0:.1f}s 后退出")
print("退出回调被触发:", app._quit_called)
assert app._quit_called, "菜单‘退出’的 command 没被执行"
assert time.monotonic() - app._quit_at < 3, "quit_app 触发后 mainloop 没有及时退出"
root.destroy()
print("MENU_QUIT_OK")

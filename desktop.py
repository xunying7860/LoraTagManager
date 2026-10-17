# -*- coding: utf-8 -*-
"""LoRA 打标管理器 桌面版：Edge --app 窗口 + 内置标准库服务（Win11 零依赖，双击即用）

v2 改动（L2-1）：
- 去掉 pywebview/pythonnet（省 .NET Runtime 加载 1~3s，包内少 0.8MB）
- 直接用系统自带 Edge 的 --app 模式：无地址栏独立窗口，观感等同原生应用
- 独立 user-data-dir，不占用户主 Edge 配置
"""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# windowed（无控制台）模式下：提前把 stdout/stderr 重定向到日志文件（PyInstaller --windowed 时 sys.stdout 为 None）
if getattr(sys, "frozen", False) and sys.stdout is None:
    try:
        _log = open(os.path.join(os.path.dirname(sys.executable), "ltm_desktop.log"), "a", encoding="utf-8")
        sys.stdout = _log
        sys.stderr = _log
    except Exception:
        pass

import server

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge():
    """找系统 Edge（Win11 自带）；找不到走注册表兜底"""
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")
        v = winreg.QueryValue(k, None)
        return v if os.path.isfile(v) else None
    except Exception:
        return None


def _find_free_port(start=8765):
    """找空闲端口：8765 起逐个尝试"""
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 8765


def _wait_port(port, timeout=15.0):
    """等服务端口就绪（标准库服务 import 快，通常 <1s）"""
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def main():
    port = _find_free_port()
    threading.Thread(target=server.serve, args=(port,), daemon=True).start()
    if not _wait_port(port):
        print("服务启动失败")
        return
    url = f"http://127.0.0.1:{port}"
    edge = find_edge()
    user_data = Path(os.environ.get("LOCALAPPDATA", ".")) / "LoraTagManager" / "edge-profile"
    if edge:
        # Edge --app 模式：无地址栏独立窗口；独立 profile 不占用户主 Edge
        subprocess.Popen([edge, f"--app={url}",
                          "--window-size=1360,860",
                          f"--user-data-dir={user_data}",
                          "--no-first-run", "--no-default-browser-check",
                          "--disable-features=Translate,OptimizationHints"])
    else:
        # 找不到 Edge 就用默认浏览器
        import webbrowser
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""LoRA 打标管理器 桌面版：pywebview 窗口 + 内置 FastAPI 服务（Win11 零依赖，双击即用）"""
import os
import socket
import sys
import threading

# windowed（无控制台）模式下：pythonnet/clr 初始化与 uvicorn 日志可能因 stdout 为 None 崩溃——
# 提前把 stdout/stderr 重定向到日志文件（PyInstaller --windowed 时 sys.stdout 为 None）
if getattr(sys, "frozen", False) and sys.stdout is None:
    try:
        _log = open(os.path.join(os.path.dirname(sys.executable), "ltm_desktop.log"), "a", encoding="utf-8")
        sys.stdout = _log
        sys.stderr = _log
    except Exception:
        pass

import webview
import uvicorn

import server


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


def main():
    port = _find_free_port()
    threading.Thread(
        target=lambda: uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning"),
        daemon=True,
    ).start()
    webview.create_window(
        "LoRA 打标管理器",
        f"http://127.0.0.1:{port}",
        width=1360,
        height=860,
        min_size=(1024, 700),
    )
    webview.start()


if __name__ == "__main__":
    main()

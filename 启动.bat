@echo off
chcp 65001 >nul
rem ============================================
rem LoRA 打标管理器 启动脚本
rem ============================================
cd /d "%~dp0"

echo 正在启动 LoRA 打标管理器...
start "" http://localhost:8765
python server.py
pause

@echo off
rem ============================================
rem LoRA Tag Manager startup script
rem Prefer packaged exe, fall back to source mode
rem ============================================
cd /d "%~dp0"
if exist "LoraTagManager.exe" (
  start "" "LoraTagManager.exe"
) else (
  start "" pythonw desktop.py
)

@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
"C:\Program Files\Python312\python.exe" "%~dp0reset_card_panel.py" --demo
if errorlevel 1 pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul || (
  echo Python не найден. Установите Python 3.9+ с python.org и отметьте "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo Первый запуск: ставлю зависимости...
  python -m venv .venv || (pause & exit /b 1)
)
.venv\Scripts\python -m pip install -q --upgrade -r requirements.txt

start "" .venv\Scripts\pythonw -m soundpad_music

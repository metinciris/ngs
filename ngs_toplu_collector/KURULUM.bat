@echo off
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 (
  py -m pip install --upgrade pip
  py -m pip install -r requirements.txt
  py -m playwright install chromium
) else (
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  python -m playwright install chromium
)
pause

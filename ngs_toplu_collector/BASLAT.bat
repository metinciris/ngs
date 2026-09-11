@echo off
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 (
  py -c "import playwright" >nul 2>nul
  if errorlevel 1 call KURULUM.bat
  py app.py
) else (
  python -c "import playwright" >nul 2>nul
  if errorlevel 1 call KURULUM.bat
  python app.py
)

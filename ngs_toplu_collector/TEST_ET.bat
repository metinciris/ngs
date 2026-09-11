@echo off
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 (py self_test.py) else (python self_test.py)
pause

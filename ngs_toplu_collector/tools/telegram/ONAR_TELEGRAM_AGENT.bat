@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0ONAR_TELEGRAM_AGENT.py"
) else (
  python "%~dp0ONAR_TELEGRAM_AGENT.py"
)
set "RESULT=%errorlevel%"
echo.
if "%RESULT%"=="0" (echo TAMAM. Telegram'da /durum komutunu deneyin.) else (echo BASARISIZ. Yukaridaki hata metnini gonderin.)
pause
exit /b %RESULT%

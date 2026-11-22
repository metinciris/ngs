@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

if not exist "BASLAT.bat" (
  echo HATA: BASLAT.bat bulunamadi.
  echo Bu dosyayi NGS Mutasyon Havuzu kurulum klasorune koyun.
  pause
  exit /b 1
)

rem Fallback uses the original beta host without editing the saved config or data.
set "NGS_PLATFORM_BASE_URL=https://beta.gennext.bio"
set "NGS_PLATFORM_RUNS_URL=https://beta.gennext.bio/runs?page=1&perPage=7"

echo GenNext: ESKI - beta.gennext.bio
echo.
call "BASLAT.bat"
exit /b %errorlevel%

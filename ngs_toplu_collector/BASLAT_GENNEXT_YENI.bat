@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

if not exist "BASLAT.bat" (
  echo HATA: BASLAT.bat bulunamadi.
  echo Bu dosyayi NGS Mutasyon Havuzu kurulum klasorune koyun.
  pause
  exit /b 1
)

rem These overrides are process-local: existing config and local data stay untouched.
set "NGS_PLATFORM_BASE_URL=https://app.gennext.bio"
set "NGS_PLATFORM_RUNS_URL=https://app.gennext.bio/runs?page=1&perPage=7"

echo GenNext: YENI - app.gennext.bio
echo Ilk kullanimda https://app.gennext.bio/auth uzerinden giris gerekebilir.
echo.
call "BASLAT.bat"
exit /b %errorlevel%

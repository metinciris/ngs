@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NGS Safe Downloader

set "APP=NGS_Safe_Downloader.py"
set "MANIFEST=https://raw.githubusercontent.com/metinciris/ngs/main/ngs-safe-downloader/latest.json"
set "PKG=NGS_Safe_Downloader.update.zip"
set "TMPDIR=_NGS_UPDATE_TMP"
set "BACKUP=NGS_Safe_Downloader.previous.py"

echo NGS Safe Downloader baslatiliyor...
echo GitHub guncelleme kontrolu yapiliyor...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "$m=Invoke-RestMethod -Uri '%MANIFEST%' -TimeoutSec 12;" ^
  "$expectedApp=([string]$m.app_sha256).ToLower();" ^
  "$local=''; if(Test-Path '%APP%'){$local=(Get-FileHash '%APP%' -Algorithm SHA256).Hash.ToLower()};" ^
  "if($local -eq $expectedApp){Write-Host ('Guncel surum: v'+$m.version); exit 0};" ^
  "Write-Host ('Yeni surum bulundu: v'+$m.version);" ^
  "$sb=New-Object System.Text.StringBuilder; foreach($u in $m.parts){$t=(Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 30).Content; [void]$sb.Append($t.Trim())};" ^
  "[IO.File]::WriteAllBytes('%PKG%', [Convert]::FromBase64String($sb.ToString()));" ^
  "$pkgHash=(Get-FileHash '%PKG%' -Algorithm SHA256).Hash.ToLower();" ^
  "if($pkgHash -ne ([string]$m.package_sha256).ToLower()){Remove-Item '%PKG%' -Force -ErrorAction SilentlyContinue; throw 'Paket SHA-256 dogrulamasi basarisiz. Eski surum korunuyor.'};" ^
  "Remove-Item '%TMPDIR%' -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Path '%TMPDIR%' | Out-Null;" ^
  "Expand-Archive -Path '%PKG%' -DestinationPath '%TMPDIR%' -Force;" ^
  "$newApp=Join-Path '%TMPDIR%' '%APP%'; if(!(Test-Path $newApp)){throw 'Guncelleme paketinde uygulama bulunamadi.'};" ^
  "$newHash=(Get-FileHash $newApp -Algorithm SHA256).Hash.ToLower(); if($newHash -ne $expectedApp){throw 'Uygulama SHA-256 dogrulamasi basarisiz.'};" ^
  "if(Test-Path '%APP%'){Copy-Item '%APP%' '%BACKUP%' -Force};" ^
  "Copy-Item $newApp '%APP%' -Force;" ^
  "Set-Content -Path 'NGS_Safe_Downloader.version.txt' -Value $m.version -Encoding ASCII;" ^
  "Remove-Item '%TMPDIR%' -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item '%PKG%' -Force -ErrorAction SilentlyContinue;" ^
  "Write-Host ('Guncelleme tamamlandi: v'+$m.version)"

if errorlevel 1 (
  echo GitHub guncelleme kontrolu yapilamadi veya guncelleme reddedildi.
  echo Mevcut yerel surumle devam ediliyor.
)

echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -c "import requests" >nul 2>nul || py -m pip install --user requests
    py "%APP%"
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -c "import requests" >nul 2>nul || python -m pip install --user requests
    python "%APP%"
    goto :end
)

echo.
echo Python bulunamadi. Python 3 kurup tekrar deneyin:
echo https://www.python.org/downloads/windows/
echo Kurulumda "Add Python to PATH" secenegini isaretleyin.
pause

:end
endlocal

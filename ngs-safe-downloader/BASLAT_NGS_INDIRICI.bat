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
  "$m=Invoke-RestMethod -Uri '%MANIFEST%?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -TimeoutSec 15;" ^
  "$expectedApp=([string]$m.app_sha256).ToLower();" ^
  "$local=''; if(Test-Path '%APP%'){$local=(Get-FileHash '%APP%' -Algorithm SHA256).Hash.ToLower()};" ^
  "if($local -eq $expectedApp){Write-Host ('Guncel surum: v'+$m.version); exit 0};" ^
  "Write-Host ('Yeni surum bulundu: v'+$m.version);" ^
  "$sb=New-Object System.Text.StringBuilder; $n=0; foreach($u in $m.parts){$n++; Write-Host ('Paket parcasi '+$n+'/'+$m.parts.Count+' indiriliyor...'); $t=(Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 45).Content; $clean=([string]$t) -replace '[^A-Za-z0-9+/=]',''; [void]$sb.Append($clean)};" ^
  "$b64=$sb.ToString(); if(($b64.Length %% 4) -ne 0){throw ('Base64 paket uzunlugu gecersiz: '+$b64.Length)};" ^
  "[IO.File]::WriteAllBytes('%PKG%', [Convert]::FromBase64String($b64));" ^
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
  echo Mevcut yerel surum varsa onunla devam ediliyor.
)

echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -c "import requests" >nul 2>nul || py -m pip install --user requests
    if exist "%APP%" py "%APP%"
    if not exist "%APP%" echo Uygulama dosyasi indirilemedi. Internet baglantisini kontrol edip tekrar deneyin.
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -c "import requests" >nul 2>nul || python -m pip install --user requests
    if exist "%APP%" python "%APP%"
    if not exist "%APP%" echo Uygulama dosyasi indirilemedi. Internet baglantisini kontrol edip tekrar deneyin.
    goto :end
)

echo.
echo Python bulunamadi. Python 3 kurup tekrar deneyin:
echo https://www.python.org/downloads/windows/
echo Kurulumda "Add Python to PATH" secenegini isaretleyin.
pause

:end
endlocal

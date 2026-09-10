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
  "$ProgressPreference='SilentlyContinue';" ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "$m=Invoke-RestMethod -Uri '%MANIFEST%' -TimeoutSec 15;" ^
  "$expectedApp=([string]$m.app_sha256).ToLower();" ^
  "$local=''; if(Test-Path '%APP%'){$local=(Get-FileHash '%APP%' -Algorithm SHA256).Hash.ToLower()};" ^
  "if($local -eq $expectedApp){Write-Host ('Guncel surum: v'+$m.version); exit 0};" ^
  "Write-Host ('Yeni surum bulundu: v'+$m.version);" ^
  "$parts=@($m.parts); if($parts.Count -lt 1){throw 'Manifest paket parcasi icermiyor.'};" ^
  "$sb=New-Object System.Text.StringBuilder; for($i=0; $i -lt $parts.Count; $i++){ $u=[string]$parts[$i]; $partUri=$u+'?v='+[string]$m.version; Write-Host ('Paket parcasi '+($i+1)+'/'+$parts.Count+' indiriliyor...'); $t=(Invoke-WebRequest -Uri $partUri -UseBasicParsing -TimeoutSec 60).Content; $clean=([regex]::Replace([string]$t,'[^A-Za-z0-9+/=]','')); if([string]::IsNullOrWhiteSpace($clean)){throw ('Paket parcasi bos: '+($i+1))}; [void]$sb.Append($clean) };" ^
  "$b64=$sb.ToString(); Write-Host ('Base64 paket uzunlugu: '+$b64.Length); if(($b64.Length -band 3) -ne 0){throw ('Base64 paket uzunlugu gecersiz: '+$b64.Length)};" ^
  "[IO.File]::WriteAllBytes('%PKG%', [Convert]::FromBase64String($b64));" ^
  "$pkgHash=(Get-FileHash '%PKG%' -Algorithm SHA256).Hash.ToLower();" ^
  "if($pkgHash -ne ([string]$m.package_sha256).ToLower()){Remove-Item '%PKG%' -Force -ErrorAction SilentlyContinue; throw ('Paket SHA-256 dogrulamasi basarisiz. Beklenen: '+$m.package_sha256+' Alinan: '+$pkgHash)};" ^
  "Remove-Item '%TMPDIR%' -Recurse -Force -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Path '%TMPDIR%' | Out-Null;" ^
  "Expand-Archive -Path '%PKG%' -DestinationPath '%TMPDIR%' -Force;" ^
  "$newApp=Join-Path '%TMPDIR%' '%APP%'; if(!(Test-Path $newApp)){throw 'Guncelleme paketinde uygulama bulunamadi.'};" ^
  "$newHash=(Get-FileHash $newApp -Algorithm SHA256).Hash.ToLower(); if($newHash -ne $expectedApp){throw ('Uygulama SHA-256 dogrulamasi basarisiz. Beklenen: '+$expectedApp+' Alinan: '+$newHash)};" ^
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

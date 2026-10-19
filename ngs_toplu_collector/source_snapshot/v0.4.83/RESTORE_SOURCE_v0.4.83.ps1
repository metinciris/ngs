param(
    [string]$OutDir = ".\restored_v0.4.83"
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$PartsDir = Join-Path $Here "parts"
$Parts = Get-ChildItem -LiteralPath $PartsDir -Filter "source.part*.b64" | Sort-Object Name
if (-not $Parts -or $Parts.Count -ne 37) {
    throw "Kaynak parcalari eksik. Beklenen: 37, bulunan: $($Parts.Count)"
}
$B64 = (($Parts | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }) -join "").Trim()
$Bytes = [Convert]::FromBase64String($B64)
$Archive = Join-Path $Here "ngs_toplu_collector_v0.4.83_PUBLIC_SOURCE.tar.xz"
[IO.File]::WriteAllBytes($Archive, $Bytes)
$Expected = "8d84af1ea9a2c9ca39f2911830db2a8bb08046f37d632a9abcc4cae0e63de65f"
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
if ($Actual -ne $Expected) {
    throw "SHA256 dogrulamasi basarisiz. Beklenen=$Expected Gercek=$Actual"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($Tar) {
    & $Tar.Source -xf $Archive -C $OutDir
    if ($LASTEXITCODE -ne 0) { throw "tar.exe arsivi acamadi." }
    Write-Host "TAMAM: kaynak dogrulandi ve cikartildi -> $OutDir"
} else {
    Write-Host "TAMAM: kaynak arsivi dogrulandi -> $Archive"
    Write-Host "Bu Windows kurulumunda tar.exe yok. Arsivi 7-Zip gibi XZ/TAR destekli bir aracla acabilirsiniz."
}
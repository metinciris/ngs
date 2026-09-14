$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parts = Get-ChildItem -Path $here -Filter 'source_part_*.b64' | Sort-Object Name
if (-not $parts) { throw 'Kaynak parcalari bulunamadi.' }
$b64 = ($parts | ForEach-Object { Get-Content -Raw $_.FullName }) -join ''
$b64 = $b64.Trim()
$bytes = [Convert]::FromBase64String($b64)
$out = Join-Path $here 'ngs_toplu_collector_v0.4.1.zip'
[IO.File]::WriteAllBytes($out, $bytes)
$sha = (Get-FileHash -Algorithm SHA256 $out).Hash.ToLower()
$expected = '7940602b695ba6e4136acb080b7a067e4f1ab6bdffff0b8a0e6fcbed5d8fe071'
if ($sha -ne $expected) {
  Remove-Item $out -Force
  throw "SHA256 dogrulanamadi: $sha"
}
Write-Host "OK: $out"
Write-Host "SHA256: $sha"

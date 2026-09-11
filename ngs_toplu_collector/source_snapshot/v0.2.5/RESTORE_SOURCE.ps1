$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parts = Get-ChildItem -Path $here -Filter 'source_part_*.b64' | Sort-Object Name
if (-not $parts) { throw 'Kaynak parcalari bulunamadi.' }
$b64 = ($parts | ForEach-Object { Get-Content -Raw $_.FullName }).Trim()
$bytes = [Convert]::FromBase64String($b64)
$out = Join-Path (Split-Path -Parent $here) 'ngs_toplu_collector_v0.2.5.zip'
[IO.File]::WriteAllBytes($out, $bytes)
$sha = (Get-FileHash -Algorithm SHA256 $out).Hash.ToLower()
$expected = '49478450efcc93e942195806e884dd1985de4de96e1170b322716ba1ea3b774e'
if ($sha -ne $expected) { Remove-Item $out -Force; throw "SHA256 dogrulanamadi: $sha" }
Write-Host "OK: $out"
Write-Host "SHA256: $sha"

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parts = Get-ChildItem -Path $here -Filter 'source_part_*.b64' | Sort-Object Name
if (-not $parts) { throw 'Kaynak parcalari bulunamadi.' }
$b64 = ($parts | ForEach-Object { Get-Content -Raw $_.FullName }).Trim()
$bytes = [Convert]::FromBase64String($b64)
$out = Join-Path (Split-Path -Parent $here) 'ngs_toplu_collector_public_v0.2.4.zip'
[IO.File]::WriteAllBytes($out, $bytes)
$sha = (Get-FileHash -Algorithm SHA256 $out).Hash.ToLower()
$expected = '066664bd96a500f76069d65602880c82b3798c953e0c2db6a2f06c4f57f31572'
if ($sha -ne $expected) { Remove-Item $out -Force; throw "SHA256 dogrulanamadi: $sha" }
Write-Host "OK: $out"
Write-Host "SHA256: $sha"

# The installer's version, from BUILD_ID in agent\config.py:
#   "2026-10-04 13:26 UTC"  ->  2026.10.4.1326
# Windows wants four numbers, each under 65536.
$root = Split-Path -Parent $PSScriptRoot
$hit = Select-String -Path (Join-Path $root "agent\config.py") `
  -Pattern 'BUILD_ID = "(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})' | Select-Object -First 1
if (-not $hit) { "0.0.0.0"; exit 0 }
$g = $hit.Matches[0].Groups
"{0}.{1}.{2}.{3}" -f [int]$g[1].Value, [int]$g[2].Value, [int]$g[3].Value, [int]($g[4].Value + $g[5].Value)

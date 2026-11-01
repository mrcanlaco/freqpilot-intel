param(
    [string]$Database = "runtime/bti.sqlite3",
    [int]$Port = 8768
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is already in use." }
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
python -m compileall -q src
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Local verification passed. Starting read-only dashboard on http://127.0.0.1:$Port"
Write-Host "This script does not modify DNS, tunnels, services, or runtime/cloudflared.yml."
& (Join-Path $PSScriptRoot "run.ps1") -Database $Database -Port $Port

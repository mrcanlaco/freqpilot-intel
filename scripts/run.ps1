param(
    [string]$Database = "runtime/bti.sqlite3",
    [int]$Port = 8768
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
python -m bti --db $Database init
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m bti --db $Database build-drafts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m bti --db $Database serve --host 127.0.0.1 --port $Port

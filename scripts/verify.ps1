$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
python scripts/bootstrap.py --config-only
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/app.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/workspace.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --test tests/test_workspace.js
exit $LASTEXITCODE

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
node --check src/bti/static/editorial.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/square-streams.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/workspace.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/drafts.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/composer.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --check src/bti/static/images.js
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node --test tests/test_workspace.js tests/test_drafts.js tests/test_images.js tests/test_square_draft.js
exit $LASTEXITCODE

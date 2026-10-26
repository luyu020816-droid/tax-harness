$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
Push-Location (Join-Path $projectRoot 'backend')
try {
    & $projectPython -m ruff check .
    if ($LASTEXITCODE) { throw 'Python lint failed.' }
    & $projectPython -m pytest tests -q
    if ($LASTEXITCODE) { throw 'Backend tests failed.' }
} finally { Pop-Location }
Push-Location (Join-Path $projectRoot 'frontend')
try {
    npm run format:check
    if ($LASTEXITCODE) { throw 'Frontend formatting failed.' }
    npm run build
    if ($LASTEXITCODE) { throw 'Frontend checks failed.' }
} finally { Pop-Location }

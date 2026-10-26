param(
    [string]$Python = 'python',
    [ValidateSet('native', 'docker')][string]$DatabaseMode = 'native'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE) { throw 'Python 3.12+ is required.' }
}
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $projectPython -m pip install -r backend/requirements.lock.txt
if ($LASTEXITCODE) { throw 'Dependency installation failed.' }
& $projectPython -m pip install --no-deps -e 'backend[dev]'
if ($LASTEXITCODE) { throw 'Project installation failed.' }
& $projectPython -m app.cli configure
if ($DatabaseMode -eq 'native') {
    & $projectPython scripts/install_postgres.py
    if ($LASTEXITCODE) { throw 'PostgreSQL client installation failed.' }
    & $projectPython scripts/native_postgres.py start
} else {
    docker compose up -d --wait postgres
}
if ($LASTEXITCODE) { throw 'Database startup failed.' }
& $projectPython -m app.cli init
if ($LASTEXITCODE) { throw 'Initialization failed.' }
Push-Location frontend
try {
    npm ci
    if ($LASTEXITCODE) { throw 'Frontend dependency installation failed.' }
    npm run build
    if ($LASTEXITCODE) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Write-Host 'Setup complete. Configure MODEL_* locally, then run scripts/start.ps1.'

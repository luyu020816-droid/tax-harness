param([int]$Port = 8100, [switch]$SkipDatabaseStart, [string]$EnvFile = '.env')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:TAX_HARNESS_ENV_FILE = Join-Path $projectRoot $EnvFile
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Run scripts/setup.ps1 first.' }
if (-not $SkipDatabaseStart -and (Test-Path -LiteralPath '.tools\pgsql\bin\pg_ctl.exe')) {
    & $projectPython scripts/native_postgres.py start
    if ($LASTEXITCODE) { throw 'Database startup failed.' }
}
# Ordinary startup never reimports samples or recreates schema.
& $projectPython -c 'from app.config import Settings; from app.db import Database; d=Database(Settings().database_url); d.open(); ok=d.ready(); d.close(); raise SystemExit(0 if ok else 1)'
if ($LASTEXITCODE) { throw 'Schema is unavailable or incompatible; run the explicit init command.' }
& $projectPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port

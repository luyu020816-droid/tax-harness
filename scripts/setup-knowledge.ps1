param([string]$Python = 'python', [switch]$SkipModelDownload)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE) { throw 'Python 3.12 x64 is required.' }
}
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $projectPython -m pip install -r backend/requirements-windows-py312.lock
if ($LASTEXITCODE) { throw 'Pinned retrieval dependencies could not be installed.' }
& $projectPython -m pip install --no-deps -e 'backend[dev,knowledge]'
if ($LASTEXITCODE) { throw 'Backend installation failed.' }
if (-not $SkipModelDownload) {
    & $projectPython scripts/install_retrieval_models.py
    if ($LASTEXITCODE) { throw 'Retrieval model verification failed.' }
}
$ocrPython = Join-Path $projectRoot '.local\ocr-runtime\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ocrPython)) {
    & $projectPython -m venv .local/ocr-runtime
    if ($LASTEXITCODE) { throw 'OCR environment creation failed.' }
}
& $ocrPython -m pip install -r backend/requirements-ocr.lock.txt
if ($LASTEXITCODE) { throw 'Pinned OCR dependencies could not be installed.' }
if (-not $SkipModelDownload) {
    & $projectPython scripts/install_ocr_models.py
    if ($LASTEXITCODE) { throw 'OCR model verification failed.' }
}
Write-Host 'Knowledge runtimes installed. PostgreSQL 16 with pgvector is required; see docs/PATCH002_DEPLOYMENT.md.'

param(
    [string]$Python = '',
    [ValidateRange(1024,65535)][int]$Port = 8011
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $existing = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
    if ($existing.local_only -eq $true) {
        Write-Output "Local embeddings already running on port $Port ($($existing.model))."
        exit 0
    }
    throw "Port $Port is occupied by a different service."
} catch {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        throw "Port $Port is occupied; check the existing service before starting another one."
    }
}
if (-not $Python) {
    $candidates = @(
        (Join-Path $PSScriptRoot '.venv/Scripts/python.exe'),
        (Join-Path $PSScriptRoot '../DevFlow-AI/backend/.venv/Scripts/python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            & $candidate -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('sentence_transformers') else 1)"
            if ($LASTEXITCODE -eq 0) { $Python = (Resolve-Path -LiteralPath $candidate).Path; break }
        }
    }
}
if (-not $Python) { throw 'Install requirements-local-embedding.txt in a virtual environment or pass -Python with an existing interpreter.' }
$logDirectory = Join-Path $PSScriptRoot '.data'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$arguments = @('-m','uvicorn','local_embedding_server:app','--host','127.0.0.1','--port',"$Port",'--workers','1')
$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDirectory 'local-embedding.stdout.log') `
    -RedirectStandardError (Join-Path $logDirectory 'local-embedding.stderr.log') -PassThru
$process.Id | Set-Content -LiteralPath (Join-Path $logDirectory 'local-embedding.pid')
Write-Output "Started local embedding process $($process.Id). Model loading may take a while; check http://127.0.0.1:$Port/health and .data/local-embedding.stderr.log."

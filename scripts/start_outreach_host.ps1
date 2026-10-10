$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $root 'compose.yaml')) {
    & docker compose --project-directory $root up -d --wait postgres | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Local PostgreSQL did not become healthy.' }
}
& (Join-Path $PSScriptRoot 'start_viber_vm.ps1')
$logDir = Join-Path $env:LOCALAPPDATA 'viber-cli'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'The viber-cli virtual environment is missing.' }
Set-Location -LiteralPath $root
& $python (Join-Path $root 'web.py') *>> (Join-Path $logDir 'host.log')


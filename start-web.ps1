param([int]$Port = 4001, [int]$EmailPort = 4000)
$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = (Get-Command python -ErrorAction Stop).Source }
& $taskPython (Join-Path $PSScriptRoot 'web.py') --port $Port --email-port $EmailPort
exit $LASTEXITCODE

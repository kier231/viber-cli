$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$stateDir = Join-Path $env:LOCALAPPDATA 'viber-cli'
$tokenPath = Join-Path $stateDir 'vm-token.txt'
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$viber = Join-Path $env:LOCALAPPDATA 'Viber\Viber.exe'
$tesseract = 'C:\Program Files\Tesseract-OCR'
if (-not (Test-Path -LiteralPath $tokenPath)) { throw 'The VM bridge token is missing.' }
if (-not (Test-Path -LiteralPath $python)) { throw 'The VM Python environment is missing.' }
if (-not (Get-Process Viber -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path -LiteralPath $viber)) { throw 'Viber Desktop is not installed.' }
    Start-Process -FilePath $viber
}
$deadline = (Get-Date).AddSeconds(45)
$process = $null
while ((Get-Date) -lt $deadline) {
    $process = Get-Process Viber -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($process) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $process) { throw 'The Viber window did not open.' }
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ViberWindow {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
[ViberWindow]::ShowWindow($process.MainWindowHandle, 3) | Out-Null
[ViberWindow]::SetForegroundWindow($process.MainWindowHandle) | Out-Null
$env:VIBER_CLI_VM_TOKEN = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
$env:VIBER_CLI_VM_PORT = '4011'
$env:PATH = "$tesseract;$env:PATH"
$env:TESSDATA_PREFIX = Join-Path $stateDir 'tessdata'
Set-Location -LiteralPath $root
& $python -m app.vm_agent


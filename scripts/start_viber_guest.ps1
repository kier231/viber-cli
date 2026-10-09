$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$stateDir = Join-Path $env:LOCALAPPDATA 'viber-cli'
$tokenPath = Join-Path $stateDir 'vm-token.txt'
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$viber = Join-Path $env:LOCALAPPDATA 'Viber\Viber.exe'
$tesseract = 'C:\Program Files\Tesseract-OCR'
if (-not (Test-Path -LiteralPath $tokenPath)) { throw 'The VM bridge token is missing.' }
if (-not (Test-Path -LiteralPath $python)) { throw 'The VM Python environment is missing.' }
$env:VIBER_CLI_VM_TOKEN = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
$env:VIBER_CLI_VM_PORT = '4011'
$env:PATH = "$tesseract;$env:PATH"
$env:TESSDATA_PREFIX = Join-Path $stateDir 'tessdata'
Set-Location -LiteralPath $root
if (-not (Get-NetTCPConnection -State Listen -LocalPort 4011 -ErrorAction SilentlyContinue)) {
    Get-Process Viber -ErrorAction SilentlyContinue | Stop-Process -Force
    $stopDeadline = (Get-Date).AddSeconds(10)
    while (Get-Process Viber -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $stopDeadline) { throw 'The old Viber process did not stop.' }
        Start-Sleep -Milliseconds 100
    }
    Start-Process -FilePath $python -WorkingDirectory $root -ArgumentList '-m','app.vm_agent' -WindowStyle Hidden
    $agentDeadline = (Get-Date).AddSeconds(70)
    while (-not (Get-NetTCPConnection -State Listen -LocalPort 4011 -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -ge $agentDeadline) { throw 'The VM bridge did not start.' }
        Start-Sleep -Milliseconds 50
    }
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


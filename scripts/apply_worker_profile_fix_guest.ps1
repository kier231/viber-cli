$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$stage = $PSScriptRoot
if ((Resolve-Path -LiteralPath $root).Path -ne $root) { throw 'Unexpected Viber installation path.' }
$listener = @(Get-NetTCPConnection -LocalPort 4011 -State Listen -ErrorAction SilentlyContinue)
$agents = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)-m\s+app\.vm_agent(?:\s|$)' })
if ($agents.Count -gt 1) { throw 'Multiple VM agents exist. Do not start a replacement.' }
if ($listener.Count) {
    $listenerIds = @($listener.OwningProcess | Select-Object -Unique)
    if ($listenerIds.Count -ne 1 -or $agents.Count -ne 1 -or $listenerIds[0] -ne $agents[0].ProcessId) {
        throw 'Port 4011 is not owned by the single expected VM agent.'
    }
}
if ($agents.Count -eq 1) {
    # Hold the original process handle: Windows may still enumerate an exited
    # PID while another component retains a handle to the process object.
    $agentProcess = Get-Process -Id $agents[0].ProcessId
    try {
        $null = $agentProcess.Handle
        Stop-Process -InputObject $agentProcess -Force
        if (-not $agentProcess.WaitForExit(15000) -or -not $agentProcess.HasExited) {
            throw 'The old VM agent did not stop. No replacement was started.'
        }
        Write-Output ('Old VM agent exit confirmed: ' + $agents[0].ProcessId)
    } finally { $agentProcess.Dispose() }
}
if (Get-NetTCPConnection -LocalPort 4011 -State Listen -ErrorAction SilentlyContinue) { throw 'The old bridge is still running.' }
$backup = Join-Path $root ('backups\profile-fix-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup | Out-Null
foreach ($name in @('viber_background.py','vm_agent.py')) {
    $destination = Join-Path $root ('app\' + $name)
    Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $name)
    Copy-Item -LiteralPath (Join-Path $stage $name) -Destination $destination -Force
}
# Guest Control runs outside the interactive desktop. Reuse the logged-in
# owner's desktop through a temporary task; never start a second UI agent.
$taskName = 'ViberProfileFixReload'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\viber-cli\scripts\start_viber_guest.ps1'
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Worker files installed; interactive restart requested.'

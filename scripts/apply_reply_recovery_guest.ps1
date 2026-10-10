param([Parameter(Mandatory=$true)][string]$ExpectedUuid)
$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$stateDir = Join-Path $env:LOCALAPPDATA 'viber-cli'
$marker = Get-Content -LiteralPath (Join-Path $stateDir 'instance-reset-complete.json') -Raw | ConvertFrom-Json
if ((Get-CimInstance Win32_ComputerSystemProduct).UUID -ne $ExpectedUuid -or $marker.uuid -ne $ExpectedUuid -or $marker.id -notin @('instance-1','instance-2')) {
    throw 'Only the verified replacement VM may receive this reply worker update.'
}
$agents = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)-m\s+app\.vm_agent(?:\s|$)' })
$readers = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)-m\s+app\.viber_source_worker(?:\s|$)' })
$listener = @(Get-NetTCPConnection -LocalPort 4011 -State Listen -ErrorAction SilentlyContinue)
if ($agents.Count -lt 1 -or $agents.Count -gt 2 -or $readers.Count -gt 2 -or @($listener.OwningProcess | Select-Object -Unique).Count -ne 1) {
    throw 'The single expected VM agent and reader could not be identified.'
}
$nativeAgent = @($agents | Where-Object ProcessId -eq $listener[0].OwningProcess)
if ($nativeAgent.Count -ne 1) { throw 'The bridge is not owned by the expected native agent.' }
if ($agents.Count -eq 2) {
    $launcher = @($agents | Where-Object ProcessId -ne $nativeAgent[0].ProcessId)
    if ($nativeAgent[0].ParentProcessId -ne $launcher[0].ProcessId -or $launcher[0].ExecutablePath -ne 'C:\viber-cli\.venv\Scripts\pythonw.exe') {
        throw 'Multiple independent VM agents exist; replacement is blocked.'
    }
}
if ($readers.Count) {
    $readerRoot = @($readers | Where-Object ParentProcessId -eq $nativeAgent[0].ProcessId)
    if ($readerRoot.Count -ne 1 -or @($readers | Where-Object { $_.ProcessId -ne $readerRoot[0].ProcessId -and $_.ParentProcessId -ne $readerRoot[0].ProcessId }).Count) {
        throw 'The private reader process tree could not be verified.'
    }
}
# A Windows venv launcher and its native Python child form one worker.
$agentProcesses = @($agents | ForEach-Object { Get-Process -Id $_.ProcessId })
$readerProcesses = @($readers | ForEach-Object { Get-Process -Id $_.ProcessId })
try {
    foreach ($process in $agentProcesses) { $null = $process.Handle }
    foreach ($process in $readerProcesses) { $null = $process.Handle }
    Stop-ScheduledTask -TaskName 'SajtologViberWorker' -ErrorAction SilentlyContinue
    foreach ($process in $agentProcesses) {
        if (-not $process.HasExited) { Stop-Process -InputObject $process -Force }
        if (-not $process.WaitForExit(15000)) { throw 'Old VM agent is still running; replacement is blocked.' }
    }
    foreach ($process in $readerProcesses) {
        if (-not $process.WaitForExit(2000)) { Stop-Process -InputObject $process -Force }
        if (-not $process.WaitForExit(15000)) { throw 'Old private reader is still running; replacement is blocked.' }
    }
    if (Get-NetTCPConnection -LocalPort 4011 -State Listen -ErrorAction SilentlyContinue) { throw 'The old bridge is still running.' }
    foreach ($name in @('viber_background.py','vm_agent.py','reply_errors.py')) {
        $destination = Join-Path $root ('app\'+$name)
        if (Test-Path -LiteralPath $destination) {
            Copy-Item -LiteralPath $destination -Destination (Join-Path $PSScriptRoot ('previous_'+$name))
        }
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $destination -Force
        if ((Get-FileHash -LiteralPath $destination).Hash -ne (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $name)).Hash) {
            throw 'Installed worker source verification failed.'
        }
    }
    Start-ScheduledTask -TaskName 'SajtologViberWorker'
    Write-Output ('Reply worker updated; old processes stopped before restarting ' + $marker.id + '.')
    Get-FileHash -LiteralPath $destination -Algorithm SHA256 | Select-Object Hash
} finally {
    foreach ($process in $agentProcesses) { $process.Dispose() }
    foreach ($process in $readerProcesses) { $process.Dispose() }
}

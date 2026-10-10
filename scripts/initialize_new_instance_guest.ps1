[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ExpectedUuid)
$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$stage = $PSScriptRoot
$actualUuid = (Get-CimInstance Win32_ComputerSystemProduct).UUID
if ($actualUuid -ne $ExpectedUuid -or $actualUuid -eq '1eee7e93-49c1-4b58-9150-68e11d16eb18') {
    throw 'This is not the intended replacement VM. Profile reset is blocked.'
}
if (Get-NetAdapter -Physical | Where-Object Status -eq 'Up') {
    throw 'Disconnect the replacement VM network before clearing copied account data.'
}
$slot = Get-Content -LiteralPath (Join-Path $stage 'slot.json') -Raw | ConvertFrom-Json
if ($slot.id -notin @('instance-1','instance-2') -or $slot.token.Length -lt 32) { throw 'Invalid instance provisioning data.' }
$stateDir = Join-Path $env:LOCALAPPDATA 'viber-cli'
$marker = Join-Path $stateDir 'instance-reset-complete.json'
if (Test-Path -LiteralPath $marker) {
    $done = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($done.id -ne $slot.id -or $done.uuid -ne $actualUuid) { throw 'The replacement VM was already provisioned for another slot.' }
    Write-Output 'The new profile was already prepared; existing activation data was preserved.'
    exit 0
}
foreach ($task in Get-ScheduledTask) {
    if (@($task.Actions | Where-Object { ($_.Arguments + ' ' + $_.Execute) -match '(?i)viber-cli|app\.vm_agent' }).Count) {
        Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-Null
    }
}
# This script runs only inside the guarded, network-disconnected replacement.
foreach ($process in @(Get-Process Viber -ErrorAction SilentlyContinue)) {
    $null = $process.Handle
    Stop-Process -InputObject $process -Force
    if (-not $process.WaitForExit(15000)) { throw 'Copied Viber process did not stop.' }
    $process.Dispose()
}
$agents = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)-m\s+app\.(vm_agent|viber_source_worker)(?:\s|$)' })
foreach ($agent in $agents) {
    $process = Get-Process -Id $agent.ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        $null = $process.Handle
        if (-not $process.HasExited) { Stop-Process -InputObject $process -Force }
        if (-not $process.WaitForExit(15000)) { throw 'Copied worker process did not stop; reset is blocked.' }
        $process.Dispose()
    }
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
foreach ($profileRoot in @($env:APPDATA, $env:LOCALAPPDATA)) {
    $profile = Join-Path $profileRoot 'ViberPC'
    if (Test-Path -LiteralPath $profile) {
        $resolvedRoot = [IO.Path]::GetFullPath($profileRoot).TrimEnd('\') + '\'
        $resolvedProfile = (Resolve-Path -LiteralPath $profile).Path
        if (-not $resolvedProfile.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected Viber profile path.' }
        Rename-Item -LiteralPath $resolvedProfile -NewName ('ViberPC-archived-' + $stamp)
    }
}
if (Test-Path -LiteralPath 'HKCU:\Software\Viber') {
    Rename-Item -LiteralPath 'HKCU:\Software\Viber' -NewName ('Viber-archived-' + $stamp)
}
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$run = Get-ItemProperty -LiteralPath $runKey -Name Viber -ErrorAction SilentlyContinue
if ($run -and $run.Viber -match '(?i)Viber\.exe') { Remove-ItemProperty -LiteralPath $runKey -Name Viber }
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $stateDir 'vm-token.txt'), $slot.token, [Text.UTF8Encoding]::new($false))
@{id=$slot.id; wait_for_activation=$true} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDir 'instance.json') -Encoding UTF8
foreach ($name in @('vm_agent.py','viber_background.py','viber_key_capture.py')) {
    Copy-Item -LiteralPath (Join-Path $stage $name) -Destination (Join-Path $root ('app\' + $name)) -Force
}
Copy-Item -LiteralPath (Join-Path $stage 'start_viber_guest.ps1') -Destination (Join-Path $root 'scripts\start_viber_guest.ps1') -Force
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute (Join-Path $root '.venv\Scripts\pythonw.exe') -Argument '-m app.vm_agent' -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'SajtologViberWorker' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
@{id=$slot.id; uuid=$actualUuid; copied_profile_cleared=$true} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
Write-Output ('Fresh Viber profile prepared for ' + $slot.id + '. Account linking will start after reboot.')

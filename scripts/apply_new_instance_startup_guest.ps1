param([Parameter(Mandatory=$true)][string]$ExpectedUuid)
$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$stateDir = Join-Path $env:LOCALAPPDATA 'viber-cli'
$marker = Get-Content -LiteralPath (Join-Path $stateDir 'instance-reset-complete.json') -Raw | ConvertFrom-Json
if ((Get-CimInstance Win32_ComputerSystemProduct).UUID -ne $ExpectedUuid -or $marker.uuid -ne $ExpectedUuid -or $marker.id -notin @('instance-1','instance-2')) {
    throw 'Only the verified replacement VM may receive this startup update.'
}
Stop-ScheduledTask -TaskName 'SajtologViberWorker' -ErrorAction SilentlyContinue
$agents = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match '(?i)-m\s+app\.vm_agent(?:\s|$)' })
$processes = @($agents | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
foreach ($process in $processes) { $null = $process.Handle }
foreach ($process in $processes) {
    if (-not $process.HasExited) { Stop-Process -InputObject $process -Force }
    if (-not $process.WaitForExit(15000)) { throw 'Old agent is still running; replacement is blocked.' }
    $process.Dispose()
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'vm_agent.py') -Destination (Join-Path $root 'app\vm_agent.py') -Force
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute (Join-Path $root '.venv\Scripts\pythonw.exe') -Argument '-m app.vm_agent' -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'SajtologViberWorker' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'SajtologViberWorker'
Write-Output ('Direct registration launcher installed for ' + $marker.id + '.')

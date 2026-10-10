$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$inventory = Get-Content -LiteralPath (Join-Path $root 'data\instances.json') -Raw | ConvertFrom-Json
if ($inventory.instances.Count -ne 2 -or @($inventory.instances | Where-Object state -ne 'AWAITING_NUMBER').Count) {
    throw 'Prepare the two new Viber profiles before starting the controller.'
}
# Task Scheduler owns the launcher, so the server survives the installer shell.
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\viber-cli\scripts\start_outreach_host.ps1'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'SajtologViberController' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'SajtologViberController'
Write-Output 'The local controller and the two new VM desktops are starting.'

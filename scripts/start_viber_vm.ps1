$ErrorActionPreference = 'Stop'
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
if (-not (Test-Path -LiteralPath $vbox)) { throw 'VirtualBox is not installed.' }
$state = (& $vbox showvminfo ViberWorker --machinereadable | Select-String '^VMState=').Line
if ($state -ne 'VMState="running"') {
    & $vbox startvm ViberWorker --type headless | Out-Null
}


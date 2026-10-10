$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
if (-not (Test-Path -LiteralPath $vbox)) { throw 'VirtualBox is not installed.' }
$inventory = Join-Path $root 'data\instances.json'
$names = @('ViberWorker')
if (Test-Path -LiteralPath $inventory) {
    $manifest = Get-Content -LiteralPath $inventory -Raw | ConvertFrom-Json
    $names = @($manifest.instances | ForEach-Object {
        if ($_.state -notin @('AWAITING_NUMBER','READY')) { throw 'Finish isolated profile preparation before starting this VM.' }
        $_.vm_name
    })
}
foreach ($name in $names) {
    $state = (& $vbox showvminfo $name --machinereadable | Select-String '^VMState=').Line
    if ($state -eq 'VMState="poweroff"') {
        & $vbox startvm $name --type gui | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('Could not start ' + $name) }
    }
}


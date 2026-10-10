# Provision only the two recorded replacement VMs; the original stays off.
param([switch]$PrepareOnly)
$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$kit = Join-Path $root 'backups\two-instance-kit'
$manifestPath = Join-Path $root 'data\instances.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.instances.Count -ne 2) { throw 'Two replacement VMs must be recorded before initializing.' }
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
$credentialPath = Join-Path $env:LOCALAPPDATA 'Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\viber-cli\vm-credential.xml'
$credential = Import-Clixml -LiteralPath $credentialPath
$passwordFile = Join-Path $root ('private\provision-auth-' + [guid]::NewGuid().ToString('N') + '.tmp')
$guestStage = 'C:\viber-cli\backups\new-instance-setup'
function Guest([string]$Vm, [string[]]$Arguments) {
    $command = $Arguments[0]
    $remaining = @($Arguments | Select-Object -Skip 1)
    & $vbox guestcontrol $Vm $command --username $credential.UserName --passwordfile $passwordFile @remaining
    if ($LASTEXITCODE -ne 0) { throw ('Guest provisioning failed for ' + $Vm + '. Its copied account remains isolated.') }
}
function Info([string]$Vm) { return @(& $vbox showvminfo $Vm --machinereadable) }
try {
    [IO.File]::WriteAllText($passwordFile, $credential.GetNetworkCredential().Password, [Text.UTF8Encoding]::new($false))
    foreach ($slot in $manifest.instances) {
        if ($slot.vm_name -notin @('ViberWorker1','ViberWorker2') -or $slot.state -notin @('NETWORK_ISOLATED','AWAITING_NUMBER')) { throw 'Unexpected provisioning slot.' }
        $vmInfo = Info $slot.vm_name
        if ($vmInfo -notcontains ('UUID="' + $slot.vm_uuid + '"')) { throw 'VM identity differs from the recorded replacement.' }
        if ($slot.state -eq 'NETWORK_ISOLATED') {
            if ($vmInfo -contains 'VMState="running"') {
                & $vbox controlvm $slot.vm_name setlinkstate1 off
            } else {
                & $vbox modifyvm $slot.vm_name --cableconnected1 off
            }
            if ($LASTEXITCODE -ne 0) { throw 'Could not isolate the replacement network.' }
            if ($vmInfo -contains 'VMState="poweroff"') {
                & $vbox startvm $slot.vm_name --type headless
                if ($LASTEXITCODE -ne 0) { throw 'Could not start the isolated replacement.' }
            }
            $deadline = (Get-Date).AddSeconds(300)
            do {
                $ErrorActionPreference = 'Continue'
                & $vbox guestcontrol $slot.vm_name run --username $credential.UserName --passwordfile $passwordFile --exe 'C:\Windows\System32\cmd.exe' --wait-stdout --wait-stderr --timeout 60000 -- cmd.exe /c exit 0 2>$null | Out-Null
                $ErrorActionPreference = 'Stop'
                if ($LASTEXITCODE -eq 0) { break }
                if ((Get-Date) -gt $deadline) { throw 'Guest Additions did not become ready.' }
                Start-Sleep -Seconds 2
            } while ($true)
            Start-Sleep -Seconds 15
            Guest -Vm $slot.vm_name -Arguments @('mkdir','--parents',$guestStage)
            $bridge = Get-Content -LiteralPath $slot.bridge_config -Raw | ConvertFrom-Json
            $slotFile = Join-Path $root ('private\' + $slot.id + '-guest.json')
            @{id=$slot.id;token=$bridge.token} | ConvertTo-Json | Set-Content -LiteralPath $slotFile -Encoding UTF8
            foreach ($name in @('initialize_new_instance_guest.ps1','vm_agent.py','viber_background.py','viber_key_capture.py','start_viber_guest.ps1')) {
                Guest -Vm $slot.vm_name -Arguments @('copyto',(Join-Path $kit $name),($guestStage + '\' + $name))
            }
            Guest -Vm $slot.vm_name -Arguments @('copyto',$slotFile,($guestStage + '\slot.json'))
            Guest -Vm $slot.vm_name -Arguments @('run','--exe','C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','--wait-stdout','--wait-stderr','--timeout','300000','--','powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',($guestStage + '\initialize_new_instance_guest.ps1'),'-ExpectedUuid',$slot.hardware_uuid)
            & $vbox controlvm $slot.vm_name acpipowerbutton
            $deadline = (Get-Date).AddSeconds(120)
            while ((Info $slot.vm_name) -notcontains 'VMState="poweroff"') {
                if ((Get-Date) -gt $deadline) { throw 'Replacement VM has not shut down after profile reset.' }
                Start-Sleep -Seconds 2
            }
            & $vbox modifyvm $slot.vm_name --cableconnected1 on
            if ($LASTEXITCODE -ne 0) { throw 'Could not connect the clean replacement network.' }
            $slot.state = 'AWAITING_NUMBER'
            $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
        }
        if (-not $PrepareOnly -and (Info $slot.vm_name) -contains 'VMState="poweroff"') {
            & $vbox startvm $slot.vm_name --type gui
            if ($LASTEXITCODE -ne 0) { throw 'Could not open the replacement registration desktop.' }
        }
        Write-Output ($slot.label + ' profile is ready for a new number.')
    }
} finally { if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile -Force } }

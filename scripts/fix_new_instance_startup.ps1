$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
$credentialPath = Join-Path $env:LOCALAPPDATA 'Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\viber-cli\vm-credential.xml'
$credential = Import-Clixml -LiteralPath $credentialPath
$authFile = Join-Path $root ('private\startup-auth-' + [guid]::NewGuid().ToString('N') + '.tmp')
$manifest = Get-Content -LiteralPath (Join-Path $root 'data\instances.json') -Raw | ConvertFrom-Json
try {
    [IO.File]::WriteAllText($authFile,$credential.GetNetworkCredential().Password,[Text.UTF8Encoding]::new($false))
    foreach ($slot in $manifest.instances) {
        if ($slot.vm_name -notin @('ViberWorker1','ViberWorker2')) { throw 'Unexpected VM.' }
        Write-Output ('Updating registration startup for ' + $slot.vm_name)
        foreach ($name in @('vm_agent.py','apply_new_instance_startup_guest.ps1')) {
            & $vbox guestcontrol $slot.vm_name copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot $name) ('C:\viber-cli\backups\new-instance-setup\' + $name)
            if ($LASTEXITCODE -ne 0) { throw 'Could not stage the registration startup update.' }
        }
        & $vbox guestcontrol $slot.vm_name run --username $credential.UserName --passwordfile $authFile --exe 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' --wait-stdout --wait-stderr --timeout 300000 -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\viber-cli\backups\new-instance-setup\apply_new_instance_startup_guest.ps1' -ExpectedUuid $slot.hardware_uuid
        if ($LASTEXITCODE -ne 0) { throw 'The new registration startup did not install.' }
    }
} finally { if (Test-Path -LiteralPath $authFile) { Remove-Item -LiteralPath $authFile -Force } }

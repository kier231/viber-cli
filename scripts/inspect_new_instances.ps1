param([string[]]$VmNames=@('ViberWorker1','ViberWorker2'))
$ErrorActionPreference = 'Stop'
if (@($VmNames | Where-Object { $_ -notin @('ViberWorker1','ViberWorker2') }).Count) { throw 'Unexpected VM selection.' }
$root = 'C:\viber-cli'
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
$credentialPath = Join-Path $env:LOCALAPPDATA 'Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\viber-cli\vm-credential.xml'
$credential = Import-Clixml -LiteralPath $credentialPath
$authFile = Join-Path $root ('private\inspect-auth-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    New-Item -ItemType File -Path $authFile | Out-Null
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().Name,'FullControl','Allow'))
    Set-Acl -LiteralPath $authFile -AclObject $acl
    [IO.File]::WriteAllText($authFile,$credential.GetNetworkCredential().Password,[Text.UTF8Encoding]::new($false))
    foreach ($vm in $VmNames) {
        Write-Output ('Inspecting ' + $vm)
        & $vbox guestcontrol $vm copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot 'inspect_new_instance_guest.ps1') 'C:\viber-cli\backups\new-instance-setup\inspect_new_instance_guest.ps1'
        if ($LASTEXITCODE -ne 0) { throw 'Could not stage startup diagnostics.' }
        & $vbox guestcontrol $vm run --username $credential.UserName --passwordfile $authFile --exe 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' --wait-stdout --wait-stderr --timeout 180000 -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\viber-cli\backups\new-instance-setup\inspect_new_instance_guest.ps1'
        if ($LASTEXITCODE -ne 0) { throw 'Startup diagnostics failed.' }
    }
} finally { if (Test-Path -LiteralPath $authFile) { Remove-Item -LiteralPath $authFile -Force } }

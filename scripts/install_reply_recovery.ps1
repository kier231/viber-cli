param([Parameter(Mandatory=$true)][string]$BackupPath,[string[]]$InstanceIds=@('instance-1','instance-2'))
$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
if (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Gracefully stop the controller and drain work before updating VM reply workers.'
}
$resolvedBackup = (Resolve-Path -LiteralPath $BackupPath).Path
if (-not $resolvedBackup.StartsWith('C:\viber-cli\backups\reply-recovery-', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unexpected recovery backup path.'
}
$credentialPath = Join-Path $env:LOCALAPPDATA 'Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\viber-cli\vm-credential.xml'
$credential = Import-Clixml -LiteralPath $credentialPath
if ($credential -isnot [Management.Automation.PSCredential]) { throw 'The saved VM credential is invalid.' }
$authFile = Join-Path $root ('private\reply-fix-auth-' + [guid]::NewGuid().ToString('N') + '.tmp')
$manifest = Get-Content -LiteralPath (Join-Path $root 'data\instances.json') -Raw | ConvertFrom-Json
if (@($InstanceIds | Where-Object { $_ -notin @('instance-1','instance-2') }).Count) { throw 'Unexpected instance selection.' }
try {
    New-Item -ItemType File -Path $authFile | Out-Null
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().Name,'FullControl','Allow'))
    Set-Acl -LiteralPath $authFile -AclObject $acl
    [IO.File]::WriteAllText($authFile,$credential.GetNetworkCredential().Password,[Text.UTF8Encoding]::new($false))
    foreach ($slot in $manifest.instances) {
        if ($slot.id -notin $InstanceIds) { continue }
        if ($slot.vm_name -notin @('ViberWorker1','ViberWorker2')) { throw 'Unexpected VM.' }
        $guestStage = 'C:\viber-cli\backups\' + (Split-Path -Leaf $resolvedBackup)
        & $vbox guestcontrol $slot.vm_name mkdir --username $credential.UserName --passwordfile $authFile --parents $guestStage
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the guest backup directory.' }
        foreach ($name in @('viber_background.py','vm_agent.py','reply_errors.py')) {
            & $vbox guestcontrol $slot.vm_name copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot ('..\app\'+$name)) ($guestStage+'\'+$name)
            if ($LASTEXITCODE -ne 0) { throw 'Could not stage the tested worker code.' }
        }
        & $vbox guestcontrol $slot.vm_name copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot 'apply_reply_recovery_guest.ps1') ($guestStage+'\apply_reply_recovery_guest.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Could not stage the trusted guest installer.' }
        & $vbox guestcontrol $slot.vm_name run --username $credential.UserName --passwordfile $authFile --exe 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' --wait-stdout --wait-stderr --timeout 60000 -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File ($guestStage+'\apply_reply_recovery_guest.ps1') -ExpectedUuid $slot.hardware_uuid
        if ($LASTEXITCODE -ne 0) { throw 'Guest reply worker update failed; no automatic retry.' }
    }
} finally {
    if (Test-Path -LiteralPath $authFile) { Remove-Item -LiteralPath $authFile -Force }
}

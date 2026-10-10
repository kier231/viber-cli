param([Parameter(Mandatory=$true)][string]$BackupPath, [string]$CredentialPath)
$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
if (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Gracefully stop the controller and drain work before updating VM readers.'
}
$resolvedBackup = (Resolve-Path -LiteralPath $BackupPath).Path
if ($resolvedBackup -notmatch '^C:\\viber-cli\\backups\\(?:reader-cache|reply-recovery)-[^\\]+$') {
    throw 'Unexpected reader backup path.'
}
$credentialCandidates = @((Join-Path $env:LOCALAPPDATA 'viber-cli\vm-credential.xml'),
    (Join-Path $env:USERPROFILE 'AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\viber-cli\vm-credential.xml'))
if (-not $CredentialPath) {
    $savedCredentials = @($credentialCandidates | Select-Object -Unique | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($savedCredentials.Count -ne 1) { throw 'Choose the exact saved host VM credential path when multiple copies exist.' }
    $CredentialPath = $savedCredentials[0]
} elseif ([IO.Path]::GetFullPath($CredentialPath) -notin $credentialCandidates -or -not (Test-Path -LiteralPath $CredentialPath -PathType Leaf)) {
    throw 'Use a saved credential from the known host Viber configuration.'
}
$credential = Import-Clixml -LiteralPath $credentialPath
if ($credential -isnot [Management.Automation.PSCredential]) { throw 'The saved VM credential is invalid.' }
$authFile = Join-Path $root ('private\reader-fix-auth-' + [guid]::NewGuid().ToString('N') + '.tmp')
$manifest = Get-Content -LiteralPath (Join-Path $root 'data\instances.json') -Raw | ConvertFrom-Json
try {
    New-Item -ItemType File -Path $authFile | Out-Null
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().Name,'FullControl','Allow'))
    Set-Acl -LiteralPath $authFile -AclObject $acl
    [IO.File]::WriteAllText($authFile,$credential.GetNetworkCredential().Password,[Text.UTF8Encoding]::new($false))
    foreach ($slot in $manifest.instances) {
        if ($slot.vm_name -notin @('ViberWorker1','ViberWorker2')) { throw 'Unexpected VM.' }
        $guestStage = 'C:\viber-cli\backups\' + (Split-Path -Leaf $resolvedBackup)
        & $vbox guestcontrol $slot.vm_name mkdir --username $credential.UserName --passwordfile $authFile --parents $guestStage
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the guest backup directory.' }
        & $vbox guestcontrol $slot.vm_name copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot '..\app\viber_source_worker.py') ($guestStage+'\viber_source_worker.py')
        if ($LASTEXITCODE -ne 0) { throw 'Could not stage the reader update.' }
        & $vbox guestcontrol $slot.vm_name copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot '..\app\viber_database.py') ($guestStage+'\viber_database.py')
        if ($LASTEXITCODE -ne 0) { throw 'Could not stage the native message query update.' }
        & $vbox guestcontrol $slot.vm_name copyto --username $credential.UserName --passwordfile $authFile (Join-Path $PSScriptRoot 'apply_reader_cache_fix_guest.ps1') ($guestStage+'\apply_reader_cache_fix_guest.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Could not stage the trusted guest installer.' }
        & $vbox guestcontrol $slot.vm_name run --username $credential.UserName --passwordfile $authFile --exe 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' --wait-stdout --wait-stderr --timeout 60000 -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File ($guestStage+'\apply_reader_cache_fix_guest.ps1') -ExpectedUuid $slot.hardware_uuid
        if ($LASTEXITCODE -ne 0) { throw 'Guest reader update failed; no automatic retry.' }
    }
} finally {
    if (Test-Path -LiteralPath $authFile) { Remove-Item -LiteralPath $authFile -Force }
}

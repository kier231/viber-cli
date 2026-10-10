# Run on the HOST PC. Install the worker fix and verify review without dispatch.
[CmdletBinding()]
param(
    [string]$CredentialPath,
    [string]$BridgeConfigPath,
    [switch]$ResumeFailedMaintenance,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$root = 'C:\viber-cli'
$kit = $PSScriptRoot
$vbox = Join-Path $env:ProgramFiles 'Oracle\VirtualBox\VBoxManage.exe'
$python = Join-Path $root '.venv\Scripts\python.exe'
$guestStage = 'C:\viber-cli\backups\worker-profile-fix'
$passwordFile = Join-Path $root ('backups\profile-fix-auth-' + [guid]::NewGuid().ToString('N') + '.tmp')
$controllerStopped = $false
function Invoke-GuestCommand([string[]]$Arguments) {
    $command = $Arguments[0]
    $commandArguments = @($Arguments | Select-Object -Skip 1)
    # Put authentication options before positional paths for VBoxManage's parser.
    & $vbox guestcontrol ViberWorker $command --username $vmCredential.UserName --passwordfile $passwordFile @commandArguments
    if ($LASTEXITCODE -ne 0) { throw 'The VM operation failed; no automatic retry.' }
}
function Start-Controller {
    if (-not (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath $python -ArgumentList 'web.py' -WorkingDirectory $root -WindowStyle Hidden
    }
}

# Validate the host and all inputs before reading credentials or stopping work.
# CheckOnly deliberately does not import or decrypt the credential file.
$hostUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Output ('Machine: ' + $env:COMPUTERNAME + '; user: ' + $hostUser)
if (-not (Test-Path -LiteralPath $vbox -PathType Leaf)) {
    throw 'VirtualBox was not found. Run this installer in PowerShell on the main PC, outside the ViberWorker VM.'
}
# Packaged Codex redirects LocalAppData writes into its private LocalCache.
# Regular PowerShell must address that physical location explicitly.
$dataFolders = @()
$packages = Join-Path $env:LOCALAPPDATA 'Packages'
if (Test-Path -LiteralPath $packages -PathType Container) {
    foreach ($package in @(Get-ChildItem -LiteralPath $packages -Directory -Filter 'OpenAI.Codex_*')) {
        $dataFolders += Join-Path $package.FullName 'LocalCache\Local\viber-cli'
    }
}
$dataFolders += Join-Path $env:LOCALAPPDATA 'viber-cli'
if (-not $CredentialPath) {
    $savedCandidates = @()
    foreach ($folder in $dataFolders) {
        $candidate = Join-Path $folder 'vm-credential.xml'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $savedCandidates += $candidate }
    }
    $packagedCandidates = @($savedCandidates | Where-Object { $_ -like '*\Packages\OpenAI.Codex_*\*' })
    if ($packagedCandidates.Count -gt 1) { throw 'Multiple saved Codex VM credentials exist. Specify the intended file with -CredentialPath. No worker files were changed.' }
    if ($savedCandidates.Count) { $CredentialPath = $savedCandidates[0] }
}
if (-not $CredentialPath) {
    throw 'No saved VM credential file was found in the regular or Codex app-data folders. No worker files were changed.'
}
if (-not [IO.Path]::IsPathRooted($CredentialPath)) {
    throw 'CredentialPath must be a full path to the saved host credential file.'
}
$credentialPath = [IO.Path]::GetFullPath($CredentialPath)
if (-not (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
    throw ('Saved VM credentials were not found at ' + $credentialPath + ' on ' + $env:COMPUTERNAME + ' for ' + $hostUser + '. No worker files were changed. Use the main PC session that originally configured ViberWorker; do not copy credentials into the VM.')
}
if (-not $BridgeConfigPath) {
    $BridgeConfigPath = Join-Path (Split-Path -Parent $credentialPath) 'vm-bridge.json'
}
if (-not [IO.Path]::IsPathRooted($BridgeConfigPath) -or -not (Test-Path -LiteralPath $BridgeConfigPath -PathType Leaf)) {
    throw 'The saved loopback bridge configuration was not found beside the VM credentials. No worker files were changed.'
}
$env:VIBER_CLI_VM_CONFIG = [IO.Path]::GetFullPath($BridgeConfigPath)
$requiredPaths = @($python, (Join-Path $root '.env'), (Join-Path $root 'web.py'))
foreach ($name in @('viber_background.py','vm_agent.py','vm_bridge.py','apply_worker_profile_fix_guest.ps1','worker_profile_fix_control.py')) {
    $requiredPaths += Join-Path $kit $name
}
foreach ($taskPath in $requiredPaths) {
    if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf)) {
        throw ('Required host installation file is missing: ' + $taskPath + '. No worker files were changed.')
    }
}
$vmInfo = & $vbox showvminfo ViberWorker --machinereadable 2>&1
if ($LASTEXITCODE -ne 0) { throw 'ViberWorker is not registered for this Windows user. Run the installer from the main PC session that configured the VM.' }
if (-not ($vmInfo -contains 'VMState="running"')) { throw 'Start the existing ViberWorker VM before installing. No worker files were changed.' }
Write-Output ('Host checks passed. Saved credential file exists: ' + $credentialPath)
$preflightArguments = @((Join-Path $kit 'worker_profile_fix_control.py'), 'preflight')
if ($ResumeFailedMaintenance) { $preflightArguments += '--resume-failed-maintenance' }
& $python @preflightArguments
if ($LASTEXITCODE -ne 0) { throw 'The saved bridge configuration or running VM health check failed. No worker files were changed.' }
if ($CheckOnly) {
    Write-Output 'Preflight complete. Credentials were not read; no processes or files were changed.'
    return
}

try {
    try {
        $vmCredential = Import-Clixml -LiteralPath $credentialPath
        if ($vmCredential -isnot [Management.Automation.PSCredential]) { throw 'The saved file does not contain a Windows credential.' }
    } catch {
        throw ('Could not load the saved VM credential as ' + $hostUser + ' on ' + $env:COMPUTERNAME + ' from ' + $credentialPath + '. Run as the same Windows user who saved it. No worker files were changed. Details: ' + $_.Exception.Message)
    }
    # Restrict the empty temporary file before writing any password to it.
    New-Item -ItemType File -Path $passwordFile | Out-Null
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().Name,'FullControl','Allow'))
    Set-Acl -LiteralPath $passwordFile -AclObject $acl
    [IO.File]::WriteAllText($passwordFile, $vmCredential.GetNetworkCredential().Password, [Text.UTF8Encoding]::new($false))
    Invoke-GuestCommand -Arguments @('mkdir','--parents',$guestStage)
    foreach ($name in @('viber_background.py','vm_agent.py','apply_worker_profile_fix_guest.ps1')) {
        # A single-file copy must name its destination file, not the existing
        # directory. Do not rely on --target-directory path interpretation.
        $guestDestination = $guestStage + '\' + $name
        Write-Output ('Staging worker file: ' + $name)
        Invoke-GuestCommand -Arguments @('copyto',(Join-Path $kit $name),$guestDestination)
    }
    & $python (Join-Path $kit 'worker_profile_fix_control.py') stop
    if ($LASTEXITCODE -ne 0) { throw 'Maintenance checks failed; the running worker was not changed.' }
    $controllerStopped = $true
    $backup = Join-Path $root ('backups\host-profile-fix-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $backup | Out-Null
    foreach ($name in @('viber_background.py','vm_agent.py','vm_bridge.py')) {
        $destination = Join-Path $root ('app\' + $name)
        Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $name)
        Copy-Item -LiteralPath (Join-Path $kit $name) -Destination $destination -Force
    }
    # Pass the executable as arg0, then a readable script path, with no encoded command.
    # This override applies only to this trusted installation process.
    & $vbox guestcontrol ViberWorker run --username $vmCredential.UserName --passwordfile $passwordFile --exe 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' --wait-stdout --wait-stderr --timeout 30000 -- powershell.exe -NoProfile -ExecutionPolicy Bypass -File ($guestStage + '\apply_worker_profile_fix_guest.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'The guest update failed.' }
    & $python (Join-Path $kit 'worker_profile_fix_control.py') health
    if ($LASTEXITCODE -ne 0) { throw 'The updated VM worker did not become ready.' }
    Start-Controller
    $readyDeadline = (Get-Date).AddSeconds(20)
    while (-not (Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -ge $readyDeadline) { throw 'The controller did not restart.' }
        Start-Sleep -Milliseconds 250
    }
    & $python (Join-Path $kit 'worker_profile_fix_control.py') verify
    if ($LASTEXITCODE -ne 0) { throw 'Review verification failed; no message was sent. Keep the backup and inspect the worker.' }
} finally {
    if ($controllerStopped) { Start-Controller }
    if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile -Force }
}

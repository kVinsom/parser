# Stop only verified local service processes and archive personal browser state.
param([Parameter(Mandatory=$true)][string]$Backup)
$ErrorActionPreference='Stop'
$taskRoot=[IO.Path]::GetFullPath((Split-Path $PSScriptRoot))
$taskBackup=[IO.Path]::GetFullPath($Backup)
$taskBackupRoot=Join-Path $taskRoot '.backups'
if (-not $taskBackup.StartsWith($taskBackupRoot + '\',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $taskBackup)) { throw 'Invalid or existing backup path.' }
$taskPython=(Resolve-Path (Join-Path $taskRoot 'services/telegram-bot/.venv/Scripts/python.exe')).Path
$taskWorkerMain=(Resolve-Path (Join-Path $taskRoot 'services/worker/main.py')).Path
$taskSources=@('.wallet-profiles','.wallet-contracts','.wallet-secrets') | ForEach-Object { Join-Path $taskRoot ('services/telegram-bot/' + $_) }
foreach($taskSource in $taskSources) {
    $taskResolved=[IO.Path]::GetFullPath($taskSource)
    if(-not $taskResolved.StartsWith($taskRoot + '\',[StringComparison]::OrdinalIgnoreCase)){throw 'State path outside project.'}
    if((Test-Path -LiteralPath $taskResolved) -and ((Get-Item -LiteralPath $taskResolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'State path is a link.'}
}
$taskAll=@(Get-CimInstance Win32_Process)
$taskTree=[System.Collections.Generic.List[object]]::new()
foreach($taskEntry in @(
    @{Path='services/telegram-bot/.artifacts/menu-preview.pid';Kind='bot'},
    @{Path='services/worker/.artifacts/monitor.pid';Kind='worker'}
)) {
    $taskPidFile=Join-Path $taskRoot $taskEntry.Path
    if(Test-Path -LiteralPath $taskPidFile){
        $taskId=[int](Get-Content -LiteralPath $taskPidFile)
        $taskProcess=$taskAll | Where-Object ProcessId -eq $taskId
        if($taskProcess){
            if($taskProcess.ExecutablePath -ne $taskPython){throw 'PID belongs to another executable.'}
            if($taskEntry.Kind -eq 'bot' -and $taskProcess.CommandLine -notmatch '\s-m vpars_bot run\s*$'){throw 'Unexpected bot command.'}
            if($taskEntry.Kind -eq 'worker' -and -not $taskProcess.CommandLine.Contains($taskWorkerMain)){throw 'Unexpected worker command.'}
            $taskTree.Add($taskProcess)
        }
    }
}
for($taskIndex=0;$taskIndex -lt $taskTree.Count;$taskIndex++){
    $taskParent=$taskTree[$taskIndex]
    foreach($taskChild in $taskAll | Where-Object {$_.ParentProcessId -eq $taskParent.ProcessId -and $_.CreationDate -ge $taskParent.CreationDate}){$taskTree.Add($taskChild)}
}
foreach($taskProcess in $taskTree){
    if($taskProcess.Name -eq 'conhost.exe' -and $taskProcess.ExecutablePath -eq (Join-Path $env:SystemRoot 'System32/conhost.exe')){continue}
    if($taskProcess.Name -ne 'python.exe' -or (-not $taskProcess.CommandLine.Contains($taskWorkerMain) -and $taskProcess.CommandLine -notmatch '\s-m vpars_bot run\s*$|\s-m uvicorn vpars_parser\.api:create_app --factory --host 127\.0\.0\.1 --port 8002')){throw 'Unexpected service child; nothing stopped.'}
}
New-Item -ItemType Directory -Path $taskBackup -Force | Out-Null
$taskAcl=[Security.AccessControl.DirectorySecurity]::new()
$taskAcl.SetAccessRuleProtection($true,$false)
foreach($taskSid in @([Security.Principal.WindowsIdentity]::GetCurrent().User,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'))){
    $taskRule=[Security.AccessControl.FileSystemAccessRule]::new($taskSid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $taskAcl.AddAccessRule($taskRule)
}
Set-Acl -LiteralPath $taskBackup -AclObject $taskAcl
$taskRecovery=Get-ScheduledTask -TaskName 'vpars-worker-recovery' -ErrorAction SilentlyContinue
$taskRecoveryEnabled=$taskRecovery -and $taskRecovery.State -ne 'Disabled'
@{recovery_enabled=[bool]$taskRecoveryEnabled} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskBackup 'recovery-state.json') -Encoding utf8
if($taskRecovery){Disable-ScheduledTask -TaskName 'vpars-worker-recovery' | Out-Null; Stop-ScheduledTask -TaskName 'vpars-worker-recovery'}
foreach($taskProcess in $taskTree){if($taskProcess.Name -ne 'conhost.exe'){Stop-Process -Id $taskProcess.ProcessId -ErrorAction SilentlyContinue}}
Start-Sleep -Seconds 1
foreach($taskProcess in $taskTree){if($taskProcess.Name -ne 'conhost.exe' -and (Get-Process -Id $taskProcess.ProcessId -ErrorAction SilentlyContinue)){throw 'A service process is still running.'}}
foreach($taskSource in $taskSources){
    if(Test-Path -LiteralPath $taskSource){
        $taskDestination=Join-Path $taskBackup (Split-Path $taskSource -Leaf)
        if(-not ([IO.Path]::GetFullPath($taskDestination)).StartsWith($taskBackup + '\',[StringComparison]::OrdinalIgnoreCase)){throw 'Archive destination outside backup.'}
        Move-Item -LiteralPath $taskSource -Destination $taskDestination
    }
}
@{processes=@($taskTree | Select-Object ProcessId,Name);stopped_at=(Get-Date).ToString('o')} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $taskBackup 'processes-stopped.json') -Encoding utf8
Write-Output 'Verified bot/worker processes stopped; personal browser state archived; recovery temporarily disabled.'

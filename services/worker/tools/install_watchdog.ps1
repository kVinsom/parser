# Recover the local worker after a process exit or the next Windows logon.
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot
$taskName = 'vpars-worker-recovery'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    return
}
$taskWrapper = (Resolve-Path (Join-Path $PSScriptRoot 'run_watchdog.py')).Path
# A GUI-subsystem executable never allocates a console. PowerShell's
# -WindowStyle Hidden can still flash a console before applying that setting.
$taskPython = (Resolve-Path (Join-Path $taskRoot '../telegram-bot/.venv/Scripts/pythonw.exe')).Path
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument ('"' + $taskWrapper + '"') -WorkingDirectory $taskRoot
$taskTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
)
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTriggers -Settings $taskSettings -Principal $taskPrincipal -Description 'Recover the local vpars search worker without changing saved Start/Stop commands.' -Force | Select-Object TaskName,State

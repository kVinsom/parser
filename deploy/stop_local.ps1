# Final cutover only: stop owned local services without changing personal Start/Stop.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot
$taskBotRoot = Join-Path $taskRoot 'services/telegram-bot'
$taskWorkerRoot = Join-Path $taskRoot 'services/worker'
$taskPython = (Resolve-Path (Join-Path $taskBotRoot '.venv/Scripts/python.exe')).Path
$taskMain = (Resolve-Path (Join-Path $taskWorkerRoot 'main.py')).Path
$taskAll = @(Get-CimInstance Win32_Process)
$taskOwned = [System.Collections.Generic.List[object]]::new()
foreach ($taskInfo in @(
    @{pidFile=(Join-Path $taskBotRoot '.artifacts/menu-preview.pid'); kind='bot'},
    @{pidFile=(Join-Path $taskWorkerRoot '.artifacts/monitor.pid'); kind='worker'}
)) {
    if (-not (Test-Path -LiteralPath $taskInfo.pidFile)) { continue }
    $taskPid = [int](Get-Content -LiteralPath $taskInfo.pidFile)
    $taskProcess = $taskAll | Where-Object ProcessId -eq $taskPid
    if (-not $taskProcess) { continue }
    if ($taskProcess.ExecutablePath -ne $taskPython -or -not $taskProcess.CommandLine) {
        throw 'Stored PID has an unexpected owner; nothing stopped.'
    }
    if (($taskInfo.kind -eq 'bot' -and $taskProcess.CommandLine -notmatch '\s-m vpars_bot run\s*$') -or
        ($taskInfo.kind -eq 'worker' -and -not $taskProcess.CommandLine.Contains($taskMain))) {
        throw 'Stored PID belongs to another command; nothing stopped.'
    }
    $taskOwned.Add($taskProcess)
}
for ($taskIndex = 0; $taskIndex -lt $taskOwned.Count; $taskIndex++) {
    $taskParent = $taskOwned[$taskIndex]
    foreach ($taskChild in $taskAll | Where-Object {
        $_.ParentProcessId -eq $taskParent.ProcessId -and $_.CreationDate -ge $taskParent.CreationDate
    }) {
        if ($taskChild.ProcessId -notin $taskOwned.ProcessId) { $taskOwned.Add($taskChild) }
    }
}
foreach ($taskProcess in $taskOwned) {
    if ($taskProcess.Name -eq 'conhost.exe' -and $taskProcess.ExecutablePath -eq (Join-Path $env:SystemRoot 'System32/conhost.exe')) { continue }
    if ($taskProcess.Name -ne 'python.exe' -or -not $taskProcess.CommandLine -or
        ($taskProcess.CommandLine -notmatch '\s-m vpars_bot run\s*$' -and
         -not $taskProcess.CommandLine.Contains($taskMain) -and
         $taskProcess.CommandLine -notmatch '\s-m uvicorn vpars_parser\.api:create_app --factory --host 127\.0\.0\.1 --port 8002 --log-level warning --no-access-log$')) {
        throw 'Unexpected child process; nothing stopped. Finish browser activity first.'
    }
}
$taskRecovery = Get-ScheduledTask -TaskName 'vpars-worker-recovery' -ErrorAction SilentlyContinue
if ($taskRecovery) {
    Disable-ScheduledTask -TaskName 'vpars-worker-recovery' | Out-Null
    Stop-ScheduledTask -TaskName 'vpars-worker-recovery' -ErrorAction SilentlyContinue
}
foreach ($taskProcess in $taskOwned) {
    # Recheck creation time in case Windows reused an exited process ID.
    $taskCurrent = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskProcess.ProcessId)"
    if ($taskCurrent -and $taskCurrent.CreationDate -eq $taskProcess.CreationDate) {
        Stop-Process -Id $taskCurrent.ProcessId -ErrorAction Stop
    }
}
Write-Output 'Local bot and worker stopped; recovery disabled. Saved Start/Stop unchanged. Export data before starting the server.'

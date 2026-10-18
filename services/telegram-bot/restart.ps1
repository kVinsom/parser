# Reload local bot code without changing saved search intent or Telegram offsets.
$ErrorActionPreference = 'Stop'
$taskPython = (Resolve-Path (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')).Path
$taskArtifacts = Join-Path $PSScriptRoot '.artifacts'
$taskPidFile = Join-Path $taskArtifacts 'menu-preview.pid'
if (Test-Path -LiteralPath $taskPidFile) {
    $taskOldId = [int](Get-Content -LiteralPath $taskPidFile)
    $taskOld = Get-CimInstance Win32_Process -Filter "ProcessId=$taskOldId"
    if ($taskOld) {
        if ($taskOld.ExecutablePath -ne $taskPython -or $taskOld.CommandLine -notmatch '\s-m vpars_bot run\s*$') {
            throw 'Stored bot PID belongs to another process; restart cancelled.'
        }
        $taskAll = @(Get-CimInstance Win32_Process)
        $taskTree = [System.Collections.Generic.List[object]]::new()
        $taskTree.Add($taskOld)
        for ($taskIndex = 0; $taskIndex -lt $taskTree.Count; $taskIndex++) {
            $taskParent = $taskTree[$taskIndex].ProcessId
            foreach ($taskChild in $taskAll | Where-Object {$_.ParentProcessId -eq $taskParent}) {
                $taskTree.Add($taskChild)
            }
        }
        foreach ($taskOwned in $taskTree) {
            if ($taskOwned.Name -eq 'conhost.exe' -and $taskOwned.ExecutablePath -eq (Join-Path $env:SystemRoot 'System32\conhost.exe')) { continue }
            if ($taskOwned.Name -ne 'python.exe' -or $taskOwned.CommandLine -notmatch '\s-m vpars_bot run\s*$') {
                throw 'Unexpected bot child process; restart cancelled.'
            }
        }
        foreach ($taskOwned in $taskTree) {
            Stop-Process -Id $taskOwned.ProcessId -ErrorAction SilentlyContinue
        }
    }
}
New-Item -ItemType Directory -Force -Path $taskArtifacts | Out-Null
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskBot = Start-Process -FilePath $taskPython -ArgumentList '-m', 'vpars_bot', 'run' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskArtifacts "bot-$taskStamp-stdout.log") -RedirectStandardError (Join-Path $taskArtifacts "bot-$taskStamp-stderr.log") -PassThru
[IO.File]::WriteAllText($taskPidFile, [string]$taskBot.Id)
if ($taskBot.WaitForExit(3000)) {
    Remove-Item -LiteralPath $taskPidFile
    throw "Bot exited during startup (code $($taskBot.ExitCode)); inspect the latest bot log in .artifacts."
}
Write-Output ('Bot process started: ' + $taskBot.Id)

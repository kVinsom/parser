param(
    [ValidateSet('start','restart','ensure','status','migrate','test')][string]$Mode = 'status',
    [ValidateRange(4,3600)][int]$Interval = 4,
    [ValidateRange(1,10)][int]$Pages = 3,
    [switch]$ReloadGateway
)
$ErrorActionPreference = 'Stop'
$taskPython = (Resolve-Path (Join-Path $PSScriptRoot '..\telegram-bot\.venv\Scripts\python.exe')).Path
$taskMain = (Resolve-Path (Join-Path $PSScriptRoot 'main.py')).Path
$taskArtifacts = Join-Path $PSScriptRoot '.artifacts'
New-Item -ItemType Directory -Force -Path $taskArtifacts | Out-Null
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Mode -in @('start', 'restart', 'ensure')) {
        $taskLaunchFile = Join-Path $taskArtifacts 'launch-settings.json'
        if ($Mode -eq 'ensure' -and (Test-Path -LiteralPath $taskLaunchFile)) {
            $taskSavedLaunch = Get-Content -LiteralPath $taskLaunchFile -Raw | ConvertFrom-Json
            $Interval = [int]$taskSavedLaunch.interval
            $Pages = [int]$taskSavedLaunch.pages
            if ($Interval -lt 4 -or $Interval -gt 3600 -or $Pages -lt 1 -or $Pages -gt 10) {
                throw 'Invalid saved worker launch settings.'
            }
        }
        $taskPidFile = Join-Path $taskArtifacts 'monitor.pid'
        if (Test-Path -LiteralPath $taskPidFile) {
            $taskStoredPid = [int](Get-Content -LiteralPath $taskPidFile)
            $taskExisting = Get-CimInstance Win32_Process -Filter "ProcessId=$taskStoredPid"
            if ($taskExisting -and $taskExisting.ExecutablePath -eq $taskPython -and
                $taskExisting.CommandLine.Contains($taskMain)) {
                if ($Mode -in @('start', 'ensure')) {
                    Write-Output 'Monitoring is already running; use restart to change the interval.'
                    exit 0
                }
                # Validate the entire owned tree before stopping anything. Never stop the bot.
                $taskAllProcesses = @(Get-CimInstance Win32_Process)
                $taskTree = [System.Collections.Generic.List[object]]::new()
                $taskTree.Add($taskExisting)
                for ($taskIndex = 0; $taskIndex -lt $taskTree.Count; $taskIndex++) {
                    $taskParentId = $taskTree[$taskIndex].ProcessId
                    $taskParentCreated = $taskTree[$taskIndex].CreationDate
                    if (-not $taskParentCreated) { throw 'Cannot verify process creation time; restart cancelled.' }
                    # Windows may reuse a dead parent's PID. An older process with
                    # the same parent ID is not a child of this worker instance.
                    foreach ($taskChild in $taskAllProcesses | Where-Object {
                        $_.ParentProcessId -eq $taskParentId -and
                        $_.CreationDate -ge $taskParentCreated
                    }) {
                        $taskTree.Add($taskChild)
                    }
                }
                $taskConsoleHost = Join-Path $env:SystemRoot 'System32\conhost.exe'
                foreach ($taskOwned in $taskTree) {
                    if ($taskOwned.Name -eq 'conhost.exe' -and $taskOwned.ExecutablePath -eq $taskConsoleHost) {
                        continue # Windows owns a console host for each verified Python child.
                    }
                    if ($taskOwned.Name -ne 'python.exe' -or -not $taskOwned.CommandLine -or
                        (-not $taskOwned.CommandLine.Contains($taskMain) -and
                         $taskOwned.CommandLine -notmatch '\s-m uvicorn vpars_parser\.api:create_app --factory --host 127\.0\.0\.1 --port 8002 --log-level warning --no-access-log$')) {
                        throw 'Unexpected child process; restart cancelled.'
                    }
                }
                # Stop the monitor before its parser so it cannot launch a replacement child.
                Stop-Process -Id $taskExisting.ProcessId -ErrorAction SilentlyContinue
                for ($taskIndex = 1; $taskIndex -lt $taskTree.Count; $taskIndex++) {
                    Stop-Process -Id $taskTree[$taskIndex].ProcessId -ErrorAction SilentlyContinue
                }
            } elseif ($taskExisting) {
                throw 'Stored PID belongs to another process; restart cancelled.'
            }
        }
        if ($Mode -ne 'ensure') {
        docker compose -f (Join-Path $PSScriptRoot '..\telegram-bot\compose.yaml') up -d --wait --wait-timeout 60
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL or Redis is not ready.' }
        $taskGatewayArgs = @('compose', '-f', (Join-Path $PSScriptRoot '..\access-gateway\bypass\compose.yaml'), 'up', '-d', '--build', '--wait', '--wait-timeout', '120')
        if ($ReloadGateway) { $taskGatewayArgs += '--force-recreate' }
        & docker @taskGatewayArgs
        if ($LASTEXITCODE -ne 0) { throw 'Vinted gateway is not ready.' }
        & (Join-Path $PSScriptRoot '..\telegram-bot\run.ps1') --migrate
        if ($LASTEXITCODE -ne 0) { throw 'Telegram schema migration failed.' }
        & $taskPython $taskMain migrate
        if ($LASTEXITCODE -ne 0) { throw 'Monitoring migration failed.' }
        }
        @{interval=$Interval; pages=$Pages} | ConvertTo-Json | Set-Content -LiteralPath $taskLaunchFile -Encoding utf8
        $taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        foreach ($taskLog in @('monitor-stdout.log','monitor-stderr.log')) {
            $taskLogPath = Join-Path $taskArtifacts $taskLog
            if (Test-Path -LiteralPath $taskLogPath) {
                Copy-Item -LiteralPath $taskLogPath -Destination (Join-Path $taskArtifacts "$taskStamp-$taskLog")
            }
        }
        $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @(('"' + $taskMain + '"'), 'run', '--interval', "$Interval", '--pages', "$Pages") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskArtifacts 'monitor-stdout.log') -RedirectStandardError (Join-Path $taskArtifacts 'monitor-stderr.log') -PassThru
        [IO.File]::WriteAllText($taskPidFile, [string]$taskProcess.Id)
        if ($taskProcess.WaitForExit(3000)) {
            Remove-Item -LiteralPath $taskPidFile
            throw "Monitoring exited during startup (code $($taskProcess.ExitCode)); inspect .artifacts/monitor-stderr.log."
        }
        Write-Output ('Monitoring process started: ' + $taskProcess.Id)
    } elseif ($Mode -eq 'test') {
        & $taskPython -m pytest -q tests
        exit $LASTEXITCODE
    } else {
        & $taskPython $taskMain $Mode
        exit $LASTEXITCODE
    }
} finally { Pop-Location }

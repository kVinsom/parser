param([switch]$SkipIntegration)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot
$taskBot = Join-Path $taskRoot 'services/telegram-bot/.venv/Scripts/python.exe'
$taskParser = Join-Path $taskRoot 'services/parser/.venv/Scripts/python.exe'
$taskGateway = Join-Path $taskRoot 'services/access-gateway/bypass'
$taskGatewayPython = Join-Path $taskGateway '.venv/Scripts/python.exe'
$taskGatewayFallback = -not (Test-Path -LiteralPath $taskGatewayPython)
if ($taskGatewayFallback) { $taskGatewayPython = $taskBot }
$taskReport = Join-Path $taskRoot ('.release-checks/' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $taskReport | Out-Null
$taskResults = [System.Collections.Generic.List[object]]::new()
$taskOldPythonPath = $env:PYTHONPATH
$taskOldBytecode = $env:PYTHONDONTWRITEBYTECODE

function Invoke-ReleaseCheck {
    param([string]$Name, [string]$Directory, [string]$Python, [string[]]$Arguments)
    Push-Location -LiteralPath $Directory
    try {
        if (-not (Test-Path -LiteralPath $Python)) { throw "Missing Python environment: $Python" }
        Write-Output "Checking $Name"
        & $Python @Arguments 2>&1 | Tee-Object -FilePath (Join-Path $taskReport "$Name.log")
        $taskCode = $LASTEXITCODE
        $taskResults.Add([pscustomobject]@{check=$Name; exit_code=$taskCode})
    } finally { Pop-Location }
}

try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    # Do not inherit unrelated packages from the caller's PYTHONPATH.
    $env:PYTHONPATH = $null
    Invoke-ReleaseCheck 'bot-dependencies' $taskRoot $taskBot @('-m','pip','check')
    Invoke-ReleaseCheck 'parser-dependencies' $taskRoot $taskParser @('-m','pip','check')
    foreach ($taskSuite in @('telegram-bot','worker','parser')) {
        $taskPython = if ($taskSuite -eq 'parser') { $taskParser } else { $taskBot }
        $taskArgs = @('-m','pytest','-q','-p','no:cacheprovider','--basetemp',
            (Join-Path $taskReport "$taskSuite-temp"),'--junitxml',
            (Join-Path $taskReport "$taskSuite.xml"))
        if ($taskSuite -eq 'telegram-bot' -and -not $SkipIntegration) { $taskArgs += '--integration' }
        if ($taskSuite -eq 'worker' -and $SkipIntegration) { continue }
        Invoke-ReleaseCheck $taskSuite (Join-Path $taskRoot "services/$taskSuite") $taskPython $taskArgs
    }
    # A clean gateway venv must not be shadowed by legacy vendored dependencies.
    $env:PYTHONPATH = if ($taskGatewayFallback) { Join-Path $taskGateway '.deps' } else { $null }
    Invoke-ReleaseCheck 'gateway-dependencies' $taskGateway $taskGatewayPython @('-m','pip','check')
    $taskTests = @(Get-ChildItem -LiteralPath $taskGateway -Filter 'test_*.py' -File | ForEach-Object FullName)
    Invoke-ReleaseCheck 'gateway' $taskGateway $taskGatewayPython (@('-m','pytest','-q','-p',
        'no:cacheprovider','--basetemp',(Join-Path $taskReport 'gateway-temp'),
        '--junitxml',(Join-Path $taskReport 'gateway.xml')) + $taskTests)
    $env:PYTHONPATH = $null
    Invoke-ReleaseCheck 'application-lint' $taskRoot $taskBot @('-m','ruff','check','--no-cache',
        'shared',
        'services/telegram-bot/vpars_bot','services/telegram-bot/tests','services/telegram-bot/migrations',
        'services/worker/vpars_worker','services/worker/tests','services/notifier/vpars_notifier')
    Invoke-ReleaseCheck 'parser-lint' $taskRoot $taskParser @('-m','ruff','check','--no-cache',
        'services/parser/vpars_parser','services/parser/tests','services/parser/tools')
    $taskGatewaySources = @(Get-ChildItem -LiteralPath $taskGateway -Filter '*.py' -File | ForEach-Object FullName)
    Invoke-ReleaseCheck 'gateway-lint' $taskRoot $taskBot (@('-m','ruff','check','--no-cache',
        '--select','E4,E7,E9,F,I,B,UP') + $taskGatewaySources)
    Invoke-ReleaseCheck 'release-tools' $taskRoot $taskBot @('-m','unittest','discover','-s','tests','-v')
} finally {
    $env:PYTHONPATH = $taskOldPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $taskOldBytecode
    [pscustomobject]@{integration=(-not $SkipIntegration); checks=@($taskResults.ToArray())} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskReport 'summary.json') -Encoding utf8
}
Write-Output "Reports: $taskReport"
if ($taskResults | Where-Object exit_code -ne 0) { exit 1 }
if ($SkipIntegration) { Write-Warning 'Partial check: PostgreSQL/Redis integration and worker suite were skipped.' }
exit 0

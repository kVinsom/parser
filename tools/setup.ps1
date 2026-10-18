param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot
& $Python -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Provide a Python 3.12+ executable with -Python.' }
foreach ($taskService in @('telegram-bot','parser','access-gateway/bypass')) {
    $taskDirectory = Join-Path $taskRoot "services/$taskService"
    $taskVenv = Join-Path $taskDirectory '.venv'
    $taskExecutable = Join-Path $taskVenv 'Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $taskExecutable)) {
        & $Python -m venv $taskVenv
        if ($LASTEXITCODE -ne 0) { throw "Cannot create environment for $taskService" }
    }
    Push-Location -LiteralPath $taskDirectory
    try {
        if ($taskService -eq 'telegram-bot') {
            & $taskExecutable -m pip install -e '../../shared' -e '.[test]' -c requirements.lock
        } else {
            & $taskExecutable -m pip install -r requirements-dev.txt
        }
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed for $taskService" }
        if ($taskService -eq 'parser') {
            & $taskExecutable tools/generate_proto.py
            if ($LASTEXITCODE -ne 0) { throw 'Parser protocol generation failed.' }
        }
        if ((Test-Path -LiteralPath '.env.example') -and -not (Test-Path -LiteralPath '.env')) {
            Copy-Item -LiteralPath '.env.example' -Destination '.env'
        }
    } finally { Pop-Location }
}
Write-Output 'Dependencies installed. Set the bot token and proxy settings in the local .env files.'

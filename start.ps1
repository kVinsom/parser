param(
    [ValidateRange(4,3600)][int]$Interval = 4,
    [ValidateRange(1,10)][int]$Pages = 3
)
$ErrorActionPreference = 'Stop'
$taskBotRoot = Join-Path $PSScriptRoot 'services/telegram-bot'
if (-not (Test-Path -LiteralPath (Join-Path $taskBotRoot '.env'))) {
    throw 'Create services/telegram-bot/.env from .env.example and set TELEGRAM_BOT_TOKEN first.'
}
# Worker launcher starts dependencies and applies both schemas before the receiver starts.
& (Join-Path $PSScriptRoot 'services/worker/run.ps1') -Mode restart -Interval $Interval -Pages $Pages
if ($LASTEXITCODE -ne 0) { throw 'Monitoring startup failed.' }
& (Join-Path $taskBotRoot 'restart.ps1')
Write-Output 'Local processes started. Check worker status and logs before opening access to users.'

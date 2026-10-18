# Open a dedicated, persistent browser for manual login/CAPTCHA and local Playwright CDP.
param(
    [Parameter(Mandatory=$true)][ValidateRange(1,[long]::MaxValue)][long]$UserId,
    [Parameter(Mandatory=$true)][ValidateSet('DE','FR','IT','UK')][string]$Region,
    [ValidateRange(1024,65535)][int]$Port = 9222,
    [ValidateSet('chrome','msedge')][string]$Browser = 'msedge'
)
$ErrorActionPreference = 'Stop'
$taskBotRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../services/telegram-bot'))
$taskPython = Join-Path $taskBotRoot '.venv/Scripts/python.exe'
$taskCandidates = if ($Browser -eq 'chrome') {
    @("$env:ProgramFiles/Google/Chrome/Application/chrome.exe", "${env:ProgramFiles(x86)}/Google/Chrome/Application/chrome.exe", "$env:LOCALAPPDATA/Google/Chrome/Application/chrome.exe")
} else {
    @("${env:ProgramFiles(x86)}/Microsoft/Edge/Application/msedge.exe", "$env:ProgramFiles/Microsoft/Edge/Application/msedge.exe", "$env:LOCALAPPDATA/Microsoft/Edge/Application/msedge.exe")
}
$taskExecutable = $taskCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $taskExecutable) { throw "Browser $Browser is not installed." }
# A separate CDP profile avoids racing an already running managed Playwright profile.
# Its identity is stable for this browser/user/region; Chrome must not open an Edge profile.
$taskProfile = Join-Path $taskBotRoot ".wallet-profiles/cdp-$Browser-$UserId-$Region"
$taskLegacyProfile = Join-Path $taskBotRoot ".wallet-profiles/cdp-$UserId-$Region"
# Preserve the earlier Edge launcher profile without copying or resetting it.
if ($Browser -eq 'msedge' -and (Test-Path -LiteralPath $taskLegacyProfile) -and -not (Test-Path -LiteralPath $taskProfile)) {
    $taskProfile = $taskLegacyProfile
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Reuse the matching browser or choose a different port."
}
New-Item -ItemType Directory -Force -Path $taskProfile | Out-Null
$taskArguments = @(
    "--user-data-dir=`"$taskProfile`"",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=$Port",
    '--no-first-run', '--no-default-browser-check', '--new-window', 'about:blank'
)
# This is the visible, interactive browser requested for manual sign-in and CAPTCHA.
Start-Process -FilePath $taskExecutable -ArgumentList $taskArguments -WindowStyle Normal | Out-Null
$taskReady = $false
for ($taskAttempt = 0; $taskAttempt -lt 20; $taskAttempt++) {
    try {
        $taskVersion = Invoke-RestMethod "http://127.0.0.1:$Port/json/version" -TimeoutSec 1
        if ($taskVersion.webSocketDebuggerUrl) { $taskReady = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 250
}
if (-not $taskReady) { throw 'The browser opened but its CDP endpoint is unavailable. Check the window and port.' }
Write-Output "Dedicated browser is ready at http://127.0.0.1:$Port. Keep it open."
Write-Output "Run the following command from $taskBotRoot to bind this browser and sign in:"
Write-Output "& `"$taskPython`" -m vpars_bot.wallet_setup login --user-id $UserId --region $Region --cdp-url http://127.0.0.1:$Port"

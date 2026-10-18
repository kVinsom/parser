# Start a temporary HTTPS endpoint for the owner-authenticated checkout app only.
# No system service, firewall change or public browser debugging endpoint is created.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskArtifacts = Join-Path $taskRoot '.release-checks'
$taskBinary = Join-Path $taskArtifacts 'cloudflared.exe'
$taskPidFile = Join-Path $taskArtifacts 'checkout-tunnel.pid'
if (Test-Path -LiteralPath $taskPidFile) {
    $taskExistingId = [int](Get-Content -LiteralPath $taskPidFile)
    $taskExisting = Get-CimInstance Win32_Process -Filter "ProcessId=$taskExistingId"
    if ($taskExisting) { throw 'A previous checkout tunnel process exists; inspect it before starting another.' }
}
if (-not (Test-Path -LiteralPath $taskBinary)) {
    New-Item -ItemType Directory -Force -Path $taskArtifacts | Out-Null
    $taskRelease = Invoke-RestMethod -Uri 'https://api.github.com/repos/cloudflare/cloudflared/releases/latest'
    $taskAsset = $taskRelease.assets | Where-Object name -eq 'cloudflared-windows-amd64.exe'
    if ($taskAsset.digest -notmatch '^sha256:([a-f0-9]{64})$') { throw 'Official SHA256 digest unavailable.' }
    $taskExpectedHash = $Matches[1]
    Invoke-WebRequest -UseBasicParsing -Uri $taskAsset.browser_download_url -OutFile $taskBinary
    if ((Get-FileHash -LiteralPath $taskBinary -Algorithm SHA256).Hash -ne $taskExpectedHash) {
        throw 'Official binary checksum mismatch.'
    }
}
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskLog = Join-Path $taskArtifacts "checkout-tunnel-$taskStamp.log"
$taskOut = Join-Path $taskArtifacts "checkout-tunnel-$taskStamp-out.log"
$taskTunnel = Start-Process -FilePath $taskBinary -ArgumentList 'tunnel', '--url', 'http://127.0.0.1:8086', '--no-autoupdate', '--protocol', 'http2' -WindowStyle Hidden -RedirectStandardError $taskLog -RedirectStandardOutput $taskOut -PassThru
[IO.File]::WriteAllText($taskPidFile, [string]$taskTunnel.Id)
$taskEndpoint = $null
for ($taskTry = 0; $taskTry -lt 25; $taskTry++) {
    if ($taskTunnel.HasExited) { throw 'Checkout tunnel exited; inspect its log.' }
    if (Test-Path -LiteralPath $taskLog) {
        # The child keeps its redirected log open on Windows. Get-Content allows
        # shared reads instead of conflicting with the writer's file handle.
        $taskText = Get-Content -LiteralPath $taskLog -Raw
        if ($taskText) {
            $taskMatch = [regex]::Match($taskText, 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($taskMatch.Success) { $taskEndpoint = $taskMatch.Value; break }
        }
    }
    Start-Sleep -Seconds 1
}
if (-not $taskEndpoint) { throw 'No HTTPS endpoint returned within 25 seconds; inspect the tunnel before retrying.' }
$taskEnv = Join-Path $taskRoot 'services\telegram-bot\.env'
$taskLines = @([IO.File]::ReadAllLines($taskEnv) | Where-Object { $_ -notmatch '^WALLET_HANDOFF_(URL|PORT)=' })
$taskLines += "WALLET_HANDOFF_URL=$taskEndpoint"
$taskLines += 'WALLET_HANDOFF_PORT=8086'
if (-not ($taskLines -match '^WALLET_CHECKOUT_MODE=')) { $taskLines += 'WALLET_CHECKOUT_MODE=manual' }
[IO.File]::WriteAllLines($taskEnv, $taskLines, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $taskArtifacts 'checkout-tunnel-url.txt'), $taskEndpoint)
Write-Output "Temporary checkout endpoint: $taskEndpoint"
Write-Output "Tunnel PID: $($taskTunnel.Id). Restart the bot to apply this URL."

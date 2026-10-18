# All generated files and optional dependencies stay beside this script.
$ErrorActionPreference = 'Stop'
$taskPython = $env:VINTED_PYTHON
if (-not $taskPython) {
    $taskCandidates = @(
        (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
    )
    $taskCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($taskCommand) { $taskCandidates += $taskCommand.Source }
    $taskPython = $taskCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $taskPython) {
    throw 'Python is not available. Install Python 3.12+ or set VINTED_PYTHON to its executable.'
}
$taskPreviousPath = $env:PYTHONPATH
$taskPreviousBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPATH = Join-Path $PSScriptRoot '.deps'
    if ($taskPreviousPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $taskPreviousPath }
    if ($args.Count -gt 0 -and $args[0] -eq '--test') {
        & $taskPython -m unittest discover -s $PSScriptRoot -p 'test_*.py' -v
    } elseif ($args.Count -gt 0 -and $args[0] -eq '--latency') {
        $taskLatencyArgs = @($args | Select-Object -Skip 1)
        & $taskPython (Join-Path $PSScriptRoot 'latency.py') @taskLatencyArgs
    } elseif ($args.Count -gt 0 -and $args[0] -eq '--grpc-server') {
        $taskGrpcArgs = @($args | Select-Object -Skip 1)
        & $taskPython (Join-Path $PSScriptRoot 'grpc_server.py') @taskGrpcArgs
    } elseif ($args.Count -gt 0 -and $args[0] -eq '--grpc') {
        $taskGrpcArgs = @($args | Select-Object -Skip 1)
        & $taskPython (Join-Path $PSScriptRoot 'grpc_client.py') @taskGrpcArgs
    } elseif ($args.Count -gt 0 -and $args[0] -eq '--generate-grpc') {
        & $taskPython -m grpc_tools.protoc "-I$PSScriptRoot\proto" "--python_out=$PSScriptRoot" "--grpc_python_out=$PSScriptRoot" (Join-Path $PSScriptRoot 'proto\vinted_rpc\access.proto')
    } elseif ($args.Count -gt 0 -and $args[0] -eq '--qa') {
        $taskQaArgs = @($args | Select-Object -Skip 1)
        & $taskPython (Join-Path $PSScriptRoot 'qa.py') @taskQaArgs
    } elseif ($args.Count -gt 0 -and $args[0] -eq '--vinted') {
        $taskVintedArgs = @($args | Select-Object -Skip 1)
        & $taskPython (Join-Path $PSScriptRoot 'vinted.py') @taskVintedArgs
    } else {
        & $taskPython (Join-Path $PSScriptRoot 'vinted.py') @args
    }
    $taskExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $taskPreviousPath
    $env:PYTHONDONTWRITEBYTECODE = $taskPreviousBytecode
}
exit $taskExitCode

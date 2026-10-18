$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Create services/parser/.venv and install requirements-dev.txt as described in README.md.'
}
$taskMode = if ($args.Count) { $args[0] } else { '--qa' }
$taskRest = @($args | Select-Object -Skip 1)
Push-Location -LiteralPath $PSScriptRoot
try {
    switch ($taskMode) {
        '--qa' { & $taskPython tools/qa.py @taskRest }
        '--benchmark' { & $taskPython tools/benchmark.py @taskRest }
        '--test' { & $taskPython -m pytest @taskRest }
        '--serve' { & $taskPython -m vpars_parser @taskRest }
        default { throw 'Use --qa, --benchmark, --test or --serve.' }
    }
    $taskResult = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $taskResult

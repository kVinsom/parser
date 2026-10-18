$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Create .venv and install dependencies as described in README.md.'
}
$taskMode = if ($args.Count) { $args[0] } else { '--test' }
$taskRest = @($args | Select-Object -Skip 1)
Push-Location -LiteralPath $PSScriptRoot
try {
    switch ($taskMode) {
        { $_ -in '--test', '--integration' } {
            $taskRun = Join-Path $PSScriptRoot ('.artifacts\qa-' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $taskRun | Out-Null
            [string[]]$taskFlags = @()
            if ($taskMode -eq '--integration') { $taskFlags += '--integration' }
            & $taskPython -m pytest -q --basetemp (Join-Path $taskRun 'temp') --junitxml (Join-Path $taskRun 'tests.xml') @taskFlags @taskRest
        }
        '--serve' { & $taskPython -m vpars_bot run @taskRest }
        '--migrate' { & $taskPython -m alembic upgrade head }
        default { throw 'Use --test, --integration, --serve or --migrate.' }
    }
    $taskResult = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $taskResult

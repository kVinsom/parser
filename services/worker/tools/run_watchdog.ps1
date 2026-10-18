$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot
$taskPython = (Resolve-Path (Join-Path $taskRoot '../telegram-bot/.venv/Scripts/python.exe')).Path
# Compatibility entry point for manual use. The scheduled task invokes
# pythonw.exe and the Python wrapper directly, without starting PowerShell.
$taskWrapper = (Resolve-Path (Join-Path $PSScriptRoot 'run_watchdog.py')).Path
& $taskPython $taskWrapper
exit $LASTEXITCODE

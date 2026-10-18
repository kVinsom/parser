param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot)).TrimEnd('\','/')
$taskPrefix = $taskRoot + [IO.Path]::DirectorySeparatorChar
$taskSkip = @('.git','.venv','.deps','.idea','.wallet-profiles','.wallet-contracts','.release-checks','dist')
$taskCacheNames = @('__pycache__','.pytest_cache','.ruff_cache')
$taskQueue = [System.Collections.Generic.Queue[string]]::new()
$taskTargets = [System.Collections.Generic.List[string]]::new()
$taskQueue.Enqueue($taskRoot)
while ($taskQueue.Count) {
    $taskDirectory = $taskQueue.Dequeue()
    foreach ($taskChild in Get-ChildItem -LiteralPath $taskDirectory -Directory -Force) {
        if ($taskChild.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        if ($taskChild.Name -in $taskCacheNames) {
            $taskResolved = [IO.Path]::GetFullPath($taskChild.FullName)
            if (-not $taskResolved.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing cleanup outside workspace: $taskResolved"
            }
            $taskTargets.Add($taskResolved)
        } elseif ($taskChild.Name -notin $taskSkip -and $taskChild.Name -ne '.artifacts') {
            $taskQueue.Enqueue($taskChild.FullName)
        }
    }
}
foreach ($taskTarget in $taskTargets) {
    Write-Output $taskTarget
    if ($Apply) { Remove-Item -LiteralPath $taskTarget -Recurse -Force }
}
Write-Output "Cache directories: $($taskTargets.Count). Apply: $Apply"

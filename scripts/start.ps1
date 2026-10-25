param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    Write-Host 'Окружение не установлено. Выполните setup.bat.'
    exit 1
}
Push-Location $projectRoot
try {
    $launchArguments = @('-m', 'cadnext.launcher', 'start')
    if ($NoBrowser) { $launchArguments += '--no-browser' }
    & $projectPython @launchArguments
    $result = $LASTEXITCODE
} finally { Pop-Location }
exit $result

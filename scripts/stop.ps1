$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    Write-Host 'Окружение .venv отсутствует. Процессы не изменены.'
    exit 1
}
Push-Location $projectRoot
try {
    & $projectPython -m cadnext.launcher stop
    $result = $LASTEXITCODE
} finally { Pop-Location }
exit $result

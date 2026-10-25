param([switch]$WithBrowser)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
try {
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue) -or
        -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw 'Установите Node.js 22.12 или новее (вместе с npm), затем повторите setup.'
    }
    & node.exe -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>22 || (a===22 && b>=12) ? 0 : 1)"
    if ($LASTEXITCODE -ne 0) { throw 'Нужен Node.js 22.12 или новее.' }
    if (-not (Test-Path -LiteralPath $projectPython)) {
        if (Get-Command py.exe -ErrorAction SilentlyContinue) {
            & py.exe -3.12 -m venv (Join-Path $projectRoot '.venv')
        } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
            & python.exe -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)"
            if ($LASTEXITCODE -ne 0) { throw 'Установите Python 3.12, затем повторите setup.' }
            & python.exe -m venv (Join-Path $projectRoot '.venv')
        } else { throw 'Установите Python 3.12, затем повторите setup.' }
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать .venv. Проверьте установку Python 3.12.' }
    }
    & $projectPython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'Окружение .venv должно использовать Python 3.12.' }
    & $projectPython -m pip install -r (Join-Path $projectRoot 'requirements.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить Python-зависимости.' }
    Push-Location (Join-Path $projectRoot 'ui')
    try {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости интерфейса.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать интерфейс.' }
        if ($WithBrowser) {
            & npx.cmd playwright install chromium
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить Chromium для проверок.' }
        }
    } finally { Pop-Location }
    Write-Host 'Установка завершена. Запустите start.bat.'
    exit 0
} catch {
    Write-Host ('Ошибка установки: ' + $_.Exception.Message)
    exit 1
}

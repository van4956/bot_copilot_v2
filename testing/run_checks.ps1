# Выполняет только локальные проверки; не запускает бота и Docker.
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $Root
$env:PYTHONIOENCODING = 'utf-8'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Сначала создайте .venv и установите requirements-test.txt.'
}
& $Python -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Нарушены зависимости.' }
& $Python -B -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Python-тесты не прошли.' }
& node --test tests/js/miniapps.test.js
if ($LASTEXITCODE -ne 0) { throw 'JavaScript-тесты не прошли.' }
& docker compose --env-file .env.test.local -f docker-compose.test.yml config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Некорректная тестовая конфигурация Docker.' }

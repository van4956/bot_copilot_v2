# Запускает только тестовый режим; останавливается клавишами Ctrl+C.
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $Root
$env:APP_ENV = 'test'
$env:APP_ENV_FILE = Join-Path $Root '.env.test.local'
$env:PYTHONIOENCODING = 'utf-8'
& (Join-Path $Root '.venv\Scripts\python.exe') -B app.py
if ($LASTEXITCODE -ne 0) { throw 'Тестовый бот завершился с ошибкой.' }

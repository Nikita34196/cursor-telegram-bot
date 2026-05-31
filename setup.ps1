# Быстрая настройка на Windows (запуск из PowerShell в папке бота)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Cursor Telegram Bot — setup ===" -ForegroundColor Cyan

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан .env — откройте и заполните BOT_TOKEN, CURSOR_API_KEY, DEFAULT_REPO_URL" -ForegroundColor Yellow
    notepad .env
    Read-Host "Нажмите Enter после сохранения .env"
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $pythonCmd = "py -3" } else {
        Write-Host "Установите Python 3.11+ с https://www.python.org/downloads/" -ForegroundColor Red
        exit 1
    }
} else {
    $pythonCmd = "python"
}

Write-Host "Установка зависимостей..." -ForegroundColor Green
Invoke-Expression "$pythonCmd -m pip install -r requirements.txt"

Write-Host "Проверка .env..." -ForegroundColor Green
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process')
    }
}

if (-not $env:BOT_TOKEN -or -not $env:CURSOR_API_KEY) {
    Write-Host "Заполните BOT_TOKEN и CURSOR_API_KEY в .env" -ForegroundColor Red
    exit 1
}

Write-Host "Запуск бота (Ctrl+C для остановки)..." -ForegroundColor Green
Invoke-Expression "$pythonCmd bot.py"

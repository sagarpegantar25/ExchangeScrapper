<#
.SYNOPSIS
  One-time setup: creates .venv, installs dependencies and the Chromium browser,
  and creates .env from the template if it does not exist.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#>
$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectDir

Write-Host "== Checking Python ==" -ForegroundColor Cyan
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $pyCmd = "py"; $pyArgs = @("-3") } else { $pyCmd = "python"; $pyArgs = @() }
$ver = & $pyCmd @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0) { throw "Python not found. Install Python 3.11+ from python.org first (see README)." }
Write-Host "Using Python $ver"
if ([version]$ver -lt [version]"3.11") { throw "Python $ver is too old - install 3.11 or newer (3.12 / 3.13 recommended)." }

if (-not (Test-Path ".venv")) {
    Write-Host "== Creating virtual environment (.venv) ==" -ForegroundColor Cyan
    & $pyCmd @pyArgs -m venv .venv
}

$venvPy = Join-Path $ProjectDir ".venv\Scripts\python.exe"
Write-Host "== Installing dependencies ==" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
Write-Host "== Installing Chromium for the browser fallback ==" -ForegroundColor Cyan
& $venvPy -m playwright install chromium

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - open it in Notepad and fill in your settings." -ForegroundColor Yellow
}

foreach ($d in "downloads","extracted","processed","logs") { New-Item -ItemType Directory -Force -Path $d | Out-Null }

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: notepad .env   then   .\.venv\Scripts\python.exe -m src.main --check"

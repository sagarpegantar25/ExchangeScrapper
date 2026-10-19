@echo off
REM ------------------------------------------------------------------
REM  One-time setup for ScraperAutomation (Command Prompt, no PowerShell).
REM  Run from anywhere:   scripts\setup.bat
REM ------------------------------------------------------------------
setlocal
cd /d "%~dp0.."
echo == Python ==
py --version || (echo Python not found - install it from python.org & exit /b 2)
if not exist ".venv\Scripts\python.exe" (
    echo == Creating .venv ==
    py -m venv .venv || exit /b 2
)
echo == Installing packages ==
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 2
if not exist ".env" (
    copy .env.example .env >nul
    echo Created .env from .env.example - fill it in with:  notepad .env
)
echo.
echo Setup complete. Next:  .venv\Scripts\python.exe -m src.main --exchange NSE --dry-run --date 17-09-2026

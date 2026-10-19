@echo off
REM ------------------------------------------------------------------
REM  Runs ScraperAutomation once (BSE + NSE, one email each). Used by Task Scheduler.
REM  Any extra arguments are passed through, e.g.:
REM     run_pipeline.bat --dry-run
REM     run_pipeline.bat --exchange NSE
REM ------------------------------------------------------------------
setlocal
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

set "LOG_DIR=D:\ScraperAutomationLogs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo [%date% %time%] ERROR: virtual environment not found in %CD%\.venv >> "%LOG_DIR%\task_scheduler.log"
    exit /b 2
)

echo [%date% %time%] Task started >> "%LOG_DIR%\task_scheduler.log"
".venv\Scripts\python.exe" -m src.main %* >> "%LOG_DIR%\task_scheduler.log" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] Task finished with exit code %RC% >> "%LOG_DIR%\task_scheduler.log"
exit /b %RC%

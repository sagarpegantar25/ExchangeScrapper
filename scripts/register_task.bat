@echo off
REM ------------------------------------------------------------------
REM  Registers the daily 08:00 task "ScraperAutomation" (BSE + NSE, one email each).
REM  Run in a Command Prompt opened AS ADMINISTRATOR. Asks for your Windows password
REM  once, so the task also runs when you are logged off.
REM     scripts\register_task.bat            (08:00)
REM     scripts\register_task.bat 07:30      (another time)
REM ------------------------------------------------------------------
setlocal
set "TIME_AT=%~1"
if "%TIME_AT%"=="" set "TIME_AT=08:00"
set "BAT=%~dp0run_pipeline.bat"
schtasks /Create /F /TN "ScraperAutomation" /TR "\"%BAT%\"" /SC DAILY /ST %TIME_AT% /RU "%USERDOMAIN%\%USERNAME%" /RP *
if errorlevel 1 exit /b 1
echo.
echo Task "ScraperAutomation" runs daily at %TIME_AT%.
echo Test it now:   schtasks /Run /TN "ScraperAutomation"
echo Last result:   schtasks /Query /TN "ScraperAutomation" /V /FO LIST ^| findstr /C:"Last Result"
REM Remove the older tasks so nothing is emailed twice (errors just mean they did not exist)
schtasks /Delete /TN "ExchangeScrapper" /F >nul 2>&1
schtasks /Delete /TN "BSE KID Scraper" /F >nul 2>&1

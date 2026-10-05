<#
.SYNOPSIS
  Registers (or updates) the daily Windows Task Scheduler job for bse-kid-scraper.

.DESCRIPTION
  * Runs scripts\run_pipeline.bat every day at -Time (default 08:00, local PC time).
  * "Run whether user is logged on or not": you will be asked for your Windows
    password once; Windows stores it securely for the task.
  * Starts the run as soon as possible if the PC was off/asleep at 08:00.
  * Wakes the PC from sleep to run (if allowed by your power plan).
  * Retries 3 times, 10 minutes apart, if the task fails to start.
  * Stops a run that takes longer than 3 hours.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\register_task.ps1
  powershell -ExecutionPolicy Bypass -File .\scripts\register_task.ps1 -Time 07:30
  powershell -ExecutionPolicy Bypass -File .\scripts\register_task.ps1 -Unregister
#>
param(
    [string]$Time = "08:00",
    [string]$TaskName = "BSE KID Scraper",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bat = Join-Path $ProjectDir "scripts\run_pipeline.bat"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path (Join-Path $ProjectDir ".venv\Scripts\python.exe"))) {
    throw "Virtual environment not found. Run the setup steps in README.md first."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Registering '$TaskName' to run daily at $Time as $user"
Write-Host "Project folder: $ProjectDir"
$cred = Get-Credential -UserName $user -Message "Windows password for $user (needed so the task runs when you are logged off)"
$plain = $cred.GetNetworkCredential().Password

$action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Bat`"" -WorkingDirectory $ProjectDir
$trigger  = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RunOnlyIfNetworkAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -User $user -Password $plain -RunLevel Limited `
    -Description "Daily BSE private placement KID -> Claude -> email pipeline ($ProjectDir)" `
    -Force | Out-Null

Write-Host ""
Write-Host "Done. Task '$TaskName' will run every day at $Time." -ForegroundColor Green
Write-Host "Test it now with:   Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check result with:  Get-ScheduledTaskInfo -TaskName '$TaskName'"

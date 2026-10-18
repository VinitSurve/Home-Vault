# Uninstall HomeVault Windows Receiver Background Task

param(
    [string]$TaskName = "HomeVault-Receiver"
)

$ErrorActionPreference = "Stop"

Write-Host "=== HomeVault Windows Scheduled Task Uninstaller ===" -ForegroundColor Cyan
Write-Host "Checking scheduled task '$TaskName'..." -ForegroundColor Cyan

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Task) {
    if ($Task.State -eq "Running") {
        Write-Host "Stopping running task '$TaskName'..." -ForegroundColor Yellow
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Start-Sleep -Seconds 1
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null
    Write-Host "Task '$TaskName' successfully removed." -ForegroundColor Green
} else {
    Write-Host "Task '$TaskName' is not registered. Nothing to do." -ForegroundColor Yellow
}

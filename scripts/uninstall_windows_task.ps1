# Uninstall HomeVault Windows Receiver Background Task

param(
    [string]$TaskName = "HomeVault-Receiver"
)

$ErrorActionPreference = "Stop"

Write-Host "Unregistering scheduled task '$TaskName'..." -ForegroundColor Cyan

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null
    Write-Host "Task '$TaskName' successfully removed." -ForegroundColor Green
} catch {
    Write-Warning "Could not remove task '$TaskName'. It may not exist."
}

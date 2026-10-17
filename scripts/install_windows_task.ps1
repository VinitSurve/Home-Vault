# Install HomeVault Windows Receiver Background Task
# Registers a Windows Scheduled Task that launches the agent at user logon.

param(
    [string]$TaskName = "HomeVault-Receiver",
    [string]$PythonExe = "",
    [string]$InboxPath = "D:\HomeVault\Inbox"
)

$ErrorActionPreference = "Stop"

Write-Host "=== HomeVault Windows Scheduled Task Installer ===" -ForegroundColor Cyan

# 1. Resolve Python executable
if (-not $PythonExe) {
    # Check if inside a virtual environment
    $VenvPython = Join-Path $PSScriptRoot "..\apps\windows-agent\.venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = (Resolve-Path $VenvPython).Path
    } else {
        $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
}

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found. Please specify -PythonExe path or activate your virtual environment."
    exit 1
}

Write-Host "Using Python executable: $PythonExe" -ForegroundColor Green

# 2. Resolve working directory (HomeVault repo root)
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Write-Host "Repository root: $RepoRoot" -ForegroundColor Green

# 3. Ensure Inbox directory exists
if (-not (Test-Path $InboxPath)) {
    Write-Host "Creating Inbox directory: $InboxPath" -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $InboxPath | Out-Null
}

# 4. Define scheduled task action
# Pass PYTHONPATH to ensure packages/protocol and apps/windows-agent are discoverable
$Arguments = "-c `"import sys; sys.path.extend(['$RepoRoot/packages/protocol/src', '$RepoRoot/apps/windows-agent/src']); from homevault_agent.service import main; sys.exit(main())`""
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $RepoRoot

# 5. Define scheduled task trigger: At User Logon
$Trigger = New-ScheduledTaskTrigger -AtLogOn

# 6. Define settings: Restart on failure, don't stop on battery
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -Priority 7

# 7. Register the Task
Write-Host "Registering task '$TaskName'..." -ForegroundColor Cyan
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "HomeVault Windows Receiver Agent — background cloud queue receiver" `
    -Force | Out-Null

Write-Host "Task '$TaskName' successfully registered!" -ForegroundColor Green
Write-Host ""
Write-Host "To test starting the task immediately:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To check task status:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'"

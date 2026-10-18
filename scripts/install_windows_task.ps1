# Install HomeVault Windows Receiver Background Task
# Registers a Windows Scheduled Task that launches the agent at user logon.

param(
    [string]$TaskName = "HomeVault-Receiver",
    [string]$PythonExe = "",
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"

Write-Host "=== HomeVault Windows Scheduled Task Installer ===" -ForegroundColor Cyan

# 1. Resolve working directory (HomeVault repo root)
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
if (-not (Test-Path $RepoRoot)) {
    Write-Error "Repository root not found at '$RepoRoot'."
    exit 1
}
Write-Host "Repository root: $RepoRoot" -ForegroundColor Green

# 2. Resolve Python executable
if (-not $PythonExe) {
    # Check primary repository virtual environment first (.venv)
    $CandidatePaths = @(
        (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
        (Join-Path $RepoRoot "apps\windows-agent\.venv\Scripts\python.exe")
    )
    if ($env:VIRTUAL_ENV) {
        $CandidatePaths = @((Join-Path $env:VIRTUAL_ENV "Scripts\python.exe")) + $CandidatePaths
    }

    foreach ($Candidate in $CandidatePaths) {
        if (Test-Path $Candidate) {
            $PythonExe = (Resolve-Path $Candidate).Path
            break
        }
    }

    if (-not $PythonExe) {
        $Cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($Cmd) {
            $PythonExe = $Cmd.Source
        }
    }
}

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found. Expected virtual environment at '$RepoRoot\.venv\Scripts\python.exe' or specify -PythonExe."
    exit 1
}

Write-Host "Using Python executable: $PythonExe" -ForegroundColor Green

# 3. Define scheduled task action
# Executes 'python.exe -m homevault_agent.service' from repository root.
# Continuous polling loop starts automatically (worker.run_forever) without --once.
# Configuration and Inbox path are read from ~/.homevault/config.toml.
$Arguments = "-m homevault_agent.service"
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument $Arguments -WorkingDirectory $RepoRoot

# 4. Define scheduled task trigger: At User Logon
$Trigger = New-ScheduledTaskTrigger -AtLogOn

# 5. Define settings: Prevent duplicates, restart on failure, indefinite runtime
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -Priority 7

# 6. Register the Task
Write-Host "Registering task '$TaskName'..." -ForegroundColor Cyan
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "HomeVault Windows Receiver Agent - background cloud queue receiver" `
    -Force | Out-Null

Write-Host "Task '$TaskName' successfully registered!" -ForegroundColor Green
Write-Host ""
Write-Host "To test starting the task immediately:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To check task status:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'"

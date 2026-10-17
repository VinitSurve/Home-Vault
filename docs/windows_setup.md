# HomeVault — Windows Receiver Setup & Operations Guide

This guide details how to set up, secure, and run the **HomeVault Receiver Agent** on your Windows PC.

---

## 1. System Requirements

- **OS**: Windows 10 or Windows 11 (64-bit)
- **Python**: Python 3.12 or newer (recommended from [python.org](https://www.python.org/) with "Add python.exe to PATH" checked)
- **Storage**: Sufficient local disk space on your target drive (e.g. `D:\HomeVault\Inbox`)
- **Network**: Internet access (no incoming ports, VPN, or static IP required)

---

## 2. Directory & Configuration Setup

### 2.1 Create HomeVault Directories
Open PowerShell as your standard user (no Administrator privileges required):

```powershell
# Create config directory
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.homevault"

# Create Inbox destination (e.g. on D: drive)
New-Item -ItemType Directory -Force -Path "D:\HomeVault\Inbox"
```

### 2.2 Place Google Cloud OAuth Client Secret
Copy `credentials.json` (downloaded from Google Cloud Console) into:
```
%USERPROFILE%\.homevault\credentials.json
```

```powershell
Copy-Item "C:\Users\YourUsername\Downloads\credentials.json" "$env:USERPROFILE\.homevault\credentials.json"
```

### 2.3 Create `config.toml` (Optional)
Place custom configuration in `%USERPROFILE%\.homevault\config.toml`:

```toml
[receiver]
inbox_path = "D:\\HomeVault\\Inbox"
db_path = "C:\\Users\\YourUsername\\.homevault\\receiver.db"
cleanup_delete_cloud_files = true

[transfer]
poll_interval_seconds = 60
max_retries = 5
```

---

## 3. Secure Key Provisioning (Windows Credential Manager)

> [!IMPORTANT]
> The encryption key is a 256-bit AES key. It must match the key generated on your Mac.
> HomeVault never stores this key in plaintext files or logs. It is kept in **Windows Credential Manager**.

### Provision the key via Python `keyring`:
Open PowerShell:

```powershell
python -c "import keyring, getpass; key = getpass.getpass('Paste HomeVault encryption key: '); keyring.set_password('HomeVault', 'encryption-key', key.strip())"
```
*(Typing with `getpass` ensures the key is hidden from PowerShell command history and terminal output).*

To verify the key exists in Windows Credential Manager without exposing it:
```powershell
python -c "import keyring; print('Key present:', bool(keyring.get_password('HomeVault', 'encryption-key')))"
```

---

## 4. Authorize Google Drive via OAuth 2.0

Run the interactive OAuth setup CLI:

```powershell
cd path\to\HomeVault
$env:PYTHONPATH = "packages\protocol\src;apps\windows-agent\src"
python -m homevault_agent.oauth
```

1. Your default web browser will open to Google's sign-in page.
2. Sign in with your Google account.
3. Grant access to HomeVault's private application files on Drive (`drive.file`).
4. The token is automatically saved into **Windows Credential Manager** under the service name `HomeVault-OAuth`.

To verify authorization status:
```powershell
python -m homevault_agent.oauth --status
```

---

## 5. Running the Receiver Agent

### 5.1 One-Shot Test Run
To test discovery and delivery for a single pass:
```powershell
python -m homevault_agent.service --once
```

### 5.2 Dry-Run Mode
To discover, download, decrypt, and verify pending transfers *without* committing files to the Inbox or issuing Drive receipts:
```powershell
python -m homevault_agent.service --dry-run --once
```

### 5.3 Continuous Background Polling (Manual Console)
```powershell
python -m homevault_agent.service --interval 60
```
Press `Ctrl+C` to trigger a graceful shutdown.

---

## 6. Auto-Start at Windows Logon (Task Scheduler)

To run HomeVault automatically in the background whenever you log into Windows:

1. Open PowerShell.
2. Run the provided registration script:
   ```powershell
   .\scripts\install_windows_task.ps1 -InboxPath "D:\HomeVault\Inbox"
   ```

### Task Management Commands:
- **Start immediately**:
  ```powershell
  Start-ScheduledTask -TaskName "HomeVault-Receiver"
  ```
- **Check status**:
  ```powershell
  Get-ScheduledTask -TaskName "HomeVault-Receiver"
  ```
- **Stop task**:
  ```powershell
  Stop-ScheduledTask -TaskName "HomeVault-Receiver"
  ```
- **Uninstall task**:
  ```powershell
  .\scripts\uninstall_windows_task.ps1
  ```

---

## 7. Security & Failure Recovery

| Scenario | Behavior |
|---|---|
| **PC powered off during Mac staging** | Staged file remains encrypted on Google Drive. The Windows receiver automatically discovers and downloads it when the PC boots and logs in. |
| **PC power loss or crash mid-download** | Temporary `.part` and staging files are cleaned up. On restart, the local SQLite database identifies the interrupted transfer and resumes/retries with bounded exponential backoff. |
| **Tampered ciphertext on Drive** | AES-256-GCM authentication fails immediately during decryption (`IntegrityError`). The corrupt file is destroyed from staging and never committed to the Inbox. |
| **Duplicate filenames** | Filename collisions are resolved safely via non-colliding suffixes (`file (1).ext`) without overwriting existing data. |
| **Insufficient disk space** | Disk usage is checked before downloading. If free space is below `2 * file_size + 20 MB`, the transfer is paused with an actionable alert rather than crashing the receiver. |
| **Drive staging cleanup** | Staged encrypted files are purged from Google Drive *only after* the completion receipt is verified and durable. |

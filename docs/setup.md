# HomeVault Setup Guide

## Prerequisites

### Both machines

- Python 3.12 or newer
- `pip` package manager
- Internet access (not necessarily the same network)
- A Google account (the same account on both machines)

### macOS (Sender)

- macOS 13 Ventura or newer
- Finder and Shortcuts app
- Homebrew (recommended for Python installation)

### Windows (Receiver)

- Windows 10 or newer
- A destination drive with sufficient space (e.g., `D:\HomeVault\Inbox`)
- Administrator access (for Task Scheduler setup)

---

## Step 1: Google Cloud Console Setup

> [!IMPORTANT]
> This is a one-time manual setup that must be done before HomeVault can connect to Google Drive.

### 1.1 Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Name it `HomeVault` (or any name you prefer)
4. Click **Create**

### 1.2 Enable Google Drive API

1. In the Cloud Console, go to **APIs & Services** → **Library**
2. Search for **Google Drive API**
3. Click **Enable**

### 1.3 Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Select **External** user type (unless you have Google Workspace)
3. Fill in:
   - App name: `HomeVault`
   - User support email: your email
   - Developer contact: your email
4. Click **Save and Continue**
5. Add scope: `https://www.googleapis.com/auth/drive.file`
6. Click **Save and Continue**
7. Add your Google email as a test user
8. Click **Save and Continue**

> [!NOTE]
> While the app is in "Testing" status, only test users you add can authorize it.
> This is fine for personal use — you never need to publish it.

### 1.4 Create OAuth Client ID

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: `HomeVault Desktop`
5. Click **Create**
6. **Download** the JSON file
7. Rename it to `credentials.json`

> [!CAUTION]
> This file contains your OAuth client ID and secret. **Never commit it to Git.**
> You'll need to copy this same file to both your Mac and Windows PC.

### 1.5 Place credentials.json

- **Mac**: Place at `~/.homevault/credentials.json`
- **Windows**: Place at `%USERPROFILE%\.homevault\credentials.json`

Create the `.homevault` directory if it doesn't exist:

```bash
# macOS
mkdir -p ~/.homevault
cp ~/Downloads/credentials.json ~/.homevault/credentials.json

# Windows (PowerShell)
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.homevault"
Copy-Item "C:\Users\YourName\Downloads\credentials.json" "$env:USERPROFILE\.homevault\credentials.json"
```

---

## Step 2: Generate and Provision Encryption Key

> [!IMPORTANT]
> This key encrypts all files before they're uploaded to Google Drive. Both machines
> must have the **same** key. If this key is lost, staged files on Drive are unrecoverable.

### 2.1 Generate the key (on either machine)

```bash
python3 -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

This prints a 44-character base64 string. **Copy it carefully.**

### 2.2 Store the key securely

**macOS:**
```bash
security add-generic-password -s "HomeVault" -a "encryption-key" -w "YOUR_KEY_HERE" -U
```

**Windows (PowerShell — after installing HomeVault dependencies):**
```powershell
python -c "import keyring; keyring.set_password('HomeVault', 'encryption-key', 'YOUR_KEY_HERE')"
```

### 2.3 Back up the key

Store a copy in your password manager (1Password, Bitwarden, etc.) or on an encrypted USB drive. See [docs/security.md](security.md#key-recovery) for recovery procedures.

---

## Step 3: Install HomeVault

### 3.1 Clone the repository (both machines)

```bash
git clone <your-repo-url> HomeVault
cd HomeVault
```

### 3.2 Install Python dependencies

**macOS:**
```bash
cd apps/mac-sender
python3 -m venv .venv
source .venv/bin/activate
pip install -e "../../packages/protocol"
pip install -e "."
```

**Windows:**
```powershell
cd apps\windows-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e "..\..\packages\protocol"
pip install -e "."
```

---

## Step 4: First-time Authorization

### 4.1 Authorize on macOS

```bash
cd apps/mac-sender
source .venv/bin/activate
python -m homevault_sender.oauth
```

This opens a browser window. Sign in with your Google account and grant Drive access. The token is stored in your macOS Keychain — no files are created.

### 4.2 Authorize on Windows

```powershell
cd apps\windows-agent
.\.venv\Scripts\Activate.ps1
python -m homevault_agent.oauth
```

Same process — sign in and grant access. Token stored in Windows Credential Manager.

---

## Step 5: Configure Windows Inbox

Edit `~/.homevault/config.toml` on Windows (created on first run):

```toml
[receiver]
inbox_path = "D:\\HomeVault\\Inbox"
```

Create the directory:
```powershell
New-Item -ItemType Directory -Force -Path "D:\HomeVault\Inbox"
```

---

## Step 6: Set Up macOS CLI and Background Worker

### 6.1 Create the CLI executable wrapper

Run the setup script from the repository root:

```bash
./scripts/setup_mac_cli.sh
```

This generates an executable at `~/.homevault/bin/homevault`. To use `homevault` from any terminal session, add it to your shell configuration (`~/.zshrc`):

```bash
echo 'export PATH="$HOME/.homevault/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify it works:
```bash
homevault --help
```

### 6.2 Set up macOS background worker (launchd)

Install the launchd User Agent to start the HomeVault worker automatically at login:

```bash
./scripts/install_mac_launchd.sh
```

To verify the agent is active:
```bash
launchctl list | grep homevault
tail -f ~/.homevault/logs/worker.log
```

To stop and remove the agent at any time:
```bash
./scripts/uninstall_mac_launchd.sh
```

---

## Step 7: Configure Windows Agent (Receiver)

### 7.1 Manual test run

```powershell
cd apps\windows-agent
.\.venv\Scripts\Activate.ps1
python -m homevault_agent.service --once
```

### 7.2 Install Task Scheduler background task

To run continuously at Windows logon:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

See [docs/windows_setup.md](windows_setup.md) for full Windows operational details.

---

## Step 8: Install Finder Quick Action (Apple Shortcuts)

HomeVault integrates seamlessly with macOS Finder via an Apple Shortcuts Quick Action:

1. Open **Shortcuts** (`/System/Applications/Shortcuts.app`).
2. Click **+** (New Shortcut) and name it **`Send to HomeVault`**.
3. In the Inspector pane `(i)` on the right:
   - Check **Use as Quick Action**.
   - Ensure **Services Menu** → **Finder** is checked.
   - Set **Shortcut Receives**: `Files` from `Quick Actions`.
4. Add action: **Choose from Menu**:
   - Prompt: `Choose transfer mode:`
   - Menu items: `Copy to Windows`, `Move to Windows`.
5. Under `Copy to Windows`, add **Run Shell Script**:
   - Shell: `/bin/bash`
   - Pass Input: **as arguments**
   - Command: `~/.homevault/bin/homevault send --mode copy "$@"`
6. Under `Move to Windows`, add **Run Shell Script**:
   - Shell: `/bin/bash`
   - Pass Input: **as arguments**
   - Command: `~/.homevault/bin/homevault send --mode move "$@"`
7. At the bottom of the shortcut, add **Show Notification**:
   - Title: `HomeVault`
   - Message: `Files queued for delivery to Windows PC.`
8. Test by right-clicking any test file in Finder → **Quick Actions** → **Send to HomeVault**.

See [integrations/macos/README.md](../integrations/macos/README.md) for screenshots and troubleshooting.

---

## Manual actions checklist

- [ ] Create Google Cloud project
- [ ] Enable Google Drive API
- [ ] Configure OAuth consent screen
- [ ] Create OAuth Desktop client ID
- [ ] Download `credentials.json` to both machines
- [ ] Generate encryption key
- [ ] Store encryption key on both machines (OS secure storage)
- [ ] Back up encryption key in password manager
- [ ] Install Python 3.12+ on both machines
- [ ] Clone repo and install dependencies on both machines
- [ ] Authorize OAuth on macOS (`python -m homevault_sender.oauth`)
- [ ] Authorize OAuth on Windows (`python -m homevault_agent.oauth`)
- [ ] Create Windows Inbox directory
- [ ] Run `scripts/setup_mac_cli.sh` and add `~/.homevault/bin` to PATH
- [ ] Install macOS launchd agent (`scripts/install_mac_launchd.sh`)
- [ ] Install Windows Task Scheduler task (`scripts/install_windows_task.ps1`)
- [ ] Create and enable Finder Quick Action in Apple Shortcuts


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

## Step 6: Start the Services

### macOS — Background worker

```bash
# Manual start
cd apps/mac-sender && source .venv/bin/activate
python -m homevault_sender.worker

# Auto-start at login (launchd) — see scripts/install_mac_launchd.sh
```

### Windows — Background agent

```powershell
# Manual start
cd apps\windows-agent && .\.venv\Scripts\Activate.ps1
python -m homevault_agent.service

# Auto-start at login (Task Scheduler) — see scripts/install_windows_task.ps1
```

---

## Step 7: Install Finder Quick Action (macOS)

See [integrations/macos/README.md](../integrations/macos/README.md) for Finder integration setup.

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
- [ ] Authorize OAuth on macOS
- [ ] Authorize OAuth on Windows
- [ ] Create Windows Inbox directory
- [ ] Install Finder Quick Action on macOS
- [ ] Set up auto-start on both machines

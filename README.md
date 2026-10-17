# HomeVault — Personal Cloud-Queued Storage System

**Transfer files from macOS to Windows via Google Drive, with encryption, integrity verification, and offline support.**

HomeVault lets you right-click any file in macOS Finder, select **Send to HomeVault** (Copy or Move), and have it delivered to your Windows PC's inbox automatically — even if the PC is off, or you're on a different network.

## How it works

```
Mac (Finder) → Queue → Encrypt → Upload to Google Drive → Windows Agent → Decrypt → Verify → Inbox
```

- **No VPN, no port forwarding, no direct connections.** Google Drive is the async transport.
- **Offline-safe.** Files queue locally if offline; the PC picks them up whenever it comes online.
- **End-to-end encrypted.** Files are encrypted before upload; Google never sees plaintext.
- **Integrity-verified.** SHA-256 hashes ensure nothing is corrupted or tampered with.
- **Move is safe.** Originals are only deleted after the PC confirms receipt with a verified receipt.

## Repository structure

```
HomeVault/
├── apps/mac-sender/        # macOS sender daemon + CLI
├── apps/windows-agent/     # Windows receiver agent
├── packages/protocol/      # Shared models, state machine, crypto, hashing
├── integrations/macos/     # Finder Quick Action, Apple Shortcuts
├── scripts/                # Setup and utility scripts
├── docs/                   # Architecture, security, setup guides
├── tests/                  # Integration tests
└── .github/workflows/      # CI (testing only, not transfer pipeline)
```

## Quick start

See [docs/setup.md](docs/setup.md) for detailed setup instructions.

### macOS Sender CLI

The macOS sender provides the `homevault` command-line interface for submitting files, inspecting queue status, and running the background worker daemon:

```bash
# Queue a file for Copy (keeps original file on Mac)
homevault copy ~/Documents/Report.pdf

# Queue a file for Move (deletes original ONLY after verified Windows delivery)
homevault move ~/Downloads/Archive.zip

# Send multiple files at once
homevault send --mode move ~/Pictures/*.png

# Check the local queue status
homevault status

# Retry a failed transfer or cancel a pending job
homevault retry <transfer-id>
homevault cancel <transfer-id>

# Run worker manually (single iteration or daemon)
homevault worker --once
homevault worker --interval 10
```

### macOS Finder Quick Action

Right-click any file in macOS Finder, choose **Quick Actions** → **Send to HomeVault**, select **Copy to Windows** or **Move to Windows**, and the file is queued immediately with macOS notifications. See [integrations/macos/README.md](integrations/macos/README.md) for the setup recipe.

### macOS Background Daemon (`launchd`)

HomeVault includes a user agent that automatically starts at login and manages background uploads and receipts:

```bash
# Install and start launchd agent
./scripts/install_mac_launchd.sh

# Stop and uninstall launchd agent
./scripts/uninstall_mac_launchd.sh

# View worker logs
tail -f ~/.homevault/logs/worker.log
```

## Documentation

- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Setup Guide](docs/setup.md)
- [Windows Receiver Guide](docs/windows_setup.md)
- [macOS Finder Integration](integrations/macos/README.md)

## Status

- Phase 0: Project planning and architecture specifications (Completed)
- Phase 1: Shared protocol foundation, models, SQLite WAL, and crypto (Completed)
- Phase 2: Google Drive v3 staging, resumable transfers, and discovery (Completed)
- Phase 3: Windows receiver agent, atomic commit, and Task Scheduler (Completed)
- Phase 4: macOS sender CLI, persistent queue, launchd daemon, and Finder Quick Action (Completed)
- Phase 5: End-to-end integration testing and operational polish (Next)

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

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

## Documentation

- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Setup Guide](docs/setup.md)

## Status

🚧 **In development** — See [CHANGELOG.md](CHANGELOG.md) for progress.

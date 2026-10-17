# Changelog

All notable changes to HomeVault will be documented in this file.

## [Unreleased]

### Phase 0 — Project Initialization
- Created repository skeleton and directory structure.
- Wrote architecture, security, and setup documentation.
- Identified manual setup steps and technical risks.

### Phase 1 — Transfer Foundation
- Shared transfer models and state machine.
- SQLite persistence with migrations and WAL mode.
- File hashing (SHA-256).
- Safe filename and destination handling.
- Unit tests for state transitions and edge cases (115 passing tests).

### Phase 2 — Google Drive Integration
- OAuth 2.0 authorization with `drive.file` scope and token persistence in OS keyring (macOS Keychain / Windows Credential Manager).
- Automatic discovery and management of private HomeVault Drive folders (`Staging`, `Metadata`, `Receipts`) with in-memory ID caching.
- Resumable chunked upload and download with bounded retries and exponential backoff for transient errors.
- Pre-upload storage quota validation to guard against cloud space exhaustion.
- macOS sender `StagingManager` with AES-256-GCM client-side encryption, Drive staging, idempotency, and safe Move finalization.
- Windows agent `CloudDiscoveryManager` with pending transfer discovery, deduplication against receipts, decryption, SHA-256 validation, and receipt emission.
- Comprehensive test suite (146 passed, 1 skipped live integration test).

### Phase 3 — Windows Receiver Agent
- Background `ReceiverWorker` with startup discovery, continuous polling at configurable intervals, and graceful signal handling (Ctrl+C, SIGTERM, SIGBREAK).
- Same-volume atomic commit (`.homevault_staging` to Inbox) preventing cross-filesystem link errors.
- Pre-download disk space checks with safety buffer to avoid out-of-space crashes.
- Durable SQLite state tracking with restart recovery: detects locally completed jobs to idempotently reissue missing cloud receipts and prune staging blobs.
- Collision safety: automatic deduplication (`(1)`, `(2)`) without silent file overwrites.
- Path traversal prevention and non-executable file permissions.
- CLI service entry point (`homevault_agent.service`) with `--once`, `--dry-run`, `--interval`, and `--inbox` flags.
- Windows Task Scheduler integration scripts (`install_windows_task.ps1` and `uninstall_windows_task.ps1`) for logon auto-start.
- Complete Windows operations and security guide (`docs/windows_setup.md`).
- Extended test suite (160 passed, 1 skipped live integration test).

### Phase 4 — macOS Sender and Finder Quick Action
- Built complete `homevault` CLI supporting `send`, `copy`, `move`, `status`, `cancel`, `retry`, and `worker` daemon.
- Created durable `SenderQueue` backed by SQLite with restart recovery, tracking queued, pending upload, and completed states.
- Implemented `SenderWorker` daemon for background staging, exponential backoff retries, completion receipt polling, and cloud blob cleanup.
- Safe non-destructive Move semantics: verifies `CompletionReceipt` against metadata, checks source file presence, and re-computes `sha256(source)` prior to unlinking original file; flags `NEEDS_ATTENTION` and retains file if source changed or receipt mismatched.
- Native macOS Notification Center integration via AppleScript (`osascript`) notifying users on queueing, staging completion, delivery confirmation, and safety alerts.
- Configured macOS `launchd` User Agent (`com.vinit.homevault.sender.plist`) for automatic startup at login, with safe installation (`scripts/install_mac_launchd.sh`) and removal (`scripts/uninstall_mac_launchd.sh`) scripts.
- Provided Apple Shortcuts Finder Quick Action recipe and bridge script (`integrations/macos/send_to_homevault.sh`) for instant right-click context menu submission without blocking Finder.
- Expanded monorepo test suite with comprehensive unit and integration tests (196 passed, 1 skipped live integration test).

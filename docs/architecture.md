# HomeVault Architecture

## Overview

HomeVault is a personal file-transfer system that uses Google Drive as an asynchronous
message queue and temporary staging area. It connects a macOS sender to a Windows receiver
without requiring direct network connectivity, VPNs, or port forwarding.

## System diagram

```
┌─────────────────────────────┐        ┌──────────────────────┐        ┌──────────────────────────────┐
│       macOS Sender          │        │     Google Drive      │        │      Windows Agent           │
│                             │        │                       │        │                              │
│  Finder Quick Action        │        │  HomeVault/            │        │  Background service          │
│       ↓                     │        │  ├── Staging/         │        │       ↓                      │
│  Local SQLite Queue         │        │  │   └── {transfer_id}│        │  Poll for pending transfers  │
│       ↓                     │        │  ├── Metadata/        │        │       ↓                      │
│  Background Worker          │        │  │   └── {transfer_id}│        │  Download encrypted file     │
│       ↓                     │   ──►  │  │       .json        │  ──►   │       ↓                      │
│  Encrypt + Hash             │        │  └── Receipts/        │        │  Decrypt + Verify hash       │
│       ↓                     │        │      └── {transfer_id}│        │       ↓                      │
│  Upload to Drive            │        │          .json        │        │  Write to Inbox              │
│       ↓                     │        │                       │        │       ↓                      │
│  Monitor for receipt (Move) │        └──────────────────────┘        │  Upload completion receipt   │
│       ↓                     │                                        │                              │
│  Safe-delete original (Move)│                                        │  Local SQLite state          │
└─────────────────────────────┘                                        └──────────────────────────────┘
```

## Component boundaries

### `packages/protocol/` — Shared transfer protocol

Platform-independent code shared by both sender and receiver:

| Module | Responsibility |
|---|---|
| `models.py` | Transfer job dataclass, transfer modes (Copy/Move), state enum |
| `state_machine.py` | Valid state transitions, transition validation |
| `hashing.py` | SHA-256 streaming file hashing |
| `crypto.py` | AES-256-GCM authenticated encryption/decryption (streaming + chunked) |
| `metadata.py` | Transfer metadata and receipt JSON schemas |
| `filenames.py` | Safe filename sanitization, deduplication, path-traversal prevention |
| `database.py` | SQLite helpers, migration framework |
| `config.py` | Shared configuration model |
| `drive/auth.py` | OAuth 2.0 flow, token storage in OS keyring (macOS Keychain / Windows Credential Manager) |
| `drive/folders.py` | HomeVault application folder hierarchy (Staging, Metadata, Receipts) |
| `drive/client.py` | Google Drive v3 client: storage quota checks, resumable upload/download with exponential backoff |

### `apps/mac-sender/` — macOS sender

| Module | Responsibility |
|---|---|
| `staging.py` | Cloud staging manager: quota validation, AES-256-GCM encryption, Drive upload, safe Move finalization |
| `oauth.py` | macOS interactive OAuth CLI helper |
| `queue.py` | Enqueue new transfer jobs from Finder (Phase 4) |
| `worker.py` | Background daemon: process upload queue (Phase 4) |
| `notifications.py` | macOS notification center integration (Phase 4) |
| `cli.py` | Command-line interface for manual operations (Phase 4) |

### `apps/windows-agent/` — Windows receiver

| Module | Responsibility |
|---|---|
| `discovery.py` | Cloud discovery manager: pending job discovery, download, decryption, integrity validation, receipt issuance |
| `oauth.py` | Windows interactive OAuth CLI helper |
| `poller.py` | Continuous background polling loop (Phase 3) |
| `service.py` | Windows startup integration (Task Scheduler) (Phase 3) |

### `integrations/macos/` — Finder integration

| Component | Responsibility |
|---|---|
| Quick Action | Finder right-click → "Send to HomeVault" |
| Apple Shortcut | Copy/Move selection dialog, invokes CLI |

## Transfer lifecycle

```
                    ┌──────────────┐
                    │ pending_upload│  ← Job created on Mac
                    └──────┬───────┘
                           │ start upload
                    ┌──────▼───────┐
                    │  uploading   │  ← Resumable upload in progress
                    └──────┬───────┘
                           │ upload complete + metadata written
                    ┌──────▼───────┐
                    │   queued     │  ← Staged on Drive, awaiting PC
                    └──────┬───────┘
                           │ PC discovers and starts download
                    ┌──────▼───────┐
                    │ downloading  │  ← PC downloading from Drive
                    └──────┬───────┘
                           │ download complete
                    ┌──────▼───────┐
                    │  verifying   │  ← Hash check + decrypt
                    └──────┬───────┘
                           │ verified OK
                    ┌──────▼───────┐
                    │  completed   │  ← File in Inbox, receipt uploaded
                    └──────────────┘

            Error paths:
                    ┌──────────────┐
                    │   failed     │  ← Permanent failure after retries
                    └──────────────┘
                    ┌──────────────┐
                    │  cancelled   │  ← User-cancelled transfer
                    └──────────────┘
                    ┌──────────────┐
                    │needs_attention│ ← Requires human review (e.g., Move
                    └──────────────┘   source changed after receipt)
```

## State ownership

- **Mac owns** states: `pending_upload`, `uploading`, `queued`
- **PC owns** states: `downloading`, `verifying`, `completed`
- **Either side** can set: `failed`, `cancelled`, `needs_attention`
- State transitions are communicated via Google Drive metadata files and receipts.

## Transfer ID format

Each transfer gets a UUID v4 identifier, e.g., `a1b2c3d4-e5f6-7890-abcd-ef1234567890`.

This ID is used as:
- The SQLite primary key on both sides.
- The Google Drive file/metadata name prefix.
- The receipt filename.

## Google Drive folder structure

```
HomeVault/                    ← App-managed root (created via API)
├── Staging/                  ← Encrypted file blobs
│   └── {transfer_id}.enc    ← AES-256-GCM encrypted file
├── Metadata/                 ← Transfer job descriptors
│   └── {transfer_id}.json   ← Original filename, size, hash, mode, timestamp
└── Receipts/                 ← Completion confirmations
    └── {transfer_id}.json   ← Receiver's verified completion receipt
```

## Idempotency guarantees

- Upload: If interrupted, the sender checks for existing staged files by transfer ID before re-uploading.
- Download: If interrupted, the receiver checks for partial downloads and resumes or restarts.
- Receipt: If the receiver crashes after writing a file but before uploading a receipt, it detects the existing file on restart and uploads the receipt.
- Move deletion: If the sender crashes after validating a receipt but before deleting the original, it re-validates and re-attempts on restart.

## Scalability considerations

This is a personal system designed for a single user. The design prioritizes:
- Correctness over throughput.
- Simplicity over distributed-systems complexity.
- Safety (never lose data) over speed.

Google Drive API quotas (typically 12,000 requests/user/100 seconds) are more than sufficient for personal use. The system uses exponential backoff for all API calls.

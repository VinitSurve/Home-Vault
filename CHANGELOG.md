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

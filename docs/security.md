# HomeVault Security Design

## Threat model

HomeVault stores encrypted files temporarily on Google Drive. The threat model considers:

| Threat | Mitigation |
|---|---|
| Google inspects file contents | Client-side AES-256-GCM encryption before upload |
| Google account compromised | Encrypted blobs are useless without the key |
| Transit interception | TLS for Google API calls + client-side encryption |
| Credential theft from disk | OS-native secure storage (Keychain / Credential Manager) |
| Path traversal on receiver | Strict filename sanitization and inbox-only writes |
| Replay/duplicate attacks | Unique transfer IDs, idempotent operations |
| Source file tampering (Move) | Hash comparison before deletion |
| Accidental data loss | Never delete until receipt is verified |

### What this system does NOT protect against

- **Compromised local machine**: If the Mac or PC is fully compromised, an attacker has access to decrypted files, the encryption key, and credentials. This is inherent to any local-encryption scheme.
- **Key loss**: If the encryption key is lost and no backup exists, all staged files on Google Drive become unrecoverable. See [Key recovery](#key-recovery).
- **Google account lockout**: If you lose access to your Google account, pending transfers on Drive are inaccessible. Local files and the local database are unaffected.
- **Metadata leakage**: Filenames, sizes, and timestamps are stored in plaintext metadata files on Drive. Only file *contents* are encrypted. A future enhancement could encrypt metadata too.

## Authentication — OAuth 2.0

### Scopes

HomeVault requests the `https://www.googleapis.com/auth/drive.file` scope.

**What `drive.file` provides**: Access only to files created or opened by the app. HomeVault cannot see, modify, or delete your other Google Drive files.

**Limitation**: The receiver must use the same Google Cloud project (same OAuth client) as the sender, because `drive.file` scope means each app can only see files it created. Since both sender and receiver use the same OAuth client ID, files created by the sender are accessible to the receiver under the same project.

**Important**: Both Mac and PC must authorize the same Google account.

### Token storage

| Platform | Storage |
|---|---|
| macOS | macOS Keychain via `keyring` library |
| Windows | Windows Credential Manager via `keyring` library |

OAuth tokens are never stored in plaintext files, environment variables, or Git.

### Token refresh

The `google-auth` library handles automatic token refresh using the stored refresh token. If a refresh token is revoked or expires, the user is prompted to re-authorize via browser.

## Encryption

### Algorithm

- **AES-256-GCM** (Galois/Counter Mode)
- Authenticated encryption — provides confidentiality, integrity, and authenticity.
- 256-bit key, 96-bit random nonce per file.
- Uses Python's `cryptography` library (which wraps OpenSSL).

### File encryption format

```
[12-byte nonce][16-byte GCM auth tag][encrypted data...]
```

- Nonce: Generated with `os.urandom(12)` — cryptographically secure random.
- Auth tag: Appended by GCM mode automatically.
- Encrypted data: The original file contents.

For files larger than available RAM, encryption is performed in streaming chunks using `cryptography`'s `CipherContext` with incremental `update()` calls.

### Key management

#### Key generation

```bash
python -c "import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Generates a 256-bit key encoded as URL-safe base64.

#### Key provisioning

The encryption key must be the **same** on both the Mac and the PC. It is provisioned manually:

1. Generate the key once on either machine.
2. Transfer it to the other machine via a trusted channel:
   - USB drive
   - Secure messaging app (Signal, etc.)
   - Read it aloud / type it manually
   - AirDrop (if both machines are nearby)
3. Store it in OS secure storage on both machines:
   - **Mac**: `security add-generic-password -s "HomeVault" -a "encryption-key" -w "<key>"`
   - **Windows**: Stored via `keyring.set_password("HomeVault", "encryption-key", "<key>")`

**The key is never stored in files, environment variables, `.env` files, or Git.**

#### Key recovery

> [!CAUTION]
> If the encryption key is lost and no backup exists, all encrypted files currently staged
> on Google Drive become **permanently unrecoverable**. Local (unencrypted) copies on the
> Mac or PC are unaffected.

**Recovery strategy:**

1. **Keep a secure backup** of the key in a password manager (1Password, Bitwarden, etc.)
   or on an encrypted USB drive stored securely.
2. If the key is lost but both machines still have it in their OS secure storage, extract it
   from either machine.
3. If both machines lose the key:
   - Any files already delivered to the PC Inbox are fine (they're decrypted).
   - Any files still staged on Drive are unrecoverable.
   - Generate a new key, provision it to both machines, and resume operations.
   - Old pending transfers (if any) must be re-submitted from the Mac.

#### Key rotation

To rotate the encryption key:

1. Ensure all pending transfers are completed (no files staged on Drive).
2. Generate a new key.
3. Provision it to both machines.
4. Old completed transfers are unaffected (they're already decrypted on the PC).

**Do not rotate the key while transfers are pending** — the receiver won't be able to
decrypt files encrypted with the old key.

## File integrity

### SHA-256 hashing

Every file is hashed with SHA-256 before encryption:

1. **Sender** computes `sha256(original_file)` and stores it in transfer metadata.
2. **Receiver** downloads the encrypted file, decrypts it, computes `sha256(decrypted_file)`.
3. If hashes match → file is intact. If not → transfer is marked `failed`.

Hashing is performed in streaming 8 KB chunks to avoid loading large files into RAM.

### Move safety

For Move operations, strict non-destructive safety checks guarantee that the original file is never deleted prematurely or incorrectly:

1. **Receipt verification**: The sender validates the `CompletionReceipt` against the local transfer metadata:
   - `transfer_id` must match.
   - `sha256_hash` must match the original plaintext hash.
   - `file_size` must match the original byte size.
2. **Source existence check**: Verifies the source file still exists at the expected path.
3. **Pre-deletion re-hashing**: Before deleting the original, the sender recomputes `sha256(original_file)` from disk in real time.
4. **Tamper / modification protection**: If the file was edited, modified, or replaced between submission and Windows completion, the original is **retained**, the transfer transitions to `NEEDS_ATTENTION`, and a macOS alert is issued.
5. **Atomic deletion**: The original local file is only deleted (`os.unlink`) after all validations pass, followed by purging the staged encrypted payload from Google Drive.

## CLI and Shell Security

- **No secrets in command line**: Neither the encryption key nor OAuth credentials are ever passed via command-line arguments or environment flags in daily use.
- **Shell history protection**: Because the CLI only accepts file paths and mode flags (`homevault send --mode copy <path>`), no secrets are ever exposed to `.zsh_history`, `ps`, or process monitors.
- **Keychain integration**: The background worker securely retrieves credentials directly from macOS Keychain via `keyring` / `security`.
- **Log sanitization**: Worker logs (`~/.homevault/logs/worker.log`) only record transfer UUIDs, filenames, byte sizes, and operational statuses; never ciphertext, keys, or bearer tokens.

## Filename safety

The receiver sanitizes all filenames before writing to disk:

- Strips path separators (`/`, `\`)
- Strips null bytes
- Rejects or replaces Windows-reserved names (`CON`, `PRN`, `NUL`, etc.)
- Limits filename length to 200 characters
- Replaces unsafe characters
- Handles duplicate filenames by appending `(1)`, `(2)`, etc.
- Resolves the final path and verifies it's within the Inbox directory (anti-traversal)

## Google Drive safety

- Files are uploaded as **private** (no sharing links created).
- The `drive.file` scope prevents HomeVault from accessing your other Drive files.
- Staged files are cleaned up only after verified receipt.
- A configurable retention policy controls how long completed transfer artifacts remain on Drive.

## Credential files excluded from Git

The `.gitignore` explicitly excludes:
- `credentials.json`, `client_secret*.json` (OAuth client config)
- `token.json`, `token.pickle` (OAuth tokens)
- `*.key`, `*.pem`, `*.p12` (encryption keys)
- `.env`, `.env.*` (environment variables)
- `*.db` (SQLite databases with transfer metadata)

"""SQLite persistence for transfer jobs.

Provides a migration framework and CRUD operations for TransferJob records.
The database file is stored in ~/.homevault/ by default and is excluded
from Git via .gitignore.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from homevault_protocol.models import TransferJob, TransferMode, TransferState

# Schema version — increment when adding migrations.
_CURRENT_VERSION = 1

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS transfers (
        transfer_id     TEXT PRIMARY KEY,
        mode            TEXT NOT NULL CHECK(mode IN ('copy', 'move')),
        state           TEXT NOT NULL,
        source_path     TEXT,
        filename        TEXT NOT NULL,
        file_size       INTEGER NOT NULL DEFAULT 0,
        sha256_hash     TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        drive_file_id   TEXT,
        drive_metadata_id TEXT,
        drive_receipt_id TEXT,
        dest_path       TEXT,
        retry_count     INTEGER NOT NULL DEFAULT 0,
        max_retries     INTEGER NOT NULL DEFAULT 5,
        error_message   TEXT,
        upload_session_uri TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_transfers_state
        ON transfers(state);

    CREATE INDEX IF NOT EXISTS idx_transfers_created
        ON transfers(created_at);

    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );

    INSERT OR REPLACE INTO schema_version (version) VALUES (1);
    """,
}


class TransferDatabase:
    """SQLite-backed storage for transfer jobs.

    Usage::

        db = TransferDatabase("~/.homevault/transfers.db")
        db.migrate()
        db.insert(job)
        job = db.get("some-uuid")
        db.update(job)

    The database uses WAL mode for better concurrent read performance and
    enforces foreign key constraints.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,  # autocommit by default
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for an explicit transaction."""
        conn = self.conn
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    def migrate(self) -> None:
        """Apply any pending schema migrations."""
        current = self._get_version()
        if current >= _CURRENT_VERSION:
            return

        for version in range(current + 1, _CURRENT_VERSION + 1):
            if version in _MIGRATIONS:
                self.conn.executescript(_MIGRATIONS[version])

    def _get_version(self) -> int:
        """Return the current schema version, or 0 if uninitialized."""
        try:
            row = self.conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            return row["version"] if row else 0
        except sqlite3.OperationalError:
            return 0

    def insert(self, job: TransferJob) -> None:
        """Insert a new transfer job. Raises if the transfer_id already exists."""
        d = job.to_dict()
        columns = ", ".join(d.keys())
        placeholders = ", ".join(f":{k}" for k in d.keys())
        self.conn.execute(
            f"INSERT INTO transfers ({columns}) VALUES ({placeholders})", d
        )

    def update(self, job: TransferJob) -> None:
        """Update an existing transfer job by transfer_id."""
        d = job.to_dict()
        sets = ", ".join(f"{k} = :{k}" for k in d if k != "transfer_id")
        d_with_id = {**d}
        self.conn.execute(
            f"UPDATE transfers SET {sets} WHERE transfer_id = :transfer_id",
            d_with_id,
        )

    def get(self, transfer_id: str) -> Optional[TransferJob]:
        """Fetch a single job by transfer_id, or None if not found."""
        row = self.conn.execute(
            "SELECT * FROM transfers WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def get_by_state(self, state: TransferState) -> list[TransferJob]:
        """Return all jobs in the given state, ordered by creation time."""
        rows = self.conn.execute(
            "SELECT * FROM transfers WHERE state = ? ORDER BY created_at ASC",
            (state.value,),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_actionable(self) -> list[TransferJob]:
        """Return all jobs that need automatic processing."""
        actionable_states = [
            s.value
            for s in TransferState
            if s
            in (
                TransferState.PENDING_UPLOAD,
                TransferState.UPLOADING,
                TransferState.QUEUED,
                TransferState.DOWNLOADING,
                TransferState.VERIFYING,
            )
        ]
        placeholders = ", ".join("?" for _ in actionable_states)
        rows = self.conn.execute(
            f"SELECT * FROM transfers WHERE state IN ({placeholders}) "
            f"ORDER BY created_at ASC",
            actionable_states,
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get_all(self) -> list[TransferJob]:
        """Return all jobs, ordered by creation time."""
        rows = self.conn.execute(
            "SELECT * FROM transfers ORDER BY created_at ASC"
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def exists(self, transfer_id: str) -> bool:
        """Check if a transfer_id exists in the database."""
        row = self.conn.execute(
            "SELECT 1 FROM transfers WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()
        return row is not None

    def delete(self, transfer_id: str) -> bool:
        """Delete a job by transfer_id. Returns True if a row was deleted."""
        cursor = self.conn.execute(
            "DELETE FROM transfers WHERE transfer_id = ?",
            (transfer_id,),
        )
        return cursor.rowcount > 0

    def count_by_state(self) -> dict[str, int]:
        """Return a dict of state → count for all states with jobs."""
        rows = self.conn.execute(
            "SELECT state, COUNT(*) as cnt FROM transfers GROUP BY state"
        ).fetchall()
        return {row["state"]: row["cnt"] for row in rows}

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> TransferJob:
        """Convert a sqlite3.Row to a TransferJob."""
        return TransferJob.from_dict(dict(row))

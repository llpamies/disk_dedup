"""SQLite-backed catalog that persists dedup state across drive sessions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    source_root TEXT NOT NULL,
    scan_started_at TEXT,
    scan_completed_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id INTEGER NOT NULL REFERENCES drives(id),
    rel_path TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    sha256 TEXT,
    status TEXT NOT NULL,
    dest_path TEXT,
    duplicate_of_id INTEGER REFERENCES files(id),
    error TEXT,
    UNIQUE(drive_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_rel_path ON files(rel_path);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

-- Directories skipped without being walked (whole-subtree exclusion rules,
-- or symlink/junction safety). Their contents are never individually
-- recorded in `files`, so this is what makes them visible in reports.
CREATE TABLE IF NOT EXISTS pruned_dirs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id INTEGER NOT NULL REFERENCES drives(id),
    rel_path TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(drive_id, rel_path)
);
"""

# Terminal statuses are never re-processed on resume unless size/mtime changed.
TERMINAL_STATUSES = {"excluded_system", "unique", "duplicate", "error"}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def get_or_create_drive(conn: sqlite3.Connection, label: str, source_root: str) -> int:
    row = conn.execute("SELECT id, source_root FROM drives WHERE label = ?", (label,)).fetchone()
    if row is not None:
        if row["source_root"] != source_root:
            raise ValueError(
                f"Drive label {label!r} was previously used for {row['source_root']!r}, "
                f"not {source_root!r}. Use a different --label or double check the path."
            )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO drives (label, source_root) VALUES (?, ?)", (label, source_root)
    )
    conn.commit()
    return cur.lastrowid


def mark_scan_started(conn: sqlite3.Connection, drive_id: int) -> None:
    conn.execute(
        "UPDATE drives SET scan_started_at = COALESCE(scan_started_at, datetime('now')) WHERE id = ?",
        (drive_id,),
    )
    conn.commit()


def mark_scan_completed(conn: sqlite3.Connection, drive_id: int) -> None:
    conn.execute(
        "UPDATE drives SET scan_completed_at = datetime('now') WHERE id = ?", (drive_id,)
    )
    conn.commit()


def upsert_scanned_file(
    conn: sqlite3.Connection,
    drive_id: int,
    rel_path: str,
    size: Optional[int],
    mtime: Optional[float],
    excluded: bool,
) -> None:
    """Record a file seen during the scan phase.

    Resumability: if a row already exists in a terminal state with the same
    size/mtime, it is left untouched. Otherwise it is (re)written as
    excluded_system or pending.
    """
    existing = conn.execute(
        "SELECT status, size, mtime FROM files WHERE drive_id = ? AND rel_path = ?",
        (drive_id, rel_path),
    ).fetchone()
    if (
        existing is not None
        and existing["status"] in TERMINAL_STATUSES
        and existing["size"] == size
        and existing["mtime"] == mtime
    ):
        return
    status = "excluded_system" if excluded else "pending"
    conn.execute(
        """
        INSERT INTO files (drive_id, rel_path, size, mtime, status)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(drive_id, rel_path) DO UPDATE SET
            size = excluded.size,
            mtime = excluded.mtime,
            status = excluded.status,
            sha256 = NULL,
            dest_path = NULL,
            duplicate_of_id = NULL,
            error = NULL
        """,
        (drive_id, rel_path, size, mtime, status),
    )


def get_pending_files(conn: sqlite3.Connection, drive_id: int) -> Iterator[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM files WHERE drive_id = ? AND status IN ('pending', 'hashing') ORDER BY rel_path",
        (drive_id,),
    )
    yield from cur


def mark_hashing(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute("UPDATE files SET status = 'hashing' WHERE id = ?", (file_id,))
    conn.commit()


def mark_error(conn: sqlite3.Connection, file_id: int, error: str) -> None:
    conn.execute("UPDATE files SET status = 'error', error = ? WHERE id = ?", (error, file_id))
    conn.commit()


def find_canonical_by_hash(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM files WHERE sha256 = ? AND status = 'unique'", (sha256,)
    ).fetchone()


def mark_duplicate(conn: sqlite3.Connection, file_id: int, sha256: str, duplicate_of_id: int) -> None:
    conn.execute(
        "UPDATE files SET status = 'duplicate', sha256 = ?, duplicate_of_id = ? WHERE id = ?",
        (sha256, duplicate_of_id, file_id),
    )
    conn.commit()


def mark_unique(conn: sqlite3.Connection, file_id: int, sha256: str, dest_path: str) -> None:
    conn.execute(
        "UPDATE files SET status = 'unique', sha256 = ?, dest_path = ? WHERE id = ?",
        (sha256, dest_path, file_id),
    )
    conn.commit()


def update_dest_path(conn: sqlite3.Connection, file_id: int, dest_path: str) -> None:
    conn.execute("UPDATE files SET dest_path = ? WHERE id = ?", (dest_path, file_id))
    conn.commit()


def update_mtime(conn: sqlite3.Connection, file_id: int, mtime: float) -> None:
    conn.execute("UPDATE files SET mtime = ? WHERE id = ?", (mtime, file_id))
    conn.commit()


def get_unique_versions_for_path(conn: sqlite3.Connection, rel_path: str) -> list[sqlite3.Row]:
    """All currently-kept unique files that originated from this same rel_path."""
    return conn.execute(
        "SELECT * FROM files WHERE rel_path = ? AND status = 'unique' ORDER BY mtime",
        (rel_path,),
    ).fetchall()


def iter_possible_versions(conn: sqlite3.Connection, drive_id: Optional[int] = None) -> Iterator[sqlite3.Row]:
    """rel_paths that ended up with more than one distinct hash kept as unique."""
    query = """
        SELECT f.*, d.label AS drive_label
        FROM files f
        JOIN drives d ON d.id = f.drive_id
        WHERE f.status = 'unique' AND f.rel_path IN (
            SELECT rel_path FROM files
            WHERE status = 'unique'
            GROUP BY rel_path
            HAVING COUNT(DISTINCT sha256) > 1
        )
    """
    params: tuple = ()
    if drive_id is not None:
        query += " AND f.drive_id = ?"
        params = (drive_id,)
    query += " ORDER BY f.rel_path, f.mtime"
    yield from conn.execute(query, params)


def iter_duplicates(conn: sqlite3.Connection, drive_id: Optional[int] = None) -> Iterator[sqlite3.Row]:
    """Files skipped as duplicates, with the drive they came from and the
    canonical (kept) copy they matched."""
    query = """
        SELECT
            f.rel_path AS rel_path,
            f.sha256 AS sha256,
            f.mtime AS mtime,
            d.label AS drive_label,
            d.source_root AS source_root,
            c.dest_path AS canonical_dest_path,
            cd.label AS canonical_drive_label
        FROM files f
        JOIN drives d ON d.id = f.drive_id
        LEFT JOIN files c ON c.id = f.duplicate_of_id
        LEFT JOIN drives cd ON cd.id = c.drive_id
        WHERE f.status = 'duplicate'
    """
    params: tuple = ()
    if drive_id is not None:
        query += " AND f.drive_id = ?"
        params = (drive_id,)
    query += " ORDER BY d.label, f.rel_path"
    yield from conn.execute(query, params)


def summary_counts(conn: sqlite3.Connection, drive_id: Optional[int] = None) -> dict:
    query = "SELECT status, COUNT(*) AS n, COALESCE(SUM(size), 0) AS total_size FROM files"
    params: tuple = ()
    if drive_id is not None:
        query += " WHERE drive_id = ?"
        params = (drive_id,)
    query += " GROUP BY status"
    return {row["status"]: {"count": row["n"], "size": row["total_size"]} for row in conn.execute(query, params)}


def record_pruned_dir(conn: sqlite3.Connection, drive_id: int, rel_path: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pruned_dirs (drive_id, rel_path, reason) VALUES (?, ?, ?)",
        (drive_id, rel_path, reason),
    )


def list_pruned_dirs(conn: sqlite3.Connection, drive_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pruned_dirs WHERE drive_id = ? ORDER BY rel_path", (drive_id,)
    ).fetchall()


def get_drive_id(conn: sqlite3.Connection, label: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM drives WHERE label = ?", (label,)).fetchone()
    return row["id"] if row else None


def list_drives(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM drives ORDER BY id").fetchall()

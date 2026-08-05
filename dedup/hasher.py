"""Copy phase: hash pending files, dedupe by content, copy unique files into
the merged destination tree with _v1/_v2 collision suffixing."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

from . import catalog
from .progress import Progress

CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _versioned_name(rel_path: str, n: int) -> str:
    p = PurePosixPath(rel_path)
    return str(p.with_name(f"{p.stem}_v{n}{p.suffix}"))


def resolve_dest_path(conn: sqlite3.Connection, dest_root: Path, rel_path: str) -> str:
    """Decide where a newly-unique file should land, renaming an existing
    plain-named file to _v1 on first collision so numbering stays consistent."""
    versions = catalog.get_unique_versions_for_path(conn, rel_path)
    if not versions:
        return rel_path

    plain_row = next((v for v in versions if v["dest_path"] == rel_path), None)
    if plain_row is not None and len(versions) == 1:
        v1_name = _versioned_name(rel_path, 1)
        old_abs = dest_root / plain_row["dest_path"]
        new_abs = dest_root / v1_name
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_abs, new_abs)
        catalog.update_dest_path(conn, plain_row["id"], v1_name)
        return _versioned_name(rel_path, 2)

    return _versioned_name(rel_path, len(versions) + 1)


def copy_and_verify(source_path: Path, dest_abs: Path, expected_sha256: str) -> None:
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_abs.with_name(dest_abs.name + ".part")
    with open(source_path, "rb") as src, open(tmp_path, "wb") as dst:
        shutil.copyfileobj(src, dst, length=CHUNK_SIZE)
    try:
        shutil.copystat(source_path, tmp_path)
    except OSError:
        pass  # metadata preservation is best-effort, never fatal

    actual = sha256_file(tmp_path)
    if actual != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise OSError(f"copy verification failed for {source_path} (hash mismatch after copy)")
    os.replace(tmp_path, dest_abs)


def copy_drive(
    conn: sqlite3.Connection,
    drive_id: int,
    source_root: Path,
    dest_root: Path,
    show_progress: bool = True,
) -> None:
    pending = list(catalog.get_pending_files(conn, drive_id))
    total_bytes = sum(row["size"] or 0 for row in pending)
    progress = Progress("Copy", total=len(pending), total_bytes=total_bytes, enabled=show_progress)

    for row in pending:
        file_id = row["id"]
        rel_path = row["rel_path"]
        source_path = source_root / rel_path
        size = row["size"] or 0

        catalog.mark_hashing(conn, file_id)
        try:
            sha = sha256_file(source_path)
        except OSError as e:
            catalog.mark_error(conn, file_id, str(e))
            progress.advance(nbytes=size, current=rel_path, error=1)
            continue

        canonical = catalog.find_canonical_by_hash(conn, sha)
        if canonical is not None:
            catalog.mark_duplicate(conn, file_id, sha, canonical["id"])
            _prefer_older_as_canonical(conn, dest_root, row, canonical)
            progress.advance(nbytes=size, current=rel_path, duplicate=1)
            continue

        dest_rel = resolve_dest_path(conn, dest_root, rel_path)
        dest_abs = dest_root / dest_rel
        try:
            copy_and_verify(source_path, dest_abs, sha)
        except OSError as e:
            catalog.mark_error(conn, file_id, str(e))
            progress.advance(nbytes=size, current=rel_path, error=1)
            continue
        catalog.mark_unique(conn, file_id, sha, dest_rel)
        progress.advance(nbytes=size, current=rel_path, unique=1)

    progress.finish()


def _prefer_older_as_canonical(conn: sqlite3.Connection, dest_root: Path, dup_row: sqlite3.Row, canonical: sqlite3.Row) -> None:
    """Same content already kept -- if this duplicate is the more original
    (older) copy, keep its mtime on the canonical file instead."""
    if dup_row["mtime"] is None or canonical["mtime"] is None:
        return
    if dup_row["mtime"] >= canonical["mtime"]:
        return
    catalog.update_mtime(conn, canonical["id"], dup_row["mtime"])
    dest_abs = dest_root / canonical["dest_path"]
    try:
        os.utime(dest_abs, (dup_row["mtime"], dup_row["mtime"]))
    except OSError:
        pass

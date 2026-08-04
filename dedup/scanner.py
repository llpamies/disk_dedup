"""Scan phase: walk a drive, classify files, record size/mtime -- no hashing."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import catalog
from .rules import RuleSet


def _to_rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def scan_drive(conn: sqlite3.Connection, drive_id: int, source_root: Path, rules: RuleSet) -> None:
    catalog.mark_scan_started(conn, drive_id)

    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
        cur_dir = Path(dirpath)
        rel_dir = "" if cur_dir == source_root else _to_rel_posix(source_root, cur_dir)

        # Prune excluded / reparse-point (junction) directories before descending,
        # recording *why* so the report can still show what was skipped even
        # though its contents are never individually walked/stat'd.
        kept = []
        for name in dirnames:
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            child_path = cur_dir / name
            if rules.is_dir_pruned(child_rel):
                catalog.record_pruned_dir(conn, drive_id, child_rel, "excluded")
                continue
            if os.path.islink(child_path):
                catalog.record_pruned_dir(conn, drive_id, child_rel, "symlink/junction")
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            file_path = cur_dir / name

            if rules.is_excluded(rel_path):
                catalog.upsert_scanned_file(conn, drive_id, rel_path, None, None, excluded=True)
                continue

            try:
                st = file_path.stat()
            except OSError:
                # Unreadable (permissions, bad sector, broken link). Still recorded
                # so the drive-level report accounts for it; copy phase will
                # re-attempt and log the real error.
                catalog.upsert_scanned_file(conn, drive_id, rel_path, None, None, excluded=False)
                continue

            catalog.upsert_scanned_file(conn, drive_id, rel_path, st.st_size, st.st_mtime, excluded=False)

        conn.commit()

    catalog.mark_scan_completed(conn, drive_id)

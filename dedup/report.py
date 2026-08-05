"""Human-readable summaries printed after scan/copy/report commands."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Optional

from . import catalog
from .progress import human_size

STATUS_LABELS = {
    "pending": "pending (not yet hashed)",
    "hashing": "in progress / interrupted",
    "excluded_system": "excluded as system/program files",
    "unique": "unique files kept",
    "duplicate": "duplicates skipped",
    "error": "unreadable / errored",
}


def print_status_summary(conn: sqlite3.Connection, drive_id: Optional[int] = None, title: str = "Summary") -> None:
    counts = catalog.summary_counts(conn, drive_id)
    print(f"\n{title}")
    print("-" * len(title))
    total_files = 0
    total_size = 0
    for status, label in STATUS_LABELS.items():
        info = counts.get(status, {"count": 0, "size": 0})
        total_files += info["count"]
        total_size += info["size"]
        if info["count"]:
            print(f"  {label:<32} {info['count']:>8}  {human_size(info['size']):>10}")
    print(f"  {'total':<32} {total_files:>8}  {human_size(total_size):>10}")


def print_scan_breakdown(conn: sqlite3.Connection, drive_id: int) -> None:
    rows = conn.execute(
        "SELECT rel_path, status, size FROM files WHERE drive_id = ?", (drive_id,)
    ).fetchall()
    by_top: dict[str, dict[str, int]] = defaultdict(lambda: {"candidate": 0, "excluded": 0})
    for row in rows:
        top = row["rel_path"].split("/", 1)[0]
        key = "excluded" if row["status"] == "excluded_system" else "candidate"
        by_top[top][key] += 1

    print("\nBy top-level folder (candidate vs individually-excluded file counts)")
    print("-" * 55)
    for top in sorted(by_top):
        c = by_top[top]
        print(f"  {top:<35} candidate={c['candidate']:<8} excluded={c['excluded']}")

    pruned = catalog.list_pruned_dirs(conn, drive_id)
    if pruned:
        print("\nDirectories skipped entirely (not walked, so not counted above)")
        print("-" * 55)
        for row in pruned:
            print(f"  {row['rel_path']:<45} ({row['reason']})")


def print_possible_versions(conn: sqlite3.Connection, drive_id: Optional[int] = None) -> None:
    rows = list(catalog.iter_possible_versions(conn, drive_id))
    if not rows:
        return
    by_path: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_path[row["rel_path"]].append(row)

    print(f"\nPossible versions ({len(by_path)} path(s) with more than one distinct copy)")
    print("-" * 60)
    print("Informational only -- all versions were kept, nothing was skipped.")
    for rel_path, versions in sorted(by_path.items()):
        print(f"  {rel_path}")
        for v in versions:
            print(f"    {v['drive_label']:<10} sha256={v['sha256'][:12]}..  -> {v['dest_path']}")


def print_drive_report(conn: sqlite3.Connection, label: str, phase: str) -> None:
    drive_id = catalog.get_drive_id(conn, label)
    if drive_id is None:
        print(f"No catalog data for drive {label!r} yet.")
        return
    print_status_summary(conn, drive_id, title=f"Drive {label} -- {phase}")
    if phase == "scan":
        print_scan_breakdown(conn, drive_id)
    if phase == "copy":
        print_possible_versions(conn, drive_id)


def print_cumulative_report(conn: sqlite3.Connection) -> None:
    drives = catalog.list_drives(conn)
    if not drives:
        print("No drives recorded in the catalog yet.")
        return
    for row in drives:
        status = "scan complete" if row["scan_completed_at"] else "scan in progress"
        print(f"Drive {row['label']}: {row['source_root']} ({status})")
    print_status_summary(conn, drive_id=None, title="Cumulative (all drives)")
    print_possible_versions(conn, drive_id=None)

"""Copy phase: hash pending files, dedupe by content, copy unique files into
the merged destination tree with _v1/_v2 collision suffixing.

Hashing is the CPU-bound step, so it's pipelined across a small thread pool
(hashlib releases the GIL for large buffers, so this gets real multi-core
throughput). Disk reads stay strictly sequential in the main thread -- only
one file is ever being read at a time -- to avoid seek-thrashing a spinning
source HDD with concurrent reads of unrelated files. Only the hash
computation itself, which needs no further disk I/O once the bytes are in
memory, runs in parallel. All SQLite/catalog access and the actual
copy-to-destination also stay on the main thread, processed strictly in the
order files were read, so behavior (dedupe results, _v1/_v2 assignment) is
identical to the fully sequential version -- just faster.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Optional

from . import catalog
from .progress import Progress

CHUNK_SIZE = 1024 * 1024

# Files at or below this size are read fully into memory and hashed on a
# worker thread. Larger files are hashed streamed, single-threaded, in the
# main thread instead -- there's little to gain from parallelism on a single
# huge file, and it keeps peak memory use bounded.
PARALLEL_HASH_MAX_BYTES = 512 * 1024 * 1024


def _cgroup_cpu_quota() -> Optional[int]:
    """Effective core count from a cgroup CPU quota, if any. Docker --cpus,
    Kubernetes limits, etc. commonly throttle this way without narrowing
    thread affinity, so plain os.cpu_count()/sched_getaffinity can overreport
    how many cores are actually schedulable."""
    try:  # cgroup v2
        quota_s, period_s = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota_s != "max":
            return max(1, int(quota_s) // int(period_s))
    except (OSError, ValueError, IndexError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0:
            return max(1, quota // period)
    except (OSError, ValueError):
        pass
    return None


def detect_cpu_count() -> int:
    """Best-effort count of cores actually usable by this process, not just
    present on the host -- affinity masks and cgroup quotas (containers, VMs)
    can both make the real number lower than os.cpu_count()."""
    try:
        n = len(os.sched_getaffinity(0))  # Linux only; respects taskset/cpuset limits
    except AttributeError:
        n = os.cpu_count() or 1
    quota = _cgroup_cpu_quota()
    if quota is not None:
        n = min(n, quota)
    return max(1, n)


DEFAULT_HASH_WORKERS = min(4, detect_cpu_count())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
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


class _Job:
    """One in-flight file: either a hash Future (small file, parallel) or an
    already-resolved sha/error (large file, or a read error hit up front)."""

    __slots__ = ("row", "future", "sha", "error")

    def __init__(self, row: sqlite3.Row, future: Optional[Future] = None,
                 sha: Optional[str] = None, error: Optional[OSError] = None):
        self.row = row
        self.future = future
        self.sha = sha
        self.error = error


def _start_job(executor: ThreadPoolExecutor, source_root: Path, row: sqlite3.Row) -> _Job:
    source_path = source_root / row["rel_path"]
    size = row["size"] or 0
    try:
        if size <= PARALLEL_HASH_MAX_BYTES:
            data = source_path.read_bytes()  # sequential disk read, main thread
            return _Job(row, future=executor.submit(_sha256_bytes, data))
        # Large file: stream-hash directly here rather than buffering it whole.
        return _Job(row, sha=sha256_file(source_path))
    except OSError as e:
        return _Job(row, error=e)


def copy_drive(
    conn: sqlite3.Connection,
    drive_id: int,
    source_root: Path,
    dest_root: Path,
    show_progress: bool = True,
    hash_workers: int = DEFAULT_HASH_WORKERS,
) -> None:
    pending = list(catalog.get_pending_files(conn, drive_id))
    total_bytes = sum(row["size"] or 0 for row in pending)
    progress = Progress("Copy", total=len(pending), total_bytes=total_bytes, enabled=show_progress)

    hash_workers = max(1, hash_workers)
    in_flight_limit = hash_workers * 2
    rows_iter = iter(pending)
    jobs: deque[_Job] = deque()

    with ThreadPoolExecutor(max_workers=hash_workers) as executor:

        def fill() -> None:
            while len(jobs) < in_flight_limit:
                row = next(rows_iter, None)
                if row is None:
                    return
                catalog.mark_hashing(conn, row["id"])
                jobs.append(_start_job(executor, source_root, row))

        fill()
        while jobs:
            job = jobs.popleft()
            fill()  # keep the pipeline topped up before doing any (blocking) work below
            _finalize_job(conn, source_root, dest_root, job, progress)

    progress.finish()


def _finalize_job(
    conn: sqlite3.Connection, source_root: Path, dest_root: Path, job: _Job, progress: Progress
) -> None:
    row = job.row
    file_id = row["id"]
    rel_path = row["rel_path"]
    size = row["size"] or 0

    if job.error is not None:
        catalog.mark_error(conn, file_id, str(job.error))
        progress.advance(nbytes=size, current=rel_path, error=1)
        return

    if job.future is not None:
        try:
            sha = job.future.result()
        except Exception as e:  # pragma: no cover -- hashing in-memory bytes shouldn't raise
            catalog.mark_error(conn, file_id, str(e))
            progress.advance(nbytes=size, current=rel_path, error=1)
            return
    else:
        sha = job.sha

    canonical = catalog.find_canonical_by_hash(conn, sha)
    if canonical is not None:
        catalog.mark_duplicate(conn, file_id, sha, canonical["id"])
        _prefer_older_as_canonical(conn, dest_root, row, canonical)
        progress.advance(nbytes=size, current=rel_path, duplicate=1)
        return

    dest_rel = resolve_dest_path(conn, dest_root, rel_path)
    dest_abs = dest_root / dest_rel
    try:
        copy_and_verify(source_root / rel_path, dest_abs, sha)
    except OSError as e:
        catalog.mark_error(conn, file_id, str(e))
        progress.advance(nbytes=size, current=rel_path, error=1)
        return
    catalog.mark_unique(conn, file_id, sha, dest_rel)
    progress.advance(nbytes=size, current=rel_path, unique=1)


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

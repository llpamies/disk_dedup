"""Copy phase: hash pending files, dedupe by content, copy unique files into
the merged destination tree with _v1/_v2 collision suffixing.

The copy phase is a three-stage pipeline:

  1. Main thread: reads source files strictly sequentially (one at a time, in
     order -- a spinning source HDD is never hit with concurrent reads) and
     makes every catalog/dedupe decision, so results are identical to a fully
     sequential run.
  2. Hash pool: computes each file's SHA-256 from the in-memory bytes.
     hashlib releases the GIL for large buffers, so this uses real cores.
  3. Writer thread (single, so the destination disk also sees strictly
     sequential writes): writes each unique file out of the same in-memory
     bytes -- the source file is read exactly once -- then re-reads the copy
     and runs the verification hash there, off the main thread.

Stage 3 is what keeps the main thread from becoming the serialized
bottleneck: without it, the destination write plus a full verification
re-hash per unique file all ran on the main thread, and the hash workers sat
idle behind it.

Catalog access never leaves the main thread. The rare orderings that could
interact with an in-flight write (a duplicate of a file whose write hasn't
committed yet, or a large streamed file) drain the write queue first, so
dedupe results and _v1/_v2 assignment stay byte-for-byte identical to a
sequential run.
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

# Files at or below this size are read fully into memory, hashed on a worker
# thread, and written to the destination from that same buffer. Larger files
# are processed streamed and inline (hash, then copy+verify) so peak memory
# stays bounded even for huge videos/images.
PARALLEL_HASH_MAX_BYTES = 512 * 1024 * 1024

# Cap on file bytes held in memory at once across the hash stage and the
# write queue combined. Filling pauses (and completed writes are reaped to
# free their buffers) once this is exceeded.
PIPELINE_MEMORY_BUDGET = 256 * 1024 * 1024

# In-flight unique files queued for the writer thread before the main thread
# stops to reap the oldest one.
WRITE_QUEUE_MAX = 8


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
    """Streamed copy from the source, used for large files and sequential mode."""
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


class _HashJob:
    """One file in the hash stage."""

    __slots__ = ("row", "data", "future", "error", "large")

    def __init__(self, row: sqlite3.Row, data: Optional[bytes] = None,
                 future: Optional[Future] = None, error: Optional[OSError] = None,
                 large: bool = False):
        self.row = row
        self.data = data
        self.future = future
        self.error = error
        self.large = large


class _WriteJob:
    """One unique file handed to the writer thread."""

    __slots__ = ("file_id", "rel_path", "dest_rel", "sha", "data", "source_path", "size", "buf_len")

    def __init__(self, file_id: int, rel_path: str, dest_rel: str, sha: str,
                 data: bytes, source_path: Path, size: int):
        self.file_id = file_id
        self.rel_path = rel_path
        self.dest_rel = dest_rel
        self.sha = sha
        self.data = data
        self.source_path = source_path
        self.size = size
        self.buf_len = len(data)


def _write_and_verify(dest_root: Path, wjob: _WriteJob) -> Optional[str]:
    """Runs on the writer thread: write the in-memory bytes, verify by
    re-hashing the copy, atomically rename into place. Returns an error
    message, or None on success. Retries the write once on a verification
    mismatch (transient memory/cache corruption) before giving up."""
    dest_abs = dest_root / wjob.dest_rel
    last_err: Optional[str] = None
    for _attempt in range(2):
        try:
            dest_abs.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_abs.with_name(dest_abs.name + ".part")
            with open(tmp_path, "wb") as f:
                f.write(wjob.data)
            try:
                shutil.copystat(wjob.source_path, tmp_path)
            except OSError:
                pass  # metadata preservation is best-effort, never fatal
            if sha256_file(tmp_path) == wjob.sha:
                os.replace(tmp_path, dest_abs)
                wjob.data = b""
                return None
            tmp_path.unlink(missing_ok=True)
            last_err = f"copy verification failed for {wjob.source_path} (hash mismatch after write)"
        except OSError as e:
            last_err = str(e)
    wjob.data = b""
    return last_err


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

    if max(1, hash_workers) == 1:
        _copy_sequential(conn, source_root, dest_root, pending, progress)
    else:
        _Pipeline(conn, source_root, dest_root, pending, progress, hash_workers).run()

    progress.finish()


def _copy_sequential(conn, source_root: Path, dest_root: Path, pending, progress: Progress) -> None:
    """--hash-workers 1: fully sequential, streamed, nothing buffered."""
    for row in pending:
        catalog.mark_hashing(conn, row["id"])
        source_path = source_root / row["rel_path"]
        try:
            sha = sha256_file(source_path)
        except OSError as e:
            catalog.mark_error(conn, row["id"], str(e))
            progress.advance(nbytes=row["size"] or 0, current=row["rel_path"], error=1)
            continue
        _dedupe_and_copy_inline(conn, source_root, dest_root, row, sha, progress)


def _dedupe_and_copy_inline(conn, source_root: Path, dest_root: Path, row, sha: str, progress: Progress) -> None:
    """Shared by sequential mode and the large-file path: catalog decision plus
    a streamed copy_and_verify, all on the main thread."""
    size = row["size"] or 0
    canonical = catalog.find_canonical_by_hash(conn, sha)
    if canonical is not None:
        catalog.mark_duplicate(conn, row["id"], sha, canonical["id"])
        _prefer_older_as_canonical(conn, dest_root, row, canonical)
        progress.advance(nbytes=size, current=row["rel_path"], duplicate=1)
        return

    dest_rel = resolve_dest_path(conn, dest_root, row["rel_path"])
    try:
        copy_and_verify(source_root / row["rel_path"], dest_root / dest_rel, sha)
    except OSError as e:
        catalog.mark_error(conn, row["id"], str(e))
        progress.advance(nbytes=size, current=row["rel_path"], error=1)
        return
    catalog.mark_unique(conn, row["id"], sha, dest_rel)
    progress.advance(nbytes=size, current=row["rel_path"], unique=1)


class _Pipeline:
    """Read (main) -> hash (pool) -> write+verify (writer thread) pipeline.

    All catalog access stays on the main thread; the writer thread only
    touches the filesystem. Any decision that could interact with a write
    still in flight (duplicate of an uncommitted sha, or a large streamed
    file) drains the write queue first, so outcomes are identical to a
    sequential run.
    """

    def __init__(self, conn, source_root: Path, dest_root: Path, pending, progress: Progress, hash_workers: int):
        self.conn = conn
        self.source_root = source_root
        self.dest_root = dest_root
        self.rows = deque(pending)
        self.progress = progress
        self.hash_workers = max(1, hash_workers)
        self.jobs: deque[_HashJob] = deque()
        self.writes: deque[tuple[Future, _WriteJob]] = deque()
        self.in_flight_shas: set[str] = set()
        self.buffered = 0

    def run(self) -> None:
        with ThreadPoolExecutor(max_workers=self.hash_workers) as hash_pool, \
                ThreadPoolExecutor(max_workers=1) as writer:
            self.hash_pool = hash_pool
            self.writer = writer
            while True:
                self._fill()
                if not self.jobs:
                    if self.rows and self.writes:
                        # Blocked on the memory budget: reap a write to free its buffer.
                        self._apply_write(*self.writes.popleft())
                        continue
                    if not self.rows:
                        break
                    continue
                job = self.jobs.popleft()
                self._fill()  # keep the pipeline topped up before any blocking work
                self._finalize(job)
            self._drain_writes()

    def _fill(self) -> None:
        in_flight_limit = self.hash_workers * 2
        while self.rows and len(self.jobs) < in_flight_limit:
            if self.buffered > PIPELINE_MEMORY_BUDGET and (self.jobs or self.writes):
                return
            row = self.rows.popleft()
            catalog.mark_hashing(self.conn, row["id"])
            if (row["size"] or 0) > PARALLEL_HASH_MAX_BYTES:
                self.jobs.append(_HashJob(row, large=True))
                continue
            try:
                data = (self.source_root / row["rel_path"]).read_bytes()  # sequential read, main thread
            except OSError as e:
                self.jobs.append(_HashJob(row, error=e))
                continue
            self.buffered += len(data)
            self.jobs.append(_HashJob(row, data=data, future=self.hash_pool.submit(_sha256_bytes, data)))

    def _finalize(self, job: _HashJob) -> None:
        row = job.row
        size = row["size"] or 0

        if job.error is not None:
            catalog.mark_error(self.conn, row["id"], str(job.error))
            self.progress.advance(nbytes=size, current=row["rel_path"], error=1)
            return

        if job.large:
            # Streamed inline; drain first so a same-hash canonical still being
            # written is committed before the dedupe lookup.
            self._drain_writes()
            try:
                sha = sha256_file(self.source_root / row["rel_path"])
            except OSError as e:
                catalog.mark_error(self.conn, row["id"], str(e))
                self.progress.advance(nbytes=size, current=row["rel_path"], error=1)
                return
            _dedupe_and_copy_inline(self.conn, self.source_root, self.dest_root, row, sha, self.progress)
            return

        sha = job.future.result()
        data = job.data
        job.data = None
        buf_len = len(data)

        if sha in self.in_flight_shas:
            self._drain_writes()

        canonical = catalog.find_canonical_by_hash(self.conn, sha)
        if canonical is not None:
            self.buffered -= buf_len
            catalog.mark_duplicate(self.conn, row["id"], sha, canonical["id"])
            _prefer_older_as_canonical(self.conn, self.dest_root, row, canonical)
            self.progress.advance(nbytes=size, current=row["rel_path"], duplicate=1)
            return

        dest_rel = resolve_dest_path(self.conn, self.dest_root, row["rel_path"])
        wjob = _WriteJob(row["id"], row["rel_path"], dest_rel, sha, data,
                         self.source_root / row["rel_path"], size)
        self.in_flight_shas.add(sha)
        self.writes.append((self.writer.submit(_write_and_verify, self.dest_root, wjob), wjob))
        if len(self.writes) > WRITE_QUEUE_MAX:
            self._apply_write(*self.writes.popleft())

    def _apply_write(self, future: Future, wjob: _WriteJob) -> None:
        err = future.result()
        self.buffered -= wjob.buf_len
        self.in_flight_shas.discard(wjob.sha)
        if err is not None:
            catalog.mark_error(self.conn, wjob.file_id, err)
            self.progress.advance(nbytes=wjob.size, current=wjob.rel_path, error=1)
        else:
            catalog.mark_unique(self.conn, wjob.file_id, wjob.sha, wjob.dest_rel)
            self.progress.advance(nbytes=wjob.size, current=wjob.rel_path, unique=1)

    def _drain_writes(self) -> None:
        while self.writes:
            self._apply_write(*self.writes.popleft())


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

from pathlib import Path

from dedup import catalog, hasher, scanner
from dedup.rules import RuleSet


def test_detect_cpu_count_respects_cgroup_v2_quota(monkeypatch):
    def fake_read_text(self, *a, **kw):
        if self == Path("/sys/fs/cgroup/cpu.max"):
            return "100000 100000"  # quota == period -> effectively 1 core
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(hasher.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
    assert hasher.detect_cpu_count() == 1


def test_detect_cpu_count_unrestricted_quota_falls_back_to_affinity(monkeypatch):
    def fake_read_text(self, *a, **kw):
        if self == Path("/sys/fs/cgroup/cpu.max"):
            return "max 100000"  # no quota set
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    monkeypatch.setattr(hasher.os, "sched_getaffinity", lambda pid: set(range(3)), raising=False)
    assert hasher.detect_cpu_count() == 3

DEFAULT_RULES = Path(__file__).resolve().parent.parent / "exclude_rules.txt"


def make_drive(root: Path) -> None:
    (root / "Users" / "John" / "Documents").mkdir(parents=True)
    (root / "Users" / "John" / "Documents" / "a.bin").write_bytes(b"alpha" * 10_000)
    (root / "Users" / "John" / "Documents" / "b.bin").write_bytes(b"beta" * 10_000)
    (root / "Users" / "John" / "Documents" / "dup.bin").write_bytes(b"same-content" * 10_000)
    (root / "Users" / "John" / "Documents" / "dup2.bin").write_bytes(b"same-content" * 10_000)


def run_copy(tmp_path: Path, name: str, hash_workers: int) -> tuple[Path, "object"]:
    hdd = tmp_path / f"hdd_{name}"
    make_drive(hdd)
    dest = tmp_path / f"dest_{name}"
    dest.mkdir()
    conn = catalog.connect(dest / "catalog.db")
    rules = RuleSet.from_file(DEFAULT_RULES)
    drive_id = catalog.get_or_create_drive(conn, "HDD", str(hdd))
    scanner.scan_drive(conn, drive_id, hdd, rules, show_progress=False)
    hasher.copy_drive(conn, drive_id, hdd, dest, show_progress=False, hash_workers=hash_workers)
    return dest, conn


def test_parallel_hashing_matches_sequential_results(tmp_path):
    dest1, conn1 = run_copy(tmp_path, "seq", hash_workers=1)
    dest4, conn4 = run_copy(tmp_path, "par", hash_workers=4)

    def snapshot(dest, conn):
        rows = conn.execute("SELECT rel_path, status, sha256, dest_path FROM files ORDER BY rel_path").fetchall()
        return [(r["rel_path"], r["status"], r["sha256"], r["dest_path"]) for r in rows]

    assert snapshot(dest1, conn1) == snapshot(dest4, conn4)

    files1 = sorted(p.relative_to(dest1).as_posix() for p in dest1.rglob("*") if p.is_file() and p.name != "catalog.db")
    files4 = sorted(p.relative_to(dest4).as_posix() for p in dest4.rglob("*") if p.is_file() and p.name != "catalog.db")
    assert files1 == files4
    for rel in files1:
        assert (dest1 / rel).read_bytes() == (dest4 / rel).read_bytes()


def test_large_file_falls_back_to_streamed_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(hasher, "PARALLEL_HASH_MAX_BYTES", 1)  # force every file down the streamed path
    dest, conn = run_copy(tmp_path, "large", hash_workers=4)

    counts = catalog.summary_counts(conn)
    assert counts["unique"]["count"] == 3  # a.bin, b.bin, dup.bin (dup2.bin is a true duplicate)
    assert counts["duplicate"]["count"] == 1
    assert (dest / "Users" / "John" / "Documents" / "a.bin").read_bytes() == b"alpha" * 10_000

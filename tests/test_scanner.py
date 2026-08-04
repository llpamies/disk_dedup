import os
from pathlib import Path

from dedup import catalog, hasher, scanner
from dedup.rules import RuleSet

DEFAULT_RULES = Path(__file__).resolve().parent.parent / "exclude_rules.txt"


def make_drive(tmp_path: Path) -> Path:
    root = tmp_path / "hdd"
    (root / "Windows" / "System32").mkdir(parents=True)
    (root / "Users" / "John" / "Documents").mkdir(parents=True)
    (root / "Users" / "John" / "AppData" / "Local" / "Temp").mkdir(parents=True)
    (root / "Windows" / "System32" / "foo.dll").write_text("system file")
    (root / "Users" / "John" / "AppData" / "Local" / "Temp" / "junk.tmp").write_text("temp junk")
    (root / "Users" / "John" / "Documents" / "budget.xlsx").write_text("budget content")
    return root


def test_scan_excludes_system_paths_and_keeps_user_data(tmp_path):
    root = make_drive(tmp_path)
    conn = catalog.connect(tmp_path / "cat.db")
    drive_id = catalog.get_or_create_drive(conn, "HDD1", str(root))
    rules = RuleSet.from_file(DEFAULT_RULES)

    scanner.scan_drive(conn, drive_id, root, rules)

    # Windows/** and Users/*/AppData/** are whole-subtree exclusions, so they're
    # pruned before descending -- their files are never individually recorded,
    # but the pruned directory itself must still be visible for the report.
    rows = {row["rel_path"]: row["status"] for row in conn.execute("SELECT * FROM files")}
    assert "Windows/System32/foo.dll" not in rows
    assert "Users/John/AppData/Local/Temp/junk.tmp" not in rows
    assert rows["Users/John/Documents/budget.xlsx"] == "pending"

    pruned = {row["rel_path"]: row["reason"] for row in catalog.list_pruned_dirs(conn, drive_id)}
    assert pruned["Windows"] == "excluded"
    assert pruned["Users/John/AppData"] == "excluded"


def test_scan_skips_symlinked_directories(tmp_path):
    root = make_drive(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.txt").write_text("should not be picked up")
    junction = root / "Users" / "John" / "Junction"
    try:
        junction.symlink_to(outside, target_is_directory=True)
    except OSError:
        return  # symlinks unsupported in this environment; skip

    conn = catalog.connect(tmp_path / "cat.db")
    drive_id = catalog.get_or_create_drive(conn, "HDD1", str(root))
    rules = RuleSet.from_file(DEFAULT_RULES)
    scanner.scan_drive(conn, drive_id, root, rules)

    rel_paths = {row["rel_path"] for row in conn.execute("SELECT rel_path FROM files")}
    assert not any("Junction" in p for p in rel_paths)
    pruned = {row["rel_path"]: row["reason"] for row in catalog.list_pruned_dirs(conn, drive_id)}
    assert pruned["Users/John/Junction"] == "symlink/junction"


def test_end_to_end_dedup_and_versioning(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rules = RuleSet.from_file(DEFAULT_RULES)
    conn = catalog.connect(dest / "catalog.db")

    hdd1 = tmp_path / "hdd1"
    (hdd1 / "Users" / "John" / "Documents").mkdir(parents=True)
    (hdd1 / "Users" / "John" / "Documents" / "Resume.docx").write_text("version A")
    (hdd1 / "Users" / "John" / "Documents" / "budget.xlsx").write_text("shared content")

    hdd2 = tmp_path / "hdd2"
    (hdd2 / "Users" / "John" / "Documents").mkdir(parents=True)
    (hdd2 / "Users" / "John" / "Documents" / "Resume.docx").write_text("version B - edited")
    (hdd2 / "Users" / "John" / "Documents" / "budget.xlsx").write_text("shared content")  # true duplicate

    d1 = catalog.get_or_create_drive(conn, "HDD1", str(hdd1))
    scanner.scan_drive(conn, d1, hdd1, rules)
    hasher.copy_drive(conn, d1, hdd1, dest)

    d2 = catalog.get_or_create_drive(conn, "HDD2", str(hdd2))
    scanner.scan_drive(conn, d2, hdd2, rules)
    hasher.copy_drive(conn, d2, hdd2, dest)

    # True duplicate: only one copy on disk.
    assert (dest / "Users" / "John" / "Documents" / "budget.xlsx").exists()
    dup_row = conn.execute(
        "SELECT * FROM files WHERE rel_path='Users/John/Documents/budget.xlsx' AND drive_id=?", (d2,)
    ).fetchone()
    assert dup_row["status"] == "duplicate"

    # Same-path, different content: both kept with _v1/_v2 suffixes, no plain name left.
    assert not (dest / "Users" / "John" / "Documents" / "Resume.docx").exists()
    assert (dest / "Users" / "John" / "Documents" / "Resume_v1.docx").read_text() == "version A"
    assert (dest / "Users" / "John" / "Documents" / "Resume_v2.docx").read_text() == "version B - edited"

    versions = list(catalog.iter_possible_versions(conn))
    assert len(versions) == 2


def test_resume_does_not_recopy_or_duplicate(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    rules = RuleSet.from_file(DEFAULT_RULES)
    conn = catalog.connect(dest / "catalog.db")

    hdd1 = tmp_path / "hdd1"
    (hdd1 / "Users" / "John").mkdir(parents=True)
    (hdd1 / "Users" / "John" / "note.txt").write_text("hello")

    d1 = catalog.get_or_create_drive(conn, "HDD1", str(hdd1))
    scanner.scan_drive(conn, d1, hdd1, rules)
    hasher.copy_drive(conn, d1, hdd1, dest)

    copied = dest / "Users" / "John" / "note.txt"
    mtime_before = copied.stat().st_mtime

    # Re-run scan + copy against the unchanged drive.
    scanner.scan_drive(conn, d1, hdd1, rules)
    hasher.copy_drive(conn, d1, hdd1, dest)

    assert copied.stat().st_mtime == mtime_before
    counts = catalog.summary_counts(conn, d1)
    assert counts["unique"]["count"] == 1
    assert "duplicate" not in counts

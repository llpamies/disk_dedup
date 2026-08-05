from dedup import catalog


def test_get_or_create_drive_is_idempotent(tmp_path):
    conn = catalog.connect(tmp_path / "cat.db")
    id1 = catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")
    id2 = catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")
    assert id1 == id2


def test_get_or_create_drive_rejects_relabeled_source(tmp_path):
    conn = catalog.connect(tmp_path / "cat.db")
    catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")
    try:
        catalog.get_or_create_drive(conn, "HDD1", "/mnt/other")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_upsert_scanned_file_skips_terminal_rows_on_resume(tmp_path):
    conn = catalog.connect(tmp_path / "cat.db")
    drive_id = catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")

    catalog.upsert_scanned_file(conn, drive_id, "a.txt", 10, 100.0, excluded=False)
    row = conn.execute("SELECT * FROM files WHERE rel_path = 'a.txt'").fetchone()
    catalog.mark_unique(conn, row["id"], "deadbeef", "a.txt")

    # Re-scanning with the same size/mtime should not reset a terminal row.
    catalog.upsert_scanned_file(conn, drive_id, "a.txt", 10, 100.0, excluded=False)
    row2 = conn.execute("SELECT * FROM files WHERE rel_path = 'a.txt'").fetchone()
    assert row2["status"] == "unique"
    assert row2["sha256"] == "deadbeef"

    # A changed file (different mtime) should be reset back to pending.
    catalog.upsert_scanned_file(conn, drive_id, "a.txt", 12, 200.0, excluded=False)
    row3 = conn.execute("SELECT * FROM files WHERE rel_path = 'a.txt'").fetchone()
    assert row3["status"] == "pending"
    assert row3["sha256"] is None


def test_find_canonical_by_hash_only_matches_unique_status(tmp_path):
    conn = catalog.connect(tmp_path / "cat.db")
    drive_id = catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")
    catalog.upsert_scanned_file(conn, drive_id, "a.txt", 10, 100.0, excluded=False)
    row = conn.execute("SELECT * FROM files WHERE rel_path = 'a.txt'").fetchone()

    assert catalog.find_canonical_by_hash(conn, "deadbeef") is None
    catalog.mark_unique(conn, row["id"], "deadbeef", "a.txt")
    found = catalog.find_canonical_by_hash(conn, "deadbeef")
    assert found is not None
    assert found["id"] == row["id"]


def test_iter_duplicates_lists_skipped_files_with_source_and_canonical_info(tmp_path):
    conn = catalog.connect(tmp_path / "cat.db")
    d1 = catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")
    d2 = catalog.get_or_create_drive(conn, "HDD2", "/mnt/hdd2")

    catalog.upsert_scanned_file(conn, d1, "Users/John/vacation.jpg", 10, 100.0, excluded=False)
    kept = conn.execute("SELECT * FROM files WHERE drive_id=? AND rel_path='Users/John/vacation.jpg'", (d1,)).fetchone()
    catalog.mark_unique(conn, kept["id"], "same-hash", "Users/John/vacation.jpg")

    catalog.upsert_scanned_file(conn, d2, "Users/John/vacation.jpg", 10, 150.0, excluded=False)
    dup = conn.execute("SELECT * FROM files WHERE drive_id=? AND rel_path='Users/John/vacation.jpg'", (d2,)).fetchone()
    catalog.mark_duplicate(conn, dup["id"], "same-hash", kept["id"])

    # An unrelated unique file should never show up in the duplicate dump.
    catalog.upsert_scanned_file(conn, d1, "Users/John/budget.xlsx", 5, 50.0, excluded=False)
    other = conn.execute("SELECT * FROM files WHERE rel_path='Users/John/budget.xlsx'").fetchone()
    catalog.mark_unique(conn, other["id"], "hash-c", "Users/John/budget.xlsx")

    results = list(catalog.iter_duplicates(conn))
    assert len(results) == 1
    row = results[0]
    assert row["rel_path"] == "Users/John/vacation.jpg"
    assert row["drive_label"] == "HDD2"
    assert row["source_root"] == "/mnt/hdd2"
    assert row["canonical_dest_path"] == "Users/John/vacation.jpg"
    assert row["canonical_drive_label"] == "HDD1"

    assert list(catalog.iter_duplicates(conn, drive_id=d1)) == []
    assert len(list(catalog.iter_duplicates(conn, drive_id=d2))) == 1


def test_iter_possible_versions_flags_same_path_different_hash(tmp_path):
    conn = catalog.connect(tmp_path / "cat.db")
    d1 = catalog.get_or_create_drive(conn, "HDD1", "/mnt/hdd1")
    d2 = catalog.get_or_create_drive(conn, "HDD2", "/mnt/hdd2")

    catalog.upsert_scanned_file(conn, d1, "Resume.docx", 10, 100.0, excluded=False)
    r1 = conn.execute("SELECT * FROM files WHERE drive_id=? AND rel_path='Resume.docx'", (d1,)).fetchone()
    catalog.mark_unique(conn, r1["id"], "hash-a", "Resume_v1.docx")

    catalog.upsert_scanned_file(conn, d2, "Resume.docx", 12, 200.0, excluded=False)
    r2 = conn.execute("SELECT * FROM files WHERE drive_id=? AND rel_path='Resume.docx'", (d2,)).fetchone()
    catalog.mark_unique(conn, r2["id"], "hash-b", "Resume_v2.docx")

    versions = list(catalog.iter_possible_versions(conn))
    assert {v["dest_path"] for v in versions} == {"Resume_v1.docx", "Resume_v2.docx"}

    # A path with only one hash across all drives should not show up.
    catalog.upsert_scanned_file(conn, d1, "budget.xlsx", 5, 50.0, excluded=False)
    rb = conn.execute("SELECT * FROM files WHERE rel_path='budget.xlsx'").fetchone()
    catalog.mark_unique(conn, rb["id"], "hash-c", "budget.xlsx")
    versions2 = list(catalog.iter_possible_versions(conn))
    assert "budget.xlsx" not in {v["dest_path"] for v in versions2}

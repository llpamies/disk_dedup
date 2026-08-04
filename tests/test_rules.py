from pathlib import Path

from dedup.rules import RuleSet


def make_ruleset(tmp_path: Path, lines: list[str]) -> RuleSet:
    rules_file = tmp_path / "rules.txt"
    rules_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return RuleSet.from_file(rules_file)


def test_directory_pattern_excludes_subtree(tmp_path):
    rs = make_ruleset(tmp_path, ["Windows/**"])
    assert rs.is_excluded("Windows/System32/foo.dll")
    assert rs.is_excluded("Windows/explorer.exe")
    assert not rs.is_excluded("Users/John/Windows Notes.txt")


def test_wildcard_segment(tmp_path):
    rs = make_ruleset(tmp_path, ["Users/*/AppData/**"])
    assert rs.is_excluded("Users/John/AppData/Local/Temp/x.tmp")
    assert rs.is_excluded("Users/Mary/AppData/Roaming/foo.cfg")
    assert not rs.is_excluded("Users/John/Documents/AppData notes.txt")


def test_basename_pattern_matches_anywhere(tmp_path):
    rs = make_ruleset(tmp_path, ["Thumbs.db"])
    assert rs.is_excluded("Thumbs.db")
    assert rs.is_excluded("Users/John/Pictures/Thumbs.db")
    assert rs.is_excluded("Users/John/Pictures/thumbs.DB")  # case-insensitive
    assert not rs.is_excluded("Users/John/Pictures/NotThumbs.db.txt")


def test_comments_and_blank_lines_ignored(tmp_path):
    rs = make_ruleset(tmp_path, ["# comment", "", "  ", "Windows/**"])
    assert rs.is_excluded("Windows/System32/foo.dll")


def test_dir_pruned_for_directory_scoped_pattern(tmp_path):
    rs = make_ruleset(tmp_path, ["Windows/**", "Users/*/AppData/**"])
    assert rs.is_dir_pruned("Windows")
    assert rs.is_dir_pruned("Users/John/AppData")
    assert not rs.is_dir_pruned("Users/John/Documents")
    assert not rs.is_dir_pruned("")


def test_glob_star_within_segment(tmp_path):
    rs = make_ruleset(tmp_path, ["Users/*/NTUSER.DAT*"])
    assert rs.is_excluded("Users/John/NTUSER.DAT")
    assert rs.is_excluded("Users/John/NTUSER.DAT.LOG1")
    assert not rs.is_excluded("Users/John/Documents/NTUSER.DAT")

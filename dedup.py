#!/usr/bin/env python3
"""CLI for extracting a deduplicated collection of user files from several
Windows-and-user-data HDDs processed one at a time.

Typical workflow, run once per drive:

    python dedup.py scan  <drive_path> --label HDD1 --dest D:\\UniqueFiles
    # review the report, tune exclude_rules.txt if needed, re-run scan
    python dedup.py copy  <drive_path> --label HDD1 --dest D:\\UniqueFiles
    python dedup.py report --dest D:\\UniqueFiles
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dedup import catalog, report, scanner, hasher
from dedup.rules import RuleSet

DEFAULT_RULES = Path(__file__).resolve().parent / "exclude_rules.txt"


def _open_catalog(dest: Path) -> "sqlite3.Connection":
    dest.mkdir(parents=True, exist_ok=True)
    return catalog.connect(dest / "catalog.db")


def cmd_scan(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve()
    rules_path = Path(args.rules).resolve()

    if not source.is_dir():
        print(f"error: source path does not exist or is not a directory: {source}", file=sys.stderr)
        return 1
    if not rules_path.is_file():
        print(f"error: exclude rules file not found: {rules_path}", file=sys.stderr)
        return 1

    rules = RuleSet.from_file(rules_path)
    conn = _open_catalog(dest)
    try:
        drive_id = catalog.get_or_create_drive(conn, args.label, str(source))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Scanning {source} as drive {args.label!r} (rules: {rules_path})...")
    scanner.scan_drive(conn, drive_id, source, rules, show_progress=not args.quiet)
    report.print_drive_report(conn, args.label, phase="scan")
    print("\nReview the counts above. If exclusions look wrong, edit the rules "
          "file and re-run scan (it's safe to repeat) before running 'copy'.")
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    dest = Path(args.dest).resolve()

    if not source.is_dir():
        print(f"error: source path does not exist or is not a directory: {source}", file=sys.stderr)
        return 1

    conn = _open_catalog(dest)
    drive_id = catalog.get_drive_id(conn, args.label)
    if drive_id is None:
        print(f"error: drive {args.label!r} has not been scanned yet -- run 'scan' first.", file=sys.stderr)
        return 1

    print(f"Copying unique files from drive {args.label!r} into {dest}...")
    hasher.copy_drive(conn, drive_id, source, dest, show_progress=not args.quiet)
    report.print_drive_report(conn, args.label, phase="copy")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    dest = Path(args.dest).resolve()
    db_path = dest / "catalog.db"
    if not db_path.is_file():
        print(f"error: no catalog found at {db_path} -- run 'scan' first.", file=sys.stderr)
        return 1

    conn = catalog.connect(db_path)

    drive_id = None
    if args.drive:
        drive_id = catalog.get_drive_id(conn, args.drive)
        if drive_id is None:
            print(f"error: no catalog data for drive {args.drive!r}.", file=sys.stderr)
            return 1

    if args.list_duplicates:
        report.print_duplicate_list(conn, drive_id)
        return 0

    if args.drive:
        report.print_drive_report(conn, args.drive, phase="copy")
    else:
        report.print_cumulative_report(conn)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Walk a drive and classify files without hashing/copying.")
    p_scan.add_argument("source", help="Path to the mounted drive/folder to scan.")
    p_scan.add_argument("--label", required=True, help="Short name identifying this drive, e.g. HDD1.")
    p_scan.add_argument("--dest", required=True, help="Destination collection folder (holds catalog.db).")
    p_scan.add_argument("--rules", default=str(DEFAULT_RULES), help="Path to exclude_rules.txt.")
    p_scan.add_argument("--quiet", action="store_true", help="Suppress the live progress line.")
    p_scan.set_defaults(func=cmd_scan)

    p_copy = sub.add_parser("copy", help="Hash pending candidates, dedupe, and copy unique files.")
    p_copy.add_argument("source", help="Path to the mounted drive/folder previously scanned.")
    p_copy.add_argument("--label", required=True, help="Same --label used for 'scan'.")
    p_copy.add_argument("--dest", required=True, help="Destination collection folder.")
    p_copy.add_argument("--quiet", action="store_true", help="Suppress the live progress bar.")
    p_copy.set_defaults(func=cmd_copy)

    p_report = sub.add_parser("report", help="Print scan/copy summary from the catalog.")
    p_report.add_argument("--dest", required=True, help="Destination collection folder.")
    p_report.add_argument("--drive", default=None, help="Limit the report to one drive label.")
    p_report.add_argument(
        "--list-duplicates",
        action="store_true",
        help="Instead of the summary, print one source file path per line for every "
        "duplicate that was skipped (not copied). Plain output, suitable for piping "
        "or redirecting to a file. Works against an existing --dest with no re-scan/copy needed.",
    )
    p_report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

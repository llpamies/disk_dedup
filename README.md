# disk_dedup

Extract a single, deduplicated collection of user files out of several old
HDDs that mix Windows system files with personal data — without needing all
the drives plugged in at once.

- **Read-only on the sources.** Nothing on the HDDs is ever modified or deleted.
- **Cross-drive dedup by content (SHA-256).** A `catalog.db` in the destination
  folder remembers every file hash seen so far, so plugging in drive 4 next
  week still correctly skips anything already copied from drive 1.
- **System files stripped by path rules**, not copied at all (see
  `exclude_rules.txt`).
- **Merged output tree.** Files from every drive land in one unified folder
  structure (`dest/Users/John/Documents/...`), not one subtree per drive.
- **Version-safe.** If the same path shows up with *different* content on two
  drives (e.g. a document edited between backups), both are kept, disambiguated
  as `Resume_v1.docx` / `Resume_v2.docx`.

## Requirements

Python 3.9+, standard library only — nothing to `pip install` to run the tool
itself (`pytest` is only needed to run the test suite).

## Usage

Process each of the 5 drives in turn, one at a time:

```bash
# 1. Dry run: classify files, no hashing or copying yet.
python dedup.py scan /mnt/hdd1 --label HDD1 --dest /mnt/collection

# Review the printed report -- especially the "skipped entirely" directory
# list and the top-level folder breakdown. If something that should be user
# data got excluded (or vice versa), edit exclude_rules.txt and re-run scan;
# it's safe to repeat.

# 2. Hash, dedupe, and copy the unique files.
python dedup.py copy /mnt/hdd1 --label HDD1 --dest /mnt/collection

# 3. Unplug HDD1, plug in HDD2, repeat with --label HDD2, etc.
python dedup.py scan /mnt/hdd2 --label HDD2 --dest /mnt/collection
python dedup.py copy /mnt/hdd2 --label HDD2 --dest /mnt/collection
```

At any point:

```bash
python dedup.py report --dest /mnt/collection            # all drives so far
python dedup.py report --dest /mnt/collection --drive HDD2

# Plain, one-path-per-line dump of source files skipped as duplicates
# (not copied) -- reads straight from the existing catalog.db, no
# re-scan/copy needed. Handy for piping/redirecting, e.g. to double check
# before reclaiming space on a source drive.
python dedup.py report --dest /mnt/collection --list-duplicates > dupes.txt
python dedup.py report --dest /mnt/collection --list-duplicates --drive HDD2
```

### Parallel hashing

If your source drive can deliver data faster than a single CPU core can run
SHA-256 over it, hashing itself becomes the bottleneck rather than the disk.
`copy` pipelines hash computation across a small thread pool (default: up to
4 cores) while keeping disk reads strictly sequential in one thread -- only
the CPU-bound hash step runs in parallel, so a spinning source HDD is never
hit with concurrent reads of unrelated files (which would cause seek
thrashing and likely make things slower, not faster). Tune it with
`--hash-workers N`, or `--hash-workers 1` to fall back to fully sequential.

The worker count actually used is printed at the start of `copy` (e.g. "4
hash worker threads"), so it's never a guess. The default is capped to
however many cores this process can actually use -- `os.sched_getaffinity`
plus a cgroup CPU quota check (Docker `--cpus`, Kubernetes limits, etc.),
not just the host's total core count, since those can throttle a process to
far fewer usable cores than `os.cpu_count()` reports. On a genuinely
single-core machine, threading a CPU-bound hash can't produce any real
speedup -- the default correctly collapses to 1 worker there, and pegging
that one core near 100% while hashing is expected, not a bug.

### Progress

Both `scan` and `copy` print a live progress line to stderr while running.
`copy` knows the file count/bytes upfront, so it's a real `N/M (x%)` bar with
bytes copied. `scan` doesn't know the total ahead of time without a wasted
extra pass over the drive, so it shows a running counter instead (files
seen, candidate/excluded tallies, current path). In a real terminal the line
updates in place; redirected to a file/log it prints periodic snapshots
instead. Pass `--quiet` to either command to suppress it.

### Resuming an interrupted run

`scan` and `copy` are both safe to re-run on the same drive/label. Files
already fully processed (excluded, copied as unique, or marked duplicate) are
left untouched; only files that changed size/mtime since the last scan, or
that were never finished, are reprocessed.

### Tuning what counts as "system files"

`exclude_rules.txt` is a plain list of path patterns (see the comments at the
top of that file for the syntax). Installed software and folder layout vary
machine to machine, so it's worth reviewing the `scan` report per drive —
pass a drive-specific copy with `--rules some_other_rules.txt` if one drive
needs different treatment.

### Same file, different content across drives

Dedup is by content hash, not by name — a file appearing at the same path on
two drives with *different* hashes is not treated as a duplicate collision
(it's almost always a different snapshot/version from a different backup
date). Both are kept, suffixed `_v1`, `_v2`, etc. `report` prints a "possible
versions" section listing every such case so you can review and decide
whether to prune manually.

### True duplicates and which copy is kept

When the exact same content is found again, only one copy is kept, and the
canonical file's modified-time is adjusted to match whichever occurrence was
older — the earliest, "most original" copy wins even if a later drive happens
to be processed first.

## Development

```bash
pip install pytest
python3 -m pytest
```

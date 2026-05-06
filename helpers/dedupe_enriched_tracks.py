"""Dedupe enriched_tracks rows that share a beatport_id.

The enriched_tracks schema has no UNIQUE constraint on beatport_id (by design —
each row records a separate detection→Beatport resolution). Over time this
accumulates duplicate rows for tracks that were detected multiple times or
that came in via both `dj detect enrich` (Shazam path) and `dj detect
sync-beatport` (playlist sync). For 81 of these in the user's DB.

This helper:
  1. Finds every beatport_id with > 1 row in enriched_tracks
  2. For each group, picks the most-populated row as canonical
     (tie-break: lowest id = oldest = most stable)
  3. Deletes the others
  4. Backs up dj.db before writing

What you keep:
  - enriched_tracks_analysis (keyed on beatport_id, untouched)
  - detected_tracks rows (independent, untouched)
  - The most-complete enriched_tracks row per beatport_id

What you lose:
  - Parallel detected_track_id linkage for the same Beatport track
    (i.e. you can no longer query "which detections resolved to this
    Beatport ID" from enriched_tracks alone — still recoverable by
    re-joining detected_tracks → enriched_tracks via fuzzy title match
    if ever needed).

Usage:
  uv run python helpers/dedupe_enriched_tracks.py --dry-run
  uv run python helpers/dedupe_enriched_tracks.py
  uv run python helpers/dedupe_enriched_tracks.py --verbose
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()

DB_PATH = Path.home() / "Music" / "dj-tools" / "dj.db"
BACKUP_DIR = Path.home() / "Music" / "dj-tools" / "backups" / "dj-tools"


def populated_score(row: sqlite3.Row, cols: list[str]) -> int:
    """Count fields that are not NULL/empty/zero. Higher = more complete row."""
    return sum(
        1 for c in cols
        if row[c] not in (None, "", 0, 0.0)
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print per-group keep/drop decisions")
    args = ap.parse_args()

    if not DB_PATH.exists():
        console.print(f"[red]DB not found:[/red] {DB_PATH}")
        return 1

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    cols = [r["name"] for r in con.execute("PRAGMA table_info(enriched_tracks)").fetchall()]

    dup_bids = [
        r["beatport_id"] for r in con.execute(
            "SELECT beatport_id FROM enriched_tracks "
            "WHERE beatport_id IS NOT NULL "
            "GROUP BY beatport_id HAVING COUNT(*) > 1 "
            "ORDER BY beatport_id"
        )
    ]
    console.print(f"[bold]bp_ids with duplicate rows:[/bold] {len(dup_bids)}")

    if not dup_bids:
        console.print("[green]Nothing to dedupe.[/green]")
        con.close()
        return 0

    delete_ids: list[int] = []
    keep_summary: list[tuple[int, int, int, list[int]]] = []  # (bid, keep_id, score, dropped_ids)

    for bid in dup_bids:
        rows = con.execute(
            "SELECT * FROM enriched_tracks WHERE beatport_id = ?",
            (bid,),
        ).fetchall()
        rows_sorted = sorted(rows, key=lambda r: (-populated_score(r, cols), r["id"]))
        keep = rows_sorted[0]
        drop = rows_sorted[1:]
        keep_summary.append((bid, keep["id"], populated_score(keep, cols), [d["id"] for d in drop]))
        delete_ids.extend(d["id"] for d in drop)

        if args.verbose:
            console.print(
                f"  bp:{bid:<10}  keep id={keep['id']} score={populated_score(keep, cols)}/{len(cols)}  "
                f"drop={','.join(str(d['id']) for d in drop)}"
            )

    total_kept = len(keep_summary)
    total_deleted = len(delete_ids)
    console.print(
        f"\nWould keep {total_kept} canonical rows, delete {total_deleted} duplicates"
    )

    if args.dry_run:
        console.print("[dim]DRY RUN — no writes[/dim]")
        con.close()
        return 0

    # Backup dj.db
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"{ts}_dedupe-enriched-tracks.db"
    con.close()  # release lock before file copy
    shutil.copy2(DB_PATH, backup_path)
    console.print(f"[dim]Backed up dj.db → {backup_path}[/dim]")

    # Reopen + delete in one transaction
    con = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(delete_ids))
    n_deleted = con.execute(
        f"DELETE FROM enriched_tracks WHERE id IN ({placeholders})",
        delete_ids,
    ).rowcount
    con.commit()
    con.close()

    console.print(f"\n[green]Deleted {n_deleted} duplicate rows.[/green]")
    console.print(
        f"enriched_tracks now has 1 row per beatport_id "
        f"(except for any with multi-row groups not deduped here)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

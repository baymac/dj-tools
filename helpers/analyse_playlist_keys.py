"""Analyze every Beatport track in a rekordbox playlist via DJ Studio's SDK,
then write the analyzed mik_key back into the rekordbox playlist.

Two-step orchestration:
    1. Extract beatport_ids from the named rekordbox playlist
    2. Run `dj detect studio-analyse --ids ...` on tracks not already in
       enriched_tracks_analysis (DJ Studio must be quit for this step)
    3. Run helpers/update_rekordbox_keys.py to rewrite KeyID per track
       using the freshly-analyzed mik_key (rekordbox must be quit for this step)

Useful when you don't want to wait for the full studio-analyse backfill but
do want accurate mik_key for one specific playlist.

Per-track time estimate: ~25-30s (Demucs + ai-beatgrid + cf.dj.studio call).
For 35 tracks: ~15-18 minutes (sequential, single-threaded — that's how
the SDK helper runs).

Usage:
    uv run helpers/analyse_playlist_keys.py --playlist "GF Birthday Set 4"
    uv run helpers/analyse_playlist_keys.py --playlist "Foo" --skip-analyse  # only run the key-update phase
    uv run helpers/analyse_playlist_keys.py --playlist "Foo" --dry-run       # report scope without doing anything
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from rekordbox.utils import beatport_id_from_folder_path

console = Console()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--playlist", required=True, help="Rekordbox playlist name (exact match)")
    ap.add_argument("--skip-analyse", action="store_true",
                    help="Skip the studio-analyse step (run the key-update only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would happen without running any subcommand")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    # ── Step 1: extract bp_ids from the playlist ────────────────────────────
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables
    import sqlite3

    db = Rekordbox6Database()
    try:
        pl = db.get_playlist(Name=args.playlist).first()
        if not pl:
            console.print(f"[red]Playlist not found:[/red] {args.playlist}")
            return 1
        songs = db.get_playlist_songs(PlaylistID=pl.ID).all()
        bp_ids: list[int] = []
        for s in songs:
            c = db.session.query(tables.DjmdContent).filter(tables.DjmdContent.ID == s.ContentID).first()
            bid = beatport_id_from_folder_path(c.FolderPath if c else None)
            if bid is not None:
                bp_ids.append(bid)
    finally:
        db.close()

    bp_ids = list(dict.fromkeys(bp_ids))  # dedupe, preserve order
    console.print(
        f"[bold]playlist:[/bold] {args.playlist}  ([cyan]{len(bp_ids)}[/cyan] Beatport tracks)"
    )

    # ── Step 2: filter out tracks already analyzed ──────────────────────────
    con = sqlite3.connect(Path.home() / "Music" / "dj-tools" / "dj.db")
    already = {
        r[0] for r in con.execute(
            f"SELECT beatport_id FROM enriched_tracks_analysis "
            f"WHERE beatport_id IN ({','.join('?' * len(bp_ids))})",
            bp_ids,
        )
    } if bp_ids else set()
    con.close()
    pending = [b for b in bp_ids if b not in already]
    console.print(
        f"[dim]already analyzed: {len(already)}    pending studio-analyse: {len(pending)}[/dim]"
    )
    if pending:
        est_sec = len(pending) * 27  # ~25-30s per track average
        console.print(
            f"[dim]est. studio-analyse runtime: ~{est_sec // 60}m{est_sec % 60}s "
            f"({len(pending)} × ~27s/track)[/dim]"
        )

    if args.dry_run:
        if pending:
            console.print("\n[dim]Would run:[/dim]")
            preview = ",".join(str(b) for b in pending[:10])
            tail = "…" if len(pending) > 10 else ""
            console.print(f"  uv run dj_cli.py detect studio-analyse --ids {preview}{tail}")
        console.print(f"  uv run python helpers/update_rekordbox_keys.py --playlist '{args.playlist}'")
        return 0

    # ── Step 3: run studio-analyse on pending tracks ────────────────────────
    if pending and not args.skip_analyse:
        ids_arg = ",".join(str(b) for b in pending)
        cmd = ["uv", "run", "dj_cli.py", "detect", "studio-analyse", "--ids", ids_arg]
        if args.verbose:
            cmd.append("--verbose")
        console.print(f"\n[bold cyan]→ running studio-analyse on {len(pending)} tracks…[/bold cyan]")
        console.print(f"[dim]({' '.join(cmd[:6])} --ids {pending[0]},…[{len(pending)} ids])[/dim]\n")
        t0 = time.time()
        rc = subprocess.run(cmd).returncode
        elapsed = time.time() - t0
        console.print(f"[dim]studio-analyse took {elapsed/60:.1f}m[/dim]")
        if rc != 0:
            console.print(f"[red]studio-analyse exited with code {rc} — aborting key update.[/red]")
            return rc

    # ── Step 4: update rekordbox KeyID using mik_key ─────────────────────────
    cmd = ["uv", "run", "python", "helpers/update_rekordbox_keys.py",
           "--playlist", args.playlist]
    if args.verbose:
        cmd.append("--verbose")
    console.print(f"\n[bold cyan]→ updating rekordbox keys…[/bold cyan]")
    rc = subprocess.run(cmd).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())

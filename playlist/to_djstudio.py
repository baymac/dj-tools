"""Write a new DJ Studio mix project file from a list of enriched-tracks rows.

The mix appears in DJ Studio's project list with linear track ordering and
empty `autoEffects` (you add transitions in DJ Studio's UI). Tracks must
already be in DJ Studio's audio-library-table — run `dj detect import-to-studio`
first if not.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional, Sequence
from uuid import uuid4

import psutil
from rich.console import Console

_DEFAULT_CONSOLE = Console()

_PROJECTS_DIR = Path.home() / "Music" / "DJ.Studio" / "Database" / "projects-table"
_LIBRARY_DIR = Path.home() / "Music" / "DJ.Studio" / "Database" / "audio-library-table"


def _is_dj_studio_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "dj.studio" in name or "dj studio" in name:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _audio_library_keys() -> set[str]:
    """Return the set of libraryKeys present in DJ Studio's audio-library-table."""
    keys: set[str] = set()
    if not _LIBRARY_DIR.is_dir():
        return keys
    for shard in _LIBRARY_DIR.iterdir():
        if not shard.is_dir():
            continue
        for f in shard.iterdir():
            if not f.is_file():
                continue
            try:
                track = json.loads(f.read_text())
                k = track.get("key")
                if k:
                    keys.add(k)
            except Exception:
                continue
    return keys


def push_to_djstudio(
    rows: Sequence[dict],
    mix_name: str,
    *,
    dry_run: bool = False,
    console: Optional[Console] = None,
) -> None:
    console = console or _DEFAULT_CONSOLE
    if not rows:
        console.print("[yellow]No tracks.[/yellow]")
        return

    if _is_dj_studio_running():
        console.print(
            "[red]DJ Studio is currently running.[/red]\n"
            "Quit DJ Studio (Cmd+Q) before running this command — it caches the projects "
            "directory and may not pick up the new mix until restart."
        )
        return

    if not _PROJECTS_DIR.is_dir():
        console.print(f"[red]DJ Studio projects directory not found: {_PROJECTS_DIR}[/red]")
        return

    library_keys = _audio_library_keys()

    track_refs: list[dict] = []
    bpms: list[float] = []
    genres: dict[str, int] = {}
    durations: list[int] = []
    missing: list[int] = []

    for row in rows:
        bid = row["beatport_id"]
        lib_key = f"beatport-sdk_{bid}"
        if lib_key not in library_keys:
            missing.append(int(bid))
            continue
        track_refs.append({
            "key": str(uuid4()),
            "libraryKey": lib_key,
        })
        bpm = row.get("tempo_precise") or row.get("bpm")
        if bpm:
            try:
                bpms.append(float(bpm))
            except (TypeError, ValueError):
                pass
        genre = row.get("genre") or ""
        if genre:
            genres[genre] = genres.get(genre, 0) + 1
        dur = row.get("duration_sec") or row.get("length_ms") or 0
        try:
            dur_int = int(dur)
            if row.get("length_ms") and not row.get("duration_sec"):
                dur_int = dur_int // 1000
            if dur_int > 0:
                durations.append(dur_int)
        except (TypeError, ValueError):
            pass

    if missing:
        preview = ", ".join(str(b) for b in missing[:5])
        console.print(
            f"[yellow]{len(missing)} of {len(rows)} tracks not in DJ Studio's library.[/yellow]\n"
            f"  Run [cyan]dj detect import-to-studio[/cyan] to import them, then re-run.\n"
            f"  missing: {preview}{'…' if len(missing) > 5 else ''}"
        )

    if not track_refs:
        console.print("[red]Nothing to write — all tracks missing from DJ Studio's library.[/red]")
        return

    project_uuid = str(uuid4())
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    main_genre = max(genres.items(), key=lambda kv: kv[1])[0] if genres else ""
    min_bpm = min(bpms) if bpms else 0.0
    max_bpm = max(bpms) if bpms else 0.0
    total_duration = sum(durations) if durations else len(track_refs) * 240

    project = {
        "key": project_uuid,
        "name": mix_name,
        "genre": main_genre,
        "duration": total_duration,
        "trackCount": len(track_refs),
        "minBpm": min_bpm,
        "maxBpm": max_bpm,
        "createdAt": now_iso,
        "lastModified": now_iso,
        "mixList": track_refs,
        "autoEffects": [],
    }

    project_path = _PROJECTS_DIR / project_uuid

    console.print(
        f"[bold]playlist → DJ Studio[/bold] ← {len(track_refs)} tracks  →  [yellow]{mix_name}[/yellow]"
    )
    console.print(f"  uuid: {project_uuid}")
    console.print(f"  path: {project_path}")
    console.print(f"  bpm:  {min_bpm:.1f} – {max_bpm:.1f}   genre: {main_genre or '-'}")

    if dry_run:
        console.print("[dim]DRY RUN — not writing.[/dim]")
        return

    project_path.write_text(json.dumps(project, indent=2))
    console.print(f"[green]Done.[/green] Open DJ Studio to see '{mix_name}' in your projects.")

"""Phase-2 enrichment: populate mik_key, mik_nrg, vocals, drums, melody from DJ Studio library."""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from detect import db as detect_db
from djstudio.keys import CAMELOT_MAP

console = Console()

_DJ_DB = Path.home() / "Music" / "DJ.Studio" / "Database"
_STEMS = {
    "vocals": _DJ_DB / "audio-library-compressedAudioViewVocals",
    "drums":  _DJ_DB / "audio-library-compressedAudioViewDrums",
    "melody": _DJ_DB / "audio-library-compressedAudioViewMelody",
}


def _camelot(key_num) -> Optional[str]:
    if key_num is None:
        return None
    try:
        return CAMELOT_MAP.get(int(key_num))
    except (TypeError, ValueError):
        return None


def _find_shard_file(base_dir: Path, library_key: str) -> Optional[Path]:
    if not base_dir.is_dir():
        return None
    for shard in base_dir.iterdir():
        if not shard.is_dir():
            continue
        candidate = shard / library_key
        if candidate.is_file():
            return candidate
    return None


def _stem_ratio(path: Path) -> Optional[float]:
    """compressedAudioView format: 2-byte sentinel + 8-byte records; field[3] is amplitude."""
    try:
        data = path.read_bytes()
        payload = data[2:]
        n = len(payload) // 8
        if n == 0:
            return None
        total = sum(struct.unpack_from("<H", payload, i * 8 + 6)[0] for i in range(n))
        return (total / n) / 65535.0
    except Exception:
        return None


def _ratio_to_intensity(ratio: float) -> str:
    if ratio < 0.03:
        return "none"
    if ratio < 0.10:
        return "low"
    if ratio < 0.25:
        return "medium"
    return "high"


def _read_stems(library_key: str) -> dict:
    result = {}
    for stem, base_dir in _STEMS.items():
        path = _find_shard_file(base_dir, library_key)
        if path is None:
            continue
        ratio = _stem_ratio(path)
        if ratio is not None:
            result[stem] = _ratio_to_intensity(ratio)
    return result


def run_enrich_studio(dry_run: bool, limit: int, verbose: bool) -> None:
    if dry_run:
        console.print("[yellow]DRY RUN[/yellow] — no changes will be made")

    console.print("Loading DJ Studio audio library…")
    from djstudio.extractor import DJStudioMixExtractor
    extractor = DJStudioMixExtractor()
    library = extractor.audio_library

    if not library:
        console.print(f"[red]No DJ Studio library found at[/red] {_DJ_DB / 'audio-library-table'}")
        sys.exit(1)

    console.print(f"[dim]{len(library)} tracks in DJ Studio library[/dim]")

    tracks = detect_db.get_studio_enrichable_tracks()
    if limit:
        tracks = tracks[:limit]

    if not tracks:
        console.print("Nothing to enrich — all matched tracks already have DJ Studio data.")
        return

    console.print(f"[bold]{len(tracks)}[/bold] tracks to enrich with DJ Studio data")

    counts = {"seen": 0, "updated": 0, "not_in_library": 0}

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        task = progress.add_task("Enriching…", total=len(tracks))

        for row in tracks:
            counts["seen"] += 1
            progress.update(task, advance=1)

            beatport_id = row["beatport_id"]
            enriched_id = row["id"]
            artist = row["artist"] or ""
            title = row["title"] or ""

            progress.update(task, description=f"{artist} — {title}")

            lib_key = f"beatport-sdk_{beatport_id}"
            track = library.get(lib_key)

            if track is None:
                counts["not_in_library"] += 1
                if verbose:
                    progress.log(f"[yellow]not in library:[/yellow] {artist} — {title}  (bp:{beatport_id})")
                continue

            mik_key = _camelot(track.get("mikKey") or track.get("camelotKey"))
            mik_nrg_raw = track.get("mikEnergy")
            try:
                mik_nrg = int(mik_nrg_raw) if mik_nrg_raw is not None else None
                if mik_nrg is not None and not 1 <= mik_nrg <= 10:
                    mik_nrg = None
            except (TypeError, ValueError):
                mik_nrg = None

            stems = _read_stems(lib_key)

            data = {
                "mik_key": mik_key,
                "mik_nrg": mik_nrg,
                "vocals": stems.get("vocals"),
                "drums": stems.get("drums"),
                "melody": stems.get("melody"),
            }

            if dry_run:
                if verbose:
                    progress.log(
                        f"[green]would update:[/green] {artist} — {title}  "
                        f"key={mik_key} nrg={mik_nrg} "
                        f"vocals={stems.get('vocals')} drums={stems.get('drums')} melody={stems.get('melody')}"
                    )
                counts["updated"] += 1
                continue

            detect_db.update_studio_enrich(enriched_id, data)
            counts["updated"] += 1
            if verbose:
                progress.log(
                    f"[green]updated:[/green] {artist} — {title}  "
                    f"key={mik_key} nrg={mik_nrg} "
                    f"vocals={stems.get('vocals')} drums={stems.get('drums')} melody={stems.get('melody')}"
                )

    console.print()
    console.print(f"[bold]Enrich-studio {'(dry run) ' if dry_run else ''}complete[/bold]")
    console.print(f"  Seen:            {counts['seen']}")
    console.print(f"  Updated:         {counts['updated']}")
    console.print(f"  Not in library:  {counts['not_in_library']}")

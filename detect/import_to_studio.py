"""Pipeline: enriched_tracks → Beatport preview MP3 → MIK 11 → DJ Studio library JSON.

Flow:
  1. Read tracks from the target table where mik_key IS NULL.
  2. For each: fetch Beatport preview URL via /catalog/tracks/{id}/.
  3. Download preview MP3 to a temp dir; set ID3 TIT2 = 'beatport_{id}' for matching.
  4. Open all files in Mixed In Key 11 (background/hidden launch).
  5. Poll ~/Library/Application Support/Mixedinkey/Collection11.mikdb until results
     appear (ZSONG rows where ZNAME = 'beatport_{id}').
  6. Write minimal DJ Studio audio-library-table JSON entry for each matched track.
  7. Caller then runs `dj detect enrich-studio [--test]` to copy mik_key/mik_nrg
     back into the table.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
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

from connections import beatport as bp_api
from detect import db as detect_db
from detect.enrich import _get_token

console = Console()

MIK_APP = "/Applications/Mixed In Key 11.app"
MIK_DB  = Path.home() / "Library" / "Application Support" / "Mixedinkey" / "Collection11.mikdb"
DJ_STUDIO_LIBRARY = Path.home() / "Music" / "DJ.Studio" / "Database" / "audio-library-table"

# Verified against real DJ.Studio audio-library-table entries.
MIK_CAMELOT: dict[int, str] = {
    0: "8B",  1: "3B",  2: "10B", 3: "5B",  4: "12B", 5: "7B",
    6: "2B",  7: "9B",  8: "4B",  9: "11B", 10: "6B", 11: "1B",
    12: "8A", 13: "3A", 14: "10A", 15: "5A", 16: "12A", 17: "7A",
    18: "2A", 19: "9A", 20: "4A", 21: "11A", 22: "6A", 23: "1A",
}

KIND = "beatport-sdk"


def _fetch_preview_url(beatport: bp_api.Beatport, track_id: int) -> Optional[str]:
    return beatport.preview_url(track_id)


def _download_preview(
    url: str, dest: Path, *, beatport_id: int, artist: str, title: str
) -> bool:
    """Stream preview MP3 to dest, then tag TIT2 = 'beatport_{id}'. Returns True on success."""
    try:
        with httpx.stream("GET", url, timeout=30, follow_redirects=True) as r:
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes(65536):
                    fh.write(chunk)
    except Exception as e:
        console.log(f"[yellow]download failed bp:{beatport_id}: {e}[/yellow]")
        return False

    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1
        try:
            tags = ID3(dest)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("TIT2")
        tags.delall("TPE1")
        tags.add(TIT2(encoding=3, text=f"beatport_{beatport_id}"))
        tags.add(TPE1(encoding=3, text=artist or ""))
        tags.save(dest, v2_version=3)
    except Exception as e:
        console.log(f"[yellow]id3 tag failed bp:{beatport_id}: {e}[/yellow]")
        # non-fatal — filename also encodes the id

    return True


def _open_in_mik(files: list[Path]) -> None:
    if not Path(MIK_APP).exists():
        raise RuntimeError(f"Mixed In Key 11 not found at {MIK_APP}")
    if not files:
        return
    # -g: don't bring MIK to foreground  -j: launch hidden
    subprocess.run(
        ["open", "-gj", "-a", MIK_APP, *[str(f) for f in files]],
        check=True,
    )


def _read_mik_results(beatport_ids: set[int]) -> dict[int, tuple[int, int]]:
    """Read ZSONG rows matching 'beatport_{id}'. Returns {beatport_id: (mik_key_int, mik_nrg_int)}."""
    if not MIK_DB.exists():
        return {}
    out: dict[int, tuple[int, int]] = {}
    uri = f"file:{MIK_DB}?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        rows = con.execute(
            "SELECT ZNAME, ZKEY, ZENERGY FROM ZSONG "
            "WHERE ZNAME LIKE 'beatport\\_%' ESCAPE '\\' "
            "AND ZKEY IS NOT NULL AND ZENERGY IS NOT NULL"
        ).fetchall()
        con.close()
    except Exception:
        return {}

    for name, zkey, zenergy in rows:
        if not name or not name.startswith("beatport_"):
            continue
        try:
            bid = int(name.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        if bid not in beatport_ids:
            continue
        try:
            mik_key_int = int(str(zkey).strip())
            mik_nrg_int = int(round(float(zenergy)))
        except (TypeError, ValueError):
            continue
        if 0 <= mik_key_int <= 23 and 1 <= mik_nrg_int <= 10:
            out[bid] = (mik_key_int, mik_nrg_int)
    return out


def _poll_mik(
    beatport_ids: set[int], *, timeout_s: int
) -> dict[int, tuple[int, int]]:
    found: dict[int, tuple[int, int]] = {}
    deadline = time.time() + timeout_s
    last_count = -1
    while time.time() < deadline:
        found = _read_mik_results(beatport_ids)
        if len(found) != last_count:
            console.log(
                f"[dim]  MIK progress: {len(found)}/{len(beatport_ids)} analysed[/dim]"
            )
            last_count = len(found)
        if len(found) >= len(beatport_ids):
            break
        time.sleep(3)
    return found


def _shard(library_key: str) -> str:
    """Any 2-hex shard name works — DJ Studio scans all shards on load."""
    return hashlib.sha1(library_key.encode()).hexdigest()[:2]


def _write_library_entry(
    *,
    beatport_id: int,
    artist: str,
    title: str,
    bpm: Optional[float],
    mik_key_int: int,
    mik_nrg_int: int,
) -> Path:
    library_key = f"{KIND}_{beatport_id}"
    camelot_str = MIK_CAMELOT[mik_key_int]
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    entry: dict = {
        "key":   library_key,
        "name":  title or "",
        "kind":  KIND,
        "size":  0,
        "fileHash": "",
        "type":  "",
        "lastModified": now_iso,
        "importDate":   now_iso,
        "rating": 0, "method": 0,
        "inLibrary": True, "isTemporary": False,
        "tag": {
            "genre": "", "artist": artist or "", "album": "",
            "track": "", "title": title or "", "year": "",
            "bpm": str(int(round(bpm))) if bpm else "",
            "key": camelot_str, "comment": "",
            "premium": False, "releaseYear": "", "label": "",
        },
        "mikKey":   mik_key_int,
        "mikEnergy": mik_nrg_int,
        "energy": 0, "danceability": 0, "mood": 0,
        "duration": 0, "sampleRate": 44100,
        "pictureType": "",
        "image": {"type": "image/jpeg"},
        "image64": {"type": "image/jpeg"},
        "image512": {"type": "image/jpeg"},
        "imageUrl": "",
        "bpm": float(bpm) if bpm else 0,
        "camelotKey":         mik_key_int,
        "originalCamelotKey": mik_key_int,
        "noteKey":            mik_key_int,
        "autoGainCalculated": False,
        "autoGain": 1,
        "cueData": {
            "loopMode": 0,
            "systemCuePoints": [], "hotCuePoints": [], "memCuePoints": [],
        },
        "externalRec": {"UUID": str(beatport_id), "fileLocationPath": ""},
        "analyzeVersion": "",
        "mixedInKeyAnalyzeVersion": "1",
        "rekordboxAnalyzeVersion": "",
        "seratoAnalyzeVersion": "",
        "structureKey": library_key,
        "audioCleaned": False, "cleanedVersion": "",
        "beatQuantize": True,
        "bpmLine": [], "beatGrids": [],
        "beatDataSource": "fixed",
        "originalAudiofileRecordKey": "",
        "bpmMultiplier": 1, "stemsType": "", "sourceKind": "",
    }

    # Merge into existing entry if present (preserves beatGrids, cueData, etc.)
    shard_dir = DJ_STUDIO_LIBRARY / _shard(library_key)
    shard_dir.mkdir(parents=True, exist_ok=True)
    out_path = shard_dir / library_key

    if not out_path.exists():
        # Also check other shards in case DJ Studio already has it
        for shard in DJ_STUDIO_LIBRARY.iterdir():
            if not shard.is_dir():
                continue
            candidate = shard / library_key
            if candidate.is_file():
                out_path = candidate
                break

    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            existing.update({
                "mikKey": mik_key_int,
                "mikEnergy": mik_nrg_int,
                "camelotKey": mik_key_int,
                "originalCamelotKey": mik_key_int,
                "noteKey": mik_key_int,
                "mixedInKeyAnalyzeVersion": "1",
            })
            existing.setdefault("tag", {})["key"] = camelot_str
            out_path.write_text(json.dumps(existing, separators=(",", ":")))
            return out_path
        except Exception:
            pass

    out_path.write_text(json.dumps(entry, separators=(",", ":")))
    return out_path


def run_import_to_studio(
    *,
    table: str = "enriched_tracks_test",
    limit: int = 0,
    timeout_s: int = 600,
    keep_temp: bool = False,
    verbose: bool = False,
) -> None:
    console.print(f"[bold]import-to-studio[/bold] ← [cyan]{table}[/cyan]")

    rows = detect_db.get_studio_enrichable_tracks(table=table)
    if limit:
        rows = rows[:limit]

    if not rows:
        console.print("Nothing to import — every row already has mik_key.")
        return

    console.print(f"{len(rows)} tracks to process")

    token = _get_token()
    http_client = bp_api.make_client(token)
    beatport = bp_api.Beatport(client=http_client)

    tmp_root = Path(tempfile.mkdtemp(prefix="mik_import_"))
    console.print(f"[dim]Temp dir: {tmp_root}[/dim]")

    downloaded: list[Path] = []
    bp_ids: set[int] = set()
    track_meta: dict[int, dict] = {}

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeElapsedColumn(),
        console=console,
    )

    try:
        with progress:
            t = progress.add_task("Downloading previews…", total=len(rows))
            for row in rows:
                bid = row["beatport_id"]
                artist = row["artist"] or ""
                title  = row["title"]  or ""
                bpm    = row["bpm"]
                progress.update(t, advance=1, description=f"{artist} — {title}")

                url = _fetch_preview_url(beatport, bid)
                if not url:
                    if verbose:
                        progress.log(f"[yellow]no preview:[/yellow] bp:{bid} {artist} — {title}")
                    continue

                fp = tmp_root / f"beatport_{bid}.mp3"
                if not _download_preview(url, fp, beatport_id=bid, artist=artist, title=title):
                    continue

                downloaded.append(fp)
                bp_ids.add(bid)
                track_meta[bid] = {"artist": artist, "title": title, "bpm": bpm}

        if not downloaded:
            console.print("[red]No previews downloaded — aborting.[/red]")
            return

        console.print(f"[green]✓[/green] Downloaded {len(downloaded)} previews")

        console.print("[bold]Opening in Mixed In Key 11…[/bold] (background)")
        _open_in_mik(downloaded)
        console.print(
            "[yellow]MIK is analysing in the background.[/yellow] "
            "Do not close MIK until polling completes."
        )

        console.print(f"Polling [dim]{MIK_DB.name}[/dim] (timeout={timeout_s}s)…")
        results = _poll_mik(bp_ids, timeout_s=timeout_s)
        console.print(f"[green]✓[/green] MIK analysed {len(results)}/{len(bp_ids)} tracks")

        if not results:
            console.print(
                "[red]No MIK results — try increasing --timeout, or "
                "open MIK manually and confirm tracks appear in its list.[/red]"
            )
            return

        wrote = 0
        for bid, (mk, mn) in results.items():
            meta = track_meta.get(bid, {})
            try:
                p = _write_library_entry(
                    beatport_id=bid,
                    artist=meta.get("artist", ""),
                    title=meta.get("title", ""),
                    bpm=meta.get("bpm"),
                    mik_key_int=mk,
                    mik_nrg_int=mn,
                )
                wrote += 1
                if verbose:
                    console.log(
                        f"[green]wrote:[/green] {p.name}  "
                        f"key={MIK_CAMELOT[mk]} nrg={mn}"
                    )
            except Exception as e:
                console.log(f"[red]write failed bp:{bid}: {e}[/red]")

        missing = bp_ids - set(results)
        if missing:
            ids_str = str(sorted(missing)[:10]) + (" …" if len(missing) > 10 else "")
            console.print(
                f"[yellow]MIK did not analyse {len(missing)} tracks "
                f"(timeout/skip): {ids_str}[/yellow]"
            )

        console.print()
        console.print(f"[bold]Done.[/bold] Wrote {wrote} DJ Studio library entries.")
        console.print(
            "[dim]Next:[/dim] "
            f"[cyan]uv run dj_cli.py detect enrich-studio{'  --test' if 'test' in table else ''}[/cyan]"
        )

    finally:
        http_client.close()
        if keep_temp:
            console.print(f"[dim]Kept temp dir: {tmp_root}[/dim]")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

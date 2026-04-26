#!/usr/bin/env python3
"""
Track Metadata Database

Stores extended track metadata alongside DJ Studio's library: energy level,
vocals/drums/melody intensity, and section markers (intro, buildup, drop, etc.).

The SQLite database lives at ~/Music/DJ.Studio/track_metadata.db.
Run `populate` to seed it from DJ Studio's audio-library-table, then
annotate tracks with `update` and mark sections with `section add`.

Usage:
    uv run track_db.py populate
    uv run track_db.py list
    uv run track_db.py show LIBRARY_KEY
    uv run track_db.py update LIBRARY_KEY --energy 8 --vocals high --drums high
    uv run track_db.py section add LIBRARY_KEY drop 128 256
    uv run track_db.py section list LIBRARY_KEY
    uv run track_db.py section remove SECTION_ID
"""

import datetime
import json
import re
import sqlite3
import sys
import argparse
from pathlib import Path
from typing import Optional


DB_PATH = Path.home() / "Music" / "DJ.Studio" / "track_metadata.db"
_DJ_DB   = Path.home() / "Music" / "DJ.Studio" / "Database"
DJ_STUDIO_LIBRARY    = _DJ_DB / "audio-library-table"
DJ_STUDIO_STRUCTURES = _DJ_DB / "track-structures-table"
DJ_STUDIO_STEMS = {
    "vocals": _DJ_DB / "audio-library-compressedAudioViewVocals",
    "drums":  _DJ_DB / "audio-library-compressedAudioViewDrums",
    "melody": _DJ_DB / "audio-library-compressedAudioViewMelody",
}

SECTION_TYPES = {"intro", "buildup", "drop", "breakdown", "outro", "bridge", "verse", "chorus"}
INTENSITY_LEVELS = {"none", "low", "medium", "high"}

CAMELOT_MAP = {
    1: "8B", 2: "3B", 3: "10B", 4: "5B", 5: "12B", 6: "7B",
    7: "2B", 8: "9B", 9: "4B", 10: "11B", 11: "6B", 12: "1B",
    13: "5A", 14: "12A", 15: "7A", 16: "2A", 17: "9A", 18: "4A",
    19: "11A", 20: "6A", 21: "1A", 22: "8A", 23: "3A", 24: "10A",
}


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracks (
            id           INTEGER PRIMARY KEY,
            library_key  TEXT UNIQUE NOT NULL,
            beatport_id  TEXT,
            beatport_url TEXT,
            title        TEXT NOT NULL,
            artist       TEXT NOT NULL,
            genre        TEXT,
            key          TEXT,
            bpm          REAL,
            energy       INTEGER CHECK(energy BETWEEN 1 AND 10),
            vocals       TEXT CHECK(vocals IN ('none', 'low', 'medium', 'high')),
            drums        TEXT CHECK(drums  IN ('none', 'low', 'medium', 'high')),
            melody       TEXT CHECK(melody IN ('none', 'low', 'medium', 'high')),
            notes        TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_sections (
            id           INTEGER PRIMARY KEY,
            track_id     INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            section_type TEXT NOT NULL CHECK(section_type IN (
                'intro', 'buildup', 'drop', 'breakdown', 'outro',
                'bridge', 'verse', 'chorus'
            )),
            start_beat   REAL NOT NULL,
            end_beat     REAL,
            notes        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_beatport  ON tracks(beatport_id);
        CREATE INDEX IF NOT EXISTS idx_sections_track   ON track_sections(track_id);
        CREATE INDEX IF NOT EXISTS idx_sections_type    ON track_sections(section_type);
    """)
    # Migration: add beatport_url to databases created before this column existed
    try:
        conn.execute("ALTER TABLE tracks ADD COLUMN beatport_url TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()


# ── DJ Studio helpers ────────────────────────────────────────────────────────

def load_dj_studio_library() -> dict:
    library: dict = {}
    if not DJ_STUDIO_LIBRARY.is_dir():
        return library
    for shard in DJ_STUDIO_LIBRARY.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if not entry.is_file():
                continue
            try:
                with open(entry) as f:
                    track = json.load(f)
                    library[track["key"]] = track
            except Exception:
                continue
    return library


def camelot_key(key_num) -> Optional[str]:
    if key_num is None:
        return None
    try:
        return CAMELOT_MAP.get(int(key_num))
    except (TypeError, ValueError):
        return None


def beatport_url(beatport_id: str, title: str) -> str:
    """Generate a Beatport track URL from ID + title slug.

    Beatport canonical format: https://www.beatport.com/track/{slug}/{id}
    The slug is derived from the title; populate sets it automatically and
    users can override with --beatport-url if the slug is wrong.
    """
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)   # strip punctuation
    slug = re.sub(r"[\s_]+", "-", slug)    # spaces/underscores → hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"https://www.beatport.com/track/{slug}/{beatport_id}"


def load_dj_studio_structures() -> dict:
    """Load track-structures-table, keyed by structureKey (== library_key)."""
    structures: dict = {}
    if not DJ_STUDIO_STRUCTURES.is_dir():
        return structures
    for shard in DJ_STUDIO_STRUCTURES.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if not entry.is_file():
                continue
            try:
                with open(entry) as f:
                    data = json.load(f)
                    structures[data["key"]] = data
            except Exception:
                continue
    return structures


def _find_shard_file(base_dir: Path, library_key: str) -> Optional[Path]:
    """Search sharded directory for a file matching library_key."""
    if not base_dir.is_dir():
        return None
    for shard in base_dir.iterdir():
        if not shard.is_dir():
            continue
        candidate = shard / library_key
        if candidate.is_file():
            return candidate
    return None


def _stem_energy_ratio(path: Path) -> Optional[float]:
    """Read a compressedAudioView file and return mean energy as 0–1 ratio.

    Format: 2-byte sentinel (0xffff), then records of 8 bytes each.
    Each record is 4 uint16 LE values; field[3] is the energy amplitude.
    """
    try:
        import struct as _struct
        data = path.read_bytes()
        payload = data[2:]  # skip 0xffff header
        n = len(payload) // 8
        if n == 0:
            return None
        total = sum(_struct.unpack_from("<H", payload, i * 8 + 6)[0] for i in range(n))
        return (total / n) / 65535.0
    except Exception:
        return None


# Calibrated from 18 tracks with stem data: vocals 0-0.16, drums 0.04-0.42, melody 0-0.52
def _ratio_to_intensity(ratio: float) -> str:
    if ratio < 0.03:
        return "none"
    if ratio < 0.10:
        return "low"
    if ratio < 0.25:
        return "medium"
    return "high"


def read_stem_intensities(library_key: str) -> dict:
    """Return vocals/drums/melody intensity strings for a track, or {} if no stem data."""
    result = {}
    for stem, base_dir in DJ_STUDIO_STEMS.items():
        path = _find_shard_file(base_dir, library_key)
        if path is None:
            continue
        ratio = _stem_energy_ratio(path)
        if ratio is not None:
            result[stem] = _ratio_to_intensity(ratio)
    return result


def _infer_section_type(label: int, nr: int, total: int) -> str:
    """Map a MIK energy label (1-10) + position to a section type.

    First segment is always intro, last is always outro. For the middle,
    the label drives the type: high energy → drop, rising → buildup,
    low → breakdown, mid → verse.
    """
    if nr == 0:
        return "intro"
    if nr == total - 1:
        return "outro"
    if label >= 8:
        return "drop"
    if label >= 6:
        return "buildup"
    if label <= 4:
        return "breakdown"
    return "verse"


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_populate(conn: sqlite3.Connection, args):
    library = load_dj_studio_library()
    if not library:
        print(f"No DJ Studio library found at {DJ_STUDIO_LIBRARY}", file=sys.stderr)
        sys.exit(1)

    structures = load_dj_studio_structures()

    now = datetime.datetime.now().isoformat()
    inserted = skipped = sections_added = 0

    for lib_key, track in library.items():
        tag = track.get("tag", {})
        title = tag.get("title", "Unknown")
        artist = tag.get("artist", "Unknown")
        genre = tag.get("genre", "")
        bpm = track.get("bpm")
        key = camelot_key(track.get("camelotKey"))

        # mikEnergy is 1-10 from Mixed In Key analysis — use as-is
        mik_energy = track.get("mikEnergy") or None
        if mik_energy is not None:
            try:
                mik_energy = int(mik_energy)
                if not 1 <= mik_energy <= 10:
                    mik_energy = None
            except (TypeError, ValueError):
                mik_energy = None

        beatport_id = None
        bp_url = None
        if lib_key.startswith("beatport-sdk_"):
            beatport_id = lib_key.split("_", 1)[1]
            bp_url = beatport_url(beatport_id, title)

        stems = read_stem_intensities(lib_key)

        try:
            conn.execute(
                """INSERT INTO tracks
                       (library_key, beatport_id, beatport_url, title, artist, genre, key, bpm,
                        energy, vocals, drums, melody, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(library_key) DO UPDATE SET
                       title=excluded.title, artist=excluded.artist, genre=excluded.genre,
                       key=excluded.key, bpm=excluded.bpm, beatport_id=excluded.beatport_id,
                       beatport_url=COALESCE(tracks.beatport_url, excluded.beatport_url),
                       energy=COALESCE(tracks.energy,  excluded.energy),
                       vocals=COALESCE(tracks.vocals,  excluded.vocals),
                       drums=COALESCE(tracks.drums,    excluded.drums),
                       melody=COALESCE(tracks.melody,  excluded.melody),
                       updated_at=excluded.updated_at""",
                (lib_key, beatport_id, bp_url, title, artist, genre, key, bpm,
                 mik_energy, stems.get("vocals"), stems.get("drums"), stems.get("melody"),
                 now, now),
            )
            inserted += 1
        except Exception as e:
            print(f"Warning: could not insert {lib_key}: {e}", file=sys.stderr)
            skipped += 1
            continue

        # Import MIK energy segments as sections — skip if track already has any sections
        structure_key = track.get("structureKey", lib_key)
        struct = structures.get(structure_key, {})
        energy_segments = struct.get("energyLevelData", [])
        if not energy_segments:
            continue

        row = conn.execute("SELECT id FROM tracks WHERE library_key = ?", (lib_key,)).fetchone()
        if not row:
            continue
        track_id = row["id"]

        existing = conn.execute(
            "SELECT COUNT(*) FROM track_sections WHERE track_id = ?", (track_id,)
        ).fetchone()[0]
        if existing:
            continue

        total = len(energy_segments)
        for seg in energy_segments:
            try:
                label = int(seg.get("label", 5))
            except (TypeError, ValueError):
                label = 5
            nr = seg.get("nr", 0)
            start_beat = seg.get("startBeatNr", 0)
            end_beat = start_beat + seg.get("beatLength", 0)
            section_type = _infer_section_type(label, nr, total)
            conn.execute(
                "INSERT INTO track_sections (track_id, section_type, start_beat, end_beat, notes) VALUES (?, ?, ?, ?, ?)",
                (track_id, section_type, float(start_beat), float(end_beat), f"MIK energy {label}/10"),
            )
            sections_added += 1

    conn.commit()
    print(f"Populated {inserted} tracks ({skipped} errors), {sections_added} sections. DB: {DB_PATH}")


def cmd_list(conn: sqlite3.Connection, args):
    rows = conn.execute("""
        SELECT t.library_key, t.artist, t.title, t.key, t.bpm, t.energy,
               t.vocals, t.drums, t.melody,
               COUNT(s.id) AS sections
          FROM tracks t
          LEFT JOIN track_sections s ON s.track_id = t.id
         GROUP BY t.id
         ORDER BY t.artist, t.title
    """).fetchall()

    if not rows:
        print("No tracks in database. Run: uv run track_db.py populate")
        return

    # Legend: E=energy, V/D/M = vocals/drums/melody first letter (N/L/M/H), Sec=section count
    print(f"\n{'Artist':<30} {'Title':<35} {'Key':<5} {'BPM':<7} {'E':<3} {'V':<3} {'D':<3} {'M':<3} Sec")
    print("-" * 105)
    for r in rows:
        e = str(r["energy"]) if r["energy"] is not None else "-"
        v = r["vocals"][0].upper() if r["vocals"] else "-"
        d = r["drums"][0].upper()  if r["drums"]  else "-"
        m = r["melody"][0].upper() if r["melody"] else "-"
        bpm_str = f"{r['bpm']:.1f}" if r["bpm"] else "-"
        print(
            f"{r['artist']:<30} {r['title']:<35} {r['key'] or '-':<5} "
            f"{bpm_str:<7} {e:<3} {v:<3} {d:<3} {m:<3} {r['sections']}"
        )
    print(f"\n{len(rows)} tracks  |  V/D/M: N=none L=low M=medium H=high")


def _find_track(conn: sqlite3.Connection, library_key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tracks WHERE library_key = ?", (library_key,)).fetchone()


def cmd_show(conn: sqlite3.Connection, args):
    track = _find_track(conn, args.library_key)
    if not track:
        print(f"Track not found: {args.library_key}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"{track['artist']} — {track['title']}")
    print(f"{'=' * 60}")
    print(f"Library key : {track['library_key']}")
    if track["beatport_id"]:
        print(f"Beatport ID : {track['beatport_id']}")
    if track["beatport_url"]:
        print(f"Beatport    : {track['beatport_url']}")
    print(f"Genre       : {track['genre'] or '-'}")
    print(f"Key         : {track['key'] or '-'}")
    print(f"BPM         : {track['bpm'] or '-'}")
    print(f"Energy      : {track['energy'] or '-'}/10")
    print(f"Vocals      : {track['vocals'] or '-'}")
    print(f"Drums       : {track['drums'] or '-'}")
    print(f"Melody      : {track['melody'] or '-'}")
    if track["notes"]:
        print(f"Notes       : {track['notes']}")

    sections = conn.execute(
        "SELECT * FROM track_sections WHERE track_id = ? ORDER BY start_beat",
        (track["id"],),
    ).fetchall()

    if sections:
        print(f"\nSections:")
        print(f"  {'ID':<5} {'Type':<12} {'Start':<10} {'End':<10} Notes")
        print(f"  {'-' * 55}")
        for s in sections:
            end = f"{s['end_beat']:.1f}" if s["end_beat"] is not None else "-"
            print(f"  {s['id']:<5} {s['section_type']:<12} {s['start_beat']:<10.1f} {end:<10} {s['notes'] or ''}")
    else:
        print("\nNo sections defined.")


def cmd_update(conn: sqlite3.Connection, args):
    track = _find_track(conn, args.library_key)
    if not track:
        print(f"Track not found: {args.library_key}", file=sys.stderr)
        sys.exit(1)

    updates: dict = {}
    if args.energy is not None:
        if not 1 <= args.energy <= 10:
            print("Energy must be 1-10", file=sys.stderr)
            sys.exit(1)
        updates["energy"] = args.energy
    if args.vocals is not None:
        updates["vocals"] = args.vocals
    if args.drums is not None:
        updates["drums"] = args.drums
    if args.melody is not None:
        updates["melody"] = args.melody
    if args.notes is not None:
        updates["notes"] = args.notes
    if args.key is not None:
        updates["key"] = args.key
    if args.bpm is not None:
        updates["bpm"] = args.bpm
    if args.beatport_url is not None:
        updates["beatport_url"] = args.beatport_url

    if not updates:
        print("Nothing to update. Use --energy, --vocals, --drums, --melody, --notes, --key, --bpm, or --beatport-url")
        return

    updates["updated_at"] = datetime.datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE tracks SET {set_clause} WHERE library_key = ?",
        [*updates.values(), args.library_key],
    )
    conn.commit()
    print(f"Updated: {track['artist']} — {track['title']}")
    for k, v in updates.items():
        if k != "updated_at":
            print(f"  {k} = {v}")


def cmd_section_add(conn: sqlite3.Connection, args):
    track = _find_track(conn, args.library_key)
    if not track:
        print(f"Track not found: {args.library_key}", file=sys.stderr)
        sys.exit(1)

    if args.type not in SECTION_TYPES:
        print(f"Section type must be one of: {', '.join(sorted(SECTION_TYPES))}", file=sys.stderr)
        sys.exit(1)

    conn.execute(
        "INSERT INTO track_sections (track_id, section_type, start_beat, end_beat, notes) VALUES (?, ?, ?, ?, ?)",
        (track["id"], args.type, args.start_beat, args.end_beat, args.notes),
    )
    conn.commit()
    end_str = f" → {args.end_beat}" if args.end_beat is not None else ""
    print(f"Added {args.type} at beat {args.start_beat}{end_str}  ({track['artist']} — {track['title']})")


def cmd_section_list(conn: sqlite3.Connection, args):
    track = _find_track(conn, args.library_key)
    if not track:
        print(f"Track not found: {args.library_key}", file=sys.stderr)
        sys.exit(1)

    sections = conn.execute(
        "SELECT * FROM track_sections WHERE track_id = ? ORDER BY start_beat",
        (track["id"],),
    ).fetchall()

    print(f"\n{track['artist']} — {track['title']}")
    if not sections:
        print("  No sections defined.")
        return

    print(f"\n  {'ID':<5} {'Type':<12} {'Start Beat':<12} {'End Beat':<12} Notes")
    print(f"  {'-' * 60}")
    for s in sections:
        end = f"{s['end_beat']:.1f}" if s["end_beat"] is not None else "-"
        print(f"  {s['id']:<5} {s['section_type']:<12} {s['start_beat']:<12.1f} {end:<12} {s['notes'] or ''}")


def cmd_section_remove(conn: sqlite3.Connection, args):
    row = conn.execute(
        "SELECT s.*, t.artist, t.title FROM track_sections s JOIN tracks t ON t.id = s.track_id WHERE s.id = ?",
        (args.section_id,),
    ).fetchone()
    if not row:
        print(f"Section {args.section_id} not found", file=sys.stderr)
        sys.exit(1)
    conn.execute("DELETE FROM track_sections WHERE id = ?", (args.section_id,))
    conn.commit()
    print(f"Removed {row['section_type']} at beat {row['start_beat']}  ({row['artist']} — {row['title']})")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Track metadata database for DJ workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  populate                              Import tracks from DJ Studio library
  list                                  List all tracks (E=energy, V/D/M=vocals/drums/melody)
  show LIBRARY_KEY                      Full track details + sections
  update LIBRARY_KEY [options]          Set energy, vocals, drums, melody, key, bpm, notes
  section add LIBRARY_KEY TYPE START [END]  Add a section marker
  section list LIBRARY_KEY              List section markers
  section remove SECTION_ID            Remove a section marker

Section types : intro  buildup  drop  breakdown  outro  bridge  verse  chorus
Intensity     : none   low      medium   high

Examples:
  uv run track_db.py populate
  uv run track_db.py show beatport-sdk_12345678
  uv run track_db.py update beatport-sdk_12345678 --energy 8 --vocals high --drums high --melody low
  uv run track_db.py section add beatport-sdk_12345678 intro 0 64
  uv run track_db.py section add beatport-sdk_12345678 buildup 192 256
  uv run track_db.py section add beatport-sdk_12345678 drop 256
  uv run track_db.py section list beatport-sdk_12345678
  uv run track_db.py section remove 3
        """,
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("populate", help="Import tracks from DJ Studio library")
    subparsers.add_parser("list", help="List all tracks")

    show_p = subparsers.add_parser("show", help="Show track details")
    show_p.add_argument("library_key")

    update_p = subparsers.add_parser("update", help="Update track metadata")
    update_p.add_argument("library_key")
    update_p.add_argument("--energy", type=int, metavar="1-10")
    update_p.add_argument("--vocals", choices=sorted(INTENSITY_LEVELS))
    update_p.add_argument("--drums",  choices=sorted(INTENSITY_LEVELS))
    update_p.add_argument("--melody", choices=sorted(INTENSITY_LEVELS))
    update_p.add_argument("--key",          help="Camelot key override (e.g. 11A)")
    update_p.add_argument("--bpm",          type=float)
    update_p.add_argument("--notes",        help="Free-form notes")
    update_p.add_argument("--beatport-url", dest="beatport_url", help="Full Beatport track URL")

    section_p = subparsers.add_parser("section", help="Manage section markers")
    section_sub = section_p.add_subparsers(dest="section_command")

    sec_add = section_sub.add_parser("add", help="Add a section marker")
    sec_add.add_argument("library_key")
    sec_add.add_argument("type", choices=sorted(SECTION_TYPES), metavar="TYPE")
    sec_add.add_argument("start_beat", type=float, metavar="START_BEAT")
    sec_add.add_argument("end_beat",   type=float, nargs="?", metavar="END_BEAT")
    sec_add.add_argument("--notes")

    sec_list = section_sub.add_parser("list", help="List sections for a track")
    sec_list.add_argument("library_key")

    sec_rm = section_sub.add_parser("remove", help="Remove a section")
    sec_rm.add_argument("section_id", type=int)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    conn = get_db()
    init_db(conn)

    try:
        if args.command == "populate":
            cmd_populate(conn, args)
        elif args.command == "list":
            cmd_list(conn, args)
        elif args.command == "show":
            cmd_show(conn, args)
        elif args.command == "update":
            cmd_update(conn, args)
        elif args.command == "section":
            if not args.section_command:
                section_p.print_help()
            elif args.section_command == "add":
                cmd_section_add(conn, args)
            elif args.section_command == "list":
                cmd_section_list(conn, args)
            elif args.section_command == "remove":
                cmd_section_remove(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

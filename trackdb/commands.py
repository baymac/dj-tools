"""`dj db` commands: populate, list, show, update, section add/list/remove."""

import datetime
import sqlite3
import sys
from typing import Optional

from .beatport import fetch_release_date, load_token
from .library import (
    beatport_url,
    camelot_key,
    infer_section_type,
    load_dj_studio_library,
    load_dj_studio_structures,
    mix_track_keys,
    read_stem_intensities,
    DJ_STUDIO_LIBRARY,
)
from .schema import DB_PATH, SECTION_TYPES


def _find_track(conn: sqlite3.Connection, library_key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tracks WHERE library_key = ?", (library_key,)).fetchone()


def cmd_populate(conn: sqlite3.Connection, args) -> None:
    library = load_dj_studio_library()
    if not library:
        print(f"No DJ Studio library found at {DJ_STUDIO_LIBRARY}", file=sys.stderr)
        sys.exit(1)

    target_keys, project = mix_track_keys(args.mix_name)
    if project is None:
        print(f"Mix not found in DJ Studio: {args.mix_name}", file=sys.stderr)
        sys.exit(1)

    if not target_keys:
        print(f"Mix '{project.get('name')}' has no tracks", file=sys.stderr)
        sys.exit(1)

    last_modified = (project.get("lastModified") or "?")[:19].replace("T", " ")
    print(
        f"Populating {len(target_keys)} tracks from '{project.get('name')}' "
        f"(modified {last_modified}, uuid {project['key'][:8]}…)"
    )

    structures = load_dj_studio_structures()
    now = datetime.datetime.now().isoformat()
    inserted = skipped = sections_added = missing_in_library = 0

    for lib_key in target_keys:
        track = library.get(lib_key)
        if track is None:
            missing_in_library += 1
            continue
        tag = track.get("tag", {})
        title = tag.get("title", "Unknown")
        artist = tag.get("artist", "Unknown")
        genre = tag.get("genre", "")
        bpm = track.get("bpm")
        # mikKey is MIK's analyzed key; camelotKey is Beatport metadata or user-edited
        key = camelot_key(track.get("mikKey") or track.get("camelotKey"))

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

        # Import MIK energy segments as sections — only on tracks that have none yet
        structure_key = track.get("structureKey", lib_key)
        struct_data = structures.get(structure_key, {})
        energy_segments = struct_data.get("energyLevelData", [])
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
            section_type = infer_section_type(label, nr, total)
            conn.execute(
                "INSERT INTO track_sections (track_id, section_type, start_beat, end_beat, notes) VALUES (?, ?, ?, ?, ?)",
                (track_id, section_type, float(start_beat), float(end_beat), f"MIK energy {label}/10"),
            )
            sections_added += 1

    conn.commit()
    extras = []
    if skipped:
        extras.append(f"{skipped} errors")
    if missing_in_library:
        extras.append(f"{missing_in_library} mix-referenced tracks not in library")
    suffix = f" ({'; '.join(extras)})" if extras else ""
    print(f"Populated {inserted} tracks{suffix}, {sections_added} new sections. DB: {DB_PATH}")

    if getattr(args, "fetch_release_dates", False):
        _fetch_release_dates_for_mix(conn, target_keys)


def _fetch_release_dates_for_mix(conn: sqlite3.Connection, target_keys: set) -> None:
    """Fetch release dates from Beatport for tracks in target_keys that lack one."""
    token = load_token()
    if not token:
        print(
            "  Skipping release date fetch — no Beatport token found.\n"
            "  Run: uv run local-analyse/beatport_auth.py login",
            file=sys.stderr,
        )
        return

    rows = conn.execute(
        "SELECT library_key, beatport_id, title, artist, release_date FROM tracks"
        " WHERE library_key IN ({})".format(",".join("?" * len(target_keys))),
        list(target_keys),
    ).fetchall()

    candidates = [r for r in rows if r["beatport_id"] and not r["release_date"]]
    if not candidates:
        print("  All tracks already have release dates — nothing to fetch.")
        return

    print(f"  Fetching release dates for {len(candidates)} tracks from Beatport API...")
    fetched = failed = 0
    now = datetime.datetime.now().isoformat()
    for row in candidates:
        date = fetch_release_date(row["beatport_id"], token)
        if date:
            conn.execute(
                "UPDATE tracks SET release_date = ?, updated_at = ? WHERE library_key = ?",
                (date, now, row["library_key"]),
            )
            print(f"    {row['artist']} — {row['title']}: {date}")
            fetched += 1
        else:
            print(f"    {row['artist']} — {row['title']}: not found", file=sys.stderr)
            failed += 1
    conn.commit()
    print(f"  Release dates: {fetched} fetched, {failed} not found.")


def cmd_list(conn: sqlite3.Connection, args) -> None:
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
        print("No tracks in database. Run: uv run dj_cli.py db populate")
        return

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


def cmd_show(conn: sqlite3.Connection, args) -> None:
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
    if track["release_date"]:
        print(f"Released    : {track['release_date']}")
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


def cmd_update(conn: sqlite3.Connection, args) -> None:
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
    if args.release_date is not None:
        if track["release_date"]:
            print(f"Skipping — track already has release_date: {track['release_date']}")
            return
        updates["release_date"] = args.release_date

    if not updates:
        print("Nothing to update. Use --energy, --vocals, --drums, --melody, --notes, --key, --bpm, --beatport-url, or --release-date")
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


def cmd_section_add(conn: sqlite3.Connection, args) -> None:
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


def cmd_section_list(conn: sqlite3.Connection, args) -> None:
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


def cmd_section_remove(conn: sqlite3.Connection, args) -> None:
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

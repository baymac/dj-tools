// Apple Music bridge for playlist-syncer CLI.
// Outputs NDJSON to stdout — one JSON object per line.
// Each record: catalog_id, library_id, name, artist, album, genre, loved, playlists
//
// Modes (mutually exclusive, checked in order):
//   --check                  → test authorization; exit 0 = OK, exit 2 = not authorized
//   --list-playlists         → JSON array of user playlist names (excludes "Favourite Songs")
//   --playlist NAME          → NDJSON for tracks in the named playlist
//   --library-songs          → NDJSON for songs with libraryAddedDate set (Music app "Songs" tab)
//   --favorites              → NDJSON for songs in the "Favourite Songs" playlist
//   --library-and-favorites  → NDJSON union of --library-songs and --favorites
//   (no args)                → NDJSON for full library (all songs, no filter)

import MusicKit
import Foundation

// ---------- Helpers ----------

func extractCatalogID(from playParameters: PlayParameters?) -> String {
    guard let pp = playParameters,
          let data = try? JSONEncoder().encode(pp),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return "" }
    if let nested = json["catalogID"] as? [String: Any], let value = nested["value"] {
        return "\(value)"
    }
    if let flat = json["catalogId"] { return "\(flat)" }
    return ""
}

func toJSON(_ dict: [String: Any]) -> String {
    let data = try! JSONSerialization.data(withJSONObject: dict)
    return String(data: data, encoding: .utf8)!
}

typealias TrackKey = String  // "title|||artist"

func trackKey(title: String, artist: String) -> TrackKey {
    "\(title.lowercased())|||\(artist.lowercased())"
}

func keyFromTrack(_ track: Track) -> TrackKey? {
    switch track {
    case .song(let s): return trackKey(title: s.title, artist: s.artistName)
    default: return nil
    }
}

// ---------- Auth check ----------

func runCheck() async {
    let status = await MusicAuthorization.request()
    if status == .authorized {
        exit(0)
    } else {
        fputs("MusicKit not authorized (status: \(status))\n", stderr)
        fputs("Open the Music app and re-run this command to grant access.\n", stderr)
        exit(2)
    }
}

// ---------- List playlists ----------

func runListPlaylists() async {
    let status = await MusicAuthorization.request()
    guard status == .authorized else {
        fputs("Error: MusicKit not authorized\n", stderr)
        exit(2)
    }

    let request = MusicLibraryRequest<Playlist>()
    guard let response = try? await request.response() else {
        fputs("Error: could not fetch playlists\n", stderr)
        exit(1)
    }

    // "Favourite Songs" is accessed via --favorites flag, not as a regular playlist
    let names = response.items.map { $0.name }.filter { $0 != "Favourite Songs" }
    let data = try! JSONSerialization.data(withJSONObject: names)
    print(String(data: data, encoding: .utf8)!)
    exit(0)
}

// ---------- Playlist data loading ----------

// Fast path: only load the Favourite Songs playlist keys.
// Used for --favorites and --library-and-favorites (no need for full trackPlaylists map).
func loadFavouriteKeys() async -> Set<TrackKey> {
    var keys = Set<TrackKey>()
    let req = MusicLibraryRequest<Playlist>()
    guard let response = try? await req.response() else { return keys }
    for playlist in response.items {
        guard playlist.name == "Favourite Songs",
              let detailed = try? await playlist.with([.tracks]),
              let tracks = detailed.tracks else { continue }
        for track in tracks {
            if let key = keyFromTrack(track) { keys.insert(key) }
        }
        break
    }
    return keys
}

struct PlaylistData {
    var favouriteKeys: Set<TrackKey> = []
    var targetKeys: Set<TrackKey>? = nil   // nil = not filtering
    var trackPlaylists: [TrackKey: [String]] = [:]
}

// Full load — used for --playlist mode (needs targetKeys) and no-args mode (needs trackPlaylists).
func loadPlaylistData(filterPlaylist: String?) async -> PlaylistData {
    var data = PlaylistData()
    var playlistRequest = MusicLibraryRequest<Playlist>()
    guard let response = try? await playlistRequest.response() else { return data }

    for playlist in response.items {
        guard let detailed = try? await playlist.with([.tracks]),
              let tracks = detailed.tracks else { continue }

        if playlist.name == "Favourite Songs" {
            for track in tracks {
                if let key = keyFromTrack(track) { data.favouriteKeys.insert(key) }
            }
        } else if let target = filterPlaylist, playlist.name == target {
            var keys = Set<TrackKey>()
            for track in tracks {
                if let key = keyFromTrack(track) { keys.insert(key) }
            }
            data.targetKeys = keys
        } else if filterPlaylist == nil {
            for track in tracks {
                if let key = keyFromTrack(track) {
                    data.trackPlaylists[key, default: []].append(playlist.name)
                }
            }
        }
    }

    return data
}

// ---------- Stream library songs ----------

enum StreamMode {
    case all              // no filter — every song in library
    case playlist         // only songs matching targetKeys
    case library          // only songs with libraryAddedDate set (Music app "Songs" tab)
    case favorites        // only songs present in favouriteKeys
    case libraryAndFavorites  // songs with libraryAddedDate set OR in favouriteKeys
}

func streamSongs(filter: PlaylistData, mode: StreamMode) async {
    let iso8601 = ISO8601DateFormatter()
    var offset = 0
    let limit = 100
    var total = 0

    while true {
        var request = MusicLibraryRequest<Song>()
        request.limit = limit
        request.offset = offset

        guard let response = try? await request.response() else {
            fputs("Error fetching at offset \(offset)\n", stderr)
            break
        }

        for song in response.items {
            let key = trackKey(title: song.title, artist: song.artistName)

            let include: Bool
            switch mode {
            case .all:
                include = true
            case .playlist:
                include = filter.targetKeys?.contains(key) ?? false
            case .library:
                include = song.libraryAddedDate != nil
            case .favorites:
                include = filter.favouriteKeys.contains(key)
            case .libraryAndFavorites:
                include = song.libraryAddedDate != nil || filter.favouriteKeys.contains(key)
            }

            guard include else { continue }

            let catalogID = extractCatalogID(from: song.playParameters)
            let addedDateStr = song.libraryAddedDate.map { iso8601.string(from: $0) } ?? ""
            let record: [String: Any] = [
                "catalog_id": catalogID,
                "library_id": song.id.rawValue,
                "name": song.title,
                "artist": song.artistName,
                "album": song.albumTitle ?? "",
                "genre": song.genreNames.first ?? "",
                "loved": filter.favouriteKeys.contains(key),
                "playlists": filter.trackPlaylists[key] ?? [],
                "library_added_date": addedDateStr,
            ]
            print(toJSON(record))
            total += 1
        }
        fflush(stdout)

        if response.items.count < limit { break }
        offset += limit
        if offset % 500 == 0 {
            fputs("Fetched \(total)…\n", stderr)
        }
    }

    fputs("Done: \(total) songs\n", stderr)
}

// ---------- Entry point ----------

func main() async {
    let args = CommandLine.arguments.dropFirst()

    if args.contains("--check") {
        await runCheck()
        return
    }

    if args.contains("--list-playlists") {
        await runListPlaylists()
        return
    }

    let status = await MusicAuthorization.request()
    guard status == .authorized else {
        fputs("Error: MusicKit not authorized\n", stderr)
        exit(2)
    }

    if args.contains("--library-songs") {
        fputs("Streaming library songs (libraryAddedDate set)…\n", stderr)
        await streamSongs(filter: PlaylistData(), mode: .library)
        exit(0)
    }

    if args.contains("--favorites") {
        fputs("Loading Favourite Songs playlist…\n", stderr)
        let favKeys = await loadFavouriteKeys()
        fputs("Streaming \(favKeys.count) favourite songs…\n", stderr)
        var filter = PlaylistData()
        filter.favouriteKeys = favKeys
        await streamSongs(filter: filter, mode: .favorites)
        exit(0)
    }

    if args.contains("--library-and-favorites") {
        fputs("Loading Favourite Songs playlist…\n", stderr)
        let favKeys = await loadFavouriteKeys()
        fputs("Streaming library + \(favKeys.count) favourite songs…\n", stderr)
        var filter = PlaylistData()
        filter.favouriteKeys = favKeys
        await streamSongs(filter: filter, mode: .libraryAndFavorites)
        exit(0)
    }

    // Playlist mode or full-library mode
    var playlistName: String? = nil
    if let idx = args.firstIndex(of: "--playlist") {
        let next = args.index(after: idx)
        if next < args.endIndex {
            playlistName = args[next]
        }
    }

    fputs("Loading playlist data…\n", stderr)
    let filter = await loadPlaylistData(filterPlaylist: playlistName)

    if let name = playlistName {
        if filter.targetKeys == nil {
            fputs("Error: playlist '\(name)' not found\n", stderr)
            exit(1)
        }
        fputs("Streaming tracks from '\(name)' (\(filter.targetKeys!.count) track keys)…\n", stderr)
        await streamSongs(filter: filter, mode: .playlist)
    } else {
        await streamSongs(filter: filter, mode: .all)
    }
    exit(0)
}

Task { await main() }
RunLoop.main.run(until: Date(timeIntervalSinceNow: 600))

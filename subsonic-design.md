# Subsonic API server for Music Assistant (fork) — design

Status: phase 1 built on branch `subsonic` (2026-11-26) as `music_assistant/providers/subsonic_server/`.

What was built differs from the plan below in a few places:

- **More of the API in phase 1:**
  - ratings (`setRating`, `userRating`, the `highest` list), stored in `subsonic_server.db`;
  - play queue sync, both the plain and the index-based calls;
  - `getUsers`, `getSimilarSongs`/`getSimilarSongs2`;
  - editing MA's own playlists (`createPlaylist`, `updatePlaylist`, `deletePlaylist`).
- **What isn't there:** renaming a playlist and playlist comments are ignored (the log notes it).
  Transcoding, `apiKey` login and Apple Music / YouTube Music playlists are still for later.
- **Where the reply shapes come from:** Navidrome 0.64 replies captured on the home server. A
  field-by-field comparison against MA's replies leaves no fields missing apart from data the test
  library doesn't have.
- **Tests:** `tests/providers/subsonic_server/` has 22 tests. The full-server ones generate FLAC
  files with ffmpeg, scan them with the filesystem provider and call the API over HTTP.

## 1. Goal

Let Subsonic apps (Arpeggi on iOS first) browse, search and play the music files that Music
Assistant (MA) already manages, so a phone gets a good native player while MA stays the single
library: one set of favourites, one play history, and the same "wrong song" blocks the voice
assistant uses.

Phase 1 serves **files in a folder only**, the same folder Navidrome serves today. It is meant
for the home network and the tailnet, with a username and password per person.

### Non-goals for phase 1

- Streaming from Apple Music or YouTube Music for tracks that have no file yet (see section 16).
- Transcoding. Files are sent as they are (`format=raw`). That is what Arpeggi asks for anyway.
- Public internet access, share links, user management over the API, podcasts, internet radio,
  jukebox mode, video, chat.
- Changes to MA's core. Everything lives in one new plugin provider, so merging upstream stays easy.

## 2. What is on the server today

**Navidrome** runs in its own container on port 4533. Tailscale serve publishes it over HTTPS on
the tailnet at `https://<host>.<tailnet>.ts.net:4534`. It reads `~/Music` read-only.

**The folder.** `~/Music` holds about 2.4 GB: 316 audio files (288 `.m4a`, 16 `.mp3`, 12
`.opus`), 233 `.lrc` lyric files, 152 `.jpg` covers and 121 `.nfo` files. All of it sits under
`~/Music/Music Assistant Cache/`, in one folder per artist and then per album. This is where the
fork's Apple Music and YouTube Music audio caches publish finished files
(`MASS_APPLE_MUSIC_LIBRARY_PATH` and `MASS_YTMUSIC_LIBRARY_PATH`). So the folder is not a CD-rip
collection: almost every file started as streaming-service audio. MA sends them at full speed,
like Navidrome (section 9).

**MA** runs with host networking and mounts the same folder read-write at `/media/music`. The
"Filesystem (local disk)" provider (`filesystem_local`) indexes it:

- 316 track mappings, which resolve to 309 library tracks.
- 293 of those 309 library tracks are merged with an Apple Music or YouTube Music mapping of the
  same song.
- 87 album mappings and 145 artist mappings.
- 303 of the file tracks have a loudness measurement in `audio_analysis` (`loudness_integrated`,
  `true_peak`).

**Arpeggi against Navidrome.** Navidrome's info log shows:

- The client registers as `Arpeggi`.
- Every play asked for `format=raw` with no transcoding.
- Each play made 2 to 4 stream requests, which looks like iOS AVPlayer's range requests.
- It sends now-playing reports.

The info log does not list API calls, so the exact set of endpoints Arpeggi uses is unknown (see
section 17).

## 3. Architecture

```
 phone (Arpeggi)                        home server: MA container, host network
 ┌───────────────┐  HTTP  :4040 (LAN)   ┌─────────────────────────────────────────────────┐
 │ Subsonic app  │ ───────────────────▶ │  subsonic_server plugin, own aiohttp server     │
 └───────────────┘  HTTPS :4041 (tailnet│                                                 │
                    via tailscale serve │  router ─▶ auth ─▶ endpoint handlers            │
                    → 127.0.0.1:4040)   │                     │          │          │     │
                                        │                     ▼          ▼          ▼     │
                                        │              library view   artwork,   file     │
                                        │              (MA library,   lyrics     server   │
                                        │               play memory,             (Range + │
                                        │               favourites,                       │
                                        │               playlog)                    │     │
                                        │                                           ▼     │
                                        │                     /media/music  (filesystem_local)
                                        └─────────────────────────────────────────────────┘
```

### Where it runs: a plugin with its own port

The server is a new **plugin provider**, `music_assistant/providers/subsonic_server/`. Turning
it on and off in MA's settings starts and stops it.

It runs its **own aiohttp server on its own port** (default 4040). It reuses
`music_assistant/helpers/webserver.py`'s `Webserver` class, which the streams server already
uses. It does not mount routes on MA's main web server (8095), because:

- Subsonic apps own the `/rest/` namespace and authenticate with query parameters. MA's
  bearer-token auth middleware must not see these requests or reject them.
- A separate port can bind to its own address and be put behind `tailscale serve` by itself,
  without exposing MA's web UI or API.
- No changes to MA's web server code means fewer merge conflicts when pulling upstream.

Port 4040 is the classic Subsonic default and is free on the server, so MA and Navidrome can run
side by side while the two are compared.

## 4. Accounts and login

Subsonic logins map **one to one onto MA users**. The Subsonic username is the MA username, and
favourites, play history and per-user provider filters follow from that.

### Passwords

Subsonic's usual login is token auth: the app sends `t = md5(password + s)` with a random salt
`s`. To check that, the server needs the plain password. MA only keeps hashes of its own login
passwords, so the plugin keeps **a separate Subsonic password per user**:

- The plugin's settings page shows one password field per MA user, built when the page opens
  (`get_config_entries` receives `mass` and can list users). A blank field means that user has no
  Subsonic access.
- The fields use MA's `SECURE_STRING` type, so they are stored encrypted with MA's key, like
  other provider secrets.
- The setting's description recommends a password generated just for Subsonic and never reused.
  It travels in every request, either hashed with md5 or as `enc:` hex, and some apps keep it in
  plain text.

### Accepted login forms

| Parameters | Meaning | Phase |
|---|---|---|
| `u` + `t` + `s` | token auth, `t == md5(password + s)` | 1 |
| `u` + `p=<password>` | plain password | 1 |
| `u` + `p=enc:<hex>` | hex-encoded password | 1 |
| `apiKey` | OpenSubsonic API key (could be an MA long-lived token) | later, only if an app needs it |

- Comparisons use `hmac.compare_digest`.
- A user who is disabled in MA, or has no Subsonic password, gets error 40, the same as a wrong
  password, so the reply doesn't reveal which users exist.
- Every request runs with MA's `current_user` context variable set to the logged-in user
  (`set_current_user`, from `controllers/webserver/helpers/auth_middleware.py`). MA's
  library queries then apply that user's provider filter, and play history is recorded under that
  user.

### Failed-login throttling

After 10 failures from one address within 5 minutes, that address gets error 40 without a
password check for the next 5 minutes. A warning is logged once per lockout. The counter lives in
memory, which is enough on a home network.

## 5. Network and security

- **Bind address and port** are settings (default `0.0.0.0:4040`). MA uses host networking, so
  this port is reachable on the LAN and on the server's Tailscale address. It must never be
  forwarded on the router or published with Tailscale Funnel.
- **HTTPS on the tailnet** works the same way as Navidrome's today:
  `tailscale serve --bg --https=4041 http://127.0.0.1:4040` gives
  `https://<host>.<tailnet>.ts.net:4041`. Tailscale already encrypts the traffic, but iOS apps
  may insist on HTTPS. The tailnet address works at home too, so one server URL in the app can
  serve both places.
- **Plain HTTP on the LAN** exposes the md5 token to anyone sniffing the home network. That is a
  limit of the Subsonic protocol. It is acceptable at home; anyone who minds can use the tailnet
  HTTPS address everywhere.
- **No secrets in logs.** Query strings and form bodies contain credentials. The access log is
  off, and any debug logging of requests strips `u`, `p`, `t`, `s` and `apiKey`.
- **No CORS headers** in phase 1, because native apps don't need them. Browser clients such as
  Feishin's web build would need an opt-in setting later.
- **Request limits:** POST bodies are capped at 1 MB, and unknown parameters are ignored.
- **Files are only ever found through MA's library**, never from a path the client sends. File
  IDs are library IDs. The absolute path is built with the filesystem provider's
  `get_absolute_path(base_path, relative_path)`, which rejects anything outside the folder. The
  path is then checked once more with `os.path.realpath`, so a symlink can't lead out of the
  folder.

## 6. The library a Subsonic app sees

### Music folders

Each `filesystem_local` provider instance selected in the plugin settings is one Subsonic music
folder. By default every instance is selected; today there is just one. Folders are numbered 1,
2, … in the order of their provider instance IDs, as Navidrome numbers its libraries.
`musicFolderId` filters every list that supports it.

### What is visible

1. **Songs.** Library tracks with a mapping on a selected filesystem instance, whose file
   resolves inside the folder. Library tracks that exist only on Apple Music or YouTube Music are
   hidden in phase 1.
2. **Albums.** Library albums with at least one visible song. An album shows only its visible
   songs, so an Apple Music album with 12 tracks, of which 3 are cached, appears with 3 songs and
   `songCount=3`.
3. **Artists.** Library artists that are the album artist of at least one visible album. That
   matches Navidrome's default "album artist" view.
4. **Blocked tracks are hidden everywhere**: lists, search, albums, playlists and random songs.
   The check is `mass.music.play_memory.is_blocked(track)`. The Chrome Nine fake that the voice
   assistant blocked has a file in the folder today, and it stays hidden in Subsonic apps too.
5. **CUE-sheet tracks** (`filesystem_local` IDs that `parse_cue_track_id` recognises) are hidden
   in phase 1. Each one is a slice of a larger file, so sending the raw file would play the wrong
   audio. Serving them needs ffmpeg cutting, which comes with transcoding later.

Queries use MA's library API with the provider filter
(`mass.music.<type>.library_items(provider=[instances], order_by=..., limit=..., offset=...)`).
During implementation, check that the provider filter means "has a mapping on this instance" for
albums and artists. If it doesn't, the plugin runs its own read-only SQL join on
`provider_mappings` through MA's database helper.

### IDs

Subsonic IDs are opaque strings. The plugin uses type-prefixed MA library IDs:

| Prefix | Item | Example |
|---|---|---|
| `ar-` | library artist | `ar-145` |
| `al-` | library album | `al-87` |
| `tr-` | library track | `tr-2832` |
| `pl-` | library playlist | `pl-9` |

- **Why library IDs:** they stay the same when a file moves, they are what play memory, favourites
  and the playlog key on, and they keep working when tracks without files become streamable
  later (section 16). The same library track keeps the same ID either way.
- **Why the prefix:** `getCoverArt` and the folder-style calls receive IDs without saying what
  type they are, so the type has to be part of the ID.
- **Bad IDs:** an unknown or malformed ID returns error 70 ("not found").
- **Risk:** if MA re-merges or deletes and re-adds a library item, its ID changes, and the app
  shows it as new after its next refresh. This is acceptable for a small home library.

## 7. Data mapping

### Song (`child`)

| Subsonic field | Source in MA |
|---|---|
| `id`, `parent`, `albumId` | `tr-<track id>`, `al-<album id>` |
| `isDir`, `type`, `mediaType` | `false`, `music`, `song` |
| `title`, `sortName` | `track.name`, `track.sort_name` |
| `album` | `track.album.name` |
| `artist`, `artistId`, `artists[]` | track artists; display string joined with `; ` |
| `displayAlbumArtist`, `albumArtists[]` | the album's artists |
| `track`, `discNumber` | `track.track_number`, `track.disc_number` |
| `year` | `album.year` |
| `genre`, `genres[]` | MA genres (`genre_media_item_mapping`) |
| `duration` | `track.duration` in seconds |
| `bitRate`, `samplingRate`, `bitDepth`, `channelCount` | the filesystem mapping's `audio_format` |
| `suffix`, `contentType` | file extension: m4a → `audio/mp4`, mp3 → `audio/mpeg`, opus → `audio/ogg`, flac → `audio/flac` |
| `size` | `os.stat` of the file, taken in a worker thread |
| `path` | the file's path relative to the music folder |
| `coverArt` | `al-<album id>`, or `tr-<id>` when the track has its own image |
| `created` | `timestamp_added` (ISO 8601) |
| `starred` | set when the track is an MA favourite; MA keeps no "starred at" time, so this uses `timestamp_modified` |
| `playCount`, `played` | this user's playlog, otherwise MA's play counters |
| `replayGain` | from `audio_analysis` (below) |
| `musicBrainzId`, `isrc[]` | MA external IDs, when present |

**ReplayGain.** Arpeggi can normalise volume, and these cached tracks come at very different
loudness levels. From the `loudness_analysis` row of the file mapping, or of the track's other
mapping when the file has none:

- `trackGain = -18 - loudness_integrated` (ReplayGain 2 reference level)
- `trackPeak = 10 ** (true_peak / 20)`
- `albumGain` uses `loudness_album` when present.
- With no measurement, the field is left out.

### Album (`albumID3`)

Fields: `id`, `name`, `artist`, `artistId`, `artists[]`, `coverArt`, `songCount` (visible songs
only), `duration` (sum of visible songs), `year`, `genre`, `genres[]`, `created`, `starred`,
`playCount`, `played`, `sortName`, and `musicBrainzId` when known.

### Artist (`artistID3`)

Fields: `id`, `name`, `sortName`, `coverArt`, `artistImageUrl` (left out; apps then call
`getCoverArt`), `albumCount` (visible albums) and `starred`.

### Index letters

`getArtists` and `getIndexes` group artists by the first letter of MA's `sort_name`, with `#` for
anything that isn't a letter. They return `ignoredArticles="The El La Los Las Le Les"`, the same
default as Navidrome.

## 8. API surface

Requests come as `/rest/<method>` or `/rest/<method>.view`, by GET (query string) or POST (form
body, the OpenSubsonic `formPost` extension). Repeated parameters such as `id=1&id=2` become
lists.

### Response envelope

- `f=xml` (the default), `f=json` and `f=jsonp&callback=` all come from the same Python dict:
  - A scalar becomes an XML attribute.
  - A dict becomes a child element.
  - A list becomes repeated child elements, or a JSON array.
  - The key `value` becomes element text (lyrics).
- XML uses the namespace `http://subsonic.org/restapi`.
- Envelope fields: `status`, `version="1.16.1"`, `type="music-assistant"`,
  `serverVersion=<MA version>` and `openSubsonic=true`.
- Errors come back with HTTP 200, `status="failed"` and `error{code, message}`, as the spec
  requires:

| Code | When |
|---|---|
| 0 | generic or unsupported method |
| 10 | required parameter missing |
| 40 | wrong username or password, or locked out |
| 42 | login form not supported (`apiKey` in phase 1) |
| 50 | not allowed (for example editing a playlist that MA can't edit) |
| 70 | not found |

- An unknown method returns HTTP 404, as Navidrome does, and a warning is logged once per method
  and client per run (section 12b). This is how the calls Arpeggi really makes get discovered.

### Phase 1 endpoints

| Area | Method | MA mapping and notes |
|---|---|---|
| System | `ping`, `getLicense` | `getLicense` returns `valid=true` |
| | `getOpenSubsonicExtensions` | answers without login, as the spec requires; lists `formPost` v1 and `songLyrics` v1 |
| | `getUser` | roles: stream, download, scrobbling and playlist on; `adminRole` = MA admin; everything else off; `folder[]` = folder IDs |
| | `getMusicFolders` | section 6 |
| | `getScanStatus`, `startScan` | a running sync task for the selected filesystem instances; `startScan` calls `mass.music.start_sync(providers=[...])`; `count` = visible songs |
| Browse (ID3) | `getArtists` | index of visible album artists, with `lastModified` and `ifModifiedSince` |
| | `getArtist` | the artist's visible albums |
| | `getAlbum` | album plus visible songs, sorted by disc then track |
| | `getSong` | |
| | `getGenres` | MA genres with counts of visible songs and albums |
| | `getArtistInfo2`, `getAlbumInfo2` | MA metadata biography or description and images; `similarArtist` empty in phase 1 |
| | `getTopSongs` | the artist's visible songs by play count |
| Browse (folders) | `getIndexes`, `getMusicDirectory` | an artist → album → song tree over the same data, for older apps |
| Lists | `getAlbumList2`, `getAlbumList` | `type` mapping below |
| | `getRandomSongs` | `size` (max 500), `genre`, `fromYear`, `toYear`, `musicFolderId` |
| | `getSongsByGenre` | |
| | `getStarred2`, `getStarred` | MA favourites that are visible |
| | `getNowPlaying` | from now-playing scrobbles held in memory |
| Search | `search3`, `search2` | MA library search over visible items with paging (`artistCount/Offset`, `albumCount/Offset`, `songCount/Offset`); an empty query lists everything page by page, which syncing apps rely on; heard tracks from play memory move to the top of song results |
| Media | `stream`, `download` | section 9 |
| | `getCoverArt` | section 10 |
| | `getLyrics`, `getLyricsBySongId` | section 10 |
| Annotate | `star`, `unstar` | `add_item_to_favorites` / `remove_item_from_favorites` for `id`, `albumId` and `artistId` |
| | `scrobble` | section 11 |
| Playlists | `getPlaylists`, `getPlaylist` | MA's own (`builtin`) playlists only, showing their visible tracks |

`getAlbumList2` `type` mapping:

| `type` | MA query (visible albums) |
|---|---|
| `newest` | `order_by="timestamp_added_desc"` |
| `recent` | this user's playlog, most recent first (as MA's recently-played query does) |
| `frequent` | `order_by="play_count_desc"` |
| `random` | `order_by="random"` |
| `alphabeticalByName` | `order_by="sort_name"` |
| `alphabeticalByArtist` | `order_by="album_artist_name"` |
| `starred` | `favorite=True` |
| `byYear` | `fromYear`/`toYear` filter; ascending or descending by the order of the two |
| `byGenre` | `genre` filter |
| `highest` | no ratings in phase 1, so it returns the same as `frequent` |

**Why playlists are limited to MA's own.** The library also has 183 Apple Music and 5 YouTube
Music playlists. Listing their tracks makes MA ask those services, and a Subsonic app that syncs
every playlist would do that for all of them. That clashes with the fork's rule against API calls
that aren't throttled or that re-fetch what is already cached. Phase 2 can show them from tracks
MA already holds, without calling the service.

### Phase 2

| Area | Methods | Notes |
|---|---|---|
| Playlist editing | `createPlaylist`, `updatePlaylist`, `deletePlaylist` | builtin playlists only. `songIndexToRemove` counts visible tracks, so map it to MA positions before calling `remove_playlist_tracks`. |
| Ratings | `setRating`; `userRating` field; `highest` list | stored in the plugin's own `subsonic_server.db` (separate file, like `play_memory.db`). Optional and off by default: 1 star = "wrong song" block. |
| Bookmarks | `getBookmarks`, `createBookmark`, `deleteBookmark` | plugin DB |
| Smarter similar songs | `getSimilarSongs2`, `similarArtist` | MA's radio mode or recommendations, limited to visible tracks |
| Transcoding | `format`, `maxBitRate`, `timeOffset`; extensions `transcodeOffset`, `transcoding` | ffmpeg through MA's audio helpers; needed for mobile data and for CUE tracks |
| Provider playlists | `getPlaylists` | from tracks MA already holds, without calling the service |
| API key login | `apiKey` | MA long-lived token, if an app needs it |

### Not supported

These return error 0, or an empty list where apps expect a list:

- shares (deliberately left out: share links hand the audio to people outside the household)
- podcasts, internet radio, jukebox, chat, user management, video, HLS, captions, avatars

### Matching Navidrome

Apps are tested against Navidrome far more than against any other server, so the plugin copies
Navidrome 0.64's replies wherever that is cheap. Reference replies were captured from the
Navidrome running on the server (read-only calls) and drive the shape of every phase 1 reply:

- **Envelope and fields.** The same element names and field sets as Navidrome, including the
  OpenSubsonic fields with empty defaults in JSON (`genres: []`, `moods: []`, `replayGain: {}`,
  `musicBrainzId: ""`, `userRating: 0`, `isCompilation: false`, `releaseDate`, `displayArtist`,
  `albumArtists`, `roles` on artists, and so on). XML leaves out empty strings and empty lists,
  as Navidrome's XML does.
- **Empty results** are empty objects (`"playlists": {}`, `"nowPlaying": {}`), not missing keys.
- **Unknown methods** get HTTP 404 with `404 page not found`, as Navidrome does (and a log line).
- **`getLyrics`** without a match returns `{"lyrics": {"value": ""}}`, not an error.
  **`getTopSongs`** without plays returns `{"topSongs": {}}`.
- **`getPlayQueue`** with nothing saved returns the queue with `changed:
  "0001-01-01T00:00:00Z"`, like Navidrome. `savePlayQueue` and `getPlayQueue` move into phase 1
  (stored per user), because players often call them at start-up.
- **`getSimilarSongs2` / `getSimilarSongs`** move into phase 1: same artist first, then the same
  genre, random order, visible songs only.
- **Stubs** that return valid empty lists so apps don't show errors: `getBookmarks`,
  `getShares`, `getPodcasts`, `getNewestPodcasts`, `getInternetRadioStations`,
  `getChatMessages`, `getVideos`.
- **`getUser`** reports the same roles as Navidrome, except `shareRole=false`.
- **`getMusicFolders`** numbers folders 1, 2, … like Navidrome.
- **`getScanStatus`** returns `scanning`, `count`, `folderCount` and `lastScan`.
- **`ignoredArticles`** is Navidrome's list: `The El La Los Las Le Les Os As O A`.
- **Artist images.** Navidrome puts a signed, login-free image URL in `artistImageUrl` and in
  `getArtistInfo2`/`getAlbumInfo2`. The plugin does the same with its own `/share/img/<token>`
  route, where the token is the item ID signed with HMAC-SHA256 using a secret kept in MA's data
  folder. The host in the URL is the one the app connected to.
- **Server type.** The envelope reports `type="music-assistant"` by default. An advanced setting
  reports `type="navidrome"` instead, for testing whether an app enables extra features for
  Navidrome. Those features may call Navidrome's own web API (`/auth/login`, `/api/...`),
  which the plugin doesn't have. Every request outside `/rest/` is logged, so a test shows it
  straight away.
- **OpenSubsonic extensions** advertised: `formPost` 1, `songLyrics` 1, `indexBasedQueue` 1 (with
  `getPlayQueueByIndex` and `savePlayQueueByIndex`). `transcoding`, `transcodeOffset`,
  `playbackReport` and `topSongsByArtistId` are not advertised until they are built.

## 9. Sending audio

`stream` and `download` behave like Navidrome's raw file serving (Go's `http.ServeContent`):

1. Resolve `id` (`tr-…`) to a visible track and then to its file (section 5).
2. Ignore `format` and `maxBitRate` in phase 1 and always send the file as it is, at full speed.
   When a client asks for transcoding, log it.
3. Use aiohttp's `FileResponse`, which handles `HEAD`, `Accept-Ranges: bytes`, single ranges
   (`206 Partial Content`), `416`, `ETag`, `Last-Modified`, `If-Range` and `If-Modified-Since`.
4. Set the headers Navidrome sends: `Content-Type` from the file extension, `Content-Length`,
   `Last-Modified` and `X-Content-Duration` (seconds, two decimals).
5. For `download`, add `Content-Disposition: attachment; filename="<file name>"`.

Because the file's size is known and ranges work, iOS AVPlayer can seek, and playback starts as
soon as the first bytes arrive. "Download for offline" runs at full network speed, as it does with
Navidrome. This is the main reason phase 1 serves files and not live streams.

### Formats

- `.m4a` (AAC) and `.mp3` play natively on iOS.
- The 12 `.opus` files (YouTube Music cache) may not play raw in Arpeggi, because AVPlayer's
  support for Ogg Opus is limited. Test this first (section 15). If they fail, phase 2
  transcoding serves them as AAC, and until then phase 1 can hide `.opus` from clients that send
  `c=Arpeggi` (a setting).

## 10. Cover art and lyrics

### Cover art

`getCoverArt?id=al-87&size=600`:

1. Look up the item by its prefixed ID.
2. Call `mass.metadata.get_image_url_for_item(item)` and then
   `mass.metadata.get_thumbnail(path, provider, size)`. MA's thumbnail cache already handles
   resizing, and the folder's `cover.jpg` files come through the filesystem provider.
3. Send `Cache-Control: public, max-age=86400` and an ETag.
4. With no image, return error 70; apps then show their placeholder.
5. Don't fetch images from the internet just for a Subsonic request. Only images MA already has,
   or would show in its own UI, are used.

### Lyrics

- `getLyricsBySongId` (OpenSubsonic `songLyrics`) returns synced lines from the track's LRC
  lyrics. The filesystem provider already reads the `.lrc` sidecar files: 233 are in the folder.
  The plugin parses `[mm:ss.xx]` into `line[{start (ms), value}]`, and falls back to plain
  lyrics with `synced=false`.
- `getLyrics?artist=&title=` finds the first visible match and returns plain text in `value`.

## 11. Scrobbling, play memory, favourites

### Scrobbling

`scrobble?id=…&submission=true` (`id` may repeat; `time` is in ms):

- For each visible track, call
  `mass.music.mark_item_played(track, fully_played=True, seconds_played=track.duration,
  userid=user.user_id, user_initiated=True)`.
- A Subsonic app only scrobbles once a track has really been listened to (usually half the track
  or 4 minutes). So these plays:
  - show up in MA's "recently played" for that user;
  - count toward play counts and MA's recommendations;
  - **feed play memory**, so phone listening helps the voice assistant pick the right song.
- Duplicate scrobbles are harmless: play memory already ignores repeats within 10 minutes.

`submission=false` means "now playing". It is kept in memory (user, client name, track, time) for
`getNowPlaying` and is **not** written to the playlog.

### Favourites

`star` and `unstar` change MA favourites, so stars show up in MA's UI and the other way round.
MA's favourites are shared by everyone, not kept per user. That is how MA works, and it is noted
in the setting's description.

### Blocks

"Wrong song" blocks made by voice hide the track in Subsonic apps too. Optional in phase 2: a
1-star rating in the app adds a block.

## 12. Library changes and scans

- New files appear when the fork's caches publish them, which happens when music plays on MA's
  speakers. The filesystem provider picks them up at its next sync, and apps see them on their
  next refresh.
- `getIndexes` and `getArtists` return `lastModified`, the newest `timestamp_modified` of visible
  items, and honour `ifModifiedSince`, so apps can skip a full reload when nothing changed.
- `startScan` starts a sync of the selected filesystem instances, and `getScanStatus` reports it.

## 12b. Logging for testing

The plugin uses its own MA logger (`music_assistant.subsonic_server`), so its log level can be
set on its own in MA's settings.

- **Every request** is logged at info level while the "Log every request" setting is on (the
  default during testing). The line has the method, client name (`c`), user, the parameters
  with `u`, `p`, `t`, `s` and `apiKey` removed, the result (ok or error code), the bytes sent
  for media, and the time taken.
- **Unknown methods and paths outside `/rest/`** are logged as warnings with the client name,
  once per method and client per run.
- **Exceptions** in a handler are logged with a traceback, and the app gets error 0.
- **Failed logins** are logged with the username and address, but never the password or token.
- **Transcoding requests** (`format` or `maxBitRate` set) are logged, since phase 1 ignores them.

## 13. Settings (plugin config entries)

| Setting | Type | Default | Notes |
|---|---|---|---|
| Port | integer | 4040 | |
| Bind address | string (advanced) | `0.0.0.0` | |
| Music folders | multi-select of `filesystem_local` instances | all | |
| Subsonic password for `<user>` | secure string, one per MA user | blank (no access) | description: generate a new one, never reuse |
| Log every request | toggle | on | section 12b |
| Server type reported to apps | `music-assistant` or `navidrome` (advanced) | `music-assistant` | section 8 |
| Hide `.opus` from Arpeggi | toggle | off | only if the opus test fails, until transcoding exists |


## 14. Code layout

```
music_assistant/providers/subsonic_server/
  __init__.py      setup(), get_config_entries() with one password field per user,
                   SubsonicServerProvider(PluginProvider): starts and stops the server
  manifest.json    type "plugin", domain "subsonic_server", no new requirements
  server.py        Webserver on its own port; /rest/{method}[.view]; GET+POST parsing;
                   sets current_user; dispatch table method -> handler
  auth.py          the login forms in section 4, lockout counter, apiKey stub (error 42)
  responses.py     envelope; one dict model -> XML / JSON / JSONP; error helper
  ids.py           ar-/al-/tr-/pl- encode and decode; music folder IDs
  library_view.py  visible-item rules (section 6): provider filter, blocked tracks, CUE,
                   path resolution, album -> visible songs, counts, lastModified
  mapping.py       MA models -> artistID3 / albumID3 / child / playlist / genre / lyrics,
                   ReplayGain
  file_server.py   file lookup and FileResponse with Navidrome's headers
  handlers/        system.py browsing.py lists.py search.py media.py annotation.py playlists.py
  strings.json, icon.svg, icon_monochrome.svg
tests/providers/subsonic_server/
```

- **No core changes.**
- **What it reads from MA:**
  - `mass.music` (library, search, favourites, `mark_item_played`, `start_sync`, `play_memory`)
  - `mass.metadata` (images)
  - `mass.webserver.auth` (users)
  - the filesystem provider's `base_path`
- **Dependencies:** the plugin uses only aiohttp and the standard library. `xml.etree.ElementTree`
  only writes XML here and never parses client input. `py-opensonic` is already pinned by the
  `opensubsonic` provider and is used only by the tests.

## 15. Testing

### Unit tests

- **Login:**
  - `t`/`s` correct and wrong
  - `p` plain and `enc:`
  - missing parameters (error 10)
  - user disabled or without a Subsonic password (error 40)
  - lockout after 10 failures, then recovery
  - `apiKey` (error 42)
- **Envelope:** the same dict rendered as XML, JSON and JSONP matches stored example files;
  error codes; HTTP 200 on errors.
- **IDs:** round trips; bad prefixes and non-numeric IDs give error 70.
- **Library view:**
  - a track only on Apple Music is hidden
  - a partial album shows only the songs with files and the right `songCount`
  - a blocked track is hidden from search, albums and random songs
  - a CUE track is hidden
  - a symlink out of the folder is refused
- **File server:**
  - every range form
  - `416`
  - `HEAD`
  - `If-Range`
  - bytes equal the file
- **Scrobble:** `submission=true` calls `mark_item_played` with the user's ID and play memory
  remembers the track; `submission=false` only changes now-playing.
- **Search:** `search3` with an empty query pages through everything; a heard track comes first.

### Integration test

Start MA's test instance with a filesystem provider over a tiny fixture folder (2 artists,
3 albums, a CUE file and a symlink), start the plugin on a random port, and use `py-opensonic` as
a real client:

- `ping`, `getArtists`, `getAlbum`, `search3`
- `stream` with and without ranges, compared to the file's bytes
- `getCoverArt`, `star`, `scrobble`

### Comparison with Navidrome

A small script calls the same methods on Navidrome and on MA's server for the same folder. It
compares the field names on each element type, not their values. Credentials come from
environment variables and are never printed. This shows which fields apps might expect that the
plugin doesn't send.

### Manual checks with Arpeggi

1. Add the server on the LAN (`http://<server>:4040`) and on the tailnet
   (`https://<host>.<tailnet>.ts.net:4041`), next to the existing Navidrome entry.
2. Browse artists and albums, search, and play an album straight through. Check seeking and the
   switch to the next track.
3. Check that ReplayGain levels sound even.
4. Play an `.opus` track (the format question).
5. Check that a scrobble shows up in MA's recently played and in play memory (`music/play_memory/list`).
6. Star a track and see it in MA's favourites.
7. Check that a blocked track doesn't appear.
8. Check the lyrics view.
9. Start an offline download.
10. Read the "unsupported Subsonic call" log lines to see what Arpeggi asked for that phase 1
    doesn't have.

## 16. Later: tracks without files (streaming)

Phase 1 keeps the IDs ready for this: a library track keeps `tr-<id>` whether or not it has a
file. What would change, and how Subsonic apps are likely to cope with the delay:

- **First-byte delay.** An uncached Apple Music track needs a license, a download and decryption
  before audio flows, which takes a few seconds. Subsonic apps wait on a slow HTTP response; their
  timeouts are usually tens of seconds. A pause of 2 to 5 seconds feels like a slow start, not a
  failure. Preloading the next track hides the delay for everything after the first track.
- **No size and no ranges until cached.** A live stream has no `Content-Length`, so seeking
  forward fails or stalls in AVPlayer. Subsonic's `estimateContentLength=true` covers the length
  but not seeking.
- **The likely approach:** when an uncached track is requested, ask the fork's cache to fetch it,
  and serve the growing file from the start. Once it is complete, it is an
  ordinary file. That reuses the cache instead of building a second streaming path,.
- **Visibility.** The app has to see these tracks to request them. That means listing library
  tracks without files, marked with a different `suffix` or a comment. It also means search would
  return them, which brings back the look-alike problem play memory solves for voice. Play
  memory's blocks and promotion already apply to `search3`.
- **Preloading.** An app that preloads the next track or downloads for offline would make the
  cache fetch whole albums from the service. That needs a cap (for example one prefetch per user)
  to stay within the fork's throttling rule.

## 17. Risks and open questions

1. **Which calls Arpeggi really makes.** Navidrome's info log doesn't show them. Two ways to find
   out: the plugin's "unsupported call" log during the first test, or run Navidrome once with
   `ND_LOGLEVEL=debug`. Station mode probably needs `getSimilarSongs2` (phase 2).
2. **`.opus` playback on iOS**: test first; the workaround is in section 9.
3. **Plain HTTP:** some iOS apps refuse plain HTTP to LAN addresses. If Arpeggi does, use the
   tailnet HTTPS address at home as well.
4. **Navidrome-only features:** if an app needs Navidrome's own web API, the logs will show
   requests outside `/rest/`; those are out of scope for phase 1.
5. **Shared favourites:** MA has one set of favourites for everyone.
6. **Album splits:** the same soundtrack exists as two folders today (one with "(16 tracks)" in the
   name). Subsonic shows whatever MA's library merged, so a split in MA is a split in the app.
7. **ID changes** after MA re-merges library items make the app treat them as new items.
8. **One process:** an MA restart (every deploy) also stops Subsonic playback on phones. Ask
   before deploying, as for any MA change.

## 18. Rollout

1. Build the plugin and its tests in the fork, and run `pre-commit run --all-files` and the test
   suite.
2. Commit, push and deploy only after asking (a deploy restarts MA). Use the usual image-based
   flow: build-fork-image, back up, switch the compose tag.
3. In MA's settings, enable "Subsonic API server", keep port 4040, and set a Subsonic password for
   each person who should have access.
4. Run `tailscale serve --bg --https=4041 http://127.0.0.1:4040`.
5. Add the server in Arpeggi next to Navidrome and work through the manual checks.
6. Keep Navidrome running until MA's server covers everything you use. Then stop Navidrome, or
   keep it as a fallback.

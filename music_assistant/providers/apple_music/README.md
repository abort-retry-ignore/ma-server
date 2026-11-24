# Apple Music audio cache and local metadata

This private fork can keep complete, played Apple Music tracks for personal playback. Set
`MASS_APPLE_MUSIC_AUDIO_CACHE` to a writable absolute directory for the cache index and temporary
files. Set `MASS_APPLE_MUSIC_LIBRARY_PATH` to a writable directory inside an existing local music
source to publish completed clear files there, with tags and album artwork. An unset library path
leaves the audio cache outside the local library. Incomplete downloads and encrypted entries are
not published.

After publication, metadata is exported in the background, with a follow-up after two minutes and
an hourly retry. The exporter first uses Apple Music metadata already in MA's cache, then the MA
library's merged metadata. Missing data is scheduled through MA's existing throttled metadata
pipeline, subject to its normal refresh interval; data already held does not need to be fetched
again. Artist images use MA's existing image cache.

## Lyrics

Apple Music's MA provider does not fetch lyrics. Published tracks can still get lyrics already in
the MA library or from an enabled metadata provider such as LRCLIB. If lyrics are missing, the
exporter schedules a normal metadata refresh and retries in the background. Results depend on the
configured providers, their matches and availability, so not every track will have lyrics.

When available, synchronized lyrics are saved alongside the audio as a track-named `.lrc` file;
otherwise plain lyrics are saved as `.lyrics.txt`. The local filesystem provider reads these
sidecars, so they remain usable with the published files. Synchronized lyrics use line-level
timing in MA.

## Sidecars and cache cleanup

The exporter also writes `artist.nfo`, `artist.jpg` and `album.nfo` when data is available. It
tracks its own sidecars under the cache index and only updates a file if its contents still match
what it previously wrote. Existing, modified or deliberately deleted sidecars are left alone.

The original cached audio is kept for 15 minutes after publication to protect outstanding playback
references, then removed only if both copies remain unchanged. The published files are the local
library, not temporary cache files. The cache's `.ma-audio-cache` directory contains the track
index and sidecar-ownership records; keep it when clearing temporary cached copies. Removing it
breaks cache-based lookup and retries for already published tracks and loses the records that
protect deliberately deleted sidecars.

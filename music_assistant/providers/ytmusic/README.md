# YouTube Music audio cache

This private fork can keep complete, played YouTube Music tracks for personal playback. Set
`MASS_YTMUSIC_AUDIO_CACHE` to a writable absolute directory outside the scanned music library; each
provider instance gets a separate index and temporary files. Set `MASS_YTMUSIC_LIBRARY_PATH` to a
separate writable directory inside an existing local music source to publish tagged tracks and
artwork. Leaving the cache path unset disables caching; leaving the library path unset keeps the
cache outside the library.

For uncached playback, the provider selects the highest advertised bitrate Opus stream available,
then the highest advertised bitrate AAC stream if no Opus is offered. If neither is available, it
falls back to the best other audio stream. Bitrate is a selection proxy, not a guarantee of how a
particular recording will sound. The separate Premium entitlement check remains unchanged.

The cache retains the provider-selected compressed audio and remuxes it with FFmpeg's `-c:a copy`,
without re-encoding. This avoids additional quality loss while keeping file size in mind. Supported
streams are stored as Opus, M4A, Ogg Vorbis, MP3 or FLAC as appropriate; no single format is
required. Unsupported stream formats continue to play normally without being cached. Existing cache
files remain in their current format and are not downloaded again to change codecs.

Only complete tracks are stored. Interrupted downloads and partial responses are discarded; a
playback that starts at a seek position uses the normal remote stream rather than caching a partial
track. Podcasts are not cached. A valid cached track plays locally without resolving a new YouTube
stream; missing or failed local audio falls back to the provider.

Published files receive track tags, album artwork, and available metadata and lyrics sidecars using
MA's normal cached and throttled metadata pipeline. Existing or user-edited files are not replaced.
The cache index and sidecar-ownership records must be kept when clearing temporary cached copies.
See the [Apple Music cache documentation](../apple_music/README.md) for shared metadata behavior.

# Content Decryption Module (CDM)

The Widevine CDM is only required the first time each track is played (to decrypt and store the clear compressed M4A in the audio cache). Once a track is in the cache, playback works without the CDM or license server.

If you enable the audio cache (`audio_cache_path`), place the two files in `/usr/local/bin/widevine_cdm` inside the container (or the equivalent path on the host):

1. client_id.bin
2. private_key.pem

These allow Music Assistant to obtain the per-track decryption keys. More info on obtaining CDM files: https://www.ismailzai.com/blog/picking-the-widevine-locks

Without the cache (or for the first play of any track), the CDM is required for all Apple Music catalog playback.

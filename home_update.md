# Updating the home Music Assistant server (Apple Music audio cache)

How to get this fork's Apple Music audio cache onto the production Music Assistant (MA) container at home, keep it there across restarts and image updates, and roll it back. Written 2026-11-23 against MA **2.10.13**. What was actually exercised on the production host: the backup command (as a dry run), the version and path checks, the smoke test, the verification steps, the settings edit, and applying the diff to the stock files. **Not run end to end:** `reapply.sh` as a script (its steps were done by hand), Procedure B on a newer MA version, and the rollback and restore commands. Those are marked "not tested" where they appear.

## 0. Set these first

Host paths are not part of this repository, so the commands use placeholders. Set them once in each new shell (use your own values):

```bash
STACK=/path/to/your/compose/stack        # folder that holds docker-compose.yml for MA
MUSIC_DIR=/path/to/your/music            # host folder mounted read-only at /media/music (your local library)
CACHE_DIR=/path/to/audio-cache           # host folder mounted read-write at /music-cache (this feature)
BACKUP_DIR=/path/to/backups              # where backups go; keep it private
PATCH=$STACK/audiocache-patch-2.10.13    # the ported patch files (section 2.2)
```

The Apple Music provider instance id is looked up from the settings file where needed (it differs per install).

## 1. Which procedure do I need?

| Situation | Do this |
|---|---|
| The container was recreated (compose change, `docker compose up -d` after an edit, image pull) and is still MA 2.10.13 | [Procedure A: re-apply the patch](#4-procedure-a-re-apply-the-patch-same-ma-version) |
| MA released a newer stable version and you want it | [Procedure B: move the patch to a new MA version](#5-procedure-b-move-the-patch-to-a-new-ma-version) |
| Something broke | [Rollback](#7-rollback) |
| Change or turn off the cache folder, clear the cache | [Operating notes](#8-operating-notes) |

**Do not run a build of the `apple-music-audio-cache` branch itself on the production data.** The branch is based on MA `dev`. Its library database schema is **74**; the running 2.10.13 uses **58** (checked 2026-11-23). Starting the `dev` code on the production volume migrates the library 16 versions forward, and 2.10.13 cannot read the result: the only way back is restoring a volume backup. The patched stable image described here is the supported route. (I also have not verified a local image build procedure for the branch.)

## 2. What this version is

- **Feature:** an optional "Audio cache folder" setting on the Apple Music provider. When set, the first play of a track downloads Apple's encrypted file through MA and keeps a copy under `Artist/Album/NN Title.m4a`. Later plays read that file and decrypt it with a fresh licence key, so no audio is downloaded again.
- **The files stay encrypted** (Widevine). Only MA with a valid Apple Music account can play them. The decryption key is never stored.
- **Only complete downloads are kept.** A skipped or failed download leaves nothing behind. Radio and the user's own uploaded (unencrypted) library tracks are not cached. There is no size cap.
- **Source:** this repository, branch `apple-music-audio-cache`, feature commit `b7234ffa9` (based on `dev`).
- **What the branch changes:** new `music_assistant/providers/apple_music/audio_cache.py`; edits to `constants.py`, `provider.py`, `streaming.py` and `strings.json` of that provider; `music_assistant/translations/en.json` (generated); new tests in `tests/providers/apple_music/test_audio_cache.py`.
- **What production runs is not that commit.** The `dev` versions of `provider.py` and `streaming.py` differ from 2.10.13 by hundreds of lines, so the same changes were ported by hand onto the 2.10.13 files. The ported files live on the host, outside git (section 2.2).

### 2.1 Production layout (as of 2026-11-23)

| Item | Value |
|---|---|
| Stack folder (`$STACK`) | holds `docker-compose.yml` |
| Container / image | `music-assistant`, `ghcr.io/music-assistant/server:latest` (MA 2.10.13), host networking, `restart: unless-stopped` |
| Data volume | `music-assistant_music-assistant-data` mounted at `/data` (settings, library, auth databases; about 225 MB) |
| Local music library | host `$MUSIC_DIR` mounted **read-only** at `/media/music` (the "Filesystem (local disk)" provider reads `/media`) |
| Audio cache | host `$CACHE_DIR` mounted **read-write** at `/music-cache`, deliberately outside `/media` so the local library never indexes the encrypted files |
| Setting | on the Apple Music provider instance: `audio_cache_path` = `/music-cache` (stored in the data volume) |
| Python package path in the container | `/app/venv/lib/python3.14/site-packages/music_assistant/providers/apple_music` |

### 2.2 Files you rely on (all under `$STACK`)

| Path | What it is |
|---|---|
| `audiocache-patch-2.10.13/{constants,provider,streaming,audio_cache}.py` | the four ported files that go into the container |
| `audiocache-patch-2.10.13/reapply.sh` | copies them in and restarts MA; refuses to run on any version other than 2.10.13 |
| `audiocache-patch-2.10.13/audiocache-2.10.13.patch` | unified diff, stock 2.10.13 to patched (applies cleanly to the stock files; used for Procedure B) |
| `audiocache-patch-2.10.13/tools/` | `enc.m4a` (synthetic encrypted test track), `e2e_cache.py` (smoke test), `cachewatch.py` (file-event watcher) |
| `backup-apple_music-2.10.13/` | the container's original, unpatched Apple Music code |
| `docker-compose.yml.bak-pre-music-20261123`, `...bak-pre-audiocache-20261123`, `...bak-nested-cache-20261123` | earlier compose files (before the local library mount, before any cache change, with the abandoned nested cache mount) |
| `settings.json.bak-pre-audiocache-20261123` | MA settings from before the cache change (mode 600; contains secrets, keep private) |

These files are **not in git**. Copy `audiocache-patch-2.10.13/` somewhere safe (or into the repo) if losing them would hurt.

### 2.3 The compose file (the relevant part)

```yaml
    image: ghcr.io/music-assistant/server:latest
    volumes:
      - music-assistant-data:/data
      - <your music folder>:/media/music:ro
      - <your cache folder>:/music-cache
```

## 3. Before any change: back up

Do this before Procedure B, and before anything you are unsure about. MA keeps SQLite databases in `/data`, so stop it first to get a consistent copy.

```bash
BK=$BACKUP_DIR/music-assistant-$(date +%Y%m%d-%H%M)
mkdir -p "$BK" && chmod 700 "$BK"          # settings.json holds MA's encryption key: keep this private
cp $STACK/docker-compose.yml "$BK"/
docker stop music-assistant
docker run --rm --entrypoint sh \
  -v music-assistant_music-assistant-data:/data:ro -v "$BK":/backup \
  ghcr.io/music-assistant/server:latest \
  -c 'tar czf /backup/data-volume.tar.gz -C /data .'
ls -la "$BK"          # expect data-volume.tar.gz of roughly 150-250 MB
docker start music-assistant      # skip this if you are about to upgrade
```

(The `tar` command was dry-run tested against the live volume, writing to `/dev/null`: it works and the data is 225 MB. The real archive write was not exercised.)

Nothing in `$CACHE_DIR` needs backing up: it is a cache and refills itself.

## 4. Procedure A: re-apply the patch (same MA version)

The patched files are copied into the container's writable layer. That survives `docker restart` but **is lost whenever the container is recreated** (image pull, compose edit, `docker compose up`). The `audio_cache_path` setting stays in the data volume, so you only need to put the code back.

```bash
# 1. is it still the version the patch was made for?
docker exec music-assistant /app/venv/bin/python -c 'from importlib.metadata import version; print(version("music-assistant"))'     # must print 2.10.13

# 2. is the patch already there? (this prints the path if it is, an error if not)
docker exec music-assistant ls /app/venv/lib/python3.14/site-packages/music_assistant/providers/apple_music/audio_cache.py

# 3. put it back and restart MA
$PATCH/reapply.sh
```

Then check the setting survived and go to [Verify](#6-verify):

```bash
docker cp music-assistant:/data/settings.json - | tar -xO | python3 -I -c "import json,sys; [print(k, v.get('values', {}).get('audio_cache_path')) for k, v in json.load(sys.stdin)['providers'].items() if v.get('domain') == 'apple_music']"
# expect one line per Apple Music instance, ending in: /music-cache
```

If an instance shows `None` instead of `/music-cache`, the setting was lost (see section 9) and you need to set it again (section 8).

`reapply.sh` hardcodes the Python 3.14 package path. If a future image uses another Python, the `docker cp` will fail loudly; use the path printed by the discovery one-liner in Procedure B step 3.

## 5. Procedure B: move the patch to a new MA version

The ported files are only valid for the MA version they were made against, because `provider.py` and `streaming.py` change between releases. So the patch has to be re-checked for each new version before it goes anywhere near production. The commands use the variables from section 0 (`$STACK`, `$PATCH`, ...).

1. **Back up** (section 3). Leave MA stopped.
2. **Pull and recreate without starting**:
   ```bash
   cd $STACK && docker compose pull && docker compose up -d --no-start
   docker run --rm --entrypoint /app/venv/bin/python ghcr.io/music-assistant/server:latest -c 'from importlib.metadata import version; print(version("music-assistant"))'
   ```
   Note the new version (call it `NEW`). A big version jump means MA will migrate its own database on first start; the backup from step 1 is your way back.
3. **Get the new version's pristine Apple Music files** out of the stopped container, and find the package path:
   ```bash
   docker run --rm --entrypoint /app/venv/bin/python ghcr.io/music-assistant/server:latest -c 'import importlib.util,os; print(os.path.join(os.path.dirname(importlib.util.find_spec("music_assistant").origin),"providers","apple_music"))'
   PKG=<the path it printed>
   mkdir -p $STACK/stock-apple_music-NEW && docker cp music-assistant:$PKG/. $STACK/stock-apple_music-NEW/
   ```
4. **See whether upstream touched the three files we patch**:
   ```bash
   for f in constants.py provider.py streaming.py; do cmp -s $STACK/backup-apple_music-2.10.13/$f $STACK/stock-apple_music-NEW/$f && echo "$f unchanged" || echo "$f CHANGED"; done
   ```
   - All unchanged: the existing patched files are good for `NEW`. Skip to step 6.
   - Some changed: build a patched copy of the new files (step 5).
5. **Port the patch to the new files**:
   ```bash
   mkdir -p $STACK/audiocache-patch-NEW && cp -a $STACK/stock-apple_music-NEW/{constants.py,provider.py,streaming.py} $STACK/audiocache-patch-NEW/
   cd $STACK/audiocache-patch-NEW
   patch -p1 --dry-run < $PATCH/audiocache-2.10.13.patch      # look at the result first
   patch -p1 < $PATCH/audiocache-2.10.13.patch                 # apply for real; this also creates audio_cache.py
   ```
   If every hunk applies, continue. If some **fail** (a `.rej` file appears), port those changes by hand. The change points are:
   - `constants.py`: the three constants `CONF_AUDIO_CACHE_PATH`, `AUDIO_CACHE_INDEX_DIR`, `AUDIO_CACHE_FILE_SUFFIX`.
   - `streaming.py`: imports (`aclosing`, `MusicAssistantError`, `AudioCache`, `AudioDownload`, `CONF_AUDIO_CACHE_PATH`); an `audio_cache` attribute set to `None` in `__init__`; in `initialize()`, read the setting and open the cache; in `get_stream_details()`, for the encrypted track branch return one of three results (cache off: unchanged `ENCRYPTED_HTTP`; hit: same type with the cached file as `path`; miss: `StreamType.CUSTOM` with `extra_input_args=["-decryption_key", key]`, `data=AudioDownload(...)`, `can_seek=False`, `allow_seek=True`); plus the new method `stream_track_and_store()`.
   - `provider.py`: import `ConfigEntry` and `ConfigEntryType` at runtime; add the `audio_cache_path` string entry to `get_config_entries()`; add `get_audio_stream()` that hands an `AudioDownload` stream to `streaming_manager.stream_track_and_store()`. If the new MA already has a `get_audio_stream` on this provider (for example for live radio), dispatch on `isinstance(streamdetails.data, AudioDownload)` and keep the existing behaviour for everything else.
   - Compare with the branch (`git show b7234ffa9`) for the exact code. Check that the core still treats `CUSTOM` streams the same way: `_resolve_media_stream_source` in `controllers/streams/audio.py` must pass the stream's `extra_input_args` to ffmpeg and call `provider.get_audio_stream()` for a track.
6. **Smoke test against the new image**, in a throwaway container with no network, so production is not touched. It needs the synthetic test track, already saved as `tools/enc.m4a` (the ffmpeg command to recreate it is just below):
   ```bash
   D=$STACK/audiocache-patch-NEW        # or $PATCH if nothing changed
   docker run --rm --network none --entrypoint /app/venv/bin/python \
     -v $D/constants.py:$PKG/constants.py:ro -v $D/provider.py:$PKG/provider.py:ro \
     -v $D/streaming.py:$PKG/streaming.py:ro -v $D/audio_cache.py:$PKG/audio_cache.py:ro \
     -v $PATCH/tools:/t:ro ghcr.io/music-assistant/server:latest -I /t/e2e_cache.py /t/enc.m4a 2>&1 | grep -E "^[0-9]\.|reference|ALL OK|Traceback"
   ```
   You must see steps `1.` to `6.` and `ALL OK` on the last line. One `Traceback` line from a `ClientConnectionResetError` is expected (the test's fake slow server noticing the client hung up). Any other traceback, or no `ALL OK`: stop here, do not deploy.
   To recreate the test track:
   ```bash
   ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=440:duration=6" -c:a aac -b:a 256k \
     -movflags +frag_keyframe+empty_moov+default_base_moof -f mp4 \
     -encryption_scheme cenc-aes-ctr -encryption_key 000102030405060708090a0b0c0d0e0f \
     -encryption_kid 0f0e0d0c0b0a09080706050403020100 $PATCH/tools/enc.m4a
   ```
7. **Deploy**: copy the four files into the stopped container, then start it:
   ```bash
   for f in constants.py provider.py streaming.py audio_cache.py; do docker cp $D/$f music-assistant:$PKG/$f; done
   docker start music-assistant
   ```
8. [Verify](#6-verify).
9. **Record the new version**: copy `reapply.sh` into the new patch folder, change its version check from `2.10.13` to `NEW`, and use that folder from now on. Keep the old folder.

## 6. Verify

After any procedure, in this order:

```bash
# 1. The provider loaded with the cache on (expect this exact line shortly after start)
docker logs music-assistant 2>&1 | grep "Keeping the audio of played tracks"
#    -> ... [music_assistant.apple_music] Keeping the audio of played tracks in /music-cache

# 2. The folder is writable from inside, the music library stays read-only
docker exec music-assistant sh -c 'touch /music-cache/.rw-test && echo "cache: write OK" && rm /music-cache/.rw-test; touch /media/music/.ro-test 2>&1'
#    -> cache: write OK  /  touch: cannot touch '/media/music/.ro-test': Read-only file system

# 3. The index folder exists on the host
ls -la $CACHE_DIR        # expect a .ma-audio-cache folder (root-owned)

# 4. MA answers
curl -s localhost:8095/info | python3 -c "import json,sys; print(json.load(sys.stdin)['server_version'])"
```

Then a real test with an Apple Music track:

1. Play a track right through, once. A folder `$CACHE_DIR/<Artist>/<Album>/` with the `.m4a` should appear, no `.part` file left, and `.ma-audio-cache/<id>.json` next to it.
2. Play the **same** track again (any device). There should be **no new or changed file**: same size and same modified time, and no duplicate named `... [<id>].m4a`. A duplicate or new `.part` means it downloaded again.
3. For direct proof, run the watcher during the second play and look for an `OPEN` of the `.m4a` with no `CREATE` of a `.part`:
   ```bash
   python3 -u -I $PATCH/tools/cachewatch.py "$CACHE_DIR"
   ```
   (Do not rely on the file's "last read" time: the disk uses `relatime`, which only updates it on the first read after a write.)

Played this way on 2026-11-23: three tracks cached on first play, and a second play of one of them opened the cached file with no write activity.

## 7. Rollback

**Back to stock Apple Music code (cache off), keep everything else.**

```bash
PKG=/app/venv/lib/python3.14/site-packages/music_assistant/providers/apple_music
docker cp $STACK/backup-apple_music-2.10.13/. music-assistant:$PKG/
docker exec music-assistant rm -f $PKG/audio_cache.py
docker restart music-assistant
```
Use the stock files of the version you are actually running. The leftover `audio_cache_path` setting is harmless to stock code in principle, but this is **not tested**; clear it (section 8) if MA complains. Cached files in `$CACHE_DIR` are simply unused.

**Back to the previous compose file** (for example to drop the cache mount): copy the wanted `docker-compose.yml.bak-*` over `docker-compose.yml`, then `cd $STACK && docker compose up -d`. This recreates the container, so the patch is lost: re-apply it (Procedure A) if you still want it.

**Restore all MA data from a section 3 backup.** Destructive: it replaces the live data, and it is how you undo an MA version upgrade that migrated the database. **Not tested.**

```bash
docker stop music-assistant
BK=$BACKUP_DIR/music-assistant-<the backup you want>
docker run --rm --entrypoint sh \
  -v music-assistant_music-assistant-data:/data -v "$BK":/backup:ro \
  ghcr.io/music-assistant/server:latest \
  -c 'find /data -mindepth 1 -delete && tar xzf /backup/data-volume.tar.gz -C /data'
```
If the image tag is the newer one you are rolling away from, point `image:` in the compose file at the older version first (for example `ghcr.io/music-assistant/server:2.10.13`) and recreate the container.

## 8. Operating notes

**Change or turn off the cache folder.** In the MA web UI: Settings, Providers, Apple Music, edit, "Audio cache folder" (the field may show as `audio_cache_path`; I did not check how 2.10.13 labels it). Empty turns the cache off. The path is the path **inside the container**, so it must be a mounted folder (`/music-cache`). Or edit the stored value with MA stopped:

```bash
docker stop music-assistant
T=$(mktemp -d)                      # private (mode 700): the settings file holds secrets
docker cp music-assistant:/data/settings.json "$T"/settings.json
BAK=$STACK/settings.json.bak-$(date +%Y%m%d-%H%M)
cp "$T"/settings.json "$BAK" && chmod 600 "$BAK"
python3 -I - "$T"/settings.json <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for instance_id, config in d["providers"].items():
    if config.get("domain") == "apple_music":
        config.setdefault("values", {})["audio_cache_path"] = "/music-cache"   # "" turns it off
        print(instance_id, config["values"])
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False)
EOF
docker cp "$T"/settings.json music-assistant:/data/settings.json
rm -rf "$T"
docker start music-assistant
```
Never print `settings.json` itself: it contains MA's encryption key.

**See how big it is:** `du -sh $CACHE_DIR` (about 5 to 18 MB per track so far). There is no size cap.

**Empty the cache** (safe: it refills as you play): `docker exec music-assistant sh -c 'rm -rf /music-cache/* /music-cache/.ma-audio-cache'`. The files belong to root (the container runs as root), so deleting them from the host needs `sudo`.

**Where files go:** `$CACHE_DIR/<Artist>/<Album>/<NN> <Title>.m4a` (album artist when the album has one, `2-03 Title` for disc 2). A second file named `... [<id>].m4a` only appears when two different tracks would get the same name.

**If a cached file stops playing:** MA drops a cached file by itself when Apple's key id for the track changes or the file's size differs from its record, then downloads it again on the next play. Deleting one file by hand is also fine; its record is ignored when the file is missing.

**The Filesystem (local disk) provider must never read the cache.** It is not mounted under `/media`, which is why it is safe. Do not move `/music-cache` under `/media`.

## 9. Known gaps and open risks

- **The patch is not part of the image.** It lives in the container's writable layer, so a recreate silently turns the cache off until Procedure A is run (MA still works, the setting stays). A permanent fix is a bind mount of the four files in `docker-compose.yml` or a small custom image. Neither is set up. A bind mount would also pin the old code over a future image, so it needs the same version check as `reapply.sh`.
- **The setting could be dropped.** If MA runs on stock code (after a recreate, before Procedure A) and rewrites its settings, it may remove the `audio_cache_path` key. Not observed, not ruled out. Always run the settings check in Procedure A.
- **Real-Apple tests are limited to first play, a repeat play and three tracks.** Skipping partway through a download and seeking into a not-yet-cached track were tested with the unit tests and a synthetic encrypted file against the 2.10.13 image, not with real Apple Music tracks.
- **Account limits are unchanged.** Apple allows one concurrent stream, and the provider still enforces that. A cache hit also still asks Apple for a licence key (a small request), so playback needs a working subscription.
- **`reapply.sh` itself has never been run.** It was syntax-checked, and the same steps were done by hand when the patch was first installed. It hardcodes the Python 3.14 package path and the version 2.10.13.
- **Unverified in 2.10.13:** how the settings page labels the new field; the rollback leaving a stale `audio_cache_path` in the settings; the restore command in section 7.
- **The branch (`dev`) and production (2.10.13) differ,** so passing tests on the branch say little about production. The smoke test against the real image (Procedure B step 6) is what to trust.

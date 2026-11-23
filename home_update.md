# Updating the home Music Assistant server (Apple Music audio cache)

How to get this fork's Apple Music audio cache onto the production Music Assistant (MA) container at home, keep it there, and roll it back. Written 2026-11-23 against MA **2.10.13**, and updated the same day for the album covers and the index-folder fix. The code is deployed straight from the repository: nothing is ported by hand.

The testing status of every procedure is in [section 9](#9-testing-status-and-known-gaps). Anything marked "not tested" there has not been run.

## 0. Set these first

Host paths are not part of this repository, so the commands use placeholders. Set them once in each new shell (use your own values):

```bash
STACK=/path/to/your/compose/stack        # folder that holds docker-compose.yml for MA
MUSIC_DIR=/path/to/your/music            # host folder mounted read-only at /media/music (your local library)
CACHE_DIR=/path/to/audio-cache           # host folder mounted read-write at /music-cache (this feature)
BACKUP_DIR=/path/to/backups              # where backups go; keep it private
REPO=/path/to/your/checkout              # git checkout of the branch apple-music-audio-cache
TOOLS=$STACK/audiocache-tools            # helper scripts and the test track (section 4, step 1)
```

One-time setup of the checkout: it needs the upstream project as a remote named `upstream`, so the scripts can tell which upstream version the branch is based on.

```bash
git -C "$REPO" remote add upstream https://github.com/music-assistant/server.git
git -C "$REPO" fetch upstream stable --tags
```

The Apple Music provider instance id is looked up from the settings file where needed (it differs per install).

## 1. Which procedure do I need?

| Situation | Do this |
|---|---|
| First deploy, or the container was recreated (image pull, compose change), same MA version | [Procedure A: deploy the code](#4-procedure-a-deploy-the-code-same-ma-version) |
| MA released a newer stable version and you want it | [Procedure B: move to a new MA version](#5-procedure-b-move-to-a-new-ma-version) |
| Something broke | [Rollback](#7-rollback) |
| Change or turn off the cache folder, clear the cache | [Operating notes](#8-operating-notes) |

**Base production code on upstream `stable`, never on `dev`.** Production runs the stable image. `dev` has a newer library database schema (74 against 58 on 2.10.13, checked 2026-11-23): code taken from `dev` can migrate the production library forward in a way the stable image cannot read, and the only way back is a volume backup. The code branch is based on `stable`, so it does not touch the database.

## 2. What this is

- **Feature:** an optional "Audio cache folder" setting on the Apple Music provider. When set, the first play of a track downloads Apple's encrypted file through MA and keeps a copy under `Artist/Album/NN Title.m4a`. Later plays read that file and decrypt it with a fresh licence key, so no audio is downloaded again.
- **The files stay encrypted** (Widevine). Only MA with a valid Apple Music account can play them. The decryption key is never stored.
- **Only complete downloads are kept.** A skipped or failed download leaves nothing behind. Radio and the user's own uploaded (unencrypted) library tracks are not cached. There is no size cap.
- **What is kept next to the audio, and where the metadata is.** The `.m4a` is exactly the file Apple serves, and it is never rewritten: it carries no artist, album or title tags and no picture (checked with `ffprobe` and by reading its MP4 boxes: no `meta`, `ilst` or `covr`; the only extras are the technical tags and Apple's loudness data). The metadata is kept beside it instead:
  - the folder and file name (`Artist/Album/NN Title.m4a`);
  - `cover.jpg` in the album folder: the album's artwork as a JPEG, fetched in the background through MA's own image functions once a track of the album has been stored, and only when the folder has none yet. A track without artwork, or a failed fetch, simply leaves no cover and does not affect the audio;
  - a small record per track in `.ma-audio-cache/<id>.json` (item id, key id, relative path, size, artist, album, title, time cached), which the cache uses to recognise a file. It never holds a key.
- **Branches in the repository:**
  - `apple-music-audio-cache`: the code. It is upstream `stable` plus three commits, so it deploys onto the stable image without porting: the cache itself; "recreate the index folder when it has gone" (before it, clearing the cache folder while MA ran stopped every later download from being kept, see section 8); and "keep the album cover next to the tracks". Nothing but code, strings and generated translations is on it.
  - `audio-cache-policy`: the documentation, including this guide and the helper scripts in `home_update_tools/`.
- **What gets deployed** (six files, copied into the running container): from `music_assistant/providers/apple_music/` the files `audio_cache.py` (new), `constants.py`, `provider.py`, `streaming.py` and `strings.json`; plus `music_assistant/translations/en.json`, which MA loads at runtime for the setting's label and description. Everything else in the container stays as the image shipped it.

### 2.1 Production layout (as of 2026-11-23)

| Item | Value |
|---|---|
| Stack folder (`$STACK`) | holds `docker-compose.yml` and `audiocache-tools/` |
| Container / image | `music-assistant`, `ghcr.io/music-assistant/server:latest` (MA 2.10.13), host networking, `restart: unless-stopped` |
| Data volume | `music-assistant_music-assistant-data` mounted at `/data` (settings, library, auth databases; about 225 MB) |
| Local music library | host `$MUSIC_DIR` mounted **read-only** at `/media/music` (the "Filesystem (local disk)" provider reads `/media`) |
| Audio cache | host `$CACHE_DIR` mounted **read-write** at `/music-cache`, deliberately outside `/media` so the local library never indexes the encrypted files |
| Setting | on the Apple Music provider instance: `audio_cache_path` = `/music-cache` (stored in the data volume) |
| How the code gets in | copied into the container's writable layer: it survives `docker restart` and is **lost when the container is recreated** |

### 2.2 The compose file (the relevant part)

```yaml
    image: ghcr.io/music-assistant/server:latest
    volumes:
      - music-assistant-data:/data
      - <your music folder>:/media/music:ro
      - <your cache folder>:/music-cache
```

## 3. Before a bigger change: back up

Procedure A replaces code files only and saves the old ones itself, so it needs no data backup. Do this before Procedure B and before anything you are unsure about. MA keeps SQLite databases in `/data`, so stop it first to get a consistent copy.

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

Nothing in `$CACHE_DIR` needs backing up: it is a cache and refills itself.

## 4. Procedure A: deploy the code (same MA version)

Use this for the first deploy, and after any container recreation (the copied files are lost then; the setting stays in the data volume).

**1. Set up the tools folder (once).** The helper scripts are on the `audio-cache-policy` branch in `home_update_tools/`. Copy that folder's three files into `$TOOLS`, and create the synthetic encrypted test track used by the smoke test:

```bash
mkdir -p "$TOOLS"
# copy deploy-audiocache.sh, e2e_cache.py and cachewatch.py from home_update_tools/ into $TOOLS, then:
chmod +x "$TOOLS"/deploy-audiocache.sh
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=440:duration=6" -c:a aac -b:a 256k \
  -movflags +frag_keyframe+empty_moov+default_base_moof -f mp4 \
  -encryption_scheme cenc-aes-ctr -encryption_key 000102030405060708090a0b0c0d0e0f \
  -encryption_kid 0f0e0d0c0b0a09080706050403020100 "$TOOLS"/enc.m4a
```

**2. Check the checkout.** It must be on the code branch, clean, and only the feature commits (section 2) on top of upstream `stable`:

```bash
git -C "$REPO" status -sb                      # on apple-music-audio-cache, nothing modified
git -C "$REPO" log --oneline upstream/stable..HEAD    # only the feature commits
```

**3. Check nothing is playing**, because MA restarts at the end (about a minute). No lines, or a "Finished" after the last "Start", means idle:

```bash
docker logs --since 15m music-assistant 2>&1 | grep -E "Start Queue Flow|Finished Queue Flow"
```

**4. Smoke test** the exact files you are about to deploy, in a throwaway container with no network. Production is not touched:

```bash
IMG=$(docker inspect music-assistant --format '{{.Image}}')
PKG=$(docker run --rm --entrypoint /app/venv/bin/python $IMG -c 'import importlib.util,os; print(os.path.dirname(importlib.util.find_spec("music_assistant").origin))')/providers/apple_music
R=$REPO/music_assistant/providers/apple_music
docker run --rm --network none --entrypoint /app/venv/bin/python \
  -v $R/constants.py:$PKG/constants.py:ro -v $R/provider.py:$PKG/provider.py:ro \
  -v $R/streaming.py:$PKG/streaming.py:ro -v $R/audio_cache.py:$PKG/audio_cache.py:ro \
  -v $TOOLS:/t:ro $IMG -I /t/e2e_cache.py /t/enc.m4a 2>&1 | grep -E "^[0-9]\.|reference|ALL OK|Traceback"
```

You must see steps `1.` to `7.` and `ALL OK` on the last line. Step 1 also checks that a `cover.jpg` appears next to the track while the audio file stays byte-identical, and step 7 that a failed cover fetch costs nothing but the cover. (The test stands in for MA's task and image functions, so it does not fetch a real picture.) One `Traceback` line from a `ClientConnectionResetError` is expected (the test's fake slow server noticing the client hung up). Any other traceback, or no `ALL OK`: stop, do not deploy.

**5. Dry run of the safety checks** (changes nothing):

```bash
REPO=$REPO BACKUP_DIR=$BACKUP_DIR DRY_RUN=1 $TOOLS/deploy-audiocache.sh
```

It must print `checks passed: ...` and `dry run: nothing changed`. The checks are: the checkout has no uncommitted changes in the deployed files; the container's image revision is in the branch's history; and upstream did not change those files between that image and the branch base. If one fails, the message says what to do (usually: rebase the branch, see Procedure B).

**6. Deploy:**

```bash
REPO=$REPO BACKUP_DIR=$BACKUP_DIR $TOOLS/deploy-audiocache.sh
```

It saves the files being replaced to `$BACKUP_DIR/pre-deploy-<time>/`, copies the six files in, compares each byte for byte with the checkout, then restarts MA. It stops with an error, before restarting, if any file differs after the copy.

**7. Verify** ([section 6](#6-verify)), and check the setting survived:

```bash
docker cp music-assistant:/data/settings.json - | tar -xO | python3 -I -c "import json,sys; [print(k, v.get('values', {}).get('audio_cache_path')) for k, v in json.load(sys.stdin)['providers'].items() if v.get('domain') == 'apple_music']"
# expect one line per Apple Music instance, ending in: /music-cache
```

If an instance shows `None`, the setting was lost (see section 9): set it again (section 8).

## 5. Procedure B: move to a new MA version

The branch is a few commits on top of upstream `stable`, so a new MA version means rebasing those commits onto the code the new image was built from, then deploying as in Procedure A.

1. **Back up** (section 3). Leave MA stopped.
2. **Pull and recreate without starting**, and note the version:
   ```bash
   cd $STACK && docker compose pull && docker compose up -d --no-start
   docker run --rm --entrypoint /app/venv/bin/python $(docker inspect music-assistant --format '{{.Image}}') -c 'from importlib.metadata import version; print(version("music-assistant"))'
   ```
   A big version jump means MA will migrate its own database on first start; the backup from step 1 is your way back.
3. **Rebase the branch onto the exact code of the new image.** The image records its upstream commit:
   ```bash
   REV=$(docker inspect music-assistant --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
   git -C "$REPO" fetch upstream stable --tags
   OLD=$(git -C "$REPO" merge-base HEAD upstream/stable)       # the current base of the branch
   git -C "$REPO" rebase --onto "$REV" "$OLD" HEAD
   ```
   If it stops on conflicts, resolve them by hand. The change points are:
   - `constants.py`: the four constants `CONF_AUDIO_CACHE_PATH`, `AUDIO_CACHE_INDEX_DIR`, `AUDIO_CACHE_FILE_SUFFIX`, `AUDIO_CACHE_COVER_NAME`.
   - `streaming.py`: an `audio_cache` attribute set to `None` in `__init__`; in `initialize()`, read the setting and open the cache; in `get_stream_details()`, for the encrypted track branch return one of three results (cache off: the unchanged `ENCRYPTED_HTTP`; hit: same type with the cached file as `path`; miss: `StreamType.CUSTOM` with `extra_input_args=["-decryption_key", key]`, `data=AudioDownload(...)`, `can_seek=False`, `allow_seek=True`); plus the new method `stream_track_and_store()`.
   - `provider.py`: the `audio_cache_path` string entry in `get_config_entries()`, and a `get_audio_stream()` that hands an `AudioDownload` stream to `streaming_manager.stream_track_and_store()`. If the new MA already has a `get_audio_stream` on this provider (for example for live radio), dispatch on `isinstance(streamdetails.data, AudioDownload)` and keep the existing behaviour for everything else.
   - `strings.json` and `translations/en.json`: the label and description of the setting. Regenerate `en.json` with `python -m scripts.build_translations` instead of merging it by hand.
   - Check that the core still treats `CUSTOM` streams the same way: `_resolve_media_stream_source` in `controllers/streams/audio.py` must pass the stream's `extra_input_args` to ffmpeg and call `provider.get_audio_stream()` for a track.
   - Check the three core functions the cover code (`_store_cover` and `_fetch_cover` in `audio_cache.py`) calls, because the new image may have changed them: `mass.create_task(target, *args, task_id=...)` in `mass.py`, and `mass.metadata.get_image_url_for_item(item, image_type)` and `mass.metadata.get_thumbnail(path, provider, size, image_format, flatten_transparency)` in `controllers/metadata/images.py`. If one changed, adjust the calls; the unit tests mock them, so they will not notice.
4. **Re-run the project checks** from the checkout (needs the project's dev environment, see `scripts/setup.sh`): `pre-commit run --files <the changed files>` and `pytest tests/providers/apple_music`. Any failure: stop, do not deploy.
5. **Smoke test against the new image**: Procedure A, step 4 (`IMG` now resolves to the new image because the container was recreated).
6. **Deploy**: Procedure A, steps 5 and 6. The script works on the stopped container and starts it.
7. **Verify** ([section 6](#6-verify)).

If the rebase or the checks cannot be made to work, run the new image as it ships (`docker start music-assistant`): MA works without the cache, and the cached files stay on disk for later.

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

1. Play a track right through, once. A folder `$CACHE_DIR/<Artist>/<Album>/` with the `.m4a` should appear, no `.part` file left, and `.ma-audio-cache/<id>.json` next to it. A few seconds after the track is stored a `cover.jpg` should appear in the same album folder (`file "$CACHE_DIR/<Artist>/<Album>/cover.jpg"` should say JPEG). Tracks cached by an earlier deploy keep working: the cache format did not change, but they get no cover, because a cover is only fetched when a track is stored (section 8).
2. Play the **same** track again (any device). There should be **no new or changed file**: same size and same modified time, and no duplicate named `... [<id>].m4a`. A duplicate or new `.part` means it downloaded again.
3. For direct proof, run the watcher during the second play and look for an `OPEN` of the `.m4a` with no `CREATE` of a `.part`:
   ```bash
   python3 -u -I $TOOLS/cachewatch.py "$CACHE_DIR"
   ```
   (Do not rely on the file's "last read" time: the disk uses `relatime`, which only updates it on the first read after a write.)

## 7. Rollback

**Back to the stock image, cache code gone (simplest).** Recreate the container from the image: the copied files disappear and the stock Apple Music code is back. The leftover `audio_cache_path` setting should be ignored by stock code, but this is **not tested**; clear it (section 8) if MA complains. Cached files in `$CACHE_DIR` are simply unused.

```bash
cd $STACK && docker compose up -d --force-recreate
```

**Back to the files that were there before a deploy.** Each deploy saved them in `$BACKUP_DIR/pre-deploy-<time>/` (`apple_music/` and `en.json`). Copy them back into the container, then `docker restart music-assistant`. The package path inside the container is printed by the first command of Procedure A, step 4 (`PKG`; `en.json` lives in the `translations` folder next to `providers`).

**Back to the previous compose file** (for example to drop the cache mount): copy the wanted compose backup over `docker-compose.yml`, then `cd $STACK && docker compose up -d`. This recreates the container, so the code is lost: deploy it again (Procedure A) if you still want the cache.

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

**Change or turn off the cache folder.** In the MA web UI: Settings, Providers, Apple Music, edit, "Audio cache folder". Empty turns the cache off. The path is the path **inside the container**, so it must be a mounted folder (`/music-cache`). Or edit the stored value with MA stopped:

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

**Empty the cache** (safe: it refills as you play): `docker exec music-assistant sh -c 'rm -rf /music-cache/* /music-cache/.ma-audio-cache'`. The files belong to root (the container runs as root), so deleting them from the host needs `sudo`. Emptying the folder while MA runs is fine from the commit "recreate the index folder when it has gone" onward: the next download recreates `.ma-audio-cache` by itself.

**If nothing is being kept after the folder was cleared.** Symptom, in the MA log on every play of a track that is not cached: `Could not start storing audio in /music-cache: [Errno 2] No such file or directory: '/music-cache/.ma-audio-cache/<id>.part'`. Playback itself is fine. This happens on code from before that commit, because the index folder was only created when MA started. Fix: deploy the current code (Procedure A), or as a stopgap create the folder (`mkdir $CACHE_DIR/.ma-audio-cache`) or restart MA. Check the folder is the same one the container sees: `stat -c %i $CACHE_DIR` on the host and `docker exec music-assistant stat -c %i /music-cache` must print the same number (a host folder that was deleted and recreated may leave the container looking at the old, deleted one, which a `docker restart music-assistant` should fix; not tested here, the one time this was investigated the numbers matched and the folder had only been emptied).

**Where files go:** `$CACHE_DIR/<Artist>/<Album>/<NN> <Title>.m4a` and `$CACHE_DIR/<Artist>/<Album>/cover.jpg` (album artist when the album has one, `2-03 Title` for disc 2). A second file named `... [<id>].m4a` only appears when two different tracks would get the same name. Both are in the album folder, and the cover is one file per album.

**Covers.** A cover is fetched in the background right after a track is stored, if the album folder has no `cover.jpg` yet. It is not fetched when a cached track is replayed, so tracks cached before covers existed, and albums whose fetch failed (no network to the image server, no artwork), stay without one until another track of that album is stored. To give an album its cover, delete that track's `.m4a` (its record is ignored once the file is gone) and play the track again. A failed fetch is logged only at debug level; turn on debug logging for the provider if you need to see why.

**If a cached file stops playing:** MA drops a cached file by itself when Apple's key id for the track changes or the file's size differs from its record, then downloads it again on the next play. Deleting one file by hand is also fine; its record is ignored when the file is missing.

**The Filesystem (local disk) provider must never read the cache.** It is not mounted under `/media`, which is why it is safe. Do not move `/music-cache` under `/media`.

## 9. Testing status and known gaps

**What has been run on the production host:**

- The backup command (as a dry run, writing to `/dev/null`).
- The smoke test, in a throwaway container from the production image.
- The verification steps and the settings edit.
- Real playback: three tracks cached on first play, and a repeat play of a cached track opened the cached file with no write activity.
- Procedure A with the deploy script from the repository (2026-11-23): the smoke test passed on the production image, the safety-check dry run passed, the deploy copied the six files and compared each byte for byte with the checkout, and MA restarted and loaded the provider with the cache on. The setting survived the restart, the new label and description are in the container's translations, and the cache folder was consistent afterwards (31 tracks, each with a record of the same size, no leftover `.part` file). That folder was emptied by hand later the same evening, which is what exposed the index-folder bug below.
- **The index-folder bug (2026-11-23).** After the folder was emptied while MA ran, the production log showed `Could not start storing audio ... .ma-audio-cache/<id>.part: No such file or directory` and nothing was kept; the host and container were confirmed to see the same folder, and the container could write to it. The cause was reproduced by a unit test that fails on the old code with that same message and passes on the fix. The fix was deployed with Procedure A and MA restarted.
- **Replays of cached tracks.** On the build deployed at 20:25: a replay read both the cached file and its record 0.5 s after the play request, with no download and no new file (seen from their last-read times, the first read after a write). On the current build (deployed 22:52): the watcher saw the cached `.m4a` opened with no `.part` created, and no new file.
- **Album covers (2026-11-23).** Tested by six unit tests (the cover is written next to the track, one fetch per album, no artwork, not a JPEG, a failed fetch costing nothing, and a slow fetch not holding up the end of the track) and by steps 1 and 7 of the smoke test on the production image. On the live server, a track from a new album was stored at 22:53:21 and its `cover.jpg` appeared 0.3 s later: a real 1000 x 1000 JPEG of about 339 KB, written through a `cover.jpg.part` that was then renamed, none left behind. The tracks cached before covers existed got none, as described in section 8.

**Not run:** Procedure B on a newer MA version (the rebase step in particular), the rollback and restore commands, and the UI path in section 8. A failed cover fetch and two tracks of one new album finishing at the same moment have only been tested with mocks, not on the live server.

**Known gaps and open risks:**

- **The code is not part of the image.** It lives in the container's writable layer, so a recreate silently turns the cache off until Procedure A is run (MA still works, the setting stays). A permanent fix is a bind mount of the six files in `docker-compose.yml` or a small custom image. Neither is set up. A bind mount would also pin the old code over a future image, so it needs the same version checks as the deploy script.
- **The setting could be dropped.** If MA runs on stock code (after a recreate, before Procedure A) and rewrites its settings, it may remove the `audio_cache_path` key. Not observed, not ruled out. Always run the settings check in Procedure A, step 7.
- **Real-Apple tests are limited to first plays and repeat plays of a handful of tracks.** Skipping partway through a download and seeking into a not-yet-cached track were tested with unit tests and a synthetic encrypted file against the production image, not with real Apple Music tracks.
- **A missing cover leaves no visible error.** A failed cover fetch is only logged at debug level, and it is not retried until another track of that album is stored. The cover depends on MA's own image functions, which a new MA version may change (Procedure B, step 3 lists what to check).
- **Account limits are unchanged.** Apple allows one concurrent stream, and the provider still enforces that. A cache hit also still asks Apple for a licence key (a small request), so playback needs a working subscription.
- **A restart logs `Error unloading player` for Chromecast players** (a timeout in the Chromecast teardown while MA shuts down). Seen once, at the 2026-11-23 restart; it comes from the Chromecast provider, not from the cache.
- **Unverified:** how the settings page shows the new field (its description now also mentions the album cover) after the translations file is deployed; the rollback leaving a stale `audio_cache_path` in the settings; the restore command in section 7.

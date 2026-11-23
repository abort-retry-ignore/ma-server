# Updating the home Music Assistant server (Apple Music audio cache)

How to get this fork's Apple Music audio cache onto the production Music Assistant (MA) container at home, keep it there, and roll it back. Written 2026-11-23 against MA **2.10.13**, and updated the same day for the album covers, the index-folder fix and a restructure that keeps the branch out of upstream's files. The code is deployed straight from the repository: nothing is ported by hand.

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

The cache folder is set with an environment variable in the compose file (section 2.2), not in MA's settings, so no provider instance id is needed anywhere.

## 1. Which procedure do I need?

| Situation | Do this |
|---|---|
| First deploy, or the container was recreated (image pull, compose change), same MA version | [Procedure A: deploy the code](#4-procedure-a-deploy-the-code-same-ma-version) |
| MA released a newer stable version and you want it | [Procedure B: move to a new MA version](#5-procedure-b-move-to-a-new-ma-version) |
| Something broke | [Rollback](#7-rollback) |
| Change or turn off the cache folder, clear the cache | [Operating notes](#8-operating-notes) |

**Base production code on upstream `stable`, never on `dev`.** Production runs the stable image. `dev` has a newer library database schema (74 against 58 on 2.10.13, checked 2026-11-23): code taken from `dev` can migrate the production library forward in a way the stable image cannot read, and the only way back is a volume backup. The code branch is based on `stable`, so it does not touch the database.

## 2. What this is

- **Feature:** an optional environment variable, `MASS_APPLE_MUSIC_AUDIO_CACHE`, that names a folder inside the container. When it is set, the first play of a track downloads Apple's encrypted file through MA and keeps a copy under `Artist/Album/NN Title.m4a`. Later plays read that file and decrypt it with a fresh licence key, so no audio is downloaded again.
- **The files stay encrypted** (Widevine). Only MA with a valid Apple Music account can play them. The decryption key is never stored.
- **Only complete downloads are kept.** A skipped or failed download leaves nothing behind. Radio and the user's own uploaded (unencrypted) library tracks are not cached. There is no size cap.
- **What is kept next to the audio, and where the metadata is.** The `.m4a` is exactly the file Apple serves, and it is never rewritten: it carries no artist, album or title tags and no picture (checked with `ffprobe` and by reading its MP4 boxes: no `meta`, `ilst` or `covr`; the only extras are the technical tags and Apple's loudness data). The metadata is kept beside it instead:
  - the folder and file name (`Artist/Album/NN Title.m4a`);
  - `cover.jpg` in the album folder: the album's artwork as a JPEG, fetched in the background through MA's own image functions, when a track of the album is stored and when a track is played from the cache, and only while the folder has none. A track without artwork, or a failed fetch, simply leaves no cover and does not affect the audio;
  - a small record per track in `.ma-audio-cache/<id>.json` (item id, key id, relative path, size, artist, album, title, time cached), which the cache uses to recognise a file. It never holds a key.
- **Branches in the repository:**
  - `apple-music-audio-cache`: the code. It is upstream `stable` plus one commit, so it deploys onto the stable image without porting. It adds two new files, `audio_cache.py` (the storage) and `cached_provider.py` (a subclass of the provider and of its streaming manager), and changes two lines of one upstream file: `__init__.py`, where `setup()` returns the subclass. `constants.py`, `provider.py`, `streaming.py`, `strings.json` and `en.json` are exactly upstream's. That is what keeps moving to a newer MA version small: the logic never sits inside a function upstream also changes. Nothing but code is on the branch.
  - `audio-cache-policy`: the documentation, including this guide and the helper scripts in `home_update_tools/`.
- **What gets deployed:** every file of `music_assistant/providers/apple_music/` (23 at the time of writing; the branch changes only `__init__.py` and adds `audio_cache.py` and `cached_provider.py`) plus `music_assistant/translations/en.json`. All the others are identical to the image, so copying them all also puts back anything an older version of the branch had changed. Everything else in the container stays as the image shipped it.

### 2.1 Production layout (as of 2026-11-23)

| Item | Value |
|---|---|
| Stack folder (`$STACK`) | holds `docker-compose.yml` and `audiocache-tools/` |
| Container / image | `music-assistant`, `ghcr.io/music-assistant/server:latest` (MA 2.10.13), host networking, `restart: unless-stopped` |
| Data volume | `music-assistant_music-assistant-data` mounted at `/data` (settings, library, auth databases; about 225 MB) |
| Local music library | host `$MUSIC_DIR` mounted **read-only** at `/media/music` (the "Filesystem (local disk)" provider reads `/media`) |
| Audio cache | host `$CACHE_DIR` mounted **read-write** at `/music-cache`, deliberately outside `/media` so the local library never indexes the encrypted files |
| Setting | the environment variable `MASS_APPLE_MUSIC_AUDIO_CACHE=/music-cache` in the compose file (not in MA's settings) |
| How the code gets in | copied into the container's writable layer: it survives `docker restart` and is **lost when the container is recreated** |

### 2.2 The compose file (the relevant part)

```yaml
    image: ghcr.io/music-assistant/server:latest
    environment:
      - MASS_APPLE_MUSIC_AUDIO_CACHE=/music-cache   # path inside the container; empty or removed turns the cache off
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

Use this for the first deploy, and after any container recreation (the copied files are lost then; the environment variable stays in the compose file). A change to the compose file recreates the container, so after one run `cd $STACK && docker compose up -d --no-start` and then this procedure: the deploy script starts the container.

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

**2. Check the checkout.** It must be on the code branch, clean, and exactly one commit on top of upstream `stable`:

```bash
git -C "$REPO" status -sb                      # on apple-music-audio-cache, nothing modified
git -C "$REPO" log --oneline upstream/stable..HEAD    # exactly the feature commit
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
  -v $R:$PKG:ro -v $TOOLS:/t:ro $IMG -I /t/e2e_cache.py /t/enc.m4a 2>&1 | grep -E "^[0-9]\.|reference|ALL OK|Traceback"
```

You must see steps `1.` to `8.` and `ALL OK` on the last line. Step 1 also checks that a `cover.jpg` appears next to the track while the audio file stays byte-identical, step 7 that a failed cover fetch costs nothing but the cover, and step 8 that playing a cached track whose album has no cover fetches it. (The test stands in for MA's task and image functions, so it does not fetch a real picture.) One `Traceback` line from a `ClientConnectionResetError` is expected (the test's fake slow server noticing the client hung up). Any other traceback, or no `ALL OK`: stop, do not deploy.

**5. Dry run of the safety checks** (changes nothing):

```bash
REPO=$REPO BACKUP_DIR=$BACKUP_DIR DRY_RUN=1 $TOOLS/deploy-audiocache.sh
```

It must print `checks passed: ...` and `dry run: nothing changed`. The checks are: the checkout has no uncommitted changes in the deployed files; the container's image revision is in the branch's history; and upstream did not change those files between that image and the branch base. If one fails, the message says what to do (usually: rebase the branch, see Procedure B).

**6. Deploy:**

```bash
REPO=$REPO BACKUP_DIR=$BACKUP_DIR $TOOLS/deploy-audiocache.sh
```

It saves the files being replaced to `$BACKUP_DIR/pre-deploy-<time>/`, copies every file of the provider folder and `en.json` in, compares each byte for byte with the checkout, then restarts MA. It also names any file the container has that the checkout has not. It stops with an error, before restarting, if any file differs after the copy.

**7. Verify** ([section 6](#6-verify)), and check the container has the setting:

```bash
docker exec music-assistant printenv MASS_APPLE_MUSIC_AUDIO_CACHE
# expect: /music-cache
```

If it prints nothing, the compose file does not set it (section 2.2): add it and recreate the container (section 8).

## 5. Procedure B: move to a new MA version

The branch is one commit on top of upstream `stable`, so a new MA version means rebasing that commit onto the code the new image was built from, then deploying as in Procedure A.

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
   Only `__init__.py` is a file of upstream, so a conflict can only come from upstream changing `setup()` or the imports there: keep upstream's version and put back the one import and the one `return CachingAppleMusicProvider(...)` line. (A merge works as well and needs no force-push: `git merge "$REV"` gives one conflict resolution. `git config rerere.enabled true` makes git remember resolutions.)

   **Check what `cached_provider.py` relies on in upstream.** Replaying cleanly does not mean it still works, because the unit tests mock the rest:
   - the provider creates `self.streaming_manager` in `__init__` and `handle_async_init` calls its `initialize()`;
   - `get_stream_details(item_id)` of the streaming manager still returns, for an encrypted track, a `StreamDetails` of type `ENCRYPTED_HTTP` with the file's URL as `path` and the key as `decryption_key` (anything else is left alone, so a new stream type of upstream is not cached but still plays);
   - `_get_decryption_key` still has parameters named `key_id` (bytes) and `item_id`. They are looked up by name; if upstream renames them, tracks still play and the log says `Audio cache skipped, no key id in the key request`, so check the log after an upgrade;
   - `MusicProvider.get_audio_stream(streamdetails, seek_position)`: if upstream's provider defines its own, our subclass hands it everything that is not ours;
   - the core's handling of `CUSTOM` streams: `_resolve_media_stream_source` in `controllers/streams/audio.py` must pass the stream's `extra_input_args` to ffmpeg and call `provider.get_audio_stream()` for a track;
   - for the covers: `provider.get_track(item_id)` (used to find the picture of a track that is played from the cache), `mass.create_task(target, *args, task_id=...)` in `mass.py`, and `mass.metadata.get_image_url_for_item(item, image_type)` and `mass.metadata.get_thumbnail(path, provider, size, image_format, flatten_transparency)` in `controllers/metadata/images.py`.

   The tests fail first if one of these changed: `test_the_key_function_of_upstream_still_has_the_parameters_we_look_for`, `test_setup_gives_the_provider_that_keeps_audio` and the stream-details tests.
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

1. Play a track right through, once. A folder `$CACHE_DIR/<Artist>/<Album>/` with the `.m4a` should appear, no `.part` file left, and `.ma-audio-cache/<id>.json` next to it. A few seconds after the track is stored a `cover.jpg` should appear in the same album folder (`file "$CACHE_DIR/<Artist>/<Album>/cover.jpg"` should say JPEG). Tracks cached by an earlier deploy keep working, the cache format did not change; they get their cover the first time they are played from the cache (section 8).
2. Play the **same** track again (any device). There should be **no new or changed file**: same size and same modified time, and no duplicate named `... [<id>].m4a`. A duplicate or new `.part` means it downloaded again.
3. For direct proof, run the watcher during the second play and look for an `OPEN` of the `.m4a` with no `CREATE` of a `.part`:
   ```bash
   python3 -u -I $TOOLS/cachewatch.py "$CACHE_DIR"
   ```
   (Do not rely on the file's "last read" time: the disk uses `relatime`, which only updates it on the first read after a write.)

## 7. Rollback

**Back to the stock image, cache code gone (simplest).** Recreate the container from the image: the copied files disappear and the stock Apple Music code is back. Stock code does not read the environment variable (remove it from the compose file if you like), and the `audio_cache_path` setting that earlier versions stored in the data volume is ignored too; that last point is **not tested**. Cached files in `$CACHE_DIR` are simply unused.

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

**Change or turn off the cache folder.** Edit `MASS_APPLE_MUSIC_AUDIO_CACHE` in the compose file. It is the path **inside the container**, so it must be a mounted folder (`/music-cache`); empty, or removing the line, turns the cache off. The change needs the container recreated, which loses the copied code, so keep a copy of the compose file and run:

```bash
cp $STACK/docker-compose.yml $STACK/docker-compose.yml.bak-$(date +%Y%m%d)
cd $STACK && docker compose up -d --no-start
REPO=$REPO BACKUP_DIR=$BACKUP_DIR $TOOLS/deploy-audiocache.sh     # starts the container
```

Earlier versions of the branch had an "Audio cache folder" field on the provider; if `settings.json` in the data volume still has an `audio_cache_path` value under the Apple Music instance, nothing reads it and it can stay. Never print `settings.json`: it contains MA's encryption key.

**See how big it is:** `du -sh $CACHE_DIR` (about 5 to 18 MB per track so far). There is no size cap.

**Empty the cache** (safe: it refills as you play): `docker exec music-assistant sh -c 'rm -rf /music-cache/* /music-cache/.ma-audio-cache'`. The files belong to root (the container runs as root), so deleting them from the host needs `sudo`. Emptying the folder while MA runs is fine on the code deployed since 2026-11-23 22:27: the next download recreates `.ma-audio-cache` by itself.

**If nothing is being kept after the folder was cleared.** Symptom, in the MA log on every play of a track that is not cached: `Could not start storing audio in /music-cache: [Errno 2] No such file or directory: '/music-cache/.ma-audio-cache/<id>.part'`. Playback itself is fine. This happened on code from before 2026-11-23 22:27, because the index folder was only created when MA started. Fix: deploy the current code (Procedure A), or as a stopgap create the folder (`mkdir $CACHE_DIR/.ma-audio-cache`) or restart MA. Check the folder is the same one the container sees: `stat -c %i $CACHE_DIR` on the host and `docker exec music-assistant stat -c %i /music-cache` must print the same number (a host folder that was deleted and recreated may leave the container looking at the old, deleted one, which a `docker restart music-assistant` should fix; not tested here, the one time this was investigated the numbers matched and the folder had only been emptied).

**Where files go:** `$CACHE_DIR/<Artist>/<Album>/<NN> <Title>.m4a` and `$CACHE_DIR/<Artist>/<Album>/cover.jpg` (album artist when the album has one, `2-03 Title` for disc 2). A second file named `... [<id>].m4a` only appears when two different tracks would get the same name. Both are in the album folder, and the cover is one file per album.

**Covers.** A cover is fetched in the background in two cases: right after a track is stored, and when a track is played from the cache while its album folder has no `cover.jpg`. The second case gives a cover to tracks cached before covers existed and to albums whose first fetch failed, on the next play. It costs one track lookup (an Apple API call, throttled) and one image fetch per album, and never delays the audio. An album that still has no cover is looked at again once an hour at most, so an album without artwork does not ask on every play; an album that has a cover is not looked at again until MA restarts. This memory of attempts is in RAM only. A failed fetch is logged only at debug level; turn on debug logging for the provider if you need to see why. To redo a cover, delete the `cover.jpg` and play a track of the album (after a restart, or an hour after the last look).

**If a cached file stops playing:** MA drops a cached file by itself when Apple's key id for the track changes or the file's size differs from its record, then downloads it again on the next play. Deleting one file by hand is also fine; its record is ignored when the file is missing.

**The Filesystem (local disk) provider must never read the cache.** It is not mounted under `/media`, which is why it is safe. Do not move `/music-cache` under `/media`.

## 9. Testing status and known gaps

**What has been run on the production host:**

- The backup command (as a dry run, writing to `/dev/null`).
- The smoke test, in a throwaway container from the production image.
- The verification steps.
- Real playback: three tracks cached on first play, and a repeat play of a cached track opened the cached file with no write activity.
- Procedure A with the deploy script from the repository (2026-11-23): the smoke test passed on the production image, the safety-check dry run passed, the deploy copied the six files and compared each byte for byte with the checkout, and MA restarted and loaded the provider with the cache on. That version had the folder as a field in the provider's settings, which survived the restart; the cache folder was consistent afterwards (31 tracks, each with a record of the same size, no leftover `.part` file). That folder was emptied by hand later the same evening, which is what exposed the index-folder bug below.
- **The index-folder bug (2026-11-23).** After the folder was emptied while MA ran, the production log showed `Could not start storing audio ... .ma-audio-cache/<id>.part: No such file or directory` and nothing was kept; the host and container were confirmed to see the same folder, and the container could write to it. The cause was reproduced by a unit test that fails on the old code with that same message and passes on the fix. The fix was deployed with Procedure A and MA restarted.
- **Replays of cached tracks.** On the build deployed at 20:25: a replay read both the cached file and its record 0.5 s after the play request, with no download and no new file (seen from their last-read times, the first read after a write). On the current build (deployed 22:52): the watcher saw the cached `.m4a` opened with no `.part` created, and no new file.
- **The restructure (2026-11-23).** The branch was reshaped so that it adds two files and changes two lines of one upstream file (section 2), and the folder moved from a settings field to the environment variable. Done and checked: all 200 Apple Music unit tests pass; the smoke test passes (all seven steps) with the whole provider folder mounted over the production image's; the single commit replays onto the current upstream `dev` with no conflicts (the previous version conflicted in four files). Deployed by recreating the container with the variable (`docker compose up -d --no-start`) and running the deploy script: it copied and byte-checked all 23 provider files plus `en.json`, and afterwards the five files the old version had changed (`constants.py`, `provider.py`, `streaming.py`, `strings.json`, `en.json`) were identical to upstream's. MA started with `Keeping the audio of played tracks in /music-cache`, the variable is in the container, and the existing cache (5 tracks, 3 covers) was untouched. A real cache hit and a real new cover on this build are listed under "Not run" until they have been seen.
- **Album covers (2026-11-23).** Tested by six unit tests (the cover is written next to the track, one fetch per album, no artwork, not a JPEG, a failed fetch costing nothing, and a slow fetch not holding up the end of the track) and by steps 1 and 7 of the smoke test on the production image. On the live server, a track from a new album was stored at 22:53:21 and its `cover.jpg` appeared 0.3 s later: a real 1000 x 1000 JPEG of about 339 KB, written through a `cover.jpg.part` that was then renamed, none left behind. The tracks cached before covers existed got none at that point; the backfill below is what gives them one.

- **Covers for tracks played from the cache (2026-11-23).** Tested by four more unit tests (the cover is fetched for an album that lacks it, an album with a cover is not asked about again, an album that could not be found is retried only after an hour, and the play of a cached track starts the check; the last one fails when the hook is removed) and by step 8 of the smoke test on the production image. Deployed with the deploy script (all 23 provider files byte-checked, MA restarted, cache on). Seeing it on real tracks is listed under "Not run" until it has been seen.

**Not run:** Procedure B on a newer MA version (the rebase step in particular, and the cache on `dev`'s or any newer streaming code: it applies cleanly to `dev`, but the tests could not be run there because the project's Python environment had the older `music_assistant_models`), the rollback and restore commands, and the compose-edit path in section 8 (it was used once, for the restructure). The cover backfill on real tracks (two kept tracks whose albums have no cover are the test), a failed cover fetch, and two tracks of one new album finishing at the same moment have only been tested with mocks, not on the live server.

**Known gaps and open risks:**

- **The code is not part of the image.** It lives in the container's writable layer, so a recreate silently turns the cache off until Procedure A is run (MA still works, the variable stays in the compose file). A permanent fix is now small: bind-mount the three files the branch adds or changes (`__init__.py`, `audio_cache.py`, `cached_provider.py`) in `docker-compose.yml`, or build a small custom image. Neither is set up. A bind mount would also pin the old `__init__.py` over a future image, so it needs the same version checks as the deploy script.
- **Real-Apple tests are limited to first plays and repeat plays of a handful of tracks.** Skipping partway through a download and seeking into a not-yet-cached track were tested with unit tests and a synthetic encrypted file against the production image, not with real Apple Music tracks.
- **A missing cover leaves no visible error.** A failed cover fetch is only logged at debug level, and it is not retried until another track of that album is stored. The cover depends on MA's own image functions, which a new MA version may change (Procedure B, step 3 lists what to check).
- **Account limits are unchanged.** Apple allows one concurrent stream, and the provider still enforces that. A cache hit also still asks Apple for a licence key (a small request), so playback needs a working subscription.
- **A restart logs `Error unloading player` for Chromecast players** (a timeout in the Chromecast teardown while MA shuts down). Seen once, at the 2026-11-23 restart; it comes from the Chromecast provider, not from the cache.
- **Unverified:** the rollback to stock code with the variable still set and the old `audio_cache_path` value still in the data volume; the restore command in section 7.

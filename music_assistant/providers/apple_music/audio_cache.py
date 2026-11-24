"""Local copies of clear (still-compressed) AAC files of Apple Music tracks."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Final

import aiofiles
import aiofiles.os
from aiofiles.os import wrap
from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import async_json_dumps, async_json_loads
from music_assistant.providers.apple_music.cache_library import CacheLibrary

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aiofiles.threadpool.binary import AsyncBufferedIOBase
    from music_assistant_models.media_items import Track

    from .provider import AppleMusicProvider

# the folder the audio is kept in, as seen by Music Assistant; the cache is off when it is not set
ENV_AUDIO_CACHE_PATH: Final = "MASS_APPLE_MUSIC_AUDIO_CACHE"
AUDIO_CACHE_INDEX_DIR: Final = ".ma-audio-cache"
AUDIO_CACHE_FILE_SUFFIX: Final = ".m4a"
AUDIO_CACHE_COVER_NAME: Final = "cover.jpg"
DOWNLOAD_TIMEOUT: Final = ClientTimeout(total=None, sock_connect=15, sock_read=30)
DOWNLOAD_CHUNK_SIZE: Final = 64 * 1024
# a download nobody finished is only removed when it is older than this, as another
# instance of the provider may be writing to the same folder
STALE_DOWNLOAD_SECONDS: Final = 3600
# an album that has no cover is only looked for again after this long
COVER_RETRY_SECONDS: Final = 3600
MAX_NAME_BYTES: Final = 100
_UNSAFE_CHARS: Final = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_SAFE_ITEM_ID: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_JPEG_START: Final = b"\xff\xd8\xff"
_DOWNLOADING: set[str] = set()
# album folder -> when its cover was last looked for (monotonic), or infinity once it has one
_COVER_ATTEMPTS: dict[str, float] = {}

make_dirs = wrap(os.makedirs)
remove_file = wrap(os.remove)
list_dir = wrap(os.listdir)
replace_file = wrap(os.replace)


@dataclass(frozen=True)
class AudioDownload:
    """What the audio stream of a track that is not in the cache yet needs."""

    url: str
    key_id: str
    decryption_key: str = ""


class AudioCache:
    """
    Clear (still-compressed) AAC files of tracks played before, in a folder tree by artist and album.

    The first play downloads Apple's encrypted bytes (so the player can decrypt live), then
    ffmpeg rewrites the finished file to clear M4A with ``-c:a copy``. Later plays use that
    clear file and need no Widevine key.
    """

    def __init__(
        self, provider: AppleMusicProvider, root: str, library_root: str | None = None
    ) -> None:
        """
        Initialize the cache.

        :param provider: The provider the files belong to.
        :param root: The folder the files are kept in.
        :param library_root: Optional destination for tagged local library files.
        """
        self.provider = provider
        self.logger = provider.logger
        self.root = root
        self._index_dir = os.path.join(root, AUDIO_CACHE_INDEX_DIR)
        self.library = CacheLibrary(self, library_root) if library_root else None

    async def setup(self) -> bool:
        """
        Prepare the folder and remove the downloads an earlier run left unfinished.

        :return: Whether the folder can be used.
        """
        if not Path(self.root).is_absolute():
            self.logger.warning("The audio cache folder %s is not an absolute path", self.root)
            return False
        probe = os.path.join(self._index_dir, ".write-test")
        try:
            await make_dirs(self._index_dir, exist_ok=True)
            async with aiofiles.open(probe, "wb"):
                pass
            await remove_file(probe)
            for name in await list_dir(self._index_dir):
                if name.endswith(".part"):
                    await self._remove_stale_download(os.path.join(self._index_dir, name))
        except OSError as err:
            self.logger.warning("The audio cache folder %s can not be used: %s", self.root, err)
            return False
        return True

    async def lookup(self, item_id: str, key_id: str) -> str | None:
        """
        Return the path of the cached file of a track, if it is there and still matches.

        A file that no longer matches what Apple serves, or that is not the size it was
        stored with, is removed so the next play stores it again.

        :param item_id: The provider item id of the track.
        :param key_id: The key id of the current stream of the track, as hex.
        """
        resolved = await self._resolve_entry(item_id)
        if resolved is None:
            return None
        entry, path, size = resolved
        if entry.get("key_id") != key_id or entry.get("size") != size:
            self.logger.debug("Dropping the cached audio of %s, it no longer matches", item_id)
            if entry.get("location") != "library":
                await self._remove(path)
            await self._remove(self._entry_path(item_id))
            return None
        return path

    async def lookup_clear(self, item_id: str) -> str | None:
        """
        Return the path of a clear cached M4A for a track, if one is kept.

        Used to play without contacting Apple: no key id is required, only a valid
        clear file whose size still matches the index.

        :param item_id: The provider item id of the track.
        """
        resolved = await self._resolve_entry(item_id)
        if resolved is None:
            return None
        entry, path, size = resolved
        if not entry.get("clear") or entry.get("size") != size:
            return None
        return path

    async def is_clear(self, item_id: str) -> bool:
        """
        Return whether the cached file of a track is a clear (unencrypted) M4A.

        :param item_id: The provider item id of the track.
        """
        if (entry := await self._read_entry(item_id)) is None:
            return False
        return bool(entry.get("clear"))

    async def stream_and_store(
        self, item_id: str, download: AudioDownload, track: Track | None
    ) -> AsyncGenerator[bytes]:
        """
        Yield the encrypted file as Apple serves it, then keep a completed clear copy.

        A download that does not reach its end leaves nothing behind.

        :param item_id: The provider item id of the track.
        :param download: URL, key id, and optional decryption key for the clear rewrite.
        :param track: The track, for the name of the file; None when it could not be fetched.
        """
        # ruff: noqa: PLR0915
        fill_id = f"{self.root}\0{item_id}"
        part_path = os.path.join(self._index_dir, f"{item_id}.part")
        sink: AsyncBufferedIOBase | None = None
        claimed = _SAFE_ITEM_ID.match(item_id) is not None and fill_id not in _DOWNLOADING
        if claimed:
            _DOWNLOADING.add(fill_id)
            sink = await self._open_part(part_path)
        received = 0
        expected: int | None = None
        reached_end = False
        try:
            async with self.provider.mass.http_session.get(
                download.url,
                headers={"Accept-Encoding": "identity"},
                timeout=DOWNLOAD_TIMEOUT,
            ) as response:
                response.raise_for_status()
                expected = response.content_length
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    if sink is not None:
                        sink = await self._write_part(sink, chunk, part_path)
                    received += len(chunk)
                    yield chunk
                reached_end = True
        finally:
            if sink is not None:
                with contextlib.suppress(OSError):
                    await sink.close()
                # ensure the part is fully on disk before ffmpeg reads it
                with contextlib.suppress(OSError):
                    fd = os.open(part_path, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                whole = received > 0 and (
                    received == expected or (expected is None and reached_end)
                )
                part_size = 0
                with contextlib.suppress(OSError):
                    part_size = (await aiofiles.os.stat(part_path)).st_size
                self.logger.info(
                    "audio cache download done item_id=%s whole=%s received=%s expected=%s "
                    "part_size=%s reached_end=%s key_len=%s key_fp=%s",
                    item_id,
                    whole,
                    received,
                    expected,
                    part_size,
                    reached_end,
                    len(download.decryption_key or ""),
                    _key_fingerprint(download.decryption_key),
                )
                if whole:
                    store_path = part_path
                    store_size = received
                    is_clear = False
                    if download.decryption_key:
                        # must end in a real media extension or ffmpeg cannot pick a muxer
                        clear_path = f"{part_path}.clear.m4a"
                        t0 = time.monotonic()
                        ok = await self._decrypt_part_to_clear(
                            part_path, clear_path, download.decryption_key
                        )
                        decrypt_ms = int((time.monotonic() - t0) * 1000)
                        if ok:
                            await self._remove(part_path)
                            store_path = clear_path
                            is_clear = True
                            try:
                                store_size = (await aiofiles.os.stat(clear_path)).st_size
                            except OSError:
                                store_size = received
                            self.logger.info(
                                "audio cache clear OK item_id=%s decrypt_ms=%s clear_size=%s",
                                item_id,
                                decrypt_ms,
                                store_size,
                            )
                        else:
                            await self._remove(clear_path)
                            self.logger.warning(
                                "Could not decrypt cached audio of %s after %sms; "
                                "keeping encrypted copy",
                                item_id,
                                decrypt_ms,
                            )
                    else:
                        self.logger.warning(
                            "audio cache has no decryption_key for item_id=%s; storing encrypted",
                            item_id,
                        )
                    await self._store(
                        item_id,
                        download.key_id,
                        store_path,
                        store_size,
                        track,
                        clear=is_clear,
                    )
                else:
                    self.logger.info(
                        "audio cache incomplete item_id=%s received=%s expected=%s; dropped",
                        item_id,
                        received,
                        expected,
                    )
                    await self._remove(part_path)
            if claimed:
                _DOWNLOADING.discard(fill_id)

    def start_missing_cover(self, item_id: str, file_path: str) -> None:
        """
        Get the cover of the album of a track that is kept, in the background, if it has none.

        Meant for a track that is played from the cache: its album may have been kept before
        covers were, or the cover could not be fetched then. An album is looked at once an
        hour at most, and not any more once it has a cover.

        :param item_id: The provider item id of the track.
        :param file_path: The path of the cached file of the track.
        """
        album_dir = os.path.dirname(file_path)
        now = time.monotonic()
        if now - _COVER_ATTEMPTS.get(album_dir, -math.inf) < COVER_RETRY_SECONDS:
            return
        _COVER_ATTEMPTS[album_dir] = now
        self.provider.mass.create_task(
            self._backfill_cover, item_id, album_dir, task_id=f"apple_music.cover.{album_dir}"
        )

    async def _resolve_entry(self, item_id: str) -> tuple[dict[str, Any], str, int] | None:
        """Return index entry, real path and on-disk size when the file is still there."""
        if not _SAFE_ITEM_ID.match(item_id):
            return None
        if (entry := await self._read_entry(item_id)) is None:
            return None
        if entry.get("location") == "library":
            if self.library is not None:
                path = os.path.realpath(os.path.join(self.library.root, str(entry.get("path", ""))))
                if path.startswith(os.path.realpath(self.library.root) + os.sep):
                    try:
                        size = (await aiofiles.os.stat(path)).st_size
                    except OSError:
                        pass
                    else:
                        if size == entry.get("size"):
                            return entry, path, size
            entry = {
                **entry,
                "location": None,
                "path": entry.get("cache_path"),
                "size": entry.get("cache_size"),
            }
        elif entry.get("location") is not None:
            return None
        path = os.path.realpath(os.path.join(self.root, str(entry.get("path", ""))))
        if not path.startswith(os.path.realpath(self.root) + os.sep):
            return None
        try:
            size = (await aiofiles.os.stat(path)).st_size
        except OSError:
            return None
        return entry, path, size

    async def _decrypt_part_to_clear(
        self, encrypted_path: str, clear_path: str, decryption_key: str
    ) -> bool:
        """
        Rewrite an encrypted download to a clear M4A with ffmpeg ``-c:a copy``.

        :param encrypted_path: Path of the finished encrypted part file.
        :param clear_path: Path to write the clear file to.
        :param decryption_key: Widevine content key as hex.
        :return: True when the clear file was written successfully.
        """
        enc_size = 0
        with contextlib.suppress(OSError):
            enc_size = (await aiofiles.os.stat(encrypted_path)).st_size
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-decryption_key",
            decryption_key,
            "-i",
            encrypted_path,
            "-c:a",
            "copy",
            "-y",
            clear_path,
        ]
        # log argv with the key redacted
        safe_args = [
            "<key>" if i and args[i - 1] == "-decryption_key" else a for i, a in enumerate(args)
        ]
        self.logger.info(
            "audio cache ffmpeg clear start enc=%s enc_size=%s clear=%s key_fp=%s argv=%s",
            encrypted_path,
            enc_size,
            clear_path,
            _key_fingerprint(decryption_key),
            safe_args,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()
            returncode = proc.returncode
        except OSError as err:
            self.logger.warning("ffmpeg clear rewrite failed to start: %s", err)
            return False
        err_text = (stderr or b"").decode(errors="replace").strip()
        clear_size = 0
        with contextlib.suppress(OSError):
            clear_size = (await aiofiles.os.stat(clear_path)).st_size
        if returncode != 0:
            self.logger.warning(
                "ffmpeg clear rewrite exited %s for %s clear_size=%s stderr=%s",
                returncode,
                encrypted_path,
                clear_size,
                err_text[:2000] if err_text else "<empty>",
            )
            return False
        if clear_size <= 0:
            self.logger.warning(
                "ffmpeg clear rewrite produced empty file for %s stderr=%s",
                encrypted_path,
                err_text[:2000] if err_text else "<empty>",
            )
            return False
        self.logger.info(
            "audio cache ffmpeg clear done rc=0 clear_size=%s stderr=%s",
            clear_size,
            err_text[:500] if err_text else "<empty>",
        )
        return True

    async def _store(
        self,
        item_id: str,
        key_id: str,
        part_path: str,
        size: int,
        track: Track | None,
        *,
        clear: bool = False,
    ) -> None:
        """Move a finished download to its place in the folder tree and record it."""
        relative = relative_file_path(track)
        if await aiofiles.os.path.exists(os.path.join(self.root, relative)):
            name = PurePath(relative)
            relative = str(name.with_name(f"{name.stem} [{item_id}]{name.suffix}"))
        target = os.path.join(self.root, relative)
        entry = {
            "item_id": item_id,
            "key_id": key_id,
            "path": relative,
            "size": size,
            "clear": clear,
            "artist": track.artists[0].name if track and track.artists else None,
            "album": track.album.name if track and track.album else None,
            "title": track.name if track else None,
            "cached_at": utc().isoformat(timespec="seconds"),
        }
        try:
            await make_dirs(os.path.dirname(target), exist_ok=True)
            await replace_file(part_path, target)
            await self._write_entry(item_id, entry)
        except OSError as err:
            self.logger.warning("Could not store the audio of %s: %s", item_id, err)
            await self._remove(part_path)
            await self._remove(target)
            return
        self.logger.info(
            "Stored the audio of %s as %s clear=%s size=%s",
            item_id,
            relative,
            clear,
            size,
        )
        if clear and self.library is not None:
            await self.library.publish(item_id, track)
            if resolved := await self._resolve_entry(item_id):
                target = resolved[1]
        if track is not None:
            # in the background, so a slow image server never holds up the end of the track
            album_dir = os.path.dirname(target)
            self.provider.mass.create_task(
                self._store_cover,
                track,
                album_dir,
                item_id,
                task_id=f"apple_music.cover.{album_dir}",
            )

    async def _open_part(self, part_path: str) -> AsyncBufferedIOBase | None:
        """Open the file a download is written to, None when that is not possible."""
        try:
            # the folder may have been cleared by hand since setup
            await make_dirs(self._index_dir, exist_ok=True)
            return await aiofiles.open(part_path, "wb")
        except OSError as err:
            self.logger.warning("Could not start storing audio in %s: %s", self.root, err)
            return None

    async def _write_part(
        self,
        sink: AsyncBufferedIOBase,
        chunk: bytes,
        part_path: str,
    ) -> AsyncBufferedIOBase | None:
        """Write a chunk of a download; on an error the copy is given up, playback goes on."""
        try:
            await sink.write(chunk)
        except OSError as err:
            self.logger.warning("Could not write to the audio cache, playing without it: %s", err)
            await sink.close()
            await self._remove(part_path)
            return None
        return sink

    async def _read_entry(self, item_id: str) -> dict[str, Any] | None:
        """Return what is recorded of a cached track."""
        try:
            async with aiofiles.open(self._entry_path(item_id), encoding="utf-8") as file:
                entry = await async_json_loads(await file.read())
        except OSError, ValueError:
            return None
        return entry if isinstance(entry, dict) else None

    async def _write_entry(self, item_id: str, entry: dict[str, Any]) -> None:
        """Record a cached track, so that a reader never sees half of it."""
        path = self._entry_path(item_id)
        async with aiofiles.open(f"{path}.tmp", "w", encoding="utf-8") as file:
            await file.write(await async_json_dumps(entry, indent=True))
        await replace_file(f"{path}.tmp", path)

    def _entry_path(self, item_id: str) -> str:
        """Return where what is recorded of a cached track is kept."""
        return os.path.join(self._index_dir, f"{item_id}.json")

    async def _remove_stale_download(self, path: str) -> None:
        """Remove an unfinished download that nothing is writing to any more."""
        with contextlib.suppress(OSError):
            if time.time() - (await aiofiles.os.stat(path)).st_mtime > STALE_DOWNLOAD_SECONDS:
                await remove_file(path)

    async def _remove(self, path: str) -> None:
        """Remove a file, if it is there."""
        with contextlib.suppress(OSError):
            await remove_file(path)

    async def _backfill_cover(self, item_id: str, album_dir: str) -> None:
        """Get the cover of an album from one of its tracks that is kept, unless it has one."""
        if await aiofiles.os.path.exists(os.path.join(album_dir, AUDIO_CACHE_COVER_NAME)):
            _COVER_ATTEMPTS[album_dir] = math.inf
            return
        try:
            track = await self.provider.get_track(item_id)
        except (MusicAssistantError, ClientError, TimeoutError) as err:
            self.logger.debug("No track details to get the cover with: %s", err)
            return
        await self._store_cover(track, album_dir, item_id)

    async def _store_cover(self, track: Track, album_dir: str, item_id: str | None = None) -> None:
        """Keep the cover of an album next to its tracks, unless it is there already."""
        cover = os.path.join(album_dir, AUDIO_CACHE_COVER_NAME)
        part = f"{cover}.part"
        try:
            if await aiofiles.os.path.exists(cover):
                return
            if (data := await self._fetch_cover(track)) is None:
                return
            async with aiofiles.open(part, "wb") as file:
                await file.write(data)
            await replace_file(part, cover)
        except (MusicAssistantError, ClientError, TimeoutError, OSError) as err:
            self.logger.debug("Could not keep the cover of %s: %s", track.name, err)
            await self._remove(part)
            return
        self.logger.debug("Stored the cover of %s as %s", track.name, cover)
        if self.library is not None and item_id is not None:
            await self.library.refresh_cover(item_id)

    async def _fetch_cover(self, track: Track) -> bytes | None:
        """Return the cover of a track as a JPEG, None when it has none."""
        metadata = self.provider.mass.metadata
        if not (url := await metadata.get_image_url_for_item(track, ImageType.THUMB)):
            return None
        data = await metadata.get_thumbnail(
            url, provider="builtin", size=0, image_format="JPEG", flatten_transparency=True
        )
        return data if isinstance(data, bytes) and data.startswith(_JPEG_START) else None


def relative_file_path(track: Track | None) -> str:
    """
    Return the path of the file of a track in the folder tree, below the cache folder.

    The tree is the artist, the album and then the track, like ``Artist/Album/01 Title.m4a``.

    :param track: The track, or None when it is not known.
    """
    artist = album = title = None
    track_number = disc_number = None
    if track is not None:
        album_artists = getattr(track.album, "artists", None)
        artists = album_artists or track.artists
        artist = artists[0].name if artists else None
        album = track.album.name if track.album else None
        title = track.name
        track_number = track.track_number
        disc_number = track.disc_number
    number = f"{track_number:02d} " if track_number else ""
    if number and disc_number and disc_number > 1:
        number = f"{disc_number}-{number}"
    file_name = f"{number}{_safe_name(title or '', 'Unknown Title')}"
    return os.path.join(
        _safe_name(artist or "", "Unknown Artist"),
        _safe_name(album or "", "Unknown Album"),
        f"{file_name}{AUDIO_CACHE_FILE_SUFFIX}",
    )


def _safe_name(value: str, fallback: str) -> str:
    """Return a name that is fine as the name of a file or folder on any file system."""
    cleaned = _UNSAFE_CHARS.sub("_", unicodedata.normalize("NFC", value)).strip(" .")
    cleaned = cleaned.encode()[:MAX_NAME_BYTES].decode(errors="ignore").rstrip(" .")
    return cleaned or fallback


def _key_fingerprint(key: str | None) -> str:
    """Return a short non-secret fingerprint of a hex key for logs."""
    if not key:
        return "<none>"
    if len(key) < 8:
        return f"len={len(key)}"
    return f"len={len(key)} head={key[:4]} tail={key[-4:]}"

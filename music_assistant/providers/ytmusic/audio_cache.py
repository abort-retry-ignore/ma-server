"""Keep complete YouTube Music streams for local personal playback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import stat
import tempfile
import time
import unicodedata
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.errors import AudioError, MusicAssistantError
from music_assistant_models.media_items import Track
from mutagen.flac import FLAC
from mutagen.id3 import (
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
    UFID,
    ID3NoHeaderError,
)
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from music_assistant.helpers.cache_library import CacheLibrary, write_mp4_tags
from music_assistant.helpers.cache_metadata_export import _can_create, _write_owned
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import async_json_dumps, async_json_loads

if TYPE_CHECKING:
    from aiofiles.threadpool.binary import AsyncBufferedIOBase

    from . import YoutubeMusicProvider

ENV_AUDIO_CACHE_PATH = "MASS_YTMUSIC_AUDIO_CACHE"
ENV_AUDIO_LIBRARY_PATH = "MASS_YTMUSIC_LIBRARY_PATH"
_INDEX_DIR = ".ma-audio-cache"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_DOWNLOAD_TIMEOUT = ClientTimeout(total=None, sock_connect=15, sock_read=60)
_STALE_PART_SECONDS = 3600
# byte range fetched per request; YouTube throttles long responses, not short ones
_RANGE_SIZE = 1024 * 1024
_COVER_RETRY_SECONDS = 3600
_ACTIVE_DOWNLOADS: set[tuple[str, str]] = set()


@dataclass(frozen=True, repr=False)
class YoutubeDownload:
    """Private stream information for one YouTube Music track."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    source_format: str = ""
    extension: str = ""
    expected_size: int | None = None


@dataclass(frozen=True)
class YoutubeCacheHit:
    """Identify a local cached track for playback fallback."""

    item_id: str


def selected_extension(stream_format: Mapping[str, Any]) -> str | None:
    """Return a supported output extension for a stream without re-encoding."""
    protocol = str(stream_format.get("protocol") or "https").lower()
    if protocol not in {"http", "https"}:
        return None
    source = str(stream_format.get("audio_ext") or "").lower()
    codec = str(stream_format.get("acodec") or "").lower()
    if source in {"m4a", "mp4", "aac"}:
        return "m4a"
    if source == "mp3" and (not codec or codec.startswith("mp3")):
        return "mp3"
    if source == "flac" and (not codec or codec.startswith("flac")):
        return "flac"
    if source in {"webm", "ogg", "opus"} and (
        codec.startswith("opus") or (source == "opus" and not codec)
    ):
        return "opus"
    if source in {"webm", "ogg"} and codec.startswith("vorbis"):
        return "ogg"
    return None


def provider_cache_root(root: str, instance_id: str) -> str | None:
    """Return a safe per-instance cache root, if the configuration is valid."""
    if not Path(root).is_absolute() or instance_id in {".", ".."}:
        return None
    if not _SAFE_ID.fullmatch(instance_id):
        return None
    return os.path.join(root, instance_id)


class YoutubeAudioCache:
    """Cache complete YouTube Music audio without re-encoding it."""

    def __init__(self, provider: YoutubeMusicProvider, root: str, library_root: str | None) -> None:
        """
        Initialize the cache.

        :param provider: Provider instance that owns the cached tracks.
        :param root: Absolute path for the provider's cache and index.
        :param library_root: Optional local music library destination.
        """
        self.provider = provider
        self.logger = provider.logger
        self.root = root
        self._index_dir = os.path.join(root, _INDEX_DIR)
        self.library = (
            CacheLibrary(self, library_root, tag_writer=_write_youtube_tags)
            if library_root
            else None
        )
        self._cover_attempts: dict[str, float] = {}

    async def setup(self) -> bool:
        """Prepare the cache directory and report whether it is usable."""
        if not Path(self.root).is_absolute():
            self.logger.warning("The YouTube Music cache folder must be an absolute path")
            return False
        if self.library and not Path(self.library.root).is_absolute():
            self.logger.warning("The YouTube Music library folder must be an absolute path")
            return False
        if self.library:
            cache_root, library_root = Path(self.root).resolve(), Path(self.library.root).resolve()
            if cache_root.is_relative_to(library_root) or library_root.is_relative_to(cache_root):
                self.logger.warning("The YouTube Music cache and library folders must be separate")
                return False
        try:
            await asyncio.to_thread(_prepare_directory, self._index_dir)
        except OSError as err:
            self.logger.warning("The YouTube Music cache folder is unavailable: %s", err)
            return False
        return True

    async def lookup(self, item_id: str) -> tuple[str, str] | None:
        """Return a complete cached file and its extension, if still available."""
        resolved = await self._resolve_entry(item_id)
        if not resolved:
            return None
        entry, path, size = resolved
        extension = str(entry.get("extension") or "")
        if (
            not entry.get("clear")
            or size != entry.get("size")
            or extension
            not in {
                "m4a",
                "opus",
                "ogg",
                "mp3",
                "flac",
            }
        ):
            return None
        return path, extension

    async def invalidate(self, item_id: str, expected_path: str) -> bool:
        """Forget a failed cache entry without deleting the user's audio file."""
        if not _SAFE_ID.fullmatch(item_id) or not (entry := await self._read_entry(item_id)):
            return False
        possible_paths: list[str | None] = []
        if entry.get("location") == "library" and self.library:
            possible_paths.append(_within_root(self.library.root, str(entry.get("path") or "")))
            possible_paths.append(_within_root(self.root, str(entry.get("cache_path") or "")))
        elif entry.get("location") is None:
            possible_paths.append(_within_root(self.root, str(entry.get("path") or "")))
        if os.path.realpath(expected_path) not in possible_paths:
            return False
        await self._remove(self._entry_path(item_id))
        return True

    async def stream_and_store(  # noqa: PLR0915
        self, item_id: str, download: YoutubeDownload
    ) -> AsyncGenerator[bytes]:
        """
        Stream a track and retain it only when the full source arrives.

        :param item_id: YouTube Music track ID.
        :param download: Private source information for the stream.
        """
        claimed = (
            bool(_SAFE_ID.fullmatch(item_id)) and (self.root, item_id) not in _ACTIVE_DOWNLOADS
        )
        part = os.path.join(self._index_dir, f"{item_id}.part")
        sink: AsyncBufferedIOBase | None = None
        if claimed:
            _ACTIVE_DOWNLOADS.add((self.root, item_id))
            sink = await self._open_part(part)
        received = 0
        expected: int | None = None
        whole_response = False
        reached_end = False
        try:
            headers = dict(download.headers)
            headers.pop("Range", None)
            headers.pop("range", None)
            headers["Accept-Encoding"] = "identity"
            if download.expected_size:
                # YouTube sends one long response at about twice playback speed, but serves
                # short byte ranges at full speed: fetch the file range by range
                expected = download.expected_size
                async for chunk in self._ranged_download(download.url, headers, expected):
                    if sink is not None:
                        sink = await self._write_part(sink, chunk, part)
                    received += len(chunk)
                    yield chunk
                whole_response = received == expected
                reached_end = True
            else:
                async with self.provider.mass.http_session.get(
                    download.url, headers=headers, timeout=_DOWNLOAD_TIMEOUT
                ) as response:
                    if response.status not in (200, 206):
                        raise AudioError(
                            f"YouTube Music audio request failed (HTTP {response.status})"
                        )
                    expected = response.content_length
                    whole_response = _is_whole_response(
                        response.status, response.headers.get("Content-Range"), expected
                    )
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if sink is not None:
                            sink = await self._write_part(sink, chunk, part)
                        received += len(chunk)
                        yield chunk
                    reached_end = True
        except (ClientError, TimeoutError) as err:
            raise AudioError(
                f"YouTube Music audio transport failed: {type(err).__name__}"
            ) from None
        finally:
            try:
                if sink is not None:
                    with contextlib.suppress(OSError):
                        await sink.close()
                    complete = (
                        reached_end
                        and whole_response
                        and received > 0
                        and (expected is None or received == expected)
                        and (download.expected_size is None or received == download.expected_size)
                    )
                    if complete:
                        await self._finish(item_id, part, received, download.extension)
                    else:
                        await self._remove(part)
            finally:
                if claimed:
                    _ACTIVE_DOWNLOADS.discard((self.root, item_id))

    async def _ranged_download(
        self, url: str, headers: dict[str, str], size: int
    ) -> AsyncGenerator[bytes]:
        """
        Yield a file of known size, fetched in consecutive byte ranges.

        :param url: The audio URL.
        :param headers: The request headers (without a Range header).
        :param size: The file size in bytes.
        """
        start = 0
        while start < size:
            end = min(start + _RANGE_SIZE, size) - 1
            ranged = {**headers, "Range": f"bytes={start}-{end}"}
            async with self.provider.mass.http_session.get(
                url, headers=ranged, timeout=_DOWNLOAD_TIMEOUT
            ) as response:
                whole = start == 0 and response.status == 200
                if response.status != 206 and not whole:
                    raise AudioError(
                        f"YouTube Music audio range request failed (HTTP {response.status})"
                    )
                got = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    got += len(chunk)
                    yield chunk
            if whole:
                # the server ignored the range and sent the whole file: the caller checks
                # that it is the expected size
                return
            if got != end - start + 1:
                raise AudioError("YouTube Music audio range came back short")
            start = end + 1

    async def refresh_metadata(self, item_id: str) -> None:
        """Publish known metadata and an album cover for a cached track."""
        if self.library:
            await self.library.publish(item_id)
        resolved = await self._resolve_entry(item_id)
        if not resolved:
            return
        track = await self.get_cached_track(item_id)
        if track is None:
            return
        await self._store_cover(item_id, track, resolved[1])

    async def get_cached_track(self, item_id: str) -> Track | None:
        """Return known track metadata without contacting YouTube Music."""
        mass = self.provider.mass
        try:
            track = await mass.music.tracks.get_library_item_by_prov_id(
                item_id, self.provider.instance_id
            )
            if isinstance(track, Track):
                return track
            track = await mass.cache.get(
                key=f"get_track.{item_id}",
                provider=self.provider.instance_id,
                base_class=Track,
                allow_expired_cache=True,
            )
            return track if isinstance(track, Track) else None
        except (MusicAssistantError, OSError, ValueError) as err:
            self.logger.debug(
                "Could not read cached YouTube Music metadata for %s: %s", item_id, err
            )
            return None

    def library_relative_path(self, track: Track, item_id: str, entry: dict[str, Any]) -> str:
        """Return the library filename based on the latest known track metadata."""
        return _relative_path(track, item_id, str(entry.get("extension") or ""))

    async def _resolve_entry(self, item_id: str) -> tuple[dict[str, Any], str, int] | None:
        if not _SAFE_ID.fullmatch(item_id) or not (entry := await self._read_entry(item_id)):
            return None
        if entry.get("location") == "library":
            if self.library:
                path = _within_root(self.library.root, str(entry.get("path") or ""))
                if path and (size := await _file_size(path)) is not None:
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
        path = _within_root(self.root, str(entry.get("path") or ""))
        if not path or (size := await _file_size(path)) is None:
            return None
        return entry, path, size

    async def _finish(self, item_id: str, part: str, size: int, extension: str) -> None:
        """Remux and publish complete data without changing the audio codec."""
        remuxed = f"{part}.remux.{extension}"
        try:
            if extension not in {"m4a", "opus", "ogg", "mp3", "flac"}:
                return
            if (await _file_size(part)) != size or not (duration := await _remux(part, remuxed)):
                self.logger.warning("Could not remux complete YouTube Music track %s", item_id)
                return
            track = await self.get_cached_track(item_id)
            if (
                track
                and track.duration
                and abs(duration - track.duration) > max(5, track.duration * 0.02)
            ):
                self.logger.warning(
                    "Cached YouTube Music track %s has an unexpected duration", item_id
                )
                return
            relative = _relative_path(track, item_id, extension)
            target, installed_stat = await asyncio.to_thread(
                _install, self.root, remuxed, relative, item_id
            )
            stored_size = installed_stat.st_size
            entry = {
                "item_id": item_id,
                "path": os.path.relpath(target, self.root),
                "size": stored_size,
                "clear": True,
                "extension": extension,
                "artist": track.artists[0].name if track and track.artists else None,
                "album": track.album.name if track and track.album else None,
                "title": track.name if track else None,
                "cached_at": utc().isoformat(timespec="seconds"),
            }
            try:
                await self._write_entry(item_id, entry)
            except OSError:
                await asyncio.to_thread(_remove_if_unchanged, target, installed_stat)
                raise
            self.logger.info("Cached YouTube Music audio of %s as %s", item_id, entry["path"])
            if self.library:
                await self.library.publish(item_id, track)
                if resolved := await self._resolve_entry(item_id):
                    target = resolved[1]
            if track:
                self.provider.mass.create_task(
                    self._store_cover,
                    item_id,
                    track,
                    target,
                    task_id=f"{self.provider.instance_id}.cache_cover.{item_id}",
                )
        except Exception as err:
            self.logger.warning("Could not cache YouTube Music track %s: %s", item_id, err)
        finally:
            await self._remove(part)
            await self._remove(remuxed)

    async def _store_cover(self, item_id: str, track: Track, audio_path: str) -> None:
        """Save a missing cover using MA's existing cached image pipeline."""
        root = (
            self.library.root
            if self.library and Path(audio_path).is_relative_to(self.library.root)
            else self.root
        )
        folder = Path(audio_path).parent
        cover = folder / "cover.jpg"
        now = time.monotonic()
        if now - self._cover_attempts.get(str(folder), -math.inf) < _COVER_RETRY_SECONDS:
            return
        if not await asyncio.to_thread(_can_create, Path(root), Path(self._index_dir), cover):
            return
        self._cover_attempts[str(folder)] = now
        try:
            metadata = self.provider.mass.metadata
            if not (url := await metadata.get_image_url_for_item(track, ImageType.THUMB)):
                return
            data = await metadata.get_thumbnail(
                url, provider="builtin", size=0, image_format="JPEG", flatten_transparency=True
            )
            if not isinstance(data, bytes) or not data.startswith(b"\xff\xd8\xff"):
                return
            changed = await asyncio.to_thread(
                _write_owned, Path(root), Path(self._index_dir), cover, data
            )
            if changed and self.library:
                await self.library.refresh_cover(item_id)
        except (OSError, MusicAssistantError, ClientError, TimeoutError) as err:
            self.logger.debug("Could not store YouTube Music cover for %s: %s", item_id, err)

    async def _open_part(self, part: str) -> AsyncBufferedIOBase | None:
        try:
            await asyncio.to_thread(os.makedirs, self._index_dir, exist_ok=True)
            return await aiofiles.open(part, "xb")
        except OSError as err:
            self.logger.warning("Could not open YouTube Music cache: %s", err)
            return None

    async def _write_part(
        self, sink: AsyncBufferedIOBase, chunk: bytes, part: str
    ) -> AsyncBufferedIOBase | None:
        try:
            await sink.write(chunk)
        except OSError as err:
            self.logger.warning("Could not write YouTube Music cache: %s", err)
            with contextlib.suppress(OSError):
                await sink.close()
            await self._remove(part)
            return None
        return sink

    async def _read_entry(self, item_id: str) -> dict[str, Any] | None:
        try:
            async with aiofiles.open(self._entry_path(item_id), encoding="utf-8") as file:
                entry = await async_json_loads(await file.read())
        except OSError, ValueError:
            return None
        return entry if isinstance(entry, dict) else None

    async def _write_entry(self, item_id: str, entry: dict[str, Any]) -> None:
        path = self._entry_path(item_id)
        async with aiofiles.open(f"{path}.tmp", "w", encoding="utf-8") as file:
            await file.write(await async_json_dumps(entry, indent=True))
        await asyncio.to_thread(os.replace, f"{path}.tmp", path)

    def _entry_path(self, item_id: str) -> str:
        return os.path.join(self._index_dir, f"{item_id}.json")

    async def _remove(self, path: str) -> None:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.remove, path)


def _prepare_directory(index: str) -> None:
    """Create and test the cache folder and remove abandoned downloads."""
    Path(index).mkdir(parents=True, exist_ok=True)
    fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=index)
    os.close(fd)
    Path(probe).unlink()
    for path in Path(index).iterdir():
        if (path.name.endswith(".part") or ".part.remux." in path.name) and (
            time.time() - path.stat().st_mtime > _STALE_PART_SECONDS
        ):
            with contextlib.suppress(OSError):
                path.unlink()


def _within_root(root: str, relative: str) -> str | None:
    """Resolve an indexed path only if it is inside its designated root."""
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    return str(candidate) if candidate != base and candidate.is_relative_to(base) else None


async def _file_size(path: str) -> int | None:
    try:
        info = await asyncio.to_thread(os.stat, path)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _is_whole_response(status: int, content_range: str | None, size: int | None) -> bool:
    """Reject partial range responses even when their own content length matches."""
    if status == 200:
        return content_range is None
    if status != 206 or not content_range or not (match := _CONTENT_RANGE.fullmatch(content_range)):
        return False
    start, end, total = map(int, match.groups())
    return start == 0 and end + 1 == total and (size is None or size == total)


async def _remux(source: str, target: str) -> float | None:
    """Create a locally playable container and return its duration."""
    try:
        status, _output = await _run_local_process(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-y",
            target,
        )
        if status != 0:
            return None
        status, output = await _run_local_process(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            target,
        )
        if status != 0:
            return None
        result = json.loads(output)
        duration = float(result.get("format", {}).get("duration") or 0)
        if (
            math.isfinite(duration)
            and duration > 0
            and any(stream.get("codec_type") == "audio" for stream in result.get("streams", []))
        ):
            return duration
    except OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError:
        return None
    return None


async def _run_local_process(*args: str) -> tuple[int | None, bytes]:
    """Run a bounded local media command and reap it if interrupted."""
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(90):
            output, _err = await process.communicate()
    except asyncio.CancelledError, TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    return process.returncode, output


def _relative_path(track: Track | None, item_id: str, extension: str) -> str:
    """Build a safe artist/album/track filename from known metadata."""
    artists = getattr(track.album, "artists", None) if track and track.album else None
    artists = artists or (track.artists if track else None)
    artist = artists[0].name if artists else "Unknown Artist"
    album = track.album.name if track and track.album else "Unknown Album"
    title = track.name if track else item_id
    number = f"{track.track_number:02d} " if track and track.track_number else ""
    if number and track and track.disc_number and track.disc_number > 1:
        number = f"{track.disc_number}-{number}"
    return os.path.join(
        _safe_name(artist, "Unknown Artist"),
        _safe_name(album, "Unknown Album"),
        f"{number}{_safe_name(title, item_id)}.{extension}",
    )


def _safe_name(value: str, fallback: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", unicodedata.normalize("NFC", value)).strip(" .")
    return cleaned.encode()[:100].decode(errors="ignore").rstrip(" .") or fallback


def _install(root: str, source: str, relative: str, item_id: str) -> tuple[str, os.stat_result]:
    """Install the remuxed cache file without replacing an existing file."""
    base = Path(root).resolve()
    target = (base / relative).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError("YouTube Music cache destination is outside its root")
    target.parent.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        suffix = "" if index == 0 else f" [{item_id}]" if index == 1 else f" [{item_id}-{index}]"
        candidate = target.with_name(f"{target.stem}{suffix}{target.suffix}")
        try:
            os.link(source, candidate)
        except FileExistsError:
            continue
        return str(candidate), candidate.stat()
    raise FileExistsError("No free YouTube Music cache destination")


def _remove_if_unchanged(path: str, expected: os.stat_result) -> None:
    """Remove only a file installed by this operation that remains unchanged."""
    with contextlib.suppress(OSError):
        current = Path(path).stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) == (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        ):
            Path(path).unlink()


def _write_youtube_tags(
    path: str, item_id: str, entry: dict[str, Any], track: Track | None
) -> None:
    """Write portable tags and the YouTube Music identity without re-encoding."""
    extension = str(entry.get("extension") or "")
    if extension == "m4a":
        write_mp4_tags(path, item_id, entry, track, "MA YouTube Music ID")
        return
    if extension == "mp3":
        _write_mp3_tags(path, item_id, entry, track)
        return
    if extension == "opus":
        audio: Any = OggOpus(path)  # type: ignore[no-untyped-call]
    elif extension == "ogg":
        audio = OggVorbis(path)  # type: ignore[no-untyped-call]
    elif extension == "flac":
        audio = FLAC(path)  # type: ignore[no-untyped-call]
    else:
        raise ValueError(f"Unsupported cached audio format: {extension}")
    if audio.tags is None:
        audio.add_tags()
    audio.tags.clear()
    title = track.name if track else entry.get("title")
    if track and track.version:
        title = f"{track.name} ({track.version})"
    artist_names = [artist.name for artist in track.artists] if track else []
    album = track.album.name if track and track.album else entry.get("album")
    if title:
        audio["TITLE"] = [str(title)]
    if artist_names:
        audio["ARTIST"] = artist_names
        audio["ARTISTS"] = artist_names
        audio["ALBUMARTIST"] = [artist_names[0]]
    elif entry.get("artist"):
        audio["ARTIST"] = [str(entry["artist"])]
    if album:
        audio["ALBUM"] = [str(album)]
    if track:
        if track.track_number:
            audio["TRACKNUMBER"] = [str(track.track_number)]
        if track.disc_number:
            audio["DISCNUMBER"] = [str(track.disc_number)]
        for external_id, tag in (
            (ExternalID.ISRC, "ISRC"),
            (ExternalID.MB_RECORDING, "MUSICBRAINZ_TRACKID"),
            (ExternalID.MB_TRACK, "MUSICBRAINZ_RELEASETRACKID"),
        ):
            if value := track.get_external_id(external_id):
                audio[tag] = [value]
        if track.metadata.genres:
            audio["GENRE"] = sorted(track.metadata.genres)
        if year := getattr(track.album, "year", None):
            audio["DATE"] = [str(year)]
    audio["MA_YOUTUBE_MUSIC_ID"] = [item_id]
    audio.save()


def _write_mp3_tags(path: str, item_id: str, entry: dict[str, Any], track: Track | None) -> None:
    """Write track metadata to an MP3 without re-encoding its audio."""
    try:
        tags = ID3(path)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        tags = ID3()  # type: ignore[no-untyped-call]
    tags.clear()  # type: ignore[no-untyped-call]
    title = track.name if track else entry.get("title")
    if track and track.version:
        title = f"{track.name} ({track.version})"
    artists = [artist.name for artist in track.artists] if track else []
    if not artists and entry.get("artist"):
        artists = [str(entry["artist"])]
    album = track.album.name if track and track.album else entry.get("album")
    if title:
        tags.add(TIT2(encoding=3, text=[str(title)]))  # type: ignore[no-untyped-call]
    if artists:
        tags.add(TPE1(encoding=3, text=artists))  # type: ignore[no-untyped-call]
        tags.add(TPE2(encoding=3, text=[artists[0]]))  # type: ignore[no-untyped-call]
    if album:
        tags.add(TALB(encoding=3, text=[str(album)]))  # type: ignore[no-untyped-call]
    if track:
        if track.track_number:
            tags.add(TRCK(encoding=3, text=[str(track.track_number)]))  # type: ignore[no-untyped-call]
        if track.disc_number:
            tags.add(TPOS(encoding=3, text=[str(track.disc_number)]))  # type: ignore[no-untyped-call]
        if track.metadata.genres:
            tags.add(TCON(encoding=3, text=sorted(track.metadata.genres)))  # type: ignore[no-untyped-call]
        if year := getattr(track.album, "year", None):
            tags.add(TDRC(encoding=3, text=[str(year)]))  # type: ignore[no-untyped-call]
        if recording_id := track.get_external_id(ExternalID.MB_RECORDING):
            tags.add(UFID(owner="http://musicbrainz.org", data=recording_id.encode()))  # type: ignore[no-untyped-call]
        for external_id, tag in (
            (ExternalID.ISRC, "TSRC"),
            (ExternalID.MB_TRACK, "MusicBrainz Track Id"),
        ):
            if value := track.get_external_id(external_id):
                tags.add(TXXX(encoding=3, desc=tag, text=[value]))  # type: ignore[no-untyped-call]
    tags.add(TXXX(encoding=3, desc="MA YouTube Music ID", text=[item_id]))  # type: ignore[no-untyped-call]
    tags.save(path)

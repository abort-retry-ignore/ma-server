"""Publish completed audio cache entries to a local music library."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import Album, ProviderMapping
from mutagen.mp4 import MP4, MP4FreeForm

from music_assistant.helpers.cache_metadata_export import CacheMetadataExporter
from music_assistant.providers.filesystem_local.constants import (
    CACHE_CATEGORY_ALBUM_INFO,
    CACHE_CATEGORY_ARTIST_INFO,
    CACHE_CATEGORY_FOLDER_IMAGES,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.media_items import Track

    from music_assistant.providers.filesystem_local import LocalFileSystemProvider

CACHE_COPY_GRACE_SECONDS = 900


class CacheLibrary:
    """Publish tagged copies of complete cache entries to a managed library directory."""

    def __init__(
        self,
        cache: Any,
        root: str,
        *,
        tag_writer: Callable[[str, str, dict[str, Any], Track | None], None],
        cache_checksum: str | None = None,
    ) -> None:
        """Initialize the library publisher."""
        self.cache = cache
        self.root = root
        self.tag_writer = tag_writer
        self._locks: dict[str, asyncio.Lock] = {}
        self.metadata_exporter = CacheMetadataExporter(cache, root, cache_checksum=cache_checksum)

    async def publish_all(self) -> None:
        """Publish existing cache entries without fetching metadata from the service."""
        try:
            entries = await asyncio.to_thread(os.listdir, self.cache._index_dir)
        except OSError as err:
            self.cache.logger.warning("Unable to list cached tracks: %s", err)
            return
        for name in sorted(entries):
            if name.endswith(".json"):
                await self.publish(name.removesuffix(".json"))

    async def publish(self, item_id: str, track: Track | None = None) -> None:
        """Publish a clear cached track and add its local mapping to the library."""
        async with self._locks.setdefault(item_id, asyncio.Lock()):
            try:
                await self._publish(item_id, track)
            except Exception as err:
                self.cache.logger.warning("Could not publish cached audio of %s: %s", item_id, err)
        for delay in (5, 120):
            self.cache.provider.mass.call_later(
                delay,
                self.export_metadata,
                item_id,
                task_id=f"{self.cache.provider.instance_id}.export_metadata.{item_id}.{delay}",
            )

    async def export_metadata(self, item_id: str) -> None:
        """Export known metadata and refresh local associations when files change."""
        if not await self.metadata_exporter.export(item_id):
            return
        async with self._locks.setdefault(item_id, asyncio.Lock()):
            try:
                if resolved := await self.cache._resolve_entry(item_id):
                    if resolved[0].get("location") == "library":
                        await self._index(item_id, resolved[1], refresh=True)
            except Exception as err:
                self.cache.logger.warning("Could not index metadata for %s: %s", item_id, err)

    async def refresh_cover(self, item_id: str) -> None:
        """Refresh an indexed album after its local cover has arrived."""
        async with self._locks.setdefault(item_id, asyncio.Lock()):
            try:
                if resolved := await self.cache._resolve_entry(item_id):
                    if resolved[0].get("location") == "library":
                        await self._index(item_id, resolved[1], refresh=True)
            except Exception as err:
                self.cache.logger.warning("Could not index cover for %s: %s", item_id, err)

    async def _publish(self, item_id: str, track: Track | None) -> None:
        if not Path(self.root).is_absolute():
            raise ValueError("The local library destination must be an absolute path")
        resolved = await self.cache._resolve_entry(item_id)
        if resolved is None:
            return
        entry, source, size = resolved
        if not entry.get("clear") or entry.get("size") != size:
            return
        if entry.get("location") == "library":
            if "library_inode" not in entry and isinstance(entry.get("published_at"), (int, float)):
                published = await asyncio.to_thread(os.stat, source)
                if published.st_mtime_ns <= int(entry["published_at"] * 1_000_000_000):
                    entry.update(
                        library_device=published.st_dev,
                        library_inode=published.st_ino,
                        library_mtime_ns=published.st_mtime_ns,
                    )
                    await self.cache._write_entry(item_id, entry)
            self._schedule_cleanup(item_id, entry)
            await self._index(item_id, source)
            return
        if track is None:
            cached_track = getattr(self.cache, "get_cached_track", None)
            if callable(cached_track):
                track = await cached_track(item_id)
            else:
                track = await self.cache.provider.mass.music.tracks.get_library_item_by_prov_id(
                    item_id, self.cache.provider.instance_id
                )
        if not all(entry.get(key) for key in ("artist", "album", "title")) and track is None:
            self.cache.logger.warning("No cached metadata for %s; leaving it in the cache", item_id)
            return
        relative = str(entry.get("path", ""))
        library_path = getattr(self.cache, "library_relative_path", None)
        if track is not None and callable(library_path):
            relative = library_path(track, item_id, entry)
        source_stat = await asyncio.to_thread(os.stat, source)
        destination, new_size = await asyncio.to_thread(
            _copy_and_tag, self.root, relative, source, item_id, entry, track, self.tag_writer
        )
        current_stat = await asyncio.to_thread(os.stat, source)
        if (source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns) != (
            current_stat.st_ino,
            current_stat.st_size,
            current_stat.st_mtime_ns,
        ):
            await asyncio.to_thread(os.remove, os.path.join(self.root, destination))
            raise ValueError("The cached file changed while being published")
        published_stat = await asyncio.to_thread(os.stat, os.path.join(self.root, destination))
        updated = {
            **entry,
            "location": "library",
            "path": destination,
            "size": new_size,
            "cache_path": entry["path"],
            "cache_size": entry["size"],
            "cache_device": source_stat.st_dev,
            "cache_inode": source_stat.st_ino,
            "cache_mtime_ns": source_stat.st_mtime_ns,
            "library_device": published_stat.st_dev,
            "library_inode": published_stat.st_ino,
            "library_mtime_ns": published_stat.st_mtime_ns,
            "published_at": time.time(),
        }
        try:
            await self.cache._write_entry(item_id, updated)
        except OSError:
            await asyncio.to_thread(os.remove, os.path.join(self.root, destination))
            raise
        self._schedule_cleanup(item_id, updated)
        await self._index(item_id, os.path.join(self.root, destination))
        self.cache.logger.info("Published cached audio of %s as %s", item_id, destination)

    def _schedule_cleanup(self, item_id: str, entry: dict[str, Any]) -> None:
        """Schedule removal of the old copy after outstanding stream details expire."""
        published_at = entry.get("published_at")
        if not isinstance(published_at, (int, float)) or not entry.get("cache_path"):
            return
        delay = max(0, published_at + CACHE_COPY_GRACE_SECONDS - time.time())
        self.cache.provider.mass.call_later(
            delay,
            self._discard_cache_copy,
            item_id,
            task_id=f"{self.cache.provider.instance_id}.cache_cleanup.{item_id}",
        )

    async def _discard_cache_copy(self, item_id: str) -> None:
        """Remove an unchanged original only while its published replacement is intact."""
        async with self._locks.setdefault(item_id, asyncio.Lock()):
            try:
                entry = await self.cache._read_entry(item_id)
                if not entry or entry.get("location") != "library":
                    return
                if time.time() < float(entry.get("published_at", 0)) + CACHE_COPY_GRACE_SECONDS:
                    return
                await asyncio.to_thread(_remove_original, self.cache.root, self.root, entry)
            except FileNotFoundError:
                return
            except (OSError, ValueError, TypeError) as err:
                self.cache.logger.warning(
                    "Could not remove old cached audio of %s: %s", item_id, err
                )

    async def _index(self, item_id: str, path: str, *, refresh: bool = False) -> None:
        """Attach the exact source mapping to the locally parsed track when possible."""
        mass = self.cache.provider.mass
        for provider in mass.music.providers:
            if provider.domain != "filesystem_local" or not provider.available:
                continue
            base = os.path.realpath(cast("LocalFileSystemProvider", provider).base_path)
            real_path = os.path.realpath(path)
            if not real_path.startswith(base + os.sep):
                continue
            relative = os.path.relpath(real_path, base)
            existing = await mass.music.tracks.get_library_item_by_prov_id(
                relative, provider.instance_id
            )
            if (
                existing
                and not refresh
                and any(
                    mapping.item_id == item_id
                    and mapping.provider_instance == self.cache.provider.instance_id
                    for mapping in existing.provider_mappings
                )
            ):
                return
            if refresh:
                local_provider = cast("LocalFileSystemProvider", provider)
                album_dir = os.path.dirname(relative)
                artist_dir = os.path.dirname(album_dir)
                for category, folder in (
                    (CACHE_CATEGORY_ALBUM_INFO, album_dir),
                    (CACHE_CATEGORY_FOLDER_IMAGES, album_dir),
                    (CACHE_CATEGORY_ARTIST_INFO, artist_dir),
                    (CACHE_CATEGORY_FOLDER_IMAGES, artist_dir),
                ):
                    await local_provider.cache.delete(
                        key=folder,
                        provider=provider.instance_id,
                        category=category,
                    )
            local_track = await provider.get_track(relative)
            if refresh and isinstance(local_track.album, Album):
                await mass.music.albums.add_item_to_library(local_track.album)
            local_track.provider_mappings.add(
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.cache.provider.domain,
                    provider_instance=self.cache.provider.instance_id,
                )
            )
            await mass.music.tracks.add_item_to_library(local_track)
            return


def _remove_original(cache_root: str, library_root: str, entry: dict[str, Any]) -> None:
    """Remove a cache-owned original only when neither file has changed."""
    cache_base = Path(cache_root).resolve()
    library_base = Path(library_root).resolve()
    source = (cache_base / str(entry.get("cache_path", ""))).resolve(strict=True)
    destination = (library_base / str(entry.get("path", ""))).resolve(strict=True)
    if not source.is_relative_to(cache_base) or not destination.is_relative_to(library_base):
        return
    if source == destination or not source.is_file() or not destination.is_file():
        return
    stat = source.stat()
    published = destination.stat()
    if (
        stat.st_dev != entry.get("cache_device")
        or stat.st_ino != entry.get("cache_inode")
        or stat.st_size != entry.get("cache_size")
        or stat.st_mtime_ns != entry.get("cache_mtime_ns")
        or published.st_dev != entry.get("library_device")
        or published.st_ino != entry.get("library_inode")
        or published.st_mtime_ns != entry.get("library_mtime_ns")
        or published.st_size != entry.get("size")
    ):
        return
    source.unlink()


def _copy_and_tag(
    root: str,
    relative: str,
    source: str,
    item_id: str,
    entry: dict[str, Any],
    track: Track | None,
    tag_writer: Callable[[str, str, dict[str, Any], Track | None], None],
) -> tuple[str, int]:
    """Create a tagged file without replacing an existing library file."""
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    target = (root_path / relative).resolve()
    if not target.is_relative_to(root_path) or target == root_path:
        raise ValueError("Cache entry points outside the local library")
    owner = root_path.stat()
    folder = root_path
    for part in target.parent.relative_to(root_path).parts:
        folder /= part
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        if os.geteuid() == 0:
            os.chown(folder, owner.st_uid, owner.st_gid)
    fd, temp_path = tempfile.mkstemp(prefix=".ma-publish-", suffix=".part", dir=target.parent)
    os.fchmod(fd, 0o644)
    if os.geteuid() == 0:
        os.fchown(fd, owner.st_uid, owner.st_gid)
    os.close(fd)
    try:
        shutil.copyfile(source, temp_path)
        tag_writer(temp_path, item_id, entry, track)
        with open(temp_path, "rb") as file:
            os.fsync(file.fileno())
        for number in range(1000):
            if number == 0:
                candidate = target
            else:
                suffix = f" [{item_id}]" if number == 1 else f" [{item_id}-{number}]"
                candidate = target.with_name(f"{target.stem}{suffix}{target.suffix}")
            try:
                os.link(temp_path, candidate)
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError(f"No free destination for {target}")
        cover = Path(source).parent / "cover.jpg"
        if cover.is_file():
            with (
                contextlib.suppress(OSError),
                cover.open("rb") as src,
                (candidate.parent / "cover.jpg").open("xb") as dst,
            ):
                shutil.copyfileobj(src, dst)
                if os.geteuid() == 0:
                    os.fchown(dst.fileno(), owner.st_uid, owner.st_gid)
        return str(candidate.relative_to(root_path)), candidate.stat().st_size
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(temp_path).unlink()


def write_mp4_tags(
    path: str,
    item_id: str,
    entry: dict[str, Any],
    track: Track | None,
    source_tag: str,
) -> None:
    """Write standard MP4 tags using already available metadata."""
    audio: Any = MP4(path)  # type: ignore[no-untyped-call]
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    cover = audio.tags.get("covr")
    audio.tags.clear()
    if cover:
        audio.tags["covr"] = cover
    title = track.name if track else entry.get("title")
    if track and track.version:
        title = f"{track.name} ({track.version})"
    artist = (
        "; ".join(artist.name for artist in track.artists)
        if track and track.artists
        else entry.get("artist")
    )
    album = track.album.name if track and track.album else entry.get("album")
    album_artists = getattr(track.album, "artists", None) if track and track.album else None
    album_artist = (
        "; ".join(artist.name for artist in album_artists) if album_artists else entry.get("artist")
    )
    for key, value in (("©nam", title), ("©ART", artist), ("©alb", album), ("aART", album_artist)):
        if value:
            audio.tags[key] = [str(value)]
    artist_names = (
        [artist.name for artist in track.artists] if track and track.artists else [artist]
    )
    audio.tags["----:com.apple.iTunes:ARTISTS"] = [
        MP4FreeForm(str(name).encode())  # type: ignore[no-untyped-call]
        for name in artist_names
        if name
    ]
    if track:
        if track.track_number:
            audio.tags["trkn"] = [(track.track_number, 0)]
        if track.disc_number:
            audio.tags["disk"] = [(track.disc_number, 0)]
        for external_id, tag in (
            (ExternalID.ISRC, "ISRC"),
            (ExternalID.MB_RECORDING, "MusicBrainz Recording Id"),
            (ExternalID.MB_TRACK, "MusicBrainz Track Id"),
        ):
            if value := track.get_external_id(external_id):
                audio.tags[f"----:com.apple.iTunes:{tag}"] = [
                    MP4FreeForm(value.encode())  # type: ignore[no-untyped-call]
                ]
        if track.metadata.genres:
            audio.tags["©gen"] = sorted(track.metadata.genres)
        if year := getattr(track.album, "year", None):
            audio.tags["©day"] = [str(year)]
    elif number := re.match(r"(?:(\d+)-)?(\d+) ", Path(str(entry.get("path", ""))).name):
        audio.tags["trkn"] = [(int(number.group(2)), 0)]
        if number.group(1):
            audio.tags["disk"] = [(int(number.group(1)), 0)]
    audio.tags[f"----:com.apple.iTunes:{source_tag}"] = [MP4FreeForm(item_id.encode())]  # type: ignore[no-untyped-call]
    audio.save()

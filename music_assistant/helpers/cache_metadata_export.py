"""Export already known metadata alongside the cached music library."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from xml.etree import ElementTree as ET

from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist

from music_assistant.helpers.compare import compare_strings

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemImage, Track


class CacheMetadataExporter:
    """Export metadata without overwriting user-maintained files."""

    def __init__(self, cache: Any, root: str, *, cache_checksum: str | None = None) -> None:
        """Initialize the exporter."""
        self.cache = cache
        self.cache_checksum = cache_checksum
        self.root = Path(root)
        self._lock = asyncio.Lock()
        self._image_attempts: dict[str, float] = {}

    async def export(self, item_id: str) -> bool:
        """Export the known metadata for a published track, returning whether files changed."""
        async with self._lock:
            try:
                return await self._export(item_id)
            except Exception as err:
                self.cache.logger.warning("Could not export metadata for %s: %s", item_id, err)
                return False

    async def _export(self, item_id: str) -> bool:
        resolved = await self.cache._resolve_entry(item_id)
        if resolved is None or resolved[0].get("location") != "library":
            return False
        entry, path, _ = resolved
        mass = self.cache.provider.mass
        track = await mass.music.tracks.get_library_item_by_prov_id(
            item_id, self.cache.provider.instance_id
        )
        if track is None:
            return False
        albums = await mass.music.tracks.get_library_track_albums(track.item_id)
        album_name = str(entry.get("album") or "")
        matches = [album for album in albums if compare_strings(album.name, album_name)]
        matches.sort(
            key=lambda album: (
                not any(
                    mapping.provider_instance == self.cache.provider.instance_id
                    for mapping in album.provider_mappings
                )
            )
        )
        album = matches[0] if matches else None
        root, audio = await asyncio.to_thread(_resolve_paths, self.root, path)
        if not audio.is_relative_to(root) or audio.parent == root:
            return False
        artist_dir = audio.parent.parent
        if artist_dir == root:
            return False
        artist = await self._get_artist(track, album, artist_dir.name)
        preferred_album = (
            await self._cached_provider_item(album, Album, "get_album") if album else None
        )
        preferred_artist = (
            await self._cached_provider_item(artist, Artist, "get_artist") if artist else None
        )
        self._schedule_missing(track, album, artist)
        changed = False
        if album:
            data = _album_nfo(album, preferred_album)
            if data:
                changed |= await self._write(audio.parent / "album.nfo", data)
        if artist:
            data = _artist_nfo(artist, preferred_artist)
            if data:
                changed |= await self._write(artist_dir / "artist.nfo", data)
            image_path = artist_dir / "artist.jpg"
            now = time.monotonic()
            if now - self._image_attempts.get(
                str(image_path), -3600.0
            ) >= 3600 and await asyncio.to_thread(
                _can_create, self.root, Path(self.cache._index_dir), image_path
            ):
                self._image_attempts[str(image_path)] = now
                changed |= await self._artist_image(artist_dir, artist, preferred_artist)
        if track.metadata.lrc_lyrics:
            changed |= await self._write(
                audio.with_suffix(".lrc"), track.metadata.lrc_lyrics.encode("utf-8")
            )
        elif track.metadata.lyrics:
            changed |= await self._write(
                audio.with_suffix(".lyrics.txt"), track.metadata.lyrics.encode("utf-8")
            )
        return changed

    async def _get_artist(self, track: Track, album: Album | None, folder: str) -> Artist | None:
        """Resolve only the artist actually named by the destination folder."""
        mass = self.cache.provider.mass
        candidates = [*(album.artists if album else []), *track.artists]
        for candidate in candidates:
            if not compare_strings(candidate.name, folder):
                continue
            artist: Artist | None
            try:
                if candidate.provider == "library":
                    artist = await mass.music.artists.get_library_item(candidate.item_id)
                else:
                    artist = await mass.music.artists.get_library_item_by_prov_id(
                        candidate.item_id, candidate.provider
                    )
            except MediaNotFoundError:
                continue
            if artist and compare_strings(artist.name, folder):
                return artist
        return None

    async def _cached_provider_item[T: Album | Artist](
        self, item: T, item_type: type[T], method: str
    ) -> T | None:
        """Read the provider's parsed metadata cache without a service request."""
        for mapping in item.provider_mappings:
            if mapping.provider_instance != self.cache.provider.instance_id:
                continue
            result = await self.cache.provider.mass.cache.get(
                key=f"{method}.{mapping.item_id}",
                provider=mapping.provider_instance,
                checksum=self.cache_checksum,
                base_class=item_type,
                allow_expired_cache=True,
            )
            if isinstance(result, item_type):
                return result
        return None

    def _schedule_missing(self, track: Track, album: Album | None, artist: Artist | None) -> None:
        """Let MA's normal throttled metadata tasks fill gaps in the background."""
        metadata = self.cache.provider.mass.metadata
        if not (track.metadata.lyrics or track.metadata.lrc_lyrics):
            metadata.schedule_update_metadata(track)
        if album and not (
            album.metadata.description and album.metadata.label and album.metadata.copyright
        ):
            metadata.schedule_update_metadata(album)
        if artist and not (artist.metadata.description and artist.metadata.images):
            metadata.schedule_update_metadata(artist)

    async def _artist_image(
        self, folder: Path, artist: Artist, preferred_artist: Artist | None
    ) -> bool:
        """Save an artist thumbnail using MA's existing cached image pipeline."""
        candidates: list[MediaItemImage] = []
        for item in (preferred_artist, artist):
            if item:
                candidates.extend(
                    image for image in item.metadata.images or [] if image.type == ImageType.THUMB
                )
        candidates.sort(key=lambda image: image.provider != self.cache.provider.instance_id)
        attempted: set[str] = set()
        for image in candidates:
            if image.provider in attempted:
                continue
            if len(attempted) >= 3:
                break
            attempted.add(image.provider)
            try:
                async with asyncio.timeout(20):
                    data = await self.cache.provider.mass.metadata.get_thumbnail(
                        image.path,
                        provider=image.provider,
                        size=0,
                        image_format="JPEG",
                        flatten_transparency=True,
                    )
            except Exception as err:
                self.cache.logger.debug("Could not save artist image for %s: %s", artist.name, err)
                continue
            if isinstance(data, bytes) and data.startswith(b"\xff\xd8\xff"):
                return await self._write(folder / "artist.jpg", data)
        return False

    async def _write(self, path: Path, data: bytes) -> bool:
        """Write a sidecar only when absent or unchanged since our last write."""
        return await asyncio.to_thread(
            _write_owned, self.root, Path(self.cache._index_dir), path, data
        )


def _resolve_paths(root: Path, path: str) -> tuple[Path, Path]:
    """Resolve filesystem paths away from the event loop."""
    return root.resolve(), Path(path).resolve()


def _can_create(root: Path, index_dir: Path, path: Path) -> bool:
    """Avoid fetching artwork for an existing or deliberately deleted sidecar."""
    base = root.resolve()
    target = path.resolve()
    if not target.is_relative_to(base) or path.exists() or path.is_symlink():
        return False
    key = hashlib.sha256(str(target.relative_to(base)).encode()).hexdigest()
    return not (index_dir / "metadata" / f"{key}.json").exists()


def _nfo(root: str, values: dict[str, str | int | None]) -> bytes:
    """Build a simple XML metadata document."""
    doc = ET.Element(root)
    for key, value in values.items():
        if value is not None and value != "":
            text = "".join(c for c in str(value) if c in "\t\n\r" or ord(c) >= 32)
            ET.SubElement(doc, key).text = text
    return cast("bytes", ET.tostring(doc, encoding="utf-8", xml_declaration=True))


def _album_nfo(album: Album, preferred: Album | None) -> bytes | None:
    """Build album metadata, preferring cached provider fields."""
    primary = preferred.metadata if preferred else album.metadata
    metadata = album.metadata
    description = primary.description or metadata.description
    label = primary.label or metadata.label
    copyright_text = primary.copyright or metadata.copyright
    if not (description or label or copyright_text):
        return None
    return _nfo(
        "album",
        {
            "title": album.name,
            "review": description,
            "label": label,
            "copyright": copyright_text,
            "year": (preferred.year if preferred else None) or album.year,
            "genre": "; ".join(sorted(metadata.genres or [])),
            "musicbrainzalbumid": album.get_external_id(ExternalID.MB_ALBUM),
            "musicbrainzreleasegroupid": album.get_external_id(ExternalID.MB_RELEASEGROUP),
        },
    )


def _artist_nfo(artist: Artist, preferred: Artist | None) -> bytes | None:
    """Build artist metadata, preferring cached provider biography."""
    description = (
        preferred.metadata.description if preferred else None
    ) or artist.metadata.description
    if not description:
        return None
    return _nfo(
        "artist",
        {
            "name": artist.name,
            "biography": description,
            "genre": "; ".join(sorted(artist.metadata.genres or [])),
            "musicbrainzartistid": artist.mbid,
        },
    )


def _write_owned(root: Path, index_dir: Path, path: Path, data: bytes) -> bool:
    """Atomically create or update an unchanged exporter-owned sidecar."""
    base = root.resolve()
    target = path.resolve()
    if not target.is_relative_to(base) or target == base:
        raise ValueError("Metadata destination is outside the library")
    key = hashlib.sha256(str(target.relative_to(base)).encode()).hexdigest()
    marker = index_dir / "metadata" / f"{key}.json"
    previous: dict[str, Any] | None = None
    if marker.is_file():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return False
    if target.exists():
        if previous is None or not target.is_file() or path.is_symlink():
            return False
        current = target.read_bytes()
        if hashlib.sha256(current).hexdigest() != previous.get("sha256"):
            return False
        if current == data:
            return False
        old_stat = target.stat()
    elif previous is not None:
        return False
    else:
        old_stat = None
    target.parent.mkdir(parents=True, exist_ok=True)
    owner = base.stat()
    fd, temp = tempfile.mkstemp(prefix=".ma-metadata-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o644)
            if os.geteuid() == 0:
                os.fchown(file.fileno(), owner.st_uid, owner.st_gid)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if old_stat is None:
            try:
                os.link(temp, target)
            except FileExistsError:
                return False
        else:
            current_stat = target.stat()
            if (current_stat.st_ino, current_stat.st_mtime_ns, current_stat.st_size) != (
                old_stat.st_ino,
                old_stat.st_mtime_ns,
                old_stat.st_size,
            ):
                return False
            Path(temp).replace(target)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker_temp = marker.with_suffix(".tmp")
        marker_temp.write_text(
            json.dumps({"sha256": hashlib.sha256(data).hexdigest()}), encoding="utf-8"
        )
        marker_temp.replace(marker)
        return True
    finally:
        Path(temp).unlink(missing_ok=True)

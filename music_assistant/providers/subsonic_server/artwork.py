"""Cover art for Subsonic clients, from MA's images with the album folder's cover as fallback."""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MusicAssistantError

from .mapping import PREFIX_ALBUM, PREFIX_ARTIST, PREFIX_PLAYLIST, PREFIX_TRACK, parse_id
from .responses import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemImage

    from .server import SubsonicServer

ARTIST_FILE_NAMES = ("artist.jpg", "artist.png", "folder.jpg")
COVER_FILE_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "album.jpg")
MAX_CACHED_IMAGES = 300


@dataclass(frozen=True)
class Image:
    """Image bytes ready to send."""

    data: bytes
    content_type: str
    etag: str


class Artwork:
    """Finds, resizes and caches cover art."""

    def __init__(self, server: SubsonicServer) -> None:
        """
        Initialize the artwork lookup.

        :param server: The Subsonic server.
        """
        self.server = server
        self.mass = server.mass
        self._cache: OrderedDict[tuple[str, int], Image] = OrderedDict()

    async def get(self, cover_id: str, size: int) -> Image | None:
        """
        Return the image for a cover art ID, or None when there is none.

        :param cover_id: The cover art ID ("al-…", "tr-…", "ar-…" or "pl-…").
        :param size: The wanted size in pixels; 0 for the original.
        """
        key = (cover_id, size)
        if (cached := self._cache.get(key)) is not None:
            self._cache.move_to_end(key)
            return cached
        try:
            prefix, item_id = parse_id(cover_id)
        except SubsonicError:
            return None
        data: bytes | None = None
        for path, provider in await self._sources(prefix, item_id):
            try:
                thumb = await self.mass.metadata.get_thumbnail(path, provider, size=size or None)
            except (MusicAssistantError, OSError) as err:
                self.server.logger.debug("Cover art for %s not at %s: %s", cover_id, path, err)
                continue
            if isinstance(thumb, bytes) and thumb:
                data = thumb
                break
        if data is None:
            return None
        image = Image(data, _content_type(data), f'"{hashlib.sha1(data).hexdigest()[:16]}"')
        self._cache[key] = image
        if len(self._cache) > MAX_CACHED_IMAGES:
            self._cache.popitem(last=False)
        return image

    def response(self, request: web.Request, image: Image) -> web.Response:
        """
        Return the HTTP response for an image, answering a matching If-None-Match with 304.

        :param request: The request.
        :param image: The image.
        """
        headers = {"Cache-Control": "public, no-cache", "ETag": image.etag}
        if request.headers.get("If-None-Match") == image.etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=image.data, content_type=image.content_type, headers=headers)

    async def _sources(self, prefix: str, item_id: int) -> list[tuple[str, str]]:
        """
        Return the (path, provider) image candidates for an item, best first.

        MA's own images come first; for albums and songs the folder's cover file and the
        artwork embedded in the audio file follow, for when MA's image is gone.

        :param prefix: The ID type prefix.
        :param item_id: The library id.
        """
        music = self.mass.music
        index = self.server.index
        if prefix == PREFIX_ALBUM:
            return await self._album_sources(item_id)
        if prefix == PREFIX_TRACK:
            sources = await self._library_images(music.tracks, item_id)
            track = index.tracks.get(item_id)
            if track is not None and track.album_id is not None:
                sources += await self._album_sources(track.album_id)
            if track is not None:
                sources.append((track.rel_path, track.provider_instance))
            return list(dict.fromkeys(sources))
        if prefix == PREFIX_ARTIST:
            return await self._artist_sources(item_id)
        if prefix == PREFIX_PLAYLIST:
            return await self._library_images(music.playlists, item_id)
        return []

    async def _album_sources(self, album_id: int) -> list[tuple[str, str]]:
        """
        Return the image candidates for an album.

        :param album_id: The library album id (negative for an "[Unknown Album]").
        """
        sources = []
        if album_id > 0:
            sources += await self._library_images(self.mass.music.albums, album_id)
        album = self.server.index.albums.get(album_id)
        tracks = self.server.index.album_tracks(album) if album is not None else []
        if tracks:
            track = tracks[0]
            folder = os.path.dirname(track.rel_path)
            abs_folder = os.path.dirname(track.abs_path)
            for name in COVER_FILE_NAMES:
                if (Path(abs_folder) / name).is_file():
                    sources.append((os.path.join(folder, name), track.provider_instance))
                    break
            sources.append((track.rel_path, track.provider_instance))
            # last resort: the image MA has for the song itself (e.g. a YouTube thumbnail)
            sources += await self._library_images(self.mass.music.tracks, track.id)
        return list(dict.fromkeys(sources))

    async def _artist_sources(self, artist_id: int) -> list[tuple[str, str]]:
        """
        Return the image candidates for an artist.

        MA's image comes first, then an artist image in the artist's folder, then the cover
        of one of the artist's albums.

        :param artist_id: The library artist id.
        """
        sources = await self._library_images(self.mass.music.artists, artist_id)
        index = self.server.index
        artist = index.artists.get(artist_id)
        albums = index.artist_albums(artist) if artist is not None else []
        tracks = index.album_tracks(albums[0]) if albums else []
        if tracks:
            track = tracks[0]
            artist_folder = os.path.dirname(os.path.dirname(track.rel_path))
            abs_artist_folder = os.path.dirname(os.path.dirname(track.abs_path))
            for name in ARTIST_FILE_NAMES:
                if (Path(abs_artist_folder) / name).is_file():
                    sources.append((os.path.join(artist_folder, name), track.provider_instance))
                    break
            sources += await self._album_sources(albums[0].id)
        return list(dict.fromkeys(sources))

    @staticmethod
    async def _library_images(controller: Any, item_id: int) -> list[tuple[str, str]]:
        """
        Return the thumb images MA has for a library item.

        :param controller: The MA media controller for the item's type.
        :param item_id: The library id.
        """
        try:
            item = await controller.get_library_item(item_id)
        except MusicAssistantError:
            return []
        images: list[MediaItemImage] = list(item.metadata.images or []) if item.metadata else []
        return [(i.path, i.provider) for i in images if i.type == ImageType.THUMB]


def _content_type(data: bytes) -> str:
    """
    Return the image type from its first bytes.

    :param data: The image bytes.
    """
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.lstrip()[:4] in (b"<svg", b"<?xm"):
        return "image/svg+xml"
    return "image/jpeg"

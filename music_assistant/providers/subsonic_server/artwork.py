"""Cover art for Subsonic clients, from MA's images with the album folder's cover as fallback."""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from music_assistant_models.errors import MusicAssistantError

from .mapping import PREFIX_ALBUM, PREFIX_ARTIST, PREFIX_PLAYLIST, PREFIX_TRACK, parse_id
from .responses import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemImage

    from .index import IndexedAlbum
    from .server import SubsonicServer

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
        source = await self._find_source(prefix, item_id)
        if source is None:
            return None
        path, provider = source
        try:
            data = await self.mass.metadata.get_thumbnail(path, provider, size=size or None)
        except (MusicAssistantError, OSError) as err:
            self.server.logger.debug("No cover art for %s: %s", cover_id, err)
            return None
        assert isinstance(data, bytes)
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

    async def _find_source(self, prefix: str, item_id: int) -> tuple[str, str] | None:
        """
        Return the (path, provider) of the image for a library item.

        :param prefix: The ID type prefix.
        :param item_id: The library id.
        """
        music = self.mass.music
        index = self.server.index
        image: MediaItemImage | None = None
        try:
            if prefix == PREFIX_ALBUM:
                image = (await music.albums.get_library_item(item_id)).image
                if image is None and (album := index.albums.get(item_id)) is not None:
                    return self._folder_cover(album)
            elif prefix == PREFIX_TRACK:
                image = (await music.tracks.get_library_item(item_id)).image
                track = index.tracks.get(item_id)
                if image is None and track is not None and track.album_id is not None:
                    album = index.albums.get(track.album_id)
                    album_image = (await music.albums.get_library_item(track.album_id)).image
                    if album_image is not None:
                        image = album_image
                    elif album is not None:
                        return self._folder_cover(album)
            elif prefix == PREFIX_ARTIST:
                image = (await music.artists.get_library_item(item_id)).image
            elif prefix == PREFIX_PLAYLIST:
                image = (await music.playlists.get_library_item(item_id)).image
        except MusicAssistantError:
            return None
        if image is None:
            return None
        return image.path, image.provider

    def _folder_cover(self, album: IndexedAlbum) -> tuple[str, str] | None:
        """
        Return a cover image file next to the album's first track, if there is one.

        :param album: The album.
        """
        tracks = self.server.index.album_tracks(album)
        if not tracks:
            return None
        track = tracks[0]
        folder = os.path.dirname(track.rel_path)
        abs_folder = os.path.dirname(track.abs_path)
        for name in COVER_FILE_NAMES:
            if (Path(abs_folder) / name).is_file():
                return os.path.join(folder, name), track.provider_instance
        return None


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

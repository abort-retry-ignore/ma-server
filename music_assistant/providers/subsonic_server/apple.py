"""
Apple Music catalogue for Subsonic clients: search, browsing and saving songs on request.

Search results and the albums and artists behind them come from MA's Apple Music provider,
which caches them and throttles its calls to Apple. A song that has no file yet is saved by
the fork's Apple Music audio cache when a client asks to play it: the request waits the few
seconds the download and decryption take, then gets an ordinary file, with its size and
range support. The cache also copies the file into the music folder, so it then shows up as
a normal song.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, defaultdict
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import MediaType, ProviderType, StreamType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.helpers import create_uri

from .constants import (
    APPLE_DOMAIN,
    APPLE_SAVE_TIMEOUT,
    APPLE_SEARCH_LIMIT,
    APPLE_SEARCH_MIN_LENGTH,
    APPLE_TTL,
    ERR_GENERIC,
    ERR_NOT_FOUND,
    MAX_APPLE_SAVES,
)
from .responses import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, SearchResults, Track

    from .index import IndexedTrack
    from .server import SubsonicServer

PREFIX_APPLE_TRACK = "amt"
PREFIX_APPLE_ALBUM = "ama"
PREFIX_APPLE_ARTIST = "amr"
APPLE_PREFIXES = (PREFIX_APPLE_TRACK, PREFIX_APPLE_ALBUM, PREFIX_APPLE_ARTIST)
_APPLE_ID = re.compile(r"^[A-Za-z0-9._]{1,300}$")
# real Apple Music ids: catalogue ids are numbers, library ids look like "l.AbC123"
_REAL_APPLE_ID = re.compile(r"^(\d+|[a-z]\.[A-Za-z0-9]+)$")
# an artist or album known only by its name, written as "n" + the name in hex
_NAME_REF = re.compile(r"^n((?:[0-9a-f]{2})+)$")
_MAX_CACHED = 500


def name_ref(item_id: str, name: str) -> str:
    """
    Return a usable id for an album or artist reference.

    MA's Apple Music provider uses the name as id when Apple gives no link to the artist or
    album; such a reference is encoded so it can be looked up by name later.

    :param item_id: The id of the reference.
    :param name: The name of the album or artist.
    """
    if _REAL_APPLE_ID.match(item_id):
        return item_id
    return "n" + name.encode().hex()


def _name_of(item_id: str) -> str | None:
    """
    Return the name of a reference made by ``name_ref``, or None for a real id.

    :param item_id: The id.
    """
    if (match := _NAME_REF.match(item_id)) is None:
        return None
    try:
        return bytes.fromhex(match.group(1)).decode()
    except UnicodeDecodeError:
        return None


def apple_id(value: str | None, *allowed: str) -> tuple[str, str] | None:
    """
    Return (prefix, Apple Music id) for an Apple catalogue Subsonic ID, or None for other IDs.

    :param value: The Subsonic ID.
    :param allowed: The Apple prefixes accepted here (all when empty).
    :raises SubsonicError: Not found (70) for an Apple ID of a type not accepted here.
    """
    prefix, _, raw = (value or "").partition("-")
    if prefix not in APPLE_PREFIXES:
        return None
    if not _APPLE_ID.match(raw) or (allowed and prefix not in allowed):
        raise SubsonicError(ERR_NOT_FOUND, "Item not found")
    return prefix, raw


class AppleCatalog:
    """Apple Music search, lookups and on-demand saving for the Subsonic server."""

    def __init__(self, server: SubsonicServer) -> None:
        """
        Initialize the catalogue.

        :param server: The Subsonic server.
        """
        self.server = server
        self.mass = server.mass
        self.logger = server.logger
        self._cache: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        self._saves: dict[str, asyncio.Task[str]] = {}
        self._user_slots: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(1)
        )
        self._all_slots = asyncio.Semaphore(MAX_APPLE_SAVES)

    @property
    def provider(self) -> Any | None:
        """Return the loaded Apple Music provider, or None."""
        for provider in self.mass.get_providers(ProviderType.MUSIC):
            if provider.domain == APPLE_DOMAIN and provider.available:
                return provider
        return None

    @property
    def enabled(self) -> bool:
        """Return whether Apple Music results are offered to clients."""
        return self.server.apple_search_enabled and self.provider is not None

    def uris(self, item_id: str) -> list[str]:
        """
        Return the uris an Apple Music track is known by.

        :param item_id: The Apple Music track id.
        """
        uris = [create_uri(MediaType.TRACK, APPLE_DOMAIN, item_id)]
        if (provider := self.provider) is not None:
            uris.append(create_uri(MediaType.TRACK, provider.instance_id, item_id))
        return uris

    def is_blocked(self, item_id: str) -> bool:
        """
        Return whether an Apple Music track was marked as the wrong song.

        :param item_id: The Apple Music track id.
        """
        return self.mass.music.play_memory.is_blocked_uri(self.uris(item_id))

    def library_track(self, item_id: str) -> IndexedTrack | None:
        """
        Return the visible library track that already has a file of this Apple Music track.

        :param item_id: The Apple Music track id.
        """
        index = self.server.index
        for uri in self.uris(item_id):
            if (track_id := index.track_by_uri.get(uri)) is not None:
                return index.get_track(track_id)
        return None

    async def search(self, query: str) -> SearchResults | None:
        """
        Return Apple Music search results for a query, or None when it is too short.

        :param query: The search text.
        """
        query = query.strip().strip('"').strip()
        provider = self.provider
        if provider is None or len(query) < APPLE_SEARCH_MIN_LENGTH:
            return None
        key = ("search", query.casefold())
        if (cached := self._get_cached(key)) is not None:
            return cast("SearchResults", cached)
        try:
            results = await self.mass.music.search(
                query,
                [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK],
                limit=APPLE_SEARCH_LIMIT,
                providers=[provider.instance_id],
            )
        except MusicAssistantError as err:
            self.logger.warning("Apple Music search for '%s' failed: %s", query, err)
            return None
        self._put_cached(key, results)
        return results

    async def track(self, item_id: str) -> Track:
        """
        Return an Apple Music track.

        :param item_id: The Apple Music track id.
        :raises SubsonicError: Not found (70).
        """
        return cast(
            "Track", await self._get("track", item_id, self.mass.music.tracks.get_provider_item)
        )

    async def album(self, item_id: str) -> Album:
        """
        Return an Apple Music album.

        :param item_id: The Apple Music album id.
        :raises SubsonicError: Not found (70).
        """
        item_id = await self._real_id(item_id, MediaType.ALBUM)
        return cast(
            "Album", await self._get("album", item_id, self.mass.music.albums.get_provider_item)
        )

    async def artist(self, item_id: str) -> Artist:
        """
        Return an Apple Music artist.

        :param item_id: The Apple Music artist id.
        :raises SubsonicError: Not found (70).
        """
        item_id = await self._real_id(item_id, MediaType.ARTIST)
        return cast(
            "Artist", await self._get("artist", item_id, self.mass.music.artists.get_provider_item)
        )

    async def album_tracks(self, item_id: str) -> list[Track]:
        """
        Return the tracks of an Apple Music album.

        :param item_id: The Apple Music album id.
        """
        item_id = await self._real_id(item_id, MediaType.ALBUM)
        return cast(
            "list[Track]", await self._get("album_tracks", item_id, self.mass.music.albums.tracks)
        )

    async def artist_albums(self, item_id: str) -> list[Album]:
        """
        Return the albums of an Apple Music artist.

        :param item_id: The Apple Music artist id.
        """
        item_id = await self._real_id(item_id, MediaType.ARTIST)
        return cast(
            "list[Album]", await self._get("artist_albums", item_id, self.mass.music.artists.albums)
        )

    async def ensure_file(self, item_id: str, user_id: str) -> str:
        """
        Return the path of the saved file of an Apple Music track, saving it first if needed.

        Requests for the same track share one download; each user saves one track at a time.

        :param item_id: The Apple Music track id.
        :param user_id: The MA user id of the requester.
        :raises SubsonicError: When the track can't be saved in time.
        """
        streams = self._streams()
        if (path := await streams.audio_cache.lookup_clear(item_id)) is not None:
            return str(path)
        task = self._saves.get(item_id)
        if task is None:
            task = asyncio.create_task(self._save(item_id, user_id))
            self._saves[item_id] = task
            task.add_done_callback(lambda _: self._saves.pop(item_id, None))
        try:
            # shielded: a client that gives up must not abort the download for the others
            return await asyncio.wait_for(asyncio.shield(task), timeout=APPLE_SAVE_TIMEOUT)
        except TimeoutError as err:
            raise SubsonicError(ERR_GENERIC, "Saving the song took too long, try again") from err

    def apple_track_id(self, track: Track) -> str | None:
        """
        Return the Apple Music id of a track from search, an album or the library.

        :param track: The track.
        """
        if track.provider != "library" and self._is_apple(track.provider):
            return track.item_id
        for mapping in track.provider_mappings:
            if mapping.provider_domain == APPLE_DOMAIN:
                return mapping.item_id
        return None

    def apple_item_id(self, item: Album | Artist) -> str | None:
        """
        Return the Apple Music id of an album or artist.

        :param item: The album or artist.
        """
        if item.provider != "library" and self._is_apple(item.provider):
            return item.item_id
        for mapping in item.provider_mappings:
            if mapping.provider_domain == APPLE_DOMAIN:
                return mapping.item_id
        return None

    async def _save(self, item_id: str, user_id: str) -> str:
        """
        Download and decrypt an Apple Music track into the audio cache.

        :param item_id: The Apple Music track id.
        :param user_id: The MA user id of the requester.
        """
        streams = self._streams()
        async with self._user_slots[user_id], self._all_slots:
            if (path := await streams.audio_cache.lookup_clear(item_id)) is not None:
                return str(path)
            started = time.monotonic()
            self.logger.info("Saving Apple Music track %s for a Subsonic client", item_id)
            try:
                details = await streams.get_stream_details(item_id)
                if details.stream_type == StreamType.LOCAL_FILE and details.path:
                    return str(details.path)
                if not hasattr(details.data, "key_id"):
                    raise SubsonicError(ERR_GENERIC, "This song can't be saved")
                async for _chunk in streams.stream_track_and_store(details):
                    pass
            except MusicAssistantError as err:
                self.logger.warning("Saving Apple Music track %s failed: %s", item_id, err)
                raise SubsonicError(ERR_GENERIC, f"Apple Music: {err}") from err
            path = await streams.audio_cache.lookup_clear(item_id)
            if path is None:
                self.logger.warning("Apple Music track %s was not saved completely", item_id)
                raise SubsonicError(ERR_GENERIC, "The song could not be saved")
            self.logger.info(
                "Saved Apple Music track %s in %.1f s", item_id, time.monotonic() - started
            )
            self.server.schedule_index_rebuild()
            return str(path)

    async def _real_id(self, item_id: str, media_type: MediaType) -> str:
        """
        Return the real Apple Music id for an id made by ``name_ref``, by searching the name.

        :param item_id: The id (returned unchanged when it is already a real id).
        :param media_type: Album or artist.
        :raises SubsonicError: Not found (70) when the search finds nothing by that name.
        """
        if (name := _name_of(item_id)) is None:
            return item_id
        results = await self.search(name)
        found: list[Any] = []
        if results is not None:
            found = list(results.artists if media_type == MediaType.ARTIST else results.albums)
        wanted = name.casefold()
        for item in sorted(found, key=lambda i: i.name.casefold() != wanted):
            if (real := self.apple_item_id(item)) is not None:
                return real
        raise SubsonicError(ERR_NOT_FOUND, f"Not found on Apple Music: {name}")

    def _streams(self) -> Any:
        """
        Return the Apple Music provider's caching stream manager.

        :raises SubsonicError: When Apple Music or its audio cache isn't available.
        """
        provider = self.provider
        streams = getattr(provider, "caching_streams", None)
        if streams is None or getattr(streams, "audio_cache", None) is None:
            raise SubsonicError(ERR_GENERIC, "Apple Music songs can't be saved on this server")
        return streams

    def _is_apple(self, provider: str) -> bool:
        """
        Return whether a provider instance id or domain is Apple Music.

        :param provider: The provider instance id or domain.
        """
        return provider == APPLE_DOMAIN or provider.startswith(f"{APPLE_DOMAIN}--")

    async def _get(self, kind: str, item_id: str, getter: Any) -> Any:
        """
        Return an Apple Music item (or list), cached in memory for a while.

        :param kind: What is fetched, part of the cache key.
        :param item_id: The Apple Music id.
        :param getter: The MA controller method, called with (item_id, provider instance).
        :raises SubsonicError: Not found (70).
        """
        provider = self.provider
        if provider is None:
            raise SubsonicError(ERR_NOT_FOUND, "Apple Music is not available")
        key = (kind, item_id)
        if (cached := self._get_cached(key)) is not None:
            return cached
        try:
            result = await getter(item_id, provider.instance_id)
        except MusicAssistantError as err:
            raise SubsonicError(ERR_NOT_FOUND, f"Not found on Apple Music: {err}") from err
        self._put_cached(key, result)
        return result

    def _get_cached(self, key: tuple[str, str]) -> Any | None:
        """
        Return a cached result that hasn't expired.

        :param key: The cache key.
        """
        entry = self._cache.get(key)
        if entry is None or entry[0] < time.monotonic():
            return None
        self._cache.move_to_end(key)
        return entry[1]

    def _put_cached(self, key: tuple[str, str], value: Any) -> None:
        """
        Remember a result for a while.

        :param key: The cache key.
        :param value: The result.
        """
        self._cache[key] = (time.monotonic() + APPLE_TTL, value)
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHED:
            self._cache.popitem(last=False)

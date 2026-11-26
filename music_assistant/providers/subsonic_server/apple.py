"""
Apple Music and YouTube Music for Subsonic clients: search, browsing and saving on request.

Search results and the albums and artists behind them come from MA's providers, which cache
them and throttle their calls. A song that has no file yet is saved by the fork's audio cache
of its service when a client asks to play it: the request waits the few seconds the download
takes, then gets an ordinary file, with its size and range support. The cache also copies the
file into the music folder, so it then shows up as a normal song.

Both services share the "amt-"/"ama-"/"amr-" IDs; YouTube Music ids carry a "y." in front.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, defaultdict
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import ImageType, MediaType, ProviderType, StreamType
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
    ICLOUD_URL_TTL,
    MAX_APPLE_SAVES,
    YOUTUBE_DOMAIN,
    YOUTUBE_SAVE_GAP,
)
from .index import normalize
from .responses import SubsonicError

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, SearchResults, Track

    from .index import IndexedTrack
    from .server import SubsonicServer

PREFIX_APPLE_TRACK = "amt"
PREFIX_APPLE_ALBUM = "ama"
PREFIX_APPLE_ARTIST = "amr"
APPLE_PREFIXES = (PREFIX_APPLE_TRACK, PREFIX_APPLE_ALBUM, PREFIX_APPLE_ARTIST)
_APPLE_ID = re.compile(r"^[A-Za-z0-9._-]{1,300}$")
# YouTube Music ids are marked with this in front
YOUTUBE_MARK = "y."
# real Apple Music ids: catalogue ids are numbers, library ids look like "l.AbC123"
_REAL_APPLE_ID = re.compile(r"^(\d+|[a-z]\.[A-Za-z0-9]+)$")
# an artist or album known only by its name, written as "n" + the name in hex
_NAME_REF = re.compile(r"^n((?:[0-9a-f]{2})+)$")
_MAX_CACHED = 500
_MAX_IMAGES = 5000
_BRACKETS = re.compile(r"[\(\[][^\)\]]*[\)\]]")


def name_ref(item_id: str, name: str, artist: str | None = None) -> str:
    """
    Return a usable id for an album or artist reference.

    MA's Apple Music provider uses the name as id when Apple gives no link to the artist or
    album; such a reference is encoded (with the artist, for an album) so it can be looked
    up by name later.

    :param item_id: The id of the reference.
    :param name: The name of the album or artist.
    :param artist: The album's artist, to find the right album by name.
    """
    if _REAL_APPLE_ID.match(item_id) or is_youtube_id(item_id):
        return item_id
    text = f"{name}\x1f{artist}" if artist else name
    return "n" + text.encode().hex()


def is_name_ref(item_id: str) -> bool:
    """
    Return whether an id was made by ``name_ref`` from a name.

    :param item_id: The id.
    """
    return _NAME_REF.match(item_id) is not None


def is_youtube_id(item_id: str) -> bool:
    """
    Return whether a catalogue id is a YouTube Music one.

    :param item_id: The catalogue id.
    """
    return item_id.startswith(YOUTUBE_MARK)


def is_icloud_id(item_id: str) -> bool:
    """
    Return whether an Apple Music track id is a song that only lives in the iCloud library.

    :param item_id: The Apple Music track id.
    """
    return item_id.startswith("i.")


def image_sources(item: Any) -> list[tuple[str, str]]:
    """
    Return the (path, provider) of an item's square images, then its wide ones.

    :param item: A media item.
    """
    metadata = getattr(item, "metadata", None)
    images = list(metadata.images or []) if metadata else []
    ordered = [i for i in images if i.type == ImageType.THUMB]
    ordered += [i for i in images if i.type == ImageType.LANDSCAPE]
    return [(i.path, i.provider) for i in ordered]


def _name_of(item_id: str) -> tuple[str, str | None] | None:
    """
    Return (name, artist) of a reference made by ``name_ref``, or None for a real id.

    :param item_id: The id.
    """
    if (match := _NAME_REF.match(item_id)) is None:
        return None
    try:
        text = bytes.fromhex(match.group(1)).decode()
    except UnicodeDecodeError:
        return None
    name, _, artist = text.partition("\x1f")
    return name, artist or None


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
        self._cache: OrderedDict[tuple[str, ...], tuple[float, Any]] = OrderedDict()
        self._saves: dict[str, asyncio.Task[str]] = {}
        self._user_slots: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(1)
        )
        self._all_slots = asyncio.Semaphore(MAX_APPLE_SAVES)
        self._images: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
        self._icloud_urls: dict[str, tuple[float, str]] = {}
        self._youtube_slot = asyncio.Semaphore(1)
        self._last_youtube_save = 0.0

    def remember_images(self, cover_id: str, item: Any) -> None:
        """
        Remember the images that came with an item, so its cover needs no Apple request.

        :param cover_id: The cover art ID the item is shown with.
        :param item: The song, album or artist from MA.
        """
        if sources := image_sources(item):
            self._images[cover_id] = sources
            self._images.move_to_end(cover_id)
            while len(self._images) > _MAX_IMAGES:
                self._images.popitem(last=False)

    def remembered_images(self, cover_id: str) -> list[tuple[str, str]] | None:
        """
        Return the remembered images of a cover art ID, or None.

        :param cover_id: The cover art ID.
        """
        return self._images.get(cover_id)

    async def icloud_url(self, item_id: str, fresh: bool = False) -> str:
        """
        Return the download link of a song that only lives in the iCloud library.

        Such songs are not encrypted; the link is reused for a while to spare Apple requests.

        :param item_id: The Apple Music track id ("i.…").
        :param fresh: Ask Apple for a new link.
        """
        cached = self._icloud_urls.get(item_id)
        if cached is not None and cached[0] > time.monotonic() and not fresh:
            return cached[1]
        try:
            details = await self._streams().get_stream_details(item_id)
        except MusicAssistantError as err:
            raise SubsonicError(ERR_GENERIC, f"Apple Music: {err}") from err
        if details.stream_type != StreamType.HTTP or not details.path:
            raise SubsonicError(ERR_GENERIC, "This song can't be played")
        self._icloud_urls[item_id] = (time.monotonic() + ICLOUD_URL_TTL, str(details.path))
        return str(details.path)

    @property
    def provider(self) -> Any | None:
        """Return the loaded Apple Music provider, or None."""
        return self._loaded(APPLE_DOMAIN)

    @property
    def youtube(self) -> Any | None:
        """Return the loaded YouTube Music provider, or None."""
        return self._loaded(YOUTUBE_DOMAIN)

    @property
    def apple_enabled(self) -> bool:
        """Return whether Apple Music results are offered to clients."""
        return self.server.apple_search_enabled and self.provider is not None

    @property
    def youtube_enabled(self) -> bool:
        """Return whether YouTube Music results are offered to clients."""
        return self.server.youtube_search_enabled and self.youtube is not None

    @property
    def enabled(self) -> bool:
        """Return whether any streaming service's results are offered to clients."""
        return self.apple_enabled or self.youtube_enabled

    def provider_for(self, item_id: str) -> tuple[Any, str]:
        """
        Return the provider and its own id for a catalogue id.

        :param item_id: The catalogue id ("y.…" for YouTube Music).
        :raises SubsonicError: Not found (70) when that service isn't available.
        """
        if is_youtube_id(item_id):
            provider, real_id = self.youtube, item_id[len(YOUTUBE_MARK) :]
        else:
            provider, real_id = self.provider, item_id
        if provider is None:
            raise SubsonicError(ERR_NOT_FOUND, "That music service is not available")
        return provider, real_id

    def ref_id(self, provider: str, item_id: str) -> str | None:
        """
        Return the catalogue id of an item a streaming provider knows by an id, or None.

        :param provider: The provider instance id or domain.
        :param item_id: The provider's id of the item.
        """
        if self._is_domain(provider, APPLE_DOMAIN):
            return item_id if self.apple_enabled else None
        if self._is_domain(provider, YOUTUBE_DOMAIN):
            return f"{YOUTUBE_MARK}{item_id}" if self.youtube_enabled else None
        return None

    @staticmethod
    def service_name(item_id: str) -> str:
        """
        Return the name of the service a catalogue id belongs to.

        :param item_id: The catalogue id.
        """
        return "YouTube Music" if is_youtube_id(item_id) else "Apple Music"

    def uris(self, item_id: str) -> list[str]:
        """
        Return the uris a catalogue track is known by.

        :param item_id: The catalogue track id.
        """
        if is_youtube_id(item_id):
            domain, provider, real_id = YOUTUBE_DOMAIN, self.youtube, item_id[len(YOUTUBE_MARK) :]
        else:
            domain, provider, real_id = APPLE_DOMAIN, self.provider, item_id
        uris = [create_uri(MediaType.TRACK, domain, real_id)]
        if provider is not None:
            uris.append(create_uri(MediaType.TRACK, provider.instance_id, real_id))
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
        provider = self.provider if self.apple_enabled else None
        return await self._search_on(provider, "Apple Music", query)

    async def search_youtube(self, query: str) -> SearchResults | None:
        """
        Return YouTube Music search results for a query, or None when it is too short.

        :param query: The search text.
        """
        provider = self.youtube if self.youtube_enabled else None
        return await self._search_on(provider, "YouTube Music", query)

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
        if is_youtube_id(item_id):
            if (path := await self._youtube_file(item_id)) is not None:
                return path
            save = self._save_youtube
        else:
            streams = self._streams()
            if (path := await streams.audio_cache.lookup_clear(item_id)) is not None:
                return str(path)
            save = self._save
        task = self._saves.get(item_id)
        if task is None:
            task = asyncio.create_task(save(item_id, user_id))
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
        return self._catalogue_id(track)

    def apple_item_id(self, item: Album | Artist) -> str | None:
        """
        Return the Apple Music id of an album or artist.

        :param item: The album or artist.
        """
        return self._catalogue_id(item)

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
        if is_youtube_id(item_id) or (named := _name_of(item_id)) is None:
            return item_id
        name, artist = named
        key = ("real_id", item_id)
        if (cached := self._get_cached(key)) is not None:
            return cast("str", cached)
        results = await self.search(f"{name} {artist}" if artist else name)
        found: list[Any] = []
        if results is not None:
            found = list(results.artists if media_type == MediaType.ARTIST else results.albums)
        wanted = normalize(_BRACKETS.sub(" ", name))
        wanted_artist = normalize(artist or "")

        def score(item: Any) -> tuple[bool, bool]:
            names = " ".join(a.name for a in getattr(item, "artists", ()))
            same_name = normalize(_BRACKETS.sub(" ", item.name)) == wanted
            same_artist = not wanted_artist or wanted_artist in normalize(names)
            return not same_name, not same_artist

        for item in sorted(found, key=score):
            if (real := self.apple_item_id(item)) is not None:
                self._put_cached(key, real)
                return real
        raise SubsonicError(ERR_NOT_FOUND, f"Not found on Apple Music: {name}")

    async def _search_on(
        self, provider: Any | None, service: str, query: str
    ) -> SearchResults | None:
        """
        Return a provider's search results for a query, cached for a while.

        :param provider: The music provider (None when it is off or not loaded).
        :param service: The service name, for the log.
        :param query: The search text.
        """
        query = query.strip().strip('"').strip()
        if provider is None or len(query) < APPLE_SEARCH_MIN_LENGTH:
            return None
        key = ("search", provider.instance_id, query.casefold())
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
            self.logger.warning("%s search for '%s' failed: %s", service, query, err)
            return None
        self._put_cached(key, results)
        return results

    def _loaded(self, domain: str) -> Any | None:
        """
        Return the loaded, available music provider of a domain, or None.

        :param domain: The provider domain.
        """
        for provider in self.mass.get_providers(ProviderType.MUSIC):
            if provider.domain == domain and provider.available:
                return provider
        return None

    def _catalogue_id(self, item: Any) -> str | None:
        """
        Return the catalogue id of a song, album or artist: Apple Music first, then YouTube.

        :param item: The item from search, an album, a playlist or the library.
        """
        provider = str(item.provider)
        if provider != "library" and (found := self.ref_id(provider, str(item.item_id))):
            return found
        mappings = list(getattr(item, "provider_mappings", ()))
        if self.apple_enabled:
            for mapping in mappings:
                if mapping.provider_domain == APPLE_DOMAIN:
                    return str(mapping.item_id)
        if self.youtube_enabled:
            for mapping in mappings:
                if mapping.provider_domain == YOUTUBE_DOMAIN:
                    return f"{YOUTUBE_MARK}{mapping.item_id}"
        return None

    async def _youtube_file(self, item_id: str) -> str | None:
        """
        Return the saved file of a YouTube Music track, or None.

        :param item_id: The catalogue id ("y.…").
        """
        provider, real_id = self.provider_for(item_id)
        cache = getattr(provider, "_audio_cache", None)
        if cache is None:
            raise SubsonicError(ERR_GENERIC, "YouTube Music songs can't be saved on this server")
        cached = await cache.lookup(real_id)
        return str(cached[0]) if cached else None

    async def _save_youtube(self, item_id: str, user_id: str) -> str:
        """
        Download a YouTube Music track into its audio cache, one at a time and spaced out.

        :param item_id: The catalogue id ("y.…").
        :param user_id: The MA user id of the requester.
        """
        provider, real_id = self.provider_for(item_id)
        async with self._user_slots[user_id], self._youtube_slot:
            if (path := await self._youtube_file(item_id)) is not None:
                return path
            # YouTube is quick to suspect bots: keep saves apart
            if (wait := self._last_youtube_save + YOUTUBE_SAVE_GAP - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            self._last_youtube_save = time.monotonic()
            started = time.monotonic()
            self.logger.info("Saving YouTube Music track %s for a Subsonic client", real_id)
            try:
                details = await provider.get_stream_details(real_id, MediaType.TRACK)
                if details.stream_type == StreamType.LOCAL_FILE and details.path:
                    return str(details.path)
                if not hasattr(details.data, "expected_size"):
                    raise SubsonicError(ERR_GENERIC, "This song can't be saved")
                async for _chunk in provider.get_audio_stream(details):
                    pass
            except MusicAssistantError as err:
                self.logger.warning("Saving YouTube Music track %s failed: %s", real_id, err)
                raise SubsonicError(ERR_GENERIC, f"YouTube Music: {err}") from err
            path = await self._youtube_file(item_id)
            if path is None:
                self.logger.warning("YouTube Music track %s was not saved completely", real_id)
                raise SubsonicError(ERR_GENERIC, "The song could not be saved")
            self.logger.info(
                "Saved YouTube Music track %s in %.1f s", real_id, time.monotonic() - started
            )
            self.server.schedule_index_rebuild()
            return path

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

    @staticmethod
    def _is_domain(provider: str, domain: str) -> bool:
        """
        Return whether a provider instance id or domain belongs to a domain.

        :param provider: The provider instance id or domain.
        :param domain: The domain.
        """
        return provider == domain or provider.startswith(f"{domain}--")

    async def _get(self, kind: str, item_id: str, getter: Any) -> Any:
        """
        Return an Apple Music item (or list), cached in memory for a while.

        :param kind: What is fetched, part of the cache key.
        :param item_id: The Apple Music id.
        :param getter: The MA controller method, called with (item_id, provider instance).
        :raises SubsonicError: Not found (70).
        """
        provider, real_id = self.provider_for(item_id)
        key = (kind, item_id)
        if (cached := self._get_cached(key)) is not None:
            return cached
        try:
            result = await getter(real_id, provider.instance_id)
        except MusicAssistantError as err:
            raise SubsonicError(ERR_NOT_FOUND, f"Not found: {err}") from err
        self._put_cached(key, result)
        return result

    def _get_cached(self, key: tuple[str, ...]) -> Any | None:
        """
        Return a cached result that hasn't expired.

        :param key: The cache key.
        """
        entry = self._cache.get(key)
        if entry is None or entry[0] < time.monotonic():
            return None
        self._cache.move_to_end(key)
        return entry[1]

    def _put_cached(self, key: tuple[str, ...], value: Any) -> None:
        """
        Remember a result for a while.

        :param key: The cache key.
        :param value: The result.
        """
        self._cache[key] = (time.monotonic() + APPLE_TTL, value)
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHED:
            self._cache.popitem(last=False)

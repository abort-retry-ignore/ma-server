"""
In-memory index of the library items a Subsonic client can see: tracks that have a file.

The index is built with read-only queries on MA's library database and rebuilt shortly after
the library changes. All Subsonic lists, counts and searches are answered from it, so they
agree with each other and stay fast. Blocked tracks ("wrong song") are checked on every
lookup, so a block applies at once.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.helpers import create_uri

from music_assistant.helpers.json import json_loads
from music_assistant.providers.filesystem_local.helpers import get_absolute_path

from .constants import CONTENT_TYPES

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable, Iterable, Mapping

    from music_assistant.mass import MusicAssistant

_NON_WORD = re.compile(r"[^\w]+")


@dataclass(slots=True)
class MusicFolder:
    """A filesystem provider instance shown to clients as a music folder."""

    id: int
    instance_id: str
    name: str
    base_path: str


@dataclass(slots=True)
class IndexedTrack:
    """A library track with a file."""

    id: int
    name: str
    sort_name: str
    version: str
    duration: int
    favorite: bool
    play_count: int
    last_played: int
    timestamp_added: int
    timestamp_modified: int
    folder_id: int
    provider_instance: str
    rel_path: str
    abs_path: str
    suffix: str
    content_type: str
    bit_rate: int
    sample_rate: int
    bit_depth: int
    channels: int
    size: int
    mtime: float
    uris: tuple[str, ...]
    artist_ids: list[int] = field(default_factory=list)
    album_id: int | None = None
    disc_number: int = 0
    track_number: int = 0
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    has_image: bool = False
    loudness: float | None = None
    true_peak: float | None = None
    album_loudness: float | None = None
    search_text: str = ""


@dataclass(slots=True)
class IndexedAlbum:
    """A library album with at least one track that has a file."""

    id: int
    name: str
    sort_name: str
    version: str
    album_type: str
    year: int | None
    favorite: bool
    play_count: int
    last_played: int
    timestamp_added: int
    timestamp_modified: int
    genres: list[str]
    has_image: bool
    artist_ids: list[int] = field(default_factory=list)
    track_ids: list[int] = field(default_factory=list)
    search_text: str = ""


@dataclass(slots=True)
class IndexedArtist:
    """A library artist of a visible album or track."""

    id: int
    name: str
    sort_name: str
    favorite: bool
    play_count: int
    last_played: int
    timestamp_added: int
    timestamp_modified: int
    album_ids: list[int] = field(default_factory=list)
    track_album_ids: list[int] = field(default_factory=list)
    search_text: str = ""


class LibraryIndex:
    """The visible part of MA's library, kept in memory."""

    def __init__(
        self,
        mass: MusicAssistant,
        folders_getter: Callable[[], list[MusicFolder]],
        logger: logging.Logger,
    ) -> None:
        """
        Initialize the index.

        :param mass: The MusicAssistant instance.
        :param folders_getter: Returns the music folders to index.
        :param logger: Logger for the index.
        """
        self.mass = mass
        self.logger = logger
        self._folders_getter = folders_getter
        self.folders: list[MusicFolder] = []
        self.tracks: dict[int, IndexedTrack] = {}
        self.albums: dict[int, IndexedAlbum] = {}
        self.artists: dict[int, IndexedArtist] = {}
        self.track_by_uri: dict[str, int] = {}
        self.last_modified = 0
        self.built_at = 0.0
        self._lock = asyncio.Lock()
        self._built = False

    @property
    def ready(self) -> bool:
        """Return whether the index was built at least once."""
        return self._built

    async def ensure_built(self) -> None:
        """Build the index if it was never built."""
        if not self._built:
            await self.rebuild()

    async def rebuild(self) -> None:
        """Rebuild the index from the library database."""
        async with self._lock:
            started = time.monotonic()
            folders = self._folders_getter()
            tracks, albums, artists = await self._load(folders)
            self.folders = folders
            self.tracks, self.albums, self.artists = tracks, albums, artists
            self.track_by_uri = {uri: t.id for t in tracks.values() for uri in t.uris}
            stamps = [t.timestamp_modified for t in tracks.values()]
            stamps += [a.timestamp_modified for a in albums.values()]
            self.last_modified = max(stamps, default=0) * 1000
            self.built_at = time.time()
            self._built = True
            self.logger.debug(
                "Subsonic index: %s tracks, %s albums, %s artists in %.2f s",
                len(tracks),
                len(albums),
                len(artists),
                time.monotonic() - started,
            )

    def is_visible(self, track: IndexedTrack, folder_id: int | None = None) -> bool:
        """
        Return whether a track can be shown: not blocked and in the requested folder.

        :param track: The track.
        :param folder_id: Only tracks in this music folder, when set.
        """
        if folder_id is not None and track.folder_id != folder_id:
            return False
        return not self.mass.music.play_memory.is_blocked_uri(track.uris)

    def get_track(self, track_id: int) -> IndexedTrack | None:
        """
        Return a visible track.

        :param track_id: The library track id.
        """
        track = self.tracks.get(track_id)
        if track is None or not self.is_visible(track):
            return None
        return track

    def visible_tracks(self, folder_id: int | None = None) -> list[IndexedTrack]:
        """
        Return all visible tracks.

        :param folder_id: Only tracks in this music folder, when set.
        """
        return [t for t in self.tracks.values() if self.is_visible(t, folder_id)]

    def album_tracks(self, album: IndexedAlbum, folder_id: int | None = None) -> list[IndexedTrack]:
        """
        Return an album's visible tracks in disc and track order.

        :param album: The album.
        :param folder_id: Only tracks in this music folder, when set.
        """
        tracks = (self.tracks.get(track_id) for track_id in album.track_ids)
        return [t for t in tracks if t is not None and self.is_visible(t, folder_id)]

    def get_album(self, album_id: int, folder_id: int | None = None) -> IndexedAlbum | None:
        """
        Return an album that has at least one visible track.

        :param album_id: The library album id.
        :param folder_id: Only consider tracks in this music folder, when set.
        """
        album = self.albums.get(album_id)
        if album is None or not self.album_tracks(album, folder_id):
            return None
        return album

    def visible_albums(self, folder_id: int | None = None) -> list[IndexedAlbum]:
        """
        Return all albums with at least one visible track.

        :param folder_id: Only consider tracks in this music folder, when set.
        """
        return [a for a in self.albums.values() if self.album_tracks(a, folder_id)]

    def get_artist(self, artist_id: int) -> IndexedArtist | None:
        """
        Return an artist with at least one visible album or track.

        :param artist_id: The library artist id.
        """
        artist = self.artists.get(artist_id)
        if artist is None or not self.artist_albums(artist):
            return None
        return artist

    def artist_albums(
        self, artist: IndexedArtist, folder_id: int | None = None
    ) -> list[IndexedAlbum]:
        """
        Return the visible albums of an artist, oldest first.

        Albums the artist is album artist of; for an artist who only appears on tracks, the
        albums holding those tracks.

        :param artist: The artist.
        :param folder_id: Only consider tracks in this music folder, when set.
        """
        album_ids = artist.album_ids or artist.track_album_ids
        albums = [self.get_album(album_id, folder_id) for album_id in album_ids]
        result = [a for a in albums if a is not None]
        result.sort(key=lambda a: (a.year or 0, a.sort_name))
        return result

    def album_artists(self, folder_id: int | None = None) -> list[IndexedArtist]:
        """
        Return the artists that are album artist of at least one visible album.

        :param folder_id: Only consider tracks in this music folder, when set.
        """
        result = []
        for artist in self.artists.values():
            if not artist.album_ids:
                continue
            if any(self.get_album(a, folder_id) for a in artist.album_ids):
                result.append(artist)
        return result

    def artist_tracks(self, artist: IndexedArtist) -> list[IndexedTrack]:
        """
        Return the visible tracks the artist performs on.

        :param artist: The artist.
        """
        return [t for t in self.visible_tracks() if artist.id in t.artist_ids]

    def genres(self) -> dict[str, tuple[int, int]]:
        """Return each genre with its number of visible tracks and albums."""
        counts: dict[str, list[int]] = {}
        for track in self.visible_tracks():
            for genre in track.genres:
                counts.setdefault(genre, [0, 0])[0] += 1
        for album in self.visible_albums():
            for genre in album.genres:
                counts.setdefault(genre, [0, 0])[1] += 1
        return {name: (c[0], c[1]) for name, c in counts.items()}

    def search_tracks(self, query: str, folder_id: int | None = None) -> list[IndexedTrack]:
        """
        Return the visible tracks matching every word of the query, best matches first.

        :param query: The search text; empty returns everything.
        :param folder_id: Only tracks in this music folder, when set.
        """
        return _search(self.visible_tracks(folder_id), query)

    def search_albums(self, query: str, folder_id: int | None = None) -> list[IndexedAlbum]:
        """
        Return the visible albums matching every word of the query.

        :param query: The search text; empty returns everything.
        :param folder_id: Only consider tracks in this music folder, when set.
        """
        return _search(self.visible_albums(folder_id), query)

    def search_artists(self, query: str, folder_id: int | None = None) -> list[IndexedArtist]:
        """
        Return the album artists matching every word of the query.

        :param query: The search text; empty returns everything.
        :param folder_id: Only consider tracks in this music folder, when set.
        """
        return _search(self.album_artists(folder_id), query)

    def random_tracks(
        self,
        count: int,
        genre: str | None = None,
        from_year: int | None = None,
        to_year: int | None = None,
        folder_id: int | None = None,
    ) -> list[IndexedTrack]:
        """
        Return random visible tracks.

        :param count: How many tracks.
        :param genre: Only tracks of this genre.
        :param from_year: Only tracks from this year or later.
        :param to_year: Only tracks from this year or earlier.
        :param folder_id: Only tracks in this music folder.
        """
        tracks = self.visible_tracks(folder_id)
        if genre:
            wanted = genre.casefold()
            tracks = [t for t in tracks if any(g.casefold() == wanted for g in t.genres)]
        if from_year is not None:
            tracks = [t for t in tracks if t.year is not None and t.year >= from_year]
        if to_year is not None:
            tracks = [t for t in tracks if t.year is not None and t.year <= to_year]
        return random.sample(tracks, min(count, len(tracks)))

    def similar_tracks(self, seed_ids: Iterable[int], count: int) -> list[IndexedTrack]:
        """
        Return tracks like the seed tracks: same artists first, then the same genres.

        :param seed_ids: Track ids to start from (left out of the result).
        :param count: How many tracks.
        """
        seeds = [t for t in (self.tracks.get(i) for i in seed_ids) if t is not None]
        excluded = {t.id for t in seeds}
        artists = {a for t in seeds for a in t.artist_ids}
        genres = {g.casefold() for t in seeds for g in t.genres}
        same_artist, same_genre = [], []
        for track in self.visible_tracks():
            if track.id in excluded:
                continue
            if artists.intersection(track.artist_ids):
                same_artist.append(track)
            elif genres.intersection(g.casefold() for g in track.genres):
                same_genre.append(track)
        random.shuffle(same_artist)
        random.shuffle(same_genre)
        return (same_artist + same_genre)[:count]

    async def _load(  # noqa: PLR0915
        self, folders: list[MusicFolder]
    ) -> tuple[dict[int, IndexedTrack], dict[int, IndexedAlbum], dict[int, IndexedArtist]]:
        """
        Read the visible library from the database.

        :param folders: The music folders to index.
        """
        db = self.mass.music.database
        by_instance = {f.instance_id: f for f in folders}
        if not by_instance:
            return {}, {}, {}
        rows = await db.get_rows_from_query(
            "SELECT t.item_id, t.name, t.sort_name, t.version, t.duration, t.favorite, "
            "t.metadata, t.play_count, t.last_played, t.timestamp_added, t.timestamp_modified, "
            "pm.provider_instance, pm.provider_item_id, pm.audio_format "
            "FROM tracks t JOIN provider_mappings pm "
            "ON pm.item_id = t.item_id AND pm.media_type = 'track' "
            "WHERE pm.provider_domain LIKE 'filesystem%' AND pm.available = 1 "
            "ORDER BY t.item_id",
            limit=0,
        )
        mappings = await _all_track_mappings(db)
        loudness = await _loudness_by_mapping(db)
        tracks: dict[int, IndexedTrack] = {}
        for row in rows:
            track_id = int(row["item_id"])
            folder = by_instance.get(row["provider_instance"])
            if folder is None or track_id in tracks:
                continue
            track = _track_from_row(row, folder, mappings.get(track_id, []), loudness)
            if track is not None:
                tracks[track_id] = track
        await asyncio.to_thread(_stat_files, list(tracks.values()))
        tracks = {k: t for k, t in tracks.items() if t.size >= 0}

        album_links: dict[int, list[tuple[int, int, int]]] = {}
        for row in await db.get_rows_from_query(
            "SELECT track_id, album_id, disc_number, track_number FROM album_tracks ORDER BY id",
            limit=0,
        ):
            track = tracks.get(int(row["track_id"]))
            if track is None:
                continue
            album_id = int(row["album_id"])
            if track.album_id is None:
                track.album_id = album_id
                track.disc_number = int(row["disc_number"] or 0)
                track.track_number = int(row["track_number"] or 0)
            if track.album_id == album_id:
                album_links.setdefault(album_id, []).append(
                    (track.disc_number, track.track_number, track.id)
                )
        albums = await _load_albums(db, album_links)
        for album in albums.values():
            for track_id in album.track_ids:
                track = tracks[track_id]
                track.year = album.year
                if not track.genres:
                    track.genres = list(album.genres)
            if not album.genres:
                # an album without its own genre takes those of its tracks
                album.genres = list(
                    dict.fromkeys(g for t in album.track_ids for g in tracks[t].genres)
                )
        track_artists: dict[int, list[int]] = {}
        for row in await db.get_rows_from_query(
            "SELECT track_id, artist_id FROM track_artists", limit=0
        ):
            if int(row["track_id"]) in tracks:
                track_artists.setdefault(int(row["track_id"]), []).append(int(row["artist_id"]))
        album_artists: dict[int, list[int]] = {}
        for row in await db.get_rows_from_query(
            "SELECT album_id, artist_id FROM album_artists", limit=0
        ):
            if int(row["album_id"]) in albums:
                album_artists.setdefault(int(row["album_id"]), []).append(int(row["artist_id"]))
        artists = await _load_artists(
            db, set().union(*track_artists.values(), *album_artists.values())
        )
        for track_id, artist_ids in track_artists.items():
            track = tracks[track_id]
            track.artist_ids = [a for a in artist_ids if a in artists]
            for artist_id in track.artist_ids:
                if track.album_id is not None:
                    _append_unique(artists[artist_id].track_album_ids, track.album_id)
        for album_id, artist_ids in album_artists.items():
            album = albums[album_id]
            album.artist_ids = [a for a in artist_ids if a in artists]
            for artist_id in album.artist_ids:
                _append_unique(artists[artist_id].album_ids, album_id)
        for album in albums.values():
            if not album.artist_ids and album.track_ids:
                album.artist_ids = list(tracks[album.track_ids[0]].artist_ids)
            names = " ".join(artists[a].name for a in album.artist_ids)
            album.search_text = normalize(f"{album.name} {names}")
        for track in tracks.values():
            names = " ".join(artists[a].name for a in track.artist_ids)
            album_name = albums[track.album_id].name if track.album_id in albums else ""
            track.search_text = normalize(f"{track.name} {names} {album_name}")
        for artist in artists.values():
            artist.search_text = normalize(artist.name)
        return tracks, albums, artists


def normalize(text: str) -> str:
    """
    Return text folded for matching: lower case, no accents, words separated by single spaces.

    :param text: The text.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_NON_WORD.sub(" ", stripped).split())


def index_letter(sort_name: str) -> str:
    """
    Return the index group of a name: its first letter, or "#".

    :param sort_name: The name used for sorting.
    """
    first = normalize(sort_name)[:1].upper()
    return first if first.isalpha() else "#"


def _search[T: (IndexedTrack, IndexedAlbum, IndexedArtist)](items: list[T], query: str) -> list[T]:
    """
    Return the items whose words start with every word of the query.

    :param items: The items to search.
    :param query: The search text; empty (or just quotes) returns every item.
    """
    words = normalize(query.replace('"', " ").replace("*", " ")).split()
    if not words:
        return sorted(items, key=lambda i: (i.sort_name, i.id))
    scored: list[tuple[int, str, int, T]] = []
    for item in items:
        item_words = item.search_text.split()
        if all(any(w.startswith(q) for w in item_words) for q in words):
            # a name that starts with the query ranks above one that only contains it
            name = normalize(item.name)
            rank = 0 if name == " ".join(words) else 1 if name.startswith(words[0]) else 2
            scored.append((rank, item.sort_name, item.id, item))
    scored.sort(key=lambda s: (s[0], s[1], s[2]))
    return [s[3] for s in scored]


def _append_unique(values: list[int], value: int) -> None:
    """
    Append a value to a list when it is not in it yet.

    :param values: The list.
    :param value: The value.
    """
    if value not in values:
        values.append(value)


def _track_from_row(
    row: Mapping[str, Any],
    folder: MusicFolder,
    mappings: list[tuple[str, str, str]],
    loudness: dict[tuple[str, str], tuple[float | None, float | None, float | None]],
) -> IndexedTrack | None:
    """
    Return the indexed track for a track row joined with its file mapping.

    :param row: The database row.
    :param folder: The music folder the file is in.
    :param mappings: All (domain, instance, item id) mappings of the track.
    :param loudness: Loudness measurements by (item id, provider).
    """
    rel_path = str(row["provider_item_id"])
    try:
        abs_path = get_absolute_path(folder.base_path, rel_path)
    except MediaNotFoundError:
        return None
    suffix = Path(rel_path).suffix.lstrip(".").lower()
    if not suffix or suffix == "cue" or "#" in suffix:
        # tracks inside a CUE sheet are slices of a bigger file
        return None
    audio_format = _json(row["audio_format"])
    metadata = _json(row["metadata"])
    track_id = int(row["item_id"])
    uris = [create_uri(MediaType.TRACK, "library", str(track_id))]
    measured: tuple[float | None, float | None, float | None] = (None, None, None)
    for domain, instance, item_id in mappings:
        uris.append(create_uri(MediaType.TRACK, instance, item_id))
        uris.append(create_uri(MediaType.TRACK, domain, item_id))
        if measured[0] is None:
            measured = (
                loudness.get((item_id, instance))
                or loudness.get((item_id, domain))
                or (
                    None,
                    None,
                    None,
                )
            )
    return IndexedTrack(
        id=track_id,
        name=str(row["name"]),
        sort_name=str(row["sort_name"] or row["name"]).casefold(),
        version=str(row["version"] or ""),
        duration=int(row["duration"] or 0),
        favorite=bool(row["favorite"]),
        play_count=int(row["play_count"] or 0),
        last_played=int(row["last_played"] or 0),
        timestamp_added=int(row["timestamp_added"] or 0),
        timestamp_modified=int(row["timestamp_modified"] or 0),
        folder_id=folder.id,
        provider_instance=folder.instance_id,
        rel_path=rel_path,
        abs_path=abs_path,
        suffix=suffix,
        content_type=CONTENT_TYPES.get(suffix, f"audio/{suffix}"),
        bit_rate=int(audio_format.get("bit_rate") or 0),
        sample_rate=int(audio_format.get("sample_rate") or 0),
        bit_depth=int(audio_format.get("bit_depth") or 0),
        channels=int(audio_format.get("channels") or 0),
        size=0,
        mtime=0.0,
        uris=tuple(dict.fromkeys(uris)),
        genres=list(metadata.get("genres") or []),
        has_image=bool(metadata.get("images")),
        loudness=measured[0],
        true_peak=measured[1],
        album_loudness=measured[2],
    )


def _stat_files(tracks: list[IndexedTrack]) -> None:
    """
    Fill in each track's file size and modification time; size -1 marks a missing file.

    :param tracks: The tracks.
    """
    for track in tracks:
        try:
            stat = Path(track.abs_path).stat()
        except OSError:
            track.size = -1
            continue
        track.size = stat.st_size
        track.mtime = stat.st_mtime


async def _all_track_mappings(db: Any) -> dict[int, list[tuple[str, str, str]]]:
    """
    Return every provider mapping of every library track.

    :param db: The library database.
    """
    result: dict[int, list[tuple[str, str, str]]] = {}
    for row in await db.get_rows_from_query(
        "SELECT item_id, provider_domain, provider_instance, provider_item_id "
        "FROM provider_mappings WHERE media_type = 'track'",
        limit=0,
    ):
        result.setdefault(int(row["item_id"]), []).append(
            (row["provider_domain"], row["provider_instance"], row["provider_item_id"])
        )
    return result


async def _loudness_by_mapping(
    db: Any,
) -> dict[tuple[str, str], tuple[float | None, float | None, float | None]]:
    """
    Return the loudness measurements of tracks by (provider item id, provider).

    :param db: The library database.
    """
    result: dict[tuple[str, str], tuple[float | None, float | None, float | None]] = {}
    for row in await db.get_rows_from_query(
        "SELECT item_id, provider, analysis_data FROM audio_analysis "
        "WHERE aa_provider_domain = 'loudness_analysis' AND media_type = 'track'",
        limit=0,
    ):
        data = _json(row["analysis_data"])
        integrated = data.get("loudness_integrated")
        if integrated is None:
            continue
        result[(str(row["item_id"]), str(row["provider"]))] = (
            float(integrated),
            _float_or_none(data.get("true_peak")),
            _float_or_none(data.get("loudness_album")),
        )
    return result


async def _load_albums(
    db: Any, album_links: dict[int, list[tuple[int, int, int]]]
) -> dict[int, IndexedAlbum]:
    """
    Return the albums that hold visible tracks.

    :param db: The library database.
    :param album_links: (disc, track number, track id) per album id.
    """
    albums: dict[int, IndexedAlbum] = {}
    if not album_links:
        return albums
    for row in await db.get_rows_from_query(
        "SELECT item_id, name, sort_name, version, album_type, year, favorite, metadata, "
        "play_count, last_played, timestamp_added, timestamp_modified FROM albums",
        limit=0,
    ):
        album_id = int(row["item_id"])
        links = album_links.get(album_id)
        if not links:
            continue
        metadata = _json(row["metadata"])
        albums[album_id] = IndexedAlbum(
            id=album_id,
            name=str(row["name"]),
            sort_name=str(row["sort_name"] or row["name"]).casefold(),
            version=str(row["version"] or ""),
            album_type=str(row["album_type"] or ""),
            year=int(row["year"]) if row["year"] else None,
            favorite=bool(row["favorite"]),
            play_count=int(row["play_count"] or 0),
            last_played=int(row["last_played"] or 0),
            timestamp_added=int(row["timestamp_added"] or 0),
            timestamp_modified=int(row["timestamp_modified"] or 0),
            genres=list(metadata.get("genres") or []),
            has_image=bool(metadata.get("images")),
            track_ids=[link[2] for link in sorted(links)],
        )
    return albums


async def _load_artists(db: Any, artist_ids: set[int]) -> dict[int, IndexedArtist]:
    """
    Return the given artists.

    :param db: The library database.
    :param artist_ids: The library artist ids to load.
    """
    artists: dict[int, IndexedArtist] = {}
    if not artist_ids:
        return artists
    for row in await db.get_rows_from_query(
        "SELECT item_id, name, sort_name, favorite, play_count, last_played, "
        "timestamp_added, timestamp_modified FROM artists",
        limit=0,
    ):
        artist_id = int(row["item_id"])
        if artist_id not in artist_ids:
            continue
        artists[artist_id] = IndexedArtist(
            id=artist_id,
            name=str(row["name"]),
            sort_name=str(row["sort_name"] or row["name"]).casefold(),
            favorite=bool(row["favorite"]),
            play_count=int(row["play_count"] or 0),
            last_played=int(row["last_played"] or 0),
            timestamp_added=int(row["timestamp_added"] or 0),
            timestamp_modified=int(row["timestamp_modified"] or 0),
        )
    return artists


def _json(value: Any) -> dict[str, Any]:
    """
    Return a JSON column as a dict (empty when missing or not an object).

    :param value: The column value.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = json_loads(value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _float_or_none(value: Any) -> float | None:
    """
    Return a float, or None for a missing value.

    :param value: The value.
    """
    return None if value is None else float(value)

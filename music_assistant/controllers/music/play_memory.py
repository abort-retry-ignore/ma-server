"""
Play memory: a lasting record of the tracks that were listened to for a while.

Voice requests often reach the search misheard ("anvil of chrome" for "Anvil of Crom"), and
streaming services carry look-alike tracks named exactly like those mishearings. A search
puts a previously heard track whose name is a close variant of the query at the top of the
track results, and leaves out tracks that someone marked as wrong.

The memory keeps its own database file next to the library database: the library schema and
its migrations stay untouched, and the playlog's 90-day pruning does not apply to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from mashumaro.exceptions import InvalidFieldValue, MissingField
from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import InvalidCommand, MediaNotFoundError, MusicAssistantError
from music_assistant_models.helpers import create_uri
from music_assistant_models.media_items import SearchResults, Track

from music_assistant.constants import DB_TABLE_PLAYLOG, MASS_LOGGER_NAME
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.util import parse_title_and_version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from music_assistant_models.media_items import ItemMapping
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.mass import MusicAssistant

DB_FILE = "play_memory.db"
TABLE_HEARD = "heard_tracks"
TABLE_BLOCKED = "blocked_tracks"
TABLE_META = "meta"
# bumped when the rule for remembering a track changes, so the playlog is read once more
META_SEEDED = "seeded_from_playlog_min_60s"

# a track counts as heard once it played this long (or to the end, when it is shorter):
# a look-alike is usually noticed and skipped well before that
MIN_SECONDS_HEARD = 60

# a heard track must be at least this similar to the query to be put on top
MATCH_THRESHOLD = 0.85
# below this length a one-letter difference changes the meaning ("hurt" vs "hurts"),
# so shorter strings only count when they match exactly
MIN_FUZZY_LENGTH = 8
MAX_PROMOTED_TRACKS = 3
# a track reported again within this window is the same listen (e.g. paused, then finished)
REPLAY_WINDOW_SECONDS = 600
# words that searches add around a title and that say nothing about which song is meant
NOISE_WORDS = frozenset({"by", "official", "audio", "remaster", "remastered", "song", "version"})
TRACK_NUMBER_PREFIX = re.compile(r"^\s*\d{1,3}\s*[.)_-]\s*")
BRACKETED = re.compile(r"\s*[(\[{][^)\]}]*[)\]}]")


@dataclass
class HeardTrack:
    """A track that was listened to for at least a minute, or to the end."""

    uri: str
    name: str
    artists: str
    plays: int
    last_played: int
    item_json: str | None
    match_keys: tuple[str, ...]


class PlayMemory:
    """Remembers tracks that were listened to and uses them to correct near-miss searches."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the play memory.

        :param mass: The Music Assistant instance.
        """
        self.mass = mass
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.music.play_memory")
        self._db: DatabaseConnection | None = None
        self._heard: dict[str, HeardTrack] = {}
        self._blocked: dict[str, dict[str, Any]] = {}
        self._unregister_commands: list[Callable[[], None]] = []

    async def setup(self) -> None:
        """Open the database, load the memory and register the API commands."""
        db = DatabaseConnection(os.path.join(self.mass.storage_path, DB_FILE))
        await db.setup(cache_size_kib=2048, mmap_size_bytes=0)
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_HEARD} ("
            "uri TEXT PRIMARY KEY, name TEXT NOT NULL, artists TEXT NOT NULL DEFAULT '', "
            "plays INTEGER NOT NULL DEFAULT 1, first_played INTEGER NOT NULL, "
            "last_played INTEGER NOT NULL, item_json TEXT)"
        )
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_BLOCKED} ("
            "uri TEXT PRIMARY KEY, track_uri TEXT NOT NULL, name TEXT NOT NULL, "
            "artists TEXT NOT NULL DEFAULT '', blocked_at INTEGER NOT NULL)"
        )
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_META} (key TEXT PRIMARY KEY, value TEXT)"
        )
        await db.commit()
        self._db = db
        for row in await db.get_rows_from_query(f"SELECT * FROM {TABLE_HEARD}", limit=0):
            self._heard[row["uri"]] = _heard_from_row(row)
        for row in await db.get_rows_from_query(f"SELECT * FROM {TABLE_BLOCKED}", limit=0):
            self._blocked[row["uri"]] = dict(row)
        self._register_api_commands()
        if not await db.get_row(TABLE_META, {"key": META_SEEDED}):
            self.mass.create_task(self._seed_from_playlog())
        self.logger.debug(
            "Loaded %s heard and %s blocked tracks", len(self._heard), len(self._blocked)
        )

    async def close(self) -> None:
        """Unregister the API commands and close the database."""
        for unregister in self._unregister_commands:
            unregister()
        self._unregister_commands = []
        if self._db:
            await self._db.close()
            self._db = None

    async def remember(self, track: Track, *, seconds_played: int, fully_played: bool) -> None:
        """
        Record a play of a track; it is kept once it played long enough.

        :param track: The track that was played.
        :param seconds_played: How long the track played.
        :param fully_played: Whether the track played to the end.
        """
        if not fully_played and seconds_played < MIN_SECONDS_HEARD:
            return
        if self._db is None or self.is_blocked(track):
            return
        track = await self._prefer_library_item(track)
        if track.uri is None:
            return
        now = int(utc_timestamp())
        existing = self._heard.get(track.uri)
        if existing and now - existing.last_played < REPLAY_WINDOW_SECONDS:
            return
        plays = existing.plays + 1 if existing else 1
        # a library track is fetched from the library database when it is needed; a streaming
        # track is kept as it was played, so a later search never has to ask the service again
        item_json = None if track.provider == "library" else json_dumps(track.to_dict())
        row = {
            "uri": track.uri,
            "name": track.name,
            "artists": _artist_names(track.artists),
            "plays": plays,
            "first_played": now,
            "last_played": now,
            "item_json": item_json,
        }
        if existing:
            await self._db.execute_write(
                f"UPDATE {TABLE_HEARD} SET name = :name, artists = :artists, plays = :plays, "
                "last_played = :last_played, item_json = :item_json WHERE uri = :uri",
                {k: v for k, v in row.items() if k != "first_played"},
            )
        else:
            await self._db.insert_or_replace(TABLE_HEARD, row)
        self._heard[track.uri] = _heard_from_row(row)
        self.logger.debug("Remembered '%s' (%s plays)", track.name, plays)

    def is_blocked(self, item: Track | ItemMapping) -> bool:
        """
        Return whether a track (or any of its provider versions) was marked as wrong.

        :param item: The track to check.
        """
        return self.is_blocked_uri(_item_uris(item))

    def is_blocked_uri(self, uris: Iterable[str]) -> bool:
        """
        Return whether any of the given track uris was marked as wrong.

        :param uris: The uris one track is known by (library and provider uris).
        """
        if not self._blocked:
            return False
        return any(uri in self._blocked for uri in uris)

    async def heard_uris_matching(self, search_query: str) -> list[str]:
        """
        Return the uris of heard tracks whose name is a close variant of the query, best first.

        :param search_query: The search query.
        """
        return [heard.uri for heard in await self._match(search_query)]

    async def adjust_search_results(
        self,
        search_query: str,
        media_types: Sequence[MediaType],
        limit: int,
        results: SearchResults,
    ) -> SearchResults:
        """
        Return the results with blocked tracks left out and close heard matches on top.

        :param search_query: The query the results were found for.
        :param media_types: The media types the search was asked for.
        :param limit: The maximum number of tracks to return.
        :param results: The search results to adjust (not modified).
        """
        if self._db is None or MediaType.TRACK not in media_types:
            return results
        tracks = [t for t in results.tracks if not self.is_blocked(t)]
        promoted: list[Track | ItemMapping] = []
        for heard in await self._match(search_query):
            found = next((t for t in tracks if heard.uri in _item_uris(t)), None)
            if found is not None:
                tracks.remove(found)
            else:
                found = await self._load_track(heard)
            if found is not None and not self.is_blocked(found):
                promoted.append(found)
        if not promoted and len(tracks) == len(results.tracks):
            return results
        if promoted:
            self.logger.debug(
                "Search '%s': putting heard tracks on top: %s",
                search_query,
                ", ".join(f"'{t.name}'" for t in promoted),
            )
        return replace(results, tracks=(promoted + tracks)[:limit])

    async def block_track(self, track: Track) -> list[str]:
        """
        Mark a track as wrong: leave it out of searches and forget that it was heard.

        :param track: The track to block.
        """
        assert self._db is not None
        assert track.uri is not None
        uris = set(_item_uris(track))
        library_track = await self._prefer_library_item(track)
        uris.update(_item_uris(library_track))
        now = int(utc_timestamp())
        artists = _artist_names(track.artists)
        for uri in sorted(uris):
            row = {
                "uri": uri,
                "track_uri": track.uri,
                "name": track.name,
                "artists": artists,
                "blocked_at": now,
            }
            await self._db.insert_or_replace(TABLE_BLOCKED, row)
            self._blocked[uri] = row
            if self._heard.pop(uri, None):
                await self._db.delete(TABLE_HEARD, {"uri": uri})
        self.logger.info("Blocked '%s' by %s (%s)", track.name, artists, ", ".join(sorted(uris)))
        return sorted(uris)

    async def block_current(self, queue_id: str | None = None) -> dict[str, Any]:
        """
        Block the track that is playing now and move on to the next track (or stop).

        :param queue_id: The queue (or player) to act on. Omit it when only one queue plays.
        """
        queue = self._get_queue(queue_id)
        queue_item = queue.current_item
        track = queue_item.media_item if queue_item else None
        if queue_item is None or not isinstance(track, Track):
            raise InvalidCommand(f"{queue.display_name} is not playing a track")
        uris = await self.block_track(track)
        await self.mass.player_queues.next(queue.queue_id)
        refreshed = self.mass.player_queues.get(queue.queue_id)
        if (
            refreshed
            and refreshed.current_item
            and refreshed.current_item.queue_item_id == queue_item.queue_item_id
        ):
            # nothing after it in the queue
            await self.mass.player_queues.stop(queue.queue_id)
        return {
            "queue_id": queue.queue_id,
            "name": track.name,
            "artists": _artist_names(track.artists),
            "blocked_uris": uris,
        }

    async def block(self, uri: str) -> list[str]:
        """
        Block a track by its uri.

        :param uri: The uri of the track to block.
        """
        item = await self.mass.music.get_item_by_uri(uri)
        if not isinstance(item, Track):
            raise InvalidCommand(f"{uri} is not a track")
        return await self.block_track(item)

    async def unblock(self, uri: str) -> list[str]:
        """
        Undo a block: the track shows up in searches again.

        :param uri: Any uri of the blocked track.
        """
        assert self._db is not None
        if not (entry := self._blocked.get(uri)):
            raise MediaNotFoundError(f"{uri} is not blocked")
        # a block covers every version of the track, all recorded under the same track_uri
        uris = sorted(
            u for u, row in self._blocked.items() if row["track_uri"] == entry["track_uri"]
        )
        for blocked_uri in uris:
            self._blocked.pop(blocked_uri, None)
            await self._db.delete(TABLE_BLOCKED, {"uri": blocked_uri})
        self.logger.info("Unblocked '%s' (%s)", entry["name"], ", ".join(uris))
        return uris

    async def list_entries(self) -> dict[str, list[dict[str, Any]]]:
        """Return the heard and blocked tracks, most recent first."""
        heard = sorted(self._heard.values(), key=lambda h: h.last_played, reverse=True)
        blocked = sorted(self._blocked.values(), key=lambda b: b["blocked_at"], reverse=True)
        return {
            "heard": [
                {
                    "uri": h.uri,
                    "name": h.name,
                    "artists": h.artists,
                    "plays": h.plays,
                    "last_played": h.last_played,
                }
                for h in heard
            ],
            "blocked": [dict(b) for b in blocked],
        }

    def _register_api_commands(self) -> None:
        """Register the API commands, so a voice assistant can mark a wrong track."""
        commands: tuple[tuple[str, Callable[..., Any], Scope], ...] = (
            ("music/play_memory/block_current", self.block_current, Scope.QUEUES_CONTROL),
            ("music/play_memory/block", self.block, Scope.LIBRARY_WRITE),
            ("music/play_memory/unblock", self.unblock, Scope.LIBRARY_WRITE),
            ("music/play_memory/list", self.list_entries, Scope.LIBRARY_READ),
        )
        for command, handler, scope in commands:
            self._unregister_commands.append(
                self.mass.register_api_command(command, handler, required_scope=scope)
            )

    def _get_queue(self, queue_id: str | None) -> PlayerQueue:
        """
        Return the queue to act on.

        :param queue_id: A queue or player id, or None for the one queue that is playing.
        """
        if queue_id:
            queue = self.mass.player_queues.get(
                queue_id
            ) or self.mass.player_queues.get_active_queue(queue_id)
            if queue is None:
                raise InvalidCommand(f"No queue found for {queue_id}")
            return queue
        playing = [q for q in self.mass.player_queues.all() if q.state == PlaybackState.PLAYING]
        if len(playing) == 1:
            return playing[0]
        if not playing:
            raise InvalidCommand("Nothing is playing")
        names = ", ".join(q.display_name for q in playing)
        raise InvalidCommand(f"Several queues are playing ({names}), pass a queue_id")

    async def _match(self, search_query: str) -> list[HeardTrack]:
        """
        Return the heard tracks whose name is a close variant of the query, best first.

        :param search_query: The search query.
        """
        query = _normalize(search_query)
        if not query or not self._heard:
            return []
        candidates = tuple(self._heard.values())
        return await asyncio.to_thread(_rank_matches, query, candidates)

    async def _load_track(self, heard: HeardTrack) -> Track | None:
        """
        Return the full track for a heard entry, without asking a streaming service if we can.

        :param heard: The heard entry.
        """
        if heard.item_json:
            try:
                return Track.from_dict(json_loads(heard.item_json))
            except MissingField, InvalidFieldValue, ValueError, TypeError, KeyError:
                # stored by an older version of the model: fetch it again below
                heard.item_json = None
        try:
            item = await self.mass.music.get_item_by_uri(heard.uri)
        except MusicAssistantError as err:
            # a provider that is offline must not break the search
            self.logger.debug("Could not load heard track %s: %s", heard.uri, err)
            return None
        if not isinstance(item, Track):
            return None
        if item.provider != "library" and self._db is not None:
            # seeded from the playlog without the full track: keep it now we have it
            heard.item_json = json_dumps(item.to_dict())
            await self._db.update(TABLE_HEARD, {"uri": heard.uri}, {"item_json": heard.item_json})
        return item

    async def _prefer_library_item(self, track: Track) -> Track:
        """
        Return the library version of a track if it has one, else the track itself.

        :param track: The track as it was played.
        """
        if track.provider == "library":
            return track
        library_track = await self.mass.music.tracks.get_library_item_by_prov_id(
            track.item_id, track.provider
        )
        return library_track or track

    async def _seed_from_playlog(self) -> None:
        """Add the tracks in the playlog that played long enough to count as heard."""
        assert self._db is not None
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id, provider, name, artists, MAX(timestamp) AS last_played "
            f"FROM {DB_TABLE_PLAYLOG} WHERE media_type = 'track' "
            f"AND (fully_played = 1 OR seconds_played >= {MIN_SECONDS_HEARD}) "
            "GROUP BY item_id, provider",
            limit=0,
        )
        added = 0
        for row in rows:
            uri = create_uri(MediaType.TRACK, row["provider"], row["item_id"])
            if row["provider"] != "library":
                library_track = await self.mass.music.tracks.get_library_item_by_prov_id(
                    row["item_id"], row["provider"]
                )
                if library_track and library_track.uri:
                    uri = library_track.uri
            if uri in self._heard or uri in self._blocked:
                continue
            artists = (
                " / ".join(a["name"] for a in json_loads(row["artists"]) if a.get("name"))
                if row["artists"]
                else ""
            )
            entry = {
                "uri": uri,
                "name": row["name"],
                "artists": artists,
                "plays": 1,
                "first_played": int(row["last_played"]),
                "last_played": int(row["last_played"]),
                "item_json": None,
            }
            # a play reported while seeding may already have added the track
            await self._db.execute_write(
                f"INSERT OR IGNORE INTO {TABLE_HEARD} "
                "(uri, name, artists, plays, first_played, last_played, item_json) VALUES "
                "(:uri, :name, :artists, :plays, :first_played, :last_played, :item_json)",
                entry,
            )
            self._heard.setdefault(uri, _heard_from_row(entry))
            added += 1
        await self._db.insert_or_replace(
            TABLE_META, {"key": META_SEEDED, "value": str(int(utc_timestamp()))}
        )
        self.logger.info("Play memory: added %s heard tracks from the playlog", added)


def _rank_matches(query: str, candidates: Iterable[HeardTrack]) -> list[HeardTrack]:
    """
    Return the candidates that match the normalized query closely enough, best first.

    :param query: The normalized search query.
    :param candidates: The heard tracks to compare against.
    """
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(query)
    scored: list[tuple[float, int, int, HeardTrack]] = []
    for heard in candidates:
        best = 0.0
        for key in heard.match_keys:
            if key == query:
                best = 1.0
                break
            if min(len(key), len(query)) < MIN_FUZZY_LENGTH:
                continue
            matcher.set_seq1(key)
            if matcher.real_quick_ratio() < MATCH_THRESHOLD:
                continue
            if matcher.quick_ratio() < MATCH_THRESHOLD:
                continue
            best = max(best, matcher.ratio())
        if best >= MATCH_THRESHOLD:
            scored.append((best, heard.plays, heard.last_played, heard))
    scored.sort(key=lambda entry: entry[:3], reverse=True)
    return [entry[3] for entry in scored[:MAX_PROMOTED_TRACKS]]


def _heard_from_row(row: Any) -> HeardTrack:
    """
    Build a heard entry, with its match keys, from a database row.

    :param row: The heard_tracks row (a mapping).
    """
    name = row["name"]
    artists = [a for a in row["artists"].split(" / ") if a][:2]
    base_title = parse_title_and_version(name, strip_for_search=True)[0]
    # uploads and file names wrap the title: "01. Anvil Of Crom [Prologue] (Conan Soundtrack)"
    bare_title = TRACK_NUMBER_PREFIX.sub("", BRACKETED.sub("", base_title)).strip()
    titles = {
        _normalize(name),
        _normalize(base_title),
        _normalize(bare_title),
    }
    titles.discard("")
    keys = set(titles)
    for title in titles:
        for artist in artists:
            normalized_artist = _normalize(artist)
            keys.add(f"{title} {normalized_artist}")
            keys.add(f"{normalized_artist} {title}")
    return HeardTrack(
        uri=row["uri"],
        name=name,
        artists=row["artists"],
        plays=int(row["plays"]),
        last_played=int(row["last_played"]),
        item_json=row["item_json"],
        match_keys=tuple(sorted(keys)),
    )


def _normalize(text: str) -> str:
    """
    Return text lowercased, without accents, punctuation and noise words.

    :param text: The text to normalize.
    """
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in NOISE_WORDS)


def _artist_names(artists: Iterable[Any]) -> str:
    """
    Return the artist names of a track joined with ' / '.

    :param artists: The artists (or artist mappings) of the track.
    """
    return " / ".join(a.name for a in artists if getattr(a, "name", None))


def _item_uris(item: Track | ItemMapping) -> list[str]:
    """
    Return every uri a track is known by: its own and those of its provider versions.

    :param item: The track.
    """
    uris = [item.uri] if item.uri else []
    for mapping in getattr(item, "provider_mappings", ()):
        uris.append(create_uri(MediaType.TRACK, mapping.provider_instance, mapping.item_id))
        uris.append(create_uri(MediaType.TRACK, mapping.provider_domain, mapping.item_id))
    return uris

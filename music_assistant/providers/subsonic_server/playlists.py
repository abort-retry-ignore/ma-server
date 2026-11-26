"""
The playlists Subsonic clients see: MA's own playlists and the Apple Music playlists in the library.

Songs of a playlist are shown when they can be played: songs with a file, and Apple Music songs
(saved on first play) while Apple Music is enabled. Listing all playlists must not fetch every
playlist's songs, so the song counts come from a small in-memory cache that a background task
fills slowly, one playlist at a time; opening a playlist always reads it fresh.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Track

from .constants import (
    APPLE_DOMAIN,
    ERR_NOT_FOUND,
    PLAYLIST_COUNT_INTERVAL,
    PLAYLIST_COUNT_MAX_AGE,
    PLAYLIST_COUNT_PAUSE,
    PLAYLIST_COUNT_START_DELAY,
)
from .mapping import PREFIX_PLAYLIST, parse_id
from .responses import SubsonicError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .index import IndexedTrack
    from .server import SubsonicServer

BUILTIN = "builtin"


class PlaylistBook:
    """Finds playlists, reads their playable songs and keeps their song counts."""

    def __init__(self, server: SubsonicServer) -> None:
        """
        Initialize.

        :param server: The Subsonic server.
        """
        self.server = server
        self.mass = server.mass
        self.logger = server.logger
        # playlist id -> (song count, duration, playlist's timestamp_modified, when counted)
        self._counts: dict[int, tuple[int, int, int, float]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start counting songs in the background."""
        if self._task is None:
            self._task = asyncio.create_task(self._count_loop())

    async def stop(self) -> None:
        """Stop the background counting."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def rows(self) -> list[Mapping[str, Any]]:
        """Return the playlists to show: MA's own, and Apple Music ones still in the library."""
        rows = await self.mass.music.database.get_rows_from_query(
            "SELECT p.item_id, p.name, p.is_editable, p.timestamp_added, p.timestamp_modified, "
            "pm.provider_domain FROM playlists p JOIN provider_mappings pm "
            "ON pm.item_id = p.item_id AND pm.media_type = 'playlist' "
            "WHERE pm.provider_domain = :builtin "
            "OR (pm.provider_domain = :apple AND pm.in_library = 1) "
            "ORDER BY p.sort_name",
            {"builtin": BUILTIN, "apple": APPLE_DOMAIN},
            limit=0,
        )
        apple = self.server.catalog.enabled
        result: dict[int, Mapping[str, Any]] = {}
        for row in rows:
            playlist_id = int(row["item_id"])
            if playlist_id in result or (row["provider_domain"] != BUILTIN and not apple):
                continue
            result[playlist_id] = row
        return list(result.values())

    async def row(self, value: str) -> Mapping[str, Any]:
        """
        Return the database row of a playlist that is shown.

        :param value: The Subsonic playlist ID.
        :raises SubsonicError: Not found (70).
        """
        _, playlist_id = parse_id(value, PREFIX_PLAYLIST)
        for row in await self.rows():
            if int(row["item_id"]) == playlist_id:
                return row
        raise SubsonicError(ERR_NOT_FOUND, "Playlist not found")

    async def items(
        self, playlist_id: int, refresh: bool, everything: bool = False
    ) -> list[tuple[int, IndexedTrack | Track | None]]:
        """
        Return (MA position, playable song) for a playlist, and remember its song count.

        :param playlist_id: The library playlist id.
        :param refresh: Read the playlist fresh instead of from MA's cache.
        :param everything: Also return songs that can't be played (song None), for positions.
        """
        result: list[tuple[int, IndexedTrack | Track | None]] = []
        async for item in self.mass.music.playlists.tracks(
            str(playlist_id), "library", force_refresh=refresh
        ):
            song = self._playable(item)
            position = int(getattr(item, "position", 0) or len(result) + 1)
            if song is not None or everything:
                result.append((position, song))
        songs = [s for _, s in result if s is not None]
        stamp = await self._stamp(playlist_id)
        self._counts[playlist_id] = (
            len(songs),
            sum(int(s.duration or 0) for s in songs),
            stamp,
            time.monotonic(),
        )
        return result

    def count(self, row: Mapping[str, Any]) -> tuple[int, int] | None:
        """
        Return the known (song count, duration) of a playlist, or None when not counted yet.

        :param row: The playlist's database row.
        """
        counted = self._counts.get(int(row["item_id"]))
        if counted is None:
            return None
        return counted[0], counted[1]

    def _playable(self, item: Any) -> IndexedTrack | Track | None:
        """
        Return the song to show for a playlist item, or None when it can't be played here.

        :param item: The playlist item from MA.
        """
        index = self.server.index
        uris = [item.uri or ""]
        for mapping in getattr(item, "provider_mappings", ()):
            uris.append(f"{mapping.provider_instance}://track/{mapping.item_id}")
            uris.append(f"{mapping.provider_domain}://track/{mapping.item_id}")
        for uri in uris:
            if (track_id := index.track_by_uri.get(uri)) is not None:
                return index.get_track(track_id)
        catalog = self.server.catalog
        if catalog.enabled and isinstance(item, Track):
            apple_id = catalog.apple_track_id(item)
            if apple_id is not None and not catalog.is_blocked(apple_id):
                return item
        return None

    async def _stamp(self, playlist_id: int) -> int:
        """
        Return a playlist's last-modified time in the library.

        :param playlist_id: The library playlist id.
        """
        row = await self.mass.music.database.get_row("playlists", {"item_id": playlist_id})
        return int(row["timestamp_modified"] or 0) if row else 0

    async def _count_loop(self) -> None:
        """Count the songs of playlists that have no fresh count, slowly, now and then."""
        await asyncio.sleep(PLAYLIST_COUNT_START_DELAY)
        while True:
            counted = 0
            for row in await self.rows():
                playlist_id = int(row["item_id"])
                known = self._counts.get(playlist_id)
                stamp = int(row["timestamp_modified"] or 0)
                if (
                    known is not None
                    and known[2] == stamp
                    and time.monotonic() - known[3] < PLAYLIST_COUNT_MAX_AGE
                ):
                    continue
                try:
                    await self.items(playlist_id, refresh=False)
                    counted += 1
                except MusicAssistantError as err:
                    self.logger.debug("Could not count playlist %s: %s", row["name"], err)
                await asyncio.sleep(PLAYLIST_COUNT_PAUSE)
            if counted:
                self.logger.info("Counted the songs of %s playlists for Subsonic clients", counted)
            await asyncio.sleep(PLAYLIST_COUNT_INTERVAL)

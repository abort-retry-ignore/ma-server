"""
The playlists Subsonic clients see: MA's own playlists and the Apple Music playlists in the library.

Songs of a playlist are shown when they can be played: songs with a file, and Apple Music songs
(saved on first play) while Apple Music is enabled. Listing all playlists must not fetch every
playlist's songs, so the song counts come from a small in-memory cache that a background task
fills slowly, one playlist at a time; opening a playlist always reads it fresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Track

from .constants import (
    APPLE_DOMAIN,
    ERR_GENERIC,
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
        # playlist id -> saved state: signature, song_count, duration, changed, counted, stamp
        self._states: dict[int, dict[str, Any]] = {}
        self._edited: set[int] = set()
        self._task: asyncio.Task[None] | None = None

    def mark_edited(self, playlist_id: int) -> None:
        """
        Note that a playlist was changed, so the next read skips MA's cache.

        :param playlist_id: The library playlist id.
        """
        self._edited.add(playlist_id)

    def needs_fresh_read(self, row: Mapping[str, Any]) -> bool:
        """
        Return whether to read a playlist fresh rather than from MA's cache.

        MA's own playlists are local and cheap to read; an Apple Music playlist is only read
        fresh after it was changed from a Subsonic app, so that apps syncing every playlist
        don't send a request to Apple for each one.

        :param row: The playlist's database row.
        """
        playlist_id = int(row["item_id"])
        if playlist_id in self._edited:
            self._edited.discard(playlist_id)
            return True
        return bool(row["provider_domain"] == BUILTIN)

    async def start(self) -> None:
        """Load what is known about the playlists and start counting songs in the background."""
        if self._task is None:
            for state in await self.server.storage.get_playlist_states():
                self._states[int(state["playlist_id"])] = state
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
        self, playlist_id: int, refresh: bool, everything: bool = False, strict: bool = False
    ) -> list[tuple[int, IndexedTrack | Track | None]]:
        """
        Return (MA position, playable song) for a playlist, and remember what it holds.

        :param playlist_id: The library playlist id.
        :param refresh: Read the playlist fresh instead of from MA's cache.
        :param everything: Also return songs that can't be played (song None), for positions.
        :param strict: Raise instead of returning nothing when a playlist that had songs comes
            back empty (an Apple hiccup), so an app doesn't keep an empty copy.
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
        known = self._states.get(playlist_id)
        if not songs and known is not None and known["song_count"] > 0 and not refresh:
            # most likely a failed read: keep what was known instead of an empty playlist
            self.logger.debug(
                "Playlist %s came back empty, keeping its %s songs",
                playlist_id,
                known["song_count"],
            )
            if strict:
                raise SubsonicError(ERR_GENERIC, "The playlist could not be read, try again")
            return result
        ids = [self._song_id(s) for s in songs]
        signature = hashlib.sha1("\n".join(ids).encode()).hexdigest()
        now = time.time()
        state = {
            "playlist_id": playlist_id,
            "signature": signature,
            "song_count": len(songs),
            "duration": sum(int(s.duration or 0) for s in songs),
            "changed": now
            if known is None or known["signature"] != signature
            else known["changed"],
            "counted": now,
            "stamp": await self._stamp(playlist_id),
        }
        self._states[playlist_id] = state
        await self.server.storage.save_playlist_state(state)
        return result

    def count(self, row: Mapping[str, Any]) -> tuple[int, int] | None:
        """
        Return the known (song count, duration) of a playlist, or None when not counted yet.

        :param row: The playlist's database row.
        """
        state = self._states.get(int(row["item_id"]))
        if state is None:
            return None
        return int(state["song_count"]), int(state["duration"])

    def changed(self, row: Mapping[str, Any]) -> float:
        """
        Return when a playlist last changed: in MA, or in the songs it holds.

        Apps refetch a playlist only when this moves, so a change in songs must move it.

        :param row: The playlist's database row.
        """
        state = self._states.get(int(row["item_id"]))
        return max(float(row["timestamp_modified"] or 0), float(state["changed"]) if state else 0.0)

    def _song_id(self, song: IndexedTrack | Track) -> str:
        """
        Return the Subsonic ID a playlist song is shown with.

        :param song: A song with a file, or an Apple Music track.
        """
        if isinstance(song, Track):
            return f"amt-{self.server.catalog.apple_track_id(song)}"
        return f"tr-{song.id}"

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
                known = self._states.get(playlist_id)
                stamp = int(row["timestamp_modified"] or 0)
                if (
                    known is not None
                    and int(known["stamp"]) == stamp
                    and time.time() - float(known["counted"]) < PLAYLIST_COUNT_MAX_AGE
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

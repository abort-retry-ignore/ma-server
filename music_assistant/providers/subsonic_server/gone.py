"""
Old song IDs: answering for songs that left the library with the same song, if it is still there.

Apps keep their own copy of the library, their queue and their history. When a song leaves
MA's library (a duplicate is removed, its file is deleted), they keep asking for its old ID.
The plugin remembers the title and first artist of every song clients have seen, so such an
ID can be pointed at the song with the same title and artist: a file in the library, or the
Apple Music or YouTube Music version of a library song.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import MusicAssistantError

from .apple import PREFIX_APPLE_TRACK
from .index import song_key
from .mapping import PREFIX_TRACK, make_id

if TYPE_CHECKING:
    from .server import SubsonicServer

# request parameters that hold song IDs
SONG_PARAMS = frozenset({"id", "songId", "songIdToAdd", "current"})
_LIBRARY_MATCHES = 25


class GoneSongs:
    """Finds the song that stands in for a song that left the library."""

    def __init__(self, server: SubsonicServer) -> None:
        """
        Initialize.

        :param server: The Subsonic server.
        """
        self.server = server
        self._answers: dict[int, str | None] = {}

    def forget(self) -> None:
        """Forget earlier answers (the library changed)."""
        self._answers.clear()

    async def replacement(self, value: str) -> str | None:
        """
        Return the song ID to use for a song ID that is no longer in the library, or None.

        :param value: A Subsonic ID.
        """
        prefix, _, raw = value.partition("-")
        if prefix != PREFIX_TRACK or not raw.isdigit():
            return None
        track_id = int(raw)
        if track_id in self.server.index.tracks:
            return None
        if track_id not in self._answers:
            self._answers[track_id] = await self._find(track_id)
        return self._answers[track_id]

    async def _find(self, track_id: int) -> str | None:
        """
        Return the ID of the song with the same title and artist as a song that left.

        :param track_id: The library id of the song that left.
        """
        known = self.server.storage.known_track(track_id)
        if known is None:
            return None
        name, artist = known
        key = song_key(name, artist)
        if (current := self.server.index.track_by_key.get(key)) is not None:
            return make_id(PREFIX_TRACK, current)
        catalog = self.server.catalog
        if not catalog.enabled:
            return None
        try:
            found = await self.server.mass.music.tracks.library_items(
                search=name, limit=_LIBRARY_MATCHES
            )
        except MusicAssistantError as err:
            self.server.logger.debug("Looking up '%s' in the library failed: %s", name, err)
            return None
        for track in found:
            first = track.artists[0].name if track.artists else ""
            if song_key(track.name, first) != key:
                continue
            item_id = catalog.apple_track_id(track)
            if item_id is not None and not catalog.is_blocked(item_id):
                return make_id(PREFIX_APPLE_TRACK, item_id)
        return None

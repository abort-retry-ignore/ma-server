"""
Two-way sync of favourite songs between Apple Music and MA (fork addition).

MA's own library sync only ever adds favourites: a song loved in Apple Music becomes an MA
favourite, but un-loving it in Apple Music never reaches MA. After each complete sync of the
library songs this module compares both sides:

- a song Apple Music had as favourite at the previous reconcile and no longer has is
  unmarked in MA (it was un-loved in Apple Music);
- a song that is an MA favourite but never was one in Apple Music is loved in Apple Music
  (it was starred in MA and passing that on to Apple did not happen).

A failed or incomplete answer from Apple must never wipe favourites, so:

- nothing happens unless the whole song listing went through;
- only songs in a ratings batch that Apple answered with a proper list are judged (Apple
  answers a batch without any rated song the same way as a failed one, so such songs are
  left alone);
- when one pass would unmark more than a quarter of the favourites (and more than a
  handful), it unmarks nothing and logs a warning instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError

from .helpers.utils import is_library_id, translate_media_type_to_apple_type

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from music_assistant.mass import MusicAssistant

CACHE_CATEGORY = 2601
CACHE_KEY = "favourite_songs"
RATINGS_BATCH = 200
# the most songs one pass may unmark: this share of the favourites, but at least MIN_REMOVALS
MAX_REMOVAL_SHARE = 0.25
MIN_REMOVALS = 20


class RatingsRecorder:
    """
    Wraps the API client's ``get_ratings`` during a song sync to note which answers are sure.

    Songs whose ratings batch came back with a list of ratings are "known": rated ones are
    favourites (or disliked), the others are not favourites.
    """

    def __init__(self, api_client: Any) -> None:
        """
        Initialize the recorder.

        :param api_client: The Apple Music API client used by the library sync.
        """
        self.api_client = api_client
        self.known: set[str] = set()
        self._original: Callable[[list[str], MediaType], Awaitable[dict[str, bool]]] | None = None

    def attach(self) -> None:
        """Start recording the song ratings requests."""
        self._original = self.api_client.get_ratings
        self.api_client.get_ratings = self._get_ratings

    def detach(self) -> None:
        """Stop recording."""
        if self._original is not None:
            self.api_client.get_ratings = self._original
            self._original = None

    async def _get_ratings(self, item_ids: list[str], media_type: MediaType) -> dict[str, bool]:
        """
        Return item id -> is favourite, like the client's own method, noting sure answers.

        :param item_ids: The Apple Music ids.
        :param media_type: The media type.
        """
        assert self._original is not None
        if media_type != MediaType.TRACK or not item_ids:
            return await self._original(item_ids, media_type)
        apple_type = translate_media_type_to_apple_type(media_type)
        endpoint = apple_type if not is_library_id(item_ids[0]) else f"library-{apple_type}"
        results: dict[str, bool] = {}
        for start in range(0, len(item_ids), RATINGS_BATCH):
            batch = item_ids[start : start + RATINGS_BATCH]
            response = await self.api_client.get_data(f"me/ratings/{endpoint}", ids=",".join(batch))
            if isinstance(response, dict) and isinstance(response.get("data"), list):
                self.known.update(batch)
                for item in response["data"]:
                    results[item["id"]] = bool(item["attributes"].get("value", False) == 1)
        return results


class FavouriteSongsSync:
    """Reconciles favourite songs between Apple Music and MA after a library song sync."""

    def __init__(
        self,
        mass: MusicAssistant,
        instance_id: str,
        set_apple_favourite: Callable[[str, MediaType, bool], Awaitable[None]],
        logger: logging.Logger,
    ) -> None:
        """
        Initialize.

        :param mass: The MusicAssistant instance.
        :param instance_id: The Apple Music provider instance id.
        :param set_apple_favourite: Loves (or un-loves) a song in Apple Music.
        :param logger: Logger.
        """
        self.mass = mass
        self.instance_id = instance_id
        self.set_apple_favourite = set_apple_favourite
        self.logger = logger

    async def reconcile(self, apple_favourites: dict[str, bool], known: set[str]) -> None:
        """
        Bring MA's favourite songs in line with Apple Music after a complete song sync.

        :param apple_favourites: Apple Music track id -> favourite, for every synced song.
        :param known: The Apple Music track ids whose rating answer was sure.
        """
        loved = {item_id for item_id, favourite in apple_favourites.items() if favourite}
        elsewhere = await self._in_other_libraries()
        rows = await self.mass.music.database.get_rows_from_query(
            "SELECT t.item_id, t.name, t.favorite, pm.provider_item_id FROM tracks t "
            "JOIN provider_mappings pm ON pm.item_id = t.item_id AND pm.media_type = 'track' "
            "WHERE pm.provider_instance = :instance",
            {"instance": self.instance_id},
            limit=0,
        )
        tracks: dict[int, dict[str, Any]] = {}
        for row in rows:
            track = tracks.setdefault(
                int(row["item_id"]),
                {"name": row["name"], "favorite": bool(row["favorite"]), "ids": []},
            )
            track["ids"].append(str(row["provider_item_id"]))
        previous: set[int] | None = await self.mass.cache.get(
            key=CACHE_KEY, provider=self.instance_id, category=CACHE_CATEGORY
        )
        if previous is None:
            # first pass: take MA's favourites as what Apple Music had, so any MA favourite
            # Apple Music does not have counts as un-loved there
            previous = {track_id for track_id, t in tracks.items() if t["favorite"]}
        previous = set(previous)
        remove: list[int] = []
        push: list[tuple[int, str]] = []
        now_loved: set[int] = set()
        sure: set[int] = set()
        for track_id, track in tracks.items():
            ids = track["ids"]
            if any(i in loved for i in ids):
                now_loved.add(track_id)
                sure.add(track_id)
                continue
            if not all(i in known and i in apple_favourites for i in ids):
                continue
            sure.add(track_id)
            if not track["favorite"]:
                continue
            if track_id in previous:
                if track_id not in elsewhere:
                    remove.append(track_id)
            else:
                push.append((track_id, ids[0]))
        favourites = sum(1 for t in tracks.values() if t["favorite"])
        limit = max(MIN_REMOVALS, int(favourites * MAX_REMOVAL_SHARE))
        if len(remove) > limit:
            self.logger.warning(
                "Apple Music favourites: %s songs would stop being MA favourites, more than "
                "the %s allowed in one pass; leaving them (for example: %s)",
                len(remove),
                limit,
                ", ".join(tracks[t]["name"] for t in remove[:5]),
            )
            remove = []
        for track_id in remove:
            await self.mass.music.tracks.set_favorite(track_id, False)
        pushed: set[int] = set()
        for track_id, apple_id in push:
            try:
                await self.set_apple_favourite(apple_id, MediaType.TRACK, True)
            except MusicAssistantError as err:
                self.logger.warning(
                    "Apple Music favourites: could not love '%s' in Apple Music: %s",
                    tracks[track_id]["name"],
                    err,
                )
                continue
            pushed.add(track_id)
        if remove or pushed:
            self.logger.info(
                "Apple Music favourites: unmarked %s songs no longer loved in Apple Music (%s), "
                "loved %s MA favourites in Apple Music (%s)",
                len(remove),
                ", ".join(tracks[t]["name"] for t in remove[:10]) or "-",
                len(pushed),
                ", ".join(tracks[t]["name"] for t in sorted(pushed)[:10]) or "-",
            )
        # songs without a sure answer keep what was known about them
        current = (previous - sure) | now_loved | pushed
        await self.mass.cache.set(
            key=CACHE_KEY,
            data=sorted(current),
            provider=self.instance_id,
            category=CACHE_CATEGORY,
            expiration=60 * 60 * 24 * 365,
        )

    async def _in_other_libraries(self) -> set[int]:
        """
        Return library songs that another streaming service also has in its library.

        Their MA favourite may come from that service (a YouTube Music like, say), so Apple
        Music not loving them is no reason to unmark them.
        """
        rows = await self.mass.music.database.get_rows_from_query(
            "SELECT DISTINCT item_id FROM provider_mappings WHERE media_type = 'track' "
            "AND in_library = 1 AND provider_instance != :instance "
            "AND provider_domain NOT LIKE 'filesystem%' AND provider_domain != 'builtin'",
            {"instance": self.instance_id},
            limit=0,
        )
        return {int(row["item_id"]) for row in rows}

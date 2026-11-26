"""Tests for the two-way favourite songs sync between Apple Music and MA."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from music_assistant_models.enums import MediaType

from music_assistant.providers.apple_music.favourites_sync import (
    FavouriteSongsSync,
    RatingsRecorder,
)

INSTANCE = "apple_music--test"


class FakeCache:
    """MA's cache, in memory."""

    def __init__(self) -> None:
        """Initialize."""
        self.data: dict[str, Any] = {}

    async def get(self, key: str, **_kwargs: Any) -> Any:
        """
        Return a cached value.

        :param key: The key.
        """
        return self.data.get(key)

    async def set(self, key: str, data: Any, **_kwargs: Any) -> None:
        """
        Store a value.

        :param key: The key.
        :param data: The value.
        """
        self.data[key] = data


def _sync(rows: list[dict[str, Any]], cache: FakeCache | None = None) -> tuple[Any, Any, AsyncMock]:
    """
    Return a sync over the given library rows, the fake mass and the Apple "love" mock.

    :param rows: Library track rows: item_id, name, favorite, provider_item_id.
    :param cache: The cache to use.
    """
    mass = SimpleNamespace(
        music=SimpleNamespace(
            database=SimpleNamespace(get_rows_from_query=AsyncMock(return_value=rows)),
            tracks=SimpleNamespace(set_favorite=AsyncMock()),
        ),
        cache=cache or FakeCache(),
    )
    love = AsyncMock()
    sync = FavouriteSongsSync(mass, INSTANCE, love, logging.getLogger("test"))  # type: ignore[arg-type]
    return sync, mass, love


def _row(item_id: int, apple_id: str, favorite: bool) -> dict[str, Any]:
    """
    Return a library track row.

    :param item_id: The library id.
    :param apple_id: The Apple Music id.
    :param favorite: Whether it is an MA favourite.
    """
    return {
        "item_id": item_id,
        "name": f"Song {item_id}",
        "favorite": favorite,
        "provider_item_id": apple_id,
    }


async def test_first_pass_follows_apple_music() -> None:
    """An MA favourite that Apple Music doesn't have is unmarked; unsure songs are left alone."""
    rows = [_row(1, "a", True), _row(2, "b", True), _row(3, "c", False), _row(4, "d", True)]
    sync, mass, love = _sync(rows)
    await sync.reconcile({"a": True, "b": False, "c": False, "d": False}, known={"a", "b", "c"})
    mass.music.tracks.set_favorite.assert_awaited_once_with(2, False)
    love.assert_not_awaited()
    assert mass.cache.data["favourite_songs"] == [1, 4]


async def test_unloved_in_apple_is_unmarked_and_starred_in_ma_is_loved() -> None:
    """After the first pass, an un-love in Apple unmarks; a new MA star is sent to Apple."""
    cache = FakeCache()
    cache.data["favourite_songs"] = [1, 2]
    rows = [_row(1, "a", True), _row(2, "b", True), _row(3, "c", True)]
    sync, mass, love = _sync(rows, cache)
    await sync.reconcile({"a": True, "b": False, "c": False}, known={"a", "b", "c"})
    mass.music.tracks.set_favorite.assert_awaited_once_with(2, False)
    love.assert_awaited_once_with("c", MediaType.TRACK, True)
    assert cache.data["favourite_songs"] == [1, 3]


async def test_too_many_removals_at_once_are_skipped() -> None:
    """When Apple's answer would unmark most favourites at once, nothing is unmarked."""
    rows = [_row(i, f"s{i}", True) for i in range(1, 31)]
    sync, mass, _love = _sync(rows)
    await sync.reconcile(
        {f"s{i}": False for i in range(1, 31)}, known={f"s{i}" for i in range(1, 31)}
    )
    mass.music.tracks.set_favorite.assert_not_awaited()


async def test_recorder_only_trusts_answered_batches() -> None:
    """Songs in a batch Apple answered with a list are known; an empty answer is not trusted."""
    answers = [
        {"data": [{"id": "1", "attributes": {"value": 1}}]},
        {},
    ]
    client = SimpleNamespace(
        get_ratings=AsyncMock(return_value={}),
        get_data=AsyncMock(side_effect=answers),
    )
    recorder = RatingsRecorder(client)
    recorder.attach()
    ids = [str(i) for i in range(1, 251)]
    result = await client.get_ratings(ids, MediaType.TRACK)
    recorder.detach()
    assert result == {"1": True}
    assert recorder.known == set(ids[:200])
    # albums go to the client's own method
    recorder.attach()
    await client.get_ratings(["9"], MediaType.ALBUM)
    recorder.detach()
    assert client.get_data.await_count == 2

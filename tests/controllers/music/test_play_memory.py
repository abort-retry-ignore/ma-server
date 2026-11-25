"""Tests for the play memory that corrects near-miss track searches."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import InvalidCommand, ProviderUnavailableError
from music_assistant_models.media_items import (
    ItemMapping,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.play_memory import (
    PlayMemory,
    _heard_from_row,
    _normalize,
    _rank_matches,
)

if TYPE_CHECKING:
    import pathlib

    from music_assistant.mass import MusicAssistant


def _track(item_id: str, provider: str, name: str, artist: str = "Some Artist") -> Track:
    """
    Return a minimal track with one artist.

    :param item_id: The item id on the provider.
    :param provider: The provider instance id (or "library").
    :param name: The track name.
    :param artist: The artist name.
    """
    mappings = (
        set()
        if provider == "library"
        else {
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider.split("--", maxsplit=1)[0],
                provider_instance=provider,
            )
        }
    )
    return Track(
        item_id=item_id,
        provider=provider,
        name=name,
        provider_mappings=mappings,
        artists=UniqueList(
            [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=f"{item_id}_artist",
                    provider=provider,
                    name=artist,
                )
            ]
        ),
    )


def _heard(name: str, artists: str = "", plays: int = 1) -> Any:
    """
    Return a heard entry built the way the database rows are.

    :param name: The track name.
    :param artists: The artist names joined with ' / '.
    :param plays: The number of full plays.
    """
    return _heard_from_row(
        {
            "uri": f"library://track/{name}",
            "name": name,
            "artists": artists,
            "plays": plays,
            "last_played": 0,
            "item_json": None,
        }
    )


def _make_mass(tmp_path: pathlib.Path) -> Mock:
    """
    Return a mocked Music Assistant with just what the play memory uses.

    :param tmp_path: Folder for the play memory database.
    """
    mass = Mock()
    mass.storage_path = str(tmp_path)
    mass.music.logger = logging.getLogger(__name__)
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    mass.register_api_command = Mock(return_value=Mock())

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> None:
        # seeding is awaited by the tests that need it
        target.close()

    mass.create_task = Mock(side_effect=_create_task)
    return mass


async def _setup_memory(mass: Mock) -> PlayMemory:
    """
    Return a play memory that is set up on the mocked mass.

    :param mass: The mocked Music Assistant.
    """
    memory = PlayMemory(mass)
    await memory.setup()
    return memory


def test_misheard_title_matches_heard_track() -> None:
    """A close spelling variant of a heard title matches it; an unrelated title does not."""
    heard = [_heard("Anvil of Crom", "Basil Poledouris"), _heard("Riders of Doom")]
    matches = _rank_matches(_normalize("anvil of chrome"), heard)
    assert [h.name for h in matches] == ["Anvil of Crom"]


def test_artist_and_title_query_matches() -> None:
    """A query in the form 'Artist - Title' matches the heard track of that artist."""
    heard = [_heard("Anvil of Crom", "Basil Poledouris")]
    matches = _rank_matches(_normalize("Basil Poledouris - Anvil of Crom"), heard)
    assert [h.name for h in matches] == ["Anvil of Crom"]


def test_wrapped_upload_title_matches() -> None:
    """Track numbers and bracketed extras around a heard title do not prevent a match."""
    heard = [_heard("01. Anvil Of Crom [Prologue] (Conan The Barbarian Soundtrack)")]
    assert _rank_matches(_normalize("anvil of chrome"), heard)


def test_short_titles_need_an_exact_match() -> None:
    """One letter changes a short title, so 'Hurts' must not pick up a heard 'Hurt'."""
    heard = [_heard("Hurt", "Johnny Cash")]
    assert not _rank_matches(_normalize("Hurts"), heard)
    assert _rank_matches(_normalize("Hurt"), heard)


def test_artist_only_and_longer_titles_do_not_match() -> None:
    """An artist search or a longer title that merely starts the same is not a variant."""
    heard = [_heard("Money", "Pink Floyd"), _heard("Lola", "The Kinks")]
    assert not _rank_matches(_normalize("Pink Floyd"), heard)
    assert not _rank_matches(_normalize("The Kinks"), heard)
    assert not _rank_matches(_normalize("Money for Nothing"), heard)


def test_noise_words_are_ignored() -> None:
    """Words a search adds around a title ('by', 'remastered') do not block a match."""
    heard = [_heard("Lola", "The Kinks")]
    assert _rank_matches(_normalize("Lola by The Kinks remastered"), heard)


def test_more_played_track_ranks_first_on_equal_match() -> None:
    """Two equally close matches are ordered by how often they were heard."""
    once = _heard("Anvil of Crom", "Basil Poledouris", plays=1)
    often = _heard("Anvil of Crom", "Basil Poledouris", plays=5)
    often.uri = "library://track/often"
    assert _rank_matches(_normalize("Anvil of Crom"), [once, often])[0] is often


async def test_heard_track_is_put_on_top_of_search_results(tmp_path: pathlib.Path) -> None:
    """A fully played track comes first in a misheard search, ahead of the look-alike."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    real = _track("111", "apple_music--abc", "Anvil of Crom", "Basil Poledouris")
    lookalike = _track("222", "apple_music--abc", "Anvil of Chrome", "Chrome Nine")
    await memory.remember(real)

    results = SearchResults(tracks=[lookalike])
    adjusted = await memory.adjust_search_results("Anvil of Chrome", [MediaType.TRACK], 5, results)

    assert [t.name for t in adjusted.tracks] == ["Anvil of Crom", "Anvil of Chrome"]
    assert results.tracks == [lookalike]
    await memory.close()


async def test_heard_track_already_in_results_moves_to_top(tmp_path: pathlib.Path) -> None:
    """A heard track that the search found anyway is moved up, not listed twice."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    real = _track("111", "apple_music--abc", "Anvil of Crom", "Basil Poledouris")
    lookalike = _track("222", "apple_music--abc", "Anvil of Chrome", "Chrome Nine")
    await memory.remember(real)

    adjusted = await memory.adjust_search_results(
        "Anvil of Chrome", [MediaType.TRACK], 5, SearchResults(tracks=[lookalike, real])
    )

    assert [t.item_id for t in adjusted.tracks] == ["111", "222"]
    await memory.close()


async def test_search_without_tracks_is_left_alone(tmp_path: pathlib.Path) -> None:
    """Searches that do not ask for tracks are returned unchanged."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    await memory.remember(_track("111", "apple_music--abc", "Anvil of Crom"))
    results = SearchResults()
    assert (
        await memory.adjust_search_results("anvil of chrome", [MediaType.ALBUM], 5, results)
        is results
    )
    await memory.close()


async def test_blocked_track_is_left_out_and_forgotten(tmp_path: pathlib.Path) -> None:
    """A blocked track disappears from searches, also under its library uri."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    lookalike = _track("222", "apple_music--abc", "Anvil of Chrome", "Chrome Nine")
    library_lookalike = _track("9", "library", "Anvil of Chrome", "Chrome Nine")
    library_lookalike.provider_mappings = set(lookalike.provider_mappings)
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=library_lookalike)
    await memory.remember(lookalike)

    blocked = await memory.block_track(lookalike)

    assert "library://track/9" in blocked
    assert "apple_music--abc://track/222" in blocked
    assert "apple_music://track/222" in blocked
    adjusted = await memory.adjust_search_results(
        "Anvil of Chrome",
        [MediaType.TRACK],
        5,
        SearchResults(tracks=[library_lookalike, lookalike]),
    )
    assert adjusted.tracks == []
    assert (await memory.list_entries())["heard"] == []
    await memory.close()


async def test_unblock_restores_every_version(tmp_path: pathlib.Path) -> None:
    """Unblocking by any of its uris lifts the whole block."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    lookalike = _track("222", "apple_music--abc", "Anvil of Chrome")
    await memory.block_track(lookalike)

    removed = await memory.unblock("apple_music://track/222")

    assert "apple_music--abc://track/222" in removed
    assert not memory.is_blocked(lookalike)
    await memory.close()


async def test_blocked_track_is_not_remembered(tmp_path: pathlib.Path) -> None:
    """Playing a blocked track to the end does not put it back in the memory."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    lookalike = _track("222", "apple_music--abc", "Anvil of Chrome")
    await memory.block_track(lookalike)
    await memory.remember(lookalike)
    assert (await memory.list_entries())["heard"] == []
    await memory.close()


async def test_replay_within_window_counts_once(tmp_path: pathlib.Path) -> None:
    """Two fully played reports of one listen count as a single play."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    track = _track("111", "apple_music--abc", "Anvil of Crom")
    await memory.remember(track)
    await memory.remember(track)
    assert (await memory.list_entries())["heard"][0]["plays"] == 1
    await memory.close()


async def test_memory_survives_a_restart(tmp_path: pathlib.Path) -> None:
    """Heard and blocked tracks are read back from the database file."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    await memory.remember(_track("111", "apple_music--abc", "Anvil of Crom", "Basil Poledouris"))
    await memory.block_track(_track("222", "apple_music--abc", "Anvil of Chrome"))
    await memory.close()

    reopened = await _setup_memory(mass)
    adjusted = await reopened.adjust_search_results(
        "anvil of chrome",
        [MediaType.TRACK],
        5,
        SearchResults(tracks=[_track("222", "apple_music--abc", "Anvil of Chrome")]),
    )
    assert [t.name for t in adjusted.tracks] == ["Anvil of Crom"]
    await reopened.close()


async def test_new_memory_is_seeded_from_the_playlog(tmp_path: pathlib.Path) -> None:
    """Fully played tracks already in the playlog are remembered, keyed by their library uri."""
    mass = _make_mass(tmp_path)
    mass.music.database.get_rows_from_query = AsyncMock(
        return_value=[
            {
                "item_id": "2826",
                "provider": "library",
                "name": "Anvil of Crom",
                "artists": '[{"name": "Basil Poledouris"}]',
                "last_played": 1000,
            },
            {
                "item_id": "333",
                "provider": "apple_music",
                "name": "Lola",
                "artists": None,
                "last_played": 2000,
            },
        ]
    )
    memory = await _setup_memory(mass)
    await memory._seed_from_playlog()

    entries = (await memory.list_entries())["heard"]
    assert [(e["uri"], e["artists"]) for e in entries] == [
        ("apple_music://track/333", ""),
        ("library://track/2826", "Basil Poledouris"),
    ]
    await memory.close()

    # a restart does not seed again
    reopened = PlayMemory(mass)
    await reopened.setup()
    assert mass.create_task.call_count == 1
    await reopened.close()


async def test_block_current_skips_to_the_next_track(tmp_path: pathlib.Path) -> None:
    """Blocking what plays now moves the queue on, and stops it when nothing follows."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    lookalike = _track("222", "apple_music--abc", "Anvil of Chrome")
    queue = Mock(queue_id="kitchen", display_name="Kitchen", state=PlaybackState.PLAYING)
    queue.current_item = Mock(queue_item_id="qi1", media_item=lookalike)
    mass.player_queues.all = Mock(return_value=(queue,))
    mass.player_queues.get = Mock(return_value=queue)
    mass.player_queues.next = AsyncMock()
    mass.player_queues.stop = AsyncMock()

    result = await memory.block_current()

    assert result["name"] == "Anvil of Chrome"
    assert memory.is_blocked(lookalike)
    mass.player_queues.next.assert_awaited_once_with("kitchen")
    # the mocked queue did not advance, so there was nothing after it
    mass.player_queues.stop.assert_awaited_once_with("kitchen")
    await memory.close()


async def test_block_current_needs_one_playing_queue(tmp_path: pathlib.Path) -> None:
    """Without a queue id, block_current refuses when nothing or several queues play."""
    mass = _make_mass(tmp_path)
    memory = await _setup_memory(mass)
    mass.player_queues.all = Mock(return_value=())
    with pytest.raises(InvalidCommand):
        await memory.block_current()
    playing = [Mock(queue_id=q, display_name=q, state=PlaybackState.PLAYING) for q in ("a", "b")]
    mass.player_queues.all = Mock(return_value=tuple(playing))
    with pytest.raises(InvalidCommand):
        await memory.block_current()
    await memory.close()


async def test_full_server_corrects_a_misheard_search(mass: MusicAssistant) -> None:
    """On a running server, a fully played track wins a misheard search until it is blocked."""
    assert "music/play_memory/block_current" in mass.command_handlers
    library_track = await mass.music.tracks.add_item_to_library(
        _track("anvil.mp3", "filesystem_local--AbCd", "Anvil of Crom", "Basil Poledouris")
    )

    misheard = await mass.music.search("anvil of chrome", [MediaType.TRACK])
    assert misheard.tracks == []

    await mass.music.mark_item_played(library_track, fully_played=True)
    misheard = await mass.music.search("anvil of chrome", [MediaType.TRACK])
    assert [t.uri for t in misheard.tracks] == [library_track.uri]

    await mass.music.play_memory.block_track(library_track)
    blocked = await mass.music.search("Anvil of Crom", [MediaType.TRACK])
    assert blocked.tracks == []


async def test_unloadable_heard_track_does_not_break_search(tmp_path: pathlib.Path) -> None:
    """A seeded streaming track whose provider is offline is skipped, the search still works."""
    mass = _make_mass(tmp_path)
    mass.music.database.get_rows_from_query = AsyncMock(
        return_value=[
            {
                "item_id": "111",
                "provider": "apple_music",
                "name": "Anvil of Crom",
                "artists": None,
                "last_played": 1000,
            }
        ]
    )
    mass.music.get_item_by_uri = AsyncMock(side_effect=ProviderUnavailableError("offline"))
    memory = await _setup_memory(mass)
    await memory._seed_from_playlog()
    other = _track("222", "apple_music--abc", "Anvil of Chrome")

    adjusted = await memory.adjust_search_results(
        "anvil of chrome", [MediaType.TRACK], 5, SearchResults(tracks=[other])
    )

    assert adjusted.tracks == [other]
    await memory.close()

"""Apple Music search results and save-on-play, with a fake Apple Music provider and cache."""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import StreamType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.providers.subsonic_server.apple import AppleCatalog
from tests.providers.subsonic_server.test_server import Server, server  # noqa: F401

APPLE = "apple_music--test"


def _mapping(item_id: str) -> set[ProviderMapping]:
    """
    Return the Apple Music mapping of an item.

    :param item_id: The Apple Music id.
    """
    return {
        ProviderMapping(item_id=item_id, provider_domain="apple_music", provider_instance=APPLE)
    }


ARTIST = Artist(
    item_id="900", provider=APPLE, name="Basil Poledouris", provider_mappings=_mapping("900")
)
ALBUM = Album(
    item_id="800",
    provider=APPLE,
    name="Conan the Barbarian",
    year=1982,
    artists=UniqueList([ARTIST]),
    provider_mappings=_mapping("800"),
)


def _track(item_id: str, name: str) -> Track:
    """
    Return an Apple Music catalogue track.

    :param item_id: The Apple Music id.
    :param name: The title.
    """
    return Track(
        item_id=item_id,
        provider=APPLE,
        name=name,
        duration=180,
        artists=UniqueList([ARTIST]),
        album=ALBUM,
        track_number=1,
        provider_mappings=_mapping(item_id),
    )


TRACKS = {
    "111": _track("111", "Anvil of Crom (Apple)"),
    "222": _track("222", "Anvil of Crom (Blocked)"),
    "333": _track("333", "Anvil of Crom (Already Saved)"),
}


class FakeStreams:
    """The Apple Music provider's caching stream manager, saving into a temp folder."""

    def __init__(self, folder: pathlib.Path) -> None:
        """
        Initialize.

        :param folder: Where saved files go.
        """
        self.folder = folder
        self.saved: dict[str, str] = {}
        self.saves = 0
        self.audio_cache = self

    async def lookup_clear(self, item_id: str) -> str | None:
        """
        Return the saved file of a track.

        :param item_id: The Apple Music id.
        """
        return self.saved.get(item_id)

    async def get_stream_details(self, item_id: str) -> Any:
        """
        Return stream details for a track that isn't saved yet.

        :param item_id: The Apple Music id.
        """
        return SimpleNamespace(
            item_id=item_id,
            stream_type=StreamType.CUSTOM,
            path=None,
            data=SimpleNamespace(key_id="k"),
        )

    async def stream_track_and_store(self, details: Any) -> Any:
        """
        Pretend to download and decrypt, then keep the file.

        :param details: The stream details.
        """
        self.saves += 1
        await asyncio.sleep(0.3)
        path = self.folder / f"{details.item_id}.m4a"
        path.write_bytes(b"apple-audio-" + details.item_id.encode() * 100)
        self.saved[details.item_id] = str(path)
        yield b"done"


@pytest.fixture
def apple(server: Server, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeStreams:  # noqa: F811
    """
    Give the server a fake Apple Music provider and catalogue answers.

    :param server: The running server.
    :param tmp_path: Temporary directory.
    :param monkeypatch: pytest monkeypatch.
    """
    streams = FakeStreams(tmp_path)
    provider = SimpleNamespace(instance_id=APPLE, domain="apple_music", caching_streams=streams)
    monkeypatch.setattr(AppleCatalog, "provider", property(lambda _self: provider))
    assert server.provider.server is not None
    catalog = server.provider.server.catalog
    results = SearchResults(artists=[ARTIST], albums=[ALBUM], tracks=list(TRACKS.values()))
    monkeypatch.setattr(catalog, "search", AsyncMock(return_value=results))
    monkeypatch.setattr(catalog, "track", AsyncMock(side_effect=lambda item_id: TRACKS[item_id]))
    monkeypatch.setattr(catalog, "album", AsyncMock(return_value=ALBUM))
    monkeypatch.setattr(catalog, "album_tracks", AsyncMock(return_value=[TRACKS["111"]]))
    monkeypatch.setattr(catalog, "artist", AsyncMock(return_value=ARTIST))
    monkeypatch.setattr(catalog, "artist_albums", AsyncMock(return_value=[ALBUM]))
    server.mass.music.play_memory._blocked["apple_music://track/222"] = {}
    return streams


@pytest.mark.usefixtures("apple")
async def test_apple_results_follow_the_music_folder(server: Server) -> None:  # noqa: F811
    """Apple songs come after local ones; blocked songs and songs with a file are left out."""
    index = server.provider.server.index  # type: ignore[union-attr]
    crom = next(t for t in index.tracks.values() if t.name == "Anvil of Crom")
    index.track_by_uri["apple_music://track/333"] = crom.id
    found = (await server.call("search3", query="anvil"))["searchResult3"]
    ids = [s["id"] for s in found["song"]]
    assert ids[:2] == [f"tr-{t.id}" for t in index.search_tracks("anvil")]
    assert ids[2:] == ["amt-111"]
    apple_song = found["song"][2]
    assert apple_song["suffix"] == "m4a"
    assert apple_song["albumId"] == "ama-800"
    assert apple_song["artistId"] == "amr-900"
    assert any(a["id"] == "ama-800" for a in found["album"])
    assert (await server.call("getSong", id="amt-111"))["song"]["title"] == "Anvil of Crom (Apple)"
    assert (await server.call("getSong", id="amt-222"))["error"]["code"] == 70


async def test_apple_song_is_saved_before_it_streams(server: Server, apple: FakeStreams) -> None:  # noqa: F811
    """Two requests for an unsaved song wait for one save and get the saved file."""
    first, second = await asyncio.gather(
        server.get("stream", id="amt-111"), server.get("stream", id="amt-111")
    )
    assert first.status == second.status == 200
    body = await first.read()
    assert body == await second.read()
    assert body.startswith(b"apple-audio-111")
    assert first.headers["Content-Type"] == "audio/mp4"
    assert apple.saves == 1
    part = await server.get("stream", id="amt-111", headers={"Range": "bytes=0-4"})
    assert part.status == 206
    assert await part.read() == b"apple"
    assert apple.saves == 1


@pytest.mark.usefixtures("apple")
async def test_apple_album_and_artist_pages(server: Server) -> None:  # noqa: F811
    """Tapping an Apple album or artist from search opens it."""
    album = (await server.call("getAlbum", id="ama-800"))["album"]
    assert album["name"] == "Conan the Barbarian"
    assert [s["id"] for s in album["song"]] == ["amt-111"]
    artist = (await server.call("getArtist", id="amr-900"))["artist"]
    assert [a["id"] for a in artist["album"]] == ["ama-800"]
    directory = (await server.call("getMusicDirectory", id="ama-800"))["directory"]
    assert [c["id"] for c in directory["child"]] == ["amt-111"]


@pytest.mark.usefixtures("apple")
async def test_apple_plays_and_stars_go_to_ma(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished play and a star of an Apple song reach MA's play history and favourites."""
    played = AsyncMock()
    starred = AsyncMock()
    monkeypatch.setattr(server.mass.music, "mark_item_played", played)
    monkeypatch.setattr(server.mass.music, "add_item_to_favorites", starred)
    await server.call("scrobble", id="amt-111", submission="false")
    playing = (await server.call("getNowPlaying"))["nowPlaying"]["entry"]
    assert playing[0]["id"] == "amt-111"
    await server.call("scrobble", id="amt-111")
    assert played.await_args is not None
    assert played.await_args.args[0].item_id == "111"
    await server.call("star", id="amt-111")
    assert starred.await_args is not None
    assert starred.await_args.args[0] == f"{APPLE}://track/111"

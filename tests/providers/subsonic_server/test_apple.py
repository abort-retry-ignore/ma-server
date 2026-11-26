"""Apple Music search results and save-on-play, with a fake Apple Music provider and cache."""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from music_assistant_models.enums import ImageType, MediaType, StreamType
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
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


@pytest.mark.usefixtures("apple")
async def test_artist_known_only_by_name_is_found_by_search(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Apple artist without a link (only a name) gets an id that opens it via search."""
    assert server.provider.server is not None
    catalog = server.provider.server.catalog
    named = _track("444", "Paranoid")
    named.artists = UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id="Black Sabbath",
                provider=APPLE,
                name="Black Sabbath",
            )
        ]
    )
    sabbath = Artist(
        item_id="901", provider=APPLE, name="Black Sabbath", provider_mappings=_mapping("901")
    )
    monkeypatch.setattr(catalog, "track", AsyncMock(return_value=named))
    monkeypatch.setattr(
        catalog, "search", AsyncMock(return_value=SearchResults(artists=[ARTIST, sabbath]))
    )
    # the real lookups, so the name is resolved to the id the search returns
    monkeypatch.setattr(catalog, "artist", AppleCatalog.artist.__get__(catalog))
    monkeypatch.setattr(catalog, "artist_albums", AppleCatalog.artist_albums.__get__(catalog))
    fetched: list[str] = []

    async def _get(kind: str, item_id: str, _getter: Any) -> Any:
        fetched.append(item_id)
        return sabbath if kind == "artist" else [ALBUM]

    monkeypatch.setattr(catalog, "_get", _get)
    song = (await server.call("getSong", id="amt-444"))["song"]
    assert song["artistId"] == "amr-n" + b"Black Sabbath".hex()
    artist = (await server.call("getArtist", id=song["artistId"]))["artist"]
    assert artist["name"] == "Black Sabbath"
    assert fetched == ["901", "901"]


async def _add_apple_playlist(
    server: Server,  # noqa: F811
    item_id: str,
    name: str,
    in_library: bool,
) -> int:
    """
    Add an Apple Music playlist to MA's library and return its library id.

    :param server: The running server.
    :param item_id: The Apple Music playlist id.
    :param name: The playlist name.
    :param in_library: Whether it is still in the Apple Music library.
    """
    playlist = Playlist(
        item_id=item_id,
        provider=APPLE,
        name=name,
        owner="me",
        is_editable=True,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="apple_music",
                provider_instance=APPLE,
                in_library=in_library,
            )
        },
    )
    added = await server.mass.music.playlists.add_item_to_library(playlist)
    return int(added.item_id)


@pytest.mark.usefixtures("apple")
async def test_apple_playlists_show_with_their_songs(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apple Music playlists in the library show, with songs that have a file or can be saved."""
    rock = await _add_apple_playlist(server, "p.rock", "Rock", True)
    await _add_apple_playlist(server, "p.gone", "Deleted in Apple Music", False)
    index = server.provider.server.index  # type: ignore[union-attr]
    crom = next(t for t in index.tracks.values() if t.name == "Anvil of Crom")
    library_crom = await server.mass.music.tracks.get_library_item(crom.id)
    library_crom.position = 1
    apple_song = TRACKS["111"].__class__.from_dict(TRACKS["111"].to_dict())
    apple_song.position = 2
    blocked = TRACKS["222"].__class__.from_dict(TRACKS["222"].to_dict())
    blocked.position = 3

    reads: list[bool] = []

    async def _tracks(item_id: str, _provider: str, force_refresh: bool = False) -> Any:
        assert int(item_id) == rock
        reads.append(force_refresh)
        for item in (library_crom, apple_song, blocked):
            yield item

    monkeypatch.setattr(server.mass.music.playlists, "tracks", _tracks)
    listed = (await server.call("getPlaylists"))["playlists"]["playlist"]
    names = [p["name"] for p in listed]
    assert "Rock" in names
    assert "Deleted in Apple Music" not in names
    playlist = (await server.call("getPlaylist", id=f"pl-{rock}"))["playlist"]
    assert [e["id"] for e in playlist["entry"]] == [f"tr-{crom.id}", "amt-111"]
    assert playlist["songCount"] == 2
    assert playlist["readonly"] is False
    counted = next(
        p
        for p in (await server.call("getPlaylists"))["playlists"]["playlist"]
        if p["name"] == "Rock"
    )
    assert counted["songCount"] == 2

    added = AsyncMock()
    removed = AsyncMock()
    monkeypatch.setattr(server.mass.music.playlists, "_handle_add_playlist_tracks", added)
    monkeypatch.setattr(server.mass.music.playlists, "_handle_remove_playlist_tracks", removed)
    await server.call(
        "updatePlaylist", playlistId=f"pl-{rock}", songIdToAdd="amt-111", songIndexToRemove=1
    )
    assert added.await_args is not None
    assert added.await_args.args == (rock, ["apple_music://track/111"])
    assert removed.await_args is not None
    assert removed.await_args.args == (rock, (2,))
    refused = await server.call("deletePlaylist", id=f"pl-{rock}")
    assert refused["error"]["code"] == 50
    # Apple playlists are read from MA's cache, except right after a change from the app
    reads.clear()
    await server.call("getPlaylist", id=f"pl-{rock}")
    await server.call("getPlaylist", id=f"pl-{rock}")
    assert reads == [True, False]


@pytest.mark.usefixtures("apple")
async def test_playlist_song_without_album_link_shows_its_own_cover(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A song listed without its album link gets its own picture, with no Apple search."""
    assert server.provider.server is not None
    catalog = server.provider.server.catalog
    listed = _track("555", "Muxima")
    listed.album = ItemMapping(
        media_type=MediaType.ALBUM,
        item_id="Angola Soundtrack",
        provider=APPLE,
        name="Angola Soundtrack",
    )
    listed.metadata.add_image(
        MediaItemImage(type=ImageType.THUMB, path="https://art.example/555.jpg", provider=APPLE)
    )
    monkeypatch.setattr(catalog, "track", AsyncMock(return_value=listed))
    searched = AsyncMock(return_value=SearchResults())
    monkeypatch.setattr(catalog, "search", searched)
    thumbs: list[str] = []

    async def _thumbnail(path: str, *_args: Any, **_kwargs: Any) -> bytes:
        thumbs.append(path)
        return b"\xff\xd8\xff" + b"0" * 20

    monkeypatch.setattr(server.mass.metadata, "get_thumbnail", _thumbnail)
    song = (await server.call("getSong", id="amt-555"))["song"]
    assert song["coverArt"] == "amt-555"
    assert song["albumId"] == "ama-n" + b"Angola Soundtrack\x1fBasil Poledouris".hex()
    art = await server.get("getCoverArt", id="amt-555", size=32)
    assert art.status == 200
    assert thumbs == ["https://art.example/555.jpg"]
    # the album known only by name shows its songs' art, also under the older id format
    album_art = await server.get("getCoverArt", id=song["albumId"], size=32)
    assert album_art.status == 200
    assert album_art.headers["Content-Type"] == "image/jpeg"
    legacy = "ama-n" + b"Angola Soundtrack".hex()
    assert (await server.get("getCoverArt", id=legacy, size=32)).status == 200
    searched.assert_not_awaited()
    # an album never seen before is found by a search, then its cover is shown
    unseen = Album(
        item_id="801",
        provider=APPLE,
        name="Together",
        artists=UniqueList([ARTIST]),
        provider_mappings=_mapping("801"),
    )
    unseen.metadata.add_image(
        MediaItemImage(type=ImageType.THUMB, path="https://art.example/801.jpg", provider=APPLE)
    )
    searched.return_value = SearchResults(albums=[unseen])
    monkeypatch.setattr(catalog, "album", AppleCatalog.album.__get__(catalog))

    async def _get(kind: str, item_id: str, _getter: Any) -> Any:
        assert (kind, item_id) == ("album", "801")
        return unseen

    monkeypatch.setattr(catalog, "_get", _get)
    found = await server.get("getCoverArt", id="ama-n" + b"Together".hex(), size=32)
    assert found.status == 200
    assert thumbs[-1] == "https://art.example/801.jpg"


@pytest.mark.usefixtures("apple")
async def test_icloud_song_passes_through_with_ranges(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A song that only lives in iCloud is passed through from Apple's link, ranges included."""
    assert server.provider.server is not None
    catalog = server.provider.server.catalog
    audio = tmp_path / "upload.mp3"
    audio.write_bytes(b"ID3" + bytes(range(256)) * 10)

    async def _serve(_request: web.Request) -> web.StreamResponse:
        return web.FileResponse(audio, headers={"Content-Type": "audio/mpeg"})

    app = web.Application()
    app.router.add_get("/song", _serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        monkeypatch.setattr(
            catalog, "icloud_url", AsyncMock(return_value=f"http://127.0.0.1:{port}/song")
        )
        full = await server.get("stream", id="amt-i.ABC123")
        assert full.status == 200
        assert full.headers["Content-Type"] == "audio/mpeg"
        assert await full.read() == audio.read_bytes()
        part = await server.get("stream", id="amt-i.ABC123", headers={"Range": "bytes=3-6"})
        assert part.status == 206
        assert await part.read() == bytes(range(4))
    finally:
        await runner.cleanup()

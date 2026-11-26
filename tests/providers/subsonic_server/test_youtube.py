"""YouTube Music search results, playlists and save-on-play, with a fake YouTube Music provider."""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)

from music_assistant.providers.subsonic_server.apple import AppleCatalog
from tests.providers.subsonic_server.test_apple import TRACKS, FakeStreams, apple  # noqa: F401
from tests.providers.subsonic_server.test_server import Server, server  # noqa: F401

YOUTUBE = "ytmusic--test"


def _mapping(item_id: str, in_library: bool = False) -> set[ProviderMapping]:
    """
    Return the YouTube Music mapping of an item.

    :param item_id: The YouTube Music id.
    :param in_library: Whether it is in the YouTube Music library.
    """
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain="ytmusic",
            provider_instance=YOUTUBE,
            in_library=in_library,
        )
    }


YT_ARTIST = Artist(
    item_id="UCbasil",
    provider=YOUTUBE,
    name="Basil Poledouris",
    provider_mappings=_mapping("UCbasil"),
)
YT_ALBUM = Album(
    item_id="MPREb_conan",
    provider=YOUTUBE,
    name="Conan the Barbarian",
    artists=UniqueList([YT_ARTIST]),
    provider_mappings=_mapping("MPREb_conan"),
)


def _yt_track(item_id: str, name: str) -> Track:
    """
    Return a YouTube Music track.

    :param item_id: The YouTube video id.
    :param name: The title.
    """
    return Track(
        item_id=item_id,
        provider=YOUTUBE,
        name=name,
        duration=200,
        artists=UniqueList([YT_ARTIST]),
        album=YT_ALBUM,
        provider_mappings=_mapping(item_id),
    )


# the same song as Apple's "Anvil of Crom (Apple)", and one Apple doesn't have
YT_TRACKS = {
    "yt-Dup_0001": _yt_track("yt-Dup_0001", "Anvil of Crom"),
    "yt-New_0002": _yt_track("yt-New_0002", "Riders of Doom"),
}


class FakeYoutube:
    """The YouTube Music provider with its audio cache, saving into a temp folder."""

    def __init__(self, folder: pathlib.Path) -> None:
        """
        Initialize.

        :param folder: Where saved files go.
        """
        self.instance_id = YOUTUBE
        self.domain = "ytmusic"
        self.available = True
        self.folder = folder
        self.saved: dict[str, str] = {}
        self.saves = 0
        self._audio_cache = self

    async def lookup(self, item_id: str) -> tuple[str, str] | None:
        """
        Return the saved file and its extension, or None.

        :param item_id: The YouTube video id.
        """
        path = self.saved.get(item_id)
        return (path, "opus") if path else None

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> Any:
        """
        Return stream details for a track that isn't saved yet.

        :param item_id: The YouTube video id.
        :param media_type: The media type.
        """
        assert media_type == MediaType.TRACK
        return SimpleNamespace(
            item_id=item_id,
            stream_type=StreamType.CUSTOM,
            path=None,
            data=SimpleNamespace(expected_size=1000),
        )

    async def get_audio_stream(self, details: Any) -> Any:
        """
        Save the track (as the audio cache does) while yielding its audio.

        :param details: The stream details.
        """
        self.saves += 1
        await asyncio.sleep(0.2)
        path = self.folder / f"{details.item_id}.opus"
        path.write_bytes(b"OggS-youtube-" + details.item_id.encode() * 50)
        self.saved[details.item_id] = str(path)
        yield b"done"


@pytest.fixture
def youtube(
    server: Server,  # noqa: F811
    apple: FakeStreams,  # noqa: ARG001, F811
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeYoutube:
    """
    Give the server a fake YouTube Music provider next to the fake Apple Music one.

    :param server: The running server.
    :param apple: The fake Apple Music streams.
    :param tmp_path: Temporary directory.
    :param monkeypatch: pytest monkeypatch.
    """
    provider = FakeYoutube(tmp_path / "youtube")
    provider.folder.mkdir()
    monkeypatch.setattr(AppleCatalog, "youtube", property(lambda _self: provider))
    assert server.provider.server is not None
    catalog = server.provider.server.catalog
    results = SearchResults(artists=[YT_ARTIST], albums=[YT_ALBUM], tracks=list(YT_TRACKS.values()))
    monkeypatch.setattr(catalog, "search_youtube", AsyncMock(return_value=results))

    def _track(item_id: str) -> Track:
        if item_id.startswith("y."):
            return YT_TRACKS[item_id[2:]]
        return TRACKS[item_id]

    monkeypatch.setattr(catalog, "track", AsyncMock(side_effect=_track))
    return provider


@pytest.mark.usefixtures("youtube")
async def test_youtube_results_only_add_what_apple_lacks(server: Server) -> None:  # noqa: F811
    """YouTube songs follow Apple's; a song Apple also has, and known artists/albums, are left out."""
    found = (await server.call("search3", query="anvil"))["searchResult3"]
    ids = [s["id"] for s in found["song"]]
    assert ids[-1] == "amt-y.yt-New_0002"
    assert "amt-111" in ids[:-1]
    assert "amt-y.yt-Dup_0001" not in ids
    song = found["song"][-1]
    assert song["suffix"] == "opus"
    assert song["contentType"] == "audio/ogg"
    assert song["path"].startswith("YouTube Music/")
    assert song["albumId"] == "ama-y.MPREb_conan"
    assert song["artistId"] == "amr-y.UCbasil"
    assert [a["id"] for a in found["artist"]].count("amr-y.UCbasil") == 0
    assert all(a["id"] != "ama-y.MPREb_conan" for a in found["album"])


async def test_youtube_song_is_saved_before_it_streams(
    server: Server,  # noqa: F811
    youtube: FakeYoutube,
) -> None:
    """Two requests for an unsaved YouTube song wait for one save and get the saved file."""
    first, second = await asyncio.gather(
        server.get("stream", id="amt-y.yt-New_0002"), server.get("stream", id="amt-y.yt-New_0002")
    )
    assert first.status == second.status == 200
    body = await first.read()
    assert body == await second.read()
    assert body.startswith(b"OggS-youtube-yt-New_0002")
    assert first.headers["Content-Type"] == "audio/ogg"
    assert youtube.saves == 1
    part = await server.get("stream", id="amt-y.yt-New_0002", headers={"Range": "bytes=0-3"})
    assert part.status == 206
    assert await part.read() == b"OggS"
    assert youtube.saves == 1


@pytest.mark.usefixtures("youtube")
async def test_youtube_plays_and_stars_go_to_ma(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished play and a star of a YouTube song reach MA's play history and favourites."""
    played = AsyncMock()
    starred = AsyncMock()
    monkeypatch.setattr(server.mass.music, "mark_item_played", played)
    monkeypatch.setattr(server.mass.music, "add_item_to_favorites", starred)
    await server.call("scrobble", id="amt-y.yt-New_0002")
    assert played.await_args is not None
    assert played.await_args.args[0].item_id == "yt-New_0002"
    await server.call("star", id="amt-y.yt-New_0002")
    assert starred.await_args is not None
    assert starred.await_args.args[0] == f"{YOUTUBE}://track/yt-New_0002"


@pytest.mark.usefixtures("youtube")
async def test_youtube_playlists_show_with_their_songs(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YouTube Music playlists in the library show; their songs can be saved and played."""
    playlist = Playlist(
        item_id="PLchill",
        provider=YOUTUBE,
        name="Chillwave",
        owner="someone",
        is_editable=False,
        provider_mappings=_mapping("PLchill", in_library=True),
    )
    added = await server.mass.music.playlists.add_item_to_library(playlist)
    playlist_id = int(added.item_id)
    song = YT_TRACKS["yt-New_0002"].__class__.from_dict(YT_TRACKS["yt-New_0002"].to_dict())
    song.position = 1

    async def _tracks(item_id: str, _provider: str, force_refresh: bool = False) -> Any:  # noqa: ARG001
        assert int(item_id) == playlist_id
        yield song

    monkeypatch.setattr(server.mass.music.playlists, "tracks", _tracks)
    listed = (await server.call("getPlaylists"))["playlists"]["playlist"]
    assert "Chillwave" in [p["name"] for p in listed]
    shown = (await server.call("getPlaylist", id=f"pl-{playlist_id}"))["playlist"]
    assert [e["id"] for e in shown["entry"]] == ["amt-y.yt-New_0002"]
    assert shown["readonly"] is True
    refused = await server.call("deletePlaylist", id=f"pl-{playlist_id}")
    assert refused["error"]["code"] == 50

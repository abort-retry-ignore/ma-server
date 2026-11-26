"""Old song IDs of songs that left the library, and library change dates after removals."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from music_assistant_models.media_items import Artist, ProviderMapping, Track, UniqueList

from music_assistant.providers.subsonic_server.index import song_key
from tests.providers.subsonic_server.test_apple import APPLE, FakeStreams, apple  # noqa: F401
from tests.providers.subsonic_server.test_server import Server, server  # noqa: F401


async def test_old_song_id_plays_the_same_song(server: Server) -> None:  # noqa: F811
    """A song ID that left the library answers with the file of the same title and artist."""
    subsonic = server.provider.server
    assert subsonic is not None
    await subsonic.storage.remember_tracks({99999: ("Anvil of Crom", "Basil Poledouris")})
    same = subsonic.index.track_by_key[song_key("Anvil of Crom", "Basil Poledouris")]
    song = (await server.call("getSong", id="tr-99999"))["song"]
    assert song["id"] == f"tr-{same}"
    response = await server.get("stream", id="tr-99999")
    assert response.status == 200
    assert response.headers["Content-Type"] == "audio/flac"
    # a song nobody has seen stays not found
    assert (await server.call("getSong", id="tr-99998"))["error"]["code"] == 70


@pytest.mark.usefixtures("apple")
async def test_old_song_id_falls_back_to_the_apple_version(
    server: Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a file of the same song, the Apple Music version of the library song is used."""
    subsonic = server.provider.server
    assert subsonic is not None
    await subsonic.storage.remember_tracks({99997: ("Lucky Girl", "Fazerdaze")})
    fazerdaze = Artist(item_id="5", provider="library", name="Fazerdaze", provider_mappings=set())
    library_song = Track(
        item_id="718",
        provider="library",
        name="Lucky Girl",
        artists=UniqueList([fazerdaze]),
        provider_mappings={
            ProviderMapping(item_id="111", provider_domain="apple_music", provider_instance=APPLE)
        },
    )
    monkeypatch.setattr(
        server.mass.music.tracks, "library_items", AsyncMock(return_value=[library_song])
    )
    song = (await server.call("getSong", id="tr-99997"))["song"]
    assert song["id"] == "amt-111"


async def test_removal_moves_the_library_change_date(server: Server) -> None:  # noqa: F811
    """Removing a song moves lastModified, and the removed song is remembered."""
    subsonic = server.provider.server
    assert subsonic is not None
    before = (await server.call("getIndexes"))["indexes"]["lastModified"]
    unchanged = (await server.call("getIndexes", ifModifiedSince=str(before)))["indexes"]
    assert "index" not in unchanged
    track = next(t for t in subsonic.index.tracks.values() if t.name != "Anvil of Crom")
    artist = subsonic.index.first_artist(track)
    await server.mass.music.tracks.remove_item_from_library(track.id)
    await subsonic.index.rebuild()
    assert track.id not in subsonic.index.tracks
    after = (await server.call("getIndexes", ifModifiedSince=str(before)))["indexes"]
    assert after["lastModified"] > before
    assert "index" in after
    assert subsonic.storage.known_track(track.id) == (track.name, artist)

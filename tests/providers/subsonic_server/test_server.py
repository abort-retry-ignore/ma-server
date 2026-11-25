"""
Full-server tests: a real music folder scanned by MA, served to a client over HTTP.

The audio files are short tones generated with ffmpeg, tagged like the files the fork's
caches publish.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
import pytest
from music_assistant_models.enums import MediaType

from music_assistant.mass import MusicAssistant
from music_assistant.providers.subsonic_server import SubsonicServerProvider
from tests.common import wait_for_sync_completion
from tests.conftest import full_mass_context

PASSWORD = "listen-carefully"

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@dataclass
class Server:
    """A running MA with the Subsonic server and a client session for it."""

    mass: MusicAssistant
    provider: SubsonicServerProvider
    base_url: str
    session: aiohttp.ClientSession
    folder: pathlib.Path

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        """
        Call an API method with token login and return the JSON response body.

        :param method: The API method.
        :param params: The method parameters.
        """
        async with self.session.get(
            f"{self.base_url}/rest/{method}.view", params=_auth_params(params)
        ) as resp:
            assert resp.status == 200, await resp.text()
            data = await resp.json(content_type=None)
        return cast("dict[str, Any]", data["subsonic-response"])

    async def get(self, method: str, **params: Any) -> aiohttp.ClientResponse:
        """
        Call an API method and return the raw response (read before leaving).

        :param method: The API method.
        :param params: The method parameters.
        """
        headers = params.pop("headers", None)
        resp = await self.session.get(
            f"{self.base_url}/rest/{method}", params=_auth_params(params), headers=headers
        )
        await resp.read()
        return resp


def _auth_params(params: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Return the parameters with token login added.

    :param params: The method parameters (lists repeat the parameter).
    """
    salt = "c0ffee"
    query: list[tuple[str, str]] = [
        ("u", "dad"),
        ("t", hashlib.md5((PASSWORD + salt).encode()).hexdigest()),
        ("s", salt),
        ("v", "1.16.1"),
        ("c", "pytest"),
        ("f", "json"),
    ]
    for key, value in params.items():
        values = value if isinstance(value, list) else [value]
        query.extend((key, str(v)) for v in values)
    return query


def _make_track(
    path: pathlib.Path, title: str, artist: str, album: str, number: int, genre: str
) -> None:
    """
    Write a short tagged FLAC file.

    :param path: Where to write it.
    :param title: Title tag.
    :param artist: Artist and album artist tag.
    :param album: Album tag.
    :param number: Track number tag.
    :param genre: Genre tag.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={300 + number * 50}:duration=3",
            "-metadata",
            f"title={title}",
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"album_artist={artist}",
            "-metadata",
            f"album={album}",
            "-metadata",
            f"track={number}",
            "-metadata",
            f"genre={genre}",
            "-metadata",
            "date=1982",
            str(path),
        ],
        check=True,
    )


def _make_library(folder: pathlib.Path) -> None:
    """
    Create the test music folder.

    :param folder: The music folder.
    """
    conan = folder / "Music Assistant Cache" / "Basil Poledouris" / "Conan"
    _make_track(
        conan / "01 Anvil of Crom.flac",
        "Anvil of Crom",
        "Basil Poledouris",
        "Conan",
        1,
        "Soundtrack",
    )
    _make_track(
        conan / "02 Riddle of Steel.flac",
        "Riddle of Steel",
        "Basil Poledouris",
        "Conan",
        2,
        "Soundtrack",
    )
    (conan / "01 Anvil of Crom.lrc").write_text("[00:01.00]Crom\n[00:02.50]Strength\n")
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64",
            "-frames:v",
            "1",
            str(conan / "cover.jpg"),
        ],
        check=True,
    )
    fake = folder / "Music Assistant Cache" / "Chrome Nine" / "Anvil of Chrome"
    _make_track(
        fake / "01 Anvil of Chrome.flac",
        "Anvil of Chrome",
        "Chrome Nine",
        "Anvil of Chrome",
        1,
        "Rock",
    )


@pytest.fixture
async def server(tmp_path: pathlib.Path) -> AsyncGenerator[Server]:
    """
    Boot MA with a scanned music folder, a user and the Subsonic server on a free port.

    :param tmp_path: Temporary directory.
    """
    folder = tmp_path / "music"
    _make_library(folder)
    async with full_mass_context(tmp_path) as mass:
        async with wait_for_sync_completion(mass):
            fs_config = await mass.config._create_provider_instance(
                "filesystem_local", {}, setup_data={"path": str(folder)}
            )
            await mass.music.start_sync(providers=[fs_config.instance_id])
        user = await mass.webserver.auth.create_user("dad", display_name="Dad")
        config = await mass.config._create_provider_instance(
            "subsonic_server", {"port": 0, "bind_ip": "127.0.0.1"}
        )
        await mass.config.save_provider_config(
            "subsonic_server",
            {"port": 0, "bind_ip": "127.0.0.1", f"password_{user.user_id}": PASSWORD},
            instance_id=config.instance_id,
        )
        provider = cast("SubsonicServerProvider", mass.get_provider(config.instance_id))
        assert provider.server is not None
        await provider.server.index.rebuild()
        async with aiohttp.ClientSession() as session:
            yield Server(
                mass,
                provider,
                f"http://127.0.0.1:{provider.server.port}",
                session,
                folder,
            )


async def test_ping_and_wrong_password(server: Server) -> None:
    """A correct login answers ok; a wrong one fails with code 40 inside HTTP 200."""
    assert (await server.call("ping"))["status"] == "ok"
    async with server.session.get(
        f"{server.base_url}/rest/ping", params={"u": "dad", "p": "nope", "f": "json"}
    ) as resp:
        assert resp.status == 200
        body = (await resp.json(content_type=None))["subsonic-response"]
    assert body["status"] == "failed"
    assert body["error"]["code"] == 40


async def test_unknown_method_is_404_like_navidrome(server: Server) -> None:
    """Unknown methods and paths get a plain 404."""
    resp = await server.get("getNothing")
    assert resp.status == 404
    async with server.session.get(f"{server.base_url}/api/song") as other:
        assert other.status == 404


async def test_browse_artist_album_and_song(server: Server) -> None:
    """Artists, albums and songs from the folder come out with Navidrome's fields."""
    artists = (await server.call("getArtists"))["artists"]
    names = [a["name"] for group in artists["index"] for a in group["artist"]]
    assert names == ["Basil Poledouris", "Chrome Nine"]
    assert artists["ignoredArticles"].startswith("The El La")
    basil = next(
        a for g in artists["index"] for a in g["artist"] if a["name"] == "Basil Poledouris"
    )
    assert basil["albumCount"] == 1
    assert "/share/img/" in basil["artistImageUrl"]

    artist = (await server.call("getArtist", id=basil["id"]))["artist"]
    album_id = artist["album"][0]["id"]
    album = (await server.call("getAlbum", id=album_id))["album"]
    assert album["name"] == "Conan"
    assert album["songCount"] == 2
    assert [s["title"] for s in album["song"]] == ["Anvil of Crom", "Riddle of Steel"]
    song = album["song"][0]
    assert song["suffix"] == "flac"
    assert song["contentType"] == "audio/flac"
    assert song["path"].startswith("Music Assistant Cache/Basil Poledouris/Conan/")
    assert song["size"] > 0
    assert song["parent"] == album_id
    assert song["artists"] == [{"id": basil["id"], "name": "Basil Poledouris"}]
    assert song["mediaType"] == "song"
    assert (await server.call("getSong", id=song["id"]))["song"]["title"] == "Anvil of Crom"
    missing = await server.call("getAlbum", id="al-999999")
    assert missing["error"]["code"] == 70


async def test_xml_is_the_default_format(server: Server) -> None:
    """Without f=json the response is Subsonic XML."""
    params = [p for p in _auth_params({}) if p[0] != "f"]
    async with server.session.get(
        f"{server.base_url}/rest/getMusicFolders.view", params=params
    ) as r:
        text = await r.text()
        assert r.content_type == "application/xml"
    assert text.startswith('<subsonic-response xmlns="http://subsonic.org/restapi" status="ok"')
    assert '<musicFolder id="1"' in text


async def test_stream_supports_ranges(server: Server) -> None:
    """The file is sent as it is, with Navidrome's headers and byte ranges."""
    song = (await server.call("search3", query="riddle"))["searchResult3"]["song"][0]
    file_bytes = (
        server.folder / "Music Assistant Cache" / song["path"].split("/", 1)[1]
    ).read_bytes()
    full = await server.get("stream", id=song["id"])
    assert full.status == 200
    assert full.headers["Content-Type"] == "audio/flac"
    assert full.headers["Accept-Ranges"] == "bytes"
    assert float(full.headers["X-Content-Duration"]) == pytest.approx(3, abs=1)
    assert await full.read() == file_bytes
    part = await server.get("stream", id=song["id"], headers={"Range": "bytes=10-19"})
    assert part.status == 206
    assert await part.read() == file_bytes[10:20]
    download = await server.get("download", id=song["id"])
    assert "attachment" in download.headers["Content-Disposition"]


async def test_cover_art_from_folder_and_signed_image_urls(server: Server) -> None:
    """Album art falls back to the folder's cover.jpg; signed image URLs need no login."""
    album = (await server.call("getAlbumList2", type="alphabeticalByName", size=5))["albumList2"]
    conan = next(a for a in album["album"] if a["name"] == "Conan")
    art = await server.get("getCoverArt", id=conan["coverArt"], size=32)
    assert art.status == 200
    assert art.headers["Content-Type"].startswith("image/")
    assert (await art.read())[:2] == b"\xff\xd8"
    etag = art.headers["ETag"]
    again = await server.get(
        "getCoverArt", id=conan["coverArt"], headers={"If-None-Match": etag}, size=32
    )
    assert again.status == 304

    info = (await server.call("getAlbumInfo2", id=conan["id"]))["albumInfo"]
    async with server.session.get(info["smallImageUrl"]) as signed:
        assert signed.status == 200
    tampered = info["smallImageUrl"].replace("/share/img/al-", "/share/img/ar-")
    async with server.session.get(tampered) as bad:
        assert bad.status == 404


async def test_search_lists_and_empty_query(server: Server) -> None:
    """Search matches word starts; an empty query lists everything for syncing apps."""
    found = (await server.call("search3", query="anvil"))["searchResult3"]
    assert sorted(s["title"] for s in found["song"]) == ["Anvil of Chrome", "Anvil of Crom"]
    everything = (await server.call("search3", query='""', songCount=10))["searchResult3"]
    assert len(everything["song"]) == 3
    paged = (await server.call("search3", query="", songCount=2, songOffset=2))["searchResult3"]
    assert len(paged["song"]) == 1
    nothing = await server.call("search3", query="zzzz")
    assert nothing["searchResult3"] == {}
    newest = (await server.call("getAlbumList2", type="newest", size=10))["albumList2"]["album"]
    assert len(newest) == 2
    by_genre = (await server.call("getSongsByGenre", genre="Soundtrack"))["songsByGenre"]["song"]
    assert len(by_genre) == 2
    genres = {
        g["value"]: g["songCount"] for g in (await server.call("getGenres"))["genres"]["genre"]
    }
    assert genres["Soundtrack"] == 2


async def test_blocked_track_is_hidden_and_heard_track_promoted(server: Server) -> None:
    """A "wrong song" block hides a track everywhere; a heard track leads a misheard search."""
    found = (await server.call("search3", query="anvil of chrome"))["searchResult3"]["song"]
    fake_id = next(s["id"] for s in found if s["title"] == "Anvil of Chrome")
    fake = await server.mass.music.tracks.get_library_item(int(fake_id.split("-")[1]))
    await server.mass.music.play_memory.block_track(fake)
    assert (await server.call("getSong", id=fake_id))["error"]["code"] == 70
    found = (await server.call("search3", query="anvil"))["searchResult3"]["song"]
    assert [s["title"] for s in found] == ["Anvil of Crom"]

    crom_id = found[0]["id"]
    await server.call("scrobble", id=crom_id, submission="true")
    misheard = (await server.call("search3", query="anvil of chrom"))["searchResult3"]["song"]
    assert misheard[0]["id"] == crom_id


async def test_scrobble_star_and_rating(server: Server) -> None:
    """Plays go into MA's history, stars are MA favourites, ratings are kept per user."""
    song = (await server.call("search3", query="riddle"))["searchResult3"]["song"][0]
    track_id = int(song["id"].split("-")[1])
    now = await server.call("scrobble", id=song["id"], submission="false")
    assert now["status"] == "ok"
    playing = (await server.call("getNowPlaying"))["nowPlaying"]["entry"]
    assert playing[0]["id"] == song["id"]
    await server.call("scrobble", id=song["id"])
    played = await server.mass.music.tracks.get_library_item(track_id)
    assert played.last_played > 0
    assert (await server.call("getSong", id=song["id"]))["song"]["playCount"] == 1

    await server.call("star", id=song["id"])
    assert (await server.mass.music.tracks.get_library_item(track_id)).favorite
    starred = (await server.call("getStarred2"))["starred2"]
    assert [s["id"] for s in starred["song"]] == [song["id"]]
    await server.call("unstar", id=song["id"])
    assert not (await server.mass.music.tracks.get_library_item(track_id)).favorite

    await server.call("setRating", id=song["id"], rating=4)
    assert (await server.call("getSong", id=song["id"]))["song"]["userRating"] == 4


async def test_lyrics_from_lrc_file(server: Server) -> None:
    """Synced lyrics come from the .lrc file next to the audio file."""
    song = (await server.call("search3", query="crom"))["searchResult3"]["song"][0]
    lyrics = (await server.call("getLyricsBySongId", id=song["id"]))["lyricsList"]
    lines = lyrics["structuredLyrics"][0]["line"]
    assert lines == [{"start": 1000, "value": "Crom"}, {"start": 2500, "value": "Strength"}]
    plain = (await server.call("getLyrics", artist="Basil Poledouris", title="Anvil of Crom"))[
        "lyrics"
    ]
    assert plain["value"] == "Crom\nStrength"
    other = await server.call("getLyrics", artist="x", title="nothing")
    assert other["lyrics"] == {"value": ""}


async def test_play_queue_round_trip(server: Server) -> None:
    """A saved play queue comes back for the next app; empty looks like Navidrome's."""
    empty = (await server.call("getPlayQueue"))["playQueue"]
    assert empty == {"username": "dad", "changed": "0001-01-01T00:00:00Z", "changedBy": ""}
    songs = (await server.call("search3", query="", songCount=3))["searchResult3"]["song"]
    ids = [s["id"] for s in songs]
    await server.call("savePlayQueue", id=ids, current=ids[1], position=4200)
    queue = (await server.call("getPlayQueue"))["playQueue"]
    assert [e["id"] for e in queue["entry"]] == ids
    assert queue["current"] == ids[1]
    assert queue["position"] == 4200
    assert queue["changedBy"] == "pytest"
    by_index = (await server.call("getPlayQueueByIndex"))["playQueueByIndex"]
    assert by_index["currentIndex"] == 1


async def test_playlists_create_update_delete(server: Server) -> None:
    """Playlists made in the app are MA playlists."""
    songs = (await server.call("search3", query="", songCount=3))["searchResult3"]["song"]
    created = await server.call(
        "createPlaylist", name="Road trip", songId=[s["id"] for s in songs[:2]]
    )
    playlist = created["playlist"]
    assert playlist["songCount"] == 2
    listed = (await server.call("getPlaylists"))["playlists"]["playlist"]
    assert playlist["id"] in [p["id"] for p in listed]
    await server.call(
        "updatePlaylist", playlistId=playlist["id"], songIndexToRemove=0, songIdToAdd=songs[2]["id"]
    )
    entries = (await server.call("getPlaylist", id=playlist["id"]))["playlist"]["entry"]
    assert [e["id"] for e in entries] == [songs[1]["id"], songs[2]["id"]]  # first one removed
    await server.call("deletePlaylist", id=playlist["id"])
    gone = await server.call("getPlaylist", id=playlist["id"])
    assert gone["error"]["code"] == 70


async def test_user_folders_scan_and_stubs(server: Server) -> None:
    """getUser, folders, scan status and the empty feature lists answer like Navidrome."""
    user = (await server.call("getUser", username="dad"))["user"]
    assert user["streamRole"] is True
    assert user["shareRole"] is False
    assert user["folder"] == [1]
    status = (await server.call("getScanStatus"))["scanStatus"]
    assert status["count"] == 3
    assert (await server.call("getShares"))["shares"] == {}
    assert (await server.call("getPodcasts"))["podcasts"] == {}
    extensions = (await server.call("getOpenSubsonicExtensions"))["openSubsonicExtensions"]
    assert {"name": "formPost", "versions": [1]} in extensions


async def test_index_follows_library_changes(server: Server) -> None:
    """A track removed from the library disappears after the index rebuild."""
    song = (await server.call("search3", query="riddle"))["searchResult3"]["song"][0]
    await server.mass.music.remove_item_from_library(MediaType.TRACK, int(song["id"].split("-")[1]))
    assert server.provider.server is not None
    await server.provider.server.index.rebuild()
    assert (await server.call("getSong", id=song["id"]))["error"]["code"] == 70

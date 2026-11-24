"""Unit tests for the audio cache of Apple Music."""

import asyncio
import base64
import inspect
import json
import logging
import math
import os
import shutil
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientPayloadError
from music_assistant_models.enums import ContentType, ExternalID, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from mutagen.mp4 import MP4

from music_assistant.helpers.tags import async_parse_tags
from music_assistant.providers.apple_music import audio_cache as cache_module
from music_assistant.providers.apple_music import setup
from music_assistant.providers.apple_music.audio_cache import (
    AUDIO_CACHE_INDEX_DIR,
    COVER_RETRY_SECONDS,
    ENV_AUDIO_CACHE_PATH,
    AudioCache,
    AudioDownload,
    relative_file_path,
)
from music_assistant.providers.apple_music.cache_library import CACHE_COPY_GRACE_SECONDS
from music_assistant.providers.apple_music.cached_provider import (
    CachingAppleMusicProvider,
    CachingStreamingManager,
)
from music_assistant.providers.apple_music.provider import AppleMusicProvider
from music_assistant.providers.apple_music.streaming import AppleMusicStreamingManager

INSTANCE = "apple_music--test"
CTRP_URL = "https://aod-ssl.itunes.apple.com/itunes-assets/x/mzaf_A397010985.rphq.aac.wa.m3u8"
KEY_URI = "data:;base64,AAAAABep6CkAHStsI7gv/Q=="
KEY_ID = base64.b64decode(KEY_URI.split(",")[1]).hex()
CHUNKS = [b"ftyp-and-moov", b"-first-fragment", b"-second-fragment"]
DATA = b"".join(CHUNKS)
COVER = b"\xff\xd8\xff\xe0 pretend this is a jpeg"
COVER_URL = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/1000x1000bb.jpg"


def _mapping(item_id: str, name: str) -> ItemMapping:
    return ItemMapping(media_type=MediaType.ARTIST, item_id=item_id, provider=INSTANCE, name=name)


def _track(
    name: str = "Song",
    artist: str = "Artist",
    album_name: str | None = "Album",
    album_artist: str | None = None,
    track_number: int | None = 3,
    disc_number: int | None = 1,
    item_id: str = "1",
) -> Track:
    """Return a track as the Apple Music parser makes it."""
    mappings = {
        ProviderMapping(item_id=item_id, provider_domain="apple_music", provider_instance=INSTANCE)
    }
    album: Album | ItemMapping | None = None
    if album_name and album_artist:
        album = Album(
            item_id="b1",
            provider=INSTANCE,
            name=album_name,
            provider_mappings=mappings,
            artists=UniqueList([_mapping("a2", album_artist)]),
        )
    elif album_name:
        album = ItemMapping(
            media_type=MediaType.ALBUM, item_id="b1", provider=INSTANCE, name=album_name
        )
    track = Track(
        item_id=item_id,
        provider=INSTANCE,
        name=name,
        provider_mappings=mappings,
        artists=UniqueList([_mapping("a1", artist)]),
    )
    if track_number is not None:
        track.track_number = track_number
    if disc_number is not None:
        track.disc_number = disc_number
    if album is not None:
        track.album = album
    return track


class _Response:
    """The part of an aiohttp response the cache reads."""

    def __init__(self, chunks: list[bytes], content_length: int | None, fail: bool) -> None:
        self._chunks = chunks
        self.content_length = content_length
        self._fail = fail
        self.content = SimpleNamespace(iter_chunked=self._iter_chunked)

    def raise_for_status(self) -> None:
        return None

    async def _iter_chunked(self, _size: int) -> AsyncGenerator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._fail and index == 1:
                raise ClientPayloadError("connection lost")
            yield chunk


class _Get:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


def _provider(
    chunks: list[bytes] | None = None,
    content_length: int | None = len(DATA),
    fail: bool = False,
    cover: bytes | None = None,
) -> Any:
    provider: Any = MagicMock(instance_id=INSTANCE)
    provider.logger = logging.getLogger("test_audio_cache")
    response = _Response(CHUNKS if chunks is None else chunks, content_length, fail)
    provider.mass.http_session.get.return_value = _Get(response)
    provider.get_track = AsyncMock(return_value=_track())
    provider.tasks = []

    def create_task(target: Any, *args: Any, **kwargs: Any) -> asyncio.Task[None]:
        kwargs.pop("task_id", None)
        task = asyncio.Task(
            target(*args, **kwargs), loop=asyncio.get_running_loop(), eager_start=True
        )
        provider.tasks.append(task)
        return task

    provider.mass.create_task = create_task
    metadata = provider.mass.metadata
    metadata.get_image_url_for_item = AsyncMock(return_value=COVER_URL if cover else None)
    metadata.get_thumbnail = AsyncMock(return_value=cover)
    return provider


def _mock(cache: AudioCache) -> Any:
    """Return the provider of a cache, which is a mock."""
    return cache.provider


async def _settle(cache: AudioCache) -> None:
    """Wait for what the cache does in the background."""
    await asyncio.gather(*_mock(cache).tasks)


async def _cache(tmp_path: Path, **kwargs: Any) -> AudioCache:
    cache = AudioCache(_provider(**kwargs), str(tmp_path))
    assert await cache.setup()
    return cache


def _stream(cache: AudioCache, track: Track | None = None, item_id: str = "1") -> Any:
    return cache.stream_and_store(
        item_id, AudioDownload(url=CTRP_URL, key_id=KEY_ID), track or _track(item_id=item_id)
    )


def _files(root: Path) -> list[str]:
    """Return what is in the folder tree, without the index."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and AUDIO_CACHE_INDEX_DIR not in path.parts
    )


def _left_in_index(root: Path) -> list[str]:
    return sorted(path.name for path in (root / AUDIO_CACHE_INDEX_DIR).iterdir())


async def test_a_whole_download_is_kept_by_artist_and_album(tmp_path: Path) -> None:
    """The bytes are passed on as they come and a copy is found again afterwards."""
    cache = await _cache(tmp_path)

    received = [chunk async for chunk in _stream(cache)]

    assert received == CHUNKS
    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]
    assert (tmp_path / "Artist" / "Album" / "03 Song.m4a").read_bytes() == DATA
    assert _left_in_index(tmp_path) == ["1.json"]
    assert await cache.lookup("1", KEY_ID) == str(
        (tmp_path / "Artist" / "Album" / "03 Song.m4a").resolve()
    )


async def test_a_folder_cleared_while_running_is_set_up_again_by_the_next_download(
    tmp_path: Path,
) -> None:
    """Emptying the folder by hand, index included, does not stop later tracks being kept."""
    cache = await _cache(tmp_path)
    _ = [chunk async for chunk in _stream(cache)]
    shutil.rmtree(tmp_path / "Artist")
    shutil.rmtree(tmp_path / AUDIO_CACHE_INDEX_DIR)
    assert await cache.lookup("1", KEY_ID) is None

    received = [chunk async for chunk in _stream(cache)]

    assert received == CHUNKS
    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]
    assert _left_in_index(tmp_path) == ["1.json"]
    assert await cache.lookup("1", KEY_ID) is not None


async def test_the_cover_of_the_album_is_kept_next_to_its_tracks(tmp_path: Path) -> None:
    """The cover is a separate file, so the audio file stays as Apple served it."""
    cache = await _cache(tmp_path, cover=COVER)

    _ = [chunk async for chunk in _stream(cache)]
    await _settle(cache)

    assert (tmp_path / "Artist" / "Album" / "cover.jpg").read_bytes() == COVER
    assert (tmp_path / "Artist" / "Album" / "03 Song.m4a").read_bytes() == DATA
    assert _files(tmp_path) == [
        os.path.join("Artist", "Album", "03 Song.m4a"),
        os.path.join("Artist", "Album", "cover.jpg"),
    ]
    _mock(cache).mass.metadata.get_thumbnail.assert_awaited_once_with(
        COVER_URL, provider="builtin", size=0, image_format="JPEG", flatten_transparency=True
    )


async def test_a_second_track_of_the_album_does_not_fetch_the_cover_again(tmp_path: Path) -> None:
    """One cover per album folder."""
    cache = await _cache(tmp_path, cover=COVER)
    other = _track(name="Other", track_number=4, item_id="2")

    _ = [chunk async for chunk in _stream(cache)]
    _ = [chunk async for chunk in _stream(cache, other, item_id="2")]
    await _settle(cache)

    assert len(_files(tmp_path)) == 3
    _mock(cache).mass.metadata.get_thumbnail.assert_awaited_once()


async def test_a_track_without_a_cover_is_kept_without_one(tmp_path: Path) -> None:
    """Not every track has artwork."""
    cache = await _cache(tmp_path)

    _ = [chunk async for chunk in _stream(cache)]
    await _settle(cache)

    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]


async def test_something_that_is_not_a_jpeg_is_not_kept_as_the_cover(tmp_path: Path) -> None:
    """A file named cover.jpg really is a JPEG."""
    cache = await _cache(tmp_path, cover=b"<html>not an image</html>")

    _ = [chunk async for chunk in _stream(cache)]
    await _settle(cache)

    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]


async def test_a_cover_that_can_not_be_fetched_does_not_cost_the_audio(tmp_path: Path) -> None:
    """The audio is kept, and nothing half written stays behind."""
    cache = await _cache(tmp_path, cover=COVER)
    _mock(cache).mass.metadata.get_thumbnail.side_effect = MediaNotFoundError("gone")

    _ = [chunk async for chunk in _stream(cache)]
    await _settle(cache)

    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]
    assert await cache.lookup("1", KEY_ID) is not None


async def test_a_slow_cover_does_not_hold_up_the_end_of_the_track(tmp_path: Path) -> None:
    """The cover is fetched in the background, after the audio is kept."""
    cache = await _cache(tmp_path, cover=COVER)
    release = asyncio.Event()

    async def slow(*_args: Any, **_kwargs: Any) -> bytes:
        await release.wait()
        return COVER

    _mock(cache).mass.metadata.get_thumbnail.side_effect = slow

    received = [chunk async for chunk in _stream(cache)]

    assert received == CHUNKS
    assert await cache.lookup("1", KEY_ID) is not None
    assert not (tmp_path / "Artist" / "Album" / "cover.jpg").exists()
    release.set()
    await _settle(cache)
    assert (tmp_path / "Artist" / "Album" / "cover.jpg").read_bytes() == COVER


@pytest.fixture(autouse=True)
def _forget_cover_attempts() -> None:
    cache_module._COVER_ATTEMPTS.clear()


async def _album_without_cover(tmp_path: Path) -> tuple[AudioCache, str]:
    """Return a cache with a kept track whose album has no cover, and the path of the track."""
    cache = await _cache(tmp_path, cover=COVER)
    _ = [chunk async for chunk in _stream(cache)]
    await _settle(cache)
    (tmp_path / "Artist" / "Album" / "cover.jpg").unlink()
    cache_module._COVER_ATTEMPTS.clear()
    path = await cache.lookup("1", KEY_ID)
    assert path is not None
    return cache, path


async def test_a_track_played_from_the_cache_gets_the_cover_its_album_lacks(
    tmp_path: Path,
) -> None:
    """Albums kept before covers existed, or whose cover failed, get one on the next play."""
    cache, path = await _album_without_cover(tmp_path)

    cache.start_missing_cover("1", path)
    await _settle(cache)

    assert (tmp_path / "Artist" / "Album" / "cover.jpg").read_bytes() == COVER
    _mock(cache).get_track.assert_awaited_with("1")


async def test_an_album_with_a_cover_is_not_asked_about_again(tmp_path: Path) -> None:
    """A cache hit costs nothing extra when the cover is there."""
    cache = await _cache(tmp_path, cover=COVER)
    _ = [chunk async for chunk in _stream(cache)]
    await _settle(cache)
    path = await cache.lookup("1", KEY_ID)
    assert path is not None
    _mock(cache).get_track.reset_mock()
    _mock(cache).mass.metadata.get_thumbnail.reset_mock()

    for _play in range(3):
        cache.start_missing_cover("1", path)
        await _settle(cache)

    _mock(cache).get_track.assert_not_awaited()
    _mock(cache).mass.metadata.get_thumbnail.assert_not_awaited()
    assert cache_module._COVER_ATTEMPTS[os.path.dirname(path)] == math.inf


async def test_a_cover_that_could_not_be_found_is_tried_again_only_after_a_while(
    tmp_path: Path,
) -> None:
    """Every play of an album without artwork does not ask the service again."""
    cache, path = await _album_without_cover(tmp_path)
    cover = tmp_path / "Artist" / "Album" / "cover.jpg"
    _mock(cache).get_track = AsyncMock(side_effect=MediaNotFoundError("gone"))

    for _play in range(3):
        cache.start_missing_cover("1", path)
        await _settle(cache)

    assert _mock(cache).get_track.await_count == 1
    assert not cover.exists()
    cache_module._COVER_ATTEMPTS[os.path.dirname(path)] -= COVER_RETRY_SECONDS + 1
    _mock(cache).get_track = AsyncMock(return_value=_track())
    cache.start_missing_cover("1", path)
    await _settle(cache)
    assert cover.read_bytes() == COVER


async def test_playing_a_cached_track_gets_the_missing_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook is on the play of a track from the cache."""
    cache, _path = await _album_without_cover(tmp_path)
    manager, _ = _stream_manager(cache, monkeypatch)

    details = await manager.get_stream_details("1")
    await _settle(cache)

    assert details.stream_type == StreamType.ENCRYPTED_HTTP
    assert (tmp_path / "Artist" / "Album" / "cover.jpg").read_bytes() == COVER


async def test_what_is_recorded_names_the_track_without_the_key(tmp_path: Path) -> None:
    """The record holds the key id the file is encrypted for, never a key."""
    cache = await _cache(tmp_path)
    _ = [chunk async for chunk in _stream(cache)]

    entry = json.loads((tmp_path / AUDIO_CACHE_INDEX_DIR / "1.json").read_text())

    assert entry["key_id"] == KEY_ID
    assert (entry["artist"], entry["album"], entry["title"]) == ("Artist", "Album", "Song")
    assert entry["size"] == len(DATA)
    assert "decryption_key" not in entry


async def test_a_download_that_stops_early_leaves_nothing(tmp_path: Path) -> None:
    """A skipped track is not kept, and no half file stays behind."""
    cache = await _cache(tmp_path)
    stream = _stream(cache)

    await anext(stream)
    await stream.aclose()

    assert _files(tmp_path) == []
    assert _left_in_index(tmp_path) == []
    assert await cache.lookup("1", KEY_ID) is None


async def test_a_download_that_fails_leaves_nothing(tmp_path: Path) -> None:
    """A connection lost halfway raises and keeps nothing."""
    cache = await _cache(tmp_path, fail=True)

    with pytest.raises(ClientPayloadError):
        _ = [chunk async for chunk in _stream(cache)]

    assert _files(tmp_path) == []
    assert _left_in_index(tmp_path) == []


async def test_a_player_that_stops_after_the_last_chunk_still_gets_a_copy(
    tmp_path: Path,
) -> None:
    """Closing right after the last chunk, with everything announced received, is whole."""
    cache = await _cache(tmp_path)
    stream = _stream(cache)

    for _ in CHUNKS:
        await anext(stream)
    await stream.aclose()

    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]


async def test_without_an_announced_length_only_the_end_makes_it_whole(
    tmp_path: Path,
) -> None:
    """With no length to compare against, closing before the end is not a whole file."""
    cache = await _cache(tmp_path, content_length=None)
    stream = _stream(cache)
    for _ in CHUNKS:
        await anext(stream)
    await stream.aclose()
    assert _files(tmp_path) == []

    _ = [chunk async for chunk in _stream(cache)]

    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]


async def test_a_second_download_of_the_same_track_does_not_write(tmp_path: Path) -> None:
    """While one download of a track is running another only plays, so one copy is made."""
    cache = await _cache(tmp_path)
    first = _stream(cache)
    await anext(first)

    second = [chunk async for chunk in _stream(cache)]
    assert second == CHUNKS
    assert _files(tmp_path) == []

    async for _chunk in first:
        pass

    assert _files(tmp_path) == [os.path.join("Artist", "Album", "03 Song.m4a")]


async def test_a_name_taken_by_another_track_gets_the_item_id(tmp_path: Path) -> None:
    """Two tracks of the same name never replace each other."""
    cache = await _cache(tmp_path)
    _ = [chunk async for chunk in _stream(cache, _track(item_id="1"), "1")]
    _ = [chunk async for chunk in _stream(cache, _track(item_id="2"), "2")]

    assert _files(tmp_path) == [
        os.path.join("Artist", "Album", "03 Song [2].m4a"),
        os.path.join("Artist", "Album", "03 Song.m4a"),
    ]
    assert await cache.lookup("1", KEY_ID) != await cache.lookup("2", KEY_ID)


async def test_a_file_stored_for_another_key_is_dropped(tmp_path: Path) -> None:
    """When Apple serves the track under a new key, the old file is removed to be stored again."""
    cache = await _cache(tmp_path)
    _ = [chunk async for chunk in _stream(cache)]

    assert await cache.lookup("1", "ffff") is None

    assert _files(tmp_path) == []
    assert await cache.lookup("1", KEY_ID) is None


async def test_a_file_of_another_size_is_dropped(tmp_path: Path) -> None:
    """A file that was cut short or changed is not played."""
    cache = await _cache(tmp_path)
    _ = [chunk async for chunk in _stream(cache)]
    target = tmp_path / "Artist" / "Album" / "03 Song.m4a"
    target.write_bytes(DATA[:5])

    assert await cache.lookup("1", KEY_ID) is None

    assert not target.exists()


async def test_a_record_pointing_outside_the_folder_is_ignored(tmp_path: Path) -> None:
    """The path in a record can not lead out of the cache folder."""
    cache = await _cache(tmp_path)
    outside = tmp_path.parent / "outside.m4a"
    outside.write_bytes(DATA)
    record = {"key_id": KEY_ID, "path": "../outside.m4a", "size": len(DATA)}
    (tmp_path / AUDIO_CACHE_INDEX_DIR / "1.json").write_text(json.dumps(record))

    assert await cache.lookup("1", KEY_ID) is None
    assert outside.exists()


async def test_an_unknown_or_unsafe_item_is_not_found(tmp_path: Path) -> None:
    """A track that was never stored, or an id that is not a plain id, is a miss."""
    cache = await _cache(tmp_path)

    assert await cache.lookup("1", KEY_ID) is None
    assert await cache.lookup("../1", KEY_ID) is None


async def test_an_unsafe_item_id_plays_without_being_stored(tmp_path: Path) -> None:
    """An id that can not be a file name is streamed and never written."""
    cache = await _cache(tmp_path)

    received = [chunk async for chunk in _stream(cache, item_id="../escape")]

    assert received == CHUNKS
    assert _files(tmp_path) == []
    assert not (tmp_path.parent / "escape.json").exists()


async def test_a_folder_that_is_not_absolute_is_not_used() -> None:
    """A relative folder would depend on where the server was started."""
    assert not await AudioCache(_provider(), "music/cache").setup()


async def test_a_folder_that_can_not_be_written_is_not_used(tmp_path: Path) -> None:
    """A path that is a file can not hold the cache."""
    blocker = tmp_path / "file"
    blocker.write_text("x")

    assert not await AudioCache(_provider(), str(blocker)).setup()


async def test_setup_removes_only_old_unfinished_downloads(tmp_path: Path) -> None:
    """A download of another instance that is still running is left alone."""
    index = tmp_path / AUDIO_CACHE_INDEX_DIR
    index.mkdir()
    old, fresh = index / "old.part", index / "fresh.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))

    assert await AudioCache(_provider(), str(tmp_path)).setup()

    assert not old.exists()
    assert fresh.exists()


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (_track(), os.path.join("Artist", "Album", "03 Song.m4a")),
        (_track(disc_number=2), os.path.join("Artist", "Album", "2-03 Song.m4a")),
        (_track(track_number=None), os.path.join("Artist", "Album", "Song.m4a")),
        (
            _track(album_artist="Various Artists"),
            os.path.join("Various Artists", "Album", "03 Song.m4a"),
        ),
        (
            _track(name='What?: "Is" <this>', artist="AC/DC"),
            os.path.join("AC_DC", "Album", "03 What__ _Is_ _this_.m4a"),
        ),
        (
            _track(name="..", artist=" . "),
            os.path.join("Unknown Artist", "Album", "03 Unknown Title.m4a"),
        ),
        (_track(album_name=None), os.path.join("Artist", "Unknown Album", "03 Song.m4a")),
        (None, os.path.join("Unknown Artist", "Unknown Album", "Unknown Title.m4a")),
    ],
)
def test_the_file_of_a_track_is_named_by_its_metadata(track: Track | None, expected: str) -> None:
    """The tree is artist, album, then the track with its number."""
    assert relative_file_path(track) == expected


def test_a_long_name_is_cut_on_a_character_boundary() -> None:
    """Names stay far below what file systems allow, without splitting a character."""
    path = relative_file_path(_track(name="é" * 200))

    assert len(Path(path).stem.encode()) <= 110


async def _upstream_key(_self: Any, license_url: str, key_id: bytes, uri: str, item_id: str) -> str:
    del license_url, key_id, uri, item_id
    return "abc123"


def _stream_manager(
    cache: AudioCache | None, monkeypatch: pytest.MonkeyPatch
) -> tuple[CachingStreamingManager, Any]:
    provider: Any = MagicMock(instance_id=INSTANCE)
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.logger = logging.getLogger("test_audio_cache")
    monkeypatch.setattr(AppleMusicStreamingManager, "_get_decryption_key", _upstream_key)
    manager = CachingStreamingManager(provider)
    manager._decrypt_client_id = b"client-id"
    manager._decrypt_private_key = b"private-key"
    manager._fetch_song_stream_metadata = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "hls-key-server-url": "https://key.example",
            "songId": "1",
            "assets": [],
        }
    )
    manager._parse_stream_url_and_uri = AsyncMock(return_value=(CTRP_URL, KEY_URI))  # type: ignore[method-assign]
    manager.audio_cache = cache
    return manager, provider


async def test_a_track_not_in_the_cache_is_fetched_by_us_and_decrypted_by_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file comes over a pipe, with the key handed to ffmpeg and nothing exposed."""
    manager, _ = _stream_manager(await _cache(tmp_path), monkeypatch)

    details = await manager.get_stream_details("1")

    assert details.stream_type == StreamType.CUSTOM
    assert details.extra_input_args == ["-decryption_key", "abc123"]
    assert details.data == AudioDownload(url=CTRP_URL, key_id=KEY_ID, decryption_key="abc123")
    assert details.path is None
    assert details.decryption_key is None
    assert (details.can_seek, details.allow_seek) == (False, True)
    assert details.audio_format.content_type == ContentType.MP4


async def test_a_cached_track_plays_from_its_file_with_a_fresh_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached file is read from disk and decrypted with the key of this play."""
    cache = await _cache(tmp_path)
    _ = [chunk async for chunk in _stream(cache)]
    manager, _provider_mock = _stream_manager(cache, monkeypatch)

    details = await manager.get_stream_details("1")
    await _settle(cache)

    assert details.stream_type == StreamType.ENCRYPTED_HTTP
    assert details.decryption_key == "abc123"
    assert details.path == str((tmp_path / "Artist" / "Album" / "03 Song.m4a").resolve())
    assert (details.can_seek, details.allow_seek) == (True, True)


async def test_without_a_cache_the_url_still_goes_to_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the cache off nothing about streaming changes."""
    manager, _ = _stream_manager(None, monkeypatch)

    details = await manager.get_stream_details("1")

    assert details.stream_type == StreamType.ENCRYPTED_HTTP
    assert details.path == CTRP_URL
    assert details.decryption_key == "abc123"


async def test_a_missing_track_does_not_stop_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the track details can not be fetched the file is kept under unknown names."""
    cache = await _cache(tmp_path)
    manager, provider = _stream_manager(cache, monkeypatch)
    provider.get_track = AsyncMock(side_effect=MediaNotFoundError("gone"))
    cache.provider = _provider()
    details = await manager.get_stream_details("1")

    received = [chunk async for chunk in manager.stream_track_and_store(details)]

    assert received == CHUNKS
    assert _files(tmp_path) == [
        os.path.join("Unknown Artist", "Unknown Album", "Unknown Title.m4a")
    ]


async def test_the_provider_hands_a_track_download_to_the_cache() -> None:
    """A stream with an audio download goes to the cache, anything else is left to the provider."""

    async def _audio(_streamdetails: StreamDetails) -> AsyncGenerator[bytes]:
        yield b"track"

    provider = object.__new__(CachingAppleMusicProvider)
    provider.caching_streams = MagicMock(stream_track_and_store=_audio)
    track = StreamDetails(
        item_id="1",
        provider=INSTANCE,
        audio_format=MagicMock(),
        stream_type=StreamType.CUSTOM,
        data=AudioDownload(url=CTRP_URL, key_id=KEY_ID),
    )
    other = StreamDetails(
        item_id="2", provider=INSTANCE, audio_format=MagicMock(), stream_type=StreamType.CUSTOM
    )

    assert [c async for c in provider.get_audio_stream(track)] == [b"track"]
    with pytest.raises(NotImplementedError):
        _ = [c async for c in provider.get_audio_stream(other)]


async def test_setup_gives_the_provider_that_keeps_audio() -> None:
    """The one change in the files of upstream: the provider that is set up is ours."""
    manifest = MagicMock(domain="apple_music")
    config = MagicMock(instance_id=INSTANCE, name="Apple Music Test")
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )

    provider = await setup(MagicMock(), manifest, config)

    assert isinstance(provider, CachingAppleMusicProvider)
    assert isinstance(provider, AppleMusicProvider)
    assert provider.streaming_manager is provider.caching_streams
    assert isinstance(provider.streaming_manager, CachingStreamingManager)


def test_the_key_function_of_upstream_still_has_the_parameters_we_look_for() -> None:
    """Fails when an update of upstream renames what the key id is taken from."""
    parameters = inspect.signature(AppleMusicStreamingManager._get_decryption_key).parameters

    assert {"key_id", "item_id"} <= set(parameters)


async def test_the_key_id_is_found_by_name_whatever_parameters_upstream_adds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parameter added to the key function upstream does not hide the key id."""

    async def _newer_upstream_key(
        _self: Any, license_url: str, key_id: bytes, uri: str, item_id: str, adam_id: str
    ) -> str:
        del key_id
        return f"{license_url}{uri}{item_id}{adam_id}"

    manager, _ = _stream_manager(None, monkeypatch)
    monkeypatch.setattr(AppleMusicStreamingManager, "_get_decryption_key", _newer_upstream_key)

    key = await manager._get_decryption_key("l", b"\x01\x02", "u", item_id="9", adam_id="a")

    assert key == "lu9a"
    assert manager._key_ids == {"9": "0102"}


async def test_a_key_function_without_a_key_id_still_plays_without_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If upstream renames what the key id is taken from, playing goes on, only uncached."""

    async def _renamed_upstream_key(
        _self: Any, license_url: str, kid: bytes, uri: str, item_id: str
    ) -> str:
        del license_url, kid, uri, item_id
        return "abc123"

    manager, _ = _stream_manager(await _cache(tmp_path), monkeypatch)
    monkeypatch.setattr(AppleMusicStreamingManager, "_get_decryption_key", _renamed_upstream_key)

    details = await manager.get_stream_details("1")

    assert details.stream_type == StreamType.ENCRYPTED_HTTP
    assert details.path == CTRP_URL
    assert details.decryption_key == "abc123"
    assert manager._key_ids == {}


async def test_publish_clear_cache_tags_and_indexes_without_replacing_files(tmp_path: Path) -> None:
    """Publish tagged audio, preserve an existing file, and keep the cache usable."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider()
    provider.mass.music.providers = []
    cache = AudioCache(provider, str(root), str(library))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    shutil.copyfile(Path("tests/fixtures/MyArtist - MyTitle.m4a"), part)
    original_size = part.stat().st_size
    occupied = library / "Artist" / "Album" / "03 Song.m4a"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"my existing music")

    await cache._store("1", KEY_ID, str(part), original_size, _track(), clear=True)
    await _settle(cache)

    published = library / "Artist" / "Album" / "03 Song [1].m4a"
    assert occupied.read_bytes() == b"my existing music"
    assert published.is_file()
    tags: Any = MP4(published).tags  # type: ignore[no-untyped-call]
    assert tags is not None
    assert tags["©nam"] == ["Song"]
    assert tags["©ART"] == ["Artist"]
    assert tags["©alb"] == ["Album"]
    assert tags["trkn"] == [(3, 0)]
    assert await cache.lookup_clear("1") == str(published.resolve())
    assert (root / "Artist" / "Album" / "03 Song.m4a").is_file()
    assert cache.library is not None
    await cache.library.publish_all()
    assert len(list(published.parent.glob("*.m4a"))) == 2
    published.unlink()
    assert await cache.lookup_clear("1") == str(
        (root / "Artist" / "Album" / "03 Song.m4a").resolve()
    )


async def test_published_version_and_isrc_are_readable_by_local_provider(tmp_path: Path) -> None:
    """The local tag parser sees recording identity and version in published audio."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider()
    provider.mass.music.providers = []
    cache = AudioCache(provider, str(root), str(library))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    shutil.copyfile(Path("tests/fixtures/MyArtist - MyTitle.m4a"), part)
    track = _track()
    track.version = "Live"
    track.external_ids.add((ExternalID.ISRC, "USRC17607839"))

    await cache._store("1", KEY_ID, str(part), part.stat().st_size, track, clear=True)
    await _settle(cache)

    published = await cache.lookup_clear("1")
    assert published is not None
    tags = await async_parse_tags(published)
    assert tags.title == "Song (Live)"
    assert tags.album == "Album"
    assert tags.artists == ("Artist",)
    assert "USRC17607839" in tags.isrc


@pytest.mark.parametrize("modified", ["none", "source", "destination"])
async def test_old_cache_copy_is_removed_only_when_unchanged(tmp_path: Path, modified: str) -> None:
    """The move finishes after old stream details expire, without removing changed files."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider()
    provider.mass.music.providers = []
    cache = AudioCache(provider, str(root), str(library))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    shutil.copyfile(Path("tests/fixtures/MyArtist - MyTitle.m4a"), part)
    await cache._store("1", KEY_ID, str(part), part.stat().st_size, _track(), clear=True)
    await _settle(cache)
    original = root / "Artist" / "Album" / "03 Song.m4a"
    if modified == "source":
        original.write_bytes(b"changed by user")
    elif modified == "destination":
        published = library / "Artist" / "Album" / "03 Song.m4a"
        with published.open("r+b") as file:
            file.write(b"x")
    entry = await cache._read_entry("1")
    assert entry is not None
    entry["published_at"] = time.time() - CACHE_COPY_GRACE_SECONDS - 1
    await cache._write_entry("1", entry)
    assert cache.library is not None

    await cache.library._discard_cache_copy("1")

    assert original.exists() is (modified != "none")
    assert await cache.lookup_clear("1") is not None


async def test_publication_links_the_exact_streaming_mapping(tmp_path: Path) -> None:
    """The local provider receives the cached track's original Apple Music identity."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider()
    provider.domain = "apple_music"
    local = MagicMock(domain="filesystem_local", instance_id="filesystem_local--test")
    local.available = True
    local.base_path = str(library)
    local_track = _track(item_id="local/path")
    local_track.provider_mappings.clear()
    local.get_track = AsyncMock(return_value=local_track)
    provider.mass.music.providers = [local]
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.mass.music.tracks.add_item_to_library = AsyncMock()
    cache = AudioCache(provider, str(root), str(library))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    shutil.copyfile(Path("tests/fixtures/MyArtist - MyTitle.m4a"), part)

    await cache._store("1", KEY_ID, str(part), part.stat().st_size, _track(), clear=True)
    await _settle(cache)

    local.get_track.assert_awaited_once_with(os.path.join("Artist", "Album", "03 Song.m4a"))
    assert any(
        mapping.item_id == "1" and mapping.provider_instance == INSTANCE
        for mapping in local_track.provider_mappings
    )
    provider.mass.music.tracks.add_item_to_library.assert_awaited_once_with(local_track)


async def test_new_cover_refreshes_the_local_album(tmp_path: Path) -> None:
    """A cover downloaded after the track is indexed is added to the local album."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider(cover=COVER)
    provider.domain = "apple_music"
    local = MagicMock(domain="filesystem_local", instance_id="filesystem_local--test")
    local.available = True
    local.base_path = str(library)
    local_track = _track(item_id="local/path", album_artist="Artist")
    local.get_track = AsyncMock(return_value=local_track)
    local.cache.delete = AsyncMock()
    provider.mass.music.providers = [local]
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    provider.mass.music.tracks.add_item_to_library = AsyncMock()
    provider.mass.music.albums.add_item_to_library = AsyncMock()
    cache = AudioCache(provider, str(root), str(library))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    shutil.copyfile(Path("tests/fixtures/MyArtist - MyTitle.m4a"), part)

    await cache._store("1", KEY_ID, str(part), part.stat().st_size, _track(), clear=True)
    await _settle(cache)

    assert (library / "Artist" / "Album" / "cover.jpg").read_bytes() == COVER
    assert local.cache.delete.await_count == 4
    provider.mass.music.albums.add_item_to_library.assert_awaited_once_with(local_track.album)
    assert local.get_track.await_count == 2


async def test_invalid_cached_file_is_not_published_or_lost(tmp_path: Path) -> None:
    """A tagging error leaves the existing cache entry and bytes untouched."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider()
    provider.mass.music.providers = []
    cache = AudioCache(provider, str(root), str(library))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    part.write_bytes(DATA)

    await cache._store("1", KEY_ID, str(part), len(DATA), _track(), clear=True)
    await _settle(cache)

    original = root / "Artist" / "Album" / "03 Song.m4a"
    assert original.read_bytes() == DATA
    assert await cache.lookup_clear("1") == str(original.resolve())
    assert not list(library.rglob("*.m4a"))


async def test_backfill_existing_clear_entry_and_ignore_encrypted(tmp_path: Path) -> None:
    """Backfill uses the stored metadata and does not publish encrypted audio."""
    root = tmp_path / "cache"
    library = tmp_path / "library"
    provider = _provider()
    provider.mass.music.providers = []
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    cache = AudioCache(provider, str(root))
    assert await cache.setup()
    part = root / AUDIO_CACHE_INDEX_DIR / "1.part"
    shutil.copyfile(Path("tests/fixtures/MyArtist - MyTitle.m4a"), part)
    await cache._store("1", KEY_ID, str(part), part.stat().st_size, _track(), clear=True)
    await _settle(cache)
    part.write_bytes(DATA)
    await cache._store("2", KEY_ID, str(part), len(DATA), _track(item_id="2"), clear=False)
    await _settle(cache)
    cache = AudioCache(provider, str(root), str(library))
    assert cache.library is not None

    await cache.library.publish_all()

    published = library / "Artist" / "Album" / "03 Song.m4a"
    tags: Any = MP4(published).tags  # type: ignore[no-untyped-call]
    assert tags is not None
    assert tags["©nam"] == ["Song"]
    assert tags["trkn"] == [(3, 0)]
    assert await cache.lookup_clear("1") == str(published.resolve())
    assert len(list(library.rglob("*.m4a"))) == 1
    assert await cache.lookup("2", KEY_ID) is not None


async def test_the_audio_cache_is_opened_when_a_folder_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder is given in the environment, next to the mount it belongs to."""
    manager, _ = _stream_manager(None, monkeypatch)
    monkeypatch.setenv(ENV_AUDIO_CACHE_PATH, str(tmp_path))

    await manager.initialize()

    assert manager.audio_cache is not None
    assert manager.audio_cache.root == str(tmp_path)
    assert (tmp_path / AUDIO_CACHE_INDEX_DIR).is_dir()


@pytest.mark.parametrize("value", [None, "", "  ", "relative/folder"])
async def test_the_audio_cache_stays_off_without_a_usable_folder(
    value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is set, or what is set can not be used: streaming is the plain upstream one."""
    manager, _ = _stream_manager(None, monkeypatch)
    monkeypatch.delenv(ENV_AUDIO_CACHE_PATH, raising=False)
    if value is not None:
        monkeypatch.setenv(ENV_AUDIO_CACHE_PATH, value)

    await manager.initialize()

    assert manager.audio_cache is None


async def test_a_stream_that_is_not_an_encrypted_file_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Radio and unencrypted library tracks are not cached, and whatever upstream adds is not ours."""
    manager, _ = _stream_manager(await _cache(tmp_path), monkeypatch)
    plain = StreamDetails(
        item_id="1",
        provider=INSTANCE,
        audio_format=MagicMock(),
        stream_type=StreamType.HTTP,
        path="https://example.com/a.mp3",
    )
    monkeypatch.setattr(
        AppleMusicStreamingManager, "get_stream_details", AsyncMock(return_value=plain)
    )

    assert await manager.get_stream_details("1") is plain


async def test_a_track_without_a_known_key_id_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the key id could not be noted the cache is not used for that play, it still plays."""
    manager, _ = _stream_manager(await _cache(tmp_path), monkeypatch)
    manager._get_decryption_key = AsyncMock(return_value="abc123")  # type: ignore[method-assign]

    details = await manager.get_stream_details("1")

    assert details.stream_type == StreamType.ENCRYPTED_HTTP
    assert details.path == CTRP_URL
    assert details.decryption_key == "abc123"

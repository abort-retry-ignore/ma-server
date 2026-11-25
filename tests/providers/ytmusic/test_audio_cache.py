"""Tests for the private YouTube Music audio cache."""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession, web
from music_assistant_models.enums import ContentType, ExternalID, MediaType, StreamType
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    ProviderMapping,
    Track,
    UniqueList,
)
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus

from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.providers.ytmusic import YoutubeMusicProvider
from music_assistant.providers.ytmusic.audio_cache import (
    YoutubeAudioCache,
    YoutubeCacheHit,
    YoutubeDownload,
    _is_whole_response,
    provider_cache_root,
    selected_extension,
)
from music_assistant.providers.ytmusic.helpers import YTM_THROTTLER, _run_ytmusic

INSTANCE = "ytmusic--test"
ITEM_ID = "abc12345678"


@contextlib.asynccontextmanager
async def _source(
    data: bytes, *, status: int = 200, content_range: str | None = None
) -> AsyncIterator[tuple[str, ClientSession, list[dict[str, str]]]]:
    """Serve fixture bytes locally and count requests without contacting a music service."""
    requests: list[dict[str, str]] = []

    async def handle(request: web.Request) -> web.Response:
        requests.append(dict(request.headers))
        headers = {"Content-Range": content_range} if content_range else None
        return web.Response(body=data, status=status, headers=headers)

    app = web.Application()
    app.router.add_get("/audio", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    try:
        async with ClientSession() as session:
            yield f"http://127.0.0.1:{port}/audio", session, requests
    finally:
        await runner.cleanup()


def _track() -> Track:
    mapping = ProviderMapping(
        item_id=ITEM_ID, provider_domain="ytmusic", provider_instance=INSTANCE
    )
    artist = ItemMapping(media_type=MediaType.ARTIST, item_id="a", provider=INSTANCE, name="Artist")
    album = ItemMapping(media_type=MediaType.ALBUM, item_id="b", provider=INSTANCE, name="Album")
    track = Track(
        item_id="42",
        provider="library",
        name="Song",
        version="Live",
        artists=UniqueList([artist]),
        album=album,
        track_number=3,
        disc_number=1,
        duration=6,
        provider_mappings={mapping},
    )
    track.external_ids.add((ExternalID.ISRC, "USRC17607839"))
    return track


def _provider(session: ClientSession, track: Track | None = None) -> MagicMock:
    provider = MagicMock()
    provider.instance_id = INSTANCE
    provider.domain = "ytmusic"
    provider.logger = logging.getLogger("test.ytmusic.cache")
    provider.mass.http_session = session
    provider.mass.music.providers = []
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=track)
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.create_task = MagicMock()
    provider.mass.call_later = MagicMock()
    return provider


def _real_provider(mass: MagicMock) -> YoutubeMusicProvider:
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    return YoutubeMusicProvider(mass, MagicMock(domain="ytmusic"), config)


def _audio_hash(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    return subprocess.check_output(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ]
    ).strip()


def _make_audio(path: Path, codec: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            codec,
            "-y",
            str(path),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    ("source", "codec", "expected"),
    [
        ("m4a", "mp4a.40.2", "m4a"),
        ("webm", "opus", "opus"),
        ("webm", "vorbis", "ogg"),
        ("mp3", "mp3", "mp3"),
        ("flac", "flac", "flac"),
        ("webm", "unknown", None),
    ],
)
def test_selected_extension_preserves_supported_codecs(
    source: str, codec: str, expected: str | None
) -> None:
    """Use a compatible compact container without demanding a particular format."""
    assert selected_extension({"audio_ext": source, "acodec": codec}) == expected
    assert selected_extension({"audio_ext": source, "acodec": codec, "protocol": "m3u8"}) is None


def test_instance_root_and_range_safety(tmp_path: Path) -> None:
    """Instances cannot escape their cache, and partial range responses are rejected."""
    assert provider_cache_root(str(tmp_path), INSTANCE) == str(tmp_path / INSTANCE)
    assert provider_cache_root("relative", INSTANCE) is None
    assert provider_cache_root(str(tmp_path), "../escape") is None
    assert provider_cache_root(str(tmp_path), "..") is None
    assert _is_whole_response(200, None, 100)
    assert _is_whole_response(206, "bytes 0-99/100", 100)
    assert not _is_whole_response(206, "bytes 0-49/100", 50)
    assert not _is_whole_response(206, None, 100)


@pytest.mark.parametrize(
    ("source_suffix", "codec", "extension"),
    [
        ("m4a", "aac", "m4a"),
        ("webm", "libopus", "opus"),
        ("webm", "libvorbis", "ogg"),
        ("mp3", "libmp3lame", "mp3"),
        ("flac", "flac", "flac"),
    ],
)
async def test_complete_stream_is_published_without_reencoding(
    tmp_path: Path, source_suffix: str, codec: str, extension: str
) -> None:
    """Store complete source audio once, tag it, and preserve its encoded packets."""
    source = tmp_path / f"source.{source_suffix}"
    _make_audio(source, codec)
    data = source.read_bytes()
    async with _source(data) as (url, session, requests):
        provider = _provider(session, _track())
        cache = YoutubeAudioCache(provider, str(tmp_path / "cache"), str(tmp_path / "library"))
        assert await cache.setup()
        download = YoutubeDownload(
            url=url, source_format=source_suffix, extension=extension, expected_size=len(data)
        )
        received = b"".join([chunk async for chunk in cache.stream_and_store(ITEM_ID, download)])
        assert received == data
        cached = await cache.lookup(ITEM_ID)
        assert cached is not None
        path, actual_extension = cached
        assert actual_extension == extension
        assert Path(path).is_file()
        assert _audio_hash(source) == _audio_hash(Path(path))
        tags = await async_parse_tags(path)
        assert tags.title == "Song (Live)"
        assert tags.album == "Album"
        assert tags.artists == ("Artist",)
        assert "USRC17607839" in tags.isrc
        assert len(requests) == 1
        assert requests[0]["Accept-Encoding"] == "identity"
        index_data = (Path(cache._index_dir) / f"{ITEM_ID}.json").read_text()
        assert url not in index_data
        if extension == "m4a":
            audio: Any = MP4(path)  # type: ignore[no-untyped-call]
            assert audio.tags["----:com.apple.iTunes:MA YouTube Music ID"] == [ITEM_ID.encode()]
            assert "----:com.apple.iTunes:MA Apple Music ID" not in audio.tags
        elif extension == "opus":
            audio = OggOpus(path)  # type: ignore[no-untyped-call]
            assert audio["MA_YOUTUBE_MUSIC_ID"] == [ITEM_ID]


@pytest.mark.parametrize("mode", ["partial_range", "wrong_size", "interrupted", "short_track"])
async def test_partial_or_interrupted_stream_is_never_cached(tmp_path: Path, mode: str) -> None:
    """A finished partial HTTP response or early consumer close is not a complete track."""
    source = tmp_path / "source.m4a"
    _make_audio(source, "aac")
    data = source.read_bytes()
    status = 206 if mode == "partial_range" else 200
    content_range = f"bytes 0-{len(data) - 1}/{len(data) + 20}" if status == 206 else None
    async with _source(data, status=status, content_range=content_range) as (
        url,
        session,
        _requests,
    ):
        track = _track()
        if mode == "short_track":
            track.duration = 120
        cache = YoutubeAudioCache(_provider(session, track), str(tmp_path / "cache"), None)
        assert await cache.setup()
        download = YoutubeDownload(
            url=url,
            source_format="m4a",
            extension="m4a",
            expected_size=len(data) + 1 if mode == "wrong_size" else None,
        )
        async with contextlib.aclosing(cache.stream_and_store(ITEM_ID, download)) as stream:
            if mode == "interrupted":
                assert await anext(stream)
            else:
                _ = b"".join([chunk async for chunk in stream])
        assert await cache.lookup(ITEM_ID) is None
        assert not list(Path(cache._index_dir).glob("*.part*"))


@pytest.mark.parametrize(
    ("source_format", "codec", "extension", "content_type"),
    [("m4a", "aac", "m4a", ContentType.M4A), ("webm", "libopus", "opus", ContentType.UNKNOWN)],
)
async def test_custom_stream_is_decodable_while_it_is_cached(
    tmp_path: Path, source_format: str, codec: str, extension: str, content_type: ContentType
) -> None:
    """The bytes provided to the playback decoder remain playable while being cached."""
    source = tmp_path / f"source.{source_format}"
    _make_audio(source, codec)
    data = source.read_bytes()
    async with _source(data) as (url, session, _requests):
        cache = YoutubeAudioCache(_provider(session, _track()), str(tmp_path / "cache"), None)
        assert await cache.setup()
        download = YoutubeDownload(url=url, source_format=source_format, extension=extension)
        input_format = AudioFormat(content_type=content_type)
        output_format = AudioFormat(
            content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
        )
        async with contextlib.aclosing(
            get_ffmpeg_stream(
                cache.stream_and_store(ITEM_ID, download), input_format, output_format
            )
        ) as stream:
            pcm = b"".join([chunk async for chunk in stream])
        assert len(pcm) > 0
        assert await cache.lookup(ITEM_ID) is not None


async def test_cache_hit_skips_youtube_and_invalidation_keeps_user_file(tmp_path: Path) -> None:
    """A local hit avoids URL resolution; invalidation removes no audio."""
    source = tmp_path / "source.m4a"
    _make_audio(source, "aac")
    data = source.read_bytes()
    async with _source(data) as (url, session, requests):
        mock = _provider(session, _track())
        cache = YoutubeAudioCache(mock, str(tmp_path / "cache"), str(tmp_path / "library"))
        assert await cache.setup()
        download = YoutubeDownload(url=url, source_format="m4a", extension="m4a")
        _ = [chunk async for chunk in cache.stream_and_store(ITEM_ID, download)]
        cached = await cache.lookup(ITEM_ID)
        assert cached is not None
        provider = _real_provider(mock.mass)
        provider._audio_cache = cache
        provider._get_stream_format = AsyncMock(side_effect=AssertionError("unexpected request"))  # type: ignore[method-assign]
        details = await provider.get_stream_details(ITEM_ID, MediaType.TRACK)
        assert details.stream_type == StreamType.LOCAL_FILE
        assert not details.is_realtime
        assert isinstance(details.data, YoutubeCacheHit)
        assert details.path == cached[0]
        assert "path" not in details.to_dict()
        assert "data" not in details.to_dict()
        provider._get_stream_format.assert_not_awaited()
        assert len(requests) == 1
        assert await provider.invalidate_cached_stream(details)
        assert Path(cached[0]).exists()
        assert await cache.lookup(ITEM_ID) is None


async def test_later_cached_metadata_publishes_without_a_service_request(tmp_path: Path) -> None:
    """A cache entry without metadata is published once MA already has track details."""
    source = tmp_path / "source.m4a"
    _make_audio(source, "aac")
    data = source.read_bytes()
    library = tmp_path / "library"
    async with _source(data) as (url, session, requests):
        provider = _provider(session)
        cache = YoutubeAudioCache(provider, str(tmp_path / "cache"), str(library))
        assert await cache.setup()
        download = YoutubeDownload(url=url, source_format="m4a", extension="m4a")
        _ = [chunk async for chunk in cache.stream_and_store(ITEM_ID, download)]
        cached = await cache.lookup(ITEM_ID)
        assert cached is not None
        assert Path(cached[0]).is_relative_to(tmp_path / "cache")
        assert not list(library.rglob("*.m4a"))
        provider.mass.cache.get = AsyncMock(return_value=_track())
        assert cache.library is not None
        await cache.library.publish_all()
        published = await cache.lookup(ITEM_ID)
        assert published is not None
        assert Path(published[0]) == library / "Artist" / "Album" / "03 Song.m4a"
        assert len(requests) == 1
        provider.get_track.assert_not_called()


async def test_publication_links_the_exact_youtube_mapping_and_preserves_cover(
    tmp_path: Path,
) -> None:
    """The local file receives its source identity and existing artwork is never replaced."""
    source = tmp_path / "source.m4a"
    _make_audio(source, "aac")
    data = source.read_bytes()
    library = tmp_path / "library"
    async with _source(data) as (url, session, _requests):
        provider = _provider(session, _track())
        local = MagicMock(domain="filesystem_local", instance_id="filesystem_local--test")
        local.available = True
        local.base_path = str(library)
        local_track = _track()
        local_track.provider_mappings.clear()
        local.get_track = AsyncMock(return_value=local_track)
        provider.mass.music.providers = [local]
        provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(
            side_effect=lambda _item, instance: _track() if instance == INSTANCE else None
        )
        provider.mass.music.tracks.add_item_to_library = AsyncMock()
        provider.mass.metadata.get_image_url_for_item = AsyncMock(return_value="cover")
        provider.mass.metadata.get_thumbnail = AsyncMock(return_value=b"\xff\xd8\xfftest-jpeg")
        cache = YoutubeAudioCache(provider, str(tmp_path / "cache"), str(library))
        assert await cache.setup()
        download = YoutubeDownload(url=url, source_format="m4a", extension="m4a")
        _ = [chunk async for chunk in cache.stream_and_store(ITEM_ID, download)]
        cached = await cache.lookup(ITEM_ID)
        assert cached is not None
        assert any(
            m.item_id == ITEM_ID
            and m.provider_instance == INSTANCE
            and m.provider_domain == "ytmusic"
            for m in local_track.provider_mappings
        )
        provider.mass.music.tracks.add_item_to_library.assert_awaited_once_with(local_track)
        assert cache.library is not None
        cache.library.refresh_cover = AsyncMock()  # type: ignore[method-assign]
        await cache._store_cover(ITEM_ID, _track(), cached[0])
        cover = Path(cached[0]).parent / "cover.jpg"
        assert cover.read_bytes() == b"\xff\xd8\xfftest-jpeg"
        cache.library.refresh_cover.assert_awaited_once_with(ITEM_ID)
        cover.write_bytes(b"my artwork")
        cache._cover_attempts.clear()
        await cache._store_cover(ITEM_ID, _track(), cached[0])
        assert cover.read_bytes() == b"my artwork"
        cover.unlink()
        cache._cover_attempts.clear()
        await cache._store_cover(ITEM_ID, _track(), cached[0])
        assert not cover.exists()
        provider.mass.metadata.get_thumbnail.assert_awaited_once()


async def test_separate_library_and_cache_roots_are_required(tmp_path: Path) -> None:
    """A scanned library must not contain cache indices or temporary downloads."""
    provider = _provider(MagicMock())
    library = tmp_path / "music"
    nested = YoutubeAudioCache(provider, str(library / "cache"), str(library))
    assert not await nested.setup()
    nested_library = YoutubeAudioCache(provider, str(tmp_path), str(tmp_path / "library"))
    assert not await nested_library.setup()


async def test_cache_failure_does_not_replace_existing_files(tmp_path: Path) -> None:
    """A pre-existing library file and its sidecars remain untouched."""
    source = tmp_path / "source.m4a"
    _make_audio(source, "aac")
    data = source.read_bytes()
    library = tmp_path / "library"
    folder = library / "Artist" / "Album"
    folder.mkdir(parents=True)
    occupied = folder / "03 Song.m4a"
    occupied.write_bytes(b"my audio")
    (folder / "cover.jpg").write_bytes(b"my artwork")
    async with _source(data) as (url, session, _requests):
        cache = YoutubeAudioCache(
            _provider(session, _track()), str(tmp_path / "cache"), str(library)
        )
        assert await cache.setup()
        download = YoutubeDownload(url=url, source_format="m4a", extension="m4a")
        _ = [chunk async for chunk in cache.stream_and_store(ITEM_ID, download)]
        cached = await cache.lookup(ITEM_ID)
        assert cached is not None
        assert cached[0].endswith(f"03 Song [{ITEM_ID}].m4a")
        assert occupied.read_bytes() == b"my audio"
        assert (folder / "cover.jpg").read_bytes() == b"my artwork"


async def test_cache_miss_is_private_and_seek_uses_the_original_source() -> None:
    """Only a full, unseeked custom stream is captured; seeks retain HTTP behavior."""
    mass = MagicMock()
    provider = _real_provider(mass)
    cache = MagicMock(spec=YoutubeAudioCache)
    cache.lookup = AsyncMock(return_value=None)
    provider._audio_cache = cache
    secret_url = "https://example.invalid/audio?secret=value"
    stream_format = {
        "url": secret_url,
        "format": "test",
        "audio_ext": "webm",
        "acodec": "opus",
        "filesize": 1234,
        "http_headers": {"User-Agent": "test"},
    }
    with patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)):
        details = await provider.get_stream_details(ITEM_ID, MediaType.TRACK)
    assert details.stream_type == StreamType.CUSTOM
    assert details.path is None
    assert details.is_realtime
    assert details.can_seek
    assert isinstance(details.data, YoutubeDownload)
    assert details.data.extension == "opus"
    assert details.data.expected_size == 1234
    assert secret_url not in repr(details)
    assert secret_url not in repr(details.data)
    assert "data" not in details.to_dict()
    fallback = await provider.fallback_failed_stream(details)
    assert fallback is not None
    assert fallback.stream_type == StreamType.HTTP
    assert fallback.path == secret_url
    assert "path" not in fallback.to_dict()
    assert "data" not in fallback.to_dict()
    cache.lookup.return_value = ("/local/song.opus", "opus")
    cached_fallback = await provider.fallback_failed_stream(details)
    assert cached_fallback is not None
    assert cached_fallback.stream_type == StreamType.LOCAL_FILE
    assert cached_fallback.path == "/local/song.opus"
    assert isinstance(cached_fallback.data, YoutubeCacheHit)
    assert not cached_fallback.is_realtime
    mass.get_provider.return_value = provider
    provider.available = True
    controller = StreamsAudio(mass)
    source, seek, _args = await controller._resolve_media_stream_source(details, 40, [])
    assert source == secret_url
    assert seek == 40


async def test_stream_and_metadata_requests_share_throttling() -> None:
    """Both service request families pass through the same rate limiter."""

    class FakeYoutubeDL:
        def __init__(self, _options: dict[str, Any]) -> None:
            self.cookiejar: list[Any] = []

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            assert not download
            return {"formats": []}

        def build_format_selector(self, _selector: str) -> Any:
            return lambda _context: iter([{"format_id": "141"}])

    provider = _real_provider(MagicMock())
    provider._netscape_cookie = ""
    provider._po_token_server_url = "http://localhost:4416"
    provider._yt_dlp_module = MagicMock(YoutubeDL=FakeYoutubeDL)
    with patch.object(YTM_THROTTLER, "acquire", AsyncMock()) as acquire:
        assert await _run_ytmusic(lambda: "ok") == "ok"
        assert (await provider._get_stream_format(ITEM_ID))["format_id"] == "141"
    assert acquire.await_count == 2


@pytest.mark.parametrize("premium_check", [False, True])
async def test_format_selection_options_are_separate_from_premium_check(
    premium_check: bool,
) -> None:
    """Playback prefers high-bitrate Opus while the entitlement probe still tests M4A."""
    options_seen: list[dict[str, Any]] = []
    selectors_seen: list[str] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, Any]) -> None:
            options_seen.append(options)
            self.cookiejar: list[Any] = []

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            assert not download
            return {"formats": []}

        def build_format_selector(self, selector: str) -> Any:
            selectors_seen.append(selector)
            return lambda _context: iter([{"format_id": "141"}])

    provider = _real_provider(MagicMock())
    provider._netscape_cookie = ""
    provider._po_token_server_url = "http://localhost:4416"
    provider._yt_dlp_module = MagicMock(YoutubeDL=FakeYoutubeDL)
    with patch.object(YTM_THROTTLER, "acquire", AsyncMock()):
        await provider._get_stream_format(ITEM_ID, require_login=premium_check)
    if premium_check:
        assert selectors_seen == ["m4a/bestaudio"]
        assert "format_sort_force" not in options_seen[0]
    else:
        assert selectors_seen == [
            "bestaudio[acodec^=opus]/bestaudio[acodec~='^(mp4a|aac)']/bestaudio"
        ]
        assert options_seen[0]["format_sort"] == ["abr", "asr"]
        assert options_seen[0]["format_sort_force"] is True


async def test_disabled_cache_and_podcast_keep_existing_stream_behavior() -> None:
    """A disabled cache does not alter a track or podcast's regular HTTP details."""
    mass = MagicMock()
    provider = _real_provider(mass)
    provider._audio_cache = None
    stream_format = {
        "url": "https://example.invalid/audio?secret=x",
        "format": "test",
        "audio_ext": "m4a",
    }
    with patch.object(provider, "_get_stream_format", AsyncMock(return_value=stream_format)):
        track = await provider.get_stream_details(ITEM_ID, MediaType.TRACK)
        episode = await provider.get_stream_details(f"podcast|{ITEM_ID}", MediaType.PODCAST_EPISODE)
    assert track.stream_type == StreamType.HTTP
    assert episode.stream_type == StreamType.HTTP
    assert track.is_realtime
    assert episode.is_realtime
    assert "path" not in track.to_dict()

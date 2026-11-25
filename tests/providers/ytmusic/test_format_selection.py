"""Tests for YouTube Music's preferred streaming formats."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import UnplayableMediaError

yt_dlp = pytest.importorskip("yt_dlp")

from music_assistant.providers.ytmusic import YoutubeMusicProvider  # noqa: E402
from music_assistant.providers.ytmusic.helpers import YTM_THROTTLER  # noqa: E402


def _format(item_id: str, codec: str, bitrate: int, *, quality: int = 0) -> dict[str, Any]:
    """Return a self-contained yt-dlp audio format."""
    return {
        "format_id": item_id,
        "ext": "webm" if codec == "opus" else "m4a" if codec.startswith("mp4a") else "ogg",
        "protocol": "https",
        "vcodec": "none",
        "acodec": codec,
        "abr": bitrate,
        "asr": 48000,
        "quality": quality,
    }


@pytest.mark.parametrize(
    ("formats", "expected"),
    [
        (
            [
                _format("opus-96", "opus", 96),
                _format("opus-160", "opus", 160),
                _format("aac-256", "mp4a.40.2", 256),
            ],
            "opus-160",
        ),
        (
            [
                _format("opus-96", "opus", 96, quality=5),
                _format("opus-160", "opus", 160, quality=1),
            ],
            "opus-160",
        ),
        ([_format("aac-128", "mp4a.40.2", 128), _format("aac-256", "aac", 256)], "aac-256"),
        ([_format("vorbis-192", "vorbis", 192)], "vorbis-192"),
    ],
)
async def test_prefers_highest_bitrate_opus_then_aac(
    formats: list[dict[str, Any]], expected: str
) -> None:
    """Choose Opus first, then the best AAC, and preserve the ordinary fallback."""
    provider, _options = _provider_with_formats(formats)
    with patch.object(YTM_THROTTLER, "acquire", AsyncMock()):
        selected = await provider._get_stream_format("video-id")
    assert selected["format_id"] == expected


async def test_premium_check_still_selects_141_with_opus_present() -> None:
    """The Premium entitlement check remains independent of playback codec preference."""
    formats = [_format("251", "opus", 160), _format("141", "mp4a.40.2", 256)]
    provider, options = _provider_with_formats(formats)
    with patch.object(YTM_THROTTLER, "acquire", AsyncMock()):
        assert await provider._user_has_ytm_premium()
    assert "format_sort_force" not in options[0]


async def test_no_audio_formats_still_reports_unplayable() -> None:
    """The selector fails cleanly when no audio stream exists."""
    provider, _options = _provider_with_formats([])
    with (
        patch.object(YTM_THROTTLER, "acquire", AsyncMock()),
        pytest.raises(UnplayableMediaError, match="No stream formats found"),
    ):
        await provider._get_stream_format("video-id")


def _provider_with_formats(
    formats: list[dict[str, Any]],
) -> tuple[YoutubeMusicProvider, list[dict[str, Any]]]:
    """Use yt-dlp's actual selector against a fake, offline extraction result."""
    options_seen: list[dict[str, Any]] = []

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, Any]) -> None:
            options_seen.append(options)
            self.cookiejar: list[Any] = []
            offline_options = dict(options)
            offline_options.pop("cookiefile", None)
            self.ydl = yt_dlp.YoutubeDL(offline_options)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def extract_info(self, _url: str, *, download: bool) -> dict[str, Any]:
            assert not download
            info = {"id": "fixture", "formats": deepcopy(formats)}
            if info["formats"]:
                self.ydl.sort_formats(info)
            return info

        def build_format_selector(self, selector: str) -> Any:
            return self.ydl.build_format_selector(selector)

    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    provider = YoutubeMusicProvider(MagicMock(), MagicMock(domain="ytmusic"), config)
    provider._netscape_cookie = ""
    provider._po_token_server_url = "http://localhost:4416"
    provider._yt_dlp_module = MagicMock(YoutubeDL=FakeYoutubeDL, utils=yt_dlp.utils)
    return provider, options_seen

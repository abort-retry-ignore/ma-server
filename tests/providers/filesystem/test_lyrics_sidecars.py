"""Tests for local plain-text lyrics sidecars."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from music_assistant.helpers.tags import async_parse_tags
from tests.providers.filesystem.test_nfo_resolution import _item, _provider


async def test_plain_lyrics_sidecar_is_read_without_replacing_embedded_tags() -> None:
    """Plain lyrics saved beside a track are available to the filesystem provider."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider._parse_album = AsyncMock(return_value=None)
    provider._resolve_artists_with_mbids = AsyncMock(return_value=[])
    provider.exists = AsyncMock(side_effect=lambda path: path.endswith(".lyrics.txt"))
    provider._read_file = AsyncMock(return_value=b"First line\nSecond line")
    tags = await async_parse_tags(str(Path("tests/fixtures/MyArtist - MyTitle.m4a")))
    tags.tags.pop("lyrics", None)

    track = await provider._parse_track_impl(_item("Artist/Album/01 Song.m4a"), tags)

    assert track.metadata.lyrics == "First line\nSecond line"
    provider._read_file.assert_awaited_once_with("Artist/Album/01 Song.lyrics.txt")

    tags.tags["lyrics"] = "Embedded lyrics"
    provider._read_file.reset_mock()
    track = await provider._parse_track_impl(_item("Artist/Album/01 Song.m4a"), tags)
    assert track.metadata.lyrics == "Embedded lyrics"
    provider._read_file.assert_not_awaited()

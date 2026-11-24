"""Tests for exporting cached music metadata without replacing user files."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.providers.apple_music.metadata_export import (
    CacheMetadataExporter,
    _write_owned,
)
from music_assistant.providers.filesystem_local.helpers import parse_nfo_root
from music_assistant.providers.filesystem_local.parsers import parse_album_nfo, parse_artist_nfo

INSTANCE = "apple_music--test"
COVER = b"\xff\xd8\xffmock-jpeg"


def _exporter(tmp_path: Path) -> tuple[CacheMetadataExporter, Any, Track, Album, Artist]:
    root = tmp_path / "music"
    folder = root / "Artist" / "Album"
    folder.mkdir(parents=True)
    audio = folder / "01 Song.m4a"
    audio.write_bytes(b"audio")
    mapping = ProviderMapping(
        item_id="123", provider_domain="apple_music", provider_instance=INSTANCE
    )
    artist_mapping = ProviderMapping(
        item_id="artist-id", provider_domain="apple_music", provider_instance=INSTANCE
    )
    album_mapping = ProviderMapping(
        item_id="album-id", provider_domain="apple_music", provider_instance=INSTANCE
    )
    image = MediaItemImage(
        type=ImageType.THUMB,
        path="https://example.test/artist.jpg",
        provider=INSTANCE,
        remotely_accessible=True,
    )
    artist = Artist(
        item_id="20",
        provider="library",
        name="Artist",
        provider_mappings={artist_mapping},
        metadata=MediaItemMetadata(description="Fallback biography", images=UniqueList([image])),
    )
    album = Album(
        item_id="30",
        provider="library",
        name="Album",
        artists=UniqueList([artist]),
        provider_mappings={album_mapping},
        year=1984,
        metadata=MediaItemMetadata(
            description="Fallback review", label="Fallback label", copyright="Fallback copyright"
        ),
    )
    track = Track(
        item_id="10",
        provider="library",
        name="Song",
        artists=UniqueList([artist]),
        album=album,
        provider_mappings={mapping},
        metadata=MediaItemMetadata(lrc_lyrics="[00:01.00]Hello"),
    )
    apple_album = Album(
        item_id="album-id",
        provider=INSTANCE,
        name="Album",
        provider_mappings={album_mapping},
        metadata=MediaItemMetadata(
            description="Apple review", label="Apple label", copyright="Apple copyright"
        ),
    )
    apple_artist = Artist(
        item_id="artist-id",
        provider=INSTANCE,
        name="Artist",
        provider_mappings={artist_mapping},
        metadata=MediaItemMetadata(description="Apple biography", images=UniqueList([image])),
    )
    cache = MagicMock()
    cache._index_dir = str(tmp_path / "index")
    cache._resolve_entry = AsyncMock(
        return_value=({"location": "library", "album": "Album"}, str(audio), 5)
    )
    cache.provider.instance_id = INSTANCE
    mass = cache.provider.mass
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=track)
    mass.music.tracks.get_library_track_albums = AsyncMock(return_value=[album])
    mass.music.artists.get_library_item = AsyncMock(return_value=artist)
    mass.metadata.get_thumbnail = AsyncMock(return_value=COVER)

    async def get_cache(**kwargs: Any) -> Any:
        return apple_album if kwargs["key"].startswith("get_album.") else apple_artist

    mass.cache.get = AsyncMock(side_effect=get_cache)
    return CacheMetadataExporter(cache, str(root)), cache, track, album, artist


async def test_export_prefers_cached_apple_and_writes_portable_files(tmp_path: Path) -> None:
    """Use cached provider metadata first and persist it in MA-readable sidecars."""
    exporter, cache, _track, _album, _artist = _exporter(tmp_path)

    assert await exporter.export("123")

    folder = tmp_path / "music" / "Artist" / "Album"
    album_xml = parse_nfo_root((folder / "album.nfo").read_bytes(), "album")
    artist_xml = parse_nfo_root((folder.parent / "artist.nfo").read_bytes(), "artist")
    assert album_xml is not None
    assert artist_xml is not None
    assert album_xml["review"] == "Apple review"
    assert album_xml["label"] == "Apple label"
    assert album_xml["copyright"] == "Apple copyright"
    assert artist_xml["biography"] == "Apple biography"
    assert (folder / "01 Song.lrc").read_text() == "[00:01.00]Hello"
    assert (folder.parent / "artist.jpg").read_bytes() == COVER
    local_album = Album(item_id="a", provider="local", name="Album", provider_mappings=set())
    local_artist = Artist(item_id="b", provider="local", name="Artist", provider_mappings=set())
    parse_album_nfo(local_album, album_xml)
    parse_artist_nfo(local_artist, artist_xml)
    assert local_album.metadata.label == "Apple label"
    assert local_album.metadata.copyright == "Apple copyright"
    assert local_artist.metadata.description == "Apple biography"
    assert not await exporter.export("123")
    cache.provider.mass.metadata.get_thumbnail.assert_awaited_once()
    cache.provider.mass.metadata.schedule_update_metadata.assert_not_called()


async def test_export_schedules_missing_fields_and_preserves_user_files(tmp_path: Path) -> None:
    """Keep user files, schedule normal refreshes, and save unsynchronized text separately."""
    exporter, cache, track, album, artist = _exporter(tmp_path)
    folder = tmp_path / "music" / "Artist" / "Album"
    (folder / "album.nfo").write_text("user album")
    (folder.parent / "artist.jpg").write_bytes(b"user artwork")
    cache.provider.mass.cache.get = AsyncMock(return_value=None)
    track.metadata.lrc_lyrics = None
    track.metadata.lyrics = "Plain lyrics"
    album.metadata.description = None
    artist.metadata.description = None

    assert await exporter.export("123")

    assert (folder / "album.nfo").read_text() == "user album"
    assert (folder.parent / "artist.jpg").read_bytes() == b"user artwork"
    assert (folder / "01 Song.lyrics.txt").read_text() == "Plain lyrics"
    cache.provider.mass.metadata.get_thumbnail.assert_not_awaited()
    assert cache.provider.mass.metadata.schedule_update_metadata.call_count == 2


async def test_export_upgrades_owned_metadata_when_apple_cache_arrives(tmp_path: Path) -> None:
    """Later provider data updates our sidecars but never replaces a user's edits."""
    exporter, cache, _track, _album, _artist = _exporter(tmp_path)
    get_apple = cache.provider.mass.cache.get
    cache.provider.mass.cache.get = AsyncMock(return_value=None)
    assert await exporter.export("123")
    folder = tmp_path / "music" / "Artist" / "Album"
    album_xml = parse_nfo_root((folder / "album.nfo").read_bytes(), "album")
    assert album_xml is not None
    assert album_xml["review"] == "Fallback review"
    (folder.parent / "artist.nfo").write_text("My personal notes")
    cache.provider.mass.cache.get = get_apple

    assert await exporter.export("123")

    album_xml = parse_nfo_root((folder / "album.nfo").read_bytes(), "album")
    assert album_xml is not None
    assert album_xml["review"] == "Apple review"
    assert (folder.parent / "artist.nfo").read_text() == "My personal notes"


async def test_export_schedules_missing_lyrics_using_ma_metadata_tasks(tmp_path: Path) -> None:
    """Missing lyrics are requested through the normal background metadata scheduler."""
    exporter, cache, track, _album, _artist = _exporter(tmp_path)
    track.metadata.lrc_lyrics = None
    track.metadata.lyrics = None

    await exporter.export("123")

    cache.provider.mass.metadata.schedule_update_metadata.assert_called_once_with(track)


@pytest.mark.parametrize("modified", ["edit", "delete"])
def test_only_unchanged_owned_sidecars_are_updated(tmp_path: Path, modified: str) -> None:
    """An edited or deleted sidecar is never overwritten or recreated."""
    root = tmp_path / "music"
    root.mkdir()
    index = tmp_path / "index"
    target = root / "album.nfo"
    assert _write_owned(root, index, target, b"first")
    assert _write_owned(root, index, target, b"second")
    if modified == "edit":
        target.write_bytes(b"user change")
    else:
        target.unlink()
    assert not _write_owned(root, index, target, b"third")
    if modified == "edit":
        assert target.read_bytes() == b"user change"
    else:
        assert not target.exists()


def test_never_overwrite_preexisting_files_or_escape_root(tmp_path: Path) -> None:
    """User-created files and paths outside the managed tree remain untouched."""
    root = tmp_path / "music"
    root.mkdir()
    target = root / "album.nfo"
    target.write_bytes(b"user")
    assert not _write_owned(root, tmp_path / "index", target, b"new")
    assert target.read_bytes() == b"user"
    with pytest.raises(ValueError, match="outside the library"):
        _write_owned(root, tmp_path / "index", root / ".." / "outside", b"new")

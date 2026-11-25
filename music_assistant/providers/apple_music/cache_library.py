"""Publish completed Apple Music cache entries to a local music library."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.helpers.cache_library import CACHE_COPY_GRACE_SECONDS, write_mp4_tags
from music_assistant.helpers.cache_library import CacheLibrary as BaseCacheLibrary

from .constants import PARSED_ITEM_CACHE_CHECKSUM

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .audio_cache import AudioCache

__all__ = ["CACHE_COPY_GRACE_SECONDS", "ENV_AUDIO_LIBRARY_PATH", "CacheLibrary"]

ENV_AUDIO_LIBRARY_PATH = "MASS_APPLE_MUSIC_LIBRARY_PATH"


class CacheLibrary(BaseCacheLibrary):
    """Publish tagged copies of Apple Music cache entries."""

    def __init__(self, cache: AudioCache, root: str) -> None:
        """Initialize the library publisher."""
        super().__init__(
            cache,
            root,
            tag_writer=_write_tags,
            cache_checksum=PARSED_ITEM_CACHE_CHECKSUM,
        )


def _write_tags(path: str, item_id: str, entry: dict[str, Any], track: Track | None) -> None:
    """Write Apple Music metadata to the cached MP4 file."""
    write_mp4_tags(path, item_id, entry, track, "MA Apple Music ID")

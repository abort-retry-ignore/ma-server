"""Export cached Apple Music metadata alongside local files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.helpers.cache_metadata_export import (
    CacheMetadataExporter as BaseMetadataExporter,
)
from music_assistant.helpers.cache_metadata_export import _write_owned

from .constants import PARSED_ITEM_CACHE_CHECKSUM

if TYPE_CHECKING:
    from .audio_cache import AudioCache

__all__ = ["CacheMetadataExporter", "_write_owned"]


class CacheMetadataExporter(BaseMetadataExporter):
    """Export known Apple Music metadata without overwriting user files."""

    def __init__(self, cache: AudioCache, root: str) -> None:
        """Initialize the exporter."""
        super().__init__(cache, root, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)

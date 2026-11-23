"""
The Apple Music provider with an audio cache.

The provider and its streaming manager are used as they are; the classes here only add to them.
That keeps this fork's changes out of the files the upstream project changes, which is what makes
moving to a newer Music Assistant version a small job.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .audio_cache import ENV_AUDIO_CACHE_PATH, AudioCache, AudioDownload
from .provider import AppleMusicProvider
from .streaming import AppleMusicStreamingManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant


class CachingStreamingManager(AppleMusicStreamingManager):
    """The streaming manager of Apple Music, keeping a copy of the audio of played tracks."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize the streaming manager."""
        super().__init__(provider)
        self.audio_cache: AudioCache | None = None
        self._key_ids: dict[str, str] = {}

    async def initialize(self) -> None:
        """Load the Widevine files like the streaming manager does, and open the audio cache."""
        await super().initialize()
        if not (cache_path := os.environ.get(ENV_AUDIO_CACHE_PATH, "").strip()):
            return
        audio_cache = AudioCache(self.provider, cache_path)
        if await audio_cache.setup():
            self.audio_cache = audio_cache
            self.logger.info("Keeping the audio of played tracks in %s", cache_path)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """
        Return StreamDetails for a single catalog or library track.

        A clear file already in the audio cache is played locally with no Apple Music
        contact. Otherwise the track is resolved with Apple as usual; a miss is fetched
        here so a clear copy can be kept while it plays.
        """
        # Clear hit: local file only — no webPlayback, license, or CDN.
        if self.audio_cache is not None:
            if cached_path := await self.audio_cache.lookup_clear(item_id):
                self.audio_cache.start_missing_cover(item_id, cached_path)
                self.logger.info(
                    "audio cache clear hit item_id=%s path=%s (no Apple Music request)",
                    item_id,
                    cached_path,
                )
                return StreamDetails(
                    item_id=item_id,
                    provider=self.provider.instance_id,
                    audio_format=AudioFormat(
                        content_type=ContentType.MP4, codec_type=ContentType.AAC
                    ),
                    stream_type=StreamType.LOCAL_FILE,
                    path=cached_path,
                    can_seek=True,
                    allow_seek=True,
                )

        details = await super().get_stream_details(item_id)
        key_id = self._key_ids.pop(item_id, None)
        if (
            self.audio_cache is None
            or key_id is None
            or details.stream_type != StreamType.ENCRYPTED_HTTP
            or not isinstance(details.path, str)
            or not details.path
            or not details.decryption_key
        ):
            return details
        if cached_path := await self.audio_cache.lookup(item_id, key_id):
            # legacy encrypted cache entry: keep the key on the details
            self.audio_cache.start_missing_cover(item_id, cached_path)
            return dataclasses.replace(details, path=cached_path)
        # miss: stream encrypted bytes with the key for live play, store clear after
        return dataclasses.replace(
            details,
            stream_type=StreamType.CUSTOM,
            path=None,
            decryption_key=None,
            extra_input_args=["-decryption_key", details.decryption_key],
            data=AudioDownload(
                url=details.path,
                key_id=key_id,
                decryption_key=details.decryption_key or "",
            ),
            can_seek=False,
        )

    async def stream_track_and_store(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes]:
        """
        Return the encrypted file of a track that is not in the audio cache yet.

        A copy is kept in the audio cache once the whole file has arrived.

        :param streamdetails: The StreamDetails from get_stream_details.
        """
        assert self.audio_cache is not None
        assert isinstance(streamdetails.data, AudioDownload)
        track = None
        try:
            track = await self.provider.get_track(streamdetails.item_id)
        except (MusicAssistantError, ClientError) as err:
            self.logger.debug("No track details to name the cached file: %s", err)
        async with aclosing(
            self.audio_cache.stream_and_store(streamdetails.item_id, streamdetails.data, track)
        ) as stream:
            async for chunk in stream:
                yield chunk

    async def _get_decryption_key(self, *args: Any, **kwargs: Any) -> str:
        """Get the decryption key for a song, and note the id of the key it is for."""
        # looked up by name, so a parameter added upstream does not matter. When upstream
        # changed it in another way the track still plays, just without the cache
        try:
            arguments = inspect.signature(super()._get_decryption_key).bind(*args, **kwargs)
            self._key_ids[arguments.arguments["item_id"]] = arguments.arguments["key_id"].hex()
        except (TypeError, KeyError, AttributeError) as err:
            self.logger.warning("Audio cache skipped, no key id in the key request: %r", err)
        return await super()._get_decryption_key(*args, **kwargs)


class CachingAppleMusicProvider(AppleMusicProvider):
    """The Apple Music provider, keeping a copy of the audio of played tracks."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self.caching_streams = CachingStreamingManager(self)
        self.streaming_manager = self.caching_streams

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the encrypted file of a track to keep a copy of.

        :param streamdetails: The StreamDetails from get_stream_details.
        :param seek_position: Ignored for a track that is kept, the file is read on from its start.
        """
        if not isinstance(streamdetails.data, AudioDownload):
            async for chunk in super().get_audio_stream(streamdetails, seek_position):
                yield chunk
            return
        async with aclosing(self.caching_streams.stream_track_and_store(streamdetails)) as stream:
            async for chunk in stream:
                yield chunk

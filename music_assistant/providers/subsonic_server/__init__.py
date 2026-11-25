"""
Subsonic API server: lets Subsonic apps browse and play the music files in MA's library.

The server runs on its own port and answers the Subsonic/OpenSubsonic API the way Navidrome
does. Subsonic logins are MA users, each with a separate Subsonic password set in this
plugin's settings. Only library tracks that have a file in one of the local music folders are
shown; files are sent as they are, with range support. Plays reported by the apps go into
MA's play history (and so into the play memory), stars are MA favourites, and tracks blocked
as "wrong song" stay hidden.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderType

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

from .constants import (
    CONF_BIND_IP,
    CONF_LOG_REQUESTS,
    CONF_MUSIC_FOLDERS,
    CONF_PASSWORD_PREFIX,
    CONF_PORT,
    CONF_SERVER_TYPE,
    DEFAULT_BIND_IP,
    DEFAULT_PORT,
    SERVER_TYPE_MA,
    SERVER_TYPE_NAVIDROME,
)
from .index import MusicFolder
from .server import SubsonicServer

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# library changes that make the index stale
INDEX_EVENTS = (
    EventType.MEDIA_ITEM_ADDED,
    EventType.MEDIA_ITEM_UPDATED,
    EventType.MEDIA_ITEM_DELETED,
    EventType.MEDIA_ITEM_PLAYED,
    EventType.PROVIDERS_UPDATED,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SubsonicServerProvider(mass, manifest, config)


class SubsonicServerProvider(PluginProvider):
    """Plugin provider that runs the Subsonic API server."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self.server: SubsonicServer | None = None
        self._unsubscribe: list[Callable[[], None]] = []

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the settings: port, folders, logging and a Subsonic password per user."""
        folders = [
            ConfigValueOption(value=p.instance_id, title=p.name)
            for p in self._filesystem_providers()
        ]
        entries = [
            ConfigEntry(
                key=CONF_PORT,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_PORT,
                range=(0, 65535),
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_BIND_IP,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_BIND_IP,
                advanced=True,
                requires_reload=True,
            ),
            ConfigEntry(
                key=CONF_MUSIC_FOLDERS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                options=folders,
                default_value=[],
                required=False,
            ),
            ConfigEntry(
                key=CONF_LOG_REQUESTS,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_SERVER_TYPE,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(value=SERVER_TYPE_MA),
                    ConfigValueOption(value=SERVER_TYPE_NAVIDROME),
                ],
                default_value=SERVER_TYPE_MA,
                advanced=True,
            ),
        ]
        for user in await self.mass.webserver.auth.list_users():
            name = user.display_name or user.username
            entries.append(
                ConfigEntry(
                    key=f"{CONF_PASSWORD_PREFIX}{user.user_id}",
                    type=ConfigEntryType.SECURE_STRING,
                    translation_key="subsonic_password",
                    translation_params=[name, user.username],
                    required=False,
                    category="subsonic_logins",
                )
            )
        return tuple(entries)

    async def handle_async_init(self) -> None:
        """Start the Subsonic server."""
        self.server = SubsonicServer(self)
        await self.server.start(
            str(self.config.get_value(CONF_BIND_IP) or DEFAULT_BIND_IP),
            cast("int", self.config.get_value(CONF_PORT) or 0),
        )

    async def loaded_in_mass(self) -> None:
        """Build the library index and keep it up to date."""
        await super().loaded_in_mass()
        assert self.server is not None
        self._unsubscribe.append(self.mass.subscribe(self._on_library_event, INDEX_EVENTS))
        self.mass.create_task(self.server.index.rebuild())

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the Subsonic server."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self.server is not None:
            await self.server.stop()
            self.server = None
        await super().unload(is_removed)

    def music_folders(self) -> list[MusicFolder]:
        """Return the music folders Subsonic apps can see, numbered from 1."""
        selected = set(self.config.get_value(CONF_MUSIC_FOLDERS) or [])  # type: ignore[arg-type]
        folders = []
        for number, provider in enumerate(self._filesystem_providers(), start=1):
            if selected and provider.instance_id not in selected:
                continue
            folders.append(
                MusicFolder(number, provider.instance_id, provider.name, provider.base_path)
            )
        return folders

    async def lookup_user(self, username: str) -> tuple[User, str] | None:
        """
        Return an enabled MA user and their Subsonic password, or None without access.

        :param username: The Subsonic (= MA) username.
        """
        user = await self.mass.webserver.auth.get_user_by_username(username)
        if user is None or not user.enabled:
            return None
        password = self.config.get_value(f"{CONF_PASSWORD_PREFIX}{user.user_id}")
        if not password:
            return None
        return user, str(password)

    async def subsonic_users(self) -> list[User]:
        """Return the enabled MA users that have a Subsonic password."""
        users = []
        for user in await self.mass.webserver.auth.list_users():
            if user.enabled and self.config.get_value(f"{CONF_PASSWORD_PREFIX}{user.user_id}"):
                users.append(user)
        return users

    def _filesystem_providers(self) -> list[LocalFileSystemProvider]:
        """Return the loaded local filesystem music providers, in a stable order."""
        providers = [
            p
            for p in self.mass.get_providers(ProviderType.MUSIC)
            if isinstance(p, LocalFileSystemProvider)
        ]
        return sorted(providers, key=lambda p: p.instance_id)

    def _on_library_event(self, event: MassEvent) -> None:
        """
        Rebuild the index after the library changed.

        :param event: The library event.
        """
        if self.server is not None:
            self.server.schedule_index_rebuild()

"""The per-request context handed to API handlers, and the registry of API methods."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .constants import ERR_GENERIC, ERR_MISSING_PARAM, MAX_LIST_SIZE
from .mapping import Mapper
from .responses import SubsonicError

if TYPE_CHECKING:
    from multidict import MultiDictProxy
    from music_assistant_models.auth import User

    from .server import SubsonicServer

type HandlerResult = dict[str, Any] | web.StreamResponse
type Handler = Callable[[RequestContext], Awaitable[HandlerResult]]


@dataclass(frozen=True)
class ApiMethod:
    """A registered Subsonic API method."""

    handler: Handler
    needs_auth: bool


API_METHODS: dict[str, ApiMethod] = {}


@dataclass
class NowPlaying:
    """A track a client reported as playing."""

    track_id: int
    username: str
    client: str
    since: float


def api_method(*names: str, needs_auth: bool = True) -> Callable[[Handler], Handler]:
    """
    Register a handler for one or more Subsonic API method names.

    :param names: The method names (without ".view").
    :param needs_auth: Whether the method requires a login.
    """

    def wrapper(handler: Handler) -> Handler:
        for name in names:
            API_METHODS[name] = ApiMethod(handler, needs_auth)
        return handler

    return wrapper


@dataclass
class RequestContext:
    """One Subsonic API request."""

    server: SubsonicServer
    request: web.Request
    params: MultiDictProxy[str]
    method: str
    client: str
    user: User | None = None
    ratings: dict[str, int] = field(default_factory=dict)
    note: str = ""
    _mapper: Mapper | None = None

    @property
    def mapper(self) -> Mapper:
        """Return the entry mapper for this request."""
        if self._mapper is None:
            self._mapper = Mapper(self.server.index, self.image_url, self.ratings)
        return self._mapper

    @property
    def user_id(self) -> str:
        """Return the logged-in user's MA user id."""
        assert self.user is not None
        return self.user.user_id

    def image_url(self, cover_id: str) -> str:
        """
        Return a signed, login-free URL for a cover art ID, on the host the client used.

        :param cover_id: The cover art ID.
        """
        return self.server.image_url(self.request, cover_id)

    def get(self, name: str, default: str | None = None) -> str | None:
        """
        Return a parameter value.

        :param name: The parameter name.
        :param default: Returned when the parameter is missing.
        """
        return self.params.get(name, default)

    def get_all(self, name: str) -> list[str]:
        """
        Return every value of a repeated parameter.

        :param name: The parameter name.
        """
        return [v for v in self.params.getall(name, []) if v != ""]

    def require(self, name: str) -> str:
        """
        Return a required parameter value.

        :param name: The parameter name.
        :raises SubsonicError: Missing parameter (10).
        """
        value = self.params.get(name)
        if value is None or value == "":
            raise SubsonicError(ERR_MISSING_PARAM, f"Required parameter is missing: {name}")
        return value

    def get_int(self, name: str, default: int | None = None) -> int | None:
        """
        Return an integer parameter.

        :param name: The parameter name.
        :param default: Returned when the parameter is missing.
        :raises SubsonicError: Generic error (0) for a value that is not a number.
        """
        value = self.params.get(name)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError as err:
            raise SubsonicError(ERR_GENERIC, f"Invalid value for parameter {name}") from err

    def get_size(self, name: str, default: int) -> int:
        """
        Return a list size parameter, capped at the maximum list size.

        :param name: The parameter name.
        :param default: Returned when the parameter is missing.
        """
        value = self.get_int(name, default)
        return max(0, min(value if value is not None else default, MAX_LIST_SIZE))

    def get_offset(self, name: str = "offset") -> int:
        """
        Return a list offset parameter (0 when missing).

        :param name: The parameter name.
        """
        return max(0, self.get_int(name, 0) or 0)

    def get_bool(self, name: str, default: bool) -> bool:
        """
        Return a boolean parameter.

        :param name: The parameter name.
        :param default: Returned when the parameter is missing.
        """
        value = self.params.get(name)
        if value is None or value == "":
            return default
        return value.lower() in ("true", "1", "yes")

    def folder_id(self) -> int | None:
        """Return the requested music folder, or None for all folders."""
        return self.get_int("musicFolderId")

"""The Subsonic API web server: routing, login, response format and request logging."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web
from multidict import MultiDict, MultiDictProxy

from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.helpers.webserver import Webserver

from . import api_library, api_media, api_user  # noqa: F401  # registers the API methods
from .artwork import Artwork
from .auth import Authenticator
from .constants import (
    CONF_LOG_REQUESTS,
    CONF_SERVER_TYPE,
    ERR_GENERIC,
    INDEX_REBUILD_DELAY,
    NAVIDROME_VERSION,
    SECRET_PARAMS,
    SERVER_TYPE_MA,
    SERVER_TYPE_NAVIDROME,
)
from .context import API_METHODS, NowPlaying, RequestContext
from .index import LibraryIndex
from .responses import SubsonicError, envelope, render, render_error
from .storage import Storage

if TYPE_CHECKING:
    from . import SubsonicServerProvider

# parameters left out of the request log besides the credentials: they are the same on every call
_QUIET_PARAMS = frozenset({"v", "c", "f"})
_MAX_LOGGED_VALUE = 80
_REBUILD_TASK_ID = "subsonic_server_index_rebuild"


class SubsonicServer:
    """Serves the Subsonic API on its own port."""

    def __init__(self, provider: SubsonicServerProvider) -> None:
        """
        Initialize the server.

        :param provider: The plugin provider that owns this server.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger
        self.index = LibraryIndex(self.mass, provider.music_folders, self.logger)
        self.storage = Storage(self.mass.storage_path)
        self.auth = Authenticator(provider.lookup_user, self.logger)
        self.artwork = Artwork(self)
        self.now_playing: dict[tuple[str, str], NowPlaying] = {}
        self._webserver = Webserver(self.logger)
        self._reported: set[tuple[str, str]] = set()

    @property
    def port(self) -> int | None:
        """Return the port the server listens on."""
        return self._webserver.port

    async def start(self, bind_ip: str, port: int) -> None:
        """
        Open the storage and start listening.

        :param bind_ip: The address to listen on.
        :param port: The port to listen on.
        """
        await self.storage.setup()
        await self._webserver.setup(
            bind_ip,
            port,
            static_routes=[
                ("*", "/rest/{method}", self.handle_rest),
                ("GET", "/share/img/{token}", self.handle_share_image),
                ("*", "/{tail:.*}", self.handle_other),
            ],
        )
        self.logger.info("Subsonic API listening on %s:%s", bind_ip, self.port)

    async def stop(self) -> None:
        """Stop listening and close the storage."""
        await self._webserver.close()
        await self.storage.close()

    def schedule_index_rebuild(self) -> None:
        """Rebuild the library index shortly, after a burst of library changes has settled."""
        self.mass.call_later(INDEX_REBUILD_DELAY, self.index.rebuild, task_id=_REBUILD_TASK_ID)

    def head(self, status: str = "ok") -> dict[str, Any]:
        """
        Return the response envelope attributes.

        :param status: "ok" or "failed".
        """
        if self.provider.config.get_value(CONF_SERVER_TYPE) == SERVER_TYPE_NAVIDROME:
            return envelope(SERVER_TYPE_NAVIDROME, NAVIDROME_VERSION, status)
        return envelope(SERVER_TYPE_MA, self.mass.version, status)

    def image_url(self, request: web.Request, cover_id: str, size: int = 600) -> str:
        """
        Return a signed, login-free image URL on the host the client connected to.

        :param request: The current request.
        :param cover_id: The cover art ID.
        :param size: The image size in pixels.
        """
        scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
        host = request.headers.get("X-Forwarded-Host") or request.host
        return f"{scheme}://{host}/share/img/{self.sign(cover_id)}?size={size}"

    def sign(self, cover_id: str) -> str:
        """
        Return the signed token for a cover art ID.

        :param cover_id: The cover art ID.
        """
        digest = hmac.new(self.storage.image_secret, cover_id.encode(), hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")
        return f"{cover_id}.{signature}"

    def verify(self, token: str) -> str | None:
        """
        Return the cover art ID of a signed token, or None when the signature is wrong.

        :param token: The token from the image URL.
        """
        cover_id, _, _signature = token.rpartition(".")
        if cover_id and hmac.compare_digest(self.sign(cover_id), token):
            return cover_id
        return None

    async def handle_rest(self, request: web.Request) -> web.StreamResponse:
        """
        Handle a Subsonic API call.

        :param request: The request.
        """
        started = time.monotonic()
        name = request.match_info["method"].removesuffix(".view")
        params = await _read_params(request)
        fmt = params.get("f", "xml")
        callback = params.get("callback")
        client = params.get("c", "")
        method = API_METHODS.get(name)
        if method is None:
            self._report_once(name, client, f"Unsupported Subsonic call '{name}' from client '%s'")
            return web.Response(status=404, text="404 page not found\n")
        ctx = RequestContext(self, request, params, name, client)
        outcome = "ok"
        try:
            if method.needs_auth:
                ctx.user = await self.auth.authenticate(params, request.remote)
                set_current_user(ctx.user)
                ctx.ratings = await self.storage.get_ratings(ctx.user.user_id)
            await self.index.ensure_built()
            result = await method.handler(ctx)
        except SubsonicError as err:
            outcome = f"error {err.code} ({err.message})"
            response: web.StreamResponse = render_error(self.head("failed"), err, fmt, callback)
        except Exception as err:
            self.logger.exception("Subsonic call '%s' failed", name)
            outcome = f"error {ERR_GENERIC} ({err})"
            error = SubsonicError(ERR_GENERIC, str(err) or type(err).__name__)
            response = render_error(self.head("failed"), error, fmt, callback)
        else:
            if isinstance(result, web.StreamResponse):
                response = result
            else:
                response = render(self.head(), result, fmt, callback)
        self._log_request(ctx, outcome, started)
        return response

    async def handle_share_image(self, request: web.Request) -> web.StreamResponse:
        """
        Serve an image from a signed URL (artistImageUrl and the info calls), without login.

        :param request: The request.
        """
        cover_id = self.verify(request.match_info["token"])
        if cover_id is None:
            return web.Response(status=404, text="404 page not found\n")
        size = request.query.get("size", "")
        await self.index.ensure_built()
        image = await self.artwork.get(cover_id, int(size) if size.isdigit() else 0)
        if image is None:
            return web.Response(status=404, text="404 page not found\n")
        return self.artwork.response(request, image)

    async def handle_other(self, request: web.Request) -> web.StreamResponse:
        """
        Answer any other path with 404, and log it once: it shows what a client expected.

        :param request: The request.
        """
        self._report_once(
            request.path,
            request.headers.get("User-Agent", ""),
            f"Request for {request.method} {request.path} outside the Subsonic API "
            "(user agent '%s')",
        )
        return web.Response(status=404, text="404 page not found\n")

    def _report_once(self, key: str, client: str, message: str) -> None:
        """
        Log a warning once per key and client.

        :param key: What is reported (method or path).
        :param client: The client name.
        :param message: The log message, with %s for the client.
        """
        if (key, client) in self._reported:
            return
        self._reported.add((key, client))
        self.logger.warning(message, client)

    def _log_request(self, ctx: RequestContext, outcome: str, started: float) -> None:
        """
        Log one API call, without credentials.

        :param ctx: The request context.
        :param outcome: "ok" or the error.
        :param started: Monotonic time the request started.
        """
        failed = outcome != "ok"
        if not failed and not self.provider.config.get_value(CONF_LOG_REQUESTS):
            return
        shown = []
        for key, value in ctx.params.items():
            if key in SECRET_PARAMS or key in _QUIET_PARAMS:
                continue
            text = value if len(value) <= _MAX_LOGGED_VALUE else value[:_MAX_LOGGED_VALUE] + "…"
            shown.append(f"{key}={text}")
        range_header = ctx.request.headers.get("Range")
        if range_header:
            shown.append(f"Range: {range_header}")
        user = ctx.user.username if ctx.user else ctx.params.get("u", "-")
        self.logger.log(
            logging.WARNING if failed else logging.INFO,
            "%s %s [%s] %s %s -> %s%s (%.0f ms)",
            ctx.request.method,
            ctx.method,
            ctx.client or "?",
            user,
            " ".join(shown),
            outcome,
            ctx.note,
            (time.monotonic() - started) * 1000,
        )


async def _read_params(request: web.Request) -> MultiDictProxy[str]:
    """
    Return the request parameters from the query string and a posted form.

    :param request: The request.
    """
    params: MultiDict[str] = MultiDict(request.query)
    if request.method == "POST" and request.content_type in (
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ):
        form = await request.post()
        for key, value in form.items():
            if isinstance(value, str):
                params.add(key, value)
    return MultiDictProxy(params)

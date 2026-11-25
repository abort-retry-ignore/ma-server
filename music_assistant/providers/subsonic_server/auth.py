"""Subsonic login: token, plain and hex-encoded passwords, with a lockout for repeated failures."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from typing import TYPE_CHECKING

from .constants import (
    ERR_AUTH_NOT_SUPPORTED,
    ERR_MISSING_PARAM,
    ERR_WRONG_CREDENTIALS,
    FAILED_LOGIN_WINDOW,
    LOCKOUT_SECONDS,
    MAX_FAILED_LOGINS,
)
from .responses import SubsonicError

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable, Mapping

    from music_assistant_models.auth import User

WRONG_CREDENTIALS = "Wrong username or password"


class Authenticator:
    """Checks Subsonic credentials against the per-user Subsonic passwords."""

    def __init__(
        self,
        lookup: Callable[[str], Awaitable[tuple[User, str] | None]],
        logger: logging.Logger,
    ) -> None:
        """
        Initialize the authenticator.

        :param lookup: Returns the enabled MA user and their Subsonic password for a username,
            or None when that user has no Subsonic access.
        :param logger: Logger for failed logins.
        """
        self._lookup = lookup
        self._logger = logger
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    async def authenticate(self, params: Mapping[str, str], remote: str | None) -> User:
        """
        Return the user the request's credentials belong to.

        :param params: The request parameters.
        :param remote: The client address, for the lockout.
        :raises SubsonicError: When the credentials are missing or wrong.
        """
        address = remote or "unknown"
        now = time.monotonic()
        if self._locked_until.get(address, 0) > now:
            raise SubsonicError(ERR_WRONG_CREDENTIALS, WRONG_CREDENTIALS)
        if "apiKey" in params:
            raise SubsonicError(ERR_AUTH_NOT_SUPPORTED, "API key authentication is not supported")
        username = params.get("u")
        if not username:
            raise SubsonicError(ERR_MISSING_PARAM, "Missing required parameter 'u'")
        token, salt, password = params.get("t"), params.get("s"), params.get("p")
        if not (token and salt) and not password:
            raise SubsonicError(ERR_MISSING_PARAM, "Missing authentication parameters")
        found = await self._lookup(username)
        if found is not None:
            user, expected = found
            if token and salt:
                digest = hashlib.md5((expected + salt).encode()).hexdigest()
                if hmac.compare_digest(digest, token.lower()):
                    return self._success(address, user)
            elif password is not None and hmac.compare_digest(
                _decode_password(password).encode(), expected.encode()
            ):
                return self._success(address, user)
        self._fail(address, username, now)
        raise SubsonicError(ERR_WRONG_CREDENTIALS, WRONG_CREDENTIALS)

    def _success(self, address: str, user: User) -> User:
        """
        Forget earlier failures of an address and return the user.

        :param address: The client address.
        :param user: The authenticated user.
        """
        self._failures.pop(address, None)
        return user

    def _fail(self, address: str, username: str, now: float) -> None:
        """
        Count a failed login and lock the address out after too many.

        :param address: The client address.
        :param username: The username that was tried.
        :param now: The current monotonic time.
        """
        failures = self._failures.setdefault(address, deque())
        failures.append(now)
        while failures and failures[0] < now - FAILED_LOGIN_WINDOW:
            failures.popleft()
        self._logger.warning("Subsonic login failed for user '%s' from %s", username, address)
        if len(failures) >= MAX_FAILED_LOGINS:
            self._locked_until[address] = now + LOCKOUT_SECONDS
            failures.clear()
            self._logger.warning(
                "Too many failed Subsonic logins from %s, refusing it for %s seconds",
                address,
                LOCKOUT_SECONDS,
            )


def _decode_password(password: str) -> str:
    """
    Return a plain password from the ``p`` parameter, which may be hex encoded ("enc:...").

    :param password: The parameter value.
    """
    if password.startswith("enc:"):
        try:
            return bytes.fromhex(password[4:]).decode()
        except ValueError, UnicodeDecodeError:
            return ""
    return password

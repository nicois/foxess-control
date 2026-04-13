"""Web portal authentication for FoxESS Cloud.

The FoxESS web dashboard uses a separate login endpoint from the Open API.
This session provides the token needed for the WebSocket real-time stream.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def ensure_password_hash(password_or_hash: str) -> str:
    """Return the MD5 hex digest.

    If *password_or_hash* already looks like an MD5 hash (32 hex chars),
    return it as-is (lowercased).  Otherwise, hash the raw password.
    """
    if _MD5_RE.match(password_or_hash):
        return password_or_hash.lower()
    return hashlib.md5(password_or_hash.encode()).hexdigest()


class FoxESSWebAuthError(Exception):
    """Web portal authentication failed."""


class FoxESSWebSession:
    """Manage a web portal session token for the FoxESS Cloud.

    *password_md5* must be the MD5 hex digest of the user's password
    (use :func:`ensure_password_hash` to convert raw passwords).
    """

    BASE_URL = "https://www.foxesscloud.com"
    LOGIN_PATH = "/c/v0/user/login"
    TOKEN_TTL = 3600 * 12  # refresh proactively every 12 hours

    def __init__(self, username: str, password_md5: str) -> None:
        self._username = username
        self._password_md5 = password_md5
        self._token: str | None = None
        self._last_login: float = 0.0
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def async_login(self) -> str:
        """Authenticate with the web portal and return a session token."""
        session = self._get_session()
        url = f"{self.BASE_URL}{self.LOGIN_PATH}"
        body = {"user": self._username, "password": self._password_md5}
        try:
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FoxESSWebAuthError(f"Web login request failed: {exc}") from exc

        errno = data.get("errno", -1)
        if errno != 0:
            raise FoxESSWebAuthError(
                f"Web login rejected: errno={errno}, msg={data.get('msg', '?')}"
            )

        token = data.get("result", {}).get("token")
        if not token:
            raise FoxESSWebAuthError("Web login response missing token")

        self._token = token
        self._last_login = time.monotonic()
        _LOGGER.debug("FoxESS web login successful")
        return token  # type: ignore[no-any-return]

    async def async_ensure_token(self) -> str:
        """Return a valid token, refreshing if stale."""
        if (
            self._token is not None
            and (time.monotonic() - self._last_login) < self.TOKEN_TTL
        ):
            return self._token
        return await self.async_login()

    @property
    def token(self) -> str | None:
        """Return the current token without refreshing."""
        return self._token

    async def async_close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

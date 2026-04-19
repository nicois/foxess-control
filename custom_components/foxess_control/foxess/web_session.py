"""Web portal authentication for FoxESS Cloud.

The FoxESS web dashboard uses a separate login endpoint from the Open API.
This session provides the token needed for the WebSocket real-time stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .signature import generate_signature

_LOGGER = logging.getLogger(__name__)

_MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def ensure_password_hash(password_or_hash: str) -> str:
    """Return the MD5 hex digest.

    If *password_or_hash* already looks like an MD5 hash (32 hex chars),
    return it as-is (lowercased).  Otherwise, hash the raw password.

    Strips whitespace to avoid common issues like trailing newlines
    from ``echo`` vs ``echo -n`` when generating hashes.
    """
    password_or_hash = password_or_hash.strip()
    if _MD5_RE.match(password_or_hash):
        _LOGGER.debug(
            "Password input recognised as MD5 hash (len=%d)",
            len(password_or_hash),
        )
        return password_or_hash.lower()
    _LOGGER.debug(
        "Password input is not MD5 (len=%d), hashing",
        len(password_or_hash),
    )
    return hashlib.md5(password_or_hash.encode()).hexdigest()


class FoxESSWebAuthError(Exception):
    """Web portal authentication failed."""


class FoxESSWebSession:
    """Manage a web portal session token for the FoxESS Cloud.

    *password_md5* must be the MD5 hex digest of the user's password
    (use :func:`ensure_password_hash` to convert raw passwords).
    """

    BASE_URL = "https://www.foxesscloud.com"
    LOGIN_PATH = "/basic/v0/user/login"
    TOKEN_TTL = 3600 * 12  # refresh proactively every 12 hours
    TIMEZONE = "Australia/Melbourne"
    LANG = "en"

    def __init__(
        self,
        username: str,
        password_md5: str,
        base_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._username = username
        self._password_md5 = password_md5
        if base_url is not None:
            self.BASE_URL = base_url
        self._token: str | None = None
        self._last_login: float = 0.0
        self._session: aiohttp.ClientSession | None = session
        self._owns_session = session is None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _make_headers(self, path: str) -> dict[str, str]:
        """Build the required headers including the WASM signature.

        Contains a CPU-bound WASM call — use :meth:`_async_make_headers`
        from async code to avoid blocking the event loop.
        """
        ts_ms = str(int(time.time() * 1000))
        token = self._token or ""
        sig = generate_signature(path, token, self.LANG, ts_ms)
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "lang": self.LANG,
            "timestamp": ts_ms,
            "signature": sig,
            "token": token,
            "timezone": self.TIMEZONE,
            "dt": f"{self.TIMEZONE}@{ts_ms}@{now}",
            "platform": "web",
        }

    async def _async_make_headers(self, path: str) -> dict[str, str]:
        """Build headers off the event loop (executor-wrapped)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._make_headers, path)

    async def async_login(self) -> str:
        """Authenticate with the web portal and return a session token."""
        session = self._get_session()
        url = f"{self.BASE_URL}{self.LOGIN_PATH}"
        headers = await self._async_make_headers(self.LOGIN_PATH)
        body = {
            "user": self._username,
            "password": self._password_md5,
            "type": 1,
            "verification": 1,
        }
        try:
            async with session.post(
                url,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
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

    async def async_discover_battery_id(self, plant_id: str) -> str | None:
        """Discover the battery compound ID via a single WebSocket message.

        Connects to the real-time WebSocket, reads the first non-stale
        message, extracts ``batteryId@batSn`` from the ``bat`` node,
        and disconnects.  Returns ``None`` if discovery fails.
        """
        from urllib.parse import quote

        try:
            token = await self.async_ensure_token()
            session = self._get_session()
            ws_base = self.BASE_URL.replace("http://", "ws://").replace(
                "https://", "wss://"
            )
            url = (
                f"{ws_base}/dew/v0/wsmaitian"
                f"?plantId={plant_id}&token={quote(token, safe='')}"
                f"&platform=web&lang=en"
            )
            async with session.ws_connect(
                url, timeout=aiohttp.ClientWSTimeout(ws_close=5.0)
            ) as ws:
                await ws.send_str("getdata")
                for _ in range(3):
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = msg.json()
                    bat = data.get("result", {}).get("node", {}).get("bat", {})
                    bid = bat.get("batteryId")
                    sn_list = bat.get("multipleBatterySoc", [])
                    if bid and sn_list:
                        first_sn = sn_list[0].get("batSn", "")
                        if first_sn:
                            compound = f"{bid}@{first_sn}"
                            _LOGGER.info("Discovered battery compound ID: %s", compound)
                            return compound
        except Exception as exc:
            _LOGGER.warning("Battery ID discovery via WebSocket failed: %s", exc)
        return None

    async def async_get_battery_temperature(
        self,
        battery_compound_id: str,
    ) -> float | None:
        """Fetch BMS battery temperature from the web portal.

        Uses ``GET /dew/v0/device/detail?id=<compound_id>&category=battery``
        where *battery_compound_id* is ``{batteryId}@{batSn}`` from the
        WebSocket ``bat`` node.

        Returns the temperature value, or ``None`` if unavailable.
        """
        try:
            result = await self.async_get(
                "/dew/v0/device/detail",
                {"id": battery_compound_id, "category": "battery"},
            )
            return self._extract_battery_temperature(result)
        except (FoxESSWebAuthError, ValueError, TypeError, KeyError) as exc:
            _LOGGER.debug("BMS temperature fetch failed: %s", exc)
            return None
        except Exception as exc:
            _LOGGER.warning("BMS temperature fetch failed: %s", exc)
            return None

    @staticmethod
    def _extract_battery_temperature(result: Any) -> float | None:
        """Extract battery temperature from device detail result.

        Handles the /dew/v0/device/detail response format where
        temperature is at result.battery.temperature.value.
        """
        if result is None:
            return None
        battery = result.get("battery", {})
        if not battery:
            return None
        temp = battery.get("temperature", {})
        val = temp.get("value") if isinstance(temp, dict) else temp
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    async def async_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Perform an authenticated GET request to the web portal API."""
        await self.async_ensure_token()
        session = self._get_session()
        headers = await self._async_make_headers(path)
        url = f"{self.BASE_URL}{path}"
        async with session.get(
            url,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json()
        errno = data.get("errno", -1)
        if errno != 0:
            raise FoxESSWebAuthError(
                f"Web API GET {path} failed: errno={errno}, msg={data.get('msg', '?')}"
            )
        return data.get("result")

    async def async_post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        """Perform an authenticated POST request to the web portal API.

        The FoxESS web portal uses POST with JSON bodies for data
        endpoints (``/dew/v0/...``).  GET requests to these endpoints
        return 405 Method Not Allowed.
        """
        await self.async_ensure_token()
        session = self._get_session()
        headers = await self._async_make_headers(path)
        url = f"{self.BASE_URL}{path}"
        async with session.post(
            url,
            headers=headers,
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            data: dict[str, Any] = await resp.json()
        errno = data.get("errno", -1)
        if errno != 0:
            raise FoxESSWebAuthError(
                f"Web API POST {path} failed: errno={errno}, msg={data.get('msg', '?')}"
            )
        return data.get("result")

    @property
    def token(self) -> str | None:
        """Return the current token without refreshing."""
        return self._token

    async def async_close(self) -> None:
        """Close the underlying HTTP session if we created it."""
        if (
            self._owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()
            self._session = None

"""Web portal authentication for FoxESS Cloud.

The FoxESS web dashboard uses a separate login endpoint from the Open API.
This session provides the token needed for the WebSocket real-time stream.
"""

from __future__ import annotations

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
        self._battery_device_id: str | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _make_headers(self, path: str) -> dict[str, str]:
        """Build the required headers including the WASM signature."""
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

    async def async_login(self) -> str:
        """Authenticate with the web portal and return a session token."""
        session = self._get_session()
        url = f"{self.BASE_URL}{self.LOGIN_PATH}"
        headers = self._make_headers(self.LOGIN_PATH)
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

    async def async_get_battery_temperature(
        self,
        plant_id: str,
        battery_sn: str,
        device_sn: str | None = None,
    ) -> float | None:
        """Fetch BMS battery temperature from the web portal.

        The web portal's battery detail endpoint returns the min cell
        temperature reported by the BMS — a different (and operationally
        more relevant) value than the Open API's ``batTemperature``.
        Low BMS temperatures limit charge rate.

        *device_sn* is the inverter's serial number.  When provided, the
        device detail endpoint is called directly with ``sn`` — this
        bypasses the device-list discovery step which may not be available
        on the web portal API.
        """
        return await self._fetch_battery_temperature(plant_id, battery_sn, device_sn)

    async def _fetch_battery_temperature(
        self,
        plant_id: str,
        battery_sn: str,
        device_sn: str | None,
    ) -> float | None:
        """Internal: fetch battery temperature, trying SN-direct first."""
        # Preferred path: use the inverter SN directly with device detail.
        # This avoids the device-list discovery endpoint which may not exist
        # on the web portal API (/dew/v0/plant/device/list returns 404).
        if device_sn:
            return await self._fetch_temp_by_sn(device_sn)

        # Legacy fallback: try device-list discovery (for any environment
        # where the device list endpoint does exist).
        if not self._battery_device_id:
            await self._discover_battery_device_id(plant_id, battery_sn)
        if not self._battery_device_id:
            return None
        return await self._fetch_temp_by_id(self._battery_device_id)

    async def _fetch_temp_by_sn(self, device_sn: str) -> float | None:
        """Fetch battery temperature using the inverter device SN directly."""
        try:
            result = await self.async_post(
                "/dew/v0/device/detail",
                {"sn": device_sn, "category": "battery"},
            )
            return self._extract_temperature(result)
        except (FoxESSWebAuthError, ValueError, TypeError, KeyError) as exc:
            _LOGGER.debug("BMS temperature fetch (by SN) failed: %s", exc)
            return None

    async def _fetch_temp_by_id(self, device_id: str) -> float | None:
        """Fetch battery temperature using a discovered device ID."""
        try:
            result = await self.async_post(
                "/dew/v0/device/detail",
                {"id": device_id, "category": "battery"},
            )
            return self._extract_temperature(result)
        except (FoxESSWebAuthError, ValueError, TypeError, KeyError) as exc:
            _LOGGER.debug("BMS temperature fetch (by ID) failed: %s", exc)
            return None

    @staticmethod
    def _extract_temperature(result: Any) -> float | None:
        """Extract the numeric temperature value from a device detail result."""
        battery = result.get("battery", {})
        temp = battery.get("temperature", {})
        val = temp.get("value")
        return float(val) if val is not None else None

    async def _discover_battery_device_id(self, plant_id: str, battery_sn: str) -> None:
        """Discover the web portal device ID for the BCU battery module.

        Note: the ``/dew/v0/plant/device/list`` endpoint may not exist on
        the FoxESS web portal (returns 404 in production).  When a
        *device_sn* is available, the caller should prefer the SN-direct
        path instead.
        """
        try:
            result = await self.async_post(
                "/dew/v0/plant/device/list",
                {"plantId": plant_id},
            )
            devices: list[dict[str, Any]] = result if isinstance(result, list) else []
            for dev in devices:
                sn = dev.get("sn", "") or dev.get("deviceSN", "")
                if sn == battery_sn:
                    dev_id = dev.get("id") or dev.get("deviceId")
                    if dev_id:
                        self._battery_device_id = str(dev_id)
                        _LOGGER.debug(
                            "Discovered battery device ID: %s",
                            self._battery_device_id,
                        )
                        return
                # Check nested battery lists
                for child in dev.get("children", []):
                    child_sn = child.get("sn", "") or child.get("deviceSN", "")
                    if child_sn == battery_sn:
                        child_id = child.get("id") or child.get("deviceId")
                        if child_id:
                            self._battery_device_id = str(child_id)
                            _LOGGER.debug(
                                "Discovered battery device ID (child): %s",
                                self._battery_device_id,
                            )
                            return
            _LOGGER.warning("Battery SN %s not found in plant device list", battery_sn)
        except (FoxESSWebAuthError, Exception) as exc:
            _LOGGER.warning("Battery device discovery failed: %s", exc)

    async def async_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Perform an authenticated GET request to the web portal API."""
        await self.async_ensure_token()
        session = self._get_session()
        headers = self._make_headers(path)
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
        headers = self._make_headers(path)
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

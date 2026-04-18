"""Low-level FoxESS Cloud API client with authentication."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import Any

import requests


class FoxESSApiError(Exception):
    """Error returned by the FoxESS Cloud API."""

    AUTH_ERRNOS = {41807, 41808, 41809}

    def __init__(self, errno: int, msg: str) -> None:
        self.errno = errno
        super().__init__(f"FoxESS API error {errno}: {msg}")

    @property
    def is_auth_error(self) -> bool:
        return self.errno in self.AUTH_ERRNOS


# HTTP status codes that are transient and worth retrying.
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class FoxESSClient:
    """Handles authentication and HTTP requests to the FoxESS Cloud API."""

    BASE_URL = "https://www.foxesscloud.com"
    MIN_REQUEST_INTERVAL = 5.0
    RATE_LIMIT_RETRIES = 10
    RATE_LIMIT_MAX_DELAY = 30.0
    RATE_LIMIT_ERRNO = 40400
    TRANSIENT_RETRIES = 3

    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key
        if base_url is not None:
            self.BASE_URL = base_url
            # No throttle needed when talking to a local simulator
            self.MIN_REQUEST_INTERVAL = 0.0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "lang": "en"})
        self._last_request_time = 0.0
        self._log = logging.getLogger(__name__)

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.MIN_REQUEST_INTERVAL:
            time.sleep(self.MIN_REQUEST_INTERVAL - elapsed)

    def _record_success(self) -> None:
        """Record the time of a successful request for throttling."""
        self._last_request_time = time.time()

    def _sign(self, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        # NOTE: The separator is the four literal characters \r\n, NOT actual
        # CRLF bytes. The raw f-string (fr'') preserves them as literals.
        signature = hashlib.md5(
            rf"{path}\r\n{self.api_key}\r\n{timestamp}".encode()
        ).hexdigest()
        return {"token": self.api_key, "timestamp": timestamp, "signature": signature}

    def _check_response(self, data: dict[str, Any]) -> Any:
        errno = data.get("errno")
        if errno != 0:
            raise FoxESSApiError(
                errno if isinstance(errno, int) else -1,
                data.get("msg", "Unknown error"),
            )
        return data.get("result")

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^attempt + random jitter."""
        base = self.MIN_REQUEST_INTERVAL
        delay: float = base * (2**attempt) + random.uniform(0, base)
        return min(delay, self.RATE_LIMIT_MAX_DELAY)

    def _is_transient(self, exc: requests.RequestException) -> bool:
        """Check if a request exception is transient and worth retrying."""
        if isinstance(exc, requests.ConnectionError | requests.Timeout):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return exc.response.status_code in _RETRYABLE_STATUS_CODES
        return False

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Make an authenticated GET request with rate-limit retry."""
        max_attempts = self.RATE_LIMIT_RETRIES + 1
        last_exc: Exception | None = None
        data: dict[str, Any] = {}
        for attempt in range(max_attempts):
            self._throttle()
            url = f"{self.BASE_URL}{path}"
            headers = self._sign(path)
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                last_exc = exc
                if self._is_transient(exc) and attempt < self.TRANSIENT_RETRIES:
                    delay = self._backoff_delay(attempt)
                    log = self._log.warning if attempt > 0 else self._log.debug
                    log(
                        "Transient error on GET %s: %s, retrying in %.1fs",
                        path,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise
            self._record_success()
            data = resp.json()
            if data.get("errno") != self.RATE_LIMIT_ERRNO:
                return self._check_response(data)
            last_exc = None
            if attempt < max_attempts - 1:
                delay = self._backoff_delay(attempt)
                self._log.warning("Rate limited, retrying in %.1fs", delay)
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        return self._check_response(data)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        """Make an authenticated POST request with rate-limit retry."""
        max_attempts = self.RATE_LIMIT_RETRIES + 1
        last_exc: Exception | None = None
        data: dict[str, Any] = {}
        for attempt in range(max_attempts):
            self._throttle()
            url = f"{self.BASE_URL}{path}"
            headers = self._sign(path)
            try:
                resp = self.session.post(
                    url, json=body or {}, headers=headers, timeout=30
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                last_exc = exc
                if self._is_transient(exc) and attempt < self.TRANSIENT_RETRIES:
                    delay = self._backoff_delay(attempt)
                    log = self._log.warning if attempt > 0 else self._log.debug
                    log(
                        "Transient error on POST %s: %s, retrying in %.1fs",
                        path,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise
            self._record_success()
            data = resp.json()
            if data.get("errno") != self.RATE_LIMIT_ERRNO:
                return self._check_response(data)
            last_exc = None
            if attempt < max_attempts - 1:
                delay = self._backoff_delay(attempt)
                self._log.warning("Rate limited, retrying in %.1fs", delay)
                time.sleep(delay)
        if last_exc is not None:
            raise last_exc
        return self._check_response(data)

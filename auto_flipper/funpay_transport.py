"""Explicit FunPay proxy configuration, pacing, and safe diagnostics.

No browser impersonation, challenge solver, retry, or direct-connect fallback.
HTTPX proxy and challenge contracts:
https://www.python-httpx.org/advanced/proxies/
https://www.python-httpx.org/environment_variables/
https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/
"""
import asyncio
import random
import time


class FunPayMarketGetPacer:
    """Serialize market GETs and space them across all users of one client.

    The legacy scanner and AccountMarketObserver share a FunPayClient, hence
    they also share this one pacer. A 429 extends the same global cooldown, so
    another node cannot be requested immediately by either component.
    """

    def __init__(self, min_interval: float, jitter: float, backoff_429: float,
                 *, clock=None, sleep=None, rng=None):
        self.min_interval = max(0.0, float(min_interval))
        self.jitter = max(0.0, float(jitter))
        self.backoff_429 = max(self.min_interval, float(backoff_429))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._next_allowed = 0.0
        self._lock = None
        self._loop = None

    def _current_lock(self):
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    async def request(self, request_callable):
        async with self._current_lock():
            delay = max(0.0, self._next_allowed - self._clock())
            if delay:
                await self._sleep(delay)
            try:
                response = await request_callable()
            except Exception:
                self._next_allowed = self._clock() + self.min_interval
                raise
            spacing = self.min_interval + self._rng.uniform(0.0, self.jitter)
            if getattr(response, "status_code", None) == 429:
                spacing = max(spacing, self.backoff_429)
            self._next_allowed = self._clock() + spacing
            return response
import importlib.util
import re
from urllib.parse import unquote, urlsplit

import httpx


class ProxyConfigurationError(ValueError):
    pass


def proxy_options(proxy_url):
    """Never inherit HTTP(S)_PROXY or unrelated SSL settings from the process."""
    if not proxy_url:
        return {"proxy": None, "trust_env": False}
    try:
        parsed = urlsplit(proxy_url)
        if (parsed.scheme not in ("http", "https", "socks5", "socks5h")
                or not parsed.hostname or parsed.port is not None and parsed.port <= 0
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(char.isspace() for char in proxy_url)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ProxyConfigurationError("FUNPAY_PROXY_INVALID: expected HTTP/HTTPS/SOCKS5 proxy URL") from None
    if parsed.scheme in ("socks5", "socks5h") and importlib.util.find_spec("socksio") is None:
        raise ProxyConfigurationError("FUNPAY_PROXY_SOCKS_DEPENDENCY_MISSING: install httpx[socks]")
    return {"proxy": proxy_url, "trust_env": False}


def response_problem(response):
    """A 403 alone does not identify a Cloudflare challenge or its root cause."""
    marker = response.headers.get("cf-mitigated", "")
    body = response.text.lower()
    challenged = (isinstance(marker, str) and marker.lower() == "challenge") or any(
        signal in body for signal in ("cf-browser-verification", "cf-chl-",
                                     "<title>just a moment", "attention required! | cloudflare"))
    if challenged:
        return "CLOUDFLARE_CHALLENGE"
    if response.status_code == 403:
        return "HTTP_403_ACCESS_DENIED"
    if response.status_code == 407:
        return "HTTP_407_PROXY_AUTH_REQUIRED"
    if response.status_code == 429:
        return "HTTP_429_RATE_LIMITED"
    if response.status_code >= 400:
        return "HTTP_" + str(response.status_code)
    return None


def safe_error(error, proxy_url="", golden_key=""):
    """Transport errors may include proxy credentials; never expose raw URLs."""
    if isinstance(error, httpx.RequestError):
        return type(error).__name__ + ": FunPay connection failed; check transport configuration"
    message = str(error)
    secrets = [proxy_url, golden_key]
    try:
        parsed = urlsplit(proxy_url)
        secrets.extend((parsed.username, parsed.password))
    except (ValueError, TypeError):
        pass
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]").replace(unquote(secret), "[redacted]")
    return re.sub(r"(?:https?|socks5h?)://[^\s'\"<>]+", "[redacted URL]", message)

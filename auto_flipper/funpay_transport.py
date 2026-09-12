"""Explicit FunPay proxy configuration and safe transport diagnostics.

No browser impersonation, challenge solver, retry, or direct-connect fallback.
HTTPX proxy and challenge contracts:
https://www.python-httpx.org/advanced/proxies/
https://www.python-httpx.org/environment_variables/
https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/
"""
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

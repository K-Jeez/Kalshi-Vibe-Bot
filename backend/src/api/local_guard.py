"""Block trading and other state-changing routes from public clients.

Loopback and private LAN addresses (a phone on the home network) are allowed.
A public address needs ``LOCAL_API_TOKEN`` and ``X-Vibe-Bot-Token``.
Read-only GETs are unchanged.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.config import settings

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

_EXACT_MUTATIONS = frozenset(
    {
        "/bot/state",
        "/trade",
        "/analyze",
        "/vault/transfer",
        "/history/reset",
        "/portfolio/live/cancel-resting-orders",
        "/portfolio/live/reconcile",
        "/positions/closed/refresh-resolution",
        "/tuning/strategy-knobs",
        "/tuning/reset-to-config-defaults",
    }
)


def _private_lan(host: str) -> bool:
    text = host
    if text.startswith("::ffff:"):
        text = text.split("::ffff:", 1)[1]
    parts = text.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return False
    if any(part < 0 or part > 255 for part in octets):
        return False
    first, second = octets[0], octets[1]
    if first == 10:
        return True
    if first == 192 and second == 168:
        return True
    if first == 172 and 16 <= second <= 31:
        return True
    return first == 169 and second == 254


def _ipv6_private(host: str) -> bool:
    """Unique-local (fc00::/7) and link-local (fe80::/10), including a zone id."""
    text = host.split("%", 1)[0]
    if text.startswith("::ffff:"):
        return _private_lan(text.split("::ffff:", 1)[1])
    if text.startswith("fe80:"):
        return True
    return text.startswith("fc") or text.startswith("fd")


def client_is_local(host: str | None) -> bool:
    """Loopback or a private LAN address. Public internet addresses are not local."""
    h = (host or "").strip().lower()
    if h in _LOCAL_HOSTS:
        return True
    if h.startswith("::ffff:127.0.0.1"):
        return True
    if _private_lan(h):
        return True
    return _ipv6_private(h)


def path_is_protected(method: str, path: str) -> bool:
    m = (method or "").upper()
    p = (path or "").split("?", 1)[0].rstrip("/") or "/"
    if m == "GET" and p in ("/logs", "/debug/raw"):
        return True
    if m not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    if p in _EXACT_MUTATIONS:
        return True
    return m == "POST" and p.startswith("/positions/") and p.endswith("/close")


def mutation_is_allowed(*, host: str | None, headers, configured_token: str) -> bool:
    if client_is_local(host):
        return True
    token = (configured_token or "").strip()
    if not token:
        return False
    supplied = ""
    if headers is not None:
        supplied = (headers.get("x-vibe-bot-token") or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, token)


class LocalMutationGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or not path_is_protected(request.method, request.url.path):
            return await call_next(request)
        host = request.client.host if request.client else None
        if mutation_is_allowed(
            host=host,
            headers=request.headers,
            configured_token=getattr(settings, "local_api_token", "") or "",
        ):
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "This action is accepted from this computer and from private LAN "
                    "addresses without a token. A public address needs LOCAL_API_TOKEN "
                    "and the X-Vibe-Bot-Token header."
                )
            },
        )

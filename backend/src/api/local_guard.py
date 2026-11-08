"""Block trading and other state-changing routes from non-local clients.

The dashboard on this computer talks to 127.0.0.1 / localhost and needs no token.
If ``HOST`` is opened to the network, those routes stay denied until
``LOCAL_API_TOKEN`` is set and the caller sends ``X-Vibe-Bot-Token``.
Read-only GETs are unchanged so a local UI keeps loading.
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
        "/tuning/stop-loss-selling",
        "/tuning/reset-to-config-defaults",
    }
)


def client_is_local(host: str | None) -> bool:
    h = (host or "").strip().lower()
    if h in _LOCAL_HOSTS:
        return True
    return h.startswith("::ffff:127.0.0.1")


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
                    "This action is accepted from this computer without a token. "
                    "Remote clients need LOCAL_API_TOKEN and the X-Vibe-Bot-Token header. "
                    "The local dashboard at http://localhost:3000 is unchanged."
                )
            },
        )

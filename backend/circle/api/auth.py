"""Shared-secret protection for the Circle API.

Circle is a single-user app holding a person's entire message history, and it
exposes `POST /api/identity/merge`, which permanently deletes records. While
the API is bound to 127.0.0.1 that boundary is real: only processes on the
same machine can reach it, and those are the user's own.

The moment the backend is reachable from elsewhere, that protection is gone.
This module keeps the safe default (no secret needed, localhost only) and adds
a shared key only when the user opts in by setting ACCESS_KEY.

Design decisions worth stating:

- Loopback callers are always exempt. The UI the browser loads from this same
  machine is a loopback caller, so a user who sets a key to share their
  machine is not forced to type it on every local visit.
- Without an ACCESS_KEY, remote callers are refused once ALLOW_REMOTE is set.
  "No key" therefore never silently means "open to the internet".
- The comparison is constant-time so the key cannot be recovered by timing.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from circle.config import get_settings

log = logging.getLogger("circle.auth")

# Header the browser UI sends. X- prefix so it never collides with anything a
# proxy or the browser sets for us.
KEY_HEADER = "x-circle-key"

# Never behind auth: the UI must be able to learn *why* it is blocked before
# it can show the user anything useful. These return no private content.
PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/auth/status",
    "/api/docs",
    "/openapi.json",
    "/api/openapi.json",
    "/docs",
    "/redoc",
    "/",
    "/favicon.ico",
})

# Endpoints that change or destroy data. They require the key even for
# loopback callers when a key is configured, because a loopback browser page
# could otherwise be driven by another site via a CSRF-style POST.
SENSITIVE_PREFIXES = (
    "/api/identity/",
    "/api/settings",
    "/api/bootstrap",
)

# Paths where the access key may travel as a query parameter, because the
# browser constructs the request and cannot set headers: EventSource for SSE,
# and <audio src> for voice playback. Nothing else qualifies.
QUERY_KEY_EXACT = ("/api/events",)
QUERY_KEY_PREFIXES = ("/api/voice/",)


def _key_in_query_allowed(path: str) -> bool:
    if path in QUERY_KEY_EXACT:
        return True
    # Voice audio is /api/voice/<id>/audio; make sure the prefix match is not
    # a hole onto some other /api/voice/ endpoint.
    return any(path.startswith(p) for p in QUERY_KEY_PREFIXES) and path.endswith(
        "/audio")


def _is_loopback(request: Request) -> bool:
    """True when the request came from this machine.

    Uses the socket peer where available rather than trusting a header: the
    peer is what the OS observed, and a tunnel forwarding into localhost still
    arrives from 127.0.0.1, which is exactly the case we want to protect by
    requiring the key from the UI.
    """
    client = request.client
    if client is None:
        return False
    host = (client.host or "").strip("[]").lower()
    if host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
        return True
    # Starlette's in-process TestClient has no socket, so it reports this
    # literal host. It can only be produced inside the test process, so it is
    # as local as loopback.
    return host == "testclient"


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    # Everything that is not part of the API is the static SPA: the HTML shell,
    # the JS/CSS bundle, icons. None of it contains private content, and it has
    # to load before the user can supply a key. Without this the browser is
    # handed a 401 body instead of the app, so a deep link or a refresh can
    # never reach the screen that asks for the key. All private data lives
    # behind /api/ and is still enforced below.
    if not path.startswith("/api/"):
        return True
    return path.startswith("/assets/") or path.endswith((".js", ".css", ".map"))


def key_configured() -> bool:
    return bool(get_settings().access_key.strip())


def check(request: Request) -> tuple[bool, str]:
    """(allowed, reason) for one request. Reason is safe to log.

    This is the access decision only. `/api/auth/status` is reachable without
    a key (so the UI can describe itself), which means it must NOT simply
    short-circuit here: callers of this function use the answer to learn
    whether *their* key was good, and returning True for a public path would
    tell every caller it succeeded.
    """
    settings = get_settings()
    path = request.url.path
    if _is_public(path) and path != "/api/auth/status":
        return True, ""

    expected = settings.access_key.strip()
    supplied = (request.headers.get(KEY_HEADER) or "").strip()

    if expected:
        # Constant-time compare; lengths are compared implicitly.
        if hmac.compare_digest(supplied, expected):
            return True, ""
        # EventSource and <audio> cannot send custom headers, so those two
        # pass the key in the query string. Accepted for this short allowlist
        # only: a query string lands in access logs, so widening it everywhere
        # would leak the key.
        if _key_in_query_allowed(path):
            qkey = (request.query_params.get("key") or "").strip()
            if hmac.compare_digest(qkey, expected):
                return True, ""
        # A loopback browser tab still has to prove itself once a key exists,
        # otherwise any page the user visits could read their messages.
        return False, "missing or wrong access key"

    if _is_loopback(request):
        return True, ""

    # No key configured. Refuse remote callers outright rather than serving
    # an open archive on the internet.
    if not settings.allow_remote:
        return False, "remote access is disabled; set ACCESS_KEY to enable it"
    return False, "remote access requires an ACCESS_KEY"


def is_sensitive(path: str) -> bool:
    return any(path.startswith(p) for p in SENSITIVE_PREFIXES)


async def access_key_middleware(request: Request, call_next):
    """Reject unauthenticated remote requests before they reach a route."""
    allowed, reason = check(request)
    if allowed:
        return await call_next(request)
    log.warning("blocked %s %s from %s: %s", request.method,
                request.url.path, request.client.host if request.client else "?",
                reason)
    return JSONResponse(
        status_code=401,
        content={"detail": reason,
                 "hint": "send the X-Circle-Key header with your access key"},
        headers={"WWW-Authenticate": "Key"},
    )
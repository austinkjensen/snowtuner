"""Pluggable auth for the snowtuner HTTP + MCP surfaces.

Two modes shipped today (set via the ``SNOWTUNER_AUTH_MODE`` env var):

  * ``none``  — open access.  Only safe when binding to localhost; the API
                refuses non-loopback hosts in this mode.
  * ``token`` — single bearer token.  Sourced from ``SNOWTUNER_API_TOKEN``
                if set; otherwise auto-generated to ``~/.snowtuner/api_token``
                on first run.  All endpoints require
                ``Authorization: Bearer <token>``.

Future modes will land here (``oidc`` for multi-user) without changing the
public surface — every caller just imports ``require_auth`` and uses it as
a FastAPI dependency.

Design choices
--------------
* Token comparison uses ``hmac.compare_digest`` to avoid timing leaks.
* The token file is created with mode 0600 so other local users can't read
  it.  Same convention as the keyring fallback in ``credentials/``.
* A handful of paths bypass auth even in ``token`` mode: ``/health``,
  ``/openapi.json``, ``/docs``, ``/redoc``.  Lets ops probes and the
  OpenAPI viewer work without a token.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


_TOKEN_PATH = Path.home() / ".snowtuner" / "api_token"

# Routes that bypass auth even when auth_mode='token'.  Health check is for
# load balancers / supervisors; OpenAPI viewer is convenience.
_PUBLIC_PATHS = frozenset({
    "/health",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
})


def get_auth_mode() -> str:
    """Read the active auth mode from env.  Defaults to 'none' for
    backward compatibility with the existing local-dev setup."""
    return os.environ.get("SNOWTUNER_AUTH_MODE", "none").lower()


def get_or_create_token() -> str:
    """Return the active API token.

    Priority:
      1. ``SNOWTUNER_API_TOKEN`` env var (operator-supplied).
      2. ``~/.snowtuner/api_token`` (auto-generated if missing).

    The auto-generated path is a 32-byte URL-safe token written with
    mode 0600.  Caller is responsible for distributing it (the SPA's
    setup page or the CLI's ``snowtuner api`` startup banner).
    """
    env_token = os.environ.get("SNOWTUNER_API_TOKEN")
    if env_token:
        return env_token.strip()

    if _TOKEN_PATH.exists():
        return _TOKEN_PATH.read_text().strip()

    # First run on this machine — generate, persist with restrictive perms.
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    _TOKEN_PATH.write_text(token + "\n")
    os.chmod(_TOKEN_PATH, 0o600)
    logger.warning(
        "Generated new API token at %s.  Copy it into the snowtuner UI "
        "or use it as a Bearer header in API/MCP calls.",
        _TOKEN_PATH,
    )
    return token


def require_auth(request: Request) -> None:
    """FastAPI dependency: validate the request against the active mode.

    Used as ``Depends(require_auth)`` on the app's router so EVERY endpoint
    inherits the check.  Public paths short-circuit through.
    """
    if request.url.path in _PUBLIC_PATHS:
        return

    mode = get_auth_mode()
    if mode == "none":
        # Loopback check: refuse to authorize a non-loopback request when
        # auth is disabled.  Belt-and-suspenders alongside the host-binding
        # check at app startup.
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(
                403,
                "SNOWTUNER_AUTH_MODE=none but the request originated from a "
                f"non-loopback host ({host!r}).  Set SNOWTUNER_AUTH_MODE=token "
                "and configure a bearer token before exposing the API to "
                "remote clients.",
            )
        return

    if mode == "token":
        expected = get_or_create_token()
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(
                401,
                "missing Authorization: Bearer <token>",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = header.split(" ", 1)[1].strip()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "invalid bearer token")
        return

    raise HTTPException(
        500,
        f"unknown SNOWTUNER_AUTH_MODE={mode!r}; "
        f"valid: none, token",
    )


def assert_safe_host(host: str) -> None:
    """Called at app startup.  Refuses to start the server if auth_mode is
    'none' but the configured bind host isn't loopback.
    """
    if get_auth_mode() != "none":
        return
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    raise SystemExit(
        f"refusing to start: SNOWTUNER_AUTH_MODE=none but bound to "
        f"non-loopback host {host!r}.  Either bind to 127.0.0.1 or set "
        f"SNOWTUNER_AUTH_MODE=token."
    )

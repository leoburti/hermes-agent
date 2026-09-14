"""ApiServerKeyTokenProvider — dashboard token auth using the existing API_SERVER_KEY.

Hermes ships two independent auth surfaces on the dashboard host: the Gateway API Server
(Bearer API_SERVER_KEY, `/v1/*` + `/api/sessions*`) and the Dashboard REST API (cookie session,
`/api/model/*`, `/api/skills`, `/api/cron/*`, ...). A native client that already holds a valid
API_SERVER_KEY (Hermes Android, hermes-desktop) has no browser to complete a cookie login, so it
could reach the Gateway but not the Dashboard-only management surface on the exact same host.

This provider closes that gap through the existing route-agnostic bearer-token seam
(`hermes_cli/dashboard_auth/token_auth.py`) instead of widening any cookie-auth code path: a
request bearing `Authorization: Bearer <API_SERVER_KEY>` against one of the explicitly registered
JSON API routes is accepted as an already-trusted machine credential, with no new secret, no
session, no cookie. Scope is intentionally narrow — registered routes only, JSON API paths that
already assume a non-interactive Bearer caller elsewhere in the codebase (mirrors `/v1/*` and
`/api/sessions*` in `gateway/platforms/api_server.py`). It is not registered as a session
provider (no login page entry, no OAuth flow, no cookie) — token auth only.
"""

from __future__ import annotations

import hmac
import logging
import os

from hermes_cli.dashboard_auth import DashboardAuthProvider, ProviderError, TokenPrincipal
from hermes_cli.dashboard_auth.token_auth import register_token_route
from plugins.dashboard_auth._shared import NonInteractiveMixin, SkipRegistration

logger = logging.getLogger(__name__)
_TAG = "dashboard-auth-api-server-key"

# JSON API routes the app already reaches on the Gateway host with the same API_SERVER_KEY
# (Settings/Memory/Skills/Cron tabs). Deliberately narrow: adding a route here is a conscious
# widening of the machine-credential surface, never a wildcard prefix.
_TOKEN_ROUTES: tuple[str, ...] = (
    "/api/model/info",
    "/api/model/options",
    "/api/model/set",
    "/api/skills",
    "/api/cron/jobs",
)

LAST_SKIP_REASON: str = ""


class ApiServerKeyTokenProvider(NonInteractiveMixin, DashboardAuthProvider):
    """Non-interactive provider: a Bearer token equal to API_SERVER_KEY is accepted on the
    registered dashboard JSON routes. Never a session/cookie provider."""

    name = "api_server_key"
    display_name = "API Server Key"
    supports_session = False
    supports_password = False
    supports_token = True

    _NOT_INTERACTIVE = "api_server_key is a non-interactive token provider (no browser login)"

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("api_key must be non-empty")
        self._api_key = api_key

    def verify_token(self, *, token: str) -> "TokenPrincipal | None":
        if hmac.compare_digest(token.encode(), self._api_key.encode()):
            return TokenPrincipal(principal="api_server_key", provider=self.name)
        return None

    # Session-provider abstract methods: unreachable (supports_session=False keeps this
    # provider out of list_session_providers()/the login page), but DashboardAuthProvider is
    # a Protocol-checked ABC so the methods must exist.
    def verify_session(self, *, access_token: str):  # noqa: D102
        return None

    def refresh_session(self, *, refresh_token: str):  # noqa: D102
        raise NotImplementedError(self._NOT_INTERACTIVE)

    def revoke_session(self, *, refresh_token: str) -> None:  # noqa: D102
        return None


def _settings() -> dict:
    api_key = os.environ.get("API_SERVER_KEY", "").strip()
    if not api_key:
        raise SkipRegistration("API_SERVER_KEY not configured; token route stays 401-only")
    return {"api_key": api_key}


def register(ctx) -> None:
    """Register ``ApiServerKeyTokenProvider`` when API_SERVER_KEY is configured, and mark the
    dashboard JSON routes it covers as token-authable. A no-op when no key is set — the routes
    then simply have no token provider and fail closed via the normal cookie gate."""
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    try:
        kwargs = _settings()
        provider = ApiServerKeyTokenProvider(**kwargs)
    except SkipRegistration as skip:
        LAST_SKIP_REASON = skip.reason
        logger.debug("%s: %s", _TAG, skip.reason)
        return
    except ValueError as exc:
        LAST_SKIP_REASON = str(exc)
        logger.warning("%s: %s", _TAG, exc)
        return
    ctx.register_dashboard_auth_provider(provider)
    for path in _TOKEN_ROUTES:
        register_token_route(path)
    logger.info(
        "%s: registered API_SERVER_KEY as a dashboard token provider for %d route(s)",
        _TAG, len(_TOKEN_ROUTES))

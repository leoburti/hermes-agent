"""Route-agnostic non-interactive (bearer-token) auth seam for the dashboard.

Any machine-credential provider plugs in here. A route opts in by registering its exact path via
:func:`register_token_route`; only registered paths are token-authable, so the auth surface of
existing routes never widens. :func:`token_auth_middleware` runs OUTERMOST (installed last) and
owns the decision for a token route: a recognised token attaches ``request.state.token_principal``
+ ``token_authenticated`` (the cookie gates honour that flag and never bounce to /login);
otherwise 401, or 503 when a provider's backing store was unreachable. Fails closed.
"""
from __future__ import annotations

import logging
import threading
from typing import Awaitable, Callable, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from hermes_cli.dashboard_auth import list_token_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import ProviderError, TokenPrincipal
from hermes_cli.dashboard_auth.cookies import read_session_cookies
from hermes_cli.dashboard_auth.request_utils import (
    client_ip as _client_ip, extract_bearer as extract_bearer_token, unreachable_response)

_log = logging.getLogger(__name__)

_token_routes: set[str] = set()  # exact paths that accept bearer-token auth
# Registered templates, pre-split into segments: ("api", "sessions", "{session_id}", "messages").
# A placeholder segment matches exactly one non-empty path segment — never a prefix wildcard.
_token_route_templates: set[tuple[str, ...]] = set()
_lock = threading.Lock()


def _split(path: str) -> tuple[str, ...]:
    return tuple(path.strip("/").split("/"))


def _is_template(path: str) -> bool:
    return "{" in path


def _template_matches(template: tuple[str, ...], segments: tuple[str, ...]) -> bool:
    """True when ``segments`` fills every placeholder of ``template`` and nothing else.

    Arity is fixed (a template never spans a different number of segments) and a
    placeholder never matches an empty segment, so ``/api/sessions//messages`` and
    ``/api/sessions/x/messages/extra`` stay unregistered.
    """
    if len(template) != len(segments):
        return False
    return all(
        bool(actual) if expected.startswith("{") and expected.endswith("}") else expected == actual
        for expected, actual in zip(template, segments))


def _has_session_cookie(request: Request) -> bool:
    """True when the caller carries either session cookie, whatever its validity.

    Presence alone decides who the cookie gate gets to judge; ``gated_auth_middleware``
    owns the verdict, so an expired or forged cookie is rejected there, not waved through.
    """
    access_token, refresh_token = read_session_cookies(request)
    return bool(access_token or refresh_token)


def register_token_route(path: str) -> None:
    """Mark ``path`` as token-authable. Idempotent; does NOT make the route public.

    ``path`` is an exact path, or a template carrying ``{placeholder}`` segments for a
    route whose id varies per request (``/api/sessions/{session_id}/messages``). A
    placeholder matches exactly ONE non-empty segment, so registering a template never
    widens the surface to a sibling or deeper route.
    """
    with _lock:
        if _is_template(path):
            _token_route_templates.add(_split(path))
        else:
            _token_routes.add(path)


def is_token_route(path: str) -> bool:
    """True if ``path`` was registered as token-authable, exactly or via a template."""
    with _lock:
        if path in _token_routes:
            return True
        templates = tuple(_token_route_templates)
    if not templates:
        return False
    segments = _split(path)
    return any(_template_matches(template, segments) for template in templates)


def clear_token_routes() -> None:
    """Test-only: drop all registered token routes."""
    with _lock:
        _token_routes.clear()
        _token_route_templates.clear()


def authenticate_token(request: Request) -> Tuple[Optional[TokenPrincipal], Optional[str]]:
    """Try every token provider against the request's bearer token. Returns ``(principal, None)``
    on success; ``(None, None)`` for no token or no recogniser (401); ``(None, name)`` when no
    provider accepted it AND at least one was unreachable (caller surfaces 503). Never raises."""
    token = extract_bearer_token(request)
    if not token:
        return None, None
    unreachable: Optional[str] = None
    for provider in list_token_providers():
        try:
            principal = provider.verify_token(token=token)
        except ProviderError as e:
            _log.warning("dashboard-auth: token provider %r unreachable during verify: %s",
                         provider.name, e)
            if unreachable is None:
                unreachable = provider.name
            continue
        except Exception as e:  # noqa: BLE001 — a buggy provider must not 500 the gate
            _log.warning("dashboard-auth: token provider %r raised during verify: %s",
                         provider.name, e)
            continue
        if principal is not None:
            return principal, None
    return None, unreachable


async def token_auth_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Pass-through for unregistered paths; for a token route, valid token -> attach principal +
    flag, unreachable -> 503, else 401.

    A caller that presents NO bearer token but DOES carry a session cookie is handed to
    ``gated_auth_middleware`` to be judged on that cookie: registering a route for bearer
    auth is additive and must not revoke the cookie access it already had (the mobile app
    logs in via ``/auth/password-login`` and then calls these routes with the cookie alone).
    A caller with neither credential still gets the 401 here — never widen an unauthenticated
    request, and in particular never let a route reach a public-path exemption it would not
    have reached on its own.
    """
    path = request.url.path
    if not is_token_route(path):
        return await call_next(request)
    if not extract_bearer_token(request) and _has_session_cookie(request):
        return await call_next(request)
    principal, unreachable = authenticate_token(request)
    if principal is not None:
        request.state.token_principal = principal
        request.state.token_authenticated = True
        return await call_next(request)
    if unreachable:
        audit_log(
            AuditEvent.TOKEN_AUTH_FAILURE, provider=unreachable, reason="provider_unreachable",
            path=path, ip=_client_ip(request))
        return unreachable_response(unreachable)

    audit_log(
        AuditEvent.TOKEN_AUTH_FAILURE, reason="no_provider_recognises_token", path=path,
        ip=_client_ip(request))
    return JSONResponse({"error": "unauthenticated", "detail": "Unauthorized"}, status_code=401)

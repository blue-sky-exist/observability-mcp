from __future__ import annotations

import httpx

from .config import BackendAuth


def httpx_auth_and_headers(auth: BackendAuth) -> tuple[httpx.Auth | None, dict[str, str]]:
    headers: dict[str, str] = {"Accept": "application/json"}
    http_auth: httpx.Auth | None = None
    if auth.type == "basic":
        http_auth = httpx.BasicAuth(auth.username, auth.password)
    elif auth.type == "bearer":
        headers["Authorization"] = f"Bearer {auth.token}"
    elif auth.type == "api-key":
        headers["Authorization"] = f"ApiKey {auth.api_key}"
    return http_auth, headers

"""HTTP Basic auth on every route. Credentials come from DESK_BASIC_AUTH_USER/PASS.

If neither is set the app refuses to start unless DESK_ALLOW_NO_AUTH=1 (local dev only)."""

from __future__ import annotations

import base64
import os
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from desk.config import Settings


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.user = settings.basic_auth_user
        self.password = settings.basic_auth_pass
        if not (self.user and self.password) and os.environ.get("DESK_ALLOW_NO_AUTH") != "1":
            raise RuntimeError(
                "Set DESK_BASIC_AUTH_USER and DESK_BASIC_AUTH_PASS (or DESK_ALLOW_NO_AUTH=1 for local dev)"
            )

    def _authorised(self, header: str | None) -> bool:
        if not (self.user and self.password):
            return True
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            user, _, pwd = raw.partition(":")
        except Exception:  # noqa: BLE001
            return False
        return secrets.compare_digest(user, self.user) and secrets.compare_digest(
            pwd, self.password
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if not self._authorised(request.headers.get("authorization")):
            return Response(
                "Unauthorized", status_code=401, headers={"WWW-Authenticate": 'Basic realm="desk"'}
            )
        return await call_next(request)

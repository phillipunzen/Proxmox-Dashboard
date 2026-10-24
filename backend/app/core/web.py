"""Security headers and serving of the built single-page app."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline headers for every response. The SPA adds its own CSP (see below)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.scheme == "https":  # browsers ignore (and warn about) COOP over http
            h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """Reject cross-site write requests: if a browser sends an Origin, it must be our host.

    Complements SameSite=Strict cookies and the CSRF token (defence in depth). Requests
    without Origin (curl, scripts) are left to the CSRF/auth checks.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            host = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if origin and origin != "null" and urlsplit(origin).netloc != host:
                return JSONResponse({"detail": "Cross-Origin-Anfrage abgelehnt"}, status_code=403)
        return await call_next(request)


def spa_csp(index_html: str) -> str:
    """Strict CSP for the SPA; SvelteKit's inline bootstrap script is allowed by hash."""
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(m.encode()).digest()).decode()}'"
        for m in _INLINE_SCRIPT.findall(index_html)
    )
    return (
        "default-src 'self'; "
        f"script-src 'self' {hashes}; "
        # ECharts and Svelte transitions set inline style attributes.
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )


def mount_spa(app: FastAPI, static_dir: str) -> bool:
    """Serve the SvelteKit build with an ``index.html`` fallback for client-side routes."""
    root = Path(static_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        return False
    index_html = index.read_text(encoding="utf-8")
    csp = spa_csp(index_html)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            headers = {}
            if path.startswith("_app/immutable/"):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return FileResponse(candidate, headers=headers)
        return HTMLResponse(index_html, headers={"Content-Security-Policy": csp,
                                                 "Cache-Control": "no-cache"})

    return True

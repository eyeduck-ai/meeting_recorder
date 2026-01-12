"""Simple password authentication for Web UI and API."""

import hashlib
import hmac
import time

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.settings import get_settings


def _generate_session_token(password: str, secret: str, timestamp: int) -> str:
    """Generate a session token."""
    message = f"{password}:{timestamp}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _verify_session_token(token: str, secret: str, max_age: int) -> bool:
    """Verify a session token."""
    try:
        parts = token.split(":")
        if len(parts) != 2:
            return False
        timestamp = int(parts[0])
        signature = parts[1]

        # Check if token expired
        if time.time() - timestamp > max_age:
            return False

        # Verify signature
        settings = get_settings()
        expected = _generate_session_token(settings.auth_password, secret, timestamp)
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def create_session_token() -> str:
    """Create a new session token."""
    settings = get_settings()
    timestamp = int(time.time())
    signature = _generate_session_token(settings.auth_password, settings.auth_session_secret, timestamp)
    return f"{timestamp}:{signature}"


def is_authenticated(request: Request) -> bool:
    """Check if request is authenticated."""
    settings = get_settings()

    # No password configured = no auth required
    if not settings.auth_password:
        return True

    # Check session cookie
    token = request.cookies.get("session")
    if token:
        return _verify_session_token(token, settings.auth_session_secret, settings.auth_session_max_age)

    # Check API key header (for API access)
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return hmac.compare_digest(api_key, settings.auth_password)

    return False


# Paths that don't require authentication
PUBLIC_PATHS = [
    "/health",
    "/login",
    "/static",
    "/favicon.ico",
]


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to check authentication."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        # No password = no auth required
        if not settings.auth_password:
            return await call_next(request)

        path = request.url.path

        # Check if path is public
        for public_path in PUBLIC_PATHS:
            if path == public_path or path.startswith(public_path + "/"):
                return await call_next(request)

        # Check authentication
        if is_authenticated(request):
            return await call_next(request)

        # Not authenticated
        if path.startswith("/api/"):
            # API requests get 401
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        else:
            # Web requests redirect to login
            return RedirectResponse(url=f"/login?next={path}", status_code=302)


def require_auth(request: Request):
    """Dependency to require authentication."""
    settings = get_settings()

    if settings.auth_password and not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")

    return True

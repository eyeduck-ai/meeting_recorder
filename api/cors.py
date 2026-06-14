"""CORS configuration helpers."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def parse_cors_allowed_origins(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize comma-separated or sequence CORS origins."""
    if value is None:
        return []
    if isinstance(value, str):
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
    else:
        origins = [origin.strip() for origin in value if origin.strip()]

    if "*" in origins:
        raise ValueError("CORS_ALLOWED_ORIGINS must list explicit origins; wildcard '*' is not allowed")

    return origins


def configure_cors(app: FastAPI, allowed_origins: str | list[str] | tuple[str, ...] | None) -> None:
    """Install CORS middleware only when explicit origins are configured."""
    origins = parse_cors_allowed_origins(allowed_origins)
    if not origins:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

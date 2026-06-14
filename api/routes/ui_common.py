"""Shared Web UI template helpers."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from config.settings import get_settings
from utils.environment import get_environment_status
from utils.timezone import utc_now

settings = get_settings()

templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def localtime_filter(value: datetime | None, format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Convert UTC datetime to local timezone and format."""
    if not value:
        return "-"
    try:
        tz = ZoneInfo(settings.timezone)
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        utc_dt = value.replace(tzinfo=ZoneInfo("UTC"))
        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime(format)
    except Exception:
        return value.strftime(format) if value else "-"


templates.env.filters["localtime"] = localtime_filter


def get_context(request: Request, **kwargs) -> dict:
    """Build template context with common data."""
    env_status = get_environment_status()
    return {
        "request": request,
        "now": utc_now(),
        "auth_enabled": bool(settings.auth_password),
        "env_status": env_status,
        **kwargs,
    }


def render_template(request: Request, name: str, **kwargs):
    """Render templates using the Starlette 1.0-compatible signature."""
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=get_context(request, **kwargs),
    )

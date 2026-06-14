"""Web UI settings routes."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from api.routes import ui_common
from database.session import get_db
from services.app_settings import get_all_settings

router = APIRouter(tags=["ui"])


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """Settings page."""
    from uploading.youtube import get_youtube_uploader

    uploader = get_youtube_uploader()
    youtube_status = {
        "configured": ui_common.settings.youtube_configured,
        "authorized": uploader.is_authorized if ui_common.settings.youtube_configured else False,
    }

    telegram_status = {
        "configured": bool(ui_common.settings.telegram_bot_token),
    }

    app_settings = get_all_settings(db)

    return ui_common.render_template(
        request,
        "settings.html",
        youtube_status=youtube_status,
        telegram_status=telegram_status,
        settings=ui_common.settings,
        app_settings=app_settings,
    )

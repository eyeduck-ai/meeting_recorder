"""Web UI routes for meetings."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from api.routes import ui_common
from database.models import Meeting
from database.session import get_db
from providers import list_provider_metadata, provider_form_config_map, validate_provider_name
from services.errors import NotFoundError
from services.meeting_service import MeetingCreateData, get_meeting_service

router = APIRouter(tags=["ui"])


@router.get("/meetings", response_class=HTMLResponse)
async def meetings_list(request: Request, db: Session = Depends(get_db)):
    """Meetings list page."""
    meetings = db.query(Meeting).order_by(Meeting.created_at.desc()).all()
    return ui_common.render_template(
        request,
        "meetings/list.html",
        meetings=meetings,
        providers={provider.name: provider for provider in list_provider_metadata()},
    )


@router.get("/meetings/new", response_class=HTMLResponse)
async def meetings_new(request: Request):
    """New meeting form."""
    return ui_common.render_template(
        request,
        "meetings/form.html",
        meeting=None,
        providers=list_provider_metadata(),
        provider_configs=json.dumps(provider_form_config_map()),
    )


@router.get("/meetings/{meeting_id}/edit", response_class=HTMLResponse)
async def meetings_edit(request: Request, meeting_id: int, db: Session = Depends(get_db)):
    """Edit meeting form."""
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return ui_common.render_template(
        request,
        "meetings/form.html",
        meeting=meeting,
        providers=list_provider_metadata(),
        provider_configs=json.dumps(provider_form_config_map()),
    )


@router.post("/meetings/save", response_class=HTMLResponse)
async def meetings_save(
    request: Request,
    db: Session = Depends(get_db),
    meeting_id: int | None = Form(None),
    name: str = Form(...),
    provider: str = Form(...),
    meeting_code: str = Form(...),
    site_base_url: str | None = Form(None),
    password: str | None = Form(None),
    clear_password: bool = Form(False),
    default_display_name: str | None = Form(None),
    default_guest_email: str | None = Form(None),
):
    """Save meeting (create or update)."""
    service = get_meeting_service()
    try:
        provider_name = validate_provider_name(provider)
        if meeting_id:
            update_data = {
                "name": name,
                "provider": provider_name,
                "meeting_code": meeting_code,
                "site_base_url": site_base_url or None,
                "default_display_name": default_display_name or None,
                "default_guest_email": default_guest_email or None,
            }
            if clear_password:
                update_data["password"] = None
            elif password:
                update_data["password"] = password

            service.update_meeting(
                db,
                meeting_id,
                update_data,
            )
        else:
            service.create_meeting(
                db,
                MeetingCreateData(
                    name=name,
                    provider=provider_name,
                    meeting_code=meeting_code,
                    site_base_url=site_base_url or None,
                    password=password or None,
                    default_display_name=default_display_name or None,
                    default_guest_email=default_guest_email or None,
                ),
            )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/meetings", status_code=303)


@router.delete("/meetings/{meeting_id}", response_class=HTMLResponse)
async def meetings_delete(meeting_id: int, db: Session = Depends(get_db)):
    """Delete meeting."""
    try:
        get_meeting_service().delete_meeting(db, meeting_id)
    except NotFoundError:
        pass
    return HTMLResponse("")

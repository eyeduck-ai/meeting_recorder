"""Web UI authentication routes."""

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.routes import ui_common

router = APIRouter(tags=["ui"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = None):
    """Login page."""
    if not ui_common.settings.auth_password:
        return RedirectResponse(url="/", status_code=302)

    return ui_common.render_template(request, "login.html", next_url=next, error=error)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
):
    """Handle login form submission."""
    if not ui_common.settings.auth_password:
        return RedirectResponse(url="/", status_code=302)

    if hmac.compare_digest(password, ui_common.settings.auth_password):
        from api.auth import create_session_token

        response = RedirectResponse(url=next, status_code=302)
        response.set_cookie(
            key="session",
            value=create_session_token(),
            max_age=ui_common.settings.auth_session_max_age,
            httponly=True,
            samesite="lax",
        )
        return response
    return ui_common.render_template(request, "login.html", next_url=next, error="Invalid password")


@router.get("/logout")
async def logout():
    """Logout and clear session."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response

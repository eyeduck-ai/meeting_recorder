from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from api.routes.ui_common import render_template
from services.operations import OperationsService

router = APIRouter(tags=["ui"])


@router.get("/operations", response_class=HTMLResponse)
async def operations_page(request: Request):
    return render_template(request, "operations.html", operations=await OperationsService().snapshot(request.app))

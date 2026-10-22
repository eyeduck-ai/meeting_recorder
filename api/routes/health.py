from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.health import get_health_probe
from utils.environment import get_environment_status
from utils.timezone import utc_now

router = APIRouter(tags=["Health"])


@router.get("/health/live")
async def liveness():
    """Event loop liveness; deliberately does not run media or database probes."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(request: Request):
    snapshot = await get_health_probe(request.app).snapshot()
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_ready = bool(scheduler and scheduler.is_running)
    ready = snapshot["recording_runtime"]["ready"] and snapshot["database_ready"] and scheduler_ready
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "scheduler_ready": scheduler_ready, **snapshot},
    )


@router.get("/health")
async def health_check(request: Request):
    """Compatibility health endpoint; readiness decisions should use /health/ready."""
    return {
        "status": "healthy",
        "timestamp": utc_now().isoformat(),
        **(await get_health_probe(request.app).snapshot()),
    }


@router.get("/api")
async def api_info(request: Request):
    return {
        "name": "Meeting Recorder",
        "version": "0.1.0",
        "status": "running",
        "environment": get_environment_status().to_dict(),
        **(await get_health_probe(request.app).snapshot()),
    }


@router.get("/api/environment")
async def environment_status():
    return get_environment_status().to_dict()

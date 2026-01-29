from fastapi import APIRouter

from utils.environment import get_environment_status
from utils.timezone import utc_now

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": utc_now().isoformat(),
    }


@router.get("/api")
async def api_info():
    """API info endpoint."""
    env_status = get_environment_status()
    return {
        "name": "Meeting Recorder",
        "version": "0.1.0",
        "status": "running",
        "environment": env_status.to_dict(),
    }


@router.get("/api/environment")
async def environment_status():
    """Get current environment status and recording capability."""
    env_status = get_environment_status()
    return env_status.to_dict()

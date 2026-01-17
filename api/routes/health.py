from fastapi import APIRouter

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
    return {
        "name": "Meeting Recorder",
        "version": "0.1.0",
        "status": "running",
    }

"""YouTube API routes for authorization and upload management."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from uploading.youtube import (
    AuthorizationError,
    get_youtube_uploader,
)

router = APIRouter(prefix="/youtube", tags=["YouTube"])


class AuthStatusResponse(BaseModel):
    """Authorization status response."""

    configured: bool
    authorized: bool


class DeviceCodeRequest(BaseModel):
    """Start device authorization request."""

    pass


class DeviceAuthResponse(BaseModel):
    """Device authorization response."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int
    message: str


class PollTokenRequest(BaseModel):
    """Poll for token request."""

    device_code: str
    timeout: int = 300


class TokenResponse(BaseModel):
    """Token response."""

    success: bool
    message: str


class UploadRequest(BaseModel):
    """Manual upload request."""

    job_id: str
    title: str | None = None
    description: str | None = None
    privacy: Literal["public", "private", "unlisted"] = "unlisted"


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status():
    """Get YouTube authorization status."""
    uploader = get_youtube_uploader()
    return AuthStatusResponse(
        configured=uploader.is_configured,
        authorized=uploader.is_authorized,
    )


@router.post("/auth/start", response_model=DeviceAuthResponse)
async def start_device_auth():
    """Start device authorization flow.

    Returns a user code and verification URL. The user must visit the URL
    and enter the code to authorize the application.
    """
    uploader = get_youtube_uploader()

    if not uploader.is_configured:
        raise HTTPException(
            status_code=400,
            detail="YouTube client ID and secret not configured. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in environment.",
        )

    try:
        response = await uploader.start_device_authorization()
        return DeviceAuthResponse(
            device_code=response.device_code,
            user_code=response.user_code,
            verification_url=response.verification_url,
            expires_in=response.expires_in,
            interval=response.interval,
            message=f"Please visit {response.verification_url} and enter code: {response.user_code}",
        )
    except AuthorizationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auth/poll", response_model=TokenResponse)
async def poll_for_token(request: PollTokenRequest):
    """Poll for access token after user authorization.

    This endpoint does a single poll attempt and returns immediately.
    The frontend should call this repeatedly until success or error.
    """
    uploader = get_youtube_uploader()

    result = await uploader.poll_for_token_once(device_code=request.device_code)

    if result.get("success"):
        return TokenResponse(
            success=True,
            message="Authorization successful",
        )
    elif result.get("pending"):
        return TokenResponse(
            success=False,
            message="pending",
        )
    else:
        return TokenResponse(
            success=False,
            message=result.get("error", "Unknown error"),
        )


@router.post("/auth/revoke", response_model=TokenResponse)
async def revoke_auth():
    """Revoke YouTube authorization."""
    uploader = get_youtube_uploader()
    uploader.revoke_authorization()
    return TokenResponse(
        success=True,
        message="Authorization revoked",
    )


@router.post("/upload")
async def upload_video(request: UploadRequest):
    """Manually upload a completed recording to YouTube.

    The job must have a successful recording with an output file.
    """
    from pathlib import Path

    from database.models import RecordingJob, get_session_local
    from uploading.youtube import UploadStatus, VideoMetadata

    uploader = get_youtube_uploader()

    if not uploader.is_configured:
        raise HTTPException(
            status_code=400,
            detail="YouTube not configured",
        )

    if not uploader.is_authorized:
        raise HTTPException(
            status_code=401,
            detail="YouTube not authorized. Please complete authorization flow first.",
        )

    # Get job from database
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        job = session.query(RecordingJob).filter(RecordingJob.job_id == request.job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if not job.output_path:
            raise HTTPException(status_code=400, detail="Job has no recording output")

        video_path = Path(job.output_path)
        if not video_path.exists():
            raise HTTPException(status_code=400, detail="Recording file not found")

        # Prepare metadata
        title = request.title or f"Recording - {job.meeting_code}"
        description = request.description or f"Recorded meeting - {job.job_id}"

        metadata = VideoMetadata(
            title=title,
            description=description,
            privacy_status=request.privacy,
        )

        # Upload
        result = await uploader.upload_video(
            video_path=video_path,
            metadata=metadata,
        )

        if result.status == UploadStatus.SUCCEEDED:
            # Update job with video ID
            job.youtube_video_id = result.video_id
            session.commit()

            return {
                "success": True,
                "video_id": result.video_id,
                "video_url": result.video_url,
            }
        else:
            return {
                "success": False,
                "error": result.error_message,
            }

    finally:
        session.close()


@router.get("/video/{video_id}")
async def get_video_info(video_id: str):
    """Get information about an uploaded video."""
    uploader = get_youtube_uploader()

    if not uploader.is_authorized:
        raise HTTPException(
            status_code=401,
            detail="YouTube not authorized",
        )

    try:
        info = await uploader.get_video_info(video_id)
        if not info:
            raise HTTPException(status_code=404, detail="Video not found")
        return info
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

"""YouTube API routes for authorization and upload management."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from recording.remux import recording_file_variants
from uploading.youtube import (
    AuthorizationError,
    get_youtube_uploader,
)

router = APIRouter(prefix="/youtube", tags=["YouTube"])


class ResolveUploadRequest(BaseModel):
    """Explicit operator reconciliation after checking the authorized YouTube channel."""

    video_id: str | None = None
    confirm_no_existing_video: bool = False


class AuthStatusResponse(BaseModel):
    """Authorization status response."""

    configured: bool
    authorized: bool


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
    """Persist one restart-safe upload intent and return immediately."""
    from database.models import JobStatus, RecordingJob
    from database.session import get_session_local
    from providers.base import redact_url_secrets
    from scheduling.job_runner import get_job_runner
    from scheduling.upload_runner import UploadRequest as WorkRequest

    SessionLocal = get_session_local()
    with SessionLocal() as session:
        job = session.query(RecordingJob).filter_by(job_id=request.job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.youtube_video_id:
            return {
                "success": True,
                "video_id": job.youtube_video_id,
                "video_url": f"https://youtu.be/{job.youtube_video_id}",
            }
        if job.status not in (JobStatus.SUCCEEDED.value, JobStatus.UPLOADING.value):
            raise HTTPException(status_code=409, detail="Recording is not ready for upload")
        if not job.output_path or not Path(job.output_path).exists():
            raise HTTPException(status_code=400, detail="Recording file not found")
        uploader = get_youtube_uploader()
        if not uploader.is_configured:
            raise HTTPException(status_code=400, detail="YouTube not configured")
        if not uploader.is_authorized:
            raise HTTPException(
                status_code=401, detail="YouTube not authorized. Please complete authorization flow first."
            )
        path = Path(job.output_path)
        trimmed = Path(job.trimmed_output_path) if job.trimmed_output_path else None
        raw = Path(job.raw_output_path) if job.raw_output_path else None
        work = WorkRequest(
            job_id=job.job_id,
            video_path=path,
            title=request.title or f"Recording - {redact_url_secrets(job.meeting_code)}",
            description=request.description,
            privacy=request.privacy,
            raw_video_path=raw,
            cleanup_video_path_after_success=trimmed if trimmed in recording_file_variants(path) else None,
        )
    try:
        state = get_job_runner().enqueue_upload(work)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if state in ("failed", "needs_review"):
        raise HTTPException(status_code=409, detail="Existing upload needs review; check operations before retrying")
    return {"success": True, "queued": True, "state": state, "job_id": request.job_id}


@router.post("/upload/{job_id}/resolve")
async def resolve_upload(job_id: str, request: ResolveUploadRequest):
    """Attach a verified existing video, or explicitly allow replacing an ambiguous session."""
    from scheduling.job_runner import get_job_runner

    if bool(request.video_id) == request.confirm_no_existing_video:
        raise HTTPException(status_code=400, detail="Provide one video_id or confirm_no_existing_video=true")
    if request.video_id:
        try:
            info = await get_youtube_uploader().get_owned_video_info(request.video_id)
        except Exception:
            raise HTTPException(status_code=502, detail="Unable to verify the YouTube video and channel") from None
        if not info:
            raise HTTPException(status_code=400, detail="Video not found in the authorized YouTube channel")
    if not get_job_runner().resolve_upload(
        job_id, video_id=request.video_id, confirmed_absent=request.confirm_no_existing_video
    ):
        raise HTTPException(status_code=409, detail="Only failed or needs_review uploads can be reconciled")
    return {"success": True, "queued": True, "job_id": job_id}


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

"""YouTube uploader module with OAuth 2.0 and resumable uploads.

This module handles:
- OAuth 2.0 authorization flow (device code flow for headless operation)
- Resumable video uploads using YouTube Data API v3
- Token storage and refresh
- Upload progress tracking and retry logic
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

import httpx

from config.settings import get_settings
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)

# YouTube API endpoints
YOUTUBE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3"

# Required scopes for YouTube upload
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class UploadStatus(str, Enum):
    """Upload status."""

    PENDING = "pending"
    AUTHORIZING = "authorizing"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UploadError(Exception):
    """Base exception for upload errors."""

    pass


class AuthorizationError(UploadError):
    """OAuth authorization error."""

    pass


class QuotaExceededError(UploadError):
    """YouTube API quota exceeded."""

    pass


class UploadFailedError(UploadError):
    """Upload failed after retries."""

    pass


@dataclass
class OAuthToken:
    """OAuth 2.0 token data."""

    access_token: str
    refresh_token: str
    expires_at: datetime
    token_type: str = "Bearer"

    @property
    def is_expired(self) -> bool:
        """Check if token is expired (with 5 minute buffer)."""
        return utc_now() >= (ensure_utc(self.expires_at) - timedelta(minutes=5))

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OAuthToken":
        """Create from dictionary."""
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            token_type=data.get("token_type", "Bearer"),
        )


@dataclass
class DeviceCodeResponse:
    """Device code flow response."""

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass
class UploadResult:
    """Result of a video upload."""

    status: UploadStatus
    video_id: str | None = None
    video_url: str | None = None
    error_message: str | None = None
    uploaded_bytes: int = 0
    total_bytes: int = 0


@dataclass
class VideoMetadata:
    """Video metadata for upload."""

    title: str
    description: str = ""
    privacy_status: str = "unlisted"  # public, private, unlisted
    tags: list[str] = field(default_factory=list)
    category_id: str = "22"  # People & Blogs


class TokenStorage:
    """Token storage using file system."""

    def __init__(self, storage_path: Path | None = None):
        settings = get_settings()
        self.storage_path = storage_path or (settings.data_dir / "youtube_token.json")

    def load(self) -> OAuthToken | None:
        """Load token from storage."""
        if not self.storage_path.exists():
            return None
        try:
            with open(self.storage_path) as f:
                data = json.load(f)
            return OAuthToken.from_dict(data)
        except Exception as e:
            logger.warning(f"Failed to load token: {e}")
            return None

    def save(self, token: OAuthToken) -> None:
        """Save token to storage."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.storage_path, "w") as f:
            json.dump(token.to_dict(), f)
        logger.info("Token saved successfully")

    def delete(self) -> None:
        """Delete stored token."""
        if self.storage_path.exists():
            self.storage_path.unlink()
            logger.info("Token deleted")


class YouTubeUploader:
    """YouTube video uploader with OAuth 2.0 and resumable uploads."""

    # Retry configuration
    MAX_RETRIES = 5
    INITIAL_RETRY_DELAY = 1  # seconds
    MAX_RETRY_DELAY = 64  # seconds
    CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks for resumable upload

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_storage: TokenStorage | None = None,
    ):
        settings = get_settings()
        self.client_id = client_id or settings.youtube_client_id
        self.client_secret = client_secret or settings.youtube_client_secret
        self.token_storage = token_storage or TokenStorage()
        self._token: OAuthToken | None = None
        self._http_client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Check if YouTube credentials are configured."""
        return bool(self.client_id and self.client_secret)

    @property
    def is_authorized(self) -> bool:
        """Check if we have a valid token."""
        token = self._get_token()
        return token is not None

    def _get_token(self) -> OAuthToken | None:
        """Get current token, loading from storage if needed."""
        if self._token is None:
            self._token = self.token_storage.load()
        return self._token

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    # -------------------------------------------------------------------------
    # OAuth 2.0 Device Code Flow (for headless operation)
    # -------------------------------------------------------------------------

    async def start_device_authorization(self) -> DeviceCodeResponse:
        """Start device code authorization flow.

        Returns device code response with user_code and verification_url.
        User must visit the URL and enter the code to authorize.
        """
        if not self.is_configured:
            raise AuthorizationError("YouTube client ID and secret not configured")

        client = await self._get_http_client()
        response = await client.post(
            YOUTUBE_DEVICE_CODE_URL,
            data={
                "client_id": self.client_id,
                "scope": " ".join(YOUTUBE_SCOPES),
            },
        )

        if response.status_code != 200:
            raise AuthorizationError(f"Failed to get device code: {response.text}")

        data = response.json()
        return DeviceCodeResponse(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_url=data["verification_url"],
            expires_in=data["expires_in"],
            interval=data["interval"],
        )

    async def poll_for_token(
        self,
        device_code: str,
        interval: int = 5,
        timeout: int = 300,
    ) -> OAuthToken:
        """Poll for access token after user authorization.

        Args:
            device_code: Device code from start_device_authorization
            interval: Polling interval in seconds
            timeout: Maximum time to wait for authorization

        Returns:
            OAuthToken if successful

        Raises:
            AuthorizationError if authorization fails or times out
        """
        client = await self._get_http_client()
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < timeout:
            response = await client.post(
                YOUTUBE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )

            data = response.json()

            if response.status_code == 200:
                # Success
                token = OAuthToken(
                    access_token=data["access_token"],
                    refresh_token=data["refresh_token"],
                    expires_at=utc_now() + timedelta(seconds=data["expires_in"]),
                )
                self._token = token
                self.token_storage.save(token)
                logger.info("YouTube authorization successful")
                return token

            error = data.get("error")
            if error == "authorization_pending":
                # User hasn't authorized yet, keep polling
                await asyncio.sleep(interval)
            elif error == "slow_down":
                # Need to slow down polling
                interval += 5
                await asyncio.sleep(interval)
            elif error == "access_denied":
                raise AuthorizationError("User denied access")
            elif error == "expired_token":
                raise AuthorizationError("Device code expired")
            else:
                raise AuthorizationError(f"Authorization error: {error}")

        raise AuthorizationError("Authorization timed out")

    async def poll_for_token_once(self, device_code: str) -> dict:
        """Single poll attempt for access token.

        Returns:
            dict with keys:
                - success: True if authorized
                - pending: True if still waiting for user
                - error: Error message if failed
        """
        client = await self._get_http_client()
        response = await client.post(
            YOUTUBE_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )

        data = response.json()

        if response.status_code == 200:
            # Success
            token = OAuthToken(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=utc_now() + timedelta(seconds=data["expires_in"]),
            )
            self._token = token
            self.token_storage.save(token)
            logger.info("YouTube authorization successful")
            return {"success": True}

        error = data.get("error")
        if error == "authorization_pending":
            return {"success": False, "pending": True}
        elif error == "slow_down":
            return {"success": False, "pending": True, "slow_down": True}
        elif error == "access_denied":
            return {"success": False, "error": "User denied access"}
        elif error == "expired_token":
            return {"success": False, "error": "expired"}
        else:
            return {"success": False, "error": f"Authorization error: {error}"}

    async def refresh_access_token(self) -> OAuthToken:
        """Refresh the access token using refresh token."""
        token = self._get_token()
        if not token:
            raise AuthorizationError("No token to refresh")

        client = await self._get_http_client()
        response = await client.post(
            YOUTUBE_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": token.refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if response.status_code != 200:
            self._token = None
            self.token_storage.delete()
            raise AuthorizationError(f"Failed to refresh token: {response.text}")

        data = response.json()
        new_token = OAuthToken(
            access_token=data["access_token"],
            refresh_token=token.refresh_token,  # Keep existing refresh token
            expires_at=utc_now() + timedelta(seconds=data["expires_in"]),
        )
        self._token = new_token
        self.token_storage.save(new_token)
        logger.info("Token refreshed successfully")
        return new_token

    async def ensure_valid_token(self) -> str:
        """Ensure we have a valid access token, refreshing if needed.

        Returns:
            Valid access token

        Raises:
            AuthorizationError if not authorized
        """
        token = self._get_token()
        if not token:
            raise AuthorizationError("Not authorized. Please run authorization flow.")

        if token.is_expired:
            token = await self.refresh_access_token()

        return token.access_token

    def revoke_authorization(self) -> None:
        """Revoke stored authorization."""
        self._token = None
        self.token_storage.delete()
        logger.info("Authorization revoked")

    # -------------------------------------------------------------------------
    # Video Upload
    # -------------------------------------------------------------------------

    async def upload_video(
        self,
        video_path: Path,
        metadata: VideoMetadata,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> UploadResult:
        """Upload a video to YouTube using resumable upload.

        Args:
            video_path: Path to video file
            metadata: Video metadata (title, description, etc.)
            progress_callback: Optional callback(uploaded_bytes, total_bytes)

        Returns:
            UploadResult with video ID on success
        """
        if not video_path.exists():
            return UploadResult(
                status=UploadStatus.FAILED,
                error_message=f"Video file not found: {video_path}",
            )

        file_size = video_path.stat().st_size
        logger.info(f"Starting upload: {video_path.name} ({file_size / 1024 / 1024:.1f} MB)")

        try:
            # Get valid access token
            access_token = await self.ensure_valid_token()

            # Initialize resumable upload
            upload_url = await self._init_resumable_upload(
                access_token=access_token,
                file_size=file_size,
                metadata=metadata,
            )

            # Upload file in chunks with retry logic
            video_id = await self._upload_file_resumable(
                upload_url=upload_url,
                video_path=video_path,
                file_size=file_size,
                progress_callback=progress_callback,
            )

            logger.info(f"Upload successful: video_id={video_id}")
            return UploadResult(
                status=UploadStatus.SUCCEEDED,
                video_id=video_id,
                video_url=f"https://youtu.be/{video_id}",
                uploaded_bytes=file_size,
                total_bytes=file_size,
            )

        except AuthorizationError as e:
            logger.error(f"Authorization error: {e}")
            return UploadResult(
                status=UploadStatus.FAILED,
                error_message=str(e),
            )
        except QuotaExceededError as e:
            logger.error(f"Quota exceeded: {e}")
            return UploadResult(
                status=UploadStatus.FAILED,
                error_message=str(e),
            )
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return UploadResult(
                status=UploadStatus.FAILED,
                error_message=str(e),
            )

    async def _init_resumable_upload(
        self,
        access_token: str,
        file_size: int,
        metadata: VideoMetadata,
    ) -> str:
        """Initialize a resumable upload session.

        Returns:
            Upload URL for resumable upload
        """
        client = await self._get_http_client()

        # Prepare video metadata
        video_resource = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
                "categoryId": metadata.category_id,
            },
            "status": {
                "privacyStatus": metadata.privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        response = await client.post(
            f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=video_resource,
        )

        if response.status_code == 403:
            error_data = response.json().get("error", {})
            if "quotaExceeded" in str(error_data):
                raise QuotaExceededError("YouTube API quota exceeded")
            raise AuthorizationError(f"Access denied: {error_data}")

        if response.status_code != 200:
            raise UploadFailedError(f"Failed to initialize upload: {response.text}")

        upload_url = response.headers.get("Location")
        if not upload_url:
            raise UploadFailedError("No upload URL in response")

        logger.debug(f"Resumable upload initialized: {upload_url}")
        return upload_url

    async def _upload_file_resumable(
        self,
        upload_url: str,
        video_path: Path,
        file_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Upload file using resumable upload protocol.

        Returns:
            Video ID
        """
        uploaded_bytes = 0
        retry_count = 0
        retry_delay = self.INITIAL_RETRY_DELAY

        client = await self._get_http_client()

        with open(video_path, "rb") as f:
            while uploaded_bytes < file_size:
                # Calculate chunk size
                remaining = file_size - uploaded_bytes
                chunk_size = min(self.CHUNK_SIZE, remaining)

                # Read chunk
                f.seek(uploaded_bytes)
                chunk = f.read(chunk_size)

                # Build content range header
                content_range = f"bytes {uploaded_bytes}-{uploaded_bytes + chunk_size - 1}/{file_size}"

                try:
                    response = await client.put(
                        upload_url,
                        headers={
                            "Content-Length": str(chunk_size),
                            "Content-Range": content_range,
                        },
                        content=chunk,
                        timeout=300.0,  # 5 minutes for large chunks
                    )

                    if response.status_code in (200, 201):
                        # Upload complete
                        data = response.json()
                        video_id = data.get("id")
                        if progress_callback:
                            progress_callback(file_size, file_size)
                        return video_id

                    elif response.status_code == 308:
                        # Resume incomplete - more chunks needed
                        range_header = response.headers.get("Range", "")
                        if range_header:
                            # Parse "bytes=0-12345" to get uploaded bytes
                            uploaded_bytes = int(range_header.split("-")[1]) + 1
                        else:
                            uploaded_bytes += chunk_size

                        if progress_callback:
                            progress_callback(uploaded_bytes, file_size)

                        # Reset retry counter on success
                        retry_count = 0
                        retry_delay = self.INITIAL_RETRY_DELAY

                    elif response.status_code in (500, 502, 503, 504):
                        # Server error - retry with backoff
                        retry_count += 1
                        if retry_count > self.MAX_RETRIES:
                            raise UploadFailedError(f"Upload failed after {self.MAX_RETRIES} retries")

                        logger.warning(f"Server error {response.status_code}, retrying in {retry_delay}s")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, self.MAX_RETRY_DELAY)

                        # Query upload status to resume
                        uploaded_bytes = await self._query_upload_status(upload_url, file_size)

                    elif response.status_code == 404:
                        # Upload session expired
                        raise UploadFailedError("Upload session expired")

                    else:
                        raise UploadFailedError(f"Upload failed with status {response.status_code}: {response.text}")

                except httpx.TimeoutException:
                    retry_count += 1
                    if retry_count > self.MAX_RETRIES:
                        raise UploadFailedError("Upload timed out")

                    logger.warning(f"Upload timeout, retrying in {retry_delay}s")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, self.MAX_RETRY_DELAY)

                    # Query upload status to resume
                    uploaded_bytes = await self._query_upload_status(upload_url, file_size)

        raise UploadFailedError("Upload incomplete")

    async def _query_upload_status(self, upload_url: str, file_size: int) -> int:
        """Query resumable upload status to get uploaded bytes.

        Returns:
            Number of bytes uploaded
        """
        client = await self._get_http_client()
        response = await client.put(
            upload_url,
            headers={
                "Content-Length": "0",
                "Content-Range": f"bytes */{file_size}",
            },
        )

        if response.status_code == 308:
            range_header = response.headers.get("Range", "")
            if range_header:
                return int(range_header.split("-")[1]) + 1
        elif response.status_code in (200, 201):
            # Already complete
            return file_size

        return 0

    # -------------------------------------------------------------------------
    # Video Info
    # -------------------------------------------------------------------------

    async def get_video_info(self, video_id: str) -> dict | None:
        """Get information about an uploaded video.

        Args:
            video_id: YouTube video ID

        Returns:
            Video info dict or None if not found
        """
        access_token = await self.ensure_valid_token()
        client = await self._get_http_client()

        response = await client.get(
            f"{YOUTUBE_API_URL}/videos",
            params={
                "part": "snippet,status,processingDetails",
                "id": video_id,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code != 200:
            logger.warning(f"Failed to get video info: {response.text}")
            return None

        data = response.json()
        items = data.get("items", [])
        return items[0] if items else None


# Global uploader instance
_uploader_instance: YouTubeUploader | None = None


def get_youtube_uploader() -> YouTubeUploader:
    """Get the global YouTube uploader instance."""
    global _uploader_instance
    if _uploader_instance is None:
        _uploader_instance = YouTubeUploader()
    return _uploader_instance

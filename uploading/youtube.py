"""YouTube uploader module with OAuth 2.0 and resumable uploads.

This module handles:
- OAuth 2.0 authorization flow (device code flow for headless operation)
- Resumable video uploads using YouTube Data API v3
- Token storage and refresh
- Upload progress tracking and retry logic
"""

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

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

# Device Flow supports youtube/youtube.readonly, but not youtube.upload.
# The youtube scope permits uploads and the channel lookup used by this client.
# https://developers.google.com/identity/protocols/oauth2/limited-input-device#allowedscopes
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
]


def _read_file_chunk(video_path: Path, offset: int, chunk_size: int) -> bytes:
    with open(video_path, "rb") as file:
        file.seek(offset)
        return file.read(chunk_size)


class UploadStatus(StrEnum):
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


class UploadNeedsReviewError(UploadError):
    """Completion cannot safely be determined; never initialize a replacement automatically."""


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
    retryable: bool = True
    needs_review: bool = False


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
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.storage_path.parent, prefix=".youtube-token-", delete=False
            ) as stream:
                temporary_path = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                json.dump(token.to_dict(), stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self.storage_path)
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
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

    async def _read_chunk(self, video_path: Path, offset: int, chunk_size: int) -> bytes:
        """Read a video chunk without blocking the event loop."""
        return await asyncio.to_thread(_read_file_chunk, video_path, offset, chunk_size)

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
            if response.status_code == 429 or response.status_code >= 500:
                raise UploadFailedError("Temporary token refresh failure")
            try:
                revoked = response.json().get("error") == "invalid_grant"
            except ValueError:
                revoked = False
            if revoked:
                self._token = None
                self.token_storage.delete()
            raise AuthorizationError("Token refresh rejected")

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
        *,
        resume_state: dict | None = None,
        state_callback: Callable[[dict], None] | None = None,
    ) -> UploadResult:
        """Upload or resume exactly one intent; persist the session before sending bytes."""
        state = dict(resume_state or {})
        if state.get("video_id"):
            video_id = state["video_id"]
            file_size = state.get("file_size", 0)
            return UploadResult(
                status=UploadStatus.SUCCEEDED,
                video_id=video_id,
                video_url=f"https://youtu.be/{video_id}",
                uploaded_bytes=file_size,
                total_bytes=file_size,
            )
        if not video_path.exists():
            return UploadResult(status=UploadStatus.FAILED, error_message="Upload source unavailable", retryable=False)
        file_size = video_path.stat().st_size

        def fingerprint() -> str:
            with video_path.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()

        def persist() -> None:
            if state_callback:
                state_callback(dict(state))

        try:
            digest = await asyncio.to_thread(fingerprint)
            if state.get("session_uri") and (state.get("sha256") != digest or state.get("file_size") != file_size):
                raise UploadNeedsReviewError("Upload source changed; existing session requires review")
            access_token = await self.ensure_valid_token()
            upload_url = state.get("session_uri")
            uploaded_bytes, video_id = 0, None
            if upload_url:
                uploaded_bytes, video_id = await self._query_upload_status(upload_url, file_size)
            else:
                upload_url = await self._init_resumable_upload(access_token, file_size, metadata)
                state.update(session_uri=upload_url, sha256=digest, file_size=file_size, uploaded_bytes=0)
                persist()

            def checkpoint(offset: int, total: int) -> None:
                state["uploaded_bytes"] = offset
                persist()
                if progress_callback:
                    progress_callback(offset, total)

            if not video_id:
                video_id = await self._upload_file_resumable(
                    upload_url,
                    video_path,
                    file_size,
                    checkpoint,
                    initial_offset=uploaded_bytes,
                )
            state.update(video_id=video_id, uploaded_bytes=file_size)
            persist()
            logger.info("Upload successful: video_id=%s", video_id)
            return UploadResult(
                status=UploadStatus.SUCCEEDED,
                video_id=video_id,
                video_url=f"https://youtu.be/{video_id}",
                uploaded_bytes=file_size,
                total_bytes=file_size,
            )
        except UploadNeedsReviewError as exc:
            return UploadResult(status=UploadStatus.FAILED, error_message=str(exc), retryable=False, needs_review=True)
        except (AuthorizationError, QuotaExceededError) as exc:
            # Provider responses may contain URLs/tokens; expose only a stable safe category.
            return UploadResult(status=UploadStatus.FAILED, error_message=type(exc).__name__, retryable=False)
        except Exception as exc:
            logger.warning("YouTube upload attempt failed (%s); resumable state retained", type(exc).__name__)
            return UploadResult(status=UploadStatus.FAILED, error_message=type(exc).__name__)

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

        self._validate_upload_uri(upload_url)
        logger.debug("Resumable upload initialized")
        return upload_url

    async def _upload_file_resumable(
        self,
        upload_url: str,
        video_path: Path,
        file_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
        *,
        initial_offset: int = 0,
    ) -> str:
        """Send bounded chunks; after uncertain responses ask the server, never guess offsets."""
        self._validate_upload_uri(upload_url)
        uploaded_bytes = initial_offset
        retry_count = 0
        client = await self._get_http_client()
        while uploaded_bytes < file_size:
            chunk = await self._read_chunk(video_path, uploaded_bytes, min(self.CHUNK_SIZE, file_size - uploaded_bytes))
            if not chunk:
                raise UploadNeedsReviewError("Upload source truncated")
            try:
                response = await client.put(
                    upload_url,
                    headers={
                        "Authorization": f"Bearer {await self.ensure_valid_token()}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {uploaded_bytes}-{uploaded_bytes + len(chunk) - 1}/{file_size}",
                    },
                    content=chunk,
                    timeout=300.0,
                )
                if response.status_code in (200, 201):
                    video_id = response.json().get("id")
                    if not video_id:
                        raise UploadNeedsReviewError("YouTube completed without a video ID; requires review")
                    if progress_callback:
                        progress_callback(file_size, file_size)
                    return video_id
                if response.status_code == 308:
                    offset = self._response_offset(response, file_size)
                    if offset <= uploaded_bytes:
                        raise httpx.TransportError("Upload made no progress")
                    uploaded_bytes = offset
                    if progress_callback:
                        progress_callback(uploaded_bytes, file_size)
                    retry_count = 0
                    continue
                if response.status_code in (404, 410):
                    raise UploadNeedsReviewError("Upload session expired; check YouTube before retrying")
                if response.status_code in (401, 403):
                    raise AuthorizationError("Upload authorization rejected")
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise UploadNeedsReviewError("Upload rejected; existing session requires review")
            except httpx.TransportError:
                pass
            retry_count += 1
            if retry_count > self.MAX_RETRIES:
                raise UploadFailedError("Upload retries exhausted")
            await asyncio.sleep(min(self.INITIAL_RETRY_DELAY * 2 ** (retry_count - 1), self.MAX_RETRY_DELAY))
            uploaded_bytes, video_id = await self._query_upload_status(upload_url, file_size)
            if video_id:
                if progress_callback:
                    progress_callback(file_size, file_size)
                return video_id
            if progress_callback:
                progress_callback(uploaded_bytes, file_size)
        # A final 308 is not proof of completion, even if it acknowledges all bytes.
        _, video_id = await self._query_upload_status(upload_url, file_size)
        if video_id:
            return video_id
        raise UploadFailedError("Upload completion pending")

    @staticmethod
    def _validate_upload_uri(upload_url: str) -> None:
        parsed = urlsplit(upload_url)
        if parsed.scheme != "https" or parsed.hostname not in ("www.googleapis.com", "youtube.googleapis.com"):
            raise UploadNeedsReviewError("Invalid resumable endpoint; requires review")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise UploadNeedsReviewError("Invalid resumable endpoint; requires review")

    @staticmethod
    def _response_offset(response: httpx.Response, file_size: int) -> int:
        value = response.headers.get("Range", "")
        if not value:
            return 0
        try:
            if not value.startswith("bytes=0-"):
                raise ValueError
            offset = int(value.removeprefix("bytes=0-")) + 1
            if offset < 0 or offset > file_size:
                raise ValueError
            return offset
        except ValueError as exc:
            raise UploadNeedsReviewError("Invalid upload acknowledgement; requires review") from exc

    async def _query_upload_status(self, upload_url: str, file_size: int) -> tuple[int, str | None]:
        """Recover both offset and completed video ID after process/network interruption."""
        self._validate_upload_uri(upload_url)
        client = await self._get_http_client()
        response = await client.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {await self.ensure_valid_token()}",
                "Content-Length": "0",
                "Content-Range": f"bytes */{file_size}",
            },
        )
        if response.status_code == 308:
            return self._response_offset(response, file_size), None
        if response.status_code in (200, 201):
            video_id = response.json().get("id")
            if not video_id:
                raise UploadNeedsReviewError("YouTube completed without a video ID; requires review")
            return file_size, video_id
        if response.status_code in (404, 410):
            raise UploadNeedsReviewError("Upload session expired; check YouTube before retrying")
        if response.status_code in (401, 403):
            raise AuthorizationError("Upload authorization rejected")
        raise UploadFailedError("Unable to verify upload session; retry existing session later")

    # -------------------------------------------------------------------------
    # Video Info
    # -------------------------------------------------------------------------

    async def get_owned_video_info(self, video_id: str) -> dict | None:
        """Verify an operator-supplied recovery ID belongs to the authorized channel."""
        info = await self.get_video_info(video_id)
        if not info:
            return None
        token = await self.ensure_valid_token()
        client = await self._get_http_client()
        response = await client.get(
            f"{YOUTUBE_API_URL}/channels",
            params={"part": "id", "mine": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            raise UploadFailedError("Unable to verify authorized channel")
        owned_ids = {item.get("id") for item in response.json().get("items", [])}
        return info if info.get("snippet", {}).get("channelId") in owned_ids else None

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


async def close_youtube_uploader() -> None:
    """Close the global uploader's HTTP client if it has been created."""
    if _uploader_instance is not None:
        await _uploader_instance.close()

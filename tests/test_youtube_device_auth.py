"""Verify the OAuth request against Google's Device Flow scope contract."""

from urllib.parse import parse_qs

import httpx
import pytest

from uploading.youtube import TokenStorage, YouTubeUploader


@pytest.mark.asyncio
async def test_device_authorization_requests_a_supported_upload_scope(tmp_path):
    def authorize(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://oauth2.googleapis.com/device/code"
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["test-client"]
        scopes = set(form["scope"][0].split())
        # Google Device Flow only supports these YouTube scopes. The broader
        # youtube scope is required for upload; readonly alone cannot upload.
        assert scopes <= {
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/youtube.readonly",
        }
        assert "https://www.googleapis.com/auth/youtube" in scopes
        assert "client_secret" not in form
        return httpx.Response(
            200,
            json={
                "device_code": "test-device-code",
                "user_code": "TEST-CODE",
                "verification_url": "https://www.google.com/device",
                "expires_in": 1800,
                "interval": 5,
            },
        )

    uploader = YouTubeUploader(
        client_id="test-client",
        client_secret="test-secret",
        token_storage=TokenStorage(tmp_path / "youtube_token.json"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(authorize)) as client:
        uploader._http_client = client
        response = await uploader.start_device_authorization()

    assert response.user_code == "TEST-CODE"
    assert response.interval == 5
    assert not (tmp_path / "youtube_token.json").exists()

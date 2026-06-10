"""Tests for officially supported Zoom provider entry points."""

from pydantic import ValidationError

from api.routes.jobs import RecordRequest
from api.routes.meetings import MeetingCreate, MeetingUpdate


def test_direct_record_request_accepts_zoom():
    """Direct recording API schema should accept Zoom."""
    request = RecordRequest(
        provider="zoom",
        meeting_code="https://zoom.us/j/123456789?pwd=abc",
        duration_sec=60,
    )

    assert request.provider == "zoom"


def test_meeting_create_accepts_zoom():
    """Meeting create schema should accept Zoom."""
    request = MeetingCreate(
        name="Zoom Standup",
        provider="zoom",
        meeting_code="https://zoom.us/j/123456789?pwd=abc",
    )

    assert request.provider == "zoom"


def test_meeting_update_accepts_zoom_provider():
    """Meeting update schema should allow switching a meeting to Zoom."""
    request = MeetingUpdate(provider="zoom")

    assert request.provider == "zoom"


def test_direct_record_request_rejects_unknown_provider():
    """Provider validation should still reject unsupported values."""
    try:
        RecordRequest(provider="teams", meeting_code="abc", duration_sec=60)
    except ValidationError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("RecordRequest accepted an unsupported provider")

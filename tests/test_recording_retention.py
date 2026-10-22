"""Original artifact retention applies equally to maintenance and upload preparation."""

import json
from types import SimpleNamespace

import pytest

import services.storage_maintenance as storage_module
from services.recording_retention import preserve_raw_recording


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (None, False),
        ([], False),
        ("broken", False),
        ({}, False),
        ({"local_recording_retention": []}, False),
        ({"local_recording_retention": {"preserve": "true"}}, False),
        ({"local_recording_retention": {"preserve": 1}}, False),
        ({"local_recording_retention": {"preserve": True}}, True),
        (json.dumps({"local_recording_retention": {"preserve": True}}), True),
    ],
)
def test_preservation_requires_explicit_true(summary, expected):
    assert preserve_raw_recording(summary) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("transcode", [False, True])
async def test_upload_preparation_keeps_preserved_mkv_and_distinct_transcode(tmp_path, monkeypatch, transcode):
    original = tmp_path / "recording.mkv"
    original.write_bytes(b"preserved exact raw container")
    canonical = original.with_suffix(".mp4")
    temporary = tmp_path / "recording.upload.mp4"

    async def remux(input_path, **kwargs):
        assert input_path == original
        canonical.write_bytes(b"canonical mp4")
        return canonical

    async def compress(input_path, **kwargs):
        assert input_path == canonical
        temporary.write_bytes(b"compressed upload mp4")
        return temporary

    monkeypatch.setattr(storage_module, "get_settings", lambda: SimpleNamespace(ffmpeg_transcode_on_upload=transcode))
    monkeypatch.setattr(storage_module, "ensure_canonical_mp4", remux)
    monkeypatch.setattr(storage_module, "ensure_upload_mp4", compress)
    result = await storage_module.prepare_upload_recording_file(original, remove_source=False)
    assert original.read_bytes() == b"preserved exact raw container"
    assert canonical.read_bytes() == b"canonical mp4"
    assert result.deleted_source is None
    assert result.freed_bytes == 0
    assert result.output_path == canonical
    assert result.temporary_upload_path == (temporary if transcode else None)

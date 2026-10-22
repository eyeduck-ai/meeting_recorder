"""Shared interpretation of the operator's original-recording retention marker."""

import json


def preserve_raw_recording(summary: dict | str | None) -> bool:
    """Only an explicit boolean true preserves the original recording artifact."""
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except (TypeError, ValueError):
            return False
    retention = summary.get("local_recording_retention") if isinstance(summary, dict) else None
    return isinstance(retention, dict) and retention.get("preserve") is True

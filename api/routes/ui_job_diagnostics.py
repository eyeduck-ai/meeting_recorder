"""Helpers for rendering per-job diagnostic logs in the Web UI."""

import json
from dataclasses import dataclass
from pathlib import Path

from database.models import RecordingJob

JOB_LOG_ORDER = (
    "metadata.json",
    "console.log",
    "ffmpeg.log",
    "remux.log",
    "transcode.log",
)
JOB_LOG_LABELS = {
    "metadata.json": "Failure metadata",
    "console.log": "Browser console log",
    "ffmpeg.log": "FFmpeg log",
    "remux.log": "Remux log",
    "transcode.log": "Transcode log",
}
JOB_LOG_EXCERPT_BYTES = 64 * 1024


@dataclass(frozen=True)
class JobLogView:
    """Rendered log block for the job details page."""

    name: str
    label: str
    content_excerpt: str
    truncated: bool
    full_log_url: str


@dataclass(frozen=True)
class FailureContextView:
    """Summary details extracted from metadata.json."""

    error_code: str | None = None
    error_message: str | None = None
    stage: str | None = None
    url: str | None = None
    title: str | None = None


def _safe_resolve(path: Path) -> Path | None:
    """Resolve a path safely without leaking errors into the request path."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _get_job_diagnostic_dir(job: RecordingJob) -> Path | None:
    """Return the resolved diagnostics directory for a job if it exists."""
    if not job.diagnostic_dir:
        return None

    diagnostic_dir = _safe_resolve(Path(job.diagnostic_dir))
    if not diagnostic_dir or not diagnostic_dir.exists() or not diagnostic_dir.is_dir():
        return None
    return diagnostic_dir


def _resolve_job_log_path(job: RecordingJob, log_name: str) -> Path | None:
    """Resolve a whitelisted per-job log file path."""
    if log_name not in JOB_LOG_ORDER:
        return None

    diagnostic_dir = _get_job_diagnostic_dir(job)
    if not diagnostic_dir:
        return None

    candidate = _safe_resolve(diagnostic_dir / log_name)
    if not candidate or not candidate.is_file():
        return None

    try:
        candidate.relative_to(diagnostic_dir)
    except ValueError:
        return None

    return candidate


def _read_text_excerpt(path: Path, max_bytes: int = JOB_LOG_EXCERPT_BYTES) -> tuple[str, bool]:
    """Read the tail of a text file for inline display."""
    file_size = path.stat().st_size
    truncated = file_size > max_bytes

    with path.open("rb") as fh:
        if truncated:
            fh.seek(file_size - max_bytes)
        content = fh.read()

    return content.decode("utf-8", errors="replace"), truncated


def _load_failure_context(job: RecordingJob) -> FailureContextView | None:
    """Extract structured failure details from metadata.json when available."""
    metadata_path = _resolve_job_log_path(job, "metadata.json")
    if not metadata_path:
        return None

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    return FailureContextView(
        error_code=metadata.get("error_code"),
        error_message=metadata.get("error_message"),
        stage=metadata.get("stage"),
        url=metadata.get("url"),
        title=metadata.get("title"),
    )


def _load_job_logs(job: RecordingJob) -> list[JobLogView]:
    """Discover and load the known per-job diagnostics logs."""
    logs: list[JobLogView] = []

    for log_name in JOB_LOG_ORDER:
        log_path = _resolve_job_log_path(job, log_name)
        if not log_path:
            continue

        content_excerpt, truncated = _read_text_excerpt(log_path)
        logs.append(
            JobLogView(
                name=log_name,
                label=JOB_LOG_LABELS.get(log_name, log_name),
                content_excerpt=content_excerpt,
                truncated=truncated,
                full_log_url=f"/jobs/{job.job_id}/logs/{log_name}",
            )
        )

    return logs

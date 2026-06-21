from pathlib import Path

from database.models import RecordingJob
from recording.remux import pick_preferred_video_path


def mark_trimmed_artifact_state(job: RecordingJob) -> None:
    """Attach a display flag for trimmed files deleted after upload."""
    trimmed_output_path = getattr(job, "trimmed_output_path", None)
    job.trimmed_artifact_removed = bool(trimmed_output_path and not Path(trimmed_output_path).exists())


def preferred_existing_output(job: RecordingJob) -> Path | None:
    """Return the best local playback/download path for a job."""
    if getattr(job, "local_recording_deleted_at", None):
        return None

    for output_path in (
        getattr(job, "output_path", None),
        getattr(job, "trimmed_output_path", None),
        getattr(job, "raw_output_path", None),
    ):
        if not output_path:
            continue
        file_path = Path(output_path)
        if file_path.exists():
            preferred = pick_preferred_video_path(file_path)
            return preferred if preferred.exists() else file_path
        preferred = pick_preferred_video_path(file_path)
        if preferred.exists():
            return preferred
    return None


def mark_recording_artifact_state(job: RecordingJob) -> None:
    """Attach recordings-list display flags derived from local artifacts."""
    mark_trimmed_artifact_state(job)
    job.local_download_available = preferred_existing_output(job) is not None

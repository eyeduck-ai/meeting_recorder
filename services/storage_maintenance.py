"""Storage retention and recording canonicalization helpers."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from database.models import DetectionLog, JobStatus, RecordingJob
from recording.mp4_validation import validate_mp4_file
from recording.remux import derive_mp4_path, ensure_canonical_mp4, ensure_upload_mp4, recording_file_variants
from utils.timezone import ensure_utc, utc_now

logger = logging.getLogger(__name__)

RECORDING_RETENTION_DAYS = 14
DIAGNOSTICS_RETENTION_DAYS = 14
LOG_RETENTION_DAYS = 14
DETECTION_LOG_RETENTION_DAYS = 14
MAINTENANCE_JOB_ID = "storage_maintenance_daily"
MAINTENANCE_HOUR = 3
MAINTENANCE_MINUTE = 30


@dataclass(frozen=True)
class CanonicalRecording:
    """Result of converting a local recording to the canonical MP4 file."""

    output_path: Path
    file_size: int
    deleted_source: Path | None = None
    freed_bytes: int = 0
    status: str = "canonicalized"
    upload_path: Path | None = None
    temporary_upload_path: Path | None = None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def canonicalize_recording_file(
    input_path: Path,
    *,
    remux_log_path: Path | None = None,
    transcode_log_path: Path | None = None,
    progress_callback=None,
    dry_run: bool = False,
) -> CanonicalRecording | None:
    """Convert a local recording to canonical MP4 using fast remux only."""
    return await _prepare_mp4_recording_file(
        input_path,
        remux_log_path=remux_log_path,
        dry_run=dry_run,
    )


async def prepare_upload_recording_file(
    input_path: Path,
    *,
    remux_log_path: Path | None = None,
    transcode_log_path: Path | None = None,
    progress_callback=None,
) -> CanonicalRecording | None:
    """Prepare an MP4 for YouTube upload while respecting upload transcode settings."""
    local = await _prepare_mp4_recording_file(
        input_path,
        remux_log_path=remux_log_path,
        dry_run=False,
    )
    if not local:
        return None

    settings = get_settings()
    if not settings.ffmpeg_transcode_on_upload:
        return local

    upload_path = await ensure_upload_mp4(
        local.output_path,
        remux_log_path=remux_log_path,
        transcode_log_path=transcode_log_path,
        progress_callback=progress_callback,
    )
    if not upload_path or not upload_path.exists():
        logger.warning("Upload transcode failed for %s; falling back to canonical MP4", local.output_path)
        return local

    return CanonicalRecording(
        output_path=local.output_path,
        file_size=local.file_size,
        deleted_source=local.deleted_source,
        freed_bytes=local.freed_bytes,
        status="upload_transcoded",
        upload_path=upload_path,
        temporary_upload_path=upload_path if upload_path.resolve() != local.output_path.resolve() else None,
    )


async def _prepare_mp4_recording_file(
    input_path: Path,
    *,
    remux_log_path: Path | None = None,
    dry_run: bool = False,
) -> CanonicalRecording | None:
    """Prepare an MP4 and remove the source only after the MP4 is validated."""
    input_path = Path(input_path)
    if input_path.suffix.lower() == ".mp4":
        if not input_path.exists():
            return None
        if not dry_run and not await validate_mp4_file(input_path):
            return None
        return CanonicalRecording(output_path=input_path, file_size=input_path.stat().st_size)

    if input_path.suffix.lower() != ".mkv":
        return None

    mp4_path = derive_mp4_path(input_path)
    if dry_run:
        size = _file_size(mp4_path) or _file_size(input_path)
        return CanonicalRecording(
            output_path=mp4_path,
            file_size=size,
            deleted_source=input_path if input_path.exists() else None,
            freed_bytes=_file_size(input_path),
            status="would_attempt",
        )

    if not input_path.exists() and mp4_path.exists() and await validate_mp4_file(mp4_path):
        return CanonicalRecording(output_path=mp4_path, file_size=mp4_path.stat().st_size)

    if not input_path.exists():
        return None

    source_size = input_path.stat().st_size
    output_path = await ensure_canonical_mp4(input_path, remux_log_path=remux_log_path)
    if not output_path or not output_path.exists():
        return None

    deleted_source = None
    freed_bytes = 0
    if output_path.resolve() != input_path.resolve() and input_path.exists():
        input_path.unlink()
        deleted_source = input_path
        freed_bytes = source_size

    return CanonicalRecording(
        output_path=output_path,
        file_size=output_path.stat().st_size,
        deleted_source=deleted_source,
        freed_bytes=freed_bytes,
    )


class StorageMaintenanceService:
    """Clean up storage across recordings, diagnostics, logs, and DB logs."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.recordings_dir = Path(self.settings.recordings_dir)
        self.diagnostics_dir = Path(self.settings.diagnostics_dir)
        self.logs_dir = Path(self.settings.logs_dir)

    async def run(self, db: Session, *, dry_run: bool = True) -> dict:
        """Run storage maintenance and return a categorized summary."""
        now = utc_now()
        result = self._empty_result(dry_run=dry_run)
        mutated = False
        vacuum_needed = False

        uploaded_mutated = self._cleanup_uploaded_recordings(db, result, now=now, dry_run=dry_run)
        mutated |= uploaded_mutated
        if uploaded_mutated and not dry_run:
            db.flush()
        mutated |= await self._canonicalize_legacy_recordings(db, result, dry_run=dry_run)
        mutated |= self._cleanup_diagnostics(db, result, now=now, dry_run=dry_run)
        self._cleanup_rotated_logs(result, now=now, dry_run=dry_run)
        deleted_detection_logs = self._cleanup_detection_logs(db, result, now=now, dry_run=dry_run)
        mutated |= deleted_detection_logs > 0
        vacuum_needed = deleted_detection_logs > 0

        if not dry_run and mutated:
            db.commit()
            if vacuum_needed:
                self._vacuum_sqlite(db, result)

        return result

    def _empty_result(self, *, dry_run: bool) -> dict:
        return {
            "dry_run": dry_run,
            "retention_days": {
                "recordings": RECORDING_RETENTION_DAYS,
                "diagnostics": DIAGNOSTICS_RETENTION_DAYS,
                "logs": LOG_RETENTION_DAYS,
                "detection_logs": DETECTION_LOG_RETENTION_DAYS,
            },
            "canonicalized": [],
            "deleted_recordings": [],
            "deleted_diagnostics": [],
            "deleted_logs": [],
            "deleted_detection_logs": 0,
            "freed_bytes": 0,
            "vacuumed": False,
            "warnings": [],
            "errors": [],
        }

    def _cutoff(self, now: datetime, days: int) -> datetime:
        return now - timedelta(days=days)

    def _recording_age_at(self, job: RecordingJob, output_path: Path) -> datetime | None:
        return ensure_utc(job.completed_at) or _file_mtime(output_path) or ensure_utc(job.created_at)

    def _job_age_at(self, job: RecordingJob, path: Path) -> datetime | None:
        return ensure_utc(job.completed_at) or ensure_utc(job.created_at) or _file_mtime(path)

    def _thumbnail_candidates(self, path: Path) -> list[Path]:
        thumbnails_dir = self.recordings_dir / "thumbnails"
        return [thumbnails_dir / f"{path.stem}.jpg"]

    def _delete_paths(self, paths: list[Path], *, dry_run: bool) -> tuple[list[dict], int]:
        deleted = []
        freed_bytes = 0
        for path in paths:
            if not path.exists() or not path.is_file():
                continue
            size = _file_size(path)
            if not dry_run:
                path.unlink()
            deleted.append({"path": str(path), "size": size})
            freed_bytes += size
        return deleted, freed_bytes

    def _cleanup_uploaded_recordings(
        self,
        db: Session,
        result: dict,
        *,
        now: datetime,
        dry_run: bool,
    ) -> bool:
        cutoff = self._cutoff(now, RECORDING_RETENTION_DAYS)
        jobs = (
            db.query(RecordingJob)
            .filter(
                RecordingJob.status == JobStatus.SUCCEEDED.value,
                RecordingJob.youtube_video_id != None,
                RecordingJob.output_path != None,
                RecordingJob.local_recording_deleted_at == None,
            )
            .all()
        )
        mutated = False
        for job in jobs:
            output_path = Path(job.output_path)
            if not _is_relative_to(output_path, self.recordings_dir):
                self._add_error(result, "recording", str(output_path), "path is outside recordings_dir")
                continue

            age_at = self._recording_age_at(job, output_path)
            if not age_at or age_at > cutoff:
                continue

            candidate_paths = [*recording_file_variants(output_path), *self._thumbnail_candidates(output_path)]
            deleted_files, freed_bytes = self._delete_paths(candidate_paths, dry_run=dry_run)
            if not deleted_files:
                continue

            entry = {
                "job_id": job.job_id,
                "reason": f"uploaded local recording older than {RECORDING_RETENTION_DAYS} days",
                "files": deleted_files,
            }
            result["deleted_recordings"].append(entry)
            result["freed_bytes"] += freed_bytes

            if not dry_run:
                job.local_recording_deleted_at = now
                job.local_recording_cleanup_reason = entry["reason"]
                mutated = True
                self._remove_empty_parent(output_path)

        return mutated

    async def _canonicalize_legacy_recordings(self, db: Session, result: dict, *, dry_run: bool) -> bool:
        jobs = (
            db.query(RecordingJob)
            .filter(
                RecordingJob.status == JobStatus.SUCCEEDED.value,
                RecordingJob.output_path != None,
                RecordingJob.local_recording_deleted_at == None,
            )
            .all()
        )
        mutated = False
        for job in jobs:
            output_path = Path(job.output_path)
            if output_path.suffix.lower() != ".mkv":
                continue
            if not _is_relative_to(output_path, self.recordings_dir):
                self._add_error(result, "canonicalize", str(output_path), "path is outside recordings_dir")
                continue
            if not output_path.exists():
                self._add_error(result, "canonicalize", str(output_path), "source MKV is missing")
                continue

            remux_log = self.diagnostics_dir / job.job_id / "remux.log"
            transcode_log = self.diagnostics_dir / job.job_id / "transcode.log"
            try:
                canonical = await canonicalize_recording_file(
                    output_path,
                    remux_log_path=remux_log,
                    transcode_log_path=transcode_log,
                    dry_run=dry_run,
                )
            except Exception as exc:
                self._add_error(result, "canonicalize", str(output_path), str(exc))
                continue

            if not canonical:
                self._add_error(result, "canonicalize", str(output_path), "MP4 conversion did not produce a file")
                continue

            result["canonicalized"].append(
                {
                    "job_id": job.job_id,
                    "from": str(output_path),
                    "to": str(canonical.output_path),
                    "deleted_source": str(canonical.deleted_source) if canonical.deleted_source else None,
                    "status": canonical.status,
                }
            )
            result["freed_bytes"] += canonical.freed_bytes

            if not dry_run:
                job.output_path = str(canonical.output_path)
                job.file_size = canonical.file_size
                mutated = True

        return mutated

    def _cleanup_diagnostics(self, db: Session, result: dict, *, now: datetime, dry_run: bool) -> bool:
        cutoff = self._cutoff(now, DIAGNOSTICS_RETENTION_DAYS)
        terminal_statuses = [
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELED.value,
        ]
        jobs = (
            db.query(RecordingJob)
            .filter(
                RecordingJob.status.in_(terminal_statuses),
                RecordingJob.diagnostic_dir != None,
            )
            .all()
        )
        mutated = False
        for job in jobs:
            diagnostic_dir = Path(job.diagnostic_dir)
            if not _is_relative_to(diagnostic_dir, self.diagnostics_dir):
                self._add_error(result, "diagnostics", str(diagnostic_dir), "path is outside diagnostics_dir")
                continue

            age_at = self._job_age_at(job, diagnostic_dir)
            if not age_at or age_at > cutoff:
                continue

            freed_bytes = self._directory_size(diagnostic_dir)
            if diagnostic_dir.exists() and not dry_run:
                shutil.rmtree(diagnostic_dir)

            result["deleted_diagnostics"].append(
                {
                    "job_id": job.job_id,
                    "path": str(diagnostic_dir),
                    "size": freed_bytes,
                    "reason": f"diagnostics older than {DIAGNOSTICS_RETENTION_DAYS} days",
                }
            )
            result["freed_bytes"] += freed_bytes

            if not dry_run:
                job.diagnostic_dir = None
                job.has_screenshot = False
                job.has_html_dump = False
                job.has_console_log = False
                mutated = True

        return mutated

    def _cleanup_rotated_logs(self, result: dict, *, now: datetime, dry_run: bool) -> None:
        cutoff = self._cutoff(now, LOG_RETENTION_DAYS)
        if not self.logs_dir.exists():
            return

        for path in self.logs_dir.iterdir():
            if not path.is_file() or path.name in {"app.log", ".gitkeep"}:
                continue
            mtime = _file_mtime(path)
            if not mtime or mtime > cutoff:
                continue

            size = _file_size(path)
            if not dry_run:
                path.unlink()
            result["deleted_logs"].append(
                {
                    "path": str(path),
                    "size": size,
                    "reason": f"log older than {LOG_RETENTION_DAYS} days",
                }
            )
            result["freed_bytes"] += size

    def _cleanup_detection_logs(self, db: Session, result: dict, *, now: datetime, dry_run: bool) -> int:
        cutoff = self._cutoff(now, DETECTION_LOG_RETENTION_DAYS).replace(tzinfo=None)
        query = db.query(DetectionLog).filter(DetectionLog.triggered_at < cutoff)
        deleted = query.count()
        result["deleted_detection_logs"] = deleted
        if not dry_run and deleted:
            query.delete(synchronize_session=False)
        return deleted

    def _vacuum_sqlite(self, db: Session, result: dict) -> None:
        bind = db.get_bind()
        if bind.dialect.name != "sqlite":
            return
        try:
            with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("VACUUM")
            result["vacuumed"] = True
        except Exception as exc:
            self._add_error(result, "database", "VACUUM", str(exc))

    def _directory_size(self, path: Path) -> int:
        if not path.exists():
            return 0
        total = 0
        for child in path.rglob("*"):
            if child.is_file():
                total += _file_size(child)
        return total

    def _remove_empty_parent(self, path: Path) -> None:
        parent = path.parent
        if parent == self.recordings_dir or not _is_relative_to(parent, self.recordings_dir):
            return
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    def _add_error(self, result: dict, scope: str, path: str, error: str) -> None:
        result["errors"].append({"scope": scope, "path": path, "error": error})


def get_storage_maintenance_service(settings: Settings | None = None) -> StorageMaintenanceService:
    return StorageMaintenanceService(settings=settings)

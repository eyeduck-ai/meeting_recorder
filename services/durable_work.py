"""Durable completed-file work intents; no capture slot or secret configuration snapshots."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from database.models import DurableWork
from database.models import RecordingJob as RecordingJobModel
from database.session import get_session_local
from utils.timezone import ensure_utc, utc_now

PENDING_STATES = ("pending", "retry_wait")
MAX_WORK_ATTEMPTS = 5


def _json_default(value):
    if isinstance(value, (Path, datetime)):
        return str(value)
    raise TypeError(type(value).__name__)


def upload_payload(request) -> dict:
    return json.loads(json.dumps(asdict(request), default=_json_default))


def post_processing_payload(request) -> dict:
    """Only processing inputs: never snapshot meeting URL/password or browser credentials."""
    job, result = request.job, request.result
    info = result.recording_info
    return {
        "job": {
            "job_id": job.job_id,
            "output_dir": str(job.output_dir),
            "diagnostics_dir": str(job.diagnostics_dir) if job.diagnostics_dir else None,
            "smart_trim_enabled": job.smart_trim_enabled,
            "activity_config": asdict(job.activity_config),
            "attempt_no": job.attempt_no,
        },
        "result": {
            "recording_info": json.loads(json.dumps(asdict(info), default=_json_default)) if info else None,
            **{
                key: str(value) if isinstance(value, (Path, datetime)) else value
                for key in (
                    "output_path",
                    "raw_output_path",
                    "trimmed_output_path",
                    "trim_start_sec",
                    "trim_end_sec",
                    "trim_status",
                    "trim_reason",
                    "dynamic_extension_stop_reason",
                    "start_time",
                    "joined_at",
                    "recording_started_at",
                    "recording_stopped_at",
                    "end_time",
                    "end_reason",
                    "ffmpeg_exit_code",
                )
                if (value := getattr(result, key)) is not None
            },
        },
        "youtube_enabled": request.youtube_enabled,
        "youtube_privacy": request.youtube_privacy,
        "meeting_name": request.meeting_name,
    }


class DurableWorkStore:
    """Short transactions for a single app process and restart-safe worker claims."""

    def __init__(self, session_factory=None):
        self._session_factory = session_factory or get_session_local

    def _session(self):
        return self._session_factory()()

    def post_processing_complete(self, job_id: str) -> bool:
        with self._session() as session:
            return (
                not session.query(DurableWork.id)
                .filter(
                    DurableWork.job_id == job_id, DurableWork.kind == "post_process", DurableWork.state != "succeeded"
                )
                .first()
            )

    def ensure(self, job_id: str, kind: str, payload: dict) -> str:
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind=kind).first()
            if row:
                return row.state
            session.add(DurableWork(job_id=job_id, kind=kind, payload_json=json.dumps(payload)))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
            return session.query(DurableWork).filter_by(job_id=job_id, kind=kind).one().state

    def claim(self, job_id: str, kind: str) -> bool:
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind=kind).first()
            if not row or row.state not in PENDING_STATES:
                return False
            if row.attempt_count >= MAX_WORK_ATTEMPTS:
                row.state = "failed"
                row.last_error = "Work attempt limit reached; manual retry required"
                session.commit()
                return False
            if row.next_attempt_at and ensure_utc(row.next_attempt_at) > utc_now():
                return False
            changed = (
                session.query(DurableWork)
                .filter_by(id=row.id, state=row.state)
                .update({"state": "running", "attempt_count": row.attempt_count + 1, "next_attempt_at": None})
            )
            session.commit()
            return bool(changed)

    def succeed(self, job_id: str, kind: str, *, upload_request=None) -> None:
        """Atomically complete post-processing and create its follow-up upload intent."""
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind=kind).first()
            if row:
                row.state, row.last_error, row.next_attempt_at = "succeeded", None, None
                if kind == "upload":
                    row.resume_json = None
            if upload_request and not session.query(DurableWork).filter_by(job_id=job_id, kind="upload").first():
                session.add(
                    DurableWork(job_id=job_id, kind="upload", payload_json=json.dumps(upload_payload(upload_request)))
                )
            session.commit()

    def fail(self, job_id: str, kind: str, *, error: str, retryable: bool = True, needs_review: bool = False) -> None:
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind=kind).first()
            if not row:
                return
            # Callers pass safe, bounded error categories, never provider response bodies/session URIs.
            row.last_error = error[:240]
            retry = retryable and row.attempt_count < MAX_WORK_ATTEMPTS
            row.state = "needs_review" if needs_review else ("retry_wait" if retry else "failed")
            row.next_attempt_at = (
                utc_now() + timedelta(seconds=min(30 * 2 ** max(0, row.attempt_count - 1), 900)) if retry else None
            )
            session.commit()

    def release(self, job_id: str, kind: str) -> None:
        with self._session() as session:
            session.query(DurableWork).filter_by(job_id=job_id, kind=kind, state="running").update({"state": "pending"})
            session.commit()

    def retry_failed(self, job_id: str, kind: str) -> bool:
        """An explicit operator retry preserves the existing resumable session."""
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind=kind, state="failed").first()
            if not row:
                return False
            if kind == "post_process" and session.query(DurableWork.id).filter_by(job_id=job_id, kind="upload").first():
                return False
            row.state, row.attempt_count, row.last_error, row.next_attempt_at = "pending", 0, None, None
            session.commit()
            return True

    def resolve_upload(self, job_id: str, *, video_id: str | None = None, confirmed_absent: bool = False) -> bool:
        """Operator reconciliation is the only way to replace an ambiguous expired session."""
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind="upload").first()
            job = session.query(RecordingJobModel).filter_by(job_id=job_id).first()
            if not row or not job or row.state not in ("needs_review", "failed"):
                return False
            if job.youtube_video_id and job.youtube_video_id != video_id:
                return False
            if video_id:
                state = json.loads(row.resume_json or "{}")
                state["video_id"] = video_id
                row.resume_json = json.dumps(state)
                job.youtube_video_id, job.youtube_uploaded_at = video_id, utc_now()
                job.status, job.error_message = "succeeded", None
            elif confirmed_absent:
                row.resume_json = None
            else:
                return False
            row.state, row.attempt_count, row.last_error, row.next_attempt_at = "pending", 0, None, None
            session.commit()
            return True

    def reset_interrupted(self) -> None:
        with self._session() as session:
            session.query(DurableWork).filter_by(state="running").update({"state": "pending"})
            session.commit()

    def load_request(self, job_id: str, kind: str):
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind=kind).one()
            payload = json.loads(row.payload_json)
        return self.restore_request(job_id, kind, payload)

    def ready(self) -> list[tuple[str, str, dict]]:
        with self._session() as session:
            rows = (
                session.query(DurableWork).filter(DurableWork.state.in_(PENDING_STATES)).order_by(DurableWork.id).all()
            )
            return [
                (row.job_id, row.kind, json.loads(row.payload_json))
                for row in rows
                if not row.next_attempt_at or ensure_utc(row.next_attempt_at) <= utc_now()
            ]

    def resume_state(self, job_id: str) -> dict:
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind="upload").first()
            return json.loads(row.resume_json or "{}") if row else {}

    def save_resume_state(self, job_id: str, state: dict) -> None:
        with self._session() as session:
            row = session.query(DurableWork).filter_by(job_id=job_id, kind="upload").one()
            row.resume_json = json.dumps(state)
            session.commit()

    def snapshot(self) -> dict:
        """Safe operations view: deliberately omit payload and private resumable capability."""
        with self._session() as session:
            counts = dict(
                session.query(DurableWork.state, func.count(DurableWork.id)).group_by(DurableWork.state).all()
            )
            rows = session.query(DurableWork).order_by(DurableWork.updated_at.desc()).limit(100).all()
            oldest = (
                session.query(func.min(DurableWork.created_at)).filter(DurableWork.state.in_(PENDING_STATES)).scalar()
            )
            return {
                "counts": counts,
                "oldest_pending_age_sec": max(0, int((utc_now() - ensure_utc(oldest)).total_seconds()))
                if oldest
                else None,
                "items": [
                    {
                        "job_id": row.job_id,
                        "kind": row.kind,
                        "state": row.state,
                        "attempt_count": row.attempt_count,
                        "next_attempt_at": ensure_utc(row.next_attempt_at).isoformat() if row.next_attempt_at else None,
                        "last_error": row.last_error,
                    }
                    for row in rows
                ],
            }

    def restore_request(self, job_id: str, kind: str, payload: dict):
        # Import lazily: post-processing depends on UploadRequest.
        from database.models import JobStatus
        from recording.activity import ActivityConfig
        from recording.ffmpeg_pipeline import RecordingInfo
        from recording.job_types import RecordingJob, RecordingResult
        from recording.post_processing import RecordingPostProcessingRequest
        from recording.remux import recording_file_variants
        from scheduling.upload_runner import UploadRequest

        with self._session() as session:
            db_job = session.query(RecordingJobModel).filter_by(job_id=job_id).first()
            if not db_job:
                raise ValueError("Recording job no longer exists")
            if kind == "upload":
                data = dict(payload)
                for key in ("video_path", "raw_video_path", "cleanup_video_path_after_success"):
                    if data.get(key):
                        data[key] = Path(data[key])
                return UploadRequest(**data)
            data = dict(payload["job"])
            data["output_dir"] = Path(data["output_dir"])
            if data.get("diagnostics_dir"):
                data["diagnostics_dir"] = Path(data["diagnostics_dir"])
            data["activity_config"] = ActivityConfig(**data["activity_config"])
            job = RecordingJob(
                provider=db_job.provider,
                meeting_code=db_job.meeting_code,
                display_name=db_job.display_name,
                duration_sec=db_job.duration_sec,
                **data,
            )
            result_data = dict(payload["result"])
            for key in ("output_path", "raw_output_path", "trimmed_output_path"):
                if result_data.get(key):
                    result_data[key] = Path(result_data[key])
            for key in ("start_time", "joined_at", "recording_started_at", "recording_stopped_at", "end_time"):
                if result_data.get(key):
                    result_data[key] = datetime.fromisoformat(result_data[key])
            info = result_data.pop("recording_info")
            if info:
                path = Path(info["output_path"])
                # A previous attempt may have already canonicalized MKV to MP4 before interruption.
                path = next((candidate for candidate in recording_file_variants(path) if candidate.exists()), path)
                info = RecordingInfo(
                    output_path=path,
                    file_size=path.stat().st_size,
                    duration_sec=info["duration_sec"],
                    start_time=datetime.fromisoformat(info["start_time"]),
                    end_time=datetime.fromisoformat(info["end_time"]),
                )
            result = RecordingResult(
                job_id=job_id,
                status=JobStatus.SUCCEEDED,
                attempt_no=job.attempt_no,
                recording_info=info,
                runtime_summary=db_job.runtime_summary,
                **result_data,
            )
            return RecordingPostProcessingRequest(
                job=job,
                result=result,
                youtube_enabled=payload["youtube_enabled"],
                youtube_privacy=payload["youtube_privacy"],
                meeting_name=payload["meeting_name"],
            )

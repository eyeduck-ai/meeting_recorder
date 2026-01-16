import asyncio
import logging
from datetime import datetime
from pathlib import Path

from database.models import (
    JobStatus,
    Schedule,
    get_session_local,
)
from database.session import JobRepository, build_result_update_fields
from recording.worker import RecordingJob, get_worker
from telegram_bot.notifications import (
    notify_recording_completed,
    notify_recording_failed,
    notify_recording_started,
    notify_youtube_upload_completed,
)
from uploading.youtube import (
    UploadStatus,
    VideoMetadata,
    get_youtube_uploader,
)

logger = logging.getLogger(__name__)


class JobRunner:
    """Job runner with single concurrency enforcement.

    Ensures only one recording job runs at a time by using an asyncio lock.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._current_schedule_id: int | None = None
        self._queue: list[int] = []  # Queue of schedule IDs waiting to run

    @property
    def is_busy(self) -> bool:
        """Check if a job is currently running."""
        return self._lock.locked()

    @property
    def current_schedule_id(self) -> int | None:
        """Get the currently running schedule ID."""
        return self._current_schedule_id

    @property
    def queue_length(self) -> int:
        """Get number of jobs waiting in queue."""
        return len(self._queue)

    def queue_schedule(self, schedule_id: int) -> bool:
        """Queue a schedule to run.

        If not busy, starts immediately. Otherwise, adds to queue.

        Args:
            schedule_id: Schedule ID to queue

        Returns:
            True if queued/started successfully
        """
        if schedule_id in self._queue:
            logger.warning(f"Schedule {schedule_id} already in queue")
            return False

        # Start async task to run the schedule
        asyncio.create_task(self._run_when_available(schedule_id))
        return True

    async def _run_when_available(self, schedule_id: int) -> None:
        """Wait for lock and run schedule.

        Args:
            schedule_id: Schedule ID to run
        """
        if self._lock.locked():
            logger.info(f"Schedule {schedule_id} waiting in queue (queue length: {len(self._queue)})")
            self._queue.append(schedule_id)

        async with self._lock:
            # Remove from queue if present
            if schedule_id in self._queue:
                self._queue.remove(schedule_id)

            self._current_schedule_id = schedule_id
            try:
                await self._execute_schedule(schedule_id)
            finally:
                self._current_schedule_id = None

    async def _execute_schedule(self, schedule_id: int) -> None:
        """Execute a scheduled recording.

        Args:
            schedule_id: Schedule ID to execute
        """
        logger.info(f"Executing schedule {schedule_id}")

        SessionLocal = get_session_local()
        session = SessionLocal()

        try:
            # Load schedule with meeting
            schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()

            if not schedule:
                logger.error(f"Schedule {schedule_id} not found")
                return

            if not schedule.enabled:
                logger.warning(f"Schedule {schedule_id} is disabled, skipping")
                return

            meeting = schedule.meeting
            if not meeting:
                logger.error(f"Meeting not found for schedule {schedule_id}")
                return

            # Create recording job
            job = RecordingJob.create(
                provider=meeting.provider,
                meeting_code=schedule.get_effective_meeting_code(),
                display_name=schedule.get_effective_display_name(),
                duration_sec=schedule.duration_sec,
                base_url=meeting.site_base_url,
                lobby_wait_sec=schedule.lobby_wait_sec,
                duration_mode=schedule.duration_mode,
                dry_run=schedule.dry_run,
                min_duration_sec=schedule.min_duration_sec,
                stillness_timeout_sec=schedule.stillness_timeout_sec,
            )

            # Store job in database
            repo = JobRepository(session)
            repo.create(
                job_id=job.job_id,
                schedule_id=schedule_id,
                provider=meeting.provider,
                meeting_code=schedule.get_effective_meeting_code(),
                display_name=schedule.get_effective_display_name(),
                base_url=meeting.site_base_url,
                duration_sec=schedule.duration_sec,
                lobby_wait_sec=schedule.lobby_wait_sec,
                status=JobStatus.QUEUED.value,
            )
            session.commit()

            logger.info(f"Created job {job.job_id} for schedule {schedule_id}")

        except Exception as e:
            logger.error(f"Failed to create job for schedule {schedule_id}: {e}")
            session.rollback()
            return
        finally:
            session.close()

        # Run the recording
        worker = get_worker()

        def on_status_change(job_id: str, status: JobStatus):
            """Update status in database and send notifications."""
            s = SessionLocal()
            try:
                r = JobRepository(s)
                update_fields = {}
                if status == JobStatus.STARTING:
                    update_fields["started_at"] = datetime.now()
                elif status == JobStatus.RECORDING:
                    update_fields["recording_started_at"] = datetime.now()
                r.update_status(job_id, status.value, **update_fields)
                s.commit()

                # Send start notification when recording begins
                if status == JobStatus.RECORDING:
                    db_job = r.get_by_job_id(job_id)
                    if db_job:
                        asyncio.create_task(notify_recording_started(db_job))
            finally:
                s.close()

        worker.set_status_callback(on_status_change)

        try:
            result = await worker.record(job)

            # Update database with result
            session = SessionLocal()
            youtube_enabled = False
            youtube_privacy = "unlisted"
            output_path = None

            try:
                repo = JobRepository(session)
                update_fields = build_result_update_fields(result)

                if result.recording_info:
                    output_path = result.recording_info.output_path

                # Get YouTube settings from schedule
                schedule = session.query(Schedule).filter(Schedule.id == schedule_id).first()
                if schedule:
                    youtube_enabled = schedule.youtube_enabled
                    youtube_privacy = schedule.youtube_privacy
                    update_fields["youtube_enabled"] = youtube_enabled

                repo.update_status(job.job_id, result.status.value, **update_fields)
                session.commit()

                # Send completion/failure notification
                db_job = repo.get_by_job_id(job.job_id)
                if db_job:
                    if result.status == JobStatus.SUCCEEDED:
                        await notify_recording_completed(db_job)
                    elif result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                        await notify_recording_failed(db_job)

                logger.info(f"Job {job.job_id} completed with status: {result.status.value}")
            finally:
                session.close()

            # YouTube upload if enabled and recording succeeded
            if youtube_enabled and result.status == JobStatus.SUCCEEDED and output_path and output_path.exists():
                await self._upload_to_youtube(
                    job_id=job.job_id,
                    video_path=output_path,
                    title=f"Recording - {job.meeting_code}",
                    privacy=youtube_privacy,
                )

        except Exception as e:
            logger.error(f"Error running job for schedule {schedule_id}: {e}")

    async def _upload_to_youtube(
        self,
        job_id: str,
        video_path: Path,
        title: str,
        privacy: str = "unlisted",
    ) -> None:
        """Upload recording to YouTube.

        Args:
            job_id: Job ID for status updates
            video_path: Path to video file
            title: Video title
            privacy: Privacy status (public, private, unlisted)
        """
        logger.info(f"Starting YouTube upload for job {job_id}")

        SessionLocal = get_session_local()
        session = SessionLocal()

        try:
            # Update status to uploading
            repo = JobRepository(session)
            repo.update_status(job_id, JobStatus.UPLOADING.value)
            session.commit()
        finally:
            session.close()

        # Get uploader
        uploader = get_youtube_uploader()

        if not uploader.is_configured:
            logger.warning("YouTube upload skipped - not configured")
            return

        if not uploader.is_authorized:
            logger.warning("YouTube upload skipped - not authorized")
            return

        try:
            # Prepare metadata
            metadata = VideoMetadata(
                title=title,
                description=f"Recorded meeting - {job_id}",
                privacy_status=privacy,
            )

            # Upload with progress logging
            def log_progress(uploaded: int, total: int):
                percent = (uploaded / total * 100) if total > 0 else 0
                logger.debug(f"Upload progress: {percent:.1f}% ({uploaded}/{total} bytes)")

            result = await uploader.upload_video(
                video_path=video_path,
                metadata=metadata,
                progress_callback=log_progress,
            )

            # Update database with result
            session = SessionLocal()
            try:
                repo = JobRepository(session)
                if result.status == UploadStatus.SUCCEEDED:
                    repo.update_status(
                        job_id,
                        JobStatus.SUCCEEDED.value,
                        youtube_video_id=result.video_id,
                    )
                    logger.info(f"YouTube upload successful: {result.video_url}")
                    session.commit()

                    # Send YouTube upload notification
                    db_job = repo.get_by_job_id(job_id)
                    if db_job and result.video_url:
                        await notify_youtube_upload_completed(db_job, result.video_url)
                else:
                    # Upload failed but recording succeeded - keep succeeded status
                    logger.error(f"YouTube upload failed: {result.error_message}")
                    session.commit()
            finally:
                session.close()

        except Exception as e:
            logger.error(f"YouTube upload error: {e}")

    async def run_immediate(
        self,
        provider: str,
        meeting_code: str,
        display_name: str,
        duration_sec: int,
        base_url: str | None = None,
        lobby_wait_sec: int = 900,
    ) -> str | None:
        """Run a recording immediately (not from schedule).

        Args:
            provider: Meeting provider
            meeting_code: Meeting code
            display_name: Display name
            duration_sec: Duration in seconds
            base_url: Base URL override
            lobby_wait_sec: Lobby wait time

        Returns:
            Job ID if started, None if busy
        """
        if self._lock.locked():
            logger.warning("Cannot run immediate job - worker is busy")
            return None

        job = RecordingJob.create(
            provider=provider,
            meeting_code=meeting_code,
            display_name=display_name,
            duration_sec=duration_sec,
            base_url=base_url,
            lobby_wait_sec=lobby_wait_sec,
        )

        # Create task to run the job
        asyncio.create_task(self._run_immediate_job(job))
        return job.job_id

    async def _run_immediate_job(self, job: RecordingJob) -> None:
        """Run an immediate job with lock.

        Args:
            job: RecordingJob to run
        """
        async with self._lock:
            SessionLocal = get_session_local()
            session = SessionLocal()

            try:
                # Store job in database
                repo = JobRepository(session)
                repo.create(
                    job_id=job.job_id,
                    provider=job.provider,
                    meeting_code=job.meeting_code,
                    display_name=job.display_name,
                    base_url=job.base_url,
                    duration_sec=job.duration_sec,
                    lobby_wait_sec=job.lobby_wait_sec,
                    status=JobStatus.QUEUED.value,
                )
                session.commit()
            finally:
                session.close()

            # Set up status callback for start notification
            def on_status_change(job_id: str, status: JobStatus):
                """Update status in database and send notifications."""
                s = SessionLocal()
                try:
                    r = JobRepository(s)
                    update_fields = {}
                    if status == JobStatus.STARTING:
                        update_fields["started_at"] = datetime.now()
                    elif status == JobStatus.RECORDING:
                        update_fields["recording_started_at"] = datetime.now()
                    r.update_status(job_id, status.value, **update_fields)
                    s.commit()

                    # Send start notification when recording begins
                    if status == JobStatus.RECORDING:
                        db_job = r.get_by_job_id(job_id)
                        if db_job:
                            asyncio.create_task(notify_recording_started(db_job))
                finally:
                    s.close()

            # Run recording
            worker = get_worker()
            worker.set_status_callback(on_status_change)
            result = await worker.record(job)

            # Update database
            session = SessionLocal()
            try:
                repo = JobRepository(session)
                update_fields = build_result_update_fields(result)

                repo.update_status(job.job_id, result.status.value, **update_fields)
                session.commit()

                # Send completion/failure notification
                db_job = repo.get_by_job_id(job.job_id)
                if db_job:
                    if result.status == JobStatus.SUCCEEDED:
                        await notify_recording_completed(db_job)
                    elif result.status in (JobStatus.FAILED, JobStatus.CANCELED):
                        await notify_recording_failed(db_job)
            finally:
                session.close()


# Global job runner instance
_runner_instance: JobRunner | None = None


def get_job_runner() -> JobRunner:
    """Get the global job runner instance."""
    global _runner_instance
    if _runner_instance is None:
        _runner_instance = JobRunner()
    return _runner_instance

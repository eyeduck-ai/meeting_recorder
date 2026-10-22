"""Non-blocking, bounded-cost readiness probes shared by health and operations."""

import asyncio
import time
from copy import deepcopy

from sqlalchemy import text

from database.session import get_session_local
from recording.runtime_checks import get_recording_runtime_status


class HealthProbe:
    def __init__(self, *, ttl_sec: float = 10, timeout_sec: float = 7):
        self._ttl_sec = ttl_sec
        self._timeout_sec = timeout_sec
        self._expires_at = 0.0
        self._value = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _probe() -> dict:
        try:
            runtime = get_recording_runtime_status()
        except Exception:
            runtime = {"ready": False, "probe_error": "runtime_probe_failed"}
        database_ready = False
        try:
            with get_session_local()() as db:
                db.execute(text("SELECT 1"))
                database_ready = True
        except Exception:
            pass
        return {"recording_runtime": runtime, "database_ready": database_ready}

    async def snapshot(self) -> dict:
        async with self._lock:
            if self._value is not None and time.monotonic() < self._expires_at:
                return deepcopy(self._value)
            if self._task is None:
                self._task = asyncio.create_task(asyncio.to_thread(self._probe), name="recording-health-probe")
            task = self._task
        try:
            # A timed-out / disconnected caller must not cancel the in-flight
            # worker or create another thread on the next health request.
            value = await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout_sec)
        except TimeoutError:
            return {"recording_runtime": {"ready": False}, "database_ready": False, "probe_timed_out": True}
        except Exception:
            value = {"recording_runtime": {"ready": False}, "database_ready": False, "probe_error": "probe_failed"}
        async with self._lock:
            if self._task is task:
                self._value = value
                self._expires_at = time.monotonic() + self._ttl_sec
                self._task = None
        return deepcopy(value)


def get_health_probe(app) -> HealthProbe:
    if not hasattr(app.state, "health_probe"):
        app.state.health_probe = HealthProbe()
    return app.state.health_probe

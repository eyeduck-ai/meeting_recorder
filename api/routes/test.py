"""Test API endpoints for system diagnostics."""

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from testing.executor import get_executor
from testing.models import TestStatus, TestType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/test", tags=["test"])


class RunTestRequest(BaseModel):
    """Request body for running a test."""

    test_type: str
    params: dict[str, Any] | None = None


class TestResponse(BaseModel):
    """Response for test operations."""

    test_id: str
    test_type: str
    status: str
    message: str | None = None


@router.post("/run", response_model=TestResponse)
async def run_test(request: RunTestRequest):
    """Start a test execution."""
    try:
        test_type = TestType(request.test_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid test type: {request.test_type}. Valid types: {[t.value for t in TestType]}",
        )

    executor = get_executor()
    test_id = await executor.run_test(test_type, request.params or {})

    return TestResponse(
        test_id=test_id,
        test_type=test_type.value,
        status="started",
        message=f"Test {test_id} started",
    )


@router.get("/{test_id}/status")
async def get_test_status(test_id: str):
    """Get the status of a test."""
    executor = get_executor()
    test_run = executor.get_test_run(test_id)

    if not test_run:
        raise HTTPException(status_code=404, detail="Test not found")

    return test_run.to_dict()


@router.post("/{test_id}/stop")
async def stop_test(test_id: str):
    """Stop a running test."""
    executor = get_executor()
    success = await executor.stop_test(test_id)

    if not success:
        raise HTTPException(
            status_code=400,
            detail="Test not found or not running",
        )

    return {"success": True, "message": "Test stopped"}


@router.get("/{test_id}/logs")
async def stream_test_logs(test_id: str):
    """Stream test logs via Server-Sent Events."""
    executor = get_executor()
    test_run = executor.get_test_run(test_id)

    if not test_run:
        raise HTTPException(status_code=404, detail="Test not found")

    async def event_generator():
        last_index = 0

        while True:
            test_run = executor.get_test_run(test_id)
            if not test_run:
                yield "event: error\ndata: Test not found\n\n"
                break

            # Send new logs
            for log in test_run.logs[last_index:]:
                yield f"data: {log.format()}\n\n"
                last_index += 1

            # Check if test is complete
            if test_run.status not in (TestStatus.PENDING, TestStatus.RUNNING):
                yield f"event: complete\ndata: {test_run.status.value}\n\n"
                break

            await asyncio.sleep(0.2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/vnc-info")
async def get_vnc_info():
    """Get VNC connection information."""
    debug_vnc = os.environ.get("DEBUG_VNC", "0") == "1"
    display = os.environ.get("DISPLAY", ":99")

    return {
        "enabled": debug_vnc,
        "display": display,
        "port": 5900,
        "resolution": "1920x1080",
        "instructions": "Connect using VNC client to container:5900" if debug_vnc else "Set DEBUG_VNC=1 to enable VNC",
    }


@router.get("/list")
async def list_tests():
    """List all test runs."""
    executor = get_executor()
    tests = executor.get_all_tests()
    return [t.to_dict() for t in tests]


@router.post("/clear")
async def clear_completed():
    """Clear completed tests from memory."""
    executor = get_executor()
    count = executor.clear_completed()
    return {"cleared": count}

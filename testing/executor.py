"""Test executor for managing async test tasks."""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from testing.models import TestResult, TestRun, TestStatus, TestType
from testing.tests.base import BaseTest

logger = logging.getLogger(__name__)

# Global executor instance
_executor: "TestExecutor | None" = None


def get_executor() -> "TestExecutor":
    """Get the global test executor instance."""
    global _executor
    if _executor is None:
        _executor = TestExecutor()
    return _executor


class TestExecutor:
    """Manages async test execution with SSE log streaming support."""

    def __init__(self) -> None:
        self._tests: dict[str, TestRun] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._virtual_env_lock = asyncio.Lock()
        self._test_instances: dict[str, BaseTest] = {}

    def get_test_run(self, test_id: str) -> TestRun | None:
        """Get a test run by ID."""
        return self._tests.get(test_id)

    def get_all_tests(self) -> list[TestRun]:
        """Get all test runs."""
        return list(self._tests.values())

    def get_active_tests(self) -> list[TestRun]:
        """Get all running tests."""
        return [t for t in self._tests.values() if t.status == TestStatus.RUNNING]

    async def run_test(
        self,
        test_type: TestType,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Start a test execution.

        Args:
            test_type: Type of test to run
            params: Optional parameters for the test

        Returns:
            Test ID for tracking
        """
        test_id = str(uuid.uuid4())[:8]
        params = params or {}

        test_run = TestRun(
            test_id=test_id,
            test_type=test_type,
            status=TestStatus.PENDING,
            params=params,
        )
        self._tests[test_id] = test_run

        # Create and start the test task
        task = asyncio.create_task(self._execute_test(test_id, test_type, params))
        self._tasks[test_id] = task

        return test_id

    async def stop_test(self, test_id: str) -> bool:
        """Stop a running test.

        Args:
            test_id: ID of the test to stop

        Returns:
            True if test was stopped, False if not found or not running
        """
        test_run = self._tests.get(test_id)
        if not test_run or test_run.status != TestStatus.RUNNING:
            return False

        # Cancel the test instance
        test_instance = self._test_instances.get(test_id)
        if test_instance:
            test_instance.cancel()

        # Cancel the task
        task = self._tasks.get(test_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        test_run.status = TestStatus.CANCELLED
        test_run.completed_at = datetime.now()
        test_run.add_log("Test cancelled by user", "WARNING")

        return True

    def clear_completed(self) -> int:
        """Clear all completed tests from memory.

        Returns:
            Number of tests cleared
        """
        completed_ids = [
            tid
            for tid, t in self._tests.items()
            if t.status in (TestStatus.SUCCEEDED, TestStatus.FAILED, TestStatus.CANCELLED)
        ]
        for tid in completed_ids:
            del self._tests[tid]
            self._tasks.pop(tid, None)
            self._test_instances.pop(tid, None)
        return len(completed_ids)

    async def _execute_test(
        self,
        test_id: str,
        test_type: TestType,
        params: dict[str, Any],
    ) -> None:
        """Execute a test and update its status."""
        test_run = self._tests[test_id]
        test_run.status = TestStatus.RUNNING
        test_run.started_at = datetime.now()
        test_run.add_log(f"Starting {test_type.value} test...")

        test_instance: BaseTest | None = None
        needs_virtual_env = test_type in (
            TestType.BROWSER,
            TestType.PROVIDER,
            TestType.RECORDING,
        )

        try:
            # Create test instance
            test_instance = self._create_test_instance(test_type, params)
            self._test_instances[test_id] = test_instance

            # Set up logging callback
            def log_callback(message: str, level: str = "INFO") -> None:
                test_run.add_log(message, level)

            test_instance.set_log_callback(log_callback)

            # Acquire lock if needed
            if needs_virtual_env:
                test_run.add_log("Waiting for virtual environment lock...")
                async with self._virtual_env_lock:
                    test_run.add_log("Lock acquired, running test...")
                    result = await test_instance.run()
            else:
                result = await test_instance.run()

            # Update test run with result
            test_run.result = result
            if result.success:
                test_run.status = TestStatus.SUCCEEDED
                test_run.add_log("Test completed successfully", "SUCCESS")
            else:
                test_run.status = TestStatus.FAILED
                test_run.add_log(f"Test failed: {result.error}", "ERROR")

        except asyncio.CancelledError:
            test_run.status = TestStatus.CANCELLED
            test_run.add_log("Test was cancelled", "WARNING")
            raise

        except Exception as e:
            logger.exception(f"Test {test_id} failed with exception")
            test_run.status = TestStatus.FAILED
            test_run.result = TestResult(success=False, error=str(e))
            test_run.add_log(f"Test failed with exception: {e}", "ERROR")

        finally:
            test_run.completed_at = datetime.now()
            if test_instance:
                try:
                    await test_instance.cleanup()
                except Exception as e:
                    test_run.add_log(f"Cleanup error: {e}", "WARNING")
            self._test_instances.pop(test_id, None)

    def _create_test_instance(
        self,
        test_type: TestType,
        params: dict[str, Any],
    ) -> BaseTest:
        """Create a test instance based on type."""
        from testing.tests.browser import BrowserTest
        from testing.tests.environment import EnvironmentTest
        from testing.tests.provider import ProviderTest
        from testing.tests.recording import RecordingTest
        from testing.tests.telegram import TelegramTest
        from testing.tests.youtube import YouTubeTest

        test_classes: dict[TestType, type[BaseTest]] = {
            TestType.ENVIRONMENT: EnvironmentTest,
            TestType.BROWSER: BrowserTest,
            TestType.PROVIDER: ProviderTest,
            TestType.RECORDING: RecordingTest,
            TestType.TELEGRAM: TelegramTest,
            TestType.YOUTUBE: YouTubeTest,
        }

        test_class = test_classes.get(test_type)
        if not test_class:
            raise ValueError(f"Unknown test type: {test_type}")

        return test_class(**params)

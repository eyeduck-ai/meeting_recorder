"""Tests for environment-backed application settings."""

import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_recording_capacity_validation_rejects_zero_concurrency():
    with pytest.raises(ValidationError, match="MAX_CONCURRENT_RECORDINGS"):
        Settings(max_concurrent_recordings=0, _env_file=None)


def test_recording_capacity_validation_rejects_zero_display_pool():
    with pytest.raises(ValidationError, match="RECORDING_DISPLAY_POOL_SIZE"):
        Settings(recording_display_pool_size=0, _env_file=None)


def test_recording_capacity_validation_rejects_concurrency_above_display_pool():
    with pytest.raises(ValidationError, match="MAX_CONCURRENT_RECORDINGS"):
        Settings(max_concurrent_recordings=3, recording_display_pool_size=2, _env_file=None)


def test_recording_capacity_validation_rejects_zero_activity_analysis_limit():
    with pytest.raises(ValidationError, match="MAX_PARALLEL_ACTIVITY_ANALYSES"):
        Settings(max_parallel_activity_analyses=0, _env_file=None)

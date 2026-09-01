"""Basic tests to verify the testing setup works."""

import pytest


def test_setup_ok() -> None:
    """Trivial test to confirm pytest is configured correctly."""
    assert True


@pytest.mark.asyncio
async def test_async_setup() -> None:
    """Test that async tests work."""
    await asyncio.sleep(0)
    assert True


# Import asyncio here to avoid issues if pytest-asyncio not installed
import asyncio  # noqa: E402
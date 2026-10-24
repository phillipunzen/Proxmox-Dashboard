from __future__ import annotations

import pytest

from app.config import Settings


class FakeClock:
    """Manually advanced clock so rate calculations are deterministic."""

    def __init__(self, start: float = 1_760_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(pve_mock=True, poll_interval=10, node_status_interval=30,
                    static_dir="/nonexistent", _env_file=None)

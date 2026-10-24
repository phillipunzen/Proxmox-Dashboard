"""Time ranges and query helpers shared by history and analysis endpoints."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from app.tsdb.client import Series

RangeKey = Literal["1h", "6h", "24h", "7d", "30d"]

# (span seconds, step seconds) -> 360..720 points per series.
RANGES: dict[str, tuple[int, int]] = {
    "1h": (3600, 10),
    "6h": (6 * 3600, 60),
    "24h": (86400, 300),
    "7d": (7 * 86400, 900),
    "30d": (30 * 86400, 3600),
}

# Rollup windows never shrink below this, otherwise a 10 s step with a 10 s poll interval
# would randomly hit windows without any sample.
MIN_WINDOW = 20


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    step: int

    @property
    def rollup(self) -> str:
        """Lookbehind for ``*_over_time`` so every step averages its own interval."""
        return f"{max(self.step, MIN_WINDOW)}s"

    @property
    def span(self) -> str:
        return f"{int(self.end - self.start)}s"


def resolve_window(range_key: str = "1h", end: float | None = None,
                   start: float | None = None, poll_interval: float = 10) -> Window:
    """Preset range ending now (default) or an explicit [start, end]."""
    end = end or time.time()
    if start is not None:
        span = max(60.0, end - start)
        step = max(int(poll_interval), int(span / 720) or 1)
        return Window(end - span, end, step)
    span, step = RANGES[range_key]
    step = max(step, int(poll_interval))
    # Align to the step so consecutive refreshes hit the same buckets.
    end = (end // step) * step
    return Window(end - span, end, step)


def esc(value: object) -> str:
    """Escape a value for use inside a double-quoted label matcher."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def selector(metric: str, **matchers: str | None) -> str:
    """``metric{a="x",b=~"y"}`` - keys ending in ``__re`` become regex matchers."""
    parts = []
    for key, value in matchers.items():
        if value is None:
            continue
        if key.endswith("__re"):
            parts.append(f'{key[:-4]}=~"{esc(value)}"')
        else:
            parts.append(f'{key}="{esc(value)}"')
    return f"{metric}{{{','.join(parts)}}}"


def points(series: list[Series]) -> list[list[float]]:
    """First series as ``[[ts, value], ...]`` (empty list if no data)."""
    return [[t, v] for t, v in series[0].points] if series else []

"""Shared FF14 result value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    source_url: str | None = None
    source_path: str | None = None
    heading: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class Freshness:
    source: str
    source_updated_at: datetime | None
    cache_status: Literal["fresh", "stale", "miss"]
    stale: bool

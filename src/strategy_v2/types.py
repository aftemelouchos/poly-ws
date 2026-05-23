"""Strategy v2 domain types (live + paper ortak)."""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    FLAT = "FLAT"
    UP = "UP"
    DOWN = "DOWN"

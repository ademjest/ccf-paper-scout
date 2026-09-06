from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Paper


@dataclass
class SourceBatch:
    records: list[Paper]
    next_cursor: str | None = None
    raw_count: int = 0
    failed_partitions: list[dict[str, str]] = field(default_factory=list)
    exhausted: bool = False


class PaperSource(Protocol):
    source_id: str

    def discover(self, request: dict, cursor: str | None = None) -> SourceBatch:
        ...

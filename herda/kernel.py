"""Kernel: one prompt, one herdr session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import Graph
    from .stream import Stream


@dataclass(frozen=True, eq=False)
class Kernel:
    graph: "Graph"
    stream: "Stream"
    name: str
    prompt: Path
    model: str
    inherit_session: bool = False
    timeout_s: float | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.stream.name}/{self.name}"

    def __repr__(self) -> str:
        return f"Kernel({self.qualified_name!r})"

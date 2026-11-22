"""Kernel: one prompt, one herdr session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

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
    agent_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.stream.name}/{self.name}"

    def __repr__(self) -> str:
        return f"Kernel({self.qualified_name!r})"

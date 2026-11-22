"""Stream events.

The model mirrors CUDA:

- a stream records an event after all work currently queued in that stream;
- another stream waits on that event before its next kernel;
- kernels inside one stream are always serialized.

Events are graph-wide named handles. An event is recorded once, at the point
where ``Stream.record_event`` is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import Graph
    from .kernel import Kernel
    from .stream import Stream


@dataclass(frozen=True, eq=False)
class Event:
    graph: "Graph"
    stream: "Stream"
    name: str
    after: "Kernel | None"

    @property
    def qualified_name(self) -> str:
        return f"{self.stream.name}/{self.name}"

    def __repr__(self) -> str:
        return f"Event({self.qualified_name!r})"

"""herda: CUDA-like streams and kernels for coding agents.

A stream is a git worktree. A kernel is one prompt in one herdr session.
The DAG scheduler starts kernels when their dependencies succeed.
"""

from __future__ import annotations

from .errors import (
    CleanupError,
    ConcurrentRunError,
    HerdaError,
    HerdaTimeoutError,
    HerdrError,
    PromptError,
    ValidationError,
    WorktreeError,
)
from .event import Event
from .execution import Execution
from .graph import Graph
from .kernel import Kernel
from .stream import Stream
from .types import ErrorInfo, KernelResult, KernelState, RunState

__all__ = [
    "Graph",
    "Stream",
    "Kernel",
    "Execution",
    "Event",
    "RunState",
    "KernelState",
    "KernelResult",
    "ErrorInfo",
    "HerdaError",
    "ValidationError",
    "PromptError",
    "WorktreeError",
    "HerdrError",
    "HerdaTimeoutError",
    "ConcurrentRunError",
    "CleanupError",
]

__version__ = "0.2.0"

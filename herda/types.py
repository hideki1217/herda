"""Public enums and result records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class RunState(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class KernelState(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    awaiting_input = "awaiting_input"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    canceled = "canceled"

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_KERNEL_STATES


_TERMINAL_KERNEL_STATES = frozenset(
    {
        KernelState.succeeded,
        KernelState.failed,
        KernelState.skipped,
        KernelState.canceled,
    }
)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    hint: str | None = None


@dataclass(frozen=True)
class KernelResult:
    name: str
    stream: str
    state: KernelState
    model: str
    terminal: str | None
    branch: str
    worktree: Path
    prompt_sha256: str
    status: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_s: float | None
    output_path: Path | None
    error: ErrorInfo | None

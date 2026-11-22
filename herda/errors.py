"""Exception hierarchy for herda.

Every public exception derives from :class:`HerdaError` and carries a stable
``code``, a human readable ``message`` and an optional ``hint``.
"""

from __future__ import annotations


class HerdaError(Exception):
    """Base class for every herda API error."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        text = f"{self.code}: {self.message}"
        if self.hint:
            text += f" (hint: {self.hint})"
        return text

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"message={self.message!r}, hint={self.hint!r})"
        )


class ValidationError(HerdaError):
    """The graph itself is malformed or the API was used incorrectly."""


class PromptError(HerdaError):
    """A kernel prompt is missing, empty, unreadable or not Markdown."""


class WorktreeError(HerdaError):
    """A git worktree or branch operation failed."""


class HerdrError(HerdaError):
    """``hc`` is missing, the core is unreachable, or an ``hc`` call failed."""


class HerdaTimeoutError(HerdaError):
    """``Execution.wait(timeout_s)`` exceeded its wait budget."""


class ConcurrentRunError(HerdaError):
    """Another run of the same graph currently holds the lock."""


class CleanupError(HerdaError):
    """A run cannot be cleaned up (active lock or dirty worktree)."""

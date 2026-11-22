"""Run directory persistence, atomic JSON writes and the per-graph lock."""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import CleanupError, ConcurrentRunError
from .naming import iso_utc

log = logging.getLogger("herda")


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def atomic_write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class RunStore:
    """Owns ``.herda/runs/<run_id>`` for one execution."""

    def __init__(self, herda_dir: Path, run_id: str) -> None:
        self.herda_dir = herda_dir
        self.run_id = run_id
        self.run_dir = herda_dir / "runs" / run_id
        self.run_json = self.run_dir / "run.json"
        self._prepare()

    def _prepare(self) -> None:
        for path in (
            self.herda_dir,
            self.herda_dir / "runs",
            self.run_dir,
            self.run_dir / "prompts",
            self.run_dir / "sessions",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            _chmod_private(path)

    def prompt_snapshot_path(self, stream: str, kernel: str) -> Path:
        path = self.run_dir / "prompts" / stream / f"{kernel}.md"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def session_dir(self, stream: str, kernel: str) -> Path:
        path = self.run_dir / "sessions" / stream / kernel
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def agent_session_dir(self, stream: str) -> Path:
        path = self.run_dir / "agent-sessions" / stream
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_private(path)
        return path

    def write_run(self, document: dict[str, Any]) -> None:
        atomic_write_json(self.run_json, document)

    def write_meta(
        self,
        stream: str,
        kernel: str,
        document: dict[str, Any],
    ) -> Path:
        path = self.session_dir(stream, kernel) / "meta.json"
        atomic_write_json(path, document)
        return path

    def write_screen(self, stream: str, kernel: str, text: str) -> Path:
        path = self.session_dir(stream, kernel) / "screen.txt"
        target = Path(path)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, target)
        return path

    def read_run(self) -> dict[str, Any] | None:
        if not self.run_json.exists():
            return None
        return read_json(self.run_json)


class GraphLock:
    """A non-blocking ``flock`` guard for one graph slug."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any | None = None

    def acquire(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod_private(self.path.parent)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ConcurrentRunError(
                    "concurrent_run",
                    f"another run of this graph is active ({self.path})",
                    "wait for it to finish or cancel it before starting again",
                ) from exc
            raise
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def lock_is_held(path: Path) -> bool:
    """Return True when another process holds the graph lock."""

    import fcntl

    if not path.exists():
        return False
    try:
        with path.open("a+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return True
                raise
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def acquire_cleanup_lock(path: Path) -> GraphLock:
    lock = GraphLock(path)
    try:
        lock.acquire()
    except ConcurrentRunError as exc:
        raise CleanupError(
            "active_run",
            "cannot clean a run while its graph lock is held",
            "wait for the run to finish or cancel it first",
        ) from exc
    return lock


def run_document(
    *,
    run_id: str,
    graph_name: str,
    graph_slug: str,
    repo: Path,
    state: str,
    started_at,
    finished_at,
    streams: dict[str, Any],
    kernels: dict[str, Any],
    branches: dict[str, str],
    agent: str | None = None,
) -> dict[str, Any]:
    return {
        "version": "0.1",
        "run_id": run_id,
        "graph": graph_name,
        "graph_slug": graph_slug,
        "repo": str(repo),
        "agent": agent,
        "state": state,
        "started_at": iso_utc(started_at),
        "finished_at": iso_utc(finished_at),
        "streams": streams,
        "kernels": kernels,
        "branches": branches,
    }


def kernel_document(result: Any, prompt_path: Path | None = None) -> dict[str, Any]:
    return {
        "name": result.name,
        "stream": result.stream,
        "state": result.state.value
        if hasattr(result.state, "value")
        else str(result.state),
        "model": result.model,
        "terminal": result.terminal,
        "branch": result.branch,
        "worktree": str(result.worktree),
        "prompt_sha256": result.prompt_sha256,
        "prompt_path": str(prompt_path) if prompt_path else None,
        "status": result.status,
        "started_at": iso_utc(result.started_at),
        "finished_at": iso_utc(result.finished_at),
        "duration_s": result.duration_s,
        "output_path": str(result.output_path) if result.output_path else None,
        "error": None
        if result.error is None
        else {
            "code": result.error.code,
            "message": result.error.message,
            "hint": result.error.hint,
        },
    }

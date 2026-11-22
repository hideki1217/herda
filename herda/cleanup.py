"""Cleanup of a finished (or abandoned) run."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from .backend import HcBackend
from .errors import CleanupError, HerdrError
from .store import (
    acquire_cleanup_lock,
    atomic_write_json,
    read_json,
)
from .worktree import (
    prune_worktrees,
    remove_engine_dir,
    remove_worktree,
    require_git_repo,
    worktree_is_dirty,
)

log = logging.getLogger("herda")


def cleanup_run(
    repo: str | Path,
    run_id: str,
    *,
    worktrees: bool = False,
    force: bool = False,
    backend: Any | None = None,
) -> None:
    root = require_git_repo(repo)
    herda_dir = root / ".herda"
    run_json = herda_dir / "runs" / run_id / "run.json"
    if not run_json.exists():
        raise CleanupError(
            "run_not_found",
            f"run {run_id!r} was not found under {herda_dir / 'runs'}",
            "run herda clean from the repository that owns the run",
        )
    try:
        document: dict[str, Any] = read_json(run_json)
    except (OSError, ValueError) as exc:
        raise CleanupError(
            "invalid_run_json",
            f"could not read {run_json}: {exc}",
        ) from exc

    graph_slug = str(document.get("graph_slug") or "")
    lock_path = herda_dir / "locks" / f"{graph_slug}.lock"
    lock = acquire_cleanup_lock(lock_path)
    try:
        terminals = [
            item.get("terminal")
            for item in (document.get("kernels") or {}).values()
            if isinstance(item, dict) and item.get("terminal")
        ]
        if terminals:
            cleanup_terminals([str(item) for item in terminals], backend=backend)

        if worktrees:
            cleanup_worktrees(document, repo=root, force=force)
            prune_worktrees(root)

        document["cleaned_at"] = _now_iso()
        document["worktrees_removed"] = bool(worktrees)
        try:
            atomic_write_json(run_json, document)
        except OSError as exc:
            log.warning("could not update %s: %s", run_json, exc)
    finally:
        lock.release()


def cleanup_terminals(
    addresses: list[str], *, backend: Any | None = None
) -> None:
    if backend is None:
        if shutil.which("hc") is None:
            message = "hc was not found in PATH; cannot clean up terminals"
            log.error("%s", message)
            raise HerdrError(
                "missing_dependency",
                message,
                "install herdr-core or remove the worktrees without terminal cleanup",
            )
        backend = HcBackend()
    for address in dict.fromkeys(addresses):
        try:
            info = backend.get(address)
        except HerdrError as exc:
            if exc.code in ("terminal_not_found", "hc_failed"):
                log.debug("terminal %s is already gone: %s", address, exc)
                continue
            raise
        try:
            if not info.finished:
                backend.kill(address)
            backend.rm(address)
        except HerdrError as exc:
            log.warning("could not remove terminal %s: %s", address, exc)


def cleanup_worktrees(
    document: dict[str, Any], *, repo: Path, force: bool
) -> None:
    repo = repo.resolve()
    owned_root = (repo / ".herda" / "worktrees").resolve()
    streams = document.get("streams") or {}
    for name, info in streams.items():
        if not isinstance(info, dict):
            continue
        raw_path = info.get("worktree")
        if not raw_path:
            continue
        path = Path(str(raw_path)).resolve()
        try:
            path.relative_to(owned_root)
        except ValueError:
            log.warning(
                "refusing to clean worktree outside %s: %s",
                owned_root,
                path,
            )
            continue
        if not path.exists():
            continue
        remove_engine_dir(path)
        if worktree_is_dirty(path) and not force:
            raise CleanupError(
                "dirty_worktree",
                f"worktree for stream {name!r} has uncommitted changes: {path}",
                "commit or discard the changes, or pass force=True",
            )
        remove_worktree(repo, path, force=force)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

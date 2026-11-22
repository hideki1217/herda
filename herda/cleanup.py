"""Cleanup of a finished (or abandoned) run."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from .backend import HcBackend
from .errors import CleanupError, HerdrError
from .store import (
    acquire_cleanup_lock,
    atomic_write_json,
    lock_is_held,
    read_json,
)
from .worktree import (
    delete_branch,
    prune_worktrees,
    remove_engine_dir,
    remove_worktree,
    require_run_repo,
    run_git,
    worktree_is_dirty,
)

log = logging.getLogger("herda")

_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
_GRAPH_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise CleanupError(
            "invalid_run_id",
            f"run_id {run_id!r} is not a valid herda run id",
            "use the run id printed by 'herda run'",
        )
    return run_id


def _validate_graph_slug(slug: Any) -> str:
    text = str(slug or "")
    if not _GRAPH_SLUG_RE.fullmatch(text):
        raise CleanupError(
            "invalid_run_json",
            f"run.json has an invalid graph_slug: {text!r}",
        )
    return text


def cleanup_run(
    repo: str | Path,
    run_id: str,
    *,
    worktrees: bool = False,
    branches: bool = False,
    force: bool = False,
    purge: bool = False,
    backend: Any | None = None,
) -> None:
    run_id = validate_run_id(run_id)
    root = require_run_repo(repo)
    herda_dir = root / ".herda"
    run_json = herda_dir / "runs" / run_id / "run.json"
    run_dir = run_json.parent
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
    if not isinstance(document, dict):
        raise CleanupError(
            "invalid_run_json",
            f"{run_json} must contain a JSON object",
        )

    if str(document.get("run_id") or "") != run_id:
        raise CleanupError(
            "run_mismatch",
            f"run.json in {run_json} does not describe run {run_id!r}",
        )

    graph_slug = _validate_graph_slug(document.get("graph_slug"))
    lock_path = herda_dir / "locks" / f"{graph_slug}.lock"
    lock = acquire_cleanup_lock(lock_path)
    try:
        kernels = document.get("kernels", {})
        streams = document.get("streams", {})
        if not isinstance(kernels, dict) or not isinstance(streams, dict):
            raise CleanupError(
                "invalid_run_json",
                "run.json kernels/streams must be objects",
            )
        document["streams"] = streams
        terminals = [
            item.get("terminal")
            for item in kernels.values()
            if isinstance(item, dict) and item.get("terminal")
        ]
        terminals_ok = True
        try:
            cleanup_terminals(
                [str(item) for item in terminals],
                run_id=run_id,
                backend=backend,
            )
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            # Worktree cleanup is still useful when the terminal backend is
            # unavailable; keep the failure visible in the log.
            terminals_ok = False
            log.warning("could not clean terminals for %s: %s", run_id, exc)

        if worktrees:
            cleanup_worktrees(
                document,
                repo=root,
                force=force,
                run_id=run_id,
                graph_slug=graph_slug,
            )
            prune_worktrees(root)

        if branches:
            _cleanup_branches(
                document,
                repo=root,
                graph_slug=graph_slug,
                run_id=run_id,
            )

        if purge:
            remaining = [
                str(info.get("worktree"))
                for info in streams.values()
                if isinstance(info, dict)
                and info.get("worktree")
                and Path(str(info["worktree"])).exists()
            ]
            if remaining:
                raise CleanupError(
                    "worktrees_remaining",
                    "cannot purge run metadata while worktrees remain: "
                    + ", ".join(remaining),
                    "run with worktrees=True (and force=True if dirty) first",
                )

        if terminals_ok:
            for item in kernels.values():
                if isinstance(item, dict):
                    item["terminal"] = None
        document["terminals_cleaned"] = bool(
            document.get("terminals_cleaned")
        ) or terminals_ok
        document["cleaned_at"] = _now_iso()
        document["worktrees_removed"] = bool(
            document.get("worktrees_removed")
        ) or bool(worktrees)
        document["branches_removed"] = bool(
            document.get("branches_removed")
        ) or bool(branches)
        written = True
        try:
            atomic_write_json(run_json, document)
        except Exception as exc:  # noqa: BLE001 - best effort
            written = False
            log.warning("could not update %s: %s", run_json, exc)
        if purge and written:
            try:
                shutil.rmtree(run_dir)
            except OSError as exc:
                log.warning("could not purge %s: %s", run_dir, exc)
    finally:
        lock.release()


def cleanup_terminals(
    addresses: list[str],
    *,
    run_id: str | None = None,
    backend: Any | None = None,
) -> None:
    if backend is None:
        backend = HcBackend()
        hc = getattr(backend, "hc", "hc")
        if shutil.which(hc) is None:
            if not addresses:
                return
            message = "hc was not found in PATH; cannot clean up terminals"
            log.error("%s", message)
            raise HerdrError(
                "missing_dependency",
                message,
                "install herdr-core or remove the worktrees without terminal cleanup",
            )

    candidates = list(dict.fromkeys(addresses))
    if run_id:
        lister = getattr(backend, "list", None)
        if lister is not None:
            try:
                discovered = lister(
                    labels={"herda.owner": "herda", "herda.run": run_id}
                )
            except HerdrError as exc:
                log.debug("could not list terminals for %s: %s", run_id, exc)
            else:
                for info in discovered:
                    if info.address not in candidates:
                        candidates.append(info.address)

    for address in candidates:
        try:
            info = backend.get(address)
        except HerdrError as exc:
            if exc.code in ("terminal_not_found", "hc_failed"):
                log.debug("terminal %s is already gone: %s", address, exc)
                continue
            raise
        labels = getattr(info, "labels", {}) or {}
        if run_id is not None:
            if labels.get("herda.owner") != "herda":
                log.warning(
                    "refusing to remove terminal %s: owner is not herda",
                    address,
                )
                continue
            if labels.get("herda.run") != run_id:
                log.warning(
                    "refusing to remove terminal %s: run label does not "
                    "match %s",
                    address,
                    run_id,
                )
                continue
        try:
            if not info.finished:
                backend.kill(address)
            backend.rm(address)
        except HerdrError as exc:
            log.warning("could not remove terminal %s: %s", address, exc)


def _cleanup_branches(
    document: dict[str, Any],
    *,
    repo: Path,
    graph_slug: str,
    run_id: str,
) -> None:
    prefix = f"herda/{graph_slug}/"
    suffix = f"-{run_id[-6:]}"
    for name, info in (document.get("streams") or {}).items():
        if not isinstance(info, dict):
            continue
        branch = str(info.get("branch") or "")
        if not branch.startswith(prefix) or not branch.endswith(suffix):
            if branch:
                log.warning(
                    "refusing to delete branch outside %s*%s: %s",
                    prefix,
                    suffix,
                    branch,
                )
            continue
        try:
            proc = delete_branch(repo, branch)
        except Exception as exc:  # noqa: BLE001 - cleanup is best effort
            log.warning(
                "could not delete branch %s for stream %s: %s",
                branch,
                name,
                exc,
            )
            continue
        if getattr(proc, "returncode", 0) != 0:
            log.warning(
                "could not delete branch %s for stream %s",
                branch,
                name,
            )


def cleanup_worktrees(
    document: dict[str, Any],
    *,
    repo: Path,
    force: bool,
    run_id: str,
    graph_slug: str,
) -> None:
    repo = repo.resolve()
    expected_root = (
        repo / ".herda" / "worktrees" / graph_slug / run_id
    ).resolve()
    streams = document.get("streams") or {}
    for name, info in streams.items():
        if not isinstance(info, dict):
            continue
        raw_path = info.get("worktree")
        if not raw_path:
            continue
        path = Path(str(raw_path)).resolve()
        try:
            path.relative_to(expected_root)
        except ValueError:
            log.warning(
                "refusing to clean worktree outside %s: %s",
                expected_root,
                path,
            )
            continue
        if not path.exists():
            continue
        if worktree_is_dirty(path) and not force:
            raise CleanupError(
                "dirty_worktree",
                f"worktree for stream {name!r} has uncommitted changes: {path}",
                "commit or discard the changes, or pass force=True",
            )
        remove_engine_dir(path)
        remove_worktree(repo, path, force=force)


def cleanup_orphans(
    repo: str | Path,
    *,
    force: bool = False,
    branches: bool = False,
    backend: Any | None = None,
) -> tuple[int, int]:
    """Remove worktrees/branches that have no matching run.json.

    A worktree is orphaned when it lives under
    ``.herda/worktrees/<graph_slug>/<run_id>/`` and ``runs/<run_id>/run.json``
    is missing. A graph lock held for the graph slug is treated as an active
    run and skipped. Branches are orphaned when no run directory has the
    branch's ``run6`` suffix.
    """

    root = require_run_repo(repo)
    herda_dir = root / ".herda"
    runs_dir = herda_dir / "runs"
    owned_root = (herda_dir / "worktrees").resolve()
    locks_dir = herda_dir / "locks"

    worktrees: list[Path] = []
    if owned_root.exists():
        proc = run_git(root, "worktree", "list", "--porcelain")
        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                worktrees.append(
                    Path(line[len("worktree "):]).resolve()
                )

    removed_worktrees = 0
    for path in worktrees:
        try:
            relative = path.relative_to(owned_root)
        except ValueError:
            continue
        parts = relative.parts
        if len(parts) != 3:
            log.warning("ignoring unexpected orphan path shape: %s", path)
            continue
        graph_slug, run_id, _stream_slug = parts
        if not _GRAPH_SLUG_RE.fullmatch(graph_slug):
            log.warning("ignoring orphan with invalid graph slug: %s", path)
            continue
        if not _RUN_ID_RE.fullmatch(run_id):
            log.warning("ignoring orphan with invalid run id: %s", path)
            continue
        if (runs_dir / run_id / "run.json").exists():
            continue
        if lock_is_held(locks_dir / f"{graph_slug}.lock"):
            log.info("skipping orphan worktree with active lock: %s", path)
            continue
        if worktree_is_dirty(path) and not force:
            log.warning(
                "skipping dirty orphan worktree (use --force): %s", path
            )
            continue
        try:
            cleanup_terminals([], run_id=run_id, backend=backend)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("could not clean orphan terminals for %s: %s", run_id, exc)
        remove_engine_dir(path)
        remove_worktree(root, path, force=force)
        removed_worktrees += 1
        log.info("removed orphan worktree: %s", path)

    removed_branches = 0
    if branches:
        branch_list = run_git(
            root,
            "branch",
            "--format=%(refname:short)",
            "--list",
            "herda/*",
        ).stdout.splitlines()
        for branch in branch_list:
            branch = branch.strip()
            if not branch.startswith("herda/"):
                continue
            tail = branch[len("herda/"):]
            if "/" not in tail:
                continue
            graph_slug, rest = tail.split("/", 1)
            if not _GRAPH_SLUG_RE.fullmatch(graph_slug):
                continue
            if "-" not in rest:
                continue
            _stream_slug, run6 = rest.rsplit("-", 1)
            if not re.fullmatch(r"[0-9a-f]{6}", run6):
                continue
            if lock_is_held(locks_dir / f"{graph_slug}.lock"):
                log.info(
                    "skipping orphan branch with active lock: %s", branch
                )
                continue
            if any(
                (path / "run.json").exists()
                for path in runs_dir.glob(f"*-{run6}")
            ):
                continue
            try:
                result = delete_branch(root, branch)
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning(
                    "could not delete orphan branch %s: %s", branch, exc
                )
                continue
            if getattr(result, "returncode", 0) == 0:
                removed_branches += 1
                log.info("removed orphan branch: %s", branch)
            else:
                log.warning("could not delete orphan branch: %s", branch)

    prune_worktrees(root)
    return (removed_worktrees, removed_branches)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

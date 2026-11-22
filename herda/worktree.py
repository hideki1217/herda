"""Thin, explicit wrappers around the git commands herda needs."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from .errors import WorktreeError

log = logging.getLogger("herda")


def require_git() -> str:
    git = shutil.which("git")
    if git is None:
        message = "git was not found in PATH"
        log.error("%s; herda needs git 2.30 or newer", message)
        raise WorktreeError(
            "missing_dependency",
            message,
            "install git and make sure the 'git' executable is on PATH",
        )
    return git


def run_git(
    repo: str | Path,
    *args: str,
    check: bool = True,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    git = require_git()
    try:
        proc = subprocess.run(
            [git, "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(
            "git_timeout",
            f"git {' '.join(args)} timed out after {timeout:g}s",
        ) from exc
    except OSError as exc:
        raise WorktreeError(
            "git_failed",
            f"could not run git: {exc}",
            "check that git is installed and executable",
        ) from exc

    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorktreeError(
            "git_failed",
            f"git {' '.join(args)} failed with exit code {proc.returncode}: {detail}",
        )
    return proc


def git_version() -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or proc.stderr).strip() or None


def is_git_repo(repo: str | Path) -> bool:
    path = Path(repo)
    if not path.exists():
        return False
    proc = run_git(path, "rev-parse", "--is-inside-work-tree", check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def require_git_repo(repo: str | Path) -> Path:
    path = Path(repo).expanduser().resolve()
    if not is_git_repo(path):
        raise WorktreeError(
            "not_a_git_repo",
            f"{path} is not a git working tree",
            "pass Graph(repo=...) pointing at the root of a git repository",
        )
    return path


def resolve_commit(repo: str | Path, ref: str) -> str:
    if not ref:
        raise WorktreeError("invalid_base", "stream base must not be empty")
    proc = run_git(
        repo,
        "rev-parse",
        "--verify",
        f"{ref}^{{commit}}",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorktreeError(
            "invalid_base",
            f"cannot resolve base {ref!r} in {repo}: {detail}",
            "use a branch, tag or commit that exists in the repository",
        )
    return proc.stdout.strip()


def branch_exists(repo: str | Path, branch: str) -> bool:
    proc = run_git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        check=False,
    )
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorktreeError(
            "git_failed", f"could not inspect branch {branch!r}: {detail}"
        )
    return proc.returncode == 0


def delete_branch(repo: str | Path, branch: str) -> subprocess.CompletedProcess[str]:
    return run_git(repo, "branch", "-D", branch, check=False)


def create_worktree(
    repo: str | Path,
    path: str | Path,
    branch: str,
    commit: str,
) -> Path:
    target = Path(path)
    if target.exists():
        raise WorktreeError(
            "worktree_exists",
            f"worktree path already exists: {target}",
            "herda never adopts an existing worktree; remove it or use a new run",
        )
    if branch_exists(repo, branch):
        raise WorktreeError(
            "branch_exists",
            f"branch already exists: {branch}",
            "herda never reuses an existing branch; use a new run",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_git(repo, "worktree", "add", "-b", branch, str(target), commit)
    except Exception:
        # `git worktree add -b` can create the branch before failing to add
        # the working tree. Roll that branch back; the caller only tracks
        # worktrees that succeeded.
        if branch_exists(repo, branch):
            delete_branch(repo, branch)
        raise
    return target


def remove_worktree(
    repo: str | Path,
    path: str | Path,
    *,
    force: bool = False,
) -> None:
    target = Path(path)
    if not target.exists():
        run_git(repo, "worktree", "prune", check=False)
        return
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    proc = run_git(repo, *args, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorktreeError(
            "worktree_remove_failed",
            f"could not remove worktree {target}: {detail}",
            "use cleanup(worktrees=True, force=True) to discard local changes",
        )


def worktree_is_dirty(path: str | Path) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    proc = run_git(
        target,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorktreeError(
            "git_failed", f"could not inspect worktree {target}: {detail}"
        )
    return bool(proc.stdout.strip())


def prune_worktrees(repo: str | Path) -> None:
    run_git(repo, "worktree", "prune", check=False)


def ensure_main_exclude(repo: str | Path) -> None:
    """Make sure the main repository ignores herda's own ``.herda/`` tree."""

    proc = run_git(repo, "rev-parse", "--git-path", "info/exclude", check=False)
    if proc.returncode != 0:
        return
    exclude = Path(proc.stdout.strip())
    if not exclude.is_absolute():
        exclude = Path(repo) / exclude
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if ".herda/" not in lines:
            with exclude.open("a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(".herda/\n")
    except OSError as exc:
        log.warning("could not update %s: %s", exclude, exc)


def init_worktree_herda(worktree: str | Path) -> Path:
    """Create the engine-owned, self-ignoring ``.herda`` directory."""

    engine = Path(worktree) / ".herda"
    try:
        engine.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(engine, 0o700)
    except OSError as exc:
        raise WorktreeError(
            "worktree_init_failed",
            f"could not create {engine}: {exc}",
        ) from exc
    ignore = engine / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return engine


def remove_engine_dir(worktree: str | Path) -> None:
    engine = Path(worktree) / ".herda"
    if not engine.exists():
        return
    try:
        shutil.rmtree(engine)
    except OSError as exc:
        raise WorktreeError(
            "cleanup_failed",
            f"could not remove {engine}: {exc}",
        ) from exc


def write_large_prompt(worktree: str | Path, stream: str, kernel: str, text: str) -> Path:
    target = (
        Path(worktree)
        / ".herda"
        / "prompts"
        / stream
        / f"{kernel}.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(text, encoding="utf-8")
    return target

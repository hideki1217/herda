"""Branch coverage for cleanup.py."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from herda import CleanupError, HerdrError
from herda.backend import TerminalInfo
from herda.cleanup import (
    cleanup_run,
    cleanup_terminals,
    cleanup_worktrees,
)

from .test_herda import RepoCase


class FakeCleanupBackend:
    def __init__(
        self,
        *,
        get_error: Exception | None = None,
        info: TerminalInfo | None = None,
        kill_error: Exception | None = None,
        rm_error: Exception | None = None,
    ) -> None:
        self.get_error = get_error
        self.info = info or TerminalInfo("t1", "live", "idle", 0, True)
        self.kill_error = kill_error
        self.rm_error = rm_error
        self.killed: list[str] = []
        self.rm_calls: list[str] = []

    def get(self, terminal: str) -> TerminalInfo:
        if self.get_error is not None:
            raise self.get_error
        return self.info

    def kill(self, terminal: str) -> None:
        self.killed.append(terminal)
        if self.kill_error is not None:
            raise self.kill_error

    def rm(self, terminal: str) -> None:
        self.rm_calls.append(terminal)
        if self.rm_error is not None:
            raise self.rm_error


class CleanupTests(RepoCase):
    def test_run_not_found_and_invalid_json(self) -> None:
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, "missing")
        self.assertEqual(caught.exception.code, "run_not_found")

        run_dir = Path(self.repo) / ".herda" / "runs" / "bad"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, "bad")
        self.assertEqual(caught.exception.code, "invalid_run_json")

    def test_update_warning_and_worktree_hooks(self) -> None:
        run_dir = Path(self.repo) / ".herda" / "runs" / "r1"
        run_dir.mkdir(parents=True)
        document = {
            "graph_slug": "g",
            "repo": self.repo,
            "kernels": {},
            "streams": {},
        }
        (run_dir / "run.json").write_text(json.dumps(document), encoding="utf-8")
        with mock.patch(
            "herda.cleanup.atomic_write_json", side_effect=OSError("no")
        ):
            with self.assertLogs("herda", level="WARNING"):
                cleanup_run(self.repo, "r1")

        with mock.patch(
            "herda.cleanup.cleanup_worktrees"
        ) as cleanup, mock.patch("herda.cleanup.prune_worktrees") as prune:
            cleanup_run(self.repo, "r1", worktrees=True)
        cleanup.assert_called_once()
        prune.assert_called_once()

    def test_cleanup_terminals(self) -> None:
        with mock.patch("herda.cleanup.shutil.which", return_value=None):
            with self.assertRaises(HerdrError) as caught:
                cleanup_terminals(["t1"])
        self.assertEqual(caught.exception.code, "missing_dependency")

        fake = FakeCleanupBackend()
        with mock.patch("herda.cleanup.shutil.which", return_value="hc"), mock.patch(
            "herda.cleanup.HcBackend", return_value=fake
        ):
            cleanup_terminals(["t1"])
        self.assertEqual(fake.killed, ["t1"])
        self.assertEqual(fake.rm_calls, ["t1"])

        finished = FakeCleanupBackend(
            info=TerminalInfo("t1", "finished", None, 0, False)
        )
        cleanup_terminals(["t1"], backend=finished)
        self.assertEqual(finished.killed, [])

        for error in (
            HerdrError("terminal_not_found", "gone"),
            HerdrError("hc_failed", "bad"),
        ):
            cleanup_terminals(["t1"], backend=FakeCleanupBackend(get_error=error))
        with self.assertRaises(HerdrError):
            cleanup_terminals(
                ["t1"], backend=FakeCleanupBackend(get_error=HerdrError("x", "x"))
            )

        with self.assertLogs("herda", level="WARNING"):
            cleanup_terminals(
                ["t1"],
                backend=FakeCleanupBackend(
                    kill_error=HerdrError("kill", "no"),
                    rm_error=HerdrError("rm", "no"),
                ),
            )

    def test_cleanup_worktrees_branches(self) -> None:
        owned = Path(self.repo) / ".herda" / "worktrees" / "g" / "r" / "s"
        owned.mkdir(parents=True)
        outside = Path(self.root) / "outside"
        outside.mkdir()
        document = {
            "streams": {
                "not-dict": "x",
                "no-path": {},
                "outside": {"worktree": str(outside)},
                "missing": {
                    "worktree": str(
                        Path(self.repo) / ".herda" / "worktrees" / "missing"
                    )
                },
                "ok": {"worktree": str(owned)},
            }
        }
        with mock.patch(
            "herda.cleanup.remove_engine_dir"
        ) as remove_engine, mock.patch(
            "herda.cleanup.worktree_is_dirty", return_value=False
        ), mock.patch(
            "herda.cleanup.remove_worktree"
        ) as remove:
            cleanup_worktrees(document, repo=Path(self.repo), force=False)
        remove_engine.assert_called_once_with(owned)
        remove.assert_called_once_with(Path(self.repo), owned, force=False)

        with mock.patch("herda.cleanup.remove_engine_dir"), mock.patch(
            "herda.cleanup.worktree_is_dirty", return_value=True
        ), mock.patch("herda.cleanup.remove_worktree") as remove:
            with self.assertRaises(CleanupError):
                cleanup_worktrees(
                    {"streams": {"ok": {"worktree": str(owned)}}},
                    repo=Path(self.repo),
                    force=False,
                )
            remove.assert_not_called()
            cleanup_worktrees(
                {"streams": {"ok": {"worktree": str(owned)}}},
                repo=Path(self.repo),
                force=True,
            )
            remove.assert_called_once_with(
                Path(self.repo), owned, force=True
            )

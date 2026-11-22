"""Branch coverage for cleanup.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from herda import CleanupError, HerdrError
from herda.backend import TerminalInfo
from herda.cleanup import (
    cleanup_orphans,
    cleanup_run,
    cleanup_terminals,
    cleanup_worktrees,
)

from .test_herda import RepoCase, _git

RUN = "20260101T000000Z-abcdef"
RUN_MISSING = "20260101T000003Z-abcdef"
RUN_BADJSON = "20260101T000004Z-abcdef"
RUN_MISMATCH = "20260101T000001Z-abcdef"
RUN_UPDATE = "20260101T000002Z-abcdef"
RUN_BADSLUG = "20260101T000005Z-abcdef"
RUN_TERMWARN = "20260101T000006Z-abcdef"
RUN_BRANCH = "20260101T000007Z-abcdef"
RUN_SHAPES = "20260101T000008Z-abcdef"
RUN_ORPHAN = "20260101T000010Z-abcdef"
RUN_EXISTING = "20260101T000011Z-123456"
RUN_ORPHAN2 = "20260101T000012Z-abcdef"


class FakeCleanupBackend:
    def __init__(
        self,
        *,
        get_error: Exception | None = None,
        info: TerminalInfo | None = None,
        kill_error: Exception | None = None,
        rm_error: Exception | None = None,
        listed: list[TerminalInfo] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.get_error = get_error
        self.info = info or TerminalInfo("t1", "live", "idle", 0, True)
        self.kill_error = kill_error
        self.rm_error = rm_error
        self.listed = list(listed or [])
        self.list_error = list_error
        self.killed: list[str] = []
        self.rm_calls: list[str] = []

    def list(self, labels=None) -> list[TerminalInfo]:
        if self.list_error is not None:
            raise self.list_error
        return list(self.listed)

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
        self.assertEqual(caught.exception.code, "invalid_run_id")

        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, RUN_MISSING)
        self.assertEqual(caught.exception.code, "run_not_found")

        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_BADJSON
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, RUN_BADJSON)
        self.assertEqual(caught.exception.code, "invalid_run_json")

        (run_dir / "run.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, RUN_BADJSON)
        self.assertEqual(caught.exception.code, "invalid_run_json")

    def test_invalid_run_id_and_mismatched_document(self) -> None:
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, "../" + RUN)
        self.assertEqual(caught.exception.code, "invalid_run_id")

        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_MISMATCH
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"run_id": "other", "graph_slug": "g"}),
            encoding="utf-8",
        )
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, RUN_MISMATCH)
        self.assertEqual(caught.exception.code, "run_mismatch")

    def test_invalid_graph_slug_and_terminal_cleanup_warning(self) -> None:
        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_BADSLUG
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {"run_id": RUN_BADSLUG, "graph_slug": "../bad"}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, RUN_BADSLUG)
        self.assertEqual(caught.exception.code, "invalid_run_json")

        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_TERMWARN
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_TERMWARN,
                    "graph_slug": "g",
                    "kernels": {},
                    "streams": {},
                }
            ),
            encoding="utf-8",
        )
        with mock.patch(
            "herda.cleanup.cleanup_terminals",
            side_effect=HerdrError("x", "no"),
        ), self.assertLogs("herda", level="WARNING"):
            cleanup_run(
                self.repo, RUN_TERMWARN, backend=FakeCleanupBackend()
            )

    def test_invalid_document_shapes(self) -> None:
        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_SHAPES
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_SHAPES,
                    "graph_slug": "g",
                    "kernels": [],
                    "streams": {},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(CleanupError) as caught:
            cleanup_run(self.repo, RUN_SHAPES)
        self.assertEqual(caught.exception.code, "invalid_run_json")

    def test_update_warning_and_worktree_hooks(self) -> None:
        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_UPDATE
        run_dir.mkdir(parents=True)
        document = {
            "run_id": RUN_UPDATE,
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
                cleanup_run(
                    self.repo, RUN_UPDATE, backend=FakeCleanupBackend()
                )

        with mock.patch(
            "herda.cleanup.cleanup_worktrees"
        ) as cleanup, mock.patch("herda.cleanup.prune_worktrees") as prune:
            cleanup_run(
                self.repo,
                RUN_UPDATE,
                worktrees=True,
                backend=FakeCleanupBackend(),
            )
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

    def test_cleanup_terminal_discovery_and_labels(self) -> None:
        discovered = TerminalInfo(
            "t2",
            "live",
            "idle",
            0,
            True,
            labels={"herda.owner": "herda", "herda.run": RUN},
        )
        fake = FakeCleanupBackend(info=discovered, listed=[discovered])
        cleanup_terminals([], run_id=RUN, backend=fake)
        self.assertEqual(fake.killed, ["t2"])
        self.assertEqual(fake.rm_calls, ["t2"])

        list_error = FakeCleanupBackend(
            listed=[], list_error=HerdrError("hc_failed", "no")
        )
        cleanup_terminals(["t1"], run_id=RUN, backend=list_error)
        self.assertEqual(list_error.killed, [])

        owner_mismatch = FakeCleanupBackend(
            info=TerminalInfo(
                "t1", "live", "idle", 0, True, labels={"herda.run": RUN}
            )
        )
        with self.assertLogs("herda", level="WARNING"):
            cleanup_terminals(["t1"], run_id=RUN, backend=owner_mismatch)
        self.assertEqual(owner_mismatch.killed, [])

        run_mismatch = FakeCleanupBackend(
            info=TerminalInfo(
                "t1",
                "live",
                "idle",
                0,
                True,
                labels={"herda.owner": "herda", "herda.run": "other"},
            )
        )
        with self.assertLogs("herda", level="WARNING"):
            cleanup_terminals(["t1"], run_id=RUN, backend=run_mismatch)
        self.assertEqual(run_mismatch.killed, [])

        with mock.patch("herda.cleanup.shutil.which", return_value=None):
            cleanup_terminals([], run_id=RUN)

    def test_cleanup_orphans(self) -> None:
        self.assertEqual(cleanup_orphans(self.repo), (0, 0))

        orphan_wt = (
            Path(self.repo)
            / ".herda"
            / "worktrees"
            / "g"
            / RUN_ORPHAN
            / "s"
        )
        orphan_wt.parent.mkdir(parents=True)
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "herda/g/s-abcdef",
            str(orphan_wt),
            "main",
        )
        _git(self.repo, "branch", "herda/g/other-deadbe", "main")

        existing_run = (
            Path(self.repo) / ".herda" / "runs" / RUN_EXISTING
        )
        existing_run.mkdir(parents=True)
        (existing_run / "run.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_EXISTING,
                    "graph_slug": "g",
                    "kernels": {},
                    "streams": {},
                }
            ),
            encoding="utf-8",
        )
        _git(self.repo, "branch", "herda/g/s-123456", "main")

        removed = cleanup_orphans(
            self.repo, branches=True, backend=FakeCleanupBackend()
        )
        self.assertEqual(removed, (1, 2))
        self.assertFalse(orphan_wt.exists())
        branches = _git(
            self.repo, "branch", "--format=%(refname:short)", "--list", "herda/*"
        ).stdout.splitlines()
        self.assertIn("herda/g/s-123456", branches)
        self.assertNotIn("herda/g/other-deadbe", branches)

    def test_cleanup_orphans_dirty_and_locked(self) -> None:
        orphan_wt = (
            Path(self.repo)
            / ".herda"
            / "worktrees"
            / "g"
            / RUN_ORPHAN
            / "s"
        )
        orphan_wt.parent.mkdir(parents=True)
        _git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "herda/g/s-abcdef",
            str(orphan_wt),
            "main",
        )
        (orphan_wt / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertLogs("herda", level="WARNING"):
            self.assertEqual(cleanup_orphans(self.repo), (0, 0))
        self.assertTrue(orphan_wt.exists())

        with mock.patch("herda.cleanup.lock_is_held", return_value=True):
            self.assertEqual(cleanup_orphans(self.repo, force=True), (0, 0))
        self.assertTrue(orphan_wt.exists())

        self.assertEqual(
            cleanup_orphans(
                self.repo,
                force=True,
                branches=True,
                backend=FakeCleanupBackend(),
            ),
            (1, 1),
        )

    def test_cleanup_orphans_invalid_paths_and_terminal_errors(self) -> None:
        wt_root = Path(self.repo) / ".herda" / "worktrees"
        wt_root.mkdir(parents=True)
        existing_run = Path(self.repo) / ".herda" / "runs" / RUN_EXISTING
        existing_run.mkdir(parents=True)
        (existing_run / "run.json").write_text("{}", encoding="utf-8")

        paths = [
            wt_root / "g" / RUN_ORPHAN / "s" / "extra",
            wt_root / "BAD!" / RUN_ORPHAN / "s",
            wt_root / "g" / "bad" / "s",
            wt_root / "g" / RUN_EXISTING / "s",
            Path(self.root) / "outside-wt",
            wt_root / "g" / RUN_ORPHAN2 / "s",
        ]
        worktree_out = "\n".join(f"worktree {p}" for p in paths) + "\n"
        with mock.patch(
            "herda.cleanup.run_git",
            side_effect=[
                SimpleNamespace(stdout=worktree_out),
                SimpleNamespace(stdout=""),
            ],
        ), mock.patch("herda.cleanup.prune_worktrees"), mock.patch(
            "herda.cleanup.worktree_is_dirty", return_value=False
        ), mock.patch(
            "herda.cleanup.remove_engine_dir"
        ) as remove_engine, mock.patch(
            "herda.cleanup.remove_worktree"
        ) as remove_worktree, mock.patch(
            "herda.cleanup.cleanup_terminals", side_effect=RuntimeError("no")
        ), self.assertLogs("herda", level="WARNING"):
            self.assertEqual(cleanup_orphans(self.repo), (1, 0))
        remove_engine.assert_called_once()
        remove_worktree.assert_called_once()

    def test_cleanup_orphans_branch_errors(self) -> None:
        wt_root = Path(self.repo) / ".herda" / "worktrees"
        wt_root.mkdir(parents=True)
        run_dir = Path(self.repo) / ".herda" / "runs" / "x-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("{}", encoding="utf-8")
        branch_out = "\n".join(
            [
                "not-herda",
                "herda/noslash",
                "herda/BAD!/x-abcdef",
                "herda/g/nodash",
                "herda/g/x-nothex",
                "herda/g/x-abc123",
                "herda/g/x-e00123",
                "herda/g/x-f00123",
                "herda/g/x-abc999",
            ]
        ) + "\n"

        def delete(repo, branch):
            if branch.endswith("e00123"):
                raise RuntimeError("no")
            if branch.endswith("f00123"):
                return SimpleNamespace(returncode=1)
            return SimpleNamespace(returncode=0)

        with mock.patch(
            "herda.cleanup.run_git",
            side_effect=[
                SimpleNamespace(stdout=""),
                SimpleNamespace(stdout=branch_out),
            ],
        ), mock.patch("herda.cleanup.prune_worktrees"), mock.patch(
            "herda.cleanup.lock_is_held", return_value=False
        ), mock.patch(
            "herda.cleanup.delete_branch", side_effect=delete
        ) as delete_branch, self.assertLogs("herda", level="WARNING"):
            self.assertEqual(
                cleanup_orphans(self.repo, branches=True), (0, 1)
            )
        self.assertEqual(delete_branch.call_count, 3)

        with mock.patch(
            "herda.cleanup.run_git",
            side_effect=[
                SimpleNamespace(stdout=""),
                SimpleNamespace(stdout="herda/g/x-abc999\n"),
            ],
        ), mock.patch("herda.cleanup.prune_worktrees"), mock.patch(
            "herda.cleanup.lock_is_held", return_value=True
        ), mock.patch(
            "herda.cleanup.delete_branch"
        ) as delete_branch:
            self.assertEqual(
                cleanup_orphans(self.repo, branches=True), (0, 0)
            )
        delete_branch.assert_not_called()

    def test_cleanup_branches(self) -> None:
        run_dir = Path(self.repo) / ".herda" / "runs" / RUN_BRANCH
        run_dir.mkdir(parents=True)
        document = {
            "run_id": RUN_BRANCH,
            "graph_slug": "g",
            "kernels": {},
            "streams": {
                "ok": {"branch": "herda/g/ok-abcdef"},
                "bad": {"branch": "main"},
                "not-dict": "x",
            },
        }
        (run_dir / "run.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        with mock.patch(
            "herda.cleanup.delete_branch",
            return_value=SimpleNamespace(returncode=0),
        ) as delete:
            cleanup_run(
                self.repo,
                RUN_BRANCH,
                branches=True,
                backend=FakeCleanupBackend(),
            )
        delete.assert_called_once_with(Path(self.repo), "herda/g/ok-abcdef")

        with mock.patch(
            "herda.cleanup.delete_branch",
            return_value=SimpleNamespace(returncode=1),
        ), self.assertLogs("herda", level="WARNING"):
            cleanup_run(
                self.repo,
                RUN_BRANCH,
                branches=True,
                backend=FakeCleanupBackend(),
            )

        with mock.patch(
            "herda.cleanup.delete_branch",
            side_effect=RuntimeError("no"),
        ), self.assertLogs("herda", level="WARNING"):
            cleanup_run(
                self.repo,
                RUN_BRANCH,
                branches=True,
                backend=FakeCleanupBackend(),
            )

    def test_cleanup_worktree_branches(self) -> None:
        owned = Path(self.repo) / ".herda" / "worktrees" / "g" / RUN / "s"
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
                "missing2": {
                    "worktree": str(
                        Path(self.repo)
                        / ".herda"
                        / "worktrees"
                        / "g"
                        / RUN
                        / "absent"
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
            cleanup_worktrees(
                document,
                repo=Path(self.repo),
                force=False,
                run_id=RUN,
                graph_slug="g",
            )
        remove_engine.assert_called_once_with(owned)
        remove.assert_called_once_with(Path(self.repo), owned, force=False)

        with mock.patch(
            "herda.cleanup.remove_engine_dir"
        ) as remove_engine2, mock.patch(
            "herda.cleanup.worktree_is_dirty", return_value=True
        ), mock.patch("herda.cleanup.remove_worktree") as remove:
            with self.assertRaises(CleanupError):
                cleanup_worktrees(
                    {"streams": {"ok": {"worktree": str(owned)}}},
                    repo=Path(self.repo),
                    force=False,
                    run_id=RUN,
                    graph_slug="g",
                )
            remove.assert_not_called()
            remove_engine2.assert_not_called()
            cleanup_worktrees(
                {"streams": {"ok": {"worktree": str(owned)}}},
                repo=Path(self.repo),
                force=True,
                run_id=RUN,
                graph_slug="g",
            )
            remove.assert_called_once_with(
                Path(self.repo), owned, force=True
            )

"""Unit tests for the small, mostly pure helpers."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from herda import (
    CleanupError,
    ConcurrentRunError,
    HerdaError,
    PromptError,
    ValidationError,
    WorktreeError,
)
from herda import app
from herda.mermaid import _escape, _short_model, to_mermaid
from herda.naming import (
    iso_utc,
    mermaid_id,
    parse_iso_utc,
    short_hash,
    slugify,
    space_id,
    tab_id,
    terminal_name,
    validate_name,
)
from herda.prompt import load_prompt, prompt_sha256, resolve_prompt
from herda.store import (
    GraphLock,
    RunStore,
    acquire_cleanup_lock,
    atomic_write_json,
    kernel_document,
    lock_is_held,
    read_json,
    run_document,
)
from herda import worktree

from .test_herda import RepoCase


class ErrorTests(unittest.TestCase):
    def test_str_and_repr(self) -> None:
        error = HerdaError("code", "message", "hint")
        self.assertEqual(str(error), "code: message (hint: hint)")
        self.assertIn("HerdaError", repr(error))
        self.assertEqual(str(HerdaError("code", "message")), "code: message")


class NamingTests(unittest.TestCase):
    def test_validate_name_errors(self) -> None:
        for value in (
            1,
            "",
            "x" * 65,
            "a\nb",
            "a/b",
            ".",
            "..",
        ):
            with self.assertRaises(ValidationError):
                validate_name("Thing", value)
        self.assertEqual(validate_name("Thing", "ok"), "ok")

    def test_slug_and_ids(self) -> None:
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertTrue(slugify("日本語").startswith("x-"))
        self.assertEqual(short_hash("abc", 6), "ba7816")
        self.assertEqual(
            space_id("g", "/repo", "Graph"),
            "herda-g-" + short_hash("/repo\0Graph", 4),
        )
        self.assertEqual(
            tab_id("space", "s", "Stream"),
            "space-s-" + short_hash("Stream", 4),
        )

    def test_terminal_name_truncation(self) -> None:
        short = terminal_name("s", "k", "run", "stream", "kernel")
        self.assertLessEqual(len(short), 32)
        self.assertTrue(short.startswith("herda-s-k-"))
        long = terminal_name(
            "a" * 20, "b" * 20, "run", "stream", "kernel"
        )
        self.assertLessEqual(len(long), 32)
        self.assertEqual(len(long.rsplit("-", 1)[-1]), 6)

    def test_mermaid_id_collision(self) -> None:
        used = {"n_same"}
        self.assertEqual(mermaid_id("n", "same", used), "n_same_2")

    def test_iso_helpers(self) -> None:
        self.assertIsNone(iso_utc(None))
        self.assertIsNone(iso_utc("not a datetime"))
        value = datetime(2026, 1, 2, 3, 4, 5, 123, tzinfo=timezone.utc)
        self.assertEqual(iso_utc(value), "2026-01-02T03:04:05Z")
        self.assertIsNone(parse_iso_utc(None))
        self.assertEqual(parse_iso_utc("2026-01-02T03:04:05Z").year, 2026)
        naive = parse_iso_utc("2026-01-02T03:04:05")
        self.assertEqual(naive.tzinfo, timezone.utc)
        self.assertEqual(
            parse_iso_utc("2026-01-02T03:04:05+09:00").hour, 18
        )


class PromptTests(RepoCase):
    def test_resolve_and_hash(self) -> None:
        path = resolve_prompt(Path(self.root), "prompts/prompt.md")
        self.assertEqual(path, Path(self.prompt))
        self.assertEqual(prompt_sha256("abc"), short_hash("abc", 64))

    def test_not_a_file(self) -> None:
        with self.assertRaises(PromptError) as caught:
            load_prompt(Path(self.prompts))
        self.assertEqual(caught.exception.code, "prompt_not_file")

    def test_invalid_utf8(self) -> None:
        path = Path(self.root) / "bad.md"
        path.write_bytes(b"\xff\xfe")
        with self.assertRaises(PromptError) as caught:
            load_prompt(path)
        self.assertEqual(caught.exception.code, "prompt_not_utf8")

    def test_unreadable(self) -> None:
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("nope")):
            with self.assertRaises(PromptError) as caught:
                load_prompt(Path(self.prompt))
        self.assertEqual(caught.exception.code, "prompt_unreadable")


class MermaidTests(RepoCase):
    def test_invalid_direction(self) -> None:
        graph = self.graph()
        graph.stream("api").kernel("a", self.prompt)
        with self.assertRaises(ValidationError):
            to_mermaid(graph, "XX")  # type: ignore[arg-type]

    def test_tb_and_escaping(self) -> None:
        graph = self.graph()
        stream = graph.stream('a&b"<c>')
        stream.kernel("k", self.prompt)
        text = graph.to_mermaid(direction="TB")
        self.assertIn("flowchart TB", text)
        self.assertIn("a&amp;b&quot;&lt;c&gt;", text)
        self.assertEqual(_escape("&\"<>"), "&amp;&quot;&lt;&gt;")
        self.assertEqual(_short_model("provider/"), "provider/")

    def test_duplicate_edge_is_drawn_once(self) -> None:
        graph = self.graph()
        api = graph.stream("api")
        design = api.kernel("design", self.prompt)
        done = api.record_event("done")
        web = graph.stream("web")
        web.wait_event(done)
        ui = web.kernel("ui", self.prompt)
        graph._kernel_dependencies[ui.qualified_name] = (design, design)
        text = graph.to_mermaid()
        self.assertEqual(text.count("n_api-design --> n_web-ui"), 1)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="herda-store-")
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_chmod_private_ignores_oserror(self) -> None:
        with mock.patch("herda.store.os.chmod", side_effect=OSError("nope")):
            RunStore(self.root / ".herda", "run")

    def test_atomic_write_failure_and_unlink_failure(self) -> None:
        target = self.root / "value.json"
        with self.assertRaises(TypeError):
            atomic_write_json(target, object())
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")],
            [],
        )
        with mock.patch("herda.store.os.unlink", side_effect=OSError("no")):
            with self.assertRaises(TypeError):
                atomic_write_json(target, object())

    def test_run_store_roundtrip(self) -> None:
        store = RunStore(self.root / ".herda", "run")
        self.assertIsNone(store.read_run())
        store.write_run({"a": 1})
        self.assertEqual(store.read_run(), {"a": 1})
        self.assertEqual(read_json(store.run_json), {"a": 1})
        snapshot = store.prompt_snapshot_path("api", "k")
        self.assertEqual(snapshot.name, "k.md")
        session = store.session_dir("api", "k")
        self.assertTrue(session.is_dir())
        screen = store.write_screen("api", "k", "hello")
        self.assertEqual(screen.read_text(), "hello")
        meta = store.write_meta("api", "k", {"x": 1})
        self.assertEqual(read_json(meta), {"x": 1})

    def test_graph_lock_lifecycle_and_conflict(self) -> None:
        path = self.root / "locks" / "g.lock"
        lock = GraphLock(path)
        lock.release()  # no-op
        lock.acquire()
        self.assertTrue(lock_is_held(path))
        with self.assertRaises(ConcurrentRunError):
            GraphLock(path).acquire()
        with self.assertRaises(CleanupError):
            acquire_cleanup_lock(path)
        lock.release()
        self.assertFalse(lock_is_held(path))

    def test_lock_non_eacces_and_open_errors(self) -> None:
        path = self.root / "locks" / "g.lock"
        path.parent.mkdir(parents=True)
        path.touch()
        with mock.patch(
            "fcntl.flock", side_effect=OSError(errno.EIO, "io")
        ):
            with self.assertRaises(OSError):
                GraphLock(path).acquire()
            self.assertFalse(lock_is_held(path))
        with mock.patch("pathlib.Path.open", side_effect=OSError("nope")):
            self.assertFalse(lock_is_held(path))
        self.assertFalse(lock_is_held(self.root / "missing.lock"))

    def test_documents(self) -> None:
        result = SimpleNamespace(
            name="api/k",
            stream="api",
            state="running",
            model="m",
            terminal=None,
            branch="b",
            worktree=Path("/w"),
            prompt_sha256="sha",
            status=None,
            started_at=None,
            finished_at=None,
            duration_s=None,
            output_path=None,
            error=HerdaError("x", "msg", "hint"),
        )
        doc = kernel_document(result)
        self.assertEqual(doc["state"], "running")
        self.assertIsNone(doc["prompt_path"])
        self.assertEqual(doc["error"], {"code": "x", "message": "msg", "hint": "hint"})
        result.error = None
        self.assertIsNone(kernel_document(result, Path("/p"))["error"])
        run = run_document(
            run_id="r",
            graph_name="g",
            graph_slug="g",
            repo=Path("/repo"),
            state="running",
            started_at=None,
            finished_at=None,
            streams={},
            kernels={},
            branches={},
        )
        self.assertEqual(run["run_id"], "r")


class WorktreeTests(RepoCase):
    def _proc(self, code: int = 0, out: str = "", err: str = ""):
        return subprocess.CompletedProcess([], code, out, err)

    def test_require_git_missing(self) -> None:
        with mock.patch("herda.worktree.shutil.which", return_value=None):
            with self.assertRaises(WorktreeError):
                worktree.require_git()
            self.assertIsNone(worktree.git_version())

    def test_run_git_errors(self) -> None:
        with mock.patch(
            "herda.worktree.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 1),
        ):
            with self.assertRaises(WorktreeError) as caught:
                worktree.run_git(self.repo, "status")
        self.assertEqual(caught.exception.code, "git_timeout")
        with mock.patch(
            "herda.worktree.subprocess.run", side_effect=OSError("nope")
        ):
            with self.assertRaises(WorktreeError) as caught:
                worktree.run_git(self.repo, "status")
        self.assertEqual(caught.exception.code, "git_failed")
        with mock.patch(
            "herda.worktree.subprocess.run",
            return_value=self._proc(1, err="bad"),
        ):
            with self.assertRaises(WorktreeError) as caught:
                worktree.run_git(self.repo, "status")
        self.assertIn("bad", str(caught.exception))

    def test_git_version_errors(self) -> None:
        with mock.patch(
            "herda.worktree.shutil.which", return_value="/usr/bin/git"
        ):
            with mock.patch(
                "herda.worktree.subprocess.run",
                side_effect=subprocess.TimeoutExpired("git", 1),
            ):
                self.assertIsNone(worktree.git_version())
            with mock.patch(
                "herda.worktree.subprocess.run",
                return_value=self._proc(0, out="", err=""),
            ):
                self.assertIsNone(worktree.git_version())

    def test_repo_and_base_errors(self) -> None:
        self.assertFalse(worktree.is_git_repo(Path(self.root) / "missing"))
        with self.assertRaises(WorktreeError):
            worktree.require_git_repo(Path(self.root) / "missing")
        with self.assertRaises(WorktreeError):
            worktree.resolve_commit(self.repo, "")
        with self.assertRaises(WorktreeError):
            worktree.resolve_commit(self.repo, "nope")

    def test_branch_exists_other_error(self) -> None:
        with mock.patch(
            "herda.worktree.run_git", return_value=self._proc(2, err="bad")
        ):
            with self.assertRaises(WorktreeError):
                worktree.branch_exists(self.repo, "x")

    def test_create_worktree_collisions_and_rollback(self) -> None:
        existing = Path(self.root) / "existing"
        existing.mkdir()
        with self.assertRaises(WorktreeError):
            worktree.create_worktree(self.repo, existing, "b", "main")
        with mock.patch("herda.worktree.branch_exists", return_value=True):
            with self.assertRaises(WorktreeError):
                worktree.create_worktree(
                    self.repo, Path(self.root) / "new", "b", "main"
                )
        with mock.patch(
            "herda.worktree.run_git", side_effect=WorktreeError("x", "boom")
        ), mock.patch(
            "herda.worktree.branch_exists", side_effect=[False, True]
        ), mock.patch("herda.worktree.delete_branch") as delete:
            with self.assertRaises(WorktreeError):
                worktree.create_worktree(
                    self.repo, Path(self.root) / "new2", "b", "main"
                )
            delete.assert_called_once()

    def test_remove_and_dirty_errors(self) -> None:
        with mock.patch("herda.worktree.run_git") as run:
            worktree.remove_worktree(self.repo, Path(self.root) / "missing")
            run.assert_called_with(self.repo, "worktree", "prune", check=False)
        with mock.patch(
            "herda.worktree.run_git", return_value=self._proc(1, err="dirty")
        ):
            with self.assertRaises(WorktreeError):
                worktree.remove_worktree(self.repo, self.repo, force=True)
            with self.assertRaises(WorktreeError):
                worktree.worktree_is_dirty(self.repo)
        self.assertFalse(worktree.worktree_is_dirty(Path(self.root) / "missing"))
        worktree.prune_worktrees(self.repo)

    def test_ensure_exclude_edges(self) -> None:
        with mock.patch(
            "herda.worktree.run_git", return_value=self._proc(1)
        ):
            worktree.ensure_main_exclude(self.repo)
        with mock.patch("pathlib.Path.open", side_effect=OSError("nope")):
            worktree.ensure_main_exclude(self.repo)
        exclude = Path(self.repo) / ".git" / "info" / "exclude"
        exclude.write_text("old", encoding="utf-8")
        worktree.ensure_main_exclude(self.repo)
        self.assertIn("old\n.herda/\n", exclude.read_text(encoding="utf-8"))

    def test_init_and_remove_engine_dir_errors(self) -> None:
        target = Path(self.root) / "wt"
        target.mkdir()
        engine = worktree.init_worktree_herda(target)
        self.assertTrue((engine / ".gitignore").exists())
        with mock.patch("pathlib.Path.mkdir", side_effect=OSError("nope")):
            with self.assertRaises(WorktreeError):
                worktree.init_worktree_herda(target / "other")
        worktree.remove_engine_dir(target / "missing")
        with mock.patch("herda.worktree.shutil.rmtree", side_effect=OSError("no")):
            with self.assertRaises(WorktreeError):
                worktree.remove_engine_dir(target)
        path = worktree.write_large_prompt(target, "api", "k", "text")
        self.assertEqual(path.read_text(), "text")


class AppTests(unittest.TestCase):
    def _proc(self, code: int = 0, out: str = "", err: str = ""):
        return subprocess.CompletedProcess([], code, out, err)

    def test_available(self) -> None:
        with mock.patch("herda.app.shutil.which", return_value=None):
            self.assertFalse(app.available())
        with mock.patch("herda.app.shutil.which", return_value="x"):
            self.assertTrue(app.available())

    def test_do_edges(self) -> None:
        with mock.patch(
            "herda.app.subprocess.run", return_value=self._proc(0)
        ):
            self.assertTrue(app._do("space.rename", {"id": "x"}))
        with mock.patch(
            "herda.app.subprocess.run",
            side_effect=subprocess.TimeoutExpired("herdr-app", 1),
        ):
            self.assertFalse(app._do("space.rename", {"id": "x"}))
        with mock.patch(
            "herda.app.subprocess.run", return_value=self._proc(1, err="")
        ):
            self.assertFalse(app._do("space.rename", {"id": "x"}))
        with mock.patch(
            "herda.app.subprocess.run", return_value=self._proc(1, out="bad")
        ):
            self.assertFalse(app._do("space.rename", {"id": "x"}))

    def test_rename(self) -> None:
        with mock.patch("herda.app.available", return_value=False):
            self.assertFalse(
                app.rename(space_id="s", space_name="S", tabs={})
            )
        with mock.patch("herda.app.available", return_value=True), mock.patch(
            "herda.app._do", return_value=True
        ):
            self.assertTrue(
                app.rename(space_id="s", space_name="S", tabs={"t": "T"})
            )
        with mock.patch("herda.app.available", return_value=True), mock.patch(
            "herda.app._do", side_effect=[False, True, True]
        ), mock.patch("herda.app.time.sleep"):
            self.assertTrue(
                app.rename(
                    space_id="s",
                    space_name="S",
                    tabs={"t": "T"},
                    retries=1,
                    delay_s=0,
                )
            )
        with mock.patch("herda.app.available", return_value=True), mock.patch(
            "herda.app._do", return_value=False
        ), mock.patch("herda.app.time.sleep"):
            self.assertFalse(
                app.rename(
                    space_id="s",
                    space_name="S",
                    tabs={"t": "T"},
                    retries=0,
                    delay_s=0,
                )
            )

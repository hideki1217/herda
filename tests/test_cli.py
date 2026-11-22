"""CLI command and loader coverage."""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from herda import HerdaError, HerdaTimeoutError, KernelState, RunState
from herda.backend import TerminalInfo
from herda.cli import (
    _cmd_app_sync,
    _cmd_clean,
    _cmd_doctor,
    _cmd_draw,
    _cmd_run,
    _cmd_status,
    _cmd_validate,
    _load_graph,
    _print_error,
    _print_results,
    _print_run_output,
    _sync_once,
    _worktree_state,
    main,
)
from herda.doctor import Check
from herda.types import ErrorInfo, KernelResult

from .test_herda import RepoCase, _git


class FakeExecution:
    def __init__(self, state: RunState = RunState.succeeded) -> None:
        self.id = "run"
        self.state = state
        self.run_dir = Path("/run")
        self.cancelled = False
        self.kernels = {
            "api/k": KernelResult(
                name="api/k",
                stream="api",
                state=KernelState.succeeded,
                model="m",
                terminal="t1",
                branch="b",
                worktree=Path("/w"),
                prompt_sha256="sha",
                status="idle",
                started_at=None,
                finished_at=None,
                duration_s=1.0,
                output_path=None,
                error=None,
            ),
            "api/bad": KernelResult(
                name="api/bad",
                stream="api",
                state=KernelState.failed,
                model="m",
                terminal="t2",
                branch="b",
                worktree=Path("/w"),
                prompt_sha256="sha",
                status="error",
                started_at=None,
                finished_at=None,
                duration_s=1.0,
                output_path=None,
                error=ErrorInfo("x", "bad"),
            ),
        }

    def wait(self, timeout_s=None):
        return self

    def cancel(self, *, grace_s: float = 10.0) -> None:
        self.cancelled = True


class TimeoutExecution(FakeExecution):
    def wait(self, timeout_s=None):
        raise HerdaTimeoutError("wait_timeout", "timeout")


class FakeGraph:
    def __init__(self, execution: FakeExecution | None = None) -> None:
        self.name = "cli"
        self.execution = execution or FakeExecution()
        self.validated = False

    def validate(self) -> None:
        self.validated = True

    def start(self):
        return self.execution

    def to_mermaid(self, *, direction="LR"):
        return f"flowchart {direction}\n"

    def draw(self, path, *, direction="LR"):
        Path(path).write_text(f"flowchart {direction}\n", encoding="utf-8")
        return Path(path)


class CliTests(RepoCase):
    def setUp(self) -> None:
        super().setUp()
        self.old_cwd = os.getcwd()
        self.graph_path = os.path.join(self.root, "graph.py")
        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write(
                "from herda import Graph\n"
                "graph = Graph('cli', 'repo', default_model='p/m')\n"
                "graph.stream('api').kernel('a', 'prompts/prompt.md')\n"
            )

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        super().tearDown()

    def test_load_graph_errors_and_success(self) -> None:
        with self.assertRaises(HerdaError) as caught:
            _load_graph(os.path.join(self.root, "missing.py"))
        self.assertEqual(caught.exception.code, "file_not_found")

        with mock.patch(
            "herda.cli.importlib.util.spec_from_file_location",
            return_value=None,
        ):
            with self.assertRaises(HerdaError) as caught:
                _load_graph(self.graph_path)
        self.assertEqual(caught.exception.code, "import_failed")

        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write("raise RuntimeError('boom')\n")
        with self.assertRaises(HerdaError) as caught:
            _load_graph(self.graph_path)
        self.assertEqual(caught.exception.code, "import_failed")

        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write(
                "from herda import ValidationError\n"
                "raise ValidationError('x', 'y')\n"
            )
        with self.assertRaises(HerdaError) as caught:
            _load_graph(self.graph_path)
        self.assertEqual(caught.exception.code, "x")

        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        with self.assertRaises(HerdaError) as caught:
            _load_graph(self.graph_path)
        self.assertEqual(caught.exception.code, "graph_missing")

        with open(self.graph_path, "w", encoding="utf-8") as fh:
            fh.write(
                "from herda import Graph\n"
                "graph = Graph('cli', 'repo', default_model='p/m')\n"
                "graph.stream('api').kernel('a', 'prompts/prompt.md')\n"
            )
        graph = _load_graph(self.graph_path)
        self.assertEqual(graph.name, "cli")

        with open(os.path.join(self.root, "helper.py"), "w") as fh:
            fh.write("MODEL = 'helper/model'\n")
        import_graph = os.path.join(self.root, "graph_import.py")
        with open(import_graph, "w") as fh:
            fh.write(
                "from herda import Graph\n"
                "from helper import MODEL\n"
                "graph = Graph('imported', 'repo', default_model=MODEL)\n"
                "graph.stream('api').kernel('a', 'prompts/prompt.md')\n"
            )
        graph = _load_graph(import_graph)
        self.assertEqual(graph.default_model, "helper/model")

        class NoRemovePath(list):
            def remove(self, item):
                raise ValueError("gone")

        with mock.patch.object(sys, "path", NoRemovePath(sys.path)):
            _load_graph(self.graph_path)
        with mock.patch.object(
            sys, "path", [str(self.root)] + list(sys.path)
        ):
            _load_graph(self.graph_path)

    def test_print_error(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _print_error(HerdaError("x", "message", "hint"))
            _print_error(HerdaError("x", "message"))
        self.assertIn("hint", err.getvalue())

    def test_validate_and_draw(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_validate(SimpleNamespace(graph=self.graph_path)), 0
            )
        self.assertIn("ok: graph", out.getvalue())

        with mock.patch(
            "herda.graph.Graph.validate_models"
        ) as validate_models, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                _cmd_validate(
                    SimpleNamespace(graph=self.graph_path, models=True)
                ),
                0,
            )
        validate_models.assert_called_once()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_draw(
                    SimpleNamespace(
                        graph=self.graph_path, output="-", direction="TB"
                    )
                ),
                0,
            )
        self.assertIn("flowchart TB", out.getvalue())

        target = os.path.join(self.root, "dag.mmd")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                _cmd_draw(
                    SimpleNamespace(
                        graph=self.graph_path,
                        output=target,
                        direction="LR",
                    )
                ),
                0,
            )
        self.assertTrue(os.path.exists(target))

        missing = os.path.join(self.root, "missing-dir", "dag.mmd")
        with self.assertRaises(HerdaError) as caught:
            _cmd_draw(
                SimpleNamespace(
                    graph=self.graph_path,
                    output=missing,
                    direction="LR",
                )
            )
        self.assertEqual(caught.exception.code, "draw_failed")

    def test_print_run_output_fallbacks(self) -> None:
        execution = FakeExecution()
        execution.run_dir = Path(self.root) / "missing-run"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_run_output(execution, True)
        self.assertIn("run_id: run", out.getvalue())

        execution.run_dir.mkdir()
        (execution.run_dir / "run.json").write_text("[]", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_run_output(execution, True)
        self.assertIn("run_id: run", out.getvalue())

    def test_worktree_state_missing(self) -> None:
        self.assertEqual(_worktree_state(None), "missing")
        self.assertEqual(_worktree_state(""), "missing")

    def test_print_results(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_results(FakeExecution())
        text = out.getvalue()
        self.assertIn("run_id: run", text)
        self.assertIn("status=idle", text)
        self.assertIn("[x: bad]", text)

    def test_cmd_run_success_failure_and_signal(self) -> None:
        for state, expected in (
            (RunState.succeeded, 0),
            (RunState.failed, 1),
        ):
            execution = FakeExecution(state)
            graph = FakeGraph(execution)
            handlers = []
            with mock.patch("herda.cli._load_graph", return_value=graph), mock.patch(
                "herda.cli.signal.getsignal", return_value=object()
            ), mock.patch(
                "herda.cli.signal.signal",
                side_effect=(
                    lambda sig, handler, h=handlers: h.append(handler)
                ),
            ), contextlib.redirect_stdout(io.StringIO()):
                result = _cmd_run(SimpleNamespace(graph="g"))
            self.assertEqual(result, expected)
            if handlers:
                handlers[0](2, None)
                self.assertTrue(execution.cancelled)

        graph = FakeGraph()
        with mock.patch("herda.cli._load_graph", return_value=graph), mock.patch(
            "herda.cli.signal.getsignal", side_effect=ValueError("no")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_cmd_run(SimpleNamespace(graph="g")), 0)

        graph = FakeGraph()
        with mock.patch("herda.cli._load_graph", return_value=graph), mock.patch(
            "herda.cli.signal.signal", side_effect=OSError("no")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_cmd_run(SimpleNamespace(graph="g")), 0)

        graph = FakeGraph()
        with mock.patch("herda.cli._load_graph", return_value=graph), mock.patch(
            "herda.cli.signal.signal",
            side_effect=[None, None, OSError("no"), ValueError("no")],
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_cmd_run(SimpleNamespace(graph="g")), 0)

    def test_cmd_doctor_json(self) -> None:
        checks = [
            Check("git", True, "ok", required=True),
            Check("extra", False, "skip", required=False),
        ]
        out = io.StringIO()
        with mock.patch(
            "herda.cli.run_doctor", return_value=checks
        ), contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_doctor(SimpleNamespace(deep=False, app=False, json=True)),
                0,
            )
        self.assertIn('"name": "git"', out.getvalue())

    def test_cmd_run_timeout_cancels(self) -> None:
        execution = TimeoutExecution()
        execution.run_dir = Path(self.root) / "run"
        execution.run_dir.mkdir()
        (execution.run_dir / "run.json").write_text(
            json.dumps({"state": "canceled"}), encoding="utf-8"
        )
        graph = FakeGraph(execution)
        out = io.StringIO()
        with mock.patch(
            "herda.cli._load_graph", return_value=graph
        ), mock.patch(
            "herda.cli.signal.getsignal", return_value=object()
        ), mock.patch(
            "herda.cli.signal.signal"
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(
            io.StringIO()
        ):
            with self.assertRaises(HerdaTimeoutError) as caught:
                _cmd_run(SimpleNamespace(graph="g", timeout=1, json=True))
        self.assertTrue(execution.cancelled)
        self.assertEqual(caught.exception.code, "run_timeout")
        self.assertIn('"state": "canceled"', out.getvalue())

    def test_cmd_run_json_output(self) -> None:
        execution = FakeExecution()
        execution.run_dir = Path(self.root) / "run"
        execution.run_dir.mkdir()
        (execution.run_dir / "run.json").write_text(
            json.dumps({"run_id": "run", "state": "succeeded"}),
            encoding="utf-8",
        )
        graph = FakeGraph(execution)
        out = io.StringIO()
        with mock.patch(
            "herda.cli._load_graph", return_value=graph
        ), mock.patch(
            "herda.cli.signal.getsignal", return_value=object()
        ), mock.patch(
            "herda.cli.signal.signal"
        ), contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_run(SimpleNamespace(graph="g", json=True)), 0
            )
        self.assertIn('"run_id": "run"', out.getvalue())

    def test_cmd_status(self) -> None:
        run_id = "20260101T000000Z-abcdef"
        run_dir = Path(self.repo) / ".herda" / "runs" / run_id
        run_dir.mkdir(parents=True)
        document = {
            "run_id": run_id,
            "graph": "g",
            "state": "succeeded",
            "agent": "pi",
            "started_at": None,
            "finished_at": None,
            "kernels": {
                "api/a": {
                    "state": "succeeded",
                    "status": "idle",
                    "error": None,
                },
                "api/bad": {
                    "state": "failed",
                    "status": "error",
                    "error": {"code": "x", "message": "bad"},
                },
                "not-dict": "x",
            },
        }
        (run_dir / "run.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_status(
                    SimpleNamespace(
                        repo=self.repo, run_id=None, json=False
                    )
                ),
                0,
            )
        self.assertIn("agent: pi", out.getvalue())
        self.assertIn("api/a: succeeded", out.getvalue())
        self.assertIn("[x: bad]", out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_status(
                    SimpleNamespace(
                        repo=self.repo, run_id=run_id, json=True
                    )
                ),
                0,
            )
        self.assertIn(run_id, out.getvalue())

        with self.assertRaises(HerdaError) as caught:
            _cmd_status(
                SimpleNamespace(
                    repo=self.repo,
                    run_id="20260101T000001Z-abcdef",
                    json=False,
                )
            )
        self.assertEqual(caught.exception.code, "run_not_found")

        with self.assertRaises(HerdaError) as caught:
            _cmd_status(
                SimpleNamespace(repo=self.repo, run_id="bad", json=False)
            )
        self.assertEqual(caught.exception.code, "invalid_run_id")

    def test_cmd_status_streams_order_dirty_and_subdir(self) -> None:
        run_id = "20260101T000010Z-abcdef"
        run_dir = Path(self.repo) / ".herda" / "runs" / run_id
        run_dir.mkdir(parents=True)
        clean = Path(self.root) / "clean-wt"
        _git(self.repo, "worktree", "add", "-b", "clean-branch", str(clean), "main")
        dirty = Path(self.root) / "dirty-wt"
        _git(self.repo, "worktree", "add", "-b", "dirty-branch", str(dirty), "main")
        (dirty / "untracked.txt").write_text("x", encoding="utf-8")
        missing = Path(self.root) / "missing-wt"
        unknown = Path(self.root) / "unknown-wt"
        unknown.mkdir()
        document = {
            "run_id": run_id,
            "graph": "g",
            "state": "succeeded",
            "agent": "pi",
            "cleaned_at": "2026-01-01T00:00:01Z",
            "worktrees_removed": False,
            "streams": {
                "api": {
                    "base": "main",
                    "base_commit": "abc",
                    "branch": "herda/g/api-abcdef",
                    "worktree": str(clean),
                },
                "web": {
                    "base": "main",
                    "base_commit": "abc",
                    "branch": "herda/g/web-abcdef",
                    "worktree": str(dirty),
                },
                "gone": {
                    "base": "main",
                    "base_commit": "abc",
                    "branch": "herda/g/gone-abcdef",
                    "worktree": str(missing),
                },
                "unknown": {
                    "base": "main",
                    "base_commit": "abc",
                    "branch": "herda/g/unknown-abcdef",
                    "worktree": str(unknown),
                },
            },
            "stream_order": ["web", 1, "missing-key", "api", "gone", "unknown"],
            "kernels": {
                "api/a": {"state": "succeeded"},
                "web/z": {"state": "succeeded"},
            },
            "kernel_order": ["web/z", "api/a"],
        }
        (run_dir / "run.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        nested = Path(self.repo) / "nested"
        nested.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_status(
                    SimpleNamespace(repo=nested, run_id=run_id, json=False)
                ),
                0,
            )
        text = out.getvalue()
        self.assertLess(text.index("  web:"), text.index("  api:"))
        self.assertLess(text.index("  web/z:"), text.index("  api/a:"))
        self.assertNotIn("missing-key", text)
        self.assertIn("status: clean", text)
        self.assertIn("status: dirty", text)
        self.assertIn("status: missing", text)
        self.assertIn("status: unknown", text)
        self.assertIn("cleaned_at:", text)
        self.assertIn("run_dir:", text)

    def test_cmd_status_empty_and_invalid_json(self) -> None:
        with self.assertRaises(HerdaError) as caught:
            _cmd_status(
                SimpleNamespace(repo=self.repo, run_id=None, json=False)
            )
        self.assertEqual(caught.exception.code, "run_not_found")

        run_id = "20260101T000009Z-abcdef"
        run_dir = Path(self.repo) / ".herda" / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(HerdaError) as caught:
            _cmd_status(
                SimpleNamespace(repo=self.repo, run_id=run_id, json=False)
            )
        self.assertEqual(caught.exception.code, "invalid_run_json")

        (run_dir / "run.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(HerdaError) as caught:
            _cmd_status(
                SimpleNamespace(repo=self.repo, run_id=run_id, json=False)
            )
        self.assertEqual(caught.exception.code, "invalid_run_json")

        (run_dir / "run.json").write_text(
            json.dumps({"kernels": []}), encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                _cmd_status(
                    SimpleNamespace(
                        repo=self.repo, run_id=run_id, json=False
                    )
                ),
                0,
            )

        (run_dir / "run.json").write_text(
            json.dumps({"streams": [], "kernels": {}}), encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                _cmd_status(
                    SimpleNamespace(
                        repo=self.repo, run_id=run_id, json=False
                    )
                ),
                0,
            )

        (run_dir / "run.json").write_text(
            json.dumps({"streams": {"s": "not-a-dict"}, "kernels": {}}),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                _cmd_status(
                    SimpleNamespace(
                        repo=self.repo, run_id=run_id, json=False
                    )
                ),
                0,
            )

    def test_cmd_clean(self) -> None:
        out = io.StringIO()
        with mock.patch("herda.cli.cleanup_run") as cleanup, contextlib.redirect_stdout(
            out
        ):
            result = _cmd_clean(
                SimpleNamespace(
                    repo=self.repo,
                    run_id="run",
                    worktrees=True,
                    branches=True,
                    force=True,
                    purge=True,
                )
            )
        self.assertEqual(result, 0)
        cleanup.assert_called_once()
        self.assertTrue(cleanup.call_args.kwargs["branches"])
        self.assertTrue(cleanup.call_args.kwargs["purge"])
        self.assertIn("cleaned run run", out.getvalue())

    def test_cmd_clean_orphans_and_missing_run_id(self) -> None:
        out = io.StringIO()
        with mock.patch(
            "herda.cli.cleanup_orphans", return_value=(2, 3)
        ) as cleanup, contextlib.redirect_stdout(out):
            self.assertEqual(
                _cmd_clean(
                    SimpleNamespace(
                        repo=self.repo,
                        run_id=None,
                        orphans=True,
                        force=False,
                        worktrees=False,
                        branches=False,
                    )
                ),
                0,
            )
        cleanup.assert_called_once_with(
            Path(self.repo), force=False, branches=False
        )
        self.assertIn("2 orphan worktrees and 3 orphan branches", out.getvalue())

        with self.assertRaises(HerdaError) as caught:
            _cmd_clean(
                SimpleNamespace(
                    repo=self.repo,
                    run_id=None,
                    orphans=False,
                    force=False,
                    worktrees=False,
                    branches=False,
                )
            )
        self.assertEqual(caught.exception.code, "run_id_required")

    def test_sync_once(self) -> None:
        class Backend:
            def __init__(self, terminals):
                self.terminals = terminals

            def list(self, labels=None):
                return self.terminals

        terminals = [
            TerminalInfo(
                "t1", "live", "idle", 0, True, labels={"herda.tab_name": "x"}
            ),
            TerminalInfo(
                "t2",
                "live",
                "idle",
                0,
                True,
                labels={
                    "herdr.space": "s",
                    "herda.space_name": "S",
                    "herdr.tab": "t",
                    "herda.tab_name": "T",
                },
            ),
        ]
        synced: set[tuple[str, str]] = set()
        with mock.patch("herda.cli.app.rename", return_value=True) as rename:
            _sync_once(Backend(terminals), synced)
        rename.assert_called_once_with(
            space_id="s",
            space_name="S",
            tabs={"t": "T"},
            retries=1,
            delay_s=0.5,
        )
        self.assertIn(("s", "t"), synced)

        with mock.patch("herda.cli.app.rename") as rename:
            _sync_once(Backend(terminals), synced)
        rename.assert_not_called()

        with mock.patch("herda.cli.app.rename", return_value=False):
            _sync_once(Backend(terminals), set())

    def test_app_sync(self) -> None:
        with mock.patch("herda.cli.app.available", return_value=False):
            self.assertEqual(
                _cmd_app_sync(SimpleNamespace(once=True, interval=1)), 1
            )

        backend = SimpleNamespace(hc_path=None, hc="hc")
        with mock.patch(
            "herda.cli.app.available", return_value=True
        ), mock.patch(
            "herda.cli.HcBackend", return_value=backend
        ), mock.patch("shutil.which", return_value=None):
            self.assertEqual(
                _cmd_app_sync(SimpleNamespace(once=True, interval=1)), 1
            )

        backend = SimpleNamespace(hc_path="/bin/hc", hc="hc")
        with mock.patch(
            "herda.cli.app.available", return_value=True
        ), mock.patch(
            "herda.cli.HcBackend", return_value=backend
        ), mock.patch("herda.cli._sync_once") as sync:
            self.assertEqual(
                _cmd_app_sync(SimpleNamespace(once=True, interval=1)), 0
            )
        sync.assert_called_once()

        with mock.patch(
            "herda.cli.app.available", return_value=True
        ), mock.patch(
            "herda.cli.HcBackend", return_value=backend
        ), mock.patch(
            "herda.cli._sync_once", side_effect=[None, KeyboardInterrupt]
        ), mock.patch("herda.cli.time.sleep") as sleep:
            self.assertEqual(
                _cmd_app_sync(SimpleNamespace(once=False, interval=1)), 0
            )
        sleep.assert_called_once_with(1)

    def test_main_error_paths_and_module_entrypoint(self) -> None:
        err = io.StringIO()
        with mock.patch(
            "herda.cli.run_doctor", side_effect=HerdaError("x", "y", "h")
        ), contextlib.redirect_stderr(err):
            self.assertEqual(main(["doctor"]), 1)
        self.assertIn("hint", err.getvalue())

        with mock.patch(
            "herda.cli.run_doctor", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(main(["doctor"]), 130)

        with mock.patch(
            "herda.cli.run_doctor", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--verbose", "doctor"]), 0)

        with mock.patch("sys.argv", ["herda", "doctor"]), mock.patch(
            "herda.cli.run_doctor", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("herda", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)

        with mock.patch("sys.argv", ["herda", "doctor"]), mock.patch(
            "herda.doctor.run_doctor", return_value=[]
        ), mock.patch(
            "herda.doctor.print_checks", return_value=0
        ), contextlib.redirect_stdout(io.StringIO()):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_module("herda.cli", run_name="__main__")
        self.assertEqual(caught.exception.code, 0)

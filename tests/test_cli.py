"""CLI command and loader coverage."""

from __future__ import annotations

import contextlib
import io
import os
import runpy
import sys
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from herda import HerdaError, KernelState, RunState
from herda.backend import TerminalInfo
from herda.cli import (
    _cmd_app_sync,
    _cmd_clean,
    _cmd_draw,
    _cmd_run,
    _cmd_validate,
    _load_graph,
    _print_error,
    _print_results,
    _sync_once,
    main,
)
from herda.types import ErrorInfo, KernelResult

from .test_herda import RepoCase


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
                side_effect=lambda sig, handler: handlers.append(handler),
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
                    force=True,
                )
            )
        self.assertEqual(result, 0)
        cleanup.assert_called_once()
        self.assertIn("cleaned run run", out.getvalue())

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

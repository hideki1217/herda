from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from herda import (
    CleanupError,
    ConcurrentRunError,
    Graph,
    HerdaTimeoutError,
    HerdrError,
    KernelState,
    PromptError,
    RunState,
    ValidationError,
    WorktreeError,
)
from herda.backend import HcBackend
from herda.cli import main as cli_main
from herda.session import START_OBSERVE_S

from .fake_backend import FakeBackend


def _git(repo, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="herda-test-")
        self.root = os.path.realpath(self._temp.name)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.com")
        _git(self.repo, "config", "user.name", "Test")
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("initial\n")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "initial")

        self.prompts = os.path.join(self.root, "prompts")
        os.makedirs(self.prompts)
        self.prompt = self._prompt("prompt.md", "do the thing\n")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _prompt(self, name: str, text: str) -> str:
        path = os.path.join(self.prompts, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def graph(self, **kwargs) -> Graph:
        backend = kwargs.pop("_backend", FakeBackend())
        kwargs.setdefault("default_model", "anthropic/claude-sonnet-5-5")
        return Graph(
            "auth",
            self.repo,
            base_dir=self.root,
            _backend=backend,
            **kwargs,
        )


class ValidationTests(RepoCase):
    def test_names_are_required_and_unique(self) -> None:
        with self.assertRaises(ValidationError):
            Graph("", self.repo, default_model="x/y")
        with self.assertRaises(ValidationError):
            Graph("a/b", self.repo, default_model="x/y")

        graph = self.graph()
        with self.assertRaises(ValidationError):
            graph.stream("")
        stream = graph.stream("api")
        stream.kernel("design", self.prompt)
        with self.assertRaises(ValidationError):
            stream.kernel("design", self.prompt)
        with self.assertRaises(ValidationError):
            graph.stream("api")

    def test_empty_graph_and_stream(self) -> None:
        with self.assertRaises(ValidationError):
            self.graph().validate()
        graph = self.graph()
        graph.stream("api")
        with self.assertRaises(ValidationError):
            graph.validate()

    def test_prompt_errors(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        stream.kernel("missing", "prompts/nope.md")
        with self.assertRaises(PromptError):
            graph.validate()

        for name, content in (("bad.txt", "hello"), ("empty.md", "  \n")):
            graph = self.graph()
            stream = graph.stream("api")
            if name == "bad.txt":
                path = os.path.join(self.root, name)
                with open(path, "w") as fh:
                    fh.write(content)
            else:
                path = self._prompt(name, content)
            stream.kernel("k", path)
            with self.assertRaises(PromptError):
                graph.validate()

    def test_event_record_and_wait(self) -> None:
        graph = self.graph()
        api = graph.stream("api")
        design = api.kernel("design", self.prompt)
        design_done = api.record_event("design_done")

        web = graph.stream("web")
        web.wait_event(design_done)
        ui = web.kernel("ui", self.prompt)

        self.assertIs(graph.events["design_done"], design_done)
        self.assertEqual(graph._dependencies_of(ui), (design,))

    def test_wait_applies_to_next_kernel_only(self) -> None:
        graph = self.graph()
        api = graph.stream("api")
        design = api.kernel("design", self.prompt)
        done = api.record_event("done")

        web = graph.stream("web")
        web.wait_event(done)
        first = web.kernel("first", self.prompt)
        second = web.kernel("second", self.prompt)

        self.assertEqual(graph._dependencies_of(first), (design,))
        self.assertEqual(graph._dependencies_of(second), (first,))

    def test_event_wait_in_same_stream_is_a_noop(self) -> None:
        graph = self.graph()
        api = graph.stream("api")
        first = api.kernel("a", self.prompt)
        event = api.record_event("a_done")
        api.wait_event(event)
        second = api.kernel("b", self.prompt)

        self.assertEqual(graph._dependencies_of(second), (first,))

    def test_wait_stream_captures_current_tail(self) -> None:
        graph = self.graph()
        api = graph.stream("api")
        api.kernel("a", self.prompt)
        tail = api.kernel("b", self.prompt)

        web = graph.stream("web")
        web.wait_stream(api)
        ui = web.kernel("ui", self.prompt)
        later = api.kernel("later", self.prompt)

        self.assertEqual(graph._dependencies_of(ui), (tail,))
        self.assertNotIn(later, graph._dependencies_of(ui))

    def test_record_event_auto_names(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        stream.kernel("a", self.prompt)
        first = stream.record_event()
        second = stream.record_event()
        self.assertRegex(first.name, r"^event-\d+$")
        self.assertNotEqual(first.name, second.name)

    def test_event_names_are_unique_and_validated(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        stream.kernel("a", self.prompt)
        stream.record_event("done")
        with self.assertRaises(ValidationError):
            stream.record_event("done")
        with self.assertRaises(ValidationError):
            stream.record_event("")
        with self.assertRaises(ValidationError):
            stream.record_event("a/b")

    def test_event_from_another_graph_is_rejected(self) -> None:
        graph = self.graph()
        other = self.graph()
        other_stream = other.stream("api")
        other_stream.kernel("a", self.prompt)
        foreign = other_stream.record_event("done")

        graph.stream("api").kernel("a", self.prompt)
        with self.assertRaises(ValidationError):
            graph.stream("web").wait_event(foreign)

    def test_stream_from_another_graph_is_rejected(self) -> None:
        graph = self.graph()
        other = self.graph()
        foreign = other.stream("api")
        foreign.kernel("a", self.prompt)

        graph.stream("api").kernel("a", self.prompt)
        with self.assertRaises(ValidationError):
            graph.stream("web").wait_stream(foreign)

    def test_inherit_requires_previous_kernel(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        with self.assertRaises(ValidationError):
            stream.kernel("first", self.prompt, inherit_session=True)

    def test_bad_base_is_a_worktree_error(self) -> None:
        graph = self.graph()
        stream = graph.stream("api", base="does-not-exist")
        stream.kernel("a", self.prompt)
        with self.assertRaises(WorktreeError):
            graph.validate()

    def test_draw_requires_mermaid_suffix(self) -> None:
        graph = self.graph()
        graph.stream("api").kernel("a", self.prompt)
        with self.assertRaises(ValidationError):
            graph.draw(os.path.join(self.root, "dag.svg"))


class MermaidTests(RepoCase):
    def test_snapshot(self) -> None:
        graph = self.graph()
        api = graph.stream("api", base="main")
        design = api.kernel("design", self.prompt)
        design_done = api.record_event("design_done")
        api.kernel("implement", self.prompt)
        web = graph.stream("web", base="main")
        web.wait_event(design_done)
        web.kernel("ui", self.prompt)

        expected = (
            "flowchart LR\n"
            '  subgraph stream_api["api  (base=main)"]\n'
            '    n_api-design["design<br/>claude-sonnet-5-5"]\n'
            '    n_api-implement["implement<br/>claude-sonnet-5-5"]\n'
            "  end\n"
            '  subgraph stream_web["web  (base=main)"]\n'
            '    n_web-ui["ui<br/>claude-sonnet-5-5"]\n'
            "  end\n"
            "  n_api-design -.-> n_api-implement\n"
            "  n_api-design --> n_web-ui\n"
        )
        self.assertEqual(graph.to_mermaid(), expected)


class ExecutionTests(RepoCase):
    def test_success_creates_worktrees_and_cleans_terminals(self) -> None:
        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        api = graph.stream("api", base="main")
        design = api.kernel("design", self.prompt)
        design_done = api.record_event("design_done")
        api.kernel("implement", self.prompt)
        api.kernel("test", self.prompt)
        web = graph.stream("web", base="main")
        web.wait_event(design_done)
        web.kernel("ui", self.prompt)

        execution = graph.run()

        self.assertEqual(execution.state, RunState.succeeded)
        self.assertTrue(
            all(
                result.state == KernelState.succeeded
                for result in execution.kernels.values()
            )
        )
        self.assertEqual(backend.terminals, {})
        for stream in graph.streams.values():
            self.assertIsNotNone(stream.worktree)
            self.assertTrue(stream.worktree.is_dir())
            self.assertTrue(stream.branch.startswith("herda/auth/"))
            self.assertTrue((stream.worktree / ".herda" / ".gitignore").exists())
            self.assertTrue(stream.branch)

        run_json = execution.run_dir / "run.json"
        document = json.loads(run_json.read_text(encoding="utf-8"))
        self.assertEqual(document["state"], "succeeded")
        self.assertEqual(document["run_id"], execution.id)
        for name, result in execution.kernels.items():
            self.assertIsNotNone(result.output_path)
            self.assertTrue(result.output_path.exists())
            self.assertIn(name, document["kernels"])

        worktrees = [stream.worktree for stream in graph.streams.values()]
        branches = [stream.branch for stream in graph.streams.values()]
        graph.cleanup(execution.id, worktrees=True)
        for path in worktrees:
            self.assertFalse(path.exists())
        for branch in branches:
            proc = _git(self.repo, "branch", "--list", branch)
            self.assertIn(branch, proc.stdout)

    def test_dependency_failure_skips_downstream_only(self) -> None:
        backend = FakeBackend(fail={"api/fail"})
        graph = self.graph(_backend=backend)
        api = graph.stream("api", base="main")
        api.kernel("good", self.prompt)
        failing = api.kernel("fail", self.prompt)
        failed = api.record_event("fail_done")
        api.kernel("downstream", self.prompt)
        web = graph.stream("web", base="main")
        web.kernel("independent", self.prompt)
        web.wait_event(failed)
        web.kernel("dependent", self.prompt)

        execution = graph.run()
        states = {
            name: result.state for name, result in execution.kernels.items()
        }
        self.assertEqual(execution.state, RunState.failed)
        self.assertEqual(states["api/good"], KernelState.succeeded)
        self.assertEqual(states["api/fail"], KernelState.failed)
        self.assertEqual(states["api/downstream"], KernelState.skipped)
        self.assertEqual(states["web/independent"], KernelState.succeeded)
        self.assertEqual(states["web/dependent"], KernelState.skipped)
        skipped = execution.kernels["web/dependent"]
        self.assertIsNotNone(skipped.error)
        self.assertEqual(skipped.error.code, "dependency")

    def test_cancel_marks_running_and_pending(self) -> None:
        backend = FakeBackend(hang={"api/slow"})
        graph = self.graph(_backend=backend)
        api = graph.stream("api", base="main")
        api.kernel("slow", self.prompt)
        api.kernel("next", self.prompt)

        execution = graph.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if execution.kernels["api/slow"].terminal:
                break
            time.sleep(0.02)
        execution.cancel(grace_s=0.2)

        self.assertEqual(execution.state, RunState.canceled)
        self.assertEqual(
            execution.kernels["api/slow"].state, KernelState.canceled
        )
        self.assertEqual(
            execution.kernels["api/next"].state, KernelState.skipped
        )
        self.assertEqual(backend.terminals, {})

    def test_blocked_then_resolved_succeeds(self) -> None:
        backend = FakeBackend(block_once={"api/blocked"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("blocked", self.prompt)

        execution = graph.run()
        result = execution.kernels["api/blocked"]
        self.assertEqual(execution.state, RunState.succeeded)
        self.assertEqual(result.state, KernelState.succeeded)
        self.assertEqual(result.status, "idle")

    def test_unreported_status_fails_fast(self) -> None:
        backend = FakeBackend(not_reported={"api/a"})
        graph = self.graph(_backend=backend, ready_timeout_s=0.4)
        graph.stream("api", base="main").kernel("a", self.prompt)

        execution = graph.run()
        result = execution.kernels["api/a"]
        self.assertEqual(execution.state, RunState.failed)
        self.assertEqual(result.state, KernelState.failed)
        self.assertEqual(result.error.code, "status_unavailable")

    def test_agent_not_started(self) -> None:
        backend = FakeBackend(not_started={"api/a"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)

        with mock.patch("herda.session.START_OBSERVE_S", 0.3):
            execution = graph.run()
        result = execution.kernels["api/a"]
        self.assertEqual(result.state, KernelState.failed)
        self.assertEqual(result.error.code, "agent_not_started")

    def test_prompt_delivery_failure(self) -> None:
        backend = FakeBackend(fail_send={"api/a"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)

        execution = graph.run()
        self.assertEqual(
            execution.kernels["api/a"].error.code, "prompt_delivery"
        )

    def test_lost_session_is_a_failure(self) -> None:
        backend = FakeBackend(lost={"api/a"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)

        execution = graph.run()
        result = execution.kernels["api/a"]
        self.assertEqual(execution.state, RunState.failed)
        self.assertEqual(result.state, KernelState.failed)
        self.assertEqual(result.error.code, "session_lost")

    def test_large_prompt_uses_a_file_instruction(self) -> None:
        large = self._prompt("large.md", "x" * (49 * 1024))
        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("large", large)

        execution = graph.run()
        self.assertEqual(execution.state, RunState.succeeded)
        sent = backend.sent_texts[0][1]
        self.assertTrue(sent.startswith("Read the instructions in "))
        worktree = graph.streams["api"].worktree
        assert worktree is not None
        self.assertTrue(
            (worktree / ".herda" / "prompts" / "api" / "large.md").exists()
        )

    def test_wait_timeout_does_not_stop_the_run(self) -> None:
        backend = FakeBackend(hang={"api/a"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.start()
        try:
            with self.assertRaises(HerdaTimeoutError):
                execution.wait(0.1)
            self.assertEqual(execution.state, RunState.running)
        finally:
            execution.cancel(grace_s=0.2)

    def test_query_and_stream_synchronize(self) -> None:
        backend = FakeBackend(hang={"api/slow"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("slow", self.prompt)
        graph.stream("web", base="main").kernel("quick", self.prompt)

        execution = graph.start()
        try:
            execution.wait_stream("web", timeout_s=3.0)
            self.assertTrue(execution.query_stream("web"))
            self.assertFalse(execution.query_stream("api"))
            self.assertFalse(execution.query())
        finally:
            execution.cancel(grace_s=0.2)
        self.assertTrue(execution.query())
        self.assertTrue(execution.query_stream("api"))
        self.assertIs(execution.synchronize(), execution)

    def test_cleanup_refuses_an_active_run(self) -> None:
        backend = FakeBackend(hang={"api/a"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.start()
        try:
            with self.assertRaises(CleanupError):
                graph.cleanup(execution.id, worktrees=True)
        finally:
            execution.cancel(grace_s=0.2)

    def test_second_start_is_rejected(self) -> None:
        graph = self.graph()
        graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.run()
        self.assertEqual(execution.state, RunState.succeeded)
        with self.assertRaises(ValidationError):
            graph.start()

    def test_cleanup_refuses_dirty_worktree(self) -> None:
        graph = self.graph()
        graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.run()
        worktree = graph.streams["api"].worktree
        assert worktree is not None
        (worktree / "dirty.txt").write_text("local work\n", encoding="utf-8")

        with self.assertRaises(CleanupError):
            graph.cleanup(execution.id, worktrees=True)
        self.assertTrue((worktree / "dirty.txt").exists())

        graph.cleanup(execution.id, worktrees=True, force=True)
        self.assertFalse(worktree.exists())


class CliTests(RepoCase):
    def test_draw_resolves_output_before_chdir(self) -> None:
        graph_py = os.path.join(self.root, "graph.py")
        with open(graph_py, "w", encoding="utf-8") as fh:
            fh.write(
                "from herda import Graph\n"
                "graph = Graph('cli', 'repo', "
                "default_model='provider/model')\n"
                "graph.stream('api').kernel('a', 'prompts/prompt.md')\n"
            )
        output = "dag.mmd"
        old_cwd = os.getcwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli_main(["draw", graph_py, "-o", output])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, output)))


class PreflightTests(RepoCase):
    def test_missing_hc_and_pi_stop_before_worktrees(self) -> None:
        graph = Graph(
            "auth",
            self.repo,
            default_model="anthropic/claude-sonnet-5-5",
            base_dir=self.root,
        )
        graph.stream("api", base="main").kernel("a", self.prompt)
        missing = os.path.join(self.root, "missing")
        with mock.patch.dict(
            os.environ,
            {
                "HERDA_HC": os.path.join(missing, "hc"),
                "HERDA_PI": os.path.join(missing, "pi"),
            },
        ):
            with self.assertLogs("herda", level="ERROR"):
                with self.assertRaises(HerdrError) as caught:
                    graph.start()
        self.assertEqual(caught.exception.code, "missing_dependency")
        self.assertFalse(
            os.path.exists(os.path.join(self.repo, ".herda", "worktrees"))
        )

    def test_backend_preflight_names_both_tools(self) -> None:
        missing = os.path.join(self.root, "missing")
        with mock.patch.dict(
            os.environ,
            {
                "HERDA_HC": os.path.join(missing, "hc"),
                "HERDA_PI": os.path.join(missing, "pi"),
            },
        ):
            with self.assertLogs("herda", level="ERROR") as logs:
                with self.assertRaises(HerdrError) as caught:
                    HcBackend().preflight()
        self.assertEqual(caught.exception.code, "missing_dependency")
        text = "\n".join(logs.output)
        self.assertIn("hc", text)
        self.assertIn("pi", text)

    def test_pi_model_argv(self) -> None:
        backend = HcBackend()
        argv, env = backend.launch_argv(
            "provider/model", inherit_session=True
        )
        self.assertEqual(
            argv,
            [
                "pi",
                "--no-approve",
                "--model",
                "provider/model",
                "--continue",
            ],
        )
        self.assertEqual(env, {"PI_PROGRAM_STATUS": "1"})

        argv, env = backend.launch_argv(
            "provider/model", inherit_session=False
        )
        self.assertEqual(
            argv, ["pi", "--no-approve", "--model", "provider/model"]
        )
        self.assertEqual(env, {"PI_PROGRAM_STATUS": "1"})

    def test_concurrent_run_lock(self) -> None:
        graph = self.graph(_backend=FakeBackend(hang={"api/slow"}))
        graph.stream("api", base="main").kernel("slow", self.prompt)
        execution = graph.start()
        try:
            other = Graph(
                "auth",
                self.repo,
                default_model="anthropic/claude-sonnet-5-5",
                base_dir=self.root,
                _backend=FakeBackend(),
            )
            other.stream("api", base="main").kernel("a", self.prompt)
            with self.assertRaises(ConcurrentRunError):
                other.start()
        finally:
            execution.cancel(grace_s=0.2)


if __name__ == "__main__":
    unittest.main()

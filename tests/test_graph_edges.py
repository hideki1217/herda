"""Graph/Stream/Execution branches not exercised by the end-to-end tests."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from herda import (
    Event,
    Execution,
    Graph,
    HerdaTimeoutError,
    HerdrError,
    RunState,
    ValidationError,
)

from .fake_backend import FakeBackend
from .test_herda import RepoCase, _git


class ExplodingBackend(FakeBackend):
    def __init__(self, *, fail: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_ops = fail

    def interrupt(self, terminal: str, *, timeout_s: float = 10.0) -> None:
        if "interrupt" in self.fail_ops:
            raise HerdrError("interrupt", "boom")
        return super().interrupt(terminal, timeout_s=timeout_s)

    def kill(self, terminal: str) -> None:
        if "kill" in self.fail_ops:
            raise HerdrError("kill", "boom")
        return super().kill(terminal)

    def rm(self, terminal: str) -> None:
        if "rm" in self.fail_ops:
            raise HerdrError("rm", "boom")
        return super().rm(terminal)


class GraphEdgeTests(RepoCase):
    def test_constructor_validation(self) -> None:
        with self.assertRaises(ValidationError):
            Graph("g", self.repo, default_model="")
        with self.assertRaises(ValidationError):
            Graph("g", self.repo, default_model="m", max_parallel=0)
        with self.assertRaises(ValidationError):
            Graph("g", self.repo, default_model="m", ready_timeout_s=0)
        with self.assertRaises(ValidationError):
            Graph(
                "g",
                self.repo,
                default_model="m",
                session_timeout_s=0,
            )
        with self.assertRaises(ValidationError):
            Graph(
                "g",
                self.repo,
                default_model="m",
                blocked_timeout_s=0,
            )
        with self.assertRaises(ValidationError):
            Graph(
                "g",
                self.repo,
                default_model="m",
                status_timeout_s=float("nan"),
            )
        with self.assertRaises(ValidationError):
            Graph(
                "g",
                self.repo,
                default_model="m",
                agent_args="--x",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValidationError):
            Graph(
                "g",
                self.repo,
                default_model="m",
                env=[],  # type: ignore[arg-type]
            )
        graph = self.graph(
            agent_args=("--no-extensions",),
            env={"A": "1", "B": "2"},
        )
        kernel = graph.stream("api").kernel(
            "a",
            self.prompt,
            agent_args=("--thinking", "high"),
            env={"B": "3"},
        )
        self.assertEqual(kernel.agent_args, ("--thinking", "high"))
        self.assertEqual(kernel.env, {"A": "1", "B": "3"})

    def test_stream_after_start_and_slug_collision(self) -> None:
        graph = self.graph()
        graph._start_attempted = True
        with self.assertRaises(ValidationError):
            graph.stream("late")
        graph = self.graph()
        first = graph.stream("A B")
        second = graph.stream("a-b")
        self.assertNotEqual(
            graph._stream_slugs[first.name], graph._stream_slugs[second.name]
        )
        self.assertTrue(graph._stream_slugs[second.name].startswith("a-b-"))

    def test_validate_private_invariant_errors(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        kernel = stream.kernel("a", self.prompt)
        object.__setattr__(kernel, "model", "")
        with mock.patch("herda.graph.load_prompt"):
            with self.assertRaises(ValidationError):
                graph.validate()

        graph = self.graph()
        stream = graph.stream("api")
        first = stream.kernel("a", self.prompt)
        second = stream.kernel("b", self.prompt)
        other = self.graph()
        foreign = other.stream("api").kernel("x", self.prompt)
        graph._kernel_dependencies[second.qualified_name] = (foreign,)
        with mock.patch("herda.graph.require_git_repo"), mock.patch(
            "herda.graph.resolve_commit"
        ):
            with self.assertRaises(ValidationError):
                graph.validate()

        graph = self.graph()
        kernel = graph.stream("api").kernel("a", self.prompt)
        object.__setattr__(kernel, "inherit_session", True)
        with mock.patch("herda.graph.require_git_repo"), mock.patch(
            "herda.graph.resolve_commit"
        ):
            with self.assertRaises(ValidationError):
                graph.validate()

        graph = self.graph()
        stream = graph.stream("api")
        first = stream.kernel("a", self.prompt)
        second = stream.kernel("b", self.prompt)
        graph._kernel_dependencies[first.qualified_name] = (second,)
        graph._kernel_dependencies[second.qualified_name] = (first,)
        with mock.patch("herda.graph.require_git_repo"), mock.patch(
            "herda.graph.resolve_commit"
        ):
            with self.assertRaises(ValidationError):
                graph.validate()

    def test_validate_models(self) -> None:
        graph = self.graph()
        graph.stream("api").kernel("a", self.prompt)
        with mock.patch(
            "herda.graph.PiAgent.list_models", return_value=None
        ):
            with self.assertRaises(ValidationError) as caught:
                graph.validate_models()
        self.assertEqual(caught.exception.code, "model_list_unavailable")

        with mock.patch(
            "herda.graph.PiAgent.list_models",
            return_value={"other/model"},
        ):
            with self.assertRaises(ValidationError) as caught:
                graph.validate_models()
        self.assertEqual(caught.exception.code, "model_unknown")

        with mock.patch(
            "herda.graph.PiAgent.list_models",
            return_value={"anthropic/claude-sonnet-5-5"},
        ):
            graph.validate_models()

    def test_previous_kernel_and_repr(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        first = stream.kernel("a", self.prompt)
        second = stream.kernel("b", self.prompt)
        self.assertIsNone(graph._previous_kernel(first))
        self.assertIs(graph._previous_kernel(second), first)
        self.assertIn("Graph('auth'", repr(graph))

    def test_start_rolls_back_after_preflight(self) -> None:
        graph = self.graph()
        graph.stream("api", base="main").kernel("a", self.prompt)
        with mock.patch(
            "herda.graph.RunStore.prompt_snapshot_path",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                graph.start()
        worktrees = _git(self.repo, "worktree", "list").stdout
        self.assertEqual(len(worktrees.strip().splitlines()), 1)
        branches = _git(self.repo, "branch", "--list", "herda/*").stdout
        self.assertEqual(branches.strip(), "")

        # A failed start before a run began must not poison the Graph object.
        execution = graph.start()
        self.assertEqual(execution.wait().state, RunState.succeeded)

    def test_prepare_dir_chmod_error(self) -> None:
        graph = self.graph()
        target = Path(self.root) / "herda-dir"
        with mock.patch("pathlib.Path.chmod", side_effect=OSError("no")):
            graph._prepare_herda_dir(target)
        self.assertTrue(target.is_dir())

    def test_rollback_swallows_errors(self) -> None:
        graph = self.graph()
        path = Path(self.root) / "wt"
        with mock.patch(
            "herda.graph.remove_engine_dir", side_effect=RuntimeError("no")
        ), mock.patch(
            "herda.graph.delete_branch", side_effect=RuntimeError("no")
        ):
            graph._rollback([(path, "b")])
        with mock.patch("herda.graph.remove_engine_dir"), mock.patch(
            "herda.graph.remove_worktree", side_effect=RuntimeError("no")
        ), mock.patch("herda.graph.delete_branch"):
            graph._rollback([(path, "b")])

    def test_app_sync_thread(self) -> None:
        graph = self.graph()
        with mock.patch(
            "herda.graph.app.available", return_value=True
        ), mock.patch("herda.graph.app.rename") as rename, mock.patch(
            "herda.graph.threading.Thread"
        ) as thread:
            thread.return_value.start.side_effect = (
                lambda: thread.call_args.kwargs["target"]()
            )
            graph._start_app_sync()
            graph._start_app_sync()
        rename.assert_called_once()


class StreamEdgeTests(RepoCase):
    def test_stream_validation(self) -> None:
        graph = self.graph()
        with self.assertRaises(ValidationError):
            graph.stream("api", base="")
        graph._start_attempted = True
        stream = graph.streams["api"] if "api" in graph.streams else None
        self.assertIsNone(stream)
        graph._start_attempted = False
        stream = graph.stream("api")
        graph._start_attempted = True
        with self.assertRaises(ValidationError):
            stream.kernel("a", self.prompt)
        with self.assertRaises(ValidationError):
            stream.record_event("e")
        with self.assertRaises(ValidationError):
            stream.wait_event(
                Event(graph=graph, stream=stream, name="e", after=None)
            )
        with self.assertRaises(ValidationError):
            stream.wait_stream(stream)

    def test_kernel_validation(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        with self.assertRaises(ValidationError):
            stream.kernel("a", self.prompt, model="")
        with self.assertRaises(ValidationError):
            stream.kernel("b", self.prompt, timeout_s=0)
        with self.assertRaises(ValidationError):
            stream.kernel("c", self.prompt, timeout_s=float("inf"))
        with self.assertRaises(ValidationError):
            stream.kernel("d", self.prompt, agent_args="--x")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            stream.kernel("e", self.prompt, env=[])  # type: ignore[arg-type]

    def test_event_validation_and_properties(self) -> None:
        graph = self.graph()
        stream = graph.stream("api")
        stream.kernel("a", self.prompt)
        stream.record_event("event-2")
        event = stream.record_event()
        self.assertEqual(event.name, "event-3")
        self.assertEqual(stream.recorded_events[-1], event)
        with self.assertRaises(ValidationError):
            stream.wait_event(object())  # type: ignore[arg-type]
        fabricated = Event(
            graph=graph, stream=stream, name="missing", after=None
        )
        with self.assertRaises(ValidationError):
            stream.wait_event(fabricated)
        with self.assertRaises(ValidationError):
            stream.wait_stream(object())  # type: ignore[arg-type]

    def test_wait_stream_empty_and_self(self) -> None:
        graph = self.graph()
        empty = graph.stream("empty")
        stream = graph.stream("api")
        first = stream.kernel("a", self.prompt)
        stream.wait_stream(stream)
        stream.wait_stream(empty)
        second = stream.kernel("b", self.prompt)
        self.assertEqual(graph._dependencies_of(second), (first,))

    def test_kernel_repr(self) -> None:
        graph = self.graph()
        kernel = graph.stream("api").kernel("a", self.prompt)
        self.assertEqual(repr(kernel), "Kernel('api/a')")


class ExecutionEdgeTests(RepoCase):
    def test_wait_stream_edges(self) -> None:
        backend = FakeBackend(hang={"api/slow"}, delay={"web/quick": 0.3})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("slow", self.prompt)
        graph.stream("web", base="main").kernel("quick", self.prompt)
        execution = graph.start()
        try:
            execution.wait_stream("web")  # no timeout: covers polling branch
            execution.wait_stream(graph.streams["web"])
            self.assertTrue(execution.query_stream("web"))
            with self.assertRaises(HerdaTimeoutError):
                execution.wait_stream("api", timeout_s=0.05)
            other = self.graph()
            foreign = other.stream("api")
            with self.assertRaises(ValidationError):
                execution.wait_stream(foreign)
            with self.assertRaises(ValidationError):
                execution.wait_stream("missing")
            with self.assertRaises(ValidationError):
                execution.wait_stream(123)  # type: ignore[arg-type]
        finally:
            execution.cancel(grace_s=0.2)
        execution.cancel()  # already terminal
        self.assertIn("Execution(", repr(execution))

    def test_cancel_swallows_backend_errors(self) -> None:
        for operation in ("interrupt", "kill", "rm"):
            backend = ExplodingBackend(fail={operation}, hang={"api/slow"})
            graph = self.graph(_backend=backend)
            graph.stream("api", base="main").kernel("slow", self.prompt)
            execution = graph.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if execution.kernels["api/slow"].terminal:
                    break
                time.sleep(0.02)
            execution.cancel(grace_s=0.05)
            self.assertEqual(execution.state, RunState.canceled)

    def test_max_parallel_one(self) -> None:
        backend = FakeBackend()
        graph = self.graph(_backend=backend, max_parallel=1)
        graph.stream("api", base="main").kernel("a", self.prompt)
        graph.stream("web", base="main").kernel("b", self.prompt)
        execution = graph.run()
        self.assertEqual(execution.state, RunState.succeeded)

    def test_scheduler_deadlock_branch(self) -> None:
        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)
        with mock.patch.object(
            Execution, "_propagate_skips_locked", lambda self: None
        ):
            execution = graph.start()
            execution.synchronize()
        self.assertEqual(execution.state, RunState.failed)
        self.assertEqual(
            execution.kernels["api/a"].error.code, "internal"
        )

    def test_scheduler_crash_branch(self) -> None:
        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)
        with mock.patch.object(
            Execution,
            "_propagate_skips_locked",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("herda", level="ERROR"):
                execution = graph.start()
                execution.synchronize()
        self.assertEqual(execution.state, RunState.failed)
        self.assertEqual(
            execution.kernels["api/a"].error.code, "internal"
        )

    def test_worker_crash_marks_kernel_failed(self) -> None:
        class Runner:
            terminal = "t1"
            status = "working"

            def run(self):
                raise RuntimeError("boom")

        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)
        with mock.patch(
            "herda.execution.SessionRunner", return_value=Runner()
        ):
            with self.assertLogs("herda", level="ERROR"):
                execution = graph.start()
                execution.synchronize()
        result = execution.kernels["api/a"]
        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.error.code, "internal")

    def test_on_state_is_deduped(self) -> None:
        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        kernel = graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.run()
        result = execution.kernels["api/a"]
        execution._on_state(kernel, result.state, result.status)

    def test_cancel_logs_alive_workers(self) -> None:
        class AliveThread:
            name = "alive"
            ident = 1

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        class LateThread:
            name = "late"
            ident = None

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

        backend = FakeBackend(hang={"api/slow"})
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("slow", self.prompt)
        execution = graph.start()
        execution._threads.append(AliveThread())  # type: ignore[arg-type]
        execution._threads.append(LateThread())  # type: ignore[arg-type]
        clock = {"now": 0.0}

        def monotonic() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        fake_time = SimpleNamespace(
            monotonic=monotonic, sleep=sleep
        )
        with mock.patch(
            "herda.execution.time", fake_time
        ), self.assertLogs("herda", level="WARNING"):
            execution.cancel(grace_s=0)
        self.assertEqual(execution.state, RunState.canceled)

    def test_scheduler_logs_alive_workers(self) -> None:
        class AliveThread:
            name = "alive"
            ident = 1

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        backend = FakeBackend()
        graph = self.graph(_backend=backend)
        graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.run()
        assert execution._scheduler is not None
        execution._scheduler.join()
        execution._threads.append(AliveThread())  # type: ignore[arg-type]
        fake_time = SimpleNamespace(
            monotonic=mock.Mock(side_effect=[0] + [1000] * 10)
        )
        with mock.patch(
            "herda.execution.time", fake_time
        ), self.assertLogs("herda", level="WARNING"):
            execution._schedule()

    def test_persist_oserror(self) -> None:
        graph = self.graph()
        graph.stream("api", base="main").kernel("a", self.prompt)
        execution = graph.run()
        with mock.patch.object(
            execution._store, "write_run", side_effect=OSError("disk")
        ):
            with self.assertLogs("herda", level="ERROR"):
                execution._persist()

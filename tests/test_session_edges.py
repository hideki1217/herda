"""Edge-case tests for SessionRunner's OSC 7501 state machine."""

from __future__ import annotations

import threading
import unittest
from unittest import mock
from pathlib import Path
from herda.backend import SendOutcome, TerminalInfo, WaitOutcome
from herda.errors import HerdrError
from herda.session import SessionRunner
from herda.store import RunStore
from herda.types import KernelState

from .test_herda import RepoCase


def info(
    status: str | None = "idle",
    *,
    turn: int = 0,
    reported: bool = True,
    state: str = "live",
    reason: str | None = None,
) -> TerminalInfo:
    return TerminalInfo(
        address="t1",
        state=state,
        status=status,
        turn=turn,
        reported=reported,
        reason=reason,
    )


def wait_outcome(
    status: str | None, turn: int, *, ended: bool = True, reason: str = "status"
):
    return WaitOutcome(
        ended=ended, reason=reason, status=status, turn=turn
    )


class ScriptedBackend:
    def __init__(
        self,
        *,
        gets: list[TerminalInfo | Exception] | None = None,
        send_result: SendOutcome | Exception | None = None,
        run_error: Exception | None = None,
        read_error: Exception | None = None,
        label_error: Exception | None = None,
        kill_error: Exception | None = None,
        rm_error: Exception | None = None,
        use_launch_argv: bool = False,
    ) -> None:
        self.gets = list(gets or [])
        self.send_result = send_result
        self.run_error = run_error
        self.read_error = read_error
        self.label_error = label_error
        self.kill_error = kill_error
        self.rm_error = rm_error
        self.run_calls: list[dict] = []
        self.launch_calls: list[tuple[str, bool]] = []
        self.killed: list[str] = []
        self.rm_calls: list[str] = []
        if use_launch_argv:
            self.launch_argv = self._launch_argv  # type: ignore[method-assign]
        else:
            self.launch_argv = None  # type: ignore[assignment]

    def _launch_argv(
        self,
        model: str,
        *,
        inherit_session: bool,
        session_dir=None,
        extra_args=(),
        env=None,
    ):
        self.launch_calls.append((model, inherit_session))
        return ["pi", "--from-launch"], {"EXTRA": "1"}

    def preflight(self) -> None:
        return None

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        if self.run_error is not None:
            raise self.run_error
        return "t1"

    def get(self, terminal: str) -> TerminalInfo:
        if self.gets:
            item = self.gets.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return info()

    def send(self, terminal: str, text: str, *, timeout_s: float, wait: bool):
        if isinstance(self.send_result, Exception):
            raise self.send_result
        if self.send_result is not None:
            return self.send_result
        return SendOutcome(
            started=True,
            status="working",
            turn=0,
            wait=wait_outcome("idle", 1),
        )

    def read(self, terminal: str, *, lines: int = 1000) -> str:
        if self.read_error is not None:
            raise self.read_error
        return "screen"

    def label(self, terminal: str, labels: dict[str, str]) -> None:
        if self.label_error is not None:
            raise self.label_error
        return None

    def kill(self, terminal: str) -> None:
        self.killed.append(terminal)
        if self.kill_error is not None:
            raise self.kill_error

    def rm(self, terminal: str) -> None:
        self.rm_calls.append(terminal)
        if self.rm_error is not None:
            raise self.rm_error

    def interrupt(self, terminal: str, *, timeout_s: float = 10.0) -> None:
        return None

    def list(self, labels=None):
        return []


class SessionEdgeTests(RepoCase):
    def make_runner(
        self,
        backend: ScriptedBackend,
        *,
        inherit: bool = False,
        session_timeout: float | None = None,
        ready_timeout: float = 0.15,
        blocked_timeout: float | None = 3600.0,
        status_timeout: float | None = 300.0,
        worktree: bool = True,
        large: Path | None = None,
    ):
        graph = self.graph(
            _backend=backend,
            blocked_timeout_s=blocked_timeout,
            status_timeout_s=status_timeout,
        )
        stream = graph.stream("api", base="main")
        if inherit:
            stream.kernel("prev", self.prompt)
        kernel = stream.kernel(
            "k",
            self.prompt,
            inherit_session=inherit,
            timeout_s=session_timeout,
        )
        if worktree:
            path = Path(self.root) / "wt"
            path.mkdir(exist_ok=True)
            stream.worktree = path
            stream.branch = "branch"
        graph.terminal_names[kernel.qualified_name] = "terminal-name"
        store = RunStore(Path(self.root) / ".herda", "run")
        runner = SessionRunner(
            kernel=kernel,
            backend=backend,
            store=store,
            cancel_event=threading.Event(),
            ready_timeout_s=ready_timeout,
            session_timeout_s=session_timeout,
            blocked_timeout_s=graph.blocked_timeout_s,
            status_timeout_s=graph.status_timeout_s,
            branches={"api": "branch"},
            prompt_text="prompt",
            prompt_sha256="sha",
            large_prompt_path=large,
            on_terminal=lambda terminal: None,
            on_state=lambda state, status: None,
        )
        return runner, kernel, graph

    def test_cancel_wins_over_failure(self) -> None:
        backend = ScriptedBackend(run_error=HerdrError("hc_failed", "boom"))
        runner, _, _ = self.make_runner(backend)
        runner.cancel_event.set()
        outcome = runner.run()
        self.assertEqual(outcome.state, KernelState.canceled)
        self.assertEqual(outcome.error.code, "canceled")

    def test_internal_crash(self) -> None:
        backend = ScriptedBackend(run_error=RuntimeError("boom"))
        runner, _, _ = self.make_runner(backend)
        with self.assertLogs("herda", level="ERROR"):
            outcome = runner.run()
        self.assertEqual(outcome.state, KernelState.failed)
        self.assertEqual(outcome.error.code, "internal")

    def test_kill_and_rm_errors_are_ignored(self) -> None:
        backend = ScriptedBackend(
            kill_error=HerdrError("kill", "no"),
            rm_error=HerdrError("rm", "no"),
        )
        runner, _, _ = self.make_runner(backend)
        outcome = runner.run()
        self.assertEqual(outcome.state, KernelState.succeeded)
        self.assertEqual(backend.killed, ["t1"])

    def test_missing_worktree(self) -> None:
        runner, _, _ = self.make_runner(ScriptedBackend(), worktree=False)
        outcome = runner.run()
        self.assertEqual(outcome.error.code, "internal")

    def test_inherit_and_launch_argv(self) -> None:
        backend = ScriptedBackend(use_launch_argv=True)
        runner, _, _ = self.make_runner(backend, inherit=True)
        outcome = runner.run()
        self.assertEqual(outcome.state, KernelState.succeeded)
        self.assertTrue(backend.launch_calls[0][1])
        self.assertEqual(backend.run_calls[0]["argv"], ["pi", "--from-launch"])
        self.assertEqual(backend.run_calls[0]["env"]["EXTRA"], "1")

    def test_run_herdr_error(self) -> None:
        backend = ScriptedBackend(run_error=HerdrError("hc_failed", "boom"))
        runner, _, _ = self.make_runner(backend)
        outcome = runner.run()
        self.assertEqual(outcome.state, KernelState.failed)
        self.assertEqual(outcome.error.code, "herdr")

    def test_ready_finished_error_blocked_and_timeout(self) -> None:
        finished = ScriptedBackend(gets=[info(state="finished")])
        self.assertEqual(
            self.make_runner(finished)[0].run().error.code, "session_lost"
        )

        error = ScriptedBackend(gets=[info("error")])
        self.assertEqual(
            self.make_runner(error)[0].run().error.code, "agent_not_ready"
        )

        blocked = ScriptedBackend(gets=[info("blocked"), info("idle")])
        self.assertEqual(
            self.make_runner(blocked)[0].run().state, KernelState.succeeded
        )

        unblocked = ScriptedBackend(
            gets=[info("blocked"), info("working"), info("idle")]
        )
        self.assertEqual(
            self.make_runner(
                unblocked, ready_timeout=5
            )[0].run().state,
            KernelState.succeeded,
        )

        working = ScriptedBackend(gets=[info("working"), info("working")])
        self.assertEqual(
            self.make_runner(working, ready_timeout=0.01)[0].run().error.code,
            "agent_not_ready",
        )

    def test_send_started_unknown(self) -> None:
        backend = ScriptedBackend(
            send_result=SendOutcome(None, "idle", 0, None)
        )
        outcome = self.make_runner(backend)[0].run()
        self.assertEqual(outcome.error.code, "status_unavailable")

    def test_observe_started_paths(self) -> None:
        timeout_backend = ScriptedBackend(
            send_result=SendOutcome(
                False, "idle", 0, wait_outcome("idle", 0, reason="not_started")
            )
        )
        outcome = self.make_runner(
            timeout_backend, session_timeout=0.001
        )[0].run()
        self.assertEqual(outcome.error.code, "timeout")

        finished_backend = ScriptedBackend(
            gets=[info("idle"), info(state="finished")],
            send_result=SendOutcome(
                False, "idle", 0, wait_outcome("idle", 0, reason="not_started")
            ),
        )
        outcome = self.make_runner(finished_backend)[0].run()
        self.assertEqual(outcome.error.code, "session_lost")

        working_backend = ScriptedBackend(
            gets=[info("idle"), info("working", turn=1)],
            send_result=SendOutcome(
                False, "idle", 0, wait_outcome("idle", 0, reason="not_started")
            ),
        )
        outcome = self.make_runner(working_backend)[0].run()
        self.assertEqual(outcome.error.code, "agent_not_started")

    def test_evaluate_wait_paths(self) -> None:
        not_started = ScriptedBackend(
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("idle", 0, reason="not_started")
            )
        )
        self.assertEqual(
            self.make_runner(not_started)[0].run().error.code,
            "agent_not_started",
        )
        exited = ScriptedBackend(
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("idle", 0, reason="exit")
            )
        )
        self.assertEqual(
            self.make_runner(exited)[0].run().error.code, "session_lost"
        )
        blocked = ScriptedBackend(
            gets=[info("idle"), info("idle", turn=1)],
            send_result=SendOutcome(True, "working", 0, wait_outcome("blocked", 0)),
        )
        self.assertEqual(
            self.make_runner(blocked)[0].run().state, KernelState.succeeded
        )

    def test_completion_paths(self) -> None:
        finished_while_blocked = ScriptedBackend(
            gets=[info("idle"), info("blocked"), info(state="finished")],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        self.assertEqual(
            self.make_runner(finished_while_blocked)[0].run().error.code,
            "session_lost",
        )

        working = ScriptedBackend(
            gets=[info("idle"), info("working"), info("idle", turn=1)],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        self.assertEqual(
            self.make_runner(working)[0].run().state, KernelState.succeeded
        )

        error = ScriptedBackend(
            gets=[info("idle"), info("error")],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        self.assertEqual(
            self.make_runner(error)[0].run().error.code, "herdr"
        )

        not_advanced = ScriptedBackend(
            gets=[info("idle"), info("idle", turn=0)],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        self.assertEqual(
            self.make_runner(not_advanced)[0].run().error.code,
            "agent_not_started",
        )

    def test_completion_and_blocked_timeouts(self) -> None:
        completion_timeout = ScriptedBackend(
            gets=[info("idle"), info("working"), info("working")],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        outcome2 = self.make_runner(
            completion_timeout, session_timeout=0.001
        )[0].run()
        self.assertEqual(outcome2.error.code, "timeout")

        blocked_timeout = ScriptedBackend(
            gets=[
                info("idle"),
                info("blocked"),
                info("blocked"),
                info("blocked"),
            ],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        outcome = self.make_runner(
            blocked_timeout, session_timeout=0.001
        )[0].run()
        self.assertEqual(outcome.error.code, "timeout")

    def test_status_and_blocked_timeouts(self) -> None:
        ready_status = ScriptedBackend(
            gets=[info("working"), info(reported=False)]
        )
        self.assertEqual(
            self.make_runner(
                ready_status,
                ready_timeout=5,
                status_timeout=1e-9,
            )[0].run().error.code,
            "status_timeout",
        )

        ready_blocked = ScriptedBackend(
            gets=[info("blocked"), info("blocked")]
        )
        self.assertEqual(
            self.make_runner(
                ready_blocked,
                ready_timeout=5,
                blocked_timeout=1e-9,
                status_timeout=None,
            )[0].run().error.code,
            "blocked_timeout",
        )

        completion_status = ScriptedBackend(
            gets=[
                info("idle"),
                info("working", turn=1),
                info(reported=False),
            ],
            send_result=SendOutcome(
                True, "working", 0, wait_outcome("working", 0, ended=False)
            ),
        )
        self.assertEqual(
            self.make_runner(
                completion_status, status_timeout=1e-9
            )[0].run().error.code,
            "status_timeout",
        )

    def test_finalization_errors_do_not_skip_reaping(self) -> None:
        backend = ScriptedBackend()
        runner, _, _ = self.make_runner(backend)
        with mock.patch.object(
            runner.store, "write_screen", side_effect=OSError("disk")
        ):
            with self.assertLogs("herda", level="WARNING"):
                outcome = runner.run()
        self.assertEqual(outcome.state, KernelState.succeeded)
        self.assertEqual(backend.killed, ["t1"])

    def test_get_errors_and_missing_terminal(self) -> None:
        not_found = ScriptedBackend(
            gets=[HerdrError("terminal_not_found", "gone")]
        )
        self.assertEqual(
            self.make_runner(not_found)[0].run().error.code, "session_lost"
        )
        other = ScriptedBackend(gets=[HerdrError("hc_failed", "bad")])
        self.assertEqual(
            self.make_runner(other)[0].run().error.code, "herdr"
        )
        runner, _, _ = self.make_runner(ScriptedBackend())
        with self.assertRaises(Exception):
            runner._terminal_required()
        self.assertIsNone(runner._save_screen())
        runner._label_state("x")

    def test_save_screen_and_label_errors(self) -> None:
        read_error = ScriptedBackend(
            read_error=HerdrError("read_failed", "no")
        )
        outcome = self.make_runner(read_error)[0].run()
        self.assertIsNone(outcome.output_path)

        label_error = ScriptedBackend(
            label_error=HerdrError("label_failed", "no")
        )
        outcome = self.make_runner(label_error)[0].run()
        self.assertEqual(outcome.state, KernelState.succeeded)

"""One kernel session: start opencode, send one prompt, wait for OSC 7501.

The state machine follows API.md §7.2 and §7.6. Nothing here scrapes the
screen; only ``hc get`` status/turn fields decide whether a turn finished.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .backend import Backend, TerminalInfo
from .errors import HerdrError
from .kernel import Kernel
from .store import RunStore
from .types import ErrorInfo, KernelState

log = logging.getLogger("herda")

LOST_REASONS = {
    "lost",
    "restarted",
    "holder_lost",
    "stopped_by_restart",
    "signaled",
}
START_OBSERVE_S = 15.0


class _Cancelled(Exception):
    pass


class _SessionFailure(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass
class SessionOutcome:
    state: KernelState
    status: str | None
    terminal: str | None
    started_at: datetime
    finished_at: datetime
    duration_s: float
    output_path: Path | None
    error: ErrorInfo | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionRunner:
    def __init__(
        self,
        *,
        kernel: Kernel,
        backend: Backend,
        store: RunStore,
        cancel_event,
        ready_timeout_s: float,
        session_timeout_s: float | None,
        branches: dict[str, str],
        prompt_text: str,
        prompt_sha256: str,
        large_prompt_path: Path | None,
        on_terminal: Callable[[str], None],
        on_state: Callable[[KernelState, str | None], None],
    ) -> None:
        self.kernel = kernel
        self.backend = backend
        self.store = store
        self.cancel_event = cancel_event
        self.ready_timeout_s = ready_timeout_s
        self.session_timeout_s = session_timeout_s
        self.branches = branches
        self.prompt_text = prompt_text
        self.prompt_sha256 = prompt_sha256
        self.large_prompt_path = large_prompt_path
        self.on_terminal = on_terminal
        self.on_state = on_state
        self.terminal: str | None = None
        self.status: str | None = None
        self._started_at = _utcnow()

    # -- entry point -----------------------------------------------------

    def run(self) -> SessionOutcome:
        started = self._started_at
        error: ErrorInfo | None = None
        state = KernelState.failed
        output_path: Path | None = None

        try:
            self._launch()
            info = self._wait_ready()
            self._label_state("ready")
            self._send_and_wait(info.turn)
            state = KernelState.succeeded
        except _Cancelled:
            state = KernelState.canceled
            error = ErrorInfo("canceled", "session was canceled")
        except _SessionFailure as exc:
            if self.cancel_event.is_set():
                state = KernelState.canceled
                error = ErrorInfo("canceled", "session was canceled")
            else:
                state = KernelState.failed
                error = ErrorInfo(exc.code, exc.message, exc.hint)
                log.warning(
                    "kernel %s failed: %s: %s",
                    self.kernel.qualified_name,
                    exc.code,
                    exc.message,
                )
        except BaseException as exc:  # noqa: BLE001 - recorded as internal
            log.exception("kernel %s crashed", self.kernel.qualified_name)
            state = KernelState.failed
            error = ErrorInfo("internal", f"{type(exc).__name__}: {exc}")

        finished = _utcnow()
        if self.terminal is not None:
            output_path = self._save_screen()
        self._write_meta(state, error, finished, output_path)

        if self.terminal is not None:
            if state == KernelState.succeeded:
                final_label = "succeeded"
            elif error is not None and error.code == "session_lost":
                final_label = "lost"
            else:
                final_label = "failed"
            self._label_state(final_label)
            try:
                self.backend.kill(self.terminal)
            except HerdrError as exc:
                log.debug("kill failed for %s: %s", self.terminal, exc)
            finally:
                try:
                    self.backend.rm(self.terminal)
                except HerdrError as exc:
                    log.debug("rm failed for %s: %s", self.terminal, exc)

        return SessionOutcome(
            state=state,
            status=self.status,
            terminal=self.terminal,
            started_at=started,
            finished_at=finished,
            duration_s=(finished - started).total_seconds(),
            output_path=output_path,
            error=error,
        )

    # -- launch ----------------------------------------------------------

    def _launch(self) -> None:
        kernel = self.kernel
        worktree = kernel.stream.worktree
        if worktree is None:
            raise _SessionFailure(
                "internal", "stream worktree has not been created"
            )
        env = {
            "HERDA_RUN_ID": self.store.run_id,
            "HERDA_GRAPH": kernel.graph.name,
            "HERDA_STREAM": kernel.stream.name,
            "HERDA_KERNEL": kernel.name,
            "HERDA_SESSION": f"{self.store.run_id}/{kernel.qualified_name}",
            "HERDA_WORKTREE": str(worktree),
            "HERDA_BRANCH": kernel.stream.branch or "",
            "HERDA_BRANCHES": _json_dumps(self.branches),
        }
        argv = ["opencode", "--model", kernel.model]
        if kernel.inherit_session:
            argv.append("--continue")
        launch = getattr(self.backend, "launch_argv", None)
        if launch is not None:
            argv, extra_env = launch(
                kernel.model, inherit_session=kernel.inherit_session
            )
            env.update(extra_env)

        labels = {
            "herdr.space": kernel.graph.space_id,
            "herdr.tab": kernel.graph.tab_ids[kernel.stream.name],
            "herda.owner": "herda",
            "herda.version": "0.1",
            "herda.graph": kernel.graph.name,
            "herda.space_name": kernel.graph.name,
            "herda.run": self.store.run_id,
            "herda.stream": kernel.stream.name,
            "herda.tab_name": kernel.stream.name,
            "herda.kernel": kernel.name,
            "herda.session": f"{self.store.run_id}/{kernel.qualified_name}",
            "herda.worktree": str(worktree),
            "herda.branch": kernel.stream.branch or "",
            "herda.model": kernel.model,
            "herda.state": "starting",
        }
        try:
            terminal = self.backend.run(
                worktree=worktree,
                argv=argv,
                env=env,
                name=kernel.graph.terminal_names[kernel.qualified_name],
                labels=labels,
            )
        except HerdrError as exc:
            raise _SessionFailure("herdr", exc.message, exc.hint) from exc

        self.terminal = terminal
        self.on_terminal(terminal)
        self._write_meta(KernelState.running, None, None, None)

    # -- readiness -------------------------------------------------------

    def _wait_ready(self) -> TerminalInfo:
        deadline = time.monotonic() + self.ready_timeout_s
        reported_any = False
        blocked_seen = False

        while True:
            self._check_cancel()
            info = self._get()
            if info.finished:
                raise _SessionFailure(
                    "session_lost",
                    f"terminal {info.address} finished before it was ready",
                )
            if info.reported:
                reported_any = True
                self.status = info.status
                if info.status in ("idle", "done"):
                    return info
                if info.status == "error":
                    raise _SessionFailure(
                        "agent_not_ready",
                        "agent reported error while starting",
                    )
                if info.status == "blocked":
                    blocked_seen = True
                    self._label_state("awaiting_input")
                    self.on_state(KernelState.awaiting_input, info.status)
            if not blocked_seen and time.monotonic() >= deadline:
                if not reported_any:
                    raise _SessionFailure(
                        "status_unavailable",
                        "no OSC 7501 status report was observed before the "
                        "ready timeout",
                        "check that the agent reports OSC 7501 and that "
                        "ready_timeout_s is long enough",
                    )
                raise _SessionFailure(
                    "agent_not_ready",
                    "agent did not become idle before the ready timeout",
                )
            time.sleep(0.25)

    # -- prompt + completion --------------------------------------------

    def _send_and_wait(self, t0: int) -> None:
        text = self._prompt_payload()
        session_deadline = (
            None
            if self.session_timeout_s is None
            else time.monotonic() + self.session_timeout_s
        )
        send_timeout = (
            max(1.0, min(180.0, session_deadline - time.monotonic()))
            if session_deadline is not None
            else 180.0
        )

        self._label_state("sending")
        self.on_state(KernelState.running, self.status)
        try:
            outcome = self.backend.send(
                self._terminal_required(),
                text,
                timeout_s=send_timeout,
                wait=True,
            )
        except HerdrError as exc:
            raise _SessionFailure(
                "prompt_delivery", exc.message, exc.hint
            ) from exc

        self.status = outcome.status or self.status
        t0 = outcome.turn
        if outcome.started is None:
            raise _SessionFailure(
                "status_unavailable",
                "hc send could not tell whether the agent started "
                "(OSC 7501 is not supported by this agent)",
            )
        if outcome.started is False:
            self._observe_started(t0, session_deadline)
        elif outcome.started is True:
            self._label_state("working")
            self.on_state(KernelState.running, self.status)

        if outcome.wait is not None and outcome.wait.ended:
            self.status = outcome.wait.status or self.status
            self._evaluate_wait(outcome.wait, t0)
            if outcome.wait.status in ("idle", "done") and outcome.wait.turn > t0:
                return

        self._wait_completion(t0, session_deadline)

    def _observe_started(
        self, t0: int, session_deadline: float | None
    ) -> None:
        deadline = time.monotonic() + START_OBSERVE_S
        while time.monotonic() < deadline:
            self._check_cancel()
            if session_deadline is not None and time.monotonic() > session_deadline:
                raise _SessionFailure("timeout", "session timeout exceeded")
            info = self._get()
            if info.finished:
                raise _SessionFailure(
                    "session_lost", "terminal finished after prompt delivery"
                )
            self.status = info.status or self.status
            if info.turn > t0 or info.status == "working":
                return
            time.sleep(0.25)
        raise _SessionFailure(
            "agent_not_started",
            "agent did not start working after the prompt was sent",
        )

    def _evaluate_wait(self, wait, t0: int) -> None:
        if wait.reason == "not_started":
            raise _SessionFailure(
                "agent_not_started",
                "agent did not start working after the prompt was sent",
            )
        if wait.reason == "exit":
            raise _SessionFailure(
                "session_lost", "terminal exited after prompt delivery"
            )
        status = wait.status
        if status == "error":
            raise _SessionFailure("herdr", "agent reported error")
        if status == "blocked":
            return

    def _wait_completion(self, t0: int, session_deadline: float | None) -> None:
        while True:
            self._check_cancel()
            info = self._get()
            if info.finished:
                raise _SessionFailure(
                    "session_lost", "terminal finished before the turn ended"
                )
            if info.reported:
                self.status = info.status
                status = info.status

                if status == "blocked":
                    self._label_state("awaiting_input")
                    self.on_state(KernelState.awaiting_input, status)
                    info = self._wait_out_of_blocked(session_deadline)
                    self.status = info.status
                    status = info.status
                    if info.finished:
                        raise _SessionFailure(
                            "session_lost",
                            "terminal finished while waiting for approval",
                        )

                if status == "working":
                    self._label_state("working")
                    self.on_state(KernelState.running, status)
                elif status == "error":
                    raise _SessionFailure("herdr", "agent reported error")
                elif info.turn > t0:
                    if status in ("idle", "done"):
                        return
                elif status in ("idle", "done"):
                    raise _SessionFailure(
                        "agent_not_started",
                        "turn did not advance after the prompt was sent",
                    )

            if (
                session_deadline is not None
                and time.monotonic() > session_deadline
            ):
                raise _SessionFailure(
                    "timeout", "session timeout exceeded"
                )
            time.sleep(0.25)

    def _wait_out_of_blocked(
        self, session_deadline: float | None
    ) -> TerminalInfo:
        while True:
            self._check_cancel()
            info = self._get()
            if info.finished:
                return info
            if info.reported and info.status != "blocked":
                return info
            if (
                session_deadline is not None
                and time.monotonic() > session_deadline
            ):
                raise _SessionFailure(
                    "timeout", "session timeout exceeded while blocked"
                )
            time.sleep(0.5)

    # -- helpers ---------------------------------------------------------

    def _prompt_payload(self) -> str:
        if self.large_prompt_path is not None:
            return (
                "Read the instructions in "
                f"{self.large_prompt_path} and follow them."
            )
        return self.prompt_text

    def _terminal_required(self) -> str:
        if self.terminal is None:
            raise _SessionFailure("internal", "no terminal address")
        return self.terminal

    def _get(self) -> TerminalInfo:
        try:
            info = self.backend.get(self._terminal_required())
        except HerdrError as exc:
            if exc.code == "terminal_not_found":
                return TerminalInfo(
                    address=self._terminal_required(),
                    state="finished",
                    status=None,
                    turn=0,
                    reported=False,
                    reason="lost",
                )
            raise _SessionFailure("herdr", exc.message, exc.hint) from exc
        if not info.finished and info.reason in LOST_REASONS:
            info.state = "finished"
        return info

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise _Cancelled()

    def _label_state(self, state: str) -> None:
        if self.terminal is None:
            return
        try:
            self.backend.label(self.terminal, {"herda.state": state})
        except HerdrError:
            log.debug("could not update state label on %s", self.terminal)

    def _save_screen(self) -> Path | None:
        if self.terminal is None:
            return None
        try:
            text = self.backend.read(self.terminal, lines=2000)
        except HerdrError as exc:
            level = (
                logging.DEBUG
                if exc.code == "terminal_not_found"
                else logging.WARNING
            )
            log.log(
                level,
                "could not save screen for %s: %s",
                self.kernel.qualified_name,
                exc,
            )
            return None
        return self.store.write_screen(
            self.kernel.stream.name, self.kernel.name, text
        )

    def _write_meta(
        self,
        state: KernelState,
        error: ErrorInfo | None,
        finished: datetime | None,
        output_path: Path | None,
    ) -> None:
        self.store.write_meta(
            self.kernel.stream.name,
            self.kernel.name,
            {
                "name": self.kernel.qualified_name,
                "state": state.value,
                "terminal": self.terminal,
                "status": self.status,
                "started_at": self._started_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "finished_at": None
                if finished is None
                else finished.isoformat().replace("+00:00", "Z"),
                "output_path": None if output_path is None else str(output_path),
                "error": None
                if error is None
                else {
                    "code": error.code,
                    "message": error.message,
                    "hint": error.hint,
                },
            },
        )


def _json_dumps(value: dict[str, str]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

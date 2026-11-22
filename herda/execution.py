"""Execution: DAG scheduler and run lifecycle."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .backend import Backend
from .errors import HerdaTimeoutError, ValidationError
from .kernel import Kernel
from .session import SessionOutcome, SessionRunner
from .store import GraphLock, RunStore, kernel_document, run_document
from .stream import Stream
from .types import ErrorInfo, KernelResult, KernelState, RunState

if TYPE_CHECKING:
    from .graph import Graph

log = logging.getLogger("herda")

_TERMINAL_RUN_STATES = {
    RunState.succeeded,
    RunState.failed,
    RunState.canceled,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Execution:
    def __init__(
        self,
        *,
        graph: "Graph",
        run_id: str,
        store: RunStore,
        lock: GraphLock,
        backend: Backend,
        streams: dict[str, dict[str, Any]],
        prompt_texts: dict[str, str],
        prompt_shas: dict[str, str],
        large_prompts: dict[str, Path],
    ) -> None:
        self.graph = graph
        self.id = run_id
        self.run_dir = store.run_dir
        self.started_at = _utcnow()
        self.finished_at: datetime | None = None
        self._state = RunState.running
        self._store = store
        self._lock = lock
        self._backend = backend
        self._streams = streams
        self._prompt_texts = prompt_texts
        self._prompt_shas = prompt_shas
        self._large_prompts = large_prompts
        self._branches = {
            name: str(info["branch"]) for name, info in streams.items()
        }
        self._cancel_event = threading.Event()
        self._cond = threading.Condition(threading.RLock())
        self._threads: list[threading.Thread] = []
        self._finished = False
        self._scheduler: threading.Thread | None = None
        self._launch_cursor = 0

        self._results: dict[str, KernelResult] = {}
        for kernel in graph.kernels.values():
            info = streams[kernel.stream.name]
            self._results[kernel.qualified_name] = KernelResult(
                name=kernel.qualified_name,
                stream=kernel.stream.name,
                state=KernelState.pending,
                model=kernel.model,
                terminal=None,
                branch=str(info["branch"]),
                worktree=Path(info["worktree"]),
                prompt_sha256=prompt_shas[kernel.qualified_name],
                status=None,
                started_at=None,
                finished_at=None,
                duration_s=None,
                output_path=None,
                error=None,
            )

    # -- public surface --------------------------------------------------

    @property
    def state(self) -> RunState:
        with self._cond:
            return self._state

    @property
    def kernels(self) -> Mapping[str, KernelResult]:
        with self._cond:
            return dict(self._results)

    def wait(self, timeout_s: float | None = None) -> "Execution":
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while self._state not in _TERMINAL_RUN_STATES:
                if deadline is None:
                    self._cond.wait(1.0)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise HerdaTimeoutError(
                            "wait_timeout",
                            f"execution {self.id} did not finish within "
                            f"{timeout_s:g}s",
                            "the run is still alive; call wait() again or "
                            "cancel()",
                        )
                    self._cond.wait(min(remaining, 1.0))
            return self

    def synchronize(self) -> "Execution":
        """CUDA-style alias for :meth:`wait` (device synchronize)."""

        return self.wait()

    def query(self) -> bool:
        """Return True when the whole execution has reached a terminal state."""

        with self._cond:
            return self._state in _TERMINAL_RUN_STATES

    def query_stream(self, stream: "Stream | str") -> bool:
        """Return True when every kernel in ``stream`` has finished."""

        names = self._stream_kernel_names(stream)
        with self._cond:
            return all(
                self._results[name].state.terminal for name in names
            )

    def wait_stream(
        self,
        stream: "Stream | str",
        timeout_s: float | None = None,
    ) -> "Execution":
        """CUDA-style ``stream synchronize`` without stopping the run."""

        names = self._stream_kernel_names(stream)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while not all(
                self._results[name].state.terminal for name in names
            ):
                if deadline is None:
                    self._cond.wait(0.2)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HerdaTimeoutError(
                        "stream_wait_timeout",
                        f"stream {stream!r} did not finish within "
                        f"{timeout_s:g}s",
                        "the run is still alive; call wait_stream() again or "
                        "cancel()",
                    )
                self._cond.wait(min(remaining, 0.2))
            return self

    def _stream_kernel_names(self, stream: "Stream | str") -> list[str]:
        if isinstance(stream, Stream):
            if stream.graph is not self.graph:
                raise ValidationError(
                    "foreign_stream",
                    f"stream {stream!r} belongs to another graph",
                )
            name = stream.name
        elif isinstance(stream, str):
            if stream not in self.graph.streams:
                raise ValidationError(
                    "unknown_stream",
                    f"stream {stream!r} does not exist",
                )
            name = stream
        else:
            raise ValidationError(
                "invalid_stream",
                f"expected a Stream or stream name, got {stream!r}",
            )
        return [
            qualified
            for qualified, kernel in self.graph.kernels.items()
            if kernel.stream.name == name
        ]

    def cancel(self, *, grace_s: float = 10.0) -> None:
        grace_s = max(0.0, grace_s)
        with self._cond:
            if self._state in _TERMINAL_RUN_STATES:
                return
            self._cancel_event.set()
            addresses = [
                result.terminal
                for result in self._results.values()
                if result.terminal
                and result.state
                in (
                    KernelState.running,
                    KernelState.awaiting_input,
                )
            ]
            self._cond.notify_all()

        for address in addresses:
            try:
                self._backend.interrupt(address, timeout_s=min(grace_s, 10.0))
            except Exception:  # noqa: BLE001 - cancellation is best effort
                log.debug("interrupt failed for %s", address, exc_info=True)

        if grace_s > 0:
            end = time.monotonic() + grace_s
            with self._cond:
                while time.monotonic() < end:
                    if all(not thread.is_alive() for thread in self._threads):
                        break
                    self._cond.wait(min(0.2, max(0.0, end - time.monotonic())))

        for address in addresses:
            try:
                self._backend.kill(address)
            except Exception:  # noqa: BLE001
                log.debug("kill failed for %s", address, exc_info=True)
            try:
                self._backend.rm(address)
            except Exception:  # noqa: BLE001
                log.debug("rm failed for %s", address, exc_info=True)

        with self._cond:
            for name, result in list(self._results.items()):
                if result.state.terminal:
                    continue
                if result.state in (
                    KernelState.running,
                    KernelState.awaiting_input,
                ):
                    self._results[name] = replace(
                        result,
                        state=KernelState.canceled,
                        status=result.status,
                        finished_at=_utcnow(),
                        error=ErrorInfo("canceled", "run was canceled"),
                    )
                else:
                    self._results[name] = replace(
                        result,
                        state=KernelState.skipped,
                        finished_at=_utcnow(),
                        error=ErrorInfo("canceled", "run was canceled"),
                    )
            self._cond.notify_all()

        with self._cond:
            threads = list(self._threads)
        deadline = time.monotonic() + max(10.0, grace_s)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if alive:
            log.warning(
                "cancel left worker threads running: %s", ", ".join(alive)
            )

        self._finalize()

    # -- scheduler -------------------------------------------------------

    def start(self) -> None:
        self._persist()
        self._scheduler = threading.Thread(
            target=self._schedule,
            name=f"herda-{self.id}",
            daemon=True,
        )
        self._scheduler.start()

    def _schedule(self) -> None:
        try:
            while True:
                with self._cond:
                    if self._cancel_event.is_set():
                        break
                    self._propagate_skips_locked()
                    if all(
                        result.state.terminal
                        for result in self._results.values()
                    ):
                        break

                    running = [
                        result
                        for result in self._results.values()
                        if result.state
                        in (
                            KernelState.running,
                            KernelState.awaiting_input,
                        )
                    ]
                    capacity = self.graph.max_parallel - len(running)
                    busy_streams = {result.stream for result in running}
                    launches: list[Kernel] = []
                    kernels = list(self.graph.kernels.values())
                    start_index = (
                        self._launch_cursor % len(kernels)
                        if kernels
                        else 0
                    )
                    ordered = (
                        kernels[start_index:] + kernels[:start_index]
                    )
                    for offset, kernel in enumerate(ordered):
                        if capacity <= 0:
                            break
                        result = self._results[kernel.qualified_name]
                        if (
                            result.state == KernelState.ready
                            and kernel.stream.name not in busy_streams
                        ):
                            self._results[kernel.qualified_name] = replace(
                                result,
                                state=KernelState.running,
                                started_at=_utcnow(),
                            )
                            launches.append(kernel)
                            capacity -= 1
                            busy_streams.add(kernel.stream.name)
                            self._launch_cursor = (
                                start_index + offset + 1
                            ) % len(kernels)

                    if launches:
                        self._cond.notify_all()
                    elif not running:
                        # No worker and nothing runnable: this can only mean a
                        # dependency cycle slipped past validation.
                        for name, result in list(self._results.items()):
                            if not result.state.terminal:
                                self._results[name] = replace(
                                    result,
                                    state=KernelState.failed,
                                    finished_at=_utcnow(),
                                    error=ErrorInfo(
                                        "internal",
                                        "no runnable kernel (cycle or "
                                        "scheduler bug)",
                                    ),
                                )
                        self._cond.notify_all()
                        break
                    else:
                        self._cond.wait(0.3)
                        continue

                for kernel in launches:
                    thread = threading.Thread(
                        target=self._run_kernel,
                        args=(kernel,),
                        name=f"herda-{kernel.qualified_name}",
                        daemon=True,
                    )
                    with self._cond:
                        self._threads.append(thread)
                    thread.start()
        except BaseException as exc:  # noqa: BLE001
            log.exception("scheduler crashed")
            with self._cond:
                for name, result in list(self._results.items()):
                    if not result.state.terminal:
                        self._results[name] = replace(
                            result,
                            state=KernelState.failed,
                            finished_at=_utcnow(),
                            error=ErrorInfo(
                                "internal",
                                f"scheduler crashed: {type(exc).__name__}: {exc}",
                            ),
                        )
        finally:
            with self._cond:
                threads = list(self._threads)
            deadline = time.monotonic() + 10.0
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(timeout=remaining)
            alive = [thread.name for thread in threads if thread.is_alive()]
            if alive:
                log.warning(
                    "scheduler left worker threads running: %s",
                    ", ".join(alive),
                )
            self._finalize()

    def _propagate_skips_locked(self) -> None:
        changed = True
        while changed:
            changed = False
            for kernel in self.graph.kernels.values():
                result = self._results[kernel.qualified_name]
                if result.state != KernelState.pending:
                    continue
                dependency_states = [
                    self._results[dependency.qualified_name].state
                    for dependency in self.graph._dependencies_of(kernel)
                ]
                bad = next(
                    (
                        state
                        for state in dependency_states
                        if state
                        in (
                            KernelState.failed,
                            KernelState.skipped,
                            KernelState.canceled,
                        )
                    ),
                    None,
                )
                if bad is not None:
                    self._results[kernel.qualified_name] = replace(
                        result,
                        state=KernelState.skipped,
                        finished_at=_utcnow(),
                        error=ErrorInfo(
                            "dependency",
                            f"dependency ended as {bad.value}",
                        ),
                    )
                    changed = True
                elif all(
                    state == KernelState.succeeded
                    for state in dependency_states
                ):
                    self._results[kernel.qualified_name] = replace(
                        result, state=KernelState.ready
                    )
                    changed = True

    def _run_kernel(self, kernel: Kernel) -> None:
        runner: SessionRunner | None = None
        try:
            runner = SessionRunner(
                kernel=kernel,
                backend=self._backend,
                store=self._store,
                cancel_event=self._cancel_event,
                ready_timeout_s=self.graph.ready_timeout_s,
                session_timeout_s=(
                    kernel.timeout_s
                    if kernel.timeout_s is not None
                    else self.graph.session_timeout_s
                ),
                blocked_timeout_s=self.graph.blocked_timeout_s,
                status_timeout_s=self.graph.status_timeout_s,
                branches=self._branches,
                prompt_text=self._prompt_texts[kernel.qualified_name],
                prompt_sha256=self._prompt_shas[kernel.qualified_name],
                large_prompt_path=self._large_prompts.get(
                    kernel.qualified_name
                ),
                on_terminal=lambda terminal: self._on_terminal(
                    kernel, terminal
                ),
                on_state=lambda state, status: self._on_state(
                    kernel, state, status
                ),
            )
            outcome = runner.run()
        except BaseException as exc:  # noqa: BLE001 - worker must terminate
            log.exception("kernel worker crashed: %s", kernel.qualified_name)
            terminal = runner.terminal if runner is not None else None
            if terminal is not None:
                for method in (
                    getattr(self._backend, "kill", None),
                    getattr(self._backend, "rm", None),
                ):
                    if method is not None:
                        try:
                            method(terminal)
                        except Exception:  # noqa: BLE001 - best effort
                            log.debug("terminal cleanup failed", exc_info=True)
            now = _utcnow()
            outcome = SessionOutcome(
                state=KernelState.failed,
                status=getattr(runner, "status", None),
                terminal=terminal,
                started_at=now,
                finished_at=now,
                duration_s=0.0,
                output_path=None,
                error=ErrorInfo(
                    "internal", f"{type(exc).__name__}: {exc}"
                ),
            )

        with self._cond:
            result = self._results[kernel.qualified_name]
            if (
                self._cancel_event.is_set()
                and result.state == KernelState.canceled
            ):
                outcome = replace(
                    outcome,
                    state=KernelState.canceled,
                    error=ErrorInfo("canceled", "run was canceled"),
                )
            self._results[kernel.qualified_name] = replace(
                result,
                state=outcome.state,
                terminal=outcome.terminal,
                status=outcome.status,
                started_at=outcome.started_at,
                finished_at=outcome.finished_at,
                duration_s=outcome.duration_s,
                output_path=outcome.output_path,
                error=outcome.error,
            )
            self._persist_locked()
            self._cond.notify_all()

    def _on_terminal(self, kernel: Kernel, terminal: str) -> None:
        with self._cond:
            result = self._results[kernel.qualified_name]
            self._results[kernel.qualified_name] = replace(
                result, terminal=terminal
            )
            self._persist_locked()

    def _on_state(
        self,
        kernel: Kernel,
        state: KernelState,
        status: str | None,
    ) -> None:
        with self._cond:
            result = self._results[kernel.qualified_name]
            if result.state == state and result.status == status:
                return
            self._results[kernel.qualified_name] = replace(
                result, state=state, status=status
            )
            self._persist_locked()
            self._cond.notify_all()

    # -- finalization ----------------------------------------------------

    def _finalize(self) -> None:
        with self._cond:
            if self._finished:
                return
            self._finished = True
            if self._cancel_event.is_set():
                self._state = RunState.canceled
            else:
                states = [
                    result.state for result in self._results.values()
                ]
                if all(state == KernelState.succeeded for state in states):
                    self._state = RunState.succeeded
                else:
                    self._state = RunState.failed
            self.finished_at = _utcnow()
            self._persist_locked()
            self._cond.notify_all()

        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    # -- persistence -----------------------------------------------------

    def _persist(self) -> None:
        with self._cond:
            self._persist_locked()

    def _persist_locked(self) -> None:
        try:
            kernels = {
                name: kernel_document(
                    result,
                    self._store.run_dir
                    / "prompts"
                    / result.stream
                    / f"{self.graph.kernels[name].name}.md",
                )
                for name, result in self._results.items()
                if name in self.graph.kernels
            }
            agent = getattr(self._backend, "agent", None)
            agent_name = getattr(agent, "name", None)
            document = run_document(
                run_id=self.id,
                graph_name=self.graph.name,
                graph_slug=self.graph.slug,
                repo=self.graph.repo,
                state=self._state.value,
                started_at=self.started_at,
                finished_at=self.finished_at,
                streams=self._streams,
                kernels=kernels,
                branches=self._branches,
                agent=agent_name,
            )
            self._store.write_run(document)
        except Exception as exc:  # noqa: BLE001 - persistence is best effort
            log.error("could not write %s: %s", self._store.run_json, exc)

    def __repr__(self) -> str:
        return f"Execution({self.id!r}, state={self.state.value!r})"

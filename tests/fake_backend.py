"""In-memory herdr backend used by the unit tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from herda.backend import SendOutcome, TerminalInfo, WaitOutcome
from herda.errors import HerdrError


@dataclass
class FakeTerminal:
    address: str
    labels: dict[str, str]
    worktree: Path
    argv: list[str]
    env: dict[str, str]
    state: str = "live"
    status: str | None = "idle"
    turn: int = 0
    reported: bool = True
    exit_code: int | None = None
    reason: str | None = None
    sent_once: bool = False
    completed: bool = False


class FakeBackend:
    def __init__(
        self,
        *,
        fail: set[str] | None = None,
        hang: set[str] | None = None,
        not_started: set[str] | None = None,
        block_once: set[str] | None = None,
        not_reported: set[str] | None = None,
        fail_send: set[str] | None = None,
        lost: set[str] | None = None,
        delay: dict[str, float] | None = None,
    ) -> None:
        self.fail = set(fail or ())
        self.hang = set(hang or ())
        self.not_started = set(not_started or ())
        self.block_once = set(block_once or ())
        self.not_reported = set(not_reported or ())
        self.fail_send = set(fail_send or ())
        self.lost = set(lost or ())
        self.delay = dict(delay or {})
        self.terminals: dict[str, FakeTerminal] = {}
        self.events: list[tuple[str, str, float]] = []
        self.sent_texts: list[tuple[str, str]] = []
        self.block_remaining: dict[str, int] = {
            key: 2 for key in self.block_once
        }
        self._counter = 0
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _key(labels: dict[str, str]) -> str:
        return f"{labels.get('herda.stream')}/{labels.get('herda.kernel')}"

    def _terminal(self, address: str) -> FakeTerminal:
        with self._lock:
            terminal = self.terminals.get(address)
        if terminal is None:
            raise HerdrError(
                "terminal_not_found", f"terminal {address} is gone"
            )
        return terminal

    def _info(self, terminal: FakeTerminal) -> TerminalInfo:
        return TerminalInfo(
            address=terminal.address,
            state=terminal.state,
            status=terminal.status,
            turn=terminal.turn,
            reported=terminal.reported,
            labels=dict(terminal.labels),
            exit_code=terminal.exit_code,
            reason=terminal.reason,
        )

    # -- Backend protocol ------------------------------------------------

    def preflight(self) -> None:
        return None

    def run(
        self,
        *,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
        name: str,
        labels: dict[str, str],
    ) -> str:
        key = self._key(labels)
        with self._lock:
            self._counter += 1
            address = f"t{self._counter}"
            reported = key not in self.not_reported
            self.terminals[address] = FakeTerminal(
                address=address,
                labels=dict(labels),
                worktree=worktree,
                argv=list(argv),
                env=dict(env),
                status="idle" if reported else None,
                reported=reported,
            )
            self.events.append(("run", key, time.monotonic()))
        return address

    def get(self, terminal: str) -> TerminalInfo:
        item = self._terminal(terminal)
        key = self._key(item.labels)
        with self._lock:
            if item.state == "finished":
                return self._info(item)
            if key in self.lost and item.sent_once:
                item.reason = "holder_lost"
                return self._info(item)
            if key in self.hang:
                item.status = "working"
                return self._info(item)
            if (
                key in self.block_once
                and item.sent_once
                and self.block_remaining.get(key, 0) > 0
            ):
                self.block_remaining[key] -= 1
                item.status = "blocked"
                return self._info(item)
            if item.sent_once and not item.completed and key not in self.fail:
                item.turn += 1
                item.status = "idle"
                item.completed = True
            return self._info(item)

    def list(self, labels: dict[str, str] | None = None) -> list[TerminalInfo]:
        wanted = labels or {}
        with self._lock:
            return [
                self._info(terminal)
                for terminal in self.terminals.values()
                if all(terminal.labels.get(k) == v for k, v in wanted.items())
            ]

    def send(
        self,
        terminal: str,
        text: str,
        *,
        timeout_s: float,
        wait: bool,
    ) -> SendOutcome:
        item = self._terminal(terminal)
        key = self._key(item.labels)
        if key in self.fail_send:
            raise HerdrError("send_failed", f"fake send failure for {key}")
        if key in self.delay:
            time.sleep(self.delay[key])
        with self._lock:
            t0 = item.turn
            item.sent_once = True
            self.sent_texts.append((key, text))
            self.events.append(("send", key, time.monotonic()))
            if key in self.not_started:
                item.status = "idle"
                item.completed = True
                return SendOutcome(
                    started=False,
                    status="idle",
                    turn=t0,
                    wait=WaitOutcome(
                        ended=True,
                        reason="not_started",
                        status="idle",
                        turn=t0,
                    ),
                )
            if key in self.fail:
                item.status = "error"
                item.turn = t0 + 1
                item.completed = True
                return SendOutcome(
                    started=True,
                    status="working",
                    turn=t0,
                    wait=WaitOutcome(
                        ended=True,
                        reason="status",
                        status="error",
                        turn=t0 + 1,
                    ),
                )
            if key in self.hang:
                item.status = "working"
                item.turn = t0
                return SendOutcome(
                    started=True,
                    status="working",
                    turn=t0,
                    wait=WaitOutcome(
                        ended=False,
                        reason="timeout",
                        status="working",
                        turn=t0,
                    ),
                )
            if key in self.lost:
                item.status = "working"
                item.turn = t0
                return SendOutcome(
                    started=True,
                    status="working",
                    turn=t0,
                    wait=WaitOutcome(
                        ended=False,
                        reason="timeout",
                        status="working",
                        turn=t0,
                    ),
                )
            if key in self.block_once:
                item.status = "working"
                item.turn = t0
                return SendOutcome(
                    started=True,
                    status="working",
                    turn=t0,
                    wait=WaitOutcome(
                        ended=False,
                        reason="timeout",
                        status="working",
                        turn=t0,
                    ),
                )
            item.status = "idle"
            item.turn = t0 + 1
            item.completed = True
            return SendOutcome(
                started=True,
                status="working",
                turn=t0,
                wait=WaitOutcome(
                    ended=True,
                    reason="status",
                    status="idle",
                    turn=t0 + 1,
                ),
            )

    def wait(self, terminal: str, *, timeout_s: float) -> WaitOutcome:
        info = self.get(terminal)
        return WaitOutcome(
            ended=not info.status == "working",
            reason="status",
            status=info.status,
            turn=info.turn,
            exit_code=info.exit_code,
        )

    def read(self, terminal: str, *, lines: int = 1000) -> str:
        item = self._terminal(terminal)
        return f"fake screen for {self._key(item.labels)}\n"

    def label(self, terminal: str, labels: dict[str, str]) -> None:
        item = self._terminal(terminal)
        with self._lock:
            item.labels.update(labels)

    def interrupt(self, terminal: str, *, timeout_s: float = 10.0) -> None:
        item = self._terminal(terminal)
        with self._lock:
            self.events.append(("interrupt", self._key(item.labels), time.monotonic()))

    def kill(self, terminal: str) -> None:
        item = self._terminal(terminal)
        with self._lock:
            self.events.append(("kill", self._key(item.labels), time.monotonic()))
            item.state = "finished"
            item.status = None
            item.reason = "killed"

    def rm(self, terminal: str) -> None:
        with self._lock:
            self.terminals.pop(terminal, None)

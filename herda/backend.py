"""Backends: the real ``hc`` CLI and the small protocol used by sessions.

Keeping this behind a protocol is deliberate: the unit tests use a fake
backend, while production always talks to ``herdr-core`` through ``hc``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .agent import PiAgent
from .errors import HerdrError

log = logging.getLogger("herda")


@dataclass
class TerminalInfo:
    address: str
    state: str
    status: str | None
    turn: int
    reported: bool
    labels: dict[str, str] = field(default_factory=dict)
    exit_code: int | None = None
    reason: str | None = None

    @property
    def finished(self) -> bool:
        return self.state == "finished"


@dataclass
class WaitOutcome:
    ended: bool
    reason: str
    status: str | None
    turn: int
    exit_code: int | None = None
    signal: int | None = None


@dataclass
class SendOutcome:
    started: bool | None
    status: str | None
    turn: int
    wait: WaitOutcome | None = None


class Backend(Protocol):
    def preflight(self) -> None: ...

    def launch_argv(
        self,
        model: str,
        *,
        inherit_session: bool,
        session_dir: Path | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]: ...

    def run(
        self,
        *,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
        name: str,
        labels: dict[str, str],
    ) -> str: ...

    def get(self, terminal: str) -> TerminalInfo: ...

    def list(self, labels: dict[str, str] | None = None) -> list[TerminalInfo]: ...

    def send(
        self,
        terminal: str,
        text: str,
        *,
        timeout_s: float,
        wait: bool,
    ) -> SendOutcome: ...

    def wait(self, terminal: str, *, timeout_s: float) -> WaitOutcome: ...

    def read(self, terminal: str, *, lines: int = 1000) -> str: ...

    def label(self, terminal: str, labels: dict[str, str]) -> None: ...

    def interrupt(self, terminal: str, *, timeout_s: float = 10.0) -> None: ...

    def kill(self, terminal: str) -> None: ...

    def rm(self, terminal: str) -> None: ...


def _json_of(proc: subprocess.CompletedProcess[str]) -> Any:
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HerdrError(
            "invalid_json",
            f"hc returned non-JSON output: {text[:300]!r}",
        ) from exc


def _detail(proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout or "").strip()
    return detail[:800] if detail else "(no output)"


class HcBackend:
    """Talk to herdr-core by invoking the ``hc`` executable."""

    def __init__(
        self,
        *,
        hc: str | None = None,
        pi: str | None = None,
        agent: PiAgent | None = None,
    ) -> None:
        self.hc = hc or os.environ.get("HERDA_HC") or "hc"
        self.agent = agent or PiAgent(pi)
        self.pi = self.agent.executable
        self.hc_path: str | None = None
        self.pi_path: str | None = None

    # -- process helpers -------------------------------------------------

    def _hc(
        self,
        args: list[str],
        *,
        timeout: float = 120.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        executable = self.hc_path or self.hc
        try:
            proc = subprocess.run(
                [executable, *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            message = f"hc executable not found: {self.hc}"
            log.error("%s", message)
            raise HerdrError(
                "hc_missing",
                message,
                "install herdr-core and make sure 'hc' is on PATH",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrError(
                "hc_timeout",
                f"hc {' '.join(args)} timed out after {timeout:g}s",
            ) from exc
        except OSError as exc:
            raise HerdrError(
                "hc_failed",
                f"could not run hc: {exc}",
                "check that the hc executable is usable",
            ) from exc

        if check and proc.returncode != 0:
            raise HerdrError(
                "hc_failed",
                f"hc {' '.join(args)} failed with exit code "
                f"{proc.returncode}: {_detail(proc)}",
            )
        return proc

    # -- dependency checks ----------------------------------------------

    def preflight(self) -> None:
        missing: list[str] = []
        self.hc_path = shutil.which(self.hc)
        if self.hc_path is None:
            missing.append(
                "hc (herdr-core): install it and put 'hc' on PATH"
            )
        self.pi_path = self.agent.resolve()
        if self.pi_path is None:
            missing.append(self.agent.missing_hint)

        if missing:
            for item in missing:
                log.error("herda preflight failed: missing %s", item)
            raise HerdrError(
                "missing_dependency",
                "required executables are missing: "
                + "; ".join(missing),
                "run 'herda doctor' for details",
            )

        proc = self._hc(["server", "status", "--json"], check=False)
        if proc.returncode != 0:
            log.error(
                "herda preflight failed: herdr-core is not reachable: %s",
                _detail(proc),
            )
            raise HerdrError(
                "core_unavailable",
                "herdr-core is not reachable",
                "start it with 'hc server start' and retry",
            )
        self._hc_version = None
        try:
            status = _json_of(proc)
            if isinstance(status, dict):
                self._hc_version = status.get("version")
        except HerdrError:
            pass

    # -- terminal operations --------------------------------------------

    def launch_argv(
        self,
        model: str,
        *,
        inherit_session: bool,
        session_dir: Path | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Delegate agent-specific argv construction to the adapter."""

        self.agent.path = self.pi_path or self.agent.path
        return self.agent.launch_argv(
            model,
            inherit_session=inherit_session,
            session_dir=session_dir,
            extra_args=extra_args,
            env=env,
        )

    def run(
        self,
        *,
        worktree: Path,
        argv: list[str],
        env: dict[str, str],
        name: str,
        labels: dict[str, str],
    ) -> str:
        args = [
            "run",
            "--bg",
            "--json",
            "--no-parent",
            "--no-notify",
            "--name",
            name,
            "--cwd",
            str(worktree),
        ]
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        args.append("--")
        args.append("env")
        for key, value in env.items():
            args.append(f"{key}={value}")
        args.extend(argv)

        proc = self._hc(args, timeout=60.0)
        data = _json_of(proc)
        if not isinstance(data, dict) or not data.get("address"):
            raise HerdrError(
                "run_failed",
                f"hc run did not return a terminal address: {_detail(proc)}",
            )
        return str(data["address"])

    def get(self, terminal: str) -> TerminalInfo:
        proc = self._hc(["get", "--json", terminal], check=False)
        if proc.returncode != 0:
            detail = _detail(proc)
            if "not_found" in detail or "not found" in detail.lower():
                raise HerdrError(
                    "terminal_not_found",
                    f"terminal {terminal} was not found",
                )
            raise HerdrError(
                "hc_failed", f"hc get {terminal} failed: {detail}"
            )
        data = _json_of(proc)
        if not isinstance(data, dict):
            raise HerdrError(
                "invalid_json",
                f"hc get {terminal} returned no terminal object",
            )
        status = data.get("status")
        status_word = status.get("state") if isinstance(status, dict) else None
        return TerminalInfo(
            address=str(data.get("address", terminal)),
            state=str(data.get("state", "live")),
            status=status_word,
            turn=int(data.get("turn") or 0),
            reported=bool(data.get("reported")),
            labels={
                str(k): str(v)
                for k, v in (data.get("labels") or {}).items()
            },
            exit_code=data.get("exit_code"),
            reason=data.get("reason"),
        )

    def list(self, labels: dict[str, str] | None = None) -> list[TerminalInfo]:
        args = ["ls", "--json"]
        for key, value in (labels or {}).items():
            args.extend(["--label", f"{key}={value}"])
        proc = self._hc(args, check=False)
        if proc.returncode != 0:
            raise HerdrError(
                "hc_failed", f"hc ls failed: {_detail(proc)}"
            )
        data = _json_of(proc)
        terminals = data.get("terminals", []) if isinstance(data, dict) else []
        result: list[TerminalInfo] = []
        for item in terminals:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            result.append(
                TerminalInfo(
                    address=str(item.get("address", "")),
                    state=str(item.get("state", "live")),
                    status=status.get("state")
                    if isinstance(status, dict)
                    else None,
                    turn=int(item.get("turn") or 0),
                    reported=bool(item.get("reported")),
                    labels={
                        str(k): str(v)
                        for k, v in (item.get("labels") or {}).items()
                    },
                    exit_code=item.get("exit_code"),
                    reason=item.get("reason"),
                )
            )
        return result

    def send(
        self,
        terminal: str,
        text: str,
        *,
        timeout_s: float,
        wait: bool,
    ) -> SendOutcome:
        args = ["send"]
        if wait:
            args.append("--wait")
        args.extend(
            [
                "--json",
                "--timeout",
                f"{timeout_s:g}",
                terminal,
                text,
            ]
        )
        proc = self._hc(args, timeout=timeout_s + 30.0, check=False)
        if proc.returncode != 0:
            raise HerdrError(
                "send_failed",
                f"hc send to {terminal} failed: {_detail(proc)}",
            )
        data = _json_of(proc)
        if not isinstance(data, dict):
            raise HerdrError(
                "invalid_json",
                f"hc send to {terminal} returned no object",
            )
        wait_data = data.get("wait")
        outcome_wait = None
        if isinstance(wait_data, dict):
            outcome_wait = WaitOutcome(
                ended=bool(wait_data.get("ended")),
                reason=str(wait_data.get("reason") or ""),
                status=wait_data.get("status"),
                turn=int(wait_data.get("turn") or 0),
                exit_code=wait_data.get("exit_code"),
                signal=wait_data.get("signal"),
            )
        return SendOutcome(
            started=data.get("started"),
            status=data.get("status"),
            turn=int(data.get("turn") or 0),
            wait=outcome_wait,
        )

    def wait(self, terminal: str, *, timeout_s: float) -> WaitOutcome:
        proc = self._hc(
            [
                "wait",
                "--json",
                "--timeout",
                f"{timeout_s:g}",
                terminal,
            ],
            timeout=timeout_s + 30.0,
            check=False,
        )
        if proc.returncode != 0:
            raise HerdrError(
                "wait_failed",
                f"hc wait {terminal} failed: {_detail(proc)}",
            )
        data = _json_of(proc)
        if not isinstance(data, dict):
            raise HerdrError(
                "invalid_json",
                f"hc wait {terminal} returned no object",
            )
        return WaitOutcome(
            ended=bool(data.get("ended")),
            reason=str(data.get("reason") or ""),
            status=data.get("status"),
            turn=int(data.get("turn") or 0),
            exit_code=data.get("exit_code"),
            signal=data.get("signal"),
        )

    def read(self, terminal: str, *, lines: int = 1000) -> str:
        proc = self._hc(
            ["read", "--json", "--lines", str(lines), terminal], check=False
        )
        if proc.returncode != 0:
            raise HerdrError(
                "read_failed",
                f"hc read {terminal} failed: {_detail(proc)}",
            )
        data = _json_of(proc)
        if isinstance(data, dict):
            return str(data.get("text") or "")
        return proc.stdout or ""

    def label(self, terminal: str, labels: dict[str, str]) -> None:
        if not labels:
            return
        args = ["label", terminal]
        args.extend(f"{key}={value}" for key, value in labels.items())
        args.append("--json")
        proc = self._hc(args, check=False)
        if proc.returncode != 0:
            log.warning(
                "could not update labels on %s: %s", terminal, _detail(proc)
            )

    def interrupt(self, terminal: str, *, timeout_s: float = 10.0) -> None:
        self._hc(
            [
                "interrupt",
                "--json",
                "--timeout",
                f"{timeout_s:g}",
                terminal,
            ],
            timeout=timeout_s + 15.0,
            check=False,
        )

    def kill(self, terminal: str) -> None:
        self._hc(["kill", terminal, "--json"], check=False)

    def rm(self, terminal: str) -> None:
        proc = self._hc(["rm", terminal, "--json"], check=False)
        if proc.returncode != 0 and "not_found" not in _detail(proc):
            log.debug("could not remove terminal %s: %s", terminal, _detail(proc))


class MissingDependencyBackend(HcBackend):
    """Kept for type clarity; behaves exactly like :class:`HcBackend`."""

"""``herda doctor``: check the external tools herda relies on."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .agent import PiAgent
from .backend import HcBackend
from .worktree import git_version

log = logging.getLogger("herda")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _which_version(command: str, *args: str) -> tuple[bool, str]:
    path = shutil.which(command)
    if path is None:
        return False, "not found in PATH"
    try:
        proc = subprocess.run(
            [path, *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run: {exc}"
    output = (proc.stdout or proc.stderr).strip().splitlines()
    first = output[0] if output else ""
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}: {first}"
    return True, first


def _hc_command() -> str:
    return HcBackend().hc


def _hc_status() -> tuple[bool, str]:
    path = shutil.which(_hc_command())
    if path is None:
        return False, "not found in PATH"
    try:
        proc = subprocess.run(
            [path, "server", "status", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not query core: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"core unreachable: {detail[:300]}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, "server status returned invalid JSON"
    version = data.get("version") or "unknown"
    pid = data.get("pid")
    return True, f"core {version} (pid {pid})"


def _pi_command() -> str:
    return PiAgent().executable


def _deep_osc(timeout_s: float = 30.0) -> Check:
    pi = _pi_command()
    backend = HcBackend()
    hc = getattr(backend, "hc", "hc")
    if shutil.which(hc) is None or shutil.which(pi) is None:
        return Check(
            "OSC 7501 (deep)",
            False,
            "skipped: hc or pi is missing",
        )
    temp = Path(tempfile.mkdtemp(prefix="herda-doctor-"))
    terminal = None
    try:
        name = f"herda-doctor-{int(time.time()) % 100000}"
        terminal = backend.run(
            worktree=temp,
            argv=[pi, "--no-session", "--no-approve"],
            env={"PI_PROGRAM_STATUS": "1"},
            name=name,
            labels={"herda.owner": "herda-doctor"},
        )
        deadline = time.monotonic() + timeout_s
        seen = None
        while time.monotonic() < deadline:
            info = backend.get(terminal)
            if info.finished:
                return Check(
                    "OSC 7501 (deep)",
                    False,
                    f"pi exited before reporting (reason={info.reason})",
                )
            if info.reported:
                seen = info.status
                if info.status in ("idle", "done"):
                    return Check(
                        "OSC 7501 (deep)",
                        True,
                        f"observed state={info.status!r}",
                    )
                if info.status == "blocked":
                    return Check(
                        "OSC 7501 (deep)",
                        False,
                        "observed blocked; resolve Pi login/trust first",
                    )
            time.sleep(0.25)
        if seen is None:
            return Check(
                "OSC 7501 (deep)",
                False,
                "no status report observed; use Pi 1.6+",
            )
        return Check(
            "OSC 7501 (deep)",
            False,
            f"last state was {seen!r}, expected idle/blocked/done",
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return Check("OSC 7501 (deep)", False, f"{type(exc).__name__}: {exc}")
    finally:
        if terminal is not None:
            try:
                backend.kill(terminal)
            finally:
                backend.rm(terminal)
        try:
            shutil.rmtree(temp)
        except OSError:
            pass


def run_doctor(*, deep: bool = False, app_check: bool = False) -> list[Check]:
    checks: list[Check] = []

    git_ok, git_detail = _which_version("git", "--version")
    checks.append(
        Check("git", git_ok, git_detail or git_version() or "missing")
    )

    hc_ok, hc_detail = _which_version(_hc_command(), "--version")
    checks.append(Check("hc", hc_ok, hc_detail))
    if hc_ok:
        core_ok, core_detail = _hc_status()
        checks.append(Check("herdr-core", core_ok, core_detail))

    pi = _pi_command()
    pi_ok, pi_detail = _which_version(pi, "--version")
    checks.append(Check("pi", pi_ok, pi_detail))

    if deep:
        checks.append(_deep_osc())

    if app_check:
        app_ok, app_detail = _which_version("herdr-app", "--version")
        checks.append(
            Check(
                "herdr-app",
                app_ok,
                app_detail
                if app_ok
                else "not found (optional; display names stay default)",
                required=False,
            )
        )

    return checks


def print_checks(checks: list[Check]) -> int:
    failed = False
    for check in checks:
        mark = "ok" if check.ok else ("fail" if check.required else "skip")
        print(f"[{mark:4}] {check.name}: {check.detail}")
        if not check.ok and check.required:
            failed = True
    if failed:
        print(
            "herda doctor: required tools are missing or unhealthy; "
            "fix them before running a graph",
        )
        return 1
    print("herda doctor: ready")
    return 0


__all__ = ["Check", "print_checks", "run_doctor"]

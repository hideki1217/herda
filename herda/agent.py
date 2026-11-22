"""Agent adapters.

herda owns the DAG, worktrees, and herdr terminals. The coding agent behind a
kernel is deliberately behind this small adapter so the rest of herda does not
need to know Pi command-line details.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence


class PiAgent:
    """Run the Pi coding agent in a herdr terminal."""

    name = "pi"
    missing_hint = "pi: install Pi and put 'pi' on PATH (https://pi.dev)"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = (
            executable or os.environ.get("HERDA_PI") or "pi"
        )
        self.path: str | None = None

    def resolve(self) -> str | None:
        self.path = shutil.which(self.executable)
        return self.path

    def version(self) -> str | None:
        path = self.path or shutil.which(self.executable)
        if path is None:
            return None
        try:
            proc = subprocess.run(
                [path, "--version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        lines = (proc.stdout or proc.stderr).strip().splitlines()
        return lines[0] if lines else None

    def launch_argv(
        self,
        model: str,
        *,
        inherit_session: bool,
        session_dir: Path | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Return the Pi argv and environment.

        ``--no-approve`` skips project-local Pi resources so a fresh worktree
        cannot stop at a trust prompt. ``--session-dir`` keeps Pi's JSONL
        session files inside herda's run directory, which makes session
        inheritance explicit and prevents stray manual runs from contaminating
        a kernel. ``PI_PROGRAM_STATUS=1`` makes OSC 7501 reporting explicit.
        """

        argv = [self.path or self.executable, "--no-approve"]
        if session_dir is not None:
            argv.extend(["--session-dir", str(session_dir)])
        argv.extend(str(arg) for arg in extra_args)
        argv.extend(["--model", model])
        if inherit_session:
            argv.append("--continue")

        launch_env = {"PI_PROGRAM_STATUS": "1"}
        if env:
            launch_env.update({str(k): str(v) for k, v in env.items()})
        return argv, launch_env


__all__ = ["PiAgent"]

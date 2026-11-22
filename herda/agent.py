"""Agent adapters.

herda owns the DAG, worktrees, and herdr terminals. The coding agent behind a
kernel is deliberately behind this small adapter so the rest of herda does not
need to know Pi command-line details.
"""

from __future__ import annotations

import fnmatch
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

    def list_models(self) -> set[str] | None:
        """Return ``provider/model`` strings advertised by Pi."""

        path = self.path or shutil.which(self.executable)
        if path is None:
            return None
        try:
            proc = subprocess.run(
                [path, "--list-models"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        models: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("provider"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                models.add(f"{parts[0]}/{parts[1]}")
        return models

    @staticmethod
    def model_available(model: str, available: set[str]) -> bool:
        """Accept exact ids, Pi patterns, and simple fuzzy names."""

        base = model.split(":", 1)[0].strip()
        if not base:
            return False
        if base in available:
            return True
        if any(char in base for char in "*?[]"):
            return any(
                fnmatch.fnmatchcase(candidate, base)
                for candidate in available
            )
        return any(base in candidate for candidate in available)

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

"""Unit tests for the Pi agent adapter."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from herda.agent import PiAgent


def proc(code: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess([], code, out, err)


class PiAgentTests(unittest.TestCase):
    def test_executable_and_resolve(self) -> None:
        self.assertEqual(PiAgent("custom").executable, "custom")
        with mock.patch.dict(os.environ, {"HERDA_PI": "env-pi"}):
            self.assertEqual(PiAgent().executable, "env-pi")
        agent = PiAgent("pi")
        with mock.patch("herda.agent.shutil.which", return_value=None):
            self.assertIsNone(agent.resolve())
        with mock.patch(
            "herda.agent.shutil.which", return_value="/bin/pi"
        ):
            self.assertEqual(agent.resolve(), "/bin/pi")

    def test_version(self) -> None:
        agent = PiAgent("pi")
        with mock.patch("herda.agent.shutil.which", return_value=None):
            self.assertIsNone(agent.version())

        agent.path = "/bin/pi"
        with mock.patch(
            "herda.agent.subprocess.run",
            return_value=proc(0, out="1.2.3\n"),
        ):
            self.assertEqual(agent.version(), "1.2.3")
        with mock.patch(
            "herda.agent.subprocess.run", side_effect=OSError("no")
        ):
            self.assertIsNone(agent.version())
        with mock.patch(
            "herda.agent.subprocess.run", return_value=proc(0)
        ):
            self.assertIsNone(agent.version())

    def test_launch_argv(self) -> None:
        agent = PiAgent("pi")
        agent.path = "/bin/pi"
        argv, env = agent.launch_argv(
            "provider/model",
            inherit_session=True,
            session_dir=Path("/run/agent-sessions/api"),
            extra_args=("--thinking", "high"),
            env={"EXTRA": "1"},
        )
        self.assertEqual(
            argv,
            [
                "/bin/pi",
                "--no-approve",
                "--session-dir",
                "/run/agent-sessions/api",
                "--thinking",
                "high",
                "--model",
                "provider/model",
                "--continue",
            ],
        )
        self.assertEqual(env, {"PI_PROGRAM_STATUS": "1", "EXTRA": "1"})

        argv, env = agent.launch_argv(
            "p/m", inherit_session=False
        )
        self.assertEqual(
            argv, ["/bin/pi", "--no-approve", "--model", "p/m"]
        )
        self.assertEqual(env, {"PI_PROGRAM_STATUS": "1"})


if __name__ == "__main__":
    unittest.main()

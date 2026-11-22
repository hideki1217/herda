"""Unit tests for the hc backend wrapper (subprocess is fully mocked)."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from herda.backend import (
    HcBackend,
    MissingDependencyBackend,
    SendOutcome,
    TerminalInfo,
    WaitOutcome,
    _detail,
    _json_of,
)
from herda.errors import HerdrError


def proc(code: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess([], code, out, err)


class BackendHelperTests(unittest.TestCase):
    def test_json_of(self) -> None:
        self.assertIsNone(_json_of(proc(0, "")))
        with self.assertRaises(HerdrError) as caught:
            _json_of(proc(0, "not json"))
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_detail(self) -> None:
        self.assertEqual(_detail(proc(0, "")), "(no output)")
        self.assertEqual(_detail(proc(0, err="x" * 900)), "x" * 800)


class BackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = HcBackend(hc="hc", pi="pi")

    def test_env_overrides_and_subclass(self) -> None:
        with mock.patch.dict(
            os.environ, {"HERDA_HC": "myhc", "HERDA_PI": "mypi"}
        ):
            backend = HcBackend()
            self.assertEqual(backend.hc, "myhc")
            self.assertEqual(backend.pi, "mypi")
        self.assertIsInstance(MissingDependencyBackend(), HcBackend)
        self.assertIsInstance(SendOutcome(True, "idle", 1), SendOutcome)
        self.assertIsInstance(
            TerminalInfo("t", "live", "idle", 1, True), TerminalInfo
        )
        self.assertIsInstance(
            WaitOutcome(True, "status", "idle", 1), WaitOutcome
        )

    def test_hc_process_errors(self) -> None:
        with mock.patch(
            "herda.backend.subprocess.run", side_effect=FileNotFoundError()
        ):
            with self.assertLogs("herda", level="ERROR"):
                with self.assertRaises(HerdrError) as caught:
                    self.backend._hc(["x"])
        self.assertEqual(caught.exception.code, "hc_missing")
        with mock.patch(
            "herda.backend.subprocess.run",
            side_effect=subprocess.TimeoutExpired("hc", 1),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend._hc(["x"], timeout=1)
        self.assertEqual(caught.exception.code, "hc_timeout")
        with mock.patch(
            "herda.backend.subprocess.run", side_effect=OSError("nope")
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend._hc(["x"])
        self.assertEqual(caught.exception.code, "hc_failed")
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(1, err="bad")
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend._hc(["x"])
            self.assertIn("bad", str(caught.exception))
            result = self.backend._hc(["x"], check=False)
        self.assertEqual(result.returncode, 1)

    def test_preflight_missing_and_unreachable(self) -> None:
        with mock.patch("herda.backend.shutil.which", return_value=None):
            with self.assertLogs("herda", level="ERROR"):
                with self.assertRaises(HerdrError) as caught:
                    self.backend.preflight()
        self.assertEqual(caught.exception.code, "missing_dependency")

        with mock.patch(
            "herda.backend.shutil.which", return_value="/bin/tool"
        ), mock.patch.object(
            self.backend.agent, "version", return_value="1.5.0"
        ):
            with self.assertLogs("herda", level="ERROR"):
                with self.assertRaises(HerdrError) as caught:
                    self.backend.preflight()
        self.assertEqual(caught.exception.code, "missing_dependency")
        self.assertIn("too old", caught.exception.message)

        with mock.patch(
            "herda.backend.shutil.which", return_value="/bin/tool"
        ), mock.patch(
            "herda.backend.subprocess.run", return_value=proc(1, err="down")
        ):
            with self.assertLogs("herda", level="ERROR"):
                with self.assertRaises(HerdrError) as caught:
                    self.backend.preflight()
        self.assertEqual(caught.exception.code, "core_unavailable")

    def test_preflight_success_and_bad_status_json(self) -> None:
        with mock.patch(
            "herda.backend.shutil.which", return_value="/bin/tool"
        ), mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps({"version": "1.2"})),
        ), mock.patch.object(
            self.backend.agent, "version", return_value="1.6.0"
        ):
            self.backend.preflight()
        self.assertEqual(self.backend._hc_version, "1.2")

        with mock.patch(
            "herda.backend.shutil.which", return_value="/bin/tool"
        ), mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0, out="nope")
        ), mock.patch.object(
            self.backend.agent, "version", return_value="1.6.0"
        ):
            self.backend.preflight()
        self.assertIsNone(self.backend._hc_version)

    def test_launch_argv(self) -> None:
        argv, env = self.backend.launch_argv(
            "provider/model", inherit_session=True
        )
        self.assertEqual(
            argv,
            [
                "pi",
                "--no-approve",
                "--model",
                "provider/model",
                "--continue",
            ],
        )
        self.assertEqual(env, {"PI_PROGRAM_STATUS": "1"})

        backend = HcBackend(hc="hc", pi="/custom/pi")
        argv, env = backend.launch_argv("p/m", inherit_session=False)
        self.assertEqual(
            argv, ["/custom/pi", "--no-approve", "--model", "p/m"]
        )
        self.assertEqual(env, {"PI_PROGRAM_STATUS": "1"})

    def test_run(self) -> None:
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps({"address": "t7"})),
        ) as run:
            address = self.backend.run(
                worktree=Path("/w"),
                argv=["pi"],
                env={"A": "B"},
                name="n",
                labels={"l": "v"},
            )
        self.assertEqual(address, "t7")
        args = run.call_args.args[0]
        self.assertIn("--label", args)
        self.assertIn("l=v", args)
        self.assertIn("A=B", args)

        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps({})),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.run(
                    worktree=Path("/w"),
                    argv=[],
                    env={},
                    name="n",
                    labels={},
                )
        self.assertEqual(caught.exception.code, "run_failed")

    def test_get(self) -> None:
        payload = {
            "address": "t1",
            "state": "live",
            "status": {"state": "working"},
            "turn": 3,
            "reported": True,
            "labels": {"a": 1},
            "exit_code": None,
            "reason": None,
        }
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps(payload)),
        ):
            info = self.backend.get("t1")
        self.assertEqual(info.status, "working")
        self.assertEqual(info.labels, {"a": "1"})
        self.assertEqual(info.turn, 3)

        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps({"status": None})),
        ):
            info = self.backend.get("t1")
        self.assertEqual(info.status, None)
        self.assertEqual(info.state, "live")

        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="not_found"),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.get("t1")
        self.assertEqual(caught.exception.code, "terminal_not_found")

        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="other"),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.get("t1")
        self.assertEqual(caught.exception.code, "hc_failed")

        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0, out="[]")
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.get("t1")
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_list(self) -> None:
        payload = {
            "terminals": [
                {
                    "address": "t1",
                    "state": "live",
                    "status": {"state": "idle"},
                    "turn": 1,
                    "reported": True,
                    "labels": {"x": "y"},
                },
                {
                    "address": "t2",
                    "state": "live",
                    "status": "working",
                    "turn": 2,
                },
                {
                    "address": "t3",
                    "state": "live",
                    "status": None,
                    "turn": 0,
                },
                "not-a-dict",
            ]
        }
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps(payload)),
        ) as run:
            terminals = self.backend.list({"owner": "herda"})
        self.assertEqual(len(terminals), 3)
        self.assertEqual(terminals[0].status, "idle")
        self.assertEqual(terminals[1].status, "working")
        self.assertIsNone(terminals[2].status)
        self.assertIn("--label", run.call_args.args[0])
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0, out="[]")
        ):
            self.assertEqual(self.backend.list(), [])
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="bad"),
        ):
            with self.assertRaises(HerdrError):
                self.backend.list()

    def test_send(self) -> None:
        wait = {
            "ended": True,
            "reason": "status",
            "status": "idle",
            "turn": 2,
            "exit_code": 0,
            "signal": None,
        }
        payload = {"started": True, "status": "working", "turn": 1, "wait": wait}
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps(payload)),
        ) as run:
            outcome = self.backend.send("t1", "hi", timeout_s=5, wait=True)
        self.assertEqual(outcome.started, True)
        self.assertEqual(outcome.wait.status, "idle")
        self.assertIn("--wait", run.call_args.args[0])
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps({"turn": 0})),
        ):
            outcome = self.backend.send("t1", "hi", timeout_s=5, wait=False)
        self.assertIsNone(outcome.wait)
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="blocked"),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.send("t1", "hi", timeout_s=5, wait=True)
        self.assertEqual(caught.exception.code, "send_failed")
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0, out="[]")
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.send("t1", "hi", timeout_s=5, wait=True)
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_wait(self) -> None:
        payload = {
            "ended": False,
            "reason": "timeout",
            "status": "working",
            "turn": 2,
            "exit_code": None,
            "signal": None,
        }
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps(payload)),
        ):
            outcome = self.backend.wait("t1", timeout_s=5)
        self.assertFalse(outcome.ended)
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="bad"),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.wait("t1", timeout_s=5)
        self.assertEqual(caught.exception.code, "wait_failed")
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0, out="[]")
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.wait("t1", timeout_s=5)
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_read(self) -> None:
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out=json.dumps({"text": "screen"})),
        ):
            self.assertEqual(self.backend.read("t1"), "screen")
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(0, out="[]"),
        ):
            self.assertEqual(self.backend.read("t1"), "[]")
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="bad"),
        ):
            with self.assertRaises(HerdrError) as caught:
                self.backend.read("t1")
        self.assertEqual(caught.exception.code, "read_failed")

    def test_label_interrupt_kill_rm(self) -> None:
        with mock.patch("herda.backend.subprocess.run") as run:
            self.backend.label("t1", {})
            run.assert_not_called()
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0)
        ):
            self.backend.label("t1", {"a": "b"})
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(1, err="bad")
        ):
            with self.assertLogs("herda", level="WARNING"):
                self.backend.label("t1", {"a": "b"})
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0)
        ) as run:
            self.backend.interrupt("t1", timeout_s=3)
            self.backend.kill("t1")
        self.assertEqual(run.call_count, 2)
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(0)
        ):
            self.backend.rm("t1")
        with mock.patch(
            "herda.backend.subprocess.run",
            return_value=proc(1, err="not_found"),
        ):
            with mock.patch("herda.backend.log.debug") as debug:
                self.backend.rm("t1")
                debug.assert_not_called()
        with mock.patch(
            "herda.backend.subprocess.run", return_value=proc(1, err="other")
        ):
            with mock.patch("herda.backend.log.debug") as debug:
                self.backend.rm("t1")
                debug.assert_called_once()

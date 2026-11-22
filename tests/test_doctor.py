"""Coverage for herda doctor and its subprocess helpers."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from herda import doctor
from herda.backend import TerminalInfo


def proc(code: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeDoctorBackend:
    def __init__(
        self,
        *,
        infos=None,
        run_error: Exception | None = None,
        kill_error: Exception | None = None,
        rm_error: Exception | None = None,
    ) -> None:
        self.infos = list(infos or [])
        self.run_error = run_error
        self.kill_error = kill_error
        self.rm_error = rm_error
        self.killed: list[str] = []
        self.rm_calls: list[str] = []

    def run(self, **kwargs):
        if self.run_error is not None:
            raise self.run_error
        return "t1"

    def get(self, terminal: str):
        if self.infos:
            return self.infos.pop(0)
        return TerminalInfo("t1", "live", "idle", 0, True)

    def kill(self, terminal: str) -> None:
        self.killed.append(terminal)
        if self.kill_error is not None:
            raise self.kill_error

    def rm(self, terminal: str) -> None:
        self.rm_calls.append(terminal)
        if self.rm_error is not None:
            raise self.rm_error


class DoctorHelperTests(unittest.TestCase):
    def test_which_version(self) -> None:
        with mock.patch("herda.doctor.shutil.which", return_value=None):
            self.assertEqual(doctor._which_version("x"), (False, "not found in PATH"))
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch(
            "herda.doctor.subprocess.run", side_effect=OSError("no")
        ):
            self.assertFalse(doctor._which_version("x")[0])
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch(
            "herda.doctor.subprocess.run", return_value=proc(1, err="bad")
        ):
            self.assertFalse(doctor._which_version("x")[0])
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch(
            "herda.doctor.subprocess.run", return_value=proc(0, err="ok")
        ):
            self.assertEqual(doctor._which_version("x"), (True, "ok"))
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch(
            "herda.doctor.subprocess.run", return_value=proc(0)
        ):
            self.assertEqual(doctor._which_version("x"), (True, ""))

    def test_hc_status(self) -> None:
        with mock.patch("herda.doctor.shutil.which", return_value=None):
            self.assertFalse(doctor._hc_status()[0])
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/hc"
        ), mock.patch(
            "herda.doctor.subprocess.run", side_effect=OSError("no")
        ):
            self.assertFalse(doctor._hc_status()[0])
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/hc"
        ), mock.patch(
            "herda.doctor.subprocess.run", return_value=proc(1, err="down")
        ):
            self.assertIn("unreachable", doctor._hc_status()[1])
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/hc"
        ), mock.patch(
            "herda.doctor.subprocess.run", return_value=proc(0, out="bad")
        ):
            self.assertIn("invalid JSON", doctor._hc_status()[1])
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/hc"
        ), mock.patch(
            "herda.doctor.subprocess.run",
            return_value=proc(0, out=json.dumps({"version": "1"})),
        ):
            ok, detail = doctor._hc_status()
        self.assertTrue(ok)
        self.assertIn("core 1", detail)
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/hc"
        ), mock.patch(
            "herda.doctor.subprocess.run",
            return_value=proc(0, out=json.dumps({})),
        ):
            ok, detail = doctor._hc_status()
        self.assertTrue(ok)
        self.assertIn("unknown", detail)

    def test_deep_osc_missing_tools(self) -> None:
        with mock.patch("herda.doctor.shutil.which", return_value=None):
            check = doctor._deep_osc()
        self.assertFalse(check.ok)
        self.assertIn("missing", check.detail)

    def test_deep_osc_success_and_finished(self) -> None:
        backend = FakeDoctorBackend(
            infos=[TerminalInfo("t1", "live", "idle", 0, True)]
        )
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch("herda.doctor.HcBackend", return_value=backend):
            check = doctor._deep_osc()
        self.assertTrue(check.ok)
        self.assertEqual(backend.killed, ["t1"])
        self.assertEqual(backend.rm_calls, ["t1"])

        backend = FakeDoctorBackend(
            infos=[TerminalInfo("t1", "finished", None, 0, False, reason="x")]
        )
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch("herda.doctor.HcBackend", return_value=backend):
            check = doctor._deep_osc()
        self.assertFalse(check.ok)
        self.assertIn("exited", check.detail)

    def test_deep_osc_blocked_is_not_success(self) -> None:
        backend = FakeDoctorBackend(
            infos=[TerminalInfo("t1", "live", "blocked", 0, True)]
        )
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch("herda.doctor.HcBackend", return_value=backend):
            check = doctor._deep_osc()
        self.assertFalse(check.ok)
        self.assertIn("blocked", check.detail)

    def test_deep_osc_last_state_and_no_report(self) -> None:
        backend = FakeDoctorBackend(
            infos=[TerminalInfo("t1", "live", "working", 0, True)]
        )
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch("herda.doctor.HcBackend", return_value=backend), mock.patch(
            "herda.doctor.time.monotonic", side_effect=[0, 0, 1]
        ), mock.patch("herda.doctor.time.sleep"):
            check = doctor._deep_osc(timeout_s=0.5)
        self.assertFalse(check.ok)
        self.assertIn("last state", check.detail)

        backend = FakeDoctorBackend(
            infos=[TerminalInfo("t1", "live", None, 0, False)]
        )
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch("herda.doctor.HcBackend", return_value=backend), mock.patch(
            "herda.doctor.time.monotonic", side_effect=[0, 0, 1]
        ), mock.patch("herda.doctor.time.sleep"):
            check = doctor._deep_osc(timeout_s=0.5)
        self.assertFalse(check.ok)
        self.assertIn("no status report", check.detail)

    def test_deep_osc_exception(self) -> None:
        backend = FakeDoctorBackend(run_error=RuntimeError("boom"))
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch("herda.doctor.HcBackend", return_value=backend):
            check = doctor._deep_osc()
        self.assertFalse(check.ok)
        self.assertIn("RuntimeError", check.detail)

    def test_deep_osc_rmtree_error_ignored(self) -> None:
        backend = FakeDoctorBackend(
            infos=[TerminalInfo("t1", "live", "idle", 0, True)]
        )
        with mock.patch(
            "herda.doctor.shutil.which", return_value="/bin/x"
        ), mock.patch(
            "herda.doctor.HcBackend", return_value=backend
        ), mock.patch(
            "herda.doctor.shutil.rmtree", side_effect=OSError("no")
        ):
            check = doctor._deep_osc()
        self.assertTrue(check.ok)

    def test_run_doctor_and_print(self) -> None:
        with mock.patch(
            "herda.doctor._which_version",
            side_effect=[
                (False, ""),
                (True, "hc 1"),
                (True, "pi 1"),
                (False, "missing"),
            ],
        ), mock.patch(
            "herda.doctor._hc_status", return_value=(True, "core")
        ), mock.patch(
            "herda.doctor.git_version", return_value="git 1"
        ), mock.patch(
            "herda.doctor._deep_osc", return_value=doctor.Check("deep", True, "")
        ):
            checks = doctor.run_doctor(deep=True, app_check=True)
        self.assertEqual(len(checks), 6)
        self.assertFalse(checks[0].ok)
        self.assertFalse(checks[-1].required)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = doctor.print_checks(
                [
                    doctor.Check("a", True, "ok"),
                    doctor.Check("b", False, "skip", required=False),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("skip", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                doctor.print_checks([doctor.Check("a", False, "bad")]), 1
            )

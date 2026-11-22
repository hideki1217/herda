"""Best-effort display-name sync with the optional ``herdr-app`` CLI.

The app itself only reacts to ``herdr.space`` / ``herdr.tab`` labels. The
human-readable names are applied with ``herdr-app do space.rename`` and
``tab.rename``. A missing app or a failed rename is never fatal: the IDs stay
stable and the run continues with herdr's default names.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

log = logging.getLogger("herda")


def available() -> bool:
    return shutil.which("herdr-app") is not None


def _do(method: str, payload: dict[str, str]) -> bool:
    try:
        proc = subprocess.run(
            ["herdr-app", "do", method, json.dumps(payload)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("herdr-app %s failed: %s", method, exc)
        return False
    if proc.returncode != 0:
        log.debug(
            "herdr-app %s failed: %s",
            method,
            (proc.stderr or proc.stdout or "").strip()[:400],
        )
        return False
    return True


def rename(
    *,
    space_id: str,
    space_name: str,
    tabs: dict[str, str],
    retries: int = 4,
    delay_s: float = 1.0,
) -> bool:
    if not available():
        log.warning(
            "herdr-app was not found; keeping herdr's default space/tab "
            "names (labels still identify the run)"
        )
        return False

    ok_space = False
    ok_tabs: set[str] = set()
    for attempt in range(retries + 1):
        if not ok_space:
            ok_space = _do(
                "space.rename", {"id": space_id, "name": space_name}
            )
        for tab, name in tabs.items():
            if tab not in ok_tabs:
                if _do("tab.rename", {"id": tab, "name": name}):
                    ok_tabs.add(tab)
        if ok_space and len(ok_tabs) == len(tabs):
            return True
        if attempt < retries:
            time.sleep(delay_s)
    log.warning(
        "herdr-app rename did not fully succeed; keeping default names for "
        "space %s",
        space_id,
    )
    return False

"""Minimal CLI: doctor, validate, run, draw, clean, app-sync."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from . import app
from .backend import HcBackend
from .cleanup import cleanup_orphans, cleanup_run, validate_run_id
from .doctor import print_checks, run_doctor
from .errors import HerdaError, HerdaTimeoutError
from .graph import Graph
from .store import read_json
from .types import RunState
from .worktree import require_git_repo

log = logging.getLogger("herda")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="herda: %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _load_graph(path_text: str) -> Graph:
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise HerdaError(
            "file_not_found", f"graph file not found: {path}"
        )
    os.chdir(path.parent)
    module_name = f"_herda_graph_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HerdaError(
            "import_failed", f"could not load {path} as a Python module"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except HerdaError:
        raise
    except Exception as exc:  # noqa: BLE001 - user graph code
        raise HerdaError(
            "import_failed",
            f"importing {path} failed: {type(exc).__name__}: {exc}",
        ) from exc
    graph = getattr(module, "graph", None)
    if not isinstance(graph, Graph):
        raise HerdaError(
            "graph_missing",
            f"{path} must define a module-level variable named 'graph'",
            "add: graph = Graph(...)",
        )
    return graph


def _print_error(exc: HerdaError) -> None:
    print(f"herda: error: {exc}", file=sys.stderr)
    if exc.hint:
        print(f"herda: hint: {exc.hint}", file=sys.stderr)


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor(deep=args.deep, app_check=args.app)
    if getattr(args, "json", False):
        print(json.dumps([asdict(check) for check in checks], indent=2))
        return 0 if all(
            check.ok or not check.required for check in checks
        ) else 1
    return print_checks(checks)


def _cmd_validate(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    graph.validate()
    if getattr(args, "models", False):
        graph.validate_models()
    print(
        f"ok: graph {graph.name!r} "
        f"({len(graph.streams)} streams, {len(graph.kernels)} kernels)"
    )
    return 0


def _cmd_draw(args: argparse.Namespace) -> int:
    # _load_graph() chdirs into the graph module's directory. Resolve the
    # output path first so "-o relative/path" is relative to the invocation
    # directory, like every other CLI argument.
    output = args.output
    if output != "-":
        output = str(Path(output).expanduser().resolve())
    graph = _load_graph(args.graph)
    graph.validate()
    if output == "-":
        sys.stdout.write(graph.to_mermaid(direction=args.direction))
        return 0
    target = graph.draw(output, direction=args.direction)
    print(target)
    return 0


def _print_results(execution) -> None:
    print(f"run_id: {execution.id}")
    print(f"state: {execution.state.value}")
    print(f"run_dir: {execution.run_dir}")
    for name, result in execution.kernels.items():
        line = f"  {name}: {result.state.value}"
        if result.status:
            line += f" (status={result.status})"
        if result.error:
            line += f" [{result.error.code}: {result.error.message}]"
        print(line)


def _cmd_run(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    execution = graph.start()

    def _cancel(signum, frame) -> None:  # noqa: ANN001
        log.warning("received signal %s; canceling run %s", signum, execution.id)
        execution.cancel(grace_s=5.0)

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _cancel)
        except (ValueError, OSError):
            pass
    try:
        try:
            execution.wait(timeout_s=getattr(args, "timeout", None))
        except HerdaTimeoutError:
            log.warning(
                "run %s exceeded its CLI timeout; canceling it",
                execution.id,
            )
            execution.cancel(grace_s=5.0)
            raise
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    _print_results(execution)
    return 0 if execution.state == RunState.succeeded else 1


def _cmd_clean(args: argparse.Namespace) -> int:
    if getattr(args, "orphans", False):
        worktrees, branches = cleanup_orphans(
            Path(args.repo),
            force=args.force,
            branches=getattr(args, "branches", False),
        )
        print(
            f"removed {worktrees} orphan worktrees and "
            f"{branches} orphan branches"
        )
        return 0
    if not args.run_id:
        raise HerdaError(
            "run_id_required",
            "herda clean needs RUN_ID or --orphans",
        )
    cleanup_run(
        Path(args.repo),
        args.run_id,
        worktrees=args.worktrees,
        branches=getattr(args, "branches", False),
        force=args.force,
    )
    print(f"cleaned run {args.run_id}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    root = require_git_repo(args.repo)
    runs_dir = root / ".herda" / "runs"
    run_id = getattr(args, "run_id", None)
    if run_id:
        run_id = validate_run_id(run_id)
    else:
        candidates = (
            [
                path.name
                for path in runs_dir.iterdir()
                if (path / "run.json").exists()
            ]
            if runs_dir.exists()
            else []
        )
        if not candidates:
            raise HerdaError(
                "run_not_found", "no herda runs were found"
            )
        run_id = max(candidates)

    run_json = runs_dir / run_id / "run.json"
    if not run_json.exists():
        raise HerdaError(
            "run_not_found", f"run {run_id!r} was not found"
        )
    try:
        document = read_json(run_json)
    except (OSError, ValueError) as exc:
        raise HerdaError(
            "invalid_run_json", f"could not read {run_json}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise HerdaError(
            "invalid_run_json", f"{run_json} must contain a JSON object"
        )

    if getattr(args, "json", False):
        print(json.dumps(document, indent=2, sort_keys=True))
        return 0

    print(f"run_id: {document.get('run_id', run_id)}")
    print(f"graph: {document.get('graph')}")
    print(f"state: {document.get('state')}")
    print(f"agent: {document.get('agent')}")
    print(f"started_at: {document.get('started_at')}")
    print(f"finished_at: {document.get('finished_at')}")
    kernels = document.get("kernels", {})
    if not isinstance(kernels, dict):
        kernels = {}
    for name, result in kernels.items():
        if not isinstance(result, dict):
            continue
        line = f"  {name}: {result.get('state')}"
        if result.get("status"):
            line += f" (status={result.get('status')})"
        error = result.get("error")
        if isinstance(error, dict):
            line += f" [{error.get('code')}: {error.get('message')}]"
        print(line)
    return 0


def _sync_once(
    backend: HcBackend, synced: set[tuple[str, str]]
) -> None:
    terminals = backend.list(labels={"herda.owner": "herda"})
    spaces: dict[str, tuple[str, dict[str, str]]] = {}
    for terminal in terminals:
        labels = terminal.labels
        space = labels.get("herdr.space")
        tab = labels.get("herdr.tab")
        space_name = labels.get("herda.space_name")
        tab_name = labels.get("herda.tab_name")
        if not (space and space_name):
            continue
        if space not in spaces:
            spaces[space] = (space_name, {})
        if tab and tab_name:
            spaces[space][1][tab] = tab_name

    for space, (space_name, tabs) in spaces.items():
        pending = {
            tab: name
            for tab, name in tabs.items()
            if (space, tab) not in synced
        }
        if (space, "space") in synced and not pending:
            continue
        if app.rename(
            space_id=space,
            space_name=space_name,
            tabs=pending,
            retries=1,
            delay_s=0.5,
        ):
            synced.add((space, "space"))
            for tab in pending:
                synced.add((space, tab))


def _cmd_app_sync(args: argparse.Namespace) -> int:
    if not app.available():
        message = (
            "herdr-app was not found; app-sync only runs on the host that "
            "has the herdr app"
        )
        log.error("%s", message)
        return 1
    backend = HcBackend()
    if backend.hc_path is None:
        import shutil

        backend.hc_path = shutil.which(backend.hc)
        if backend.hc_path is None:
            log.error("hc was not found; cannot read herda labels")
            return 1
    synced: set[tuple[str, str]] = set()
    try:
        while True:
            _sync_once(backend, synced)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="herda",
        description="CUDA-like streams and kernels for coding agents",
    )
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check external tools")
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="start pi in a herdr terminal and observe OSC 7501",
    )
    doctor.add_argument(
        "--app",
        action="store_true",
        help="also check for herdr-app",
    )
    doctor.add_argument(
        "--json", action="store_true", help="print checks as JSON"
    )
    doctor.set_defaults(func=_cmd_doctor)

    validate = sub.add_parser("validate", help="validate a graph module")
    validate.add_argument("graph")
    validate.add_argument(
        "--models",
        action="store_true",
        help="also check model ids with 'pi --list-models'",
    )
    validate.set_defaults(func=_cmd_validate)

    run = sub.add_parser("run", help="run a graph until it finishes")
    run.add_argument("graph")
    run.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="cancel the run after this many seconds",
    )
    run.set_defaults(func=_cmd_run)

    draw = sub.add_parser("draw", help="write the DAG as Mermaid")
    draw.add_argument("graph")
    draw.add_argument("-o", "--output", required=True)
    draw.add_argument(
        "--direction", choices=("LR", "TB"), default="LR"
    )
    draw.set_defaults(func=_cmd_draw)

    clean = sub.add_parser("clean", help="clean terminals/worktrees of a run")
    clean.add_argument("run_id", nargs="?")
    clean.add_argument(
        "--orphans",
        action="store_true",
        help="remove worktrees/branches that have no run.json",
    )
    clean.add_argument("--worktrees", action="store_true")
    clean.add_argument(
        "--branches",
        action="store_true",
        help="also delete herda branches for this run",
    )
    clean.add_argument("--force", action="store_true")
    clean.add_argument("--repo", default=".")
    clean.set_defaults(func=_cmd_clean)

    status = sub.add_parser("status", help="show a run")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--json", action="store_true")
    status.add_argument("--repo", default=".")
    status.set_defaults(func=_cmd_status)

    app_sync = sub.add_parser(
        "app-sync",
        help="rename remote space/tabs from herda labels (host only)",
    )
    app_sync.add_argument("--once", action="store_true")
    app_sync.add_argument("--interval", type=float, default=3.0)
    app_sync.set_defaults(func=_cmd_app_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except HerdaError as exc:
        _print_error(exc)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

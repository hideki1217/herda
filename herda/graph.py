"""Public Graph object and run preflight."""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from . import app
from .backend import Backend, HcBackend
from .cleanup import cleanup_run
from .errors import ValidationError
from .event import Event
from .execution import Execution
from .kernel import Kernel
from .mermaid import to_mermaid
from .naming import (
    short_hash,
    slugify,
    space_id as make_space_id,
    tab_id as make_tab_id,
    terminal_name,
    validate_name,
)
from .prompt import load_prompt, prompt_sha256
from .store import GraphLock, RunStore
from .stream import Stream
from .worktree import (
    create_worktree,
    delete_branch,
    ensure_main_exclude,
    init_worktree_herda,
    remove_engine_dir,
    remove_worktree,
    require_git_repo,
    resolve_commit,
    write_large_prompt,
)

log = logging.getLogger("herda")

PROMPT_INLINE_LIMIT = 48 * 1024


class Graph:
    def __init__(
        self,
        name: str,
        repo: str | Path,
        *,
        default_model: str,
        base_dir: str | Path | None = None,
        max_parallel: int = 4,
        ready_timeout_s: float = 120.0,
        session_timeout_s: float | None = None,
        _backend: Backend | None = None,
    ) -> None:
        validate_name("Graph", name)
        if not isinstance(default_model, str) or not default_model.strip():
            raise ValidationError(
                "model_required",
                "Graph.default_model must not be empty",
                "use pi's provider/model form, e.g. "
                "anthropic/claude-sonnet-5-5",
            )
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise ValidationError(
                "invalid_max_parallel",
                "max_parallel must be an integer >= 1",
            )
        if ready_timeout_s <= 0:
            raise ValidationError(
                "invalid_timeout", "ready_timeout_s must be > 0"
            )
        if session_timeout_s is not None and session_timeout_s <= 0:
            raise ValidationError(
                "invalid_timeout", "session_timeout_s must be > 0 or None"
            )

        self.name = name
        self.repo = Path(repo).expanduser().resolve()
        self.default_model = default_model
        self.base_dir = (
            Path.cwd() if base_dir is None else Path(base_dir)
        ).expanduser().resolve()
        self.max_parallel = max_parallel
        self.ready_timeout_s = float(ready_timeout_s)
        self.session_timeout_s = session_timeout_s
        self.slug = slugify(name, fallback="graph")
        self.space_id = make_space_id(self.slug, str(self.repo), name)

        self._streams: dict[str, Stream] = {}
        self._kernels: dict[str, Kernel] = {}
        self._events: dict[str, Event] = {}
        self._kernel_dependencies: dict[str, tuple[Kernel, ...]] = {}
        self._stream_slugs: dict[str, str] = {}
        self.tab_ids: dict[str, str] = {}
        self.terminal_names: dict[str, str] = {}
        self._start_attempted = False
        self._backend = _backend
        self._app_sync_started = False

    # -- graph construction ---------------------------------------------

    def stream(self, name: str, *, base: str = "HEAD") -> Stream:
        if self._start_attempted:
            raise ValidationError(
                "graph_started",
                "cannot add streams after Graph.start()",
                "declare the whole graph before starting it",
            )
        validate_name("Stream", name)
        if name in self._streams:
            raise ValidationError(
                "duplicate_stream",
                f"stream {name!r} is already declared",
            )
        stream = Stream(self, name, base)
        self._streams[name] = stream
        slug = slugify(name, fallback="stream")
        if slug in self._stream_slugs.values():
            slug = f"{slug}-{short_hash(name, 4)}"
        self._stream_slugs[name] = slug
        self.tab_ids[name] = make_tab_id(self.space_id, slug, name)
        return stream

    def _dependencies_of(self, kernel: Kernel) -> tuple[Kernel, ...]:
        """Internal scheduling edges for a kernel.

        Kernels never declare dependencies themselves. The edges come from
        stream order and recorded/waiting events.
        """

        return self._kernel_dependencies.get(kernel.qualified_name, ())

    # -- validation ------------------------------------------------------

    def validate(self) -> None:
        if not self._streams:
            raise ValidationError(
                "empty_graph",
                f"graph {self.name!r} has no streams",
            )

        for stream in self._streams.values():
            if not stream.kernels:
                raise ValidationError(
                    "empty_stream",
                    f"stream {stream.name!r} has no kernels",
                )

        for kernel in self._kernels.values():
            if not kernel.model.strip():
                raise ValidationError(
                    "model_required",
                    f"kernel {kernel.qualified_name} has no model",
                )
            # This reads the prompt but writes nothing; start() snapshots it.
            load_prompt(kernel.prompt)
            for dependency in self._dependencies_of(kernel):
                if dependency.qualified_name not in self._kernels:
                    raise ValidationError(
                        "unknown_dependency",
                        f"kernel {kernel.qualified_name} depends on "
                        f"{dependency.qualified_name}, which is not declared",
                    )
            if kernel.inherit_session:
                previous = self._previous_kernel(kernel)
                if previous is None:
                    raise ValidationError(
                        "inherit_without_previous",
                        f"kernel {kernel.qualified_name} sets "
                        "inherit_session=True but has no previous kernel",
                    )

        self._check_cycles()
        require_git_repo(self.repo)
        for stream in self._streams.values():
            resolve_commit(self.repo, stream.base)

    def _previous_kernel(self, kernel: Kernel) -> Kernel | None:
        kernels = kernel.stream.kernels
        index = kernels.index(kernel)
        return kernels[index - 1] if index > 0 else None

    def _check_cycles(self) -> None:
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(kernel: Kernel) -> None:
            name = kernel.qualified_name
            if name in done:
                return
            if name in visiting:
                raise ValidationError(
                    "cycle",
                    f"dependency cycle detected at {name}",
                )
            visiting.add(name)
            for dependency in self._dependencies_of(kernel):
                visit(dependency)
            visiting.remove(name)
            done.add(name)

        for kernel in self._kernels.values():
            visit(kernel)

    # -- execution -------------------------------------------------------

    def start(self) -> Execution:
        if self._start_attempted:
            raise ValidationError(
                "graph_started",
                "Graph.start() can only be called once",
                "create a new Graph for another run",
            )
        self._start_attempted = True
        self.validate()

        backend = self._backend
        if backend is None:
            backend = HcBackend()
            self._backend = backend
        backend.preflight()

        herda_dir = self.repo / ".herda"
        self._prepare_herda_dir(herda_dir)
        lock = GraphLock(herda_dir / "locks" / f"{self.slug}.lock")
        lock.acquire()

        created: list[tuple[Path, str]] = []
        try:
            run_id = self._new_run_id()
            run6 = run_id[-6:]
            store = RunStore(herda_dir, run_id)
            base_commits: dict[str, str] = {}
            branches: dict[str, str] = {}
            worktrees: dict[str, Path] = {}

            for stream in self._streams.values():
                base_commits[stream.name] = resolve_commit(
                    self.repo, stream.base
                )
                branch = (
                    f"herda/{self.slug}/"
                    f"{self._stream_slugs[stream.name]}-{run6}"
                )
                path = (
                    herda_dir
                    / "worktrees"
                    / self.slug
                    / run_id
                    / self._stream_slugs[stream.name]
                )
                created_worktree = create_worktree(
                    self.repo, path, branch, base_commits[stream.name]
                )
                created.append((created_worktree, branch))
                branches[stream.name] = branch
                worktrees[stream.name] = created_worktree

            ensure_main_exclude(self.repo)
            for path in worktrees.values():
                init_worktree_herda(path)

            prompt_texts: dict[str, str] = {}
            prompt_shas: dict[str, str] = {}
            large_prompts: dict[str, Path] = {}
            for kernel in self._kernels.values():
                text = load_prompt(kernel.prompt)
                prompt_texts[kernel.qualified_name] = text
                prompt_shas[kernel.qualified_name] = prompt_sha256(text)
                snapshot = store.prompt_snapshot_path(
                    kernel.stream.name, kernel.name
                )
                snapshot.write_text(text, encoding="utf-8")
                if len(text.encode("utf-8")) > PROMPT_INLINE_LIMIT:
                    large_prompts[kernel.qualified_name] = write_large_prompt(
                        worktrees[kernel.stream.name],
                        kernel.stream.name,
                        kernel.name,
                        text,
                    )

            for stream in self._streams.values():
                stream.branch = branches[stream.name]
                stream.worktree = worktrees[stream.name]

            self.terminal_names = {
                kernel.qualified_name: terminal_name(
                    self._stream_slugs[kernel.stream.name],
                    slugify(kernel.name, fallback="kernel"),
                    run_id,
                    kernel.stream.name,
                    kernel.name,
                )
                for kernel in self._kernels.values()
            }

            streams_doc = {
                stream.name: {
                    "name": stream.name,
                    "base": stream.base,
                    "base_commit": base_commits[stream.name],
                    "branch": branches[stream.name],
                    "worktree": str(worktrees[stream.name]),
                    "space_id": self.space_id,
                    "tab_id": self.tab_ids[stream.name],
                }
                for stream in self._streams.values()
            }

            execution = Execution(
                graph=self,
                run_id=run_id,
                store=store,
                lock=lock,
                backend=backend,
                streams=streams_doc,
                prompt_texts=prompt_texts,
                prompt_shas=prompt_shas,
                large_prompts=large_prompts,
            )
            self._start_app_sync()
            execution.start()
            return execution
        except BaseException:
            self._rollback(created)
            lock.release()
            raise

    def run(self) -> Execution:
        execution = self.start()
        return execution.wait()

    def _prepare_herda_dir(self, herda_dir: Path) -> None:
        for path in (
            herda_dir,
            herda_dir / "worktrees",
            herda_dir / "runs",
            herda_dir / "locks",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.chmod(0o700)
            except OSError:
                pass

    @staticmethod
    def _new_run_id() -> str:
        stamp = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        return f"{stamp}-{secrets.token_hex(3)}"

    def _rollback(self, created: list[tuple[Path, str]]) -> None:
        for path, branch in reversed(created):
            try:
                remove_engine_dir(path)
                remove_worktree(self.repo, path, force=False)
            except Exception:  # noqa: BLE001 - best effort rollback
                log.debug("worktree rollback failed for %s", path, exc_info=True)
            try:
                delete_branch(self.repo, branch)
            except Exception:  # noqa: BLE001
                log.debug("branch rollback failed for %s", branch, exc_info=True)

    # -- rendering -------------------------------------------------------

    def to_mermaid(
        self, *, direction: Literal["LR", "TB"] = "LR"
    ) -> str:
        return to_mermaid(self, direction)

    def draw(
        self,
        path: str | Path,
        *,
        direction: Literal["LR", "TB"] = "LR",
    ) -> Path:
        target = Path(path)
        if target.suffix.lower() not in (".mmd", ".md"):
            raise ValidationError(
                "invalid_draw_path",
                "draw() writes Mermaid text to a .mmd or .md file",
            )
        target.write_text(
            self.to_mermaid(direction=direction), encoding="utf-8"
        )
        return target

    # -- cleanup ---------------------------------------------------------

    def cleanup(
        self,
        run_id: str,
        *,
        worktrees: bool = False,
        force: bool = False,
    ) -> None:
        cleanup_run(
            self.repo,
            run_id,
            worktrees=worktrees,
            force=force,
            backend=self._backend,
        )

    # -- properties ------------------------------------------------------

    @property
    def streams(self) -> Mapping[str, Stream]:
        return MappingProxyType(dict(self._streams))

    @property
    def kernels(self) -> Mapping[str, Kernel]:
        return MappingProxyType(dict(self._kernels))

    @property
    def events(self) -> Mapping[str, Event]:
        return MappingProxyType(dict(self._events))

    def _start_app_sync(self) -> None:
        if self._app_sync_started or not app.available():
            return
        self._app_sync_started = True
        tabs = dict(self.tab_ids)
        space = self.space_id
        name = self.name

        def worker() -> None:
            app.rename(
                space_id=space,
                space_name=name,
                tabs=tabs,
            )

        threading.Thread(
            target=worker,
            name=f"herda-app-sync-{self.slug}",
            daemon=True,
        ).start()

    def __repr__(self) -> str:
        return (
            f"Graph({self.name!r}, repo={str(self.repo)!r}, "
            f"streams={len(self._streams)}, kernels={len(self._kernels)})"
        )

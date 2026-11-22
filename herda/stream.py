"""Stream: one execution lane backed by one git worktree and branch.

Within a stream, kernels run in declaration order. Cross-stream ordering is
expressed with :meth:`record_event` and :meth:`wait_event`, exactly like CUDA
events.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from .errors import ValidationError
from .event import Event
from .kernel import Kernel
from .naming import validate_name
from .prompt import resolve_prompt
from .timeouts import validate_timeout

if TYPE_CHECKING:
    from .graph import Graph


class Stream:
    def __init__(self, graph: "Graph", name: str, base: str) -> None:
        validate_name("Stream", name)
        if not isinstance(base, str) or not base:
            raise ValidationError(
                "invalid_base", f"Stream {name!r} base must not be empty"
            )
        self.name = name
        self.graph = graph
        self.base = base
        self.branch: str | None = None
        self.worktree: Path | None = None
        self._kernels: list[Kernel] = []
        self._recorded_events: list[Event] = []
        self._pending_deps: list[Kernel] = []

    def kernel(
        self,
        name: str,
        prompt: str | Path,
        *,
        model: str | None = None,
        inherit_session: bool = False,
        timeout_s: float | None = None,
        agent_args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Kernel:
        if self.graph._start_attempted:
            raise ValidationError(
                "graph_started",
                "cannot add kernels after Graph.start()",
                "declare the whole graph before starting it",
            )
        validate_name("Kernel", name)
        if any(kernel.name == name for kernel in self._kernels):
            raise ValidationError(
                "duplicate_kernel",
                f"kernel {name!r} is already declared in stream "
                f"{self.name!r}",
            )

        resolved_model = model if model is not None else self.graph.default_model
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise ValidationError(
                "model_required",
                f"kernel {self.name}/{name} has no model",
                "pass model=... or set Graph.default_model",
            )
        resolved_timeout = validate_timeout(
            f"kernel {self.name}/{name} timeout_s", timeout_s
        )
        if isinstance(agent_args, str):
            raise ValidationError(
                "invalid_agent_args",
                f"kernel {self.name}/{name} agent_args must be a sequence",
            )
        if env is not None and not isinstance(env, Mapping):
            raise ValidationError(
                "invalid_env",
                f"kernel {self.name}/{name} env must be a mapping",
            )

        previous = self._kernels[-1] if self._kernels else None
        if inherit_session and previous is None:
            raise ValidationError(
                "inherit_without_previous",
                f"kernel {self.name}/{name} sets inherit_session=True "
                "but has no previous kernel in its stream",
                "inherit_session only continues the previous kernel's "
                "conversation",
            )

        # Stream order is implicit: a kernel depends on the previous kernel
        # in the same stream. Event/stream waits add the recorded position of
        # another stream; only the first kernel after the wait needs that edge.
        dependencies: list[Kernel] = []
        if previous is not None:
            dependencies.append(previous)
        for dependency in self._pending_deps:
            if dependency not in dependencies:
                dependencies.append(dependency)

        prompt_path = resolve_prompt(self.graph.base_dir, prompt)

        resolved_agent_args = (
            tuple(str(arg) for arg in agent_args)
            if agent_args is not None
            else self.graph.agent_args
        )
        resolved_env = dict(self.graph.env)
        if env is not None:
            resolved_env.update({str(k): str(v) for k, v in env.items()})

        kernel = Kernel(
            graph=self.graph,
            stream=self,
            name=name,
            prompt=prompt_path,
            model=resolved_model,
            inherit_session=bool(inherit_session),
            timeout_s=resolved_timeout,
            agent_args=resolved_agent_args,
            env=resolved_env,
        )
        self._kernels.append(kernel)
        self.graph._kernels[kernel.qualified_name] = kernel
        self.graph._kernel_dependencies[kernel.qualified_name] = tuple(
            dependencies
        )
        self._pending_deps.clear()
        return kernel

    def record_event(self, name: str | None = None) -> Event:
        """Record an event at the current tail of this stream.

        The event is complete when every kernel declared so far in this
        stream has succeeded. Deleting/adding kernels in another stream does
        not change it.

        ``name`` may be omitted; CUDA-style handles do not need a name. An
        omitted name gets a graph-wide ``event-<n>`` name.
        """

        if self.graph._start_attempted:
            raise ValidationError(
                "graph_started",
                "cannot record events after Graph.start()",
                "declare the whole graph before starting it",
            )
        if name is None:
            index = len(self.graph._events) + 1
            name = f"event-{index}"
            while name in self.graph._events:
                index += 1
                name = f"event-{index}"
        validate_name("Event", name)
        if name in self.graph._events:
            raise ValidationError(
                "duplicate_event",
                f"event {name!r} is already recorded in graph "
                f"{self.graph.name!r}",
            )
        event = Event(
            graph=self.graph,
            stream=self,
            name=name,
            after=self._kernels[-1] if self._kernels else None,
        )
        self.graph._events[name] = event
        self._recorded_events.append(event)
        return event

    def wait_event(self, event: Event) -> None:
        """Make subsequent kernels in this stream wait for ``event``.

        Like ``cudaStreamWaitEvent``, work already declared in this stream is
        not affected. A wait on an event from the same stream is a no-op,
        because the stream is already serialized.
        """

        if self.graph._start_attempted:
            raise ValidationError(
                "graph_started",
                "cannot add event waits after Graph.start()",
                "declare the whole graph before starting it",
            )
        if not isinstance(event, Event):
            raise ValidationError(
                "invalid_event",
                f"wait_event() expects an Event, got {event!r}",
            )
        if event.graph is not self.graph:
            raise ValidationError(
                "foreign_event",
                f"event {event!r} belongs to another graph",
            )
        if self.graph._events.get(event.name) is not event:
            raise ValidationError(
                "event_not_recorded",
                f"event {event.name!r} was not recorded in this graph",
            )
        if event.stream is self or event.after is None:
            return
        if event.after not in self._pending_deps:
            self._pending_deps.append(event.after)

    def wait_stream(self, stream: "Stream") -> None:
        """Wait for all work currently declared in ``stream``.

        This is the CUDA convenience form of ``record_event`` plus
        ``wait_event`` at the current tail of ``stream``. Kernels declared on
        ``stream`` later do not affect this wait.
        """

        if self.graph._start_attempted:
            raise ValidationError(
                "graph_started",
                "cannot add stream waits after Graph.start()",
                "declare the whole graph before starting it",
            )
        if not isinstance(stream, Stream):
            raise ValidationError(
                "invalid_stream",
                f"wait_stream() expects a Stream, got {stream!r}",
            )
        if stream.graph is not self.graph:
            raise ValidationError(
                "foreign_stream",
                f"stream {stream!r} belongs to another graph",
            )
        if stream is self or not stream._kernels:
            return
        dependency = stream._kernels[-1]
        if dependency not in self._pending_deps:
            self._pending_deps.append(dependency)

    @property
    def kernels(self) -> tuple[Kernel, ...]:
        return tuple(self._kernels)

    @property
    def recorded_events(self) -> tuple[Event, ...]:
        return tuple(self._recorded_events)

    def __repr__(self) -> str:
        return f"Stream({self.name!r})"

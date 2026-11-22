"""Static Mermaid rendering of a herda DAG."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .naming import mermaid_id

if TYPE_CHECKING:
    from .graph import Graph


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _short_model(model: str) -> str:
    return model.split("/", 1)[-1].strip() or model


def to_mermaid(graph: "Graph", direction: Literal["LR", "TB"] = "LR") -> str:
    if direction not in ("LR", "TB"):
        from .errors import ValidationError

        raise ValidationError(
            "invalid_direction",
            f"mermaid direction must be 'LR' or 'TB', got {direction!r}",
        )

    used: set[str] = set()
    node_ids: dict[str, str] = {}
    lines = [f"flowchart {direction}"]

    for stream in graph.streams.values():
        stream_id = mermaid_id("stream", stream.name, used)
        lines.append(
            f'  subgraph {stream_id}["{_escape(stream.name)}'
            f'  (base={_escape(stream.base)})"]'
        )
        for kernel in stream.kernels:
            node_id = mermaid_id("n", kernel.qualified_name, used)
            node_ids[kernel.qualified_name] = node_id
            label = (
                f"{_escape(kernel.name)}<br/>"
                f"{_escape(_short_model(kernel.model))}"
            )
            lines.append(f'    {node_id}["{label}"]')
        lines.append("  end")

    edges: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, str, bool]] = set()
    for kernel in graph.kernels.values():
        target = node_ids[kernel.qualified_name]
        for dependency in graph._dependencies_of(kernel):
            source = node_ids[dependency.qualified_name]
            dotted = dependency.stream is kernel.stream
            key = (source, target, dotted)
            if key in seen:
                continue
            seen.add(key)
            edges.append((source, target, dotted))

    for source, target, dotted in edges:
        arrow = "-.->" if dotted else "-->"
        lines.append(f"  {source} {arrow} {target}")

    return "\n".join(lines) + "\n"

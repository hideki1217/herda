"""Build a tiny hello-world project with five prompts.

Run ``./setup.sh`` first (or use ``./run.sh``), then:

    PYTHONPATH=../.. python3 -m herda validate graph.py
    PYTHONPATH=../.. python3 -m herda run graph.py

The model comes from ``HERDA_MODEL`` so the example is not tied to one
provider.
"""

from __future__ import annotations

import os
from pathlib import Path

from herda import Graph

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "workspace"
MODEL = os.environ.get("HERDA_MODEL", "anthropic/claude-sonnet-5-5")

graph = Graph(
    "hello-world",
    WORKSPACE,
    default_model=MODEL,
    base_dir=HERE,
    ready_timeout_s=180,       # pi can take a while on first start
    session_timeout_s=600,     # one prompt turn, not the whole graph
)

# code stream: scaffold -> greet -> test
code = graph.stream("code", base="main")
scaffold = code.kernel("scaffold", "prompts/code/01-scaffold.md")
scaffold_done = code.record_event("scaffold_done")

greet = code.kernel(
    "greet",
    "prompts/code/02-greet.md",
    inherit_session=True,      # greet continues scaffold's conversation
)
test = code.kernel("test", "prompts/code/03-test.md")

# docs stream: design can run in parallel with greet after scaffold.
docs = graph.stream("docs", base="main")
docs.wait_event(scaffold_done)
design = docs.kernel("design", "prompts/docs/01-design.md")

# readme waits for every code kernel declared so far (scaffold/greet/test)
# and for docs/design, which is already ordered inside the docs stream.
docs.wait_stream(code)
docs.kernel("readme", "prompts/docs/02-readme.md")

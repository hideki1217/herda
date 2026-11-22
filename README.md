# herda

CUDA streams for coding agents: a DAG engine on top of
[herdr-core](https://herdr.dev/core/), where each stream is a git worktree and
each kernel is one herdr session (one prompt) running
[pi](https://pi.dev/).

The API specification is in [API.md](./API.md).

```python
from herda import Graph

graph = Graph(name="auth", repo="/repo/app", default_model="anthropic/claude-sonnet-5-5")

api = graph.stream("api", base="main")
design = api.kernel("design", "prompts/design.md")
design_done = api.record_event("design_done")
api.kernel("implement", "prompts/implement.md")

web = graph.stream("web", base="main")
web.wait_event(design_done)
web.kernel("ui", "prompts/ui.md")

graph.run()
```

- DAG: Mermaid rendering
- stream: one git worktree, kernels run in declaration order
- kernel: one prompt, one herdr session, visible in the herdr app
- synchronization: CUDA-style events (`record_event` / `wait_event` / `wait_stream`)
- completion: OSC 7501 status, no screen scraping
- session inheritance: off by default

## CUDA-style synchronization

Kernels never declare `depends_on`. Work inside a stream is serialized by
declaration order; cross-stream ordering is expressed with events, exactly like
CUDA.

```python
code = graph.stream("code", base="main")
code.kernel("scaffold", "prompts/scaffold.md")
scaffold_done = code.record_event("scaffold_done")   # name is optional
code.kernel("implement", "prompts/implement.md")
code.kernel("test", "prompts/test.md")

docs = graph.stream("docs", base="main")
docs.wait_event(scaffold_done)      # cudaStreamWaitEvent
docs.kernel("design", "prompts/design.md")
docs.wait_stream(code)              # wait for code's tail at this point
docs.kernel("readme", "prompts/readme.md")
```

| CUDA | herda |
|---|---|
| stream | `graph.stream(...)` |
| kernel launch | `stream.kernel(...)` |
| `cudaEventRecord` | `stream.record_event()` |
| `cudaStreamWaitEvent` | `stream.wait_event(event)` |
| `wait_stream` | `stream.wait_stream(other)` |
| `cudaStreamSynchronize` | `execution.wait_stream(stream)` |
| `cudaDeviceSynchronize` | `execution.synchronize()` / `wait()` |
| `cudaStreamQuery` | `execution.query_stream(stream)` |
| run query | `execution.query()` |

## Installation

```bash
pip install -e .
```

herda itself is pure Python (3.11+). The runtime dependencies are checked
before a run starts:

- `git` 2.30+
- `hc` (herdr-core) with a reachable core
- `pi` (the Pi coding agent) 1.6+

If any of them is missing, `herda` logs which dependency is missing and stops
before creating worktrees or terminals.

```bash
herda doctor          # git / hc / core / pi
herda doctor --deep   # also start pi and observe OSC 7501
herda doctor --app    # also check the optional herdr-app
```

herda launches pi as `pi --no-approve --model <provider/model>`. Pi's
`--model` accepts the same `provider/model` form used by `Graph.default_model`
and kernel `model=...`. Use `pi --list-models` to find a model id for
your provider. With `inherit_session=True`, herda adds `--continue`
so pi resumes the previous kernel's session in the same worktree.

`--no-approve` keeps project-local Pi settings and extensions from triggering
an interactive trust prompt in a fresh worktree. Repository context files such
as `AGENTS.md` are still loaded. Override the pi executable with `HERDA_PI` if
it is not on `PATH`.

Pi sessions are stored per run and stream under
`.herda/runs/<run_id>/agent-sessions/<stream>/`, so `inherit_session=True`
does not depend on Pi's global session history.

## Operations

```bash
herda validate graph.py --models     # check model ids with pi --list-models
herda status [RUN_ID] [--json]       # inspect run.json
herda clean RUN_ID --worktrees       # remove worktrees for a finished run
herda clean RUN_ID --worktrees --branches  # also remove herda/* branches
herda clean --orphans                # recover worktrees without run.json
herda clean --orphans --branches     # also remove orphan herda/* branches
```

## Examples

- [`examples/hello_world`](./examples/hello_world/): five prompts split across
  two streams build a small Python hello-world project, using a cross-stream
  event, `wait_stream` and `inherit_session=True`.

```bash
cd examples/hello_world
HERDA_MODEL=<provider/model> ./run.sh
```

## Development

```bash
pip install -e '.[dev]'
ruff check herda tests
mypy herda
coverage run -m unittest discover -v
coverage report -m
```

The unit tests use an in-memory fake herdr backend and make no network or
model calls. They cover validation, DAG scheduling, failure propagation,
cancel, cleanup, Mermaid output and the missing-dependency preflight.

Statement coverage is 100%:

```bash
coverage run -m unittest discover
coverage report
```

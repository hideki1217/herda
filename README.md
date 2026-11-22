# herda

CUDA streams for coding agents: a DAG engine on top of
[herdr-core](https://herdr.dev/core/), where each stream is a git worktree and
each kernel is one herdr session (one prompt) running opencode.

The API specification is in [API.md](./API.md).

```python
from herda import Graph

graph = Graph(name="auth", repo="/repo/app", default_model="anthropic/claude-sonnet-5-5")

api = graph.stream("api", base="main")
design = api.kernel("design", "prompts/design.md")
api.kernel("implement", "prompts/implement.md", depends_on=[design])

graph.run()
```

- DAG: Mermaid rendering
- stream: one git worktree, kernels run in order
- kernel: one prompt, one herdr session, visible in the herdr app
- completion: OSC 7501 status, no screen scraping
- session inheritance: off by default

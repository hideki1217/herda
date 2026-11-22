Continue in the `docs` worktree. The current directory is the repository root.
`docs/plan.md` should already exist.

Write `README.md` at the repository root (not `docs/README.md`) for the tiny
hello-world project:

- Follow the outline in `docs/plan.md`.
- Include a short description.
- Include a `Usage` section with:
  - `python3 hello.py`
  - `python3 hello.py Ada`
- Include a `Test` section with `python3 -m unittest -v`.
- Mention that the implementation is plain Python, standard library only.
- Do not invent extra features, configuration, packaging, or dependencies.
- Keep it concise.

Commit the README on the current branch:

```bash
git add README.md
git commit -m "add README"
```

Reply with a one-line summary and the commit hash.

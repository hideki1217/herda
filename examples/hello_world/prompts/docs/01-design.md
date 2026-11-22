You are working in a separate `docs` worktree of the same tiny project.
The code is being written on another branch, so do not wait for it and do not
try to read code files.

Create `docs/plan.md` with a short outline for the project README:

- What the project is: a Python hello-world program.
- Intended usage:
  - `python3 hello.py`
  - `python3 hello.py Ada`
- Development check: `python3 -m unittest -v`
- Keep it under 25 lines.

Do not create `README.md` yet; a later prompt will do that.

Commit the outline on the current branch:

```bash
git add docs/plan.md
git commit -m "add README outline"
```

Reply with a short summary and the commit hash.

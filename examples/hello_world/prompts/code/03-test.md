Work in the current worktree. The previous kernels created `hello.py` with a
`greet()` function and a small `argparse` CLI.

Add tests using only the Python standard library:

1. Create `test_hello.py`.
2. Use `unittest`.
3. Cover:
   - `greet()` returns `"Hello, world!"`.
   - `greet("Ada")` returns `"Hello, Ada!"`.
   - Running `python3 hello.py Ada` prints `Hello, Ada!` (use
     `subprocess.run` with `sys.executable`).
4. Run `python3 -m unittest -v` and make all tests pass.
5. Do not rewrite the feature; only change `hello.py` if a real bug prevents
   the tests from passing.

Commit only the files you changed on the current branch, for example:

```bash
git add test_hello.py
git commit -m "add unittest coverage"
```

Reply with the test command, the result, and the commit hash.

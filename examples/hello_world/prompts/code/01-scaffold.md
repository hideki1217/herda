You are working in a fresh git worktree of a tiny Python project.

Create the first version of a hello-world program:

1. Create `hello.py` in the repository root.
2. Define:

   ```python
   def greet(name: str = "world") -> str:
       return f"Hello, {name}!"
   ```

3. Add a `if __name__ == "__main__":` block that prints `greet()`.
4. Do not add tests or a README yet.
5. Run `python3 hello.py` and verify it prints exactly:

   ```
   Hello, world!
   ```

Keep the implementation small and standard-library only.

When the command succeeds, commit only the file you created on the current
branch:

```bash
git add hello.py
git commit -m "add hello-world scaffold"
```

Reply with a one-line summary and the commit hash.

# hello_world example

5 つの prompt を 2 つの stream に分けて投入し、Python の hello world
プロジェクトを作る例です。

```
code stream:  scaffold(hello.py) -> greet(CLI 追加) -> test(unittest)
                    │                                  │
docs stream:        └─> design(docs/plan.md) -> readme <┘
```

- `code` と `docs` は別々の git worktree / branch です。
- `code/greet` は `inherit_session=True` で `code/scaffold` の会話を引き継ぎます。
- Stream 内は宣言順に直列です。Kernel に `depends_on` は付けません。
- `code` は `scaffold_done` を record します。
- `docs` は `wait_event(scaffold_done)` の後に `design` を実行します。
- `docs/readme` は `wait_stream(code)` で code stream 全体（scaffold/greet/test）
  の完了を待ってから実行します。`inherit_session=True` で `design` の会話も
  引き継ぎます。
- 各 prompt は最後に `git add` / `git commit` を行います。herda 自身は
  commit しません。branch に成果を残すのは prompt / agent の責務です。
- herda は stream 間でファイルを自動マージしません。成果は branch ごとに残ります。

## 実行

リポジトリのルートで:

```bash
cd examples/hello_world
HERDA_MODEL=fireworks/accounts/fireworks/models/deepseek-v4p1-flash-1017 \
  ./run.sh
```

`run.sh` は `workspace/` に小さな git リポジトリを作ってから
`herda run graph.py` を実行します。モデルは `HERDA_MODEL` で指定してください
（未指定時は `anthropic/claude-sonnet-5-5`）。

`herda doctor` で `git` / `hc` / `pi` を確認できます。

## 結果を見る

実行が終わると run_id と各 Kernel の状態が表示されます。branch は
`workspace` リポジトリに残っているので、worktree で確認できます。

```bash
cd workspace
git branch --list 'herda/hello-world/*'
git worktree list
```

`code` stream の worktree は次のような場所です。

```bash
cd .herda/worktrees/hello-world/<run_id>/code
python3 hello.py
python3 hello.py Ada
python3 -m unittest -v
```

`docs` stream の worktree では、リポジトリ root の README を確認できます。

```bash
cd ../docs
cat README.md
```

各 kernel が commit 済みなので、main に merge できます。code は fast-forward、
docs は code を先に merge すると merge commit になります（どちらも clean に統合できます）。

```bash
cd workspace
git merge herda/hello-world/code-<run6>
git merge herda/hello-world/docs-<run6>
```

`run6` は run_id の末尾 6 文字です。

## 後片付け

herda は worktree を自動削除しません。run_id を指定して明示的に消します。

```bash
cd workspace
herda clean <run_id> --worktrees
```

各 worktree は prompt が commit 済みなので、`--force` なしで削除できます。
commit に失敗した kernel がある場合は dirty として拒否されるので、worktree で
commit するか、成果を捨ててよい場合だけ `--force` を付けてください。

branch は削除されません。不要なら通常どおり `git branch -D` で消してください。
run ディレクトリまで消す場合は `herda clean <run_id> --worktrees --purge` を使います。

## Mermaid

```bash
cd examples/hello_world
./setup.sh
PYTHONPATH=../.. python3 -m herda draw graph.py -o dag.mmd
```

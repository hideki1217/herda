#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace="$here/workspace"

if [ ! -d "$workspace/.git" ]; then
  mkdir -p "$workspace"
  git -C "$workspace" init -b main
  git -C "$workspace" config user.name "herda example"
  git -C "$workspace" config user.email "example@herda.invalid"
  cat > "$workspace/README.md" <<'EOF'
# hello-world workspace

This repository is created by examples/hello_world/setup.sh.
The prompts in this example build the hello-world project here.
EOF
  git -C "$workspace" add README.md
  git -C "$workspace" commit -m "initial commit"
fi

printf 'workspace ready: %s\n' "$workspace"

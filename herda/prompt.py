"""Prompt path resolution and validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import PromptError


def resolve_prompt(base_dir: Path, prompt: str | Path) -> Path:
    path = Path(prompt)
    if not path.is_absolute():
        path = base_dir / path
    return path.expanduser().resolve()


def load_prompt(path: Path) -> str:
    if not path.exists():
        raise PromptError(
            "prompt_missing",
            f"prompt does not exist: {path}",
            "check the path relative to Graph.base_dir",
        )
    if not path.is_file():
        raise PromptError(
            "prompt_not_file",
            f"prompt is not a regular file: {path}",
        )
    if path.suffix.lower() != ".md":
        raise PromptError(
            "prompt_not_markdown",
            f"prompt must be a .md file: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PromptError(
            "prompt_not_utf8",
            f"prompt is not valid UTF-8: {path}",
        ) from exc
    except OSError as exc:
        raise PromptError(
            "prompt_unreadable",
            f"could not read prompt {path}: {exc}",
        ) from exc
    if not text.strip():
        raise PromptError(
            "prompt_empty",
            f"prompt is empty: {path}",
        )
    return text


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

"""Name validation and deterministic slugs/ids.

The user-facing names are free Unicode (with a few forbidden characters).
Everything that has to be a git branch component, a path component or a
herdr terminal name goes through :func:`slugify`.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

MAX_NAME_LENGTH = 64

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SLUG_SEP_RE = re.compile(r"[^a-z0-9]+")


def validate_name(kind: str, name: str) -> str:
    from .errors import ValidationError

    if not isinstance(name, str):
        raise ValidationError(
            "invalid_name",
            f"{kind} name must be a string",
            f"pass a non-empty {kind.lower()} name",
        )
    if not name:
        raise ValidationError(
            "invalid_name", f"{kind} name must not be empty"
        )
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            "invalid_name",
            f"{kind} name {name!r} is {len(name)} characters; the limit is "
            f"{MAX_NAME_LENGTH}",
        )
    if _CONTROL_RE.search(name):
        raise ValidationError(
            "invalid_name",
            f"{kind} name {name!r} contains a control character",
        )
    if "/" in name:
        raise ValidationError(
            "invalid_name",
            f"{kind} name {name!r} must not contain '/'",
            "'/' separates stream and kernel names (stream/kernel)",
        )
    if name in (".", ".."):
        raise ValidationError(
            "invalid_name",
            f"{kind} name {name!r} is reserved for path traversal",
        )
    return name


def short_hash(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def slugify(name: str, *, fallback: str = "x") -> str:
    """Return a lowercase ASCII identifier suitable for git/path use.

    Unicode is NFKD-normalized and non-ASCII characters are dropped. If
    nothing usable remains, a short hash of the original name is appended so
    different names cannot collapse into the same slug.
    """

    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_SEP_RE.sub("-", ascii_text).strip("-")
    if not slug:
        return f"{fallback}-{short_hash(name, 6)}"
    return slug


def space_id(graph_slug: str, repo: str, graph_name: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{graph_name}".encode("utf-8")).hexdigest()
    return f"herda-{graph_slug}-{digest[:4]}"


def tab_id(space: str, stream_slug: str, stream_name: str) -> str:
    digest = hashlib.sha256(stream_name.encode("utf-8")).hexdigest()
    return f"{space}-{stream_slug}-{digest[:4]}"


def terminal_name(
    stream_slug: str,
    kernel_slug: str,
    run_id: str,
    stream_name: str,
    kernel_name: str,
) -> str:
    """``herda-<stream>-<kernel>-<hash6>`` capped at 32 characters.

    herdr allows ``[a-z0-9_-]`` and at most 32 characters. When the two slugs
    together exceed 18 characters the rear of the pair is trimmed.
    """

    digest = short_hash(f"{run_id}\0{stream_name}\0{kernel_name}", 6)
    pair = f"{stream_slug}-{kernel_slug}"
    if len(pair) > 18:
        pair = pair[:18].rstrip("-_")
    name = f"herda-{pair}-{digest}"
    return name[:32].rstrip("-_")


def mermaid_id(prefix: str, name: str, used: set[str]) -> str:
    base = f"{prefix}_{slugify(name)}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def iso_utc(value: object) -> str | None:
    from datetime import timezone

    if value is None:
        return None
    if not hasattr(value, "astimezone"):
        return None
    dt = value.astimezone(timezone.utc)  # type: ignore[union-attr]
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str | None):
    if not value:
        return None
    from datetime import datetime, timezone

    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

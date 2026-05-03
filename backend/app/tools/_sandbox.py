"""Sandbox path resolution shared by file-touching tools.

Per ADR 0003 §5: read/write tools are scoped to a single sandbox directory.
We resolve every path through `safe_path`, which rejects symlink and `..`
traversal *and* absolute paths that escape the sandbox root.
"""

from pathlib import Path

from backend.app.config import get_settings


class SandboxError(ValueError):
    """Raised when a tool argument tries to step outside the sandbox."""


def sandbox_root() -> Path:
    root = Path(get_settings().agent_sandbox).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_path(user_path: str) -> Path:
    """Resolve `user_path` relative to the sandbox; reject anything that escapes.

    `Path.resolve()` collapses `..` and follows symlinks, so we can compare the
    final real path against the sandbox real path. `is_relative_to` is the
    canonical containment check on Python 3.9+.
    """
    if not user_path or user_path.strip() != user_path:
        raise SandboxError("path must be a non-empty, untrimmed string")
    root = sandbox_root()
    target = (root / user_path).resolve()
    if not target.is_relative_to(root):
        raise SandboxError(f"path escapes sandbox: {user_path!r}")
    return target

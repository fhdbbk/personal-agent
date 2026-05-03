from pathlib import Path

# backend/app/agent/prompt.py -> repo root is parents[3]
SOUL_PATH = Path(__file__).resolve().parents[3] / "SOUL.md"


def system_prompt() -> str:
    """Read SOUL.md fresh on every call so personality edits are hot."""
    return SOUL_PATH.read_text(encoding="utf-8").strip()

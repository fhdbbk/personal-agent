"""Smoke-test the list_files tool directly (no agent, no LLM).

Sets up a small fixture inside the sandbox, lists it, and checks that:
  1. The root listing succeeds and contains the fixture entries.
  2. A nested directory lists correctly.
  3. Non-existent and not-a-directory paths raise the expected errors.
  4. Sandbox-escape paths are rejected by safe_path.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.tools._sandbox import SandboxError, sandbox_root
from backend.app.tools.list_files import list_files


FIXTURE_DIR = "smoke_list_files_fixture"


def _setup_fixture() -> None:
    root = sandbox_root() / FIXTURE_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "alpha.txt").write_text("first")
    (root / "Beta.md").write_text("second second")
    (root / "subdir").mkdir(exist_ok=True)
    (root / "subdir" / "inner.txt").write_text("inside")


async def expect_ok(path: str, must_contain: list[str]) -> bool:
    print(f"\n=== OK case: list_files({path!r}) ===")
    try:
        out = await list_files(path)
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    print(out)
    missing = [s for s in must_contain if s not in out]
    if missing:
        print(f"  FAIL: missing expected entries: {missing}", file=sys.stderr)
        return False
    return True


async def expect_raise(path: str, exc_type: type, label: str) -> bool:
    print(f"\n=== REJECT case ({label}): list_files({path!r}) ===")
    try:
        out = await list_files(path)
    except exc_type as e:
        print(f"  rejected as expected: {type(e).__name__}: {e}")
        return True
    except Exception as e:
        print(f"  WRONG ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    print(f"  FAIL: should have raised {exc_type.__name__}. Got: {out[:200]}", file=sys.stderr)
    return False


async def main() -> int:
    _setup_fixture()
    results = []

    results.append(await expect_ok(".", [FIXTURE_DIR]))
    results.append(
        await expect_ok(
            FIXTURE_DIR,
            ["subdir/", "alpha.txt", "Beta.md"],
        )
    )
    results.append(await expect_ok(f"{FIXTURE_DIR}/subdir", ["inner.txt"]))

    results.append(await expect_raise(f"{FIXTURE_DIR}/no_such", FileNotFoundError, "missing path"))
    results.append(
        await expect_raise(f"{FIXTURE_DIR}/alpha.txt", NotADirectoryError, "file not dir")
    )
    results.append(await expect_raise("../etc", SandboxError, "escape attempt"))

    bad = sum(1 for ok in results if not ok)
    print(f"\n{'PASS' if bad == 0 else f'FAIL ({bad})'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

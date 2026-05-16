# Pytest patterns: fixtures, autouse, and asyncio_mode

What we worked out while reading [backend/tests/test_agent_loop.py](../../backend/tests/test_agent_loop.py) during [2026-05-16](../sessions/2026-05-16-ui-polish-num-ctx-loop-tests.md). Two distinct gotchas worth keeping: how `autouse` fixtures combine with `yield` for teardown, and why pytest-asyncio's `asyncio_mode` setting deserves a careful choice.

## Fixtures in one paragraph

A fixture is a function pytest calls to set up (and optionally tear down) state for tests. Declared with `@pytest.fixture`. A test "requests" a fixture by naming it as a parameter (`def test_x(some_fixture): ...`), or the fixture can opt every test into itself by being marked `autouse=True`. A fixture that does setup + teardown uses `yield` — code before `yield` runs before the test, code after runs after, even if the test fails. Think of `yield` as "the test happens here."

## The autouse + yield + lru_cache pattern

[backend/tests/test_agent_loop.py:23-29](../../backend/tests/test_agent_loop.py#L23-L29):

```python
@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()
```

This fixture exists because `get_settings()` in [backend/app/config.py](../../backend/app/config.py) is wrapped in `functools.lru_cache`. The first call freezes the `Settings` object — and the `PA_*` env vars it read — for the rest of the process.

The leak hazard: a test uses `monkeypatch.setenv("PA_AGENT_MAX_STEPS", "2")` and calls `get_settings.cache_clear()` so the new value takes effect. If we don't reset on teardown, the *modified* settings stay cached past the test, and the **next** test silently inherits `PA_AGENT_MAX_STEPS=2`. Order-dependent failures, very hard to debug.

The fixture's job: call `get_settings.cache_clear()` on **teardown** of every test (the line after `yield`). `autouse=True` means we don't have to remember to opt in — pytest applies it automatically.

The general principle: **any module-level cache that depends on test-mutable state needs a reset hook.** `lru_cache`, module-level singletons, connection pools, etc. An autouse teardown fixture is the cheapest way to enforce that.

## `asyncio_mode = "auto"` and the silent-pass trap

From [pyproject.toml:29](../../pyproject.toml#L29):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

This is a [`pytest-asyncio`](https://pytest-asyncio.readthedocs.io/) setting controlling which `async def` tests it runs on an event loop.

| Mode | Behaviour |
|---|---|
| `auto` | Every `async def test_*` is automatically treated as an asyncio test. No decorator needed. |
| `strict` (the default) | Only async tests marked `@pytest.mark.asyncio` get an event loop. |

We picked `auto` because the agent loop is async end-to-end — every test in [test_agent_loop.py](../../backend/tests/test_agent_loop.py) is `async def`, so requiring a marker on every one would just be noise.

### Why `strict` would have been actively dangerous here

If `asyncio_mode = "strict"` and you forget the `@pytest.mark.asyncio` marker, pytest doesn't error. It runs the function, Python constructs the coroutine object, and the function returns immediately without ever being awaited. You get:

- A green checkmark.
- A `RuntimeWarning: coroutine '...' was never awaited` buried in the output.
- **None of the assertions in the test body actually ran.**

A test asserting `assert False` inside an unawaited coroutine would still "pass." That's the failure mode to remember: the danger of `strict` isn't that tests fail loudly — it's that they pass silently.

### When `strict` is the right choice instead

Codebases where most tests are sync and async ones are the exception. There, requiring an explicit marker keeps the async-ness visible at the call site and prevents accidental async tests from being introduced without thought. The rule of thumb: if `grep -c "async def test"` is most of your test count, use `auto`; if it's the minority, use `strict`.

## Why both notes belong in the same file

They're both pytest behaviours that look like configuration trivia but have load-bearing correctness implications. Both fail in the *silent* direction — stale settings bleeding between tests, or async tests that never run — and both are cheap to get right once you know the shape of the trap.

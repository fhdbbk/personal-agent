# Protocol vs ABC: why `LLMProvider` is a Protocol

What we worked out while writing the LLM provider abstraction during [2026-05-16](../sessions/2026-05-16-llm-provider-abstraction.md). The decision to make [`LLMProvider`](../../backend/app/llm/base.py) a `typing.Protocol` rather than an `abc.ABC` shaped how the rest of the seam fits together — and the rule of thumb that came out of it is worth remembering.

## The one-line difference

Both describe an interface — "to be an X, implement these methods." They differ on **how Python decides whether a class qualifies**:

- **ABC: nominal typing.** You qualify by explicitly inheriting. `class Foo(MyABC):` plus override the abstract methods.
- **Protocol: structural typing** (duck typing for types). You qualify by having the right shape. No inheritance required.

Everything else follows from that.

## What it looks like in this codebase

None of the three provider adapters inherits from `LLMProvider`:

- [backend/app/llm/ollama.py:23](../../backend/app/llm/ollama.py#L23) — `class OllamaProvider:`
- [backend/app/llm/anthropic.py:42](../../backend/app/llm/anthropic.py#L42) — `class AnthropicProvider:`
- [backend/app/llm/openai.py:25](../../backend/app/llm/openai.py#L25) — `class OpenAIProvider:`

They don't even import `LLMProvider`. They just have a `chat_stream` method with the matching signature. Yet pyright/mypy happily accepts:

```python
provider: LLMProvider = OllamaProvider(settings)
```

…because structural typing only cares about the shape. The `FakeProvider` in [backend/tests/test_agent_loop.py](../../backend/tests/test_agent_loop.py) is the same — no inheritance, no import of `LLMProvider`, and yet it satisfies the `provider: LLMProvider` parameter on `run_turn`.

With an ABC, every implementer (including every test fake) would need `class XProvider(LLMProvider):` and would have to import the base class. The Protocol lets the test file stay decoupled from the `llm/` package — and lets the provider adapters keep their dependency direction one-way (they import the SDKs, not each other).

## Trade-off table

| Concern | `Protocol` | `ABC` |
|---|---|---|
| How you qualify | Have the methods | Inherit + override |
| Coupling to the interface module | None — Protocol stays invisible to implementers | Hard — every implementer must import and inherit |
| Runtime enforcement | Off by default; opt in with `@runtime_checkable` + `isinstance(...)` | Automatic — `TypeError` if you instantiate without overriding |
| Default / mixin behaviour | Methods are signatures only (`...` body) — no shared code | ABCs can carry real method implementations subclasses inherit |
| Retrofitting over types you don't own | Yes — describe `Sequence`-like shapes from third-party libs without modifying them | No — you'd have to wrap or subclass |
| Discoverability | "What implements this?" is harder — no inheritance graph | Easy: `cls.__subclasses__()` lists implementers |
| Multiple interfaces | Trivial — a class can match N Protocols | Possible via multiple inheritance, gets messy fast |

## Why Protocol fit this particular seam

Three reasons made it the right pick for `LLMProvider` specifically:

1. **No shared implementation to inherit.** Every adapter's `chat_stream` is bespoke translation work — Anthropic's content blocks have nothing in common with OpenAI's indexed fragments. There's no template-method scaffolding the adapters could share, so a base class would just be a sterner contract with extra ceremony.
2. **Dependency direction stays one-way.** With ABC inheritance, each adapter would import `llm.base`. Today they don't. The package boundary is sharper because of that: `llm/base.py` is pure types, leaf-imported by callers but importing nothing from peer modules at runtime (see also [`TYPE_CHECKING`](../../backend/app/llm/base.py#L21-L24) for the same idea on `Tool`).
3. **Tests stay decoupled.** The same Protocol that the real adapters match is what the `FakeProvider` matches. Tests don't need to know `LLMProvider` exists.

## When ABC is the right call instead

The pattern flips when:

- **You have real code to share.** Template-method with hook overrides, common helpers, utility methods on the base. `io.IOBase` is the textbook example — concrete implementations of `readlines()` etc. that subclasses inherit for free.
- **You want runtime enforcement that's catchable at instantiation time.** A plugin registry that scans `cls.__subclasses__()` and refuses to load classes missing the contract; or a base class whose `__init__` requires subclasses to set certain attributes. `MyABC()` failing with a clear "must override foo, bar" is a better DX than mypy errors, when you're shipping a library to users who may not be running a type checker.
- **You need the class to be the source of truth at runtime.** If your code uses `isinstance(x, MyInterface)` as a real branch point (not just a debug assertion), an ABC's automatic subclass tracking is cheaper than a `@runtime_checkable` Protocol — Protocols' `isinstance` is slow because it has to walk every attribute.

## Rule of thumb

**Protocol for "what shape are you?". ABC for "what hierarchy are you in, and what shared scaffolding do you get?".**

Most modern Python interface design has been drifting toward Protocol since [PEP 544](https://peps.python.org/pep-0544/) (Python 3.8+) precisely because most interfaces don't need the shared-implementation half of an ABC — they only need the contract half, which is what Protocols give you without the inheritance tax.

## The dead end we didn't take

The first instinct when designing `LLMProvider` was an ABC, on autopilot:

```python
class LLMProvider(ABC):
    @abstractmethod
    async def chat_stream(self, messages, tools): ...

class OllamaProvider(LLMProvider):
    async def chat_stream(self, messages, tools):
        ...
```

Three things made this feel wrong, in order:

1. **The test fake would need to import the ABC.** That's the kind of import that grows into a knot when you're refactoring later. Catching it now meant the Protocol path stayed feasible.
2. **There was nothing to put on the base class except `...`.** An ABC whose only contents are abstract-method stubs is a Protocol cosplay — same purpose, more import overhead.
3. **Adapters living in `llm/{ollama,anthropic,openai}.py` having to type `class X(LLMProvider):` to satisfy a contract they already satisfy by shape is busywork.** Easy to forget on a new adapter; nothing about the failure mode (a runtime TypeError on first instantiation) is better than what a type checker would already tell you.

## Related

- [`TYPE_CHECKING` in base.py](../../backend/app/llm/base.py#L21-L24) — same family of idea: keep the typing infrastructure invisible at runtime. The `Tool` import is only resolved by type checkers, so `llm/base.py` doesn't pull `tools/registry` into memory at runtime, and a future circular import is prevented.
- [ADR 0007](../decisions/0007-llm-provider-abstraction.md) — the design rationale that picked Protocol over ABC + LiteLLM-style normalizers as the right pivot for this abstraction.

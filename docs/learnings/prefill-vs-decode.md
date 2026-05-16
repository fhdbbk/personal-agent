# Prefill vs decode: why time-to-first-token dominates

What we learned poking at chat latency during [2026-05-02](../sessions/2026-05-02-phase-1-chat-mvp.md) when [scripts/smoke_chat_ws.py](../../scripts/smoke_chat_ws.py) reported `ttft=8816ms total=9039ms` — first token took 8.8 s, the remaining ten tokens added 0.2 s combined.

## The headline

LLM inference happens in two phases with very different costs:

| Phase | What it does | Cost shape |
|---|---|---|
| **Prefill** | Run the entire input prompt through every transformer layer once, compute keys/values for each token, store them in the KV cache. | `O(prompt_length)` — one big batched matmul per layer. |
| **Decode** | Generate one output token at a time, computing attention only for the new token against the cached prefix. | `O(1)` per token — small matmul, then sample. |

Time to first token (TTFT) is essentially **prefill time**. Time per subsequent token is **decode time**. They can differ by 10–100×, especially on CPU.

## Why prefill is so much heavier

A transformer's attention layer needs every token's key/value vector at every layer to attend over them. For an N-token prompt:

- **Prefill** does one forward pass over all N tokens at once. The matmul shapes are big (N × hidden) — exactly the workload GPUs are built for. Even so, it's N times more arithmetic than processing one token.
- **Decode** is the autoregressive bit. The new token attends to all N+i prior tokens, but the prior tokens' K/V are already in the cache from prefill — no recomputation. Each step is a tiny matmul.

So once prefill finishes, decode steps are nearly free in comparison. The cliff between them is what creates the "long pause, then a stream" feel.

## What we measured

```
prompt: SOUL.md system prompt (~300 tok) + new user message (~10 tok)
output: "1, 2, 3, 4, 5"        (~10 tok)

ttft   = 8816 ms   (≈ prefill time for ~310 tokens)
total  = 9039 ms
decode = 9039 - 8816 = 223 ms for 10 tokens ≈ 22 ms/token
```

Ratio: prefill cost per token (~28 ms) is in the same ballpark as decode cost per token (~22 ms), but prefill processes ~30× more tokens than decode produces. So prefill wins the wall-clock fight by a mile.

The mental model: `total ≈ prefill + N_out × decode_per_token`. Prompt length is the lever for the first term, output length for the second.

## How Ollama reports these on the wire

Ollama's `chat()` response surfaces the prefill/decode split as three fields on the **final** chunk of a stream (`done=True`); intermediate chunks have them as `None`:

| Field | What it counts |
|---|---|
| `prompt_eval_count` | Tokens in the **input** — system prompt + history + tool definitions + any tool results so far. This is the prefill work. |
| `eval_count` | Tokens the model **generated** this call. This is the decode work. |
| `eval_duration` | Nanoseconds spent generating those output tokens. `eval_count / (eval_duration / 1e9)` → tokens/sec. |

There's also `prompt_eval_duration` for prefill time and `load_duration` for the cold model-load cost, which we don't currently surface.

[backend/app/agent/loop.py:163-169](../../backend/app/agent/loop.py#L163-L169) accumulates these across every iteration of a ReAct turn — one user message can trigger multiple `chat()` calls (model → tool → model → …), each with its own prefill + decode. The loop sums them so the WebSocket handler can report a single per-turn figure.

Worth knowing for tests: forging these fields in a fake `ChatResponse` (e.g. [backend/tests/test_agent_loop.py:107-112](../../backend/tests/test_agent_loop.py#L107-L112)) is how we assert the loop's aggregation logic adds up correctly without a live Ollama. See also [pytest-patterns.md](pytest-patterns.md).

## Why CPU vs GPU matters more for prefill

Prefill is one giant matrix multiplication that parallelizes beautifully — exactly what GPU tensor cores are designed for. CPUs do it serially-ish (SIMD helps but can't fully close the gap). Decode is tiny matmuls; the parallelism advantage matters less.

That's why flipping `PA_OLLAMA_DEVICE` from `cpu` to `gpu` can collapse TTFT by an order of magnitude while only modestly improving per-token decode speed. The bottleneck shifts: on CPU, prefill is the wall; on GPU, decode often becomes the dominant term for long replies.

## KV cache and prefix reuse

The KV cache built during prefill isn't always thrown away after the turn. Some inference runtimes (Ollama / llama.cpp included) can **keep it across turns** if the prompt prefix is identical. Concretely:

- Turn 1: prefill the full prompt → KV cache covers the whole prompt.
- Turn 2: prompt = `[system, …turn1, new user msg]`. The system + turn1 prefix is unchanged → reuse those KV entries; only prefill the new tokens.

This is why **prompt-prefix stability matters**. If you splice or reorder messages between turns, you invalidate the cache and pay the full prefill cost again. Same goes for any change to the system prompt — that's at the start, so it busts everything downstream.

Practical consequence: re-reading SOUL.md on every turn (which we do) is fine *only because the file usually doesn't change between turns*. The day we start dynamically rewriting SOUL.md mid-conversation, we'd lose cache reuse and TTFT would jump.

## What streaming actually buys us

Streaming does **not** reduce total time. The wall-clock to finish a reply is the same whether you stream or not. What it changes is the *user's perception*: instead of waiting `ttft + N_out × decode` in silence and then seeing a wall of text, the user sees activity at `ttft` and then watches it grow. For an 8-second prefill, that's the difference between "broken" and "thinking."

This is why we made the WebSocket the primary chat path even before optimization — perceived latency is the only latency that matters to the human at the keyboard.

## Practical levers

When TTFT feels bad:

- **Shrink the system prompt.** Each token saved in SOUL.md is one fewer token to prefill on every turn. CLAUDE.md's "keep it terse" guidance is partly about this.
- **Keep prompt prefixes stable** so KV cache reuse can kick in across turns. Don't shuffle history order or splice in new context at the front.
- **Use the GPU.** Prefill is where the GPU pays for itself.
- **Cap conversation history.** Our [memory/buffer.py](../../backend/app/memory/buffer.py) ring buffer (`maxlen=32`) is doing this — it bounds the worst-case prompt length so TTFT can't grow without limit as a conversation drags on.

When per-token output feels slow (prefill is fine, but generation crawls):

- That's a decode problem — model size, quantization level, or pure CPU-bound throughput. Smaller / more aggressively quantized model, or GPU.

## Pitfalls worth knowing

- **The first call after server start is a special case.** It also pays the model load cost (weights into RAM/VRAM), which is on top of prefill. We saw 14 s cold vs 9 s warm in the smoke test — the 5 s gap was loading qwen3.5:4b. After the model is "hot," subsequent TTFT measurements are the real number.
- **Confusing "tokens/sec" numbers.** Some benchmarks quote prefill tokens/sec (huge, GPU-friendly), some quote decode tokens/sec (small, what end users actually feel). Check which you're reading.
- **Prefill scales worse than linearly at very long contexts** because attention is O(N²) in sequence length. For our short prompts it doesn't matter, but it's why doubling a 32k-token prompt can quadruple TTFT.

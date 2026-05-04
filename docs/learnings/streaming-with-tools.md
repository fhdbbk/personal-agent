# Streaming an Ollama call that has tools enabled

What we learned restoring token-by-token UX after Phase 2 shipped non-streaming on 2026-05-03 (evening), and the probe-driven fix later that night. ADR 0003 §1 deferred this; [ADR 0004](../decisions/0004-streaming-with-tools.md) is the resolution.

## The headline

`ollama.AsyncClient.chat(stream=True, tools=[...])` works *as you'd hope* against `qwen3.5:4b` on Ollama 0.22.x: content streams normally as token deltas, and `tool_calls` arrive **complete in a single late chunk** — not interleaved, not split. So you can stream every iteration of an agent loop without a special-case branch, and still know at stream-end whether to dispatch tools or hand the answer back.

Our ADR 0003 was too cautious about this. We assumed streaming + tools would either (a) buffer content until tool_calls were decided, or (b) emit tool_calls as fragmentary deltas we'd have to merge. Neither happened. The buffered-content fear was specifically what shipped Phase 2 as non-streaming.

## The dead end

The first probe ran a stream against `tools=ollama_tool_specs()` with the prompt "Say hi in one word." It returned **68 chunks**, of which 67 had empty content and chunk 66 carried the literal string `'Hi'`. That looked exactly like the buffered-until-the-end shape we feared:

```
chunk 0..65: done=False  content=''  tool_calls=None
chunk    66: done=False  content='Hi'  tool_calls=None
chunk    67: done=True   content=''   tool_calls=None
```

The natural read: "tools=[...] makes Ollama hold content until decode finishes." Lent credibility because qwen3 does have a thinking mode and we know it sometimes holds output back. We almost left it there.

## What was actually happening

Re-running the same prompt twice more produced **3 chunks** with content streaming on chunks 0–1 and `done` on 2 — the normal streamed shape, indistinguishable from a no-tools call:

```
chunk 0: content='Hi'   tool_calls=None
chunk 1: content=' '    tool_calls=None
chunk 2: done=True
```

The 67-chunk run was a **cold-model warmup artifact** — those empty chunks weren't content holdback, they were just the wire equivalent of "still loading weights, hold on." Once the model was hot, streaming was crisp.

Lesson: when probing inference behaviour, **always run the probe twice on a warm model before drawing conclusions**. The first call after server start (or after the model has been swapped out by Ollama's keep-alive eviction) is a category we already knew from [prefill-vs-decode.md](prefill-vs-decode.md), but it bit us again here in disguise.

## The tool-calling shape, confirmed

Driving `What's in notes.txt? Use read_file.` with the same setup:

```
chunk 0: content=''  tool_calls=[ToolCall(read_file, {'path': 'notes.txt'})]
chunk 1: done=True
```

So tool_calls arrive as a single fully-formed list on a chunk near the end of the stream — not deltas across multiple chunks, not embedded in content. The last-seen value is the truth.

## What the loop now does per iteration

```python
stream = await client.chat(model=..., messages=msgs, tools=specs, stream=True, think=...)

content_chunks: list[str] = []
final_tool_calls: list = []
async for chunk in stream:
    msg = chunk.message
    if msg.content:
        content_chunks.append(msg.content)
        await on_event({"type": "token", "delta": msg.content})   # forward to UI
    if msg.tool_calls:
        final_tool_calls = list(msg.tool_calls)                   # last one wins

content = "".join(content_chunks)
if not final_tool_calls:
    return content                                                # already streamed
# else: dispatch each tool, append a {role:"tool", ...} message, continue the for loop
```

Three things to notice:

1. **No special branching** for "is this iteration going to call tools?" The same code path handles both. We only know after the stream ends, and that's fine because tool_calls land in a coherent chunk, not as deltas we'd have to assemble.
2. **`token` frames go out as content arrives.** The UI's existing assistant-message reducer (from Phase 1) appends them onto the last assistant bubble — no new frame type, no new state.
3. **The post-tool reply also streams.** When the loop iterates after dispatching, the *next* model call also streams. The user sees: assistant text (if any pre-tool reasoning) → tool card pops in → tool result → assistant text streams back. The UI's reducer correctly starts a *new* assistant message after a tool card because the last transcript item is `kind:'tool'`, not a message.

## What we didn't recover (yet)

- **Mid-stream tool_calls** — we don't know what happens if a future model emits the call name early and the args later, as deltas. The code falls back to "last seen wins" which would silently drop earlier tool_calls. Comment in [loop.py](../../backend/app/agent/loop.py) flags this; we'll fix it the day a streamed `tool_calls` actually arrives in pieces.
- **Pre-tool reasoning streaming** — qwen3.5:4b empirically emits zero content before a tool call, so the "let me check that file…" pattern is supported by the architecture but unverified in practice. If we change models, this may suddenly start happening and would Just Work.

## Pitfalls worth knowing

- **The first call after `ollama serve` starts (or after model eviction) is a different statistical population.** Empty-content chunks while weights load can look like buffered content. Warm the model with a throwaway request before measuring.
- **`stream=True` returns the iterable immediately; the model hasn't started.** The await on `client.chat(...)` only sets up the request. Time-to-first-real-chunk is the actual TTFT, not the time to get the iterator.
- **Each chunk's `message.content` is a delta, not an accumulating buffer.** Append, don't replace. (Different from some tool-calling APIs that emit cumulative state.)
- **Each chunk's `message.tool_calls` is *not* a delta** for this version of Ollama+qwen3. It's full and final when present. Don't apply the same append-rule you used for content.
- **`assistant.model_dump()` on the final response message is what we did pre-streaming.** With streaming you don't have a single `Message` to dump — you build the dict by hand: `{role: "assistant", content: <accumulated>, tool_calls: [tc.model_dump() for tc in final_tool_calls]}`. Easy to forget when retrofitting streaming into a previously non-streamed loop.

## Why this matters beyond Phase 2

The same pattern (stream content, watch a separate field for "structural" decisions, branch at stream end) generalizes. Phase 5 calendar tools, web search, anything we add later — they all live inside the same loop, and they all benefit from the streamed UX automatically. The architecture's job here was to *not* be a special case for the tool-using path. Mission accomplished, six lines of code later.

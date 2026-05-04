# 2026-05-05 — HLD + LLD design docs

## Goal

Step back from feature work and produce a synthesised architectural view of the project. ADRs answer *why*, learnings answer *what we found*, sessions answer *what happened when* — none of them answer *what is this system, top to bottom?* Add the missing layer: a High-Level Design and a Low-Level Design with Mermaid diagrams for visual understanding.

## What we did

1. **Explored the current state in parallel.** Two Explore subagents in parallel — one mapped the backend (api/chat.py, agent/loop.py, tools, memory, config, logging), one walked the docs (every ADR, every learning, both READMEs, the latest session log) and the frontend (App.tsx state shape, frame reducer, WS lifecycle). Came back with file:line anchors throughout, enough to write the docs without re-reading every module.
2. **Aligned on three structural choices via AskUserQuestion.** Scope: full 7-phase vision with planned phases marked as design intent. Tooling: Mermaid in Markdown (renders natively on GitHub and in VS Code, no committed binaries). Layout: new `docs/design/` folder with two files, matching the existing per-folder docs convention.
3. **Wrote a plan file**, approved as-is, then verified the high-precision sections against code: re-read [api/chat.py](../../backend/app/api/chat.py), [agent/loop.py](../../backend/app/agent/loop.py), [tools/registry.py](../../backend/app/tools/registry.py), [memory/buffer.py](../../backend/app/memory/buffer.py), [config.py](../../backend/app/config.py), [_sandbox.py](../../backend/app/tools/_sandbox.py), and the two tool files so frame names, callback signatures, and config defaults match the code exactly.
4. **Created [docs/design/README.md](../design/README.md).** Short index. Explains the HLD-vs-LLD split, the relationship between design docs (living) and ADRs (immutable), and the maintenance expectation (refresh after sessions that change a component's *shape*, not edits within one).
5. **Wrote [docs/design/HLD.md](../design/HLD.md).** Vision and non-goals; system context Mermaid diagram showing actors and external systems with dashed lines for planned phases; component view with subgraphs; tech-stack table cross-linked to every ADR; three "how a turn flows" walkthroughs (plain stream, tool turn with approval, planned memory-augmented); cross-cutting concerns (config, logging, sandboxing, persona, approval); per-phase delta diagrams marked Done / In progress / Planned; deployment topology diagram with the three current processes and the Vite proxy; repo-layout skeleton.
6. **Wrote [docs/design/LLD.md](../design/LLD.md).** Module-by-module reference, ~1000 lines. Process and module map; shared data structures table + class diagram; API layer with endpoint table and WS connection state diagram; full frame protocol tables (server→client and client→server) with example payloads; agent loop pseudocode with four sequence diagrams (plain stream, tool with approval, error+retry showing the consecutive-error counter cutoff, approval denied as the not-an-error case); tool registry class diagram and "how to add a tool" recipe; sandbox accept/reject flowchart; ring-buffer retention; SOUL.md hot-load rationale; complete config/logging tables; frontend state shape, frame-reducer table, component tree; planned-phase sketches (Phase 3 long-term memory with sequence diagram and SQL schema, Phase 4 voice topology with new audio frame types, Phase 5 calendar tools, Phase 6 hot-path candidates, Phase 7 mobile options); appendix file index.
7. **Updated [CLAUDE.md](../../CLAUDE.md).** Documentation-discipline section now lists four folders (added `docs/design/`) and the maintenance rule. Repo-layout tree picked up `design/` as a sibling of `decisions/`, `learnings/`, `sessions/`.

## Decisions made

No new ADRs. The choices in this session — Mermaid, two-file split, full-vision scope — are documentation organisation, not architecture. They live in [docs/design/README.md](../design/README.md) instead.

## Snags + fixes

- **None worth recording.** This was a "synthesise what's already there" session. The biggest risk was inventing details that don't match the code — mitigated by the second-pass code re-read in step 3 and by anchoring everything in `file:line` links so future drift is visible.

## Open threads / next session

- **Phase 2 second session: `web_search` + `python_exec`.** Still the natural next chunk of work, unchanged from yesterday. ADR 0003 §5 is the starting point; expect a learnings doc on Linux rlimits.
- **Unit tests on the agent loop.** Same standing item — the transport-agnostic shape exists for this. The new LLD's sequence diagrams are good fixtures to point tests at: each diagram is a test case.
- **Verify the Mermaid renders in the actual VS Code preview.** Mermaid syntax errors fail silently in some renderers. Eyeball every block on first open; if any are broken, fix in place.
- **Pick a real model for "long-term memory" before Phase 3 starts.** The HLD/LLD lock in `all-MiniLM-L6-v2`; double-check that's still the right call when Phase 3 starts (sentence-transformers landscape moves quickly).
- **Streamed mid-iteration tool_calls deltas.** Carried over.
- **UI papercuts** (long tool args/results display, "Running…" indicator). Carried over.
- **Carried forward (still on Fahad):** phone browser smoke test, disable Windows Ollama autostart, DHCP reservation, SOUL.md rewrite in own voice.

## Out-of-band notes

- The design docs cover the **full 7-phase vision** including planned phases. This is more speculative than the "current state only" alternative, but it makes the early-phase architecture visibly headed somewhere. Each planned section is explicitly marked as design intent. Expect to *replace* (not edit) those sections when each phase actually lands, the same way new ADRs replace old ones.
- Mermaid was the easy choice given the all-Markdown convention in the repo, but it's worth knowing the alternatives if the diagrams ever outgrow it: PlantUML (more expressive, requires a renderer); structurizr (C4 model native); draw.io (committed XML/SVG). Switch only if Mermaid genuinely can't express something we need.

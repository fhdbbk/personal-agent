# 2026-05-10 — `fetch_url` + `list_files` tools (backfill)

> **Backfill note.** This session happened on 2026-05-10 between the
> `web_search` session ([2026-05-10-web-search-tool.md](2026-05-10-web-search-tool.md))
> and the token-stats UI session ([2026-05-10-token-stats-ui.md](2026-05-10-token-stats-ui.md))
> but never got its own log. ADR 0006 and the two new tool files were
> sitting uncommitted in the working tree without a session entry.
> Reconstructing from the diff and the ADR.

## Goal

Two tools the agent kept hinting it wanted:

1. **`fetch_url`** — `web_search` returns title + URL + snippet, but for
   live data (currency rates, weather, prices) the snippet is stale and
   the knowledge-panel content isn't in `ddgs.text`. Need a way to follow
   a hit through to the page itself and read the live content.
2. **`list_files`** — the agent often guesses at filenames before
   `read_file` and gets it wrong. A directory listing closes that gap
   without forcing the user to write filenames into the prompt.

## What we did

1. **Wrote [`backend/app/tools/list_files.py`](../../backend/app/tools/list_files.py).** Non-recursive on purpose — a recursive deep-tree listing dwarfs the file content the model actually wants. Sorted output (dirs first, then files, then "other"), each file annotated with size in bytes, capped at 200 entries with a truncation marker. Reuses [`_sandbox.safe_path`](../../backend/app/tools/_sandbox.py) for the path-escape check; `iterdir()` runs in a thread via `asyncio.to_thread`. Raises `FileNotFoundError` / `NotADirectoryError` (the loop turns these into model-visible error strings via `_dispatch_tool`).
2. **Wrote [`backend/app/tools/fetch_url.py`](../../backend/app/tools/fetch_url.py).** HTTP client: `primp.AsyncClient(impersonate="chrome")`. Started with `httpx`, hit the same TLS-fingerprint 403 we saw with the DDG raw scrape — even Wikipedia returns 403 to httpx with a full Firefox header set, while `curl` with the same UA gets 200. Swapping to primp (already a transitive dep via `ddgs`, cf. ADR 0005) fixed it once and for all. Then removed `httpx` from direct deps — net new HTTP libs: zero.
3. **Extractor: `trafilatura.extract`.** Considered a hand-rolled `<script>`/`<style>`/`<nav>` strip with selectolax (~30 lines, more from-scratch). In practice the boilerplate-removal heuristics that make extraction usable across news sites, blogs, docs, and Wikipedia are exactly trafilatura's job. Pays for itself with one new direct dep + ~5 MB of lxml/justext. Added to [`pyproject.toml`](../../pyproject.toml).
4. **Safety: public-hosts only.** The naive `urlparse(url).hostname in {"localhost", ...}` check is bypassed by `127.0.0.1.nip.io` or DNS-rebinding tricks, so we resolve the host with `socket.getaddrinfo` and reject any address that's loopback / private / link-local / multicast / reserved / unspecified using the `ipaddress` module. Also scheme-checked: only `http` / `https`, no `file://`. Caps: 2 MB on the wire, 12 KB of extracted text fed back to the model, 15s timeout. Truncation is signalled in the result body so the model knows it didn't see everything.
5. **Approval-gated.** `fetch_url` is `requires_approval=True` in [`backend/app/tools/registry.py`](../../backend/app/tools/registry.py), matching `write_file`'s policy. Reasoning: read-only but fan-out — the model picks the URL, and an approval click makes outbound network egress visible to the user. Friction cost is small, audit value is high. ADR 0006 §"Approval-gated" walks through the trade-off.
6. **Wrote [ADR 0006](../decisions/0006-fetch-url-tool.md)** to capture the four sub-decisions: primp over httpx, trafilatura over hand-roll, approval-gating, public-host check via DNS. Open questions section flags Cloudflare/Akamai bot-mode (still defeats fingerprinting in some cases — fallback is "honest 'I couldn't read it'") and approval fatigue on heavy research turns.
7. **Smoke scripts.**
   - [`scripts/smoke_list_files.py`](../../scripts/smoke_list_files.py): builds a fixture inside the sandbox (subdir, two files, one nested file), runs `list_files(".")`, `list_files(fixture)`, `list_files(fixture/subdir)`, then exercises the error paths (missing path, file-not-dir, escape attempt). All pass.
   - [`scripts/smoke_fetch_url.py`](../../scripts/smoke_fetch_url.py): one happy-path GET against `en.wikipedia.org/wiki/Pound_sterling`, then four reject cases: `127.0.0.1:8000`, `localhost`, `10.0.0.1` (private), `file:///etc/passwd` (wrong scheme). All pass — the public-host check correctly resolves `localhost` to a loopback IP and rejects, even though the URL string itself doesn't say "127".
   - [`scripts/smoke_agent_fetch_url.py`](../../scripts/smoke_agent_fetch_url.py): end-to-end through the agent — "use web_search to find the Wikipedia article on the Pound sterling, then use fetch_url on the wikipedia.org link to read it, and summarise in one sentence." Asserts both tool_calls appear in the WS frame stream. We auto-approve the fetch.
8. **Registered both tools** in [`backend/app/tools/registry.py`](../../backend/app/tools/registry.py): `list_files` (no approval), `fetch_url` (approval). Updated the `TOOLS` dict ordering: file tools first (list_files, read_file, write_file), then web tools (web_search, fetch_url). Cosmetic — the model sees the schemas in dict-iteration order.
9. **SOUL.md tweak.** Added a one-liner under a new "Tool Use" section: *"For fetch_url, when summarising a result, name the URL you read."* Caught the model occasionally summarising without citing the source page; the prompt nudge is cheaper than a code-side guard.
10. **AI.md updates** (the source of truth that `CLAUDE.md` / `GEMINI.md` symlink to): added the ADR 0006 row, list_files / fetch_url to the repo-layout tree, the new smoke-script names, and the new commands.

## Decisions made

- **[ADR 0006](../decisions/0006-fetch-url-tool.md)** — `fetch_url` uses `primp` + `trafilatura`, approval-gated, http(s) + public hosts only.
- **No ADR for `list_files`.** Pure mechanical addition: third file tool, slots into the existing sandboxed pattern (`safe_path` + `to_thread`). Nothing architecturally novel — would have been ADR-noise.

## Snags + fixes

- **httpx 403s on Wikipedia.** Wasted maybe 20 minutes on header tricks (Accept-Language, full Firefox UA, Referer, Origin) before remembering ADR 0005's exact lesson and switching to primp. The pattern is now: *if a public site returns 4xx to a Python HTTP client and 200 to curl, it's TLS/HTTP-2 fingerprinting, not headers.* Don't repeat the header-tweak dance.
- **`localhost` resolves to loopback at the OS layer, not the URL parser.** First version of the safety check did a string compare on `parsed.hostname`. That doesn't catch `localhost` (which resolves to 127.0.0.1) or `127.0.0.1.nip.io` (resolves to 127.0.0.1) or any internal-only DNS record. Switched to `getaddrinfo` + `ipaddress.is_*` checks; smoke tests cover both bypasses.
- **Approval-fatigue is now a real-feeling cost on multi-hop research.** The smoke chained one search + one fetch and it was fine. Two or three follow-up fetches in a single turn would be annoying. ADR 0006's "open questions" tracks this; the documented fix is session-scoped host allow-listing if it bites.

## Open threads / next session

- **`python_exec` is still the next major Phase 2 item.** Subprocess + `RLIMIT_CPU` + `RLIMIT_AS` + sandbox-dir chdir + no-network. Owe a learnings doc on Linux rlimits (carried from earlier sessions).
- **Approval-fatigue mitigation.** If `fetch_url`'s click count becomes annoying in real use, the documented fix is per-host session allow-listing — strictly easier than going the other direction. Don't preempt.
- **Cloudflare/Akamai bot-mode coverage.** Some sites still 403 even primp. The agent's fallback is to report "couldn't read the page" and try the next search hit. Watch for sites we *care* about that fall in this gap; if e.g. a major news domain stops working, revisit (Playwright is the next rung up the ladder, with a real cost).
- **Per-host rate limits.** None today. `PA_AGENT_MAX_STEPS=8` × `PA_AGENT_MAX_RETRIES_PER_TOOL=2` caps the worst case at <20 calls per turn, well under any reasonable threshold. Revisit if the ceiling moves.
- **Carried forward:** unit tests on the agent loop; phone-browser smoke; disable Windows Ollama autostart; DHCP reservation; SOUL.md rewrite in own voice.

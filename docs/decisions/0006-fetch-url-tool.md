# 0006 — `fetch_url` tool: primp + trafilatura, approval-gated

**Status:** Accepted
**Date:** 2026-05-10
**Phase:** 2

## Context

`web_search` returns title / URL / snippet triples (ADR 0005). For
exploratory questions the snippet is enough, but the model fails on
*live data*: currency conversion, weather, stock quotes, scoreboards.
Two reasons:

1. **Snippets are stale.** DDG's body field is taken from the indexed
   page, often days or weeks old. "1 GBP in USD" rendered last Tuesday
   is not the rate the user wants today.
2. **Knowledge-panel content isn't in `ddgs.text`.** The instant-answer
   widgets that show the live rate or the local forecast are rendered
   client-side or fetched from a separate endpoint; they don't appear
   in the result body the library returns.

The agent needs a way to follow a search hit through to the actual
page and read the live content.

## Decision

Add a new tool `fetch_url(url)` that GETs an http(s) URL and returns
the main text content with boilerplate stripped.

- **HTTP client:** `primp.AsyncClient` with `impersonate="chrome"`. Already
  a transitive dep via `ddgs` (cf. ADR 0005). Real browser TLS / HTTP-2
  fingerprint defeats the same fingerprint-based 403s that bit us with
  the DDG raw scrape.
- **Extractor:** `trafilatura.extract` for main-content extraction. New
  direct dep. Pulls `lxml` and a few small libraries; ~5 MB installed.
- **Safety:** http(s) only, public hosts only. We resolve the hostname
  and reject any address that's loopback / private / link-local /
  multicast / reserved / unspecified. The agent cannot be talked into
  hitting `localhost`, `127.0.0.1`, RFC1918 ranges, or `file://`.
- **Caps:** 2 MB download cap on the wire, 12 KB cap on extracted text
  fed back to the model. Truncation is signalled in the result body.
- **Approval-gated:** `requires_approval=True` in the registry. Every
  fetch surfaces the URL via the `tool_approval` frame before the
  request goes out. This matches `write_file`'s policy — both tools
  produce side effects the user should see (one local, one remote).

## Why these pieces, and what we rejected

### `primp` over `httpx`

We started with `httpx` (clean async API, sane defaults). Wikipedia,
which is the friendliest possible test target, returns **403** to
`httpx` with any `User-Agent`, including a full Firefox header set.
`curl` with the same UA returns 200. The difference is TLS / HTTP-2
fingerprint, not headers — exactly the failure mode ADR 0005 already
documented for DDG. `primp` solves this once, and `ddgs` already
brings it in as a transitive dep, so we get the fingerprinting client
for free without a second HTTP library in the deps tree.

We added `httpx` as a direct dep first and removed it after the
primp swap. Net result: zero new HTTP libraries.

### `trafilatura` over rolling our own

A plain `<script>` / `<style>` / `<nav>` / `<footer>` strip with
selectolax would be ~30 lines and "more from-scratch", but in practice
the boilerplate-removal heuristics that make extraction usable across
news sites, blogs, docs, and Wikipedia are exactly what trafilatura
is for. The cost is one new direct dep and ~5 MB of lxml/justext.
For a tool whose value is "give the model clean readable text" the
library earns its keep.

We did not consider `BeautifulSoup` — heavier interface, and its
default extraction is identical to "get all text", which is the
problem we're trying to avoid.

### Approval-gated, not auto-run

`web_search` is not approval-gated because it's read-only and goes
to one well-known endpoint. `fetch_url` is also read-only, but it
can hit *any* URL the model decides on, including ones the user has
not seen. The risk isn't local damage — it's outbound network egress
to URLs the user did not authorise. We don't think this is a serious
threat for a single-user laptop project, but the friction cost of an
approval click is small (the UI already supports it for `write_file`),
and the audit value is high. If the friction becomes annoying we can
add an "auto-approve hosts I've approved this session" mechanism
later — that's strictly easier than going the other direction.

### Public-host check via DNS resolution

The naive validation (`urlparse(url).hostname in {"localhost", ...}`)
is bypassed by anyone (including the model) writing
`http://127.0.0.1.nip.io/` or even DNS rebinding tricks. We instead
resolve the host and check every returned address with the
`ipaddress` module. This catches:

- explicit private literals (`10.0.0.1`, `192.168.1.1`)
- explicit loopback (`127.0.0.1`, `[::1]`)
- DNS names that resolve to private ranges (`localhost`, custom
  records pointing at internal services)
- link-local, multicast, reserved, unspecified ranges

It does *not* catch DNS rebinding mid-flight (the resolve we do and
the resolve primp does are separate). For a Phase 2 tool that only
makes one GET per call, that gap is acceptable; we're not building a
hostile-network-resistant fetcher.

## Consequences

**Easier**
- The agent can answer "what's 100 GBP in USD?" and "what's the
  weather in Lahore right now?" by chaining `web_search` →
  `fetch_url` and reading the actual page.
- One generic tool covers a broad family of "live data" cases without
  N domain-specific tools (weather API, FX API, stock API). We keep
  the option to add those later if `fetch_url` proves unreliable for
  a specific case.
- Reuses `primp` from the ADR 0005 stack — no new HTTP-client
  surface area.

**Harder**
- Approval friction on multi-step research turns: every fetch is
  one click. For a heavy research session this is annoying.
- Extraction quality varies by page. JS-heavy SPAs (some news sites,
  most modern dashboards) render server-side shells with no useful
  text; trafilatura returns empty and we surface "extracted no
  readable text". The model has to recover by trying another result.
- `lxml` is a heavier dep than the rest of the project. Acceptable
  on desktop / laptop targets; would matter for mobile (Phase 7).

## Alternatives reconsidered

- **Domain-specific tools (`currency_convert`, `weather`, etc.).**
  Cleaner output, more reliable, but tool-sprawl. Deferred — we add
  these only if `fetch_url` is consistently bad for a specific
  domain.
- **DDG instant-answer endpoint** (`api.duckduckgo.com/?q=...&format=json`).
  Cheap and key-free, but coverage is patchy and the answer is often
  empty. Could be added as a *third* tool later — would not displace
  `fetch_url`.
- **`requests-html` / Playwright for JS-rendered pages.** Heavyweight
  (Playwright bundles a browser binary). Out of scope for Phase 2.
  If JS-rendered pages turn out to matter we'll consider Playwright
  in a later ADR.

## Open questions / risks

- **Sites that block even `primp`.** Cloudflare's stricter rules and
  Akamai bot-mode can defeat fingerprinting. We'll hit this; the
  fallback is "the agent reports it couldn't read the page" — which
  is honest and recoverable.
- **Approval fatigue.** If users ask for many follow-ups in one turn
  the per-fetch click gets old. Track this; if it's a real problem,
  add session-scoped host allow-listing.
- **Per-host rate limits.** We don't have any. A pathological loop
  could hammer a site. Phase 2's `PA_AGENT_MAX_STEPS=8` and
  `PA_AGENT_MAX_RETRIES_PER_TOOL=2` bound the worst case to <20
  calls per turn, which is below any reasonable site's threshold.
  Revisit if that ceiling rises.

## What this doesn't change

- ADRs 0001-0005 are unaffected.
- The wire protocol from ADR 0002 / 0004 — `tool_call`,
  `tool_approval`, `tool_result`, `done` frames — is unchanged.
  `fetch_url` slots in via the existing `requires_approval=True`
  path that `write_file` already uses.
- The agent loop in `loop.py` does not change. New tool = one entry
  in `TOOLS`, one schema, one async function — exactly what
  ADR 0003 §3 was designed for.

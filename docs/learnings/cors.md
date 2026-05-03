# CORS

What we learned while wiring [backend/app/main.py](../../backend/app/main.py) to talk to [frontend/](../../frontend/) during [2026-05-02](../sessions/2026-05-02-phase-1-chat-mvp.md).

## The headline

CORS is a **browser** safety check, not a server one. Your server can serve any request from any client — `curl`, Postman, another server, our smoke scripts — without CORS ever entering the picture. The check fires only when JavaScript running in a browser page tries to call a different *origin* than the page itself was loaded from. The server's job is to opt in by sending the right response headers; the browser then decides whether to hand the response to the JS that asked for it.

## What "origin" means

`scheme://host:port`. Any of the three differing makes it a different origin:

- `http://localhost:5173` ≠ `http://localhost:8000` (port differs → different origin)
- `http://localhost:5173` ≠ `http://127.0.0.1:5173` (host string differs, even though they resolve to the same machine)
- `http://example.com` ≠ `https://example.com` (scheme differs)

Our Phase 1 setup: page loads from Vite at `:5173`, JS wants to POST to FastAPI at `:8000`. Two ports, two origins, browser blocks it by default. Either side has to do something:

- Server-side: send `Access-Control-Allow-Origin` headers (FastAPI's `CORSMiddleware` does this).
- Client-side workaround: route the request through a same-origin proxy so the browser never sees a cross-origin call.

We did both. They serve different roles — see below.

## The preflight bit (why CORS isn't just one header)

For "non-simple" requests — anything sending `Content-Type: application/json`, custom headers, methods other than GET/HEAD/POST-with-form-encoding — the browser sends an extra `OPTIONS` request *before* the real one, asking the server "are you OK with a POST from origin X carrying a JSON body?" The server has to answer with the right `Access-Control-Allow-*` headers, then the real request goes. Our `/chat` POST sends JSON, so every chat triggers a preflight in dev. `CORSMiddleware` handles it transparently.

## Why the Vite dev proxy makes our `CORSMiddleware` mostly redundant

[frontend/vite.config.ts](../../frontend/vite.config.ts) proxies `/chat` and `/health` to `127.0.0.1:8000`. From the browser's perspective, every fetch goes to `http://localhost:5173/...` — same origin as the page — so CORS never triggers. Vite (a Node process, not a browser) does the actual cross-origin call to the backend, and Node doesn't care about CORS.

So `CORSMiddleware` is currently a belt-and-suspenders: it catches the case where something *does* hit `:8000` directly from a browser (e.g. you point a second app at the API for testing). If we ever drop the dev proxy, we'd need it for real.

## WebSockets aren't quite CORS

WebSocket connections don't go through the CORS preflight machinery. The browser sends an `Origin` header on the upgrade request, and the server may inspect it and accept/reject the connection — but FastAPI/Starlette don't enforce origin checks on WS by default. Worth remembering when we expose this beyond localhost: a malicious page could open a WS to our `/chat/stream` from any origin unless we add an explicit origin check.

## What changes in production

When we eventually `npm run build` the frontend and serve `dist/` from FastAPI itself (single port, single origin), CORS goes away entirely. The middleware can be removed, the dev proxy can be removed, and the whole class of issue disappears. CORS is fundamentally a *development-time* problem caused by running the SPA dev server and the API on different ports.

### What "SPA" and "dev server" mean here

**SPA = Single Page Application.** Traditional websites: each URL is a separate HTML page that the server renders. Modern frontend: the server ships *one* HTML file plus a big JavaScript bundle. JS takes over the page, and "navigating" between routes (`/chat`, `/settings`, …) is just JS swapping out DOM nodes — the page never reloads, the URL changes via the History API. Our React app is an SPA: there's exactly one HTML file in [frontend/index.html](../../frontend/index.html).

**SPA dev server (Vite).** Building an SPA from `.tsx` source files is non-trivial — TypeScript needs compiling, JSX transforming, CSS preprocessing, modules bundling. Vite is a small HTTP server (defaults to `:5173`) purpose-built to do that **on the fly** while you iterate:

- compiles `.tsx`/`.ts` to JS as the browser requests each module — no upfront build step,
- **HMR (Hot Module Replacement)**: save `App.tsx` and Vite swaps just that module in the running browser without a full reload, preserving component state,
- runs the proxy config we use to forward `/chat` and `/health` to the FastAPI backend.

It's optimized for developer ergonomics, not for serving real users — slow per request, unminified, not hardened for production traffic. That's why production needs a different shape.

### `npm run build` → the `dist/` folder

When you're ready to ship, `npm run build` walks the source tree, compiles all the TSX/CSS, bundles it into a few minified files, hashes their filenames for cache-busting, and writes the result as **plain static files** into `frontend/dist/`:

```
frontend/dist/
├── index.html              # the SPA shell, references the JS/CSS by hashed name
├── assets/
│   ├── index-a1b2c3d4.js   # all React + app code, bundled and minified
│   └── index-e5f6g7h8.css  # all CSS, bundled
└── favicon.svg
```

No Node, no Vite, no TypeScript compiler needed at runtime. Any HTTP server that can serve files can serve this.

### "Serve `dist/` from FastAPI"

Instead of running a separate Vite process on `:5173`, you tell FastAPI: "for any URL that isn't an API route, serve the file from `frontend/dist/`." FastAPI has `StaticFiles` for this:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="spa")
```

Now `http://localhost:8000/` returns `dist/index.html`, `http://localhost:8000/assets/index-a1b2c3d4.js` returns the bundled JS, and `http://localhost:8000/chat` still hits the API route because FastAPI matches route handlers before the static mount. For SPA routing (URLs the SPA owns client-side, like `/settings`, that don't exist on disk), you'd add a catch-all that returns `index.html` so the SPA can take over.

The result: one process, one port, one origin. Browser loads from `localhost:8000`, fetches go to `localhost:8000`, no CORS preflight, no middleware needed. The Vite dev server and its `:5173` port exist only during development.

## Pitfalls worth knowing

- **`allow_origins=["*"]` + `allow_credentials=True` is forbidden by browsers.** Wildcards mean "any origin," which is incompatible with sending cookies. If we add session cookies later, we must list explicit origins.
- **`http://localhost` and `http://127.0.0.1` are *different origins.*** That's why our list has both. Easy to forget; the symptom is "it works for me but a teammate's browser blocks it."
- **CORS errors only show up in the browser's devtools console**, not in the server logs. The server returns a normal response; the browser silently throws it away. If a fetch is "failing for no reason" but `curl` works, suspect CORS first.

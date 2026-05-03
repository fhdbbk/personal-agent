"""Smoke test: confirm Ollama is reachable and the configured model can complete a prompt."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollama import Client

from backend.app.config import get_settings


def main() -> int:
    settings = get_settings()
    client = Client(host=settings.ollama_host)

    models = [m.model for m in client.list().models]
    print(f"Ollama at {settings.ollama_host} — {len(models)} model(s) available")
    for name in models:
        print(f"  - {name}")

    if settings.ollama_model not in models:
        print(
            f"\nConfigured model '{settings.ollama_model}' is not pulled.\n"
            f"Run: ollama pull {settings.ollama_model}",
            file=sys.stderr,
        )
        return 1

    prompt = "Reply with exactly the word: pong"
    print(f"\nPinging '{settings.ollama_model}' with: {prompt!r}")
    t0 = time.perf_counter()
    response = client.chat(
        model=settings.ollama_model,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.perf_counter() - t0
    text = response.message.content.strip()
    print(f"Reply ({elapsed:.2f}s): {text!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

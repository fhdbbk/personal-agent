import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.chat import router as chat_router
from backend.app.config import get_settings
from backend.app.logging_config import configure_logging

# Configure logging before the app starts so import-time messages are captured.
configure_logging()
log = logging.getLogger("pa.main")

app = FastAPI(title="Personal Assistant", version="0.1.0")
_settings = get_settings()
log.info(
    "starting Personal Assistant (model=%s device=%s think=%s)",
    _settings.ollama_model,
    _settings.ollama_device,
    _settings.ollama_think,
)

# Vite dev server runs on 5173 by default; allow it during local development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)


@app.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "ollama_host": settings.ollama_host,
        "ollama_model": settings.ollama_model,
    }

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

# Provider-specific knobs only make sense for the active backend; cloud
# providers don't use device/think, Ollama doesn't have an API key.
match _settings.llm_provider:
    case "ollama":
        _details = (
            f"model={_settings.ollama_model} "
            f"device={_settings.ollama_device} "
            f"think={_settings.ollama_think}"
        )
    case "anthropic":
        _details = f"model={_settings.anthropic_model}"
    case "openai":
        _details = f"model={_settings.openai_model}"

log.info(
    "starting Personal Assistant (provider=%s %s)",
    _settings.llm_provider,
    _details,
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
    body: dict[str, str] = {
        "status": "ok",
        "provider": settings.llm_provider,
    }
    match settings.llm_provider:
        case "ollama":
            body["model"] = settings.ollama_model
            body["ollama_host"] = settings.ollama_host
        case "anthropic":
            body["model"] = settings.anthropic_model
        case "openai":
            body["model"] = settings.openai_model
    return body

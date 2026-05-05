from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Device = Literal["auto", "cpu", "gpu"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PA_", env_file=".env", extra="ignore")

    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen3.5:4b")
    # Qwen3 (and friends) think before answering by default. Off makes
    # replies snappy; turn on for harder problems where reasoning helps.
    ollama_think: bool = Field(default=False)
    # auto = let Ollama decide; cpu = force num_gpu=0; gpu = force max offload.
    ollama_device: Device = Field(default="auto")
    request_timeout_s: float = Field(default=60.0)

    # Phase 2 agent loop. See docs/decisions/0003-agent-loop.md.
    agent_sandbox: str = Field(default="sandbox")
    agent_max_steps: int = Field(default=8)
    agent_max_retries_per_tool: int = Field(default=2)
    agent_auto_approve: bool = Field(default=False)

    log_dir: str = Field(default="logs")
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def device_options() -> dict | None:
    """Translate PA_OLLAMA_DEVICE into an Ollama `options` dict, or None for auto."""
    device = get_settings().ollama_device
    if device == "cpu":
        return {"num_gpu": 0}
    if device == "gpu":
        return {"num_gpu": 999}  # ollama clamps to the model's actual layer count
    return None

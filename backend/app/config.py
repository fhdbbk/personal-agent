from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Device = Literal["auto", "cpu", "gpu"]
LLMProviderName = Literal["ollama", "anthropic", "openai"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PA_", env_file=".env", extra="ignore")

    # LLM backend selection. Each provider reads its own subset below.
    llm_provider: LLMProviderName = Field(default="ollama")

    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen3.5:4b")
    # Qwen3 (and friends) think before answering by default. Off makes
    # replies snappy; turn on for harder problems where reasoning helps.
    ollama_think: bool = Field(default=False)
    # auto = let Ollama decide; cpu = force num_gpu=0; gpu = force max offload.
    ollama_device: Device = Field(default="auto")
    # Context window in tokens. Ollama defaults to 4096 — far smaller than
    # what modern small models can handle (qwen2.5/3 trained at 32k natively;
    # YaRN extends further with quality risk). 32k is a safe ceiling for our
    # 16 GB target; bump to 65536 in .env if you have the VRAM headroom.
    ollama_num_ctx: int = Field(default=32768)
    request_timeout_s: float = Field(default=60.0)

    # Anthropic — read only when llm_provider="anthropic".
    anthropic_api_key: str = Field(default="")
    anthropic_model: str = Field(default="claude-haiku-4-5")
    # Anthropic's Messages API requires max_tokens. Generous default; the
    # tool-using loop typically produces short replies between tool calls.
    anthropic_max_tokens: int = Field(default=4096)

    # OpenAI — read only when llm_provider="openai".
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-4o-mini")

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


def ollama_options() -> dict:
    """Build the Ollama `options` dict from settings.

    Always returns a non-empty dict because `num_ctx` is always set —
    callers can splat it unconditionally.
    """
    s = get_settings()
    opts: dict = {"num_ctx": s.ollama_num_ctx}
    if s.ollama_device == "cpu":
        opts["num_gpu"] = 0
    elif s.ollama_device == "gpu":
        opts["num_gpu"] = 999  # ollama clamps to the model's actual layer count
    return opts

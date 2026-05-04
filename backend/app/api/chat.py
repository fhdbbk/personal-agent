import logging
import time
from functools import lru_cache

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ollama import AsyncClient
from pydantic import BaseModel, Field

from backend.app.agent.loop import AgentError, run_turn
from backend.app.agent.prompt import system_prompt
from backend.app.config import get_settings
from backend.app.memory.buffer import Message, buffer

router = APIRouter()
log = logging.getLogger("pa.chat")


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str


class ResetRequest(BaseModel):
    conversation_id: str = Field(min_length=1)


class ResetResponse(BaseModel):
    conversation_id: str
    cleared: bool


@lru_cache
def _client() -> AsyncClient:
    # ollama forwards kwargs to httpx.AsyncClient. For streaming calls the
    # read-timeout becomes a per-chunk idle timeout, which is what we want:
    # a long generation is fine, a stalled connection aborts.
    s = get_settings()
    return AsyncClient(host=s.ollama_host, timeout=s.request_timeout_s)


def _build_messages(conversation_id: str, user_text: str) -> list[dict[str, str]]:
    history = buffer.history(conversation_id)
    msgs: list[dict[str, str]] = [{"role": "system", "content": system_prompt()}]
    msgs.extend({"role": m.role, "content": m.content} for m in history)
    msgs.append({"role": "user", "content": user_text})
    return msgs


def _persist_turn(conversation_id: str, user_text: str, reply: str) -> None:
    buffer.append(conversation_id, Message(role="user", content=user_text))
    buffer.append(conversation_id, Message(role="assistant", content=reply))


def _device_options() -> dict | None:
    """Translate PA_OLLAMA_DEVICE into an Ollama `options` dict.

    Returns None when no override is needed (auto), otherwise a dict that
    pins `num_gpu` to force CPU-only or full-GPU offload.
    """
    device = get_settings().ollama_device
    if device == "cpu":
        return {"num_gpu": 0}
    if device == "gpu":
        return {"num_gpu": 999}  # ollama clamps to the model's actual layer count
    return None


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    settings = get_settings()
    log.info("chat cid=%s msg_len=%d", req.conversation_id, len(req.message))
    t0 = time.perf_counter()
    msgs = _build_messages(req.conversation_id, req.message)
    options = _device_options()
    extra = {"options": options} if options else {}
    try:
        resp = await _client().chat(
            model=settings.ollama_model,
            messages=msgs,
            think=settings.ollama_think,
            **extra,
        )
    except Exception:
        log.exception("chat failed cid=%s", req.conversation_id)
        raise
    reply = resp["message"]["content"]
    _persist_turn(req.conversation_id, req.message, reply)
    log.info(
        "chat done cid=%s reply_len=%d latency_ms=%d",
        req.conversation_id,
        len(reply),
        int((time.perf_counter() - t0) * 1000),
    )
    return ChatResponse(conversation_id=req.conversation_id, reply=reply)


@router.post("/chat/reset", response_model=ResetResponse)
async def chat_reset(req: ResetRequest) -> ResetResponse:
    log.info("chat reset cid=%s", req.conversation_id)
    buffer.clear(req.conversation_id)
    return ResetResponse(conversation_id=req.conversation_id, cleared=True)


@router.websocket("/chat/stream")
async def chat_stream(ws: WebSocket) -> None:
    """Multi-turn agent loop. Each client message is one turn.

    Frame protocol (server -> client):
      {"type": "token",         "delta": "...",   "conversation_id": "..."}
      {"type": "tool_call",     "call_id": "...", "name": "...", "args": {...}, "conversation_id": "..."}
      {"type": "tool_result",   "call_id": "...", "ok": true,    "preview": "...", "conversation_id": "..."}
      {"type": "tool_approval", "call_id": "...", "name": "...", "args": {...}, "conversation_id": "..."}
      {"type": "done",          "conversation_id": "..."}
      {"type": "error",         "error": "...",   "conversation_id": "..."}

    Client -> server:
      {"conversation_id": "...", "message": "..."}                  (new turn)
      {"type": "approval_response", "call_id": "...", "approved": true}   (during a turn)

    During a turn, the only frame we expect from the client is
    `approval_response`. The UI disables the composer while a turn is in
    flight, so this is enforced from both sides.
    """
    await ws.accept()
    log.info("ws connected")
    try:
        while True:
            payload = await ws.receive_json()
            try:
                req = ChatRequest(**payload)
            except Exception as e:
                log.warning("ws bad request: %s", e)
                await ws.send_json({"type": "error", "error": f"bad request: {e}"})
                continue

            log.info(
                "ws turn cid=%s msg_len=%d", req.conversation_id, len(req.message)
            )
            t0 = time.perf_counter()

            async def on_event(frame: dict) -> None:
                await ws.send_json({**frame, "conversation_id": req.conversation_id})

            async def request_approval(call_id: str, name: str, args: dict) -> bool:
                await ws.send_json(
                    {
                        "type": "tool_approval",
                        "call_id": call_id,
                        "name": name,
                        "args": args,
                        "conversation_id": req.conversation_id,
                    }
                )
                # Block the loop on the matching approval_response. Anything
                # else from the client during a turn is a protocol violation.
                while True:
                    frame = await ws.receive_json()
                    if (
                        frame.get("type") == "approval_response"
                        and frame.get("call_id") == call_id
                    ):
                        return bool(frame.get("approved"))
                    log.warning(
                        "ws unexpected frame during approval: %s", frame.get("type")
                    )

            try:
                base_msgs = _build_messages(req.conversation_id, req.message)
                final = await run_turn(
                    conversation_id=req.conversation_id,
                    base_messages=base_msgs,
                    client=_client(),
                    on_event=on_event,
                    request_approval=request_approval,
                )
                _persist_turn(req.conversation_id, req.message, final)
                # The loop streamed token frames as they arrived; just close.
                await ws.send_json(
                    {"type": "done", "conversation_id": req.conversation_id}
                )
                log.info(
                    "ws turn done cid=%s reply_len=%d latency_ms=%d",
                    req.conversation_id,
                    len(final),
                    int((time.perf_counter() - t0) * 1000),
                )
            except AgentError as e:
                log.warning("agent error cid=%s: %s", req.conversation_id, e)
                await ws.send_json(
                    {
                        "type": "error",
                        "error": str(e),
                        "conversation_id": req.conversation_id,
                    }
                )
            except WebSocketDisconnect:
                raise
            except Exception as e:
                log.exception("ws turn failed cid=%s", req.conversation_id)
                await ws.send_json(
                    {
                        "type": "error",
                        "error": str(e),
                        "conversation_id": req.conversation_id,
                    }
                )
    except WebSocketDisconnect:
        log.info("ws disconnected")
        return

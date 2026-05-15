import { useEffect, useRef, useState } from 'react'
import './App.css'

type Role = 'user' | 'assistant'

interface TurnStats {
  eval_tokens: number
  prompt_tokens: number
  eval_seconds: number
  tokens_per_sec: number
  model_calls: number
  ttft_seconds: number | null
}

type TranscriptItem =
  | { kind: 'message'; id: string; role: Role; content: string; stats?: TurnStats }
  | {
      kind: 'tool'
      id: string
      call_id: string
      name: string
      args: Record<string, unknown>
      awaitingApproval: boolean
      result?: { ok: boolean; preview: string }
    }

interface TokenFrame {
  type: 'token'
  delta: string
  conversation_id: string
}
interface DoneFrame {
  type: 'done'
  conversation_id: string
  stats?: TurnStats
}
interface ErrorFrame {
  type: 'error'
  error: string
  conversation_id?: string
}
interface ToolCallFrame {
  type: 'tool_call'
  call_id: string
  name: string
  args: Record<string, unknown>
  conversation_id: string
}
interface ToolResultFrame {
  type: 'tool_result'
  call_id: string
  ok: boolean
  preview: string
  conversation_id: string
}
interface ToolApprovalFrame {
  type: 'tool_approval'
  call_id: string
  name: string
  args: Record<string, unknown>
  conversation_id: string
}

type ServerFrame =
  | TokenFrame
  | DoneFrame
  | ErrorFrame
  | ToolCallFrame
  | ToolResultFrame
  | ToolApprovalFrame

function newConversationId(): string {
  return (
    'c-' +
    (typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2))
  )
}

function newId(): string {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2)
}

function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${path}`
}

type ConnState = 'connecting' | 'open' | 'closed'

export default function App() {
  const [transcript, setTranscript] = useState<TranscriptItem[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conn, setConn] = useState<ConnState>('connecting')
  const [conversationId, setConversationId] = useState<string>(newConversationId)
  const wsRef = useRef<WebSocket | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const el = scrollerRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [transcript])

  useEffect(() => {
    const ws = new WebSocket(wsUrl('/chat/stream'))
    wsRef.current = ws
    setConn('connecting')

    ws.onopen = () => {
      if (wsRef.current === ws) setConn('open')
    }

    ws.onmessage = (ev) => {
      const frame = JSON.parse(ev.data) as ServerFrame
      handleFrame(frame)
    }

    ws.onerror = () => {
      if (wsRef.current === ws) setError('WebSocket error')
    }
    ws.onclose = () => {
      if (wsRef.current === ws) {
        wsRef.current = null
        setConn('closed')
      }
    }

    return () => ws.close()
  }, [])

  function handleFrame(frame: ServerFrame) {
    if (frame.type === 'token') {
      setTranscript((prev) => {
        const next = prev.slice()
        const last = next[next.length - 1]
        if (last && last.kind === 'message' && last.role === 'assistant') {
          next[next.length - 1] = { ...last, content: last.content + frame.delta }
        } else {
          next.push({
            kind: 'message',
            id: newId(),
            role: 'assistant',
            content: frame.delta,
          })
        }
        return next
      })
    } else if (frame.type === 'done') {
      if (frame.stats) {
        const stats = frame.stats
        setTranscript((prev) => {
          // Attach stats to the most recent assistant message in this turn.
          for (let i = prev.length - 1; i >= 0; i--) {
            const it = prev[i]
            if (it.kind === 'message' && it.role === 'assistant') {
              const next = prev.slice()
              next[i] = { ...it, stats }
              return next
            }
          }
          return prev
        })
      }
      setBusy(false)
    } else if (frame.type === 'error') {
      setError(frame.error)
      setBusy(false)
    } else if (frame.type === 'tool_call') {
      setTranscript((prev) =>
        upsertTool(prev, frame.call_id, {
          name: frame.name,
          args: frame.args,
        }),
      )
    } else if (frame.type === 'tool_approval') {
      setTranscript((prev) =>
        upsertTool(prev, frame.call_id, {
          name: frame.name,
          args: frame.args,
          awaitingApproval: true,
        }),
      )
    } else if (frame.type === 'tool_result') {
      setTranscript((prev) =>
        prev.map((it) =>
          it.kind === 'tool' && it.call_id === frame.call_id
            ? {
                ...it,
                awaitingApproval: false,
                result: { ok: frame.ok, preview: frame.preview },
              }
            : it,
        ),
      )
    }
  }

  function upsertTool(
    prev: TranscriptItem[],
    call_id: string,
    patch: { name: string; args: Record<string, unknown>; awaitingApproval?: boolean },
  ): TranscriptItem[] {
    const idx = prev.findIndex((it) => it.kind === 'tool' && it.call_id === call_id)
    if (idx >= 0) {
      const existing = prev[idx]
      if (existing.kind !== 'tool') return prev
      const next = prev.slice()
      next[idx] = {
        ...existing,
        name: patch.name,
        args: patch.args,
        awaitingApproval: patch.awaitingApproval ?? existing.awaitingApproval,
      }
      return next
    }
    return [
      ...prev,
      {
        kind: 'tool',
        id: newId(),
        call_id,
        name: patch.name,
        args: patch.args,
        awaitingApproval: patch.awaitingApproval ?? false,
      },
    ]
  }

  function send() {
    const text = input.trim()
    if (!text || busy) return
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setError('Not connected')
      return
    }
    setError(null)
    setTranscript((prev) => [
      ...prev,
      { kind: 'message', id: newId(), role: 'user', content: text },
    ])
    setInput('')
    setBusy(true)
    ws.send(
      JSON.stringify({
        conversation_id: conversationId,
        message: text,
      }),
    )
  }

  function respondToApproval(call_id: string, approved: boolean) {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    // Optimistically clear the awaitingApproval flag — the server will
    // either send a tool_result (denied) or run the tool and then send one.
    setTranscript((prev) =>
      prev.map((it) =>
        it.kind === 'tool' && it.call_id === call_id
          ? { ...it, awaitingApproval: false }
          : it,
      ),
    )
    ws.send(JSON.stringify({ type: 'approval_response', call_id, approved }))
  }

  async function newChat() {
    if (busy) return
    const oldCid = conversationId
    setTranscript([])
    setError(null)
    setConversationId(newConversationId())
    try {
      await fetch('/chat/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: oldCid }),
      })
    } catch {
      // best-effort eviction; the rotated cid is enough
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Personal Assistant</h1>
        <span className="meta">
          <span className={`dot ${conn}`} title={`socket: ${conn}`} />
          <span className="cid" title="conversation id">
            {conversationId.slice(0, 10)}…
          </span>
          <button
            className="newchat"
            onClick={newChat}
            disabled={busy}
            title="Start a new conversation"
          >
            New
          </button>
        </span>
      </header>

      <div ref={scrollerRef} className="messages">
        {transcript.length === 0 && (
          <div className="empty">Say something to start.</div>
        )}
        {transcript.map((it) =>
          it.kind === 'message' ? (
            <div key={it.id} className={`msg ${it.role}`}>
              <div className="role">{it.role}</div>
              <div className="content">{it.content || (busy ? '…' : '')}</div>
              {it.stats && <StatsLine stats={it.stats} />}
            </div>
          ) : (
            <ToolCard
              key={it.id}
              item={it}
              onApprove={() => respondToApproval(it.call_id, true)}
              onDeny={() => respondToApproval(it.call_id, false)}
            />
          ),
        )}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="composer">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Message (Enter to send, Shift+Enter for newline)"
          rows={2}
          disabled={busy}
        />
        <button onClick={send} disabled={busy || !input.trim() || conn !== 'open'}>
          {busy ? '…' : 'Send'}
        </button>
      </div>
    </div>
  )
}

function StatsLine({ stats }: { stats: TurnStats }) {
  const parts = [
    `${stats.tokens_per_sec.toFixed(1)} tok/s`,
    `${stats.eval_tokens} tokens`,
    `${stats.eval_seconds.toFixed(2)}s`,
  ]
  if (stats.ttft_seconds != null) parts.push(`ttft ${stats.ttft_seconds.toFixed(2)}s`)
  if (stats.model_calls > 1) parts.push(`${stats.model_calls} calls`)
  if (stats.prompt_tokens) parts.push(`prompt ${stats.prompt_tokens}`)
  return <div className="stats">{parts.join(' · ')}</div>
}

function ToolCard({
  item,
  onApprove,
  onDeny,
}: {
  item: Extract<TranscriptItem, { kind: 'tool' }>
  onApprove: () => void
  onDeny: () => void
}) {
  const status = item.result
    ? item.result.ok
      ? 'ok'
      : 'error'
    : item.awaitingApproval
      ? 'await'
      : 'running'
  return (
    <div className={`tool tool-${status}`}>
      <div className="tool-head">
        <span className="tool-name">{item.name}</span>
        <span className="tool-status">
          {status === 'await' && 'awaiting approval'}
          {status === 'running' && 'running…'}
          {status === 'ok' && 'ok'}
          {status === 'error' && 'error'}
        </span>
      </div>
      <pre className="tool-args">{JSON.stringify(item.args, null, 2)}</pre>
      {item.result && (
        <pre className="tool-result">{item.result.preview}</pre>
      )}
      {item.awaitingApproval && (
        <div className="tool-approval">
          <button className="approve" onClick={onApprove}>
            Approve
          </button>
          <button className="deny" onClick={onDeny}>
            Deny
          </button>
        </div>
      )}
    </div>
  )
}

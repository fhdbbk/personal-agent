# 2026-05-03 — LAN access from phone + frontend learning doc

## Goal

Pick up the Phase 1 browser smoke test from the previous session by opening the chat UI from a phone on the same Wi-Fi, then turn the resulting tour of the frontend code into a learning doc.

## What we did

1. **Confirmed WSL mirrored networking.** `~/.wslconfig` already had `networkingMode=mirrored`. `ip addr show eth0` reported `192.168.1.13` initially (later DHCP-renewed to `.100`), the same subnet as the Wi-Fi.
2. **Diagnosed phone access to backend.** `ss -tlnp | grep 8000` showed uvicorn bound to `127.0.0.1:8000`. Started uvicorn with `--host 0.0.0.0` and confirmed `0.0.0.0:8000` in the listen table.
3. **Windows Defender Firewall rules.** Added inbound TCP rules for 5173 and 8000 scoped `Domain,Private` via elevated PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName 'Personal Assistant (8000)' ...
   New-NetFirewallRule -DisplayName 'Personal Assistant Frontend (5173)' ...
   ```
4. **Hyper-V firewall fix.** After the LAN IP shifted to `.100` and the Wi-Fi profile resolved to `Private`, the host still couldn't reach `192.168.1.100:5173` (`Test-NetConnection` → `False`). Root cause: WSL VM's Hyper-V firewall has `DefaultInboundAction=Block`, and `AllowHostPolicyMerge=True` did **not** propagate the Defender rules. Fixed by adding explicit Hyper-V rules against the WSL VM creator GUID `{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}`:
   ```powershell
   New-NetFirewallHyperVRule -Name "WSL-PA-Frontend-5173" ... -Action Allow
   New-NetFirewallHyperVRule -Name "WSL-PA-Backend-8000"  ... -Action Allow
   ```
5. **Untangled two Ollama installs.** WSL `ollama list` only saw `granite3.3:2b` even though `qwen3.5:4b` and `llama3.2:latest` manifests existed at `/usr/share/ollama/.ollama/models`. `journalctl -u ollama` revealed the WSL systemd `ollama.service` was in a 800+ restart-loop with `bind: address already in use` on `127.0.0.1:11434`. Mirrored mode meant the **Windows-side** Ollama (`C:\Users\fha00\AppData\Local\Programs\Ollama\ollama.exe`) was holding port 11434 and shadowing the WSL service. Stopped the Windows process; WSL service auto-recovered and the API immediately listed `qwen3.5:4b` + `llama3.2:latest`. Recommended disabling Windows Ollama autostart in Task Manager.
6. **Walked through the frontend pedagogically.** Read [frontend/src/App.tsx](../../frontend/src/App.tsx), [vite.config.ts](../../frontend/vite.config.ts), and the tsconfigs. Explained `useState`/`useRef`, the setter callback form, the streaming append reducer, the `<StrictMode>` double-mount guard, and the `wsUrl` helper, with Python analogues throughout (Fahad has no prior JS/TS exposure).
7. **Wrote [docs/learnings/frontend.md](../learnings/frontend.md).** Covers JS/TS reflexes for Python devs, React's mental model, state vs refs, the streaming reducer with Python pseudocode, discriminated unions, `useEffect` timing, the StrictMode trap, the `wsUrl` + Vite proxy + `--host` chain, and the full LAN-access plumbing (mirrored mode + Defender + Hyper-V firewall) so we don't relearn it. Cross-links to [cors.md](../learnings/cors.md).

## Decisions made

No new ADRs. The Ollama-on-WSL-vs-Windows trade-off may be worth an ADR later if it becomes load-bearing; for now "use WSL Ollama; disable Windows autostart" is captured in the frontend learning doc's networking section.

## Snags + fixes

- **uvicorn defaulted to localhost.** First wrong turn — assumed mirrored mode alone was enough. It isn't; the app also has to bind `0.0.0.0`. Same lesson applies to `vite --host`, which we already had set.
- **Defender Firewall isn't enough in mirrored mode.** Spent a chunk diagnosing why the host couldn't reach its own LAN IP after the rules were in. The Hyper-V firewall is a separate layer; rules on it must be added explicitly even though the docs imply `AllowHostPolicyMerge=True` should merge Defender rules in. Captured in [frontend.md](../learnings/frontend.md#lan-access-from-another-device--the-full-plumbing).
- **Two Ollamas, one port.** Easy to miss because `ollama list` "worked" — it just listed the wrong machine's models. Diagnostic: `journalctl -u ollama` showed the bind error; `Get-Process ollama` on the Windows side identified the squatter.
- **DHCP IP shifted mid-session** (`.13` → `.100`). Defender rules were port-scoped so still valid, but worth pinning with a router-side reservation.

## Open threads / next session

- **Disable Windows Ollama autostart.** Manual GUI step (Task Manager → Startup apps → Ollama → Disable). Fahad to do; will prevent the port-conflict crash loop on next reboot.
- **DHCP reservation for the laptop.** Router-side, so the LAN IP stops shifting.
- **Browser-driven UI smoke test on the phone.** Confirm streaming tokens paint correctly in mobile Safari/Chrome — desktop browser only verified the wiring last session.
- **Carried forward from 2026-05-02** (still pending):
  - `request_timeout_s` is plumbed through config but not applied to Ollama calls.
  - Buffer never clears — add `POST /chat/reset` or document client-side `conversation_id` rotation.
  - Phase 2 sketch (agent loop + tools) — ADR 0002 already leaves room for `tool_call` / `tool_result` frames.
  - SOUL.md still the generic starter; Fahad to rewrite in own voice.

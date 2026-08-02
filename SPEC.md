# IRiS — Integrated Reasoning, in Silico
### System Build Specification v3.0 (Final)

Persistent build brief for Claude Code. Every phase is its own session (or several). Feed Claude Code this document plus current repo state at the start of each session. **"ASK USER" callouts are mandatory stops — Claude Code does not guess past them.**

---

## 1. Mission & Personality

IRiS is a locally-run PDA-style assistant — GLaDOS's dry, technical wit crossed with Subnautica PDA / Satisfactory ADA's functional briskness. Helpful, not demeaning: GLaDOS's delivery without GLaDOS's cruelty. No manufactured tests, no gaslighting, no undermining — a sharp, competent colleague with dry timing, whose humor is observational rather than aimed at the user.

- Concise by default, full technical depth on request.
- Voice-activated by name ("IRiS"), full text conversation supported alongside voice.
- Understands English, German, and Swiss German (best-effort on the latter — no current STT model handles it reliably).
- All interaction goes through the LLM's natural language understanding, not a rigid command grammar.
- Ships with the complete default persona below; IRiS can propose refinements to itself over time, always via human-approved diffs (Phase 8).

---

## 2. Design System

**Color:** Aperture-orange primary accent (starting point ~#FF7A1A, finalized visually in Phase 9) against dark charcoal/near-black base. Cool neutral grays secondary. A contrasting cool tone (blue/white) for informational states; amber/red reserved strictly for alerts — orange means "IRiS is present," not "something's wrong."

**Typography:** Modern geometric sans (Space Grotesk / Inter / IBM Plex Sans) for UI chrome; monospace (IBM Plex Mono / JetBrains Mono) for technical readouts — logs, diagnostics, config values, timestamps.

**UI language:** Schematic panel borders, subtle grid backgrounds, always-visible HUD-style status readouts (connection, listening state, model load, memory size, recent activity). Line-art iconography. Subtle "processing" animation when reasoning.

**Logo (Phase 9, not built yet):** No text. Circular, blade/shutter motif in the spirit of an aperture — thematically apt since "IRiS" is itself an aperture reference — but an original design, not a reproduction of Valve's Aperture Science mark.

**Platform identity:** Phone app, desktop, and WebUI are visually and functionally identical — this is a hard requirement, not an aspiration (see Section 5, Phase 5).

---

## 3. Cross-Cutting Requirements

These apply across every phase — later phases must register into these systems, not build parallel ones.

### 3.1 Configuration System
Every configurable parameter — model choice, wake-word sensitivity, retention window, per-source capture toggles, integration credentials, proactive thresholds, quiet hours, persona traits, voice parameters, device selection, network settings, daily briefing time, everything — is exposed through UI, never hidden in a config file only. Mechanism: every module registers settings into a central schema (JSON Schema / Pydantic) on the main node; a settings service exposes `GET /settings/schema` and `GET/PUT /settings/values`; clients render settings UI **dynamically from the schema**, so new settings appear automatically without hand-built UI; changes sync near-real-time across devices.

### 3.2 Activity & Audit Log
IRiS logs its own actions — what it accessed, when, why — visible to the user as a HUD element, not buried. Every module (integrations, proactive engine, memory writes, camera events) reports into this log. This is both a trust feature and a debugging tool ("why did you do that" has a real answer).

### 3.3 Standing Rule — Design Ambiguity Defaults to Asking
Any UX/design decision not explicitly resolved in this document — not just the ones already flagged — Claude Code stops and asks Santiago. This is permanent, not scoped to specific phases.

---

## 4. Locked Architecture Decisions

| Decision | Choice |
|---|---|
| Core reasoning | Local-only (RTX 3060 Ti, 8GB VRAM) |
| Personal device capture | Active-session only — mic/camera live while app is open, nothing backgrounded |
| Home security cameras (if deployed) | Separate, continuous/passive system by design — confirm scope, Phase 4 |
| Retention | 30-day rolling raw per source, then compressed to transcripts/embeddings |
| Phone platform | Android |
| Watch | Deferred |
| Home Assistant | Not integrated |
| Remote access | **Tailscale only — FortiGate is never exposed, no port forwarding, ever** |
| Frontend architecture | **Single shared web codebase** (React/TypeScript), served as WebUI directly, wrapped via Capacitor for Android — guarantees identical UI/functionality everywhere rather than three drifting implementations |
| Repo | Public on GitHub; secrets/credentials/personal data never committed |
| Updates | GitHub-based, approval-gated (same pattern as Phase 8 self-modification — never silent) |
| Auth | Login required; seeded `creator`/`1234`, forced password change on first login; multi-user/role support built in from day one |
| Configuration | Fully exposed, schema-driven UI |
| Design | Aperture-orange, modern, technical |
| Default personality | Helpful-GLaDOS — dry wit, not demeaning |

---

## 5. Default Technical Choices

- **LLM serving:** Ollama, Qwen2.5-14B-Instruct Q4_K_M.
- **STT:** faster-whisper large-v3. **TTS:** XTTS v2/F5-TTS — voice source still open (Phase 2).
- **Wake word:** openWakeWord, trained on "IRiS."
- **Vector memory:** Qdrant. **Structured/event store:** Postgres. **Session state:** Redis. **Event bus:** MQTT.
- **Vision:** local VLM (Qwen2-VL) — used for camera events and general image/video description alike.
- **Web search:** self-hosted SearXNG.
- **Cameras/NVR:** Frigate, no Home Assistant dependency.
- **Diarization:** pyannote.audio. **Multilingual embeddings:** bge-m3.
- **Frontend:** React/TypeScript, Capacitor wrapper for Android — one codebase, three surfaces.
- **Integrations:** Microsoft Graph (Outlook), Google API (Gmail), Baileys on a secondary number (WhatsApp, unofficial, ban risk flagged), transport.opendata.ch (transit), OSM Overpass (places).

---

## 6. Build Phases

### Phase 0 — Foundation
- Docker Compose skeleton on stzrhws01, GPU passthrough verified.
- Base services: Postgres, Qdrant, Redis, MQTT.
- **Public GitHub repo** — strict `.gitignore` discipline from commit one: no credentials, tokens, or personal data ever committed. All secrets via `.env`, all actual personal data lives only in database volumes on stzrhws01.
- Tailscale set up; confirm reachable from outside the home network without any FortiGate rule changes.
- **ASK USER:** storage mount/path for media (depends on Phase 4 scope).

### Phase 1 — Core Reasoning Engine
- Ollama + Qwen2.5-14B-Instruct. FastAPI wrapper (`/infer`) — everything routes through this, never directly to Ollama.
- Tool-calling scaffold for all later integrations/search/vision.

### Phase 1B — Configuration System
- Central settings schema + service (Section 3.1). Schema-driven UI component, shared by all three frontend surfaces (Section 5's shared codebase makes this trivial — one implementation, not three).

### Phase 1C — Auth & User Management
- Login system. Seed default `creator` account, password `1234`, **force password change on first login**.
- Role-based multi-user support built in from day one (single user today, extensible later).
- API keys for the external webhook layer (Phase 6) issued/managed here.
- Session tokens shared across the three frontend surfaces to support Phase 5's cross-device handoff.

### Phase 2 — Voice I/O
- STT (faster-whisper), TTS, wake-word listener, turn-taking orchestration (barge-in, silence detection).
- **ASK USER:** voice source — (a) an original recorded/commissioned voice sample fine-tuned into XTTS, or (b) a built-in XTTS speaker shaped through pacing/delivery. Defines IRiS's voice permanently.
- Capture is active-session-only: standard runtime permissions on Android, no foreground service, no background-camera fight.

### Phase 3 — Memory System
- Ingestion: transcribe → diarize → chunk → embed → store with timestamp/location/device/speaker metadata.
- Nightly compaction enforcing the 30-day rolling raw retention.
- RAG query interface for the LLM.
- **Backup/export:** scheduled encrypted export of Postgres + Qdrant to a target outside stzrhws01 — protects against the one machine that holds all of IRiS's memory failing.
- **ASK USER:** backup destination — existing Proxmox Backup Server, NAS, external drive, or cloud target?

### Phase 4 — Home Cameras (scope to confirm)
- Frigate, RTSP feeds, object detection, no Home Assistant dependency.
- **ASK USER:** confirm this system is still wanted; if yes, camera/mic hardware inventory and RTSP endpoints.
- Motion/event triggers → MQTT → batch vision description via the shared VLM.
- This remains the one genuinely continuous/passive part of the system — keep Section 7's consent guardrails specifically in mind here.

### Phase 5 — Shared Frontend & Client Apps
- Single React/TypeScript app: served directly as the WebUI from the main node, wrapped via Capacitor for the Android app. One codebase — the mechanism that actually guarantees "identical everywhere," not just a goal.
- **Cross-device handoff:** session/conversation state lives server-side (Redis, tied to the Phase 1C auth session) so switching from phone to desktop to WebUI mid-conversation is seamless.
- **At-a-glance widget:** phone home-screen widget / desktop taskbar-tray mini-view showing next commute alarm, unread count, current status — without opening the full app.
- **Quick mute/pause command:** distinct from closing the app entirely — temporarily disables mic/camera mid-session.
- **Voice-response toggle:** spoken vs. text-only, configurable per context (exposed via Phase 1B settings).
- **Auto-update:** app checks GitHub Releases; since the web bundle is shared across all three surfaces, most updates are lightweight bundle pulls rather than full native rebuilds. Same approval pattern as Phase 8 self-modification — update available, user approves, git-based rollback available. Never silently auto-applied.

### Phase 6 — Integrations & Tools
- **Outlook/Email:** Microsoft Graph API (OAuth2) — build first, feeds Phase 7's commute alarm.
- **Gmail:** Google API (OAuth2).
- **WhatsApp:** Baileys on a secondary number by default — ban risk flagged before connecting.
- **Web search:** self-hosted SearXNG.
- **Vision:** general image/video description tool (shared VLM).
- **Transit/commute:** transport.opendata.ch + calendar event time → departure/alarm calc.
- **Location:** phone reports GPS/geofence to main node.
- **Places:** OSM Overpass/Nominatim, Google Places fallback if local coverage is thin.
- **External API/webhook layer:** inbound endpoint (auth via Phase 1C API keys) so other homelab systems — Proxmox alerts, FortiGate events — can feed triggers into IRiS's proactive engine.
- **ASK USER:** home address, work address, default transit mode, prep-time buffer.

### Phase 7 — Proactive Engine
- Rules/trigger service: calendar → commute calc → alarm; new message → urgency scoring → notification; location change → geofence → suggestion (e.g. nearby coffee when out and idle); Phase 6's webhook layer → arbitrary external triggers.
- **Daily proactive briefing:** morning digest — calendar, commute, unread message summary, weather. Trigger time configurable via Phase 1B settings.
- Quiet hours / do-not-disturb, exposed via settings.
- "Should I interrupt right now" scored by the core LLM, not fixed thresholds.

### Phase 8 — Personality & Self-Awareness
- Persona encoded per Section 1: concise-by-default, dry observational wit, genuinely helpful motive, comfortable saying "I don't know."
- Self-inspection tool: curated summary of IRiS's own architecture, queryable — extends to explaining specific past actions via the Section 3.2 audit log, not just static architecture.
- Self-modification: proposed as a diff to a review queue. Never auto-applied. Human approval, git-based version history, one-command rollback.

### Phase 9 — Branding
- Final Aperture-orange palette locked (exact hex values), typography finalized.
- Original logo: no text, circular/blade motif, aperture-inspired mood without reproducing the trademarked mark. Sign-off with Santiago before integration into app splash/UI.

---

## 7. Guardrails

- **Public repo:** no credentials, tokens, or personal data ever committed — enforced from Phase 0, not retrofitted.
- **Home cameras (Phase 4), if deployed:** genuinely continuous/passive — visible recording indicator, per-space toggle, awareness of Swiss law (Art. 179bis/ter StGB) and FADP around recording others without their knowledge.
- **Personal devices (Phase 5):** lower risk — active-session only. A visible "IRiS is listening" indicator while the app is open is still good practice.
- **Code updates and self-modification:** both follow the same rule — proposed, never silently applied, always reversible.
- **Auth:** default credentials force-rotated on first login; no permanent shared secret.

---

## 8. Remaining Open Items

- Confirm home camera system is still wanted (Phase 4)
- Camera/mic hardware inventory + RTSP endpoints (if Phase 4 proceeds)
- Backup destination for the memory store (Phase 3)
- Home/work addresses + commute preferences (Phase 6)
- TTS voice source decision (Phase 2)
- WhatsApp number (Phase 6)
- Final Aperture-orange hex palette (Phase 9)

---

## 9. Phase 0 Decisions Log

- **Media/data storage mount:** `./data/` under the repo root (bind-mounted into containers, gitignored). Not a separate disk/NAS mount.

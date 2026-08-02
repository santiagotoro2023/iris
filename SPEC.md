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

### 3.4 Standing Rule — One-Command Install/Uninstall, Configure in UI
Stated by Santiago, verbatim:

> I also need you to create a setup.sh file that sets everything up on its own. There should also be a --uninstall option for the setup.sh to cleanly shut down and remove everything cleanly. This should be documented and kept up to date with the changes you make so there is never stale install or uninstall components. If anything needs to be configured it is done in the webUI of the server and / or the app, as little cli interaction for the user as possible, just the install and then move to UI.

This is a **standing maintenance obligation**, not a one-time task: every phase that adds a service, volume, model, system package, or config file must update `setup.sh` (both install and uninstall paths) and its documentation in the same change. Stale install/uninstall components are a defect.

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

- **LLM serving:** Ollama, ~~Qwen2.5-14B-Instruct Q4_K_M~~ → **`qwen3:8b`, thinking off by default** (superseded 2026-08-02 with Santiago's approval; original text kept for the record). No 14B model fits this GPU's 7.1 GiB; qwen3:8b is the only candidate that runs 100% on GPU *and* answers in under a second. Measurements and reasoning in §10.
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

- **GPU passthrough — use the explicit CDI device, not `--gpus all`.** Host runs Docker 29.6.2, which resolves GPUs only through CDI. `--gpus all` makes Docker auto-detect a vendor and it guesses wrong here (`AMD CDI spec not found`) because the NVIDIA driver also creates `/dev/dri` nodes. Working form:

  ```
  docker run --rm --device nvidia.com/gpu=all <image>          # CLI
  devices: ["nvidia.com/gpu=all"]                              # compose service
  ```

  Setup on stzrhws01: `nvidia-container-toolkit` 1.19.1-1 from NVIDIA's apt repo (not in Debian trixie), CDI spec at `/etc/cdi/nvidia.yaml` generated via `nvidia-ctk cdi generate`. Driver 550.163.01.

  **Regenerate `/etc/cdi/nvidia.yaml` after every driver update** — the spec pins driver-versioned library paths (`libcuda.so.550.163.01` etc.), so a driver bump silently breaks passthrough until it's regenerated.

- **Verified 2026-08-02:** GPU enumerates inside a container (RTX 3060 Ti); all four base services up and functionally responding (Postgres accepting connections, Redis PONG, Qdrant healthz, MQTT pub/sub round-trip).

- **MQTT config lives in the repo, not `./data`.** `data/` is gitignored, so a config file kept there would be missing from a fresh clone and Mosquitto 2.x would fall back to defaults (anonymous access denied). Tracked at `mosquitto/mosquitto.conf`, bind-mounted read-only. General rule: config in the repo, runtime state in `./data`.

---

## 10. Phase 1 Decisions Log

- **`/infer` contract.** `POST /infer {messages: [...], model?: str}` → `{message, messages}`. Runs the tool-calling loop server-side and returns the full transcript including tool turns. Nothing else may talk to Ollama directly (Phase 1 requirement).

- **Tool scaffold.** `api/main.py` holds a `TOOLS` registry and a `@tool(name, description, parameters)` decorator; later phases register integrations/search/vision by importing and decorating. Loop is capped at `MAX_TOOL_HOPS = 5` (returns HTTP 508). A failing or unknown tool reports the error back to the model as a tool message rather than failing the request, so the model can recover or explain. One seeded tool, `current_time` — an LLM cannot know the clock and a PDA must.

- **⚠ OPEN — the model does not fit in VRAM.** Measured on stzrhws01, 2026-08-02:

  | | |
  |---|---|
  | Model on disk | 9.0 GB (Qwen2.5-14B-Instruct Q4_K_M) |
  | VRAM total / available | 7.8 GiB / 7.1 GiB |
  | Layers on GPU | 32 of 49 |
  | Split | 36% CPU / 64% GPU |
  | Generation | **12.1 tok/s** |
  | Prompt processing | 531 tok/s |

  It works and answers correctly, but 17 layers run on CPU. At 12 tok/s a 150-token
  reply takes ~12 s, which is likely too slow for the Phase 2 voice conversation
  loop (barge-in, turn-taking) even though it is fine for text.

  Section 5 locks this model, so changing it is Santiago's call, not Claude Code's.
  **ASK USER** before altering the model choice.

- **Model benchmark, 2026-08-02.** Three candidates, identical prompts, `temperature 0`:

  | | 14B Q4_K_M | 14B Q3_K_M | 7B Q4_K_M |
  |---|---|---|---|
  | Size / fits 7.1 GiB VRAM | 9.0 GB / no | 7.3 GB / no | 4.7 GB / **yes** |
  | CPU/GPU split | 36/64 | 23/77 | 0/100 |
  | Generation | 12.5 tok/s | 18.9 tok/s | **78.3 tok/s** |
  | Reasoning (4 problems) | 4/4 | 4/4 | 4/4 |
  | Tool decisions (4 cases) | 4/4 | 4/4 | 4/4 |
  | Swiss German (read, not scored) | **best** | lost "hüt", mistranslated "Grüezi" | one output was gibberish |
  | Language drift on tool calls | rare (1 of 8) | 2 of 4 | none |

  Notes that matter more than the table:

  - **Reasoning is a wash.** An earlier single-shot run appeared to show 14B Q4 failing
    an age puzzle; with an explicit answer format all three score 4/4. That first result
    was a parsing artifact, not a capability difference.
  - **Keyword scoring overstated Swiss German.** All three scored 3/3 automatically, but
    reading the outputs, 7B produced `"ich gehe gehen, brutzelst du noch öffnungen im
    laden?"` — gibberish. Judge these by reading, not by substring match.
  - **Language drift is mitigable.** Both 14B variants sometimes emit Thai in the
    `content` field alongside an otherwise-correct tool call. A system prompt pinning
    the reply language takes Q3 from 2/4 to 0/4. Phase 8 adds a persona system prompt
    anyway, so this costs nothing.
  - **Q3_K_M is dominated** — slower than 7B, worse Swiss German than Q4, driftiest of
    the three. Not a serious candidate.

  Real trade-off is 7B (6.3× faster, fits VRAM, weaker Swiss German) vs 14B Q4
  (best language quality, 12.5 tok/s, partially on CPU).

- **Qwen3 benchmark, 2026-08-02.** Section 5 predates the discovery that no 14B model
  fits this GPU, so Qwen3 was measured on the same prompts:

  | | qwen3:8b (think off) | qwen3:8b (think on) | qwen3:14b | qwen2.5:14b Q4 | qwen2.5:7b |
  |---|---|---|---|---|---|
  | VRAM residency | **100% GPU, 5.6 GB** | 100% GPU | 63% GPU | 64% GPU | 100% GPU, 4.7 GB |
  | Generation | 73.8 tok/s | 67.0 tok/s | 10.7 tok/s | 12.3 tok/s | 78.3 tok/s |
  | **Median latency** | **0.6 s** | 11.2 s | 4.7 s | 4.1 s | 1.0 s |
  | Reasoning | 3/4 | **4/4** | 4/4 | 4/4 | 4/4 |
  | Tool decisions | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 |
  | Language drift | none | none | none | 1 of 4 | none |
  | Swiss German | mediocre | mediocre | **best** | good | worst |

  - **`qwen3:14b` has the best Swiss German of anything tested** — it was the only model
    to render `'bini z spaat cho'` with the correct person ("bin ich zu spät gekommen").
    But it runs 37% on CPU at 10.7 tok/s, slower than the Qwen2.5 14B it would replace.
  - **`qwen3:8b`'s thinking mode is a per-request flag**, not a model choice. Off: 0.6 s
    median latency, 3/4 reasoning. On: 4/4 reasoning, 11.2 s median latency. One model,
    one VRAM slot, latency spent only where it buys something.
  - **Headroom matters for Phase 2.** qwen3:8b leaves ~2.8 GiB free. faster-whisper and
    TTS can co-reside rather than forcing a model swap per voice turn.
  - Swiss German being weaker on 8B is a smaller loss than it looks: Section 1 already
    calls Swiss German best-effort *because STT cannot handle it reliably*. The LLM is
    not the bottleneck on that path.

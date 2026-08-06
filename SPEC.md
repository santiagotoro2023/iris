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
- **STT:** faster-whisper large-v3. **TTS:** XTTS v2/F5-TTS — **voice source resolved 2026-08-02: option (b), a built-in XTTS speaker shaped through pacing and delivery** (Santiago's choice; the Phase 2 ASK USER is closed).
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

---

## 11. Phase 1B Decisions Log

- **Registration API.** `settings.setting(key, type=..., default=..., title=..., **json_schema)`.
  Dotted keys (`llm.model`, `voice.wake_sensitivity`) group in the UI by prefix. A `default`
  and a `title` are mandatory — clients render entirely from the schema, so a setting without
  a title would appear as a bare key. Later phases register at import time; no UI work needed.

- **Only overrides are stored.** The `settings` table holds a row solely for values the user
  has actually changed. Changing a default in code therefore takes effect for everyone who
  never touched that setting, and rows for settings no longer registered are ignored at load
  rather than resurrecting removed config.

- **Validation happens on the merged result, not the patch.** `PUT /settings/values` accepts a
  partial update but validates `current | patch` against the whole schema, so no sequence of
  valid-looking patches can leave the configuration in a state the schema forbids. Rejections
  return the offending key and reason.

- **Live sync is SSE, not MQTT.** MQTT is the event bus for backend modules, but browsers
  cannot speak raw MQTT without a websocket listener and extra broker config. `/settings/stream`
  is server-sent events — no dependency, no broker change, works through the same origin the
  UI is already served from. Backend modules that later need change notifications should get
  an MQTT publish added alongside; nothing needs it yet.

- **Postgres, not a JSON file.** Multi-device editing is an explicit requirement (§3.1), and
  concurrent writes to a JSON file lose updates. `jsonschema` and `psycopg[binary]` were added
  to the API image; hand-rolling validation for an endpoint that Phase 6 will put integration
  credentials behind is not worth the saved dependency.

- **`api/static/index.html` is deliberately disposable.** Vanilla JS, no build step, ~250 lines,
  styled per §2. Phase 5's React app replaces it. It exists because §3.4 requires configuration
  to happen in a UI, and until Phase 5 there is no other UI.

- **Env vars seed defaults only.** `IRIS_MODEL` / `IRIS_THINK` / `IRIS_TZ` set the *default* of
  the corresponding setting; the live value always comes from the settings service. `setup.sh`
  still needs `IRIS_MODEL` in `.env` because it decides which model to pull.

- **Verified 2026-08-02:** schema renders in-browser with correct input types per JSON type;
  a change made via `curl` appeared in an open browser with no reload (SSE); a change typed in
  the browser persisted to Postgres; settings survive an API restart; invalid enum rejected with
  a readable message; the `general.timezone` change measurably moved the `current_time` tool
  output from +02:00 to +12:00. No console errors.

---

## 12. Dropdowns Everywhere & Logo

- **Stated by Santiago, verbatim:**

  > I like it but i dont want to have to type in things where its not really needed in the web ui like for example timezone or model selection basically anywhere there is expected values there should be a dropdown.

  **Standing rule:** any setting with a knowable set of valid values is registered with an
  `enum` and renders as a dropdown. Free text is only acceptable where the value genuinely
  cannot be enumerated (a person's name, a free-form prompt). This applies to every phase.

- **Enums may be callables.** `settings.setting(..., enum=fn)` resolves `fn()` at request time,
  so choices that change at runtime stay accurate: installed Ollama models, and later connected
  audio devices, cameras, and calendars. Registered as a function, not a snapshot taken at import.

- **A dropdown must never invalidate the stored value.** `_installed_models()` keeps the last
  good list if Ollama is unreachable and always includes the active and default values, so a
  stopped service cannot make the saved configuration fail validation. Any future dynamic enum
  must do the same.

- **Logo (Phase 9, partial).** `api/static/logo.svg` — original mark, no text, 6 iris blades
  drawn as chords tangent to a hexagonal opening, inside a ring broken by six gaps. The gaps
  give it the schematic quality §2 asks for and separate it from a stock camera glyph; it is an
  independent geometric construction, not a trace of anyone else's aperture mark. Line-art in
  Aperture-orange, legible down to 16 px, used as the header mark and the favicon.
  Palette and typography are still Phase 9 work; the exact orange is not locked yet.

---

## 13. Phase 1C Decisions Log

- **scrypt from the standard library, no hashing dependency.** `hashlib.scrypt`
  (n=2¹⁴, r=8, p=1, 16-byte random salt per password) is a proper memory-hard KDF and
  ships with Python. Stored as `scrypt$<salt_hex>$<hash_hex>`; verification is
  `hmac.compare_digest`. A malformed or unknown-scheme hash verifies as False rather
  than raising, so a corrupted row cannot 500 the login endpoint.

- **Sessions in Redis, reachable by cookie *or* bearer.** The web UI gets an httpOnly,
  SameSite=Lax cookie; the Android app can send `Authorization: Bearer`. Both resolve to
  the same `session:<token>` key, which is what makes Phase 5's cross-device handoff
  possible — one session store, three surfaces. TTL comes from the `auth.session_hours`
  setting, so it is configurable in the UI like everything else.

- **`Secure` on the cookie is opt-in via `IRIS_COOKIE_SECURE`, default off.** Tailscale
  carries traffic inside its own encrypted tunnel but serves plain HTTP, where a Secure
  cookie would make login silently impossible. Turn it on only behind a real TLS
  terminator. This is an env var rather than a UI setting on purpose: getting it wrong
  from inside the UI would lock the user out of the UI.

- **Forced password change is a gate, not a suggestion.** `current_user` authenticates;
  `active_user` additionally refuses (403) while `must_change` is set. Every route except
  `/auth/me`, `/auth/password` and `/auth/logout` depends on `active_user`, so the seeded
  `1234` account genuinely cannot do anything else. Changing the password also evicts that
  user's other sessions.

- **API keys are shown once and stored as SHA-256 digests.** Keys are 256-bit random
  strings, so a fast digest is right — scrypt would only slow down webhook verification
  for no gain against a value that cannot be guessed. `verify_api_key` stamps `last_used_at`
  so abandoned keys are visible. Issued here, consumed by Phase 6.

- **Login lockout: 10 failures per account, 15 minutes.** The seeded password is `1234`,
  so unbounded guessing had to be closed. Known ceiling, marked in the code: it locks
  the *account*, so someone who knows a username can deny service for 15 minutes. Fine
  while Tailscale is the only way in; revisit if IRiS is ever exposed more broadly.

- **`/healthz` public, `/health` authenticated.** Liveness probes need an unauthenticated
  endpoint, but the detailed one names the model and tools, so it sits behind the session.

- **Verified 2026-08-02:** unauthenticated requests to `/health`, `/settings/*` and `/infer`
  all return 401; login as `creator`/`1234` reports `must_change_password: true` and settings
  return 403 until the password is changed; wrong current password and passwords under 8
  characters are rejected; after the change, settings and inference return 200 and the old
  password no longer logs in; an issued API key appears in the database only as a digest;
  logout invalidates the session; the 10th consecutive bad password returns 429 and the
  correct password is refused while locked. Browser: `/` redirects to the login page, and
  `creator`/`1234` lands on the forced password-change form rather than the settings UI.

---

## 14. App-Drawn Controls & Full Web Client

- **Stated by Santiago, verbatim:**

  > There is still HTML warnings (like password too short) and general unstyled HTML
  > elements, please fix that so all elements (drop down, increase / decrease, password
  > length etc.) are all handled by the application and not in that horrible default
  > HTML style. Also in the webui of the server should also be all of the functions
  > that will be in the app like a way to see memories interact with the model tts stt
  > etc. fully functionality.

  **Standing rule:** no native form chrome anywhere in the product. No `<select>`, no
  number-input spinner arrows, no browser validation bubbles or `required`/`minlength`
  tooltips. Forms carry `novalidate` and report every error inline in IRiS's own style.
  Applies to every phase and to Phase 5's React port — those must reuse these controls,
  not fall back to native ones.

- **Shared controls live in `api/static/app.js`:** `Combo` (replaces `<select>`,
  filterable — a 486-entry timezone list is unusable otherwise, keyboard accessible with
  arrows/Home/End/Enter/Escape and correct `combobox`/`listbox` ARIA), `Stepper`
  (replaces the number spinner, clamps and disables at bounds), `Toggle` (replaces the
  checkbox, `role="switch"`). `app.js` is wrapped in an IIFE and exports only
  `window.IRiS`, because leaking `$` into global scope makes every page that
  destructures it a redeclaration `SyntaxError`.

- **The web UI is the whole client, not a settings page.** Tabs: Chat, Settings,
  Activity. Voice and Memory arrive with Phases 2 and 3 — they are not stubbed, because
  a tab that pretends to show memories IRiS does not have yet is worse than no tab.

- **One reasoning path.** `reasoning.py` holds the tool registry and the tool-calling
  loop; `/infer` and the chat view both call `reasoning.run`, so there is one place
  where IRiS thinks rather than two that drift.

- **Conversations are server-side (Redis), per user**, storing the full transcript
  including tool turns — the chat view shows what IRiS actually did, not just what it
  said. This is the Phase 5 cross-device handoff mechanism, working early.

- **Redis now persists (AOF, `./data/redis`).** It holds conversations, which are user
  content, not just disposable sessions. Without a volume every container recreate
  silently destroyed chat history — found by losing a conversation mid-test.

- **Static files are served `no-cache`.** Browsers otherwise cache the app shell
  heuristically and keep running old code after an update; this cost a confusing
  debugging round where a fix appeared not to work. Still allows 304s. Matters more
  once Phase 5 ships updates as bundle pulls.

- **Verified 2026-08-02:** login validation reports "enter a username" inline with no
  browser bubble and no form navigation; the timezone combobox filters 486 entries to
  one on "zur"; the session-hours stepper renders with app-drawn − / + and no native
  spinner; a chat message round-trips through the tool loop and renders as bubbles; a
  follow-up in the same conversation correctly recalled the earlier question; the
  activity log shows `auth.login`, `chat.message` with tools used, `settings.change`
  and `chat.delete`, each attributed to `creator`; Redis data survived a full container
  recreate. 20/20 tests pass.

---

## 15. Phase 2 (part 1) — Speech-to-Text

- **Stated by Santiago, verbatim:**

  > I dont like the selection for chats, should be a scrollable list not a drop down.

  Conversations are a scrollable list panel beside the transcript, with the active one
  marked and per-item delete. Collapses above the chat on narrow screens.

- **STT is its own GPU service** (`stt/`, faster-whisper). Kept out of the API image
  because the CUDA/cuDNN base is ~3 GB and the API has no business carrying it. Clients
  never call it directly — everything routes through `/voice/transcribe`, same rule as
  Ollama.

- **⚠ The speech and language models do not both fit in 8 GB.** Measured 2026-08-02:

  | Whisper placement | STT latency | LLM throughput | LLM split |
  |---|---|---|---|
  | GPU, large-v3 int8_float16 (2.2 GB) | 0.8 s | 31.9 tok/s | 20% CPU / 80% GPU |
  | CPU, large-v3 int8 | 11.8 s | 72.7 tok/s | 100% GPU |

  Neither is right in every case: GPU wins a voice turn end-to-end (~4 s vs ~13 s), CPU
  wins text-only chat. **Resolved by releasing the speech model when idle** — default
  300 s, configurable (`voice.stt_idle_unload`, `0` keeps it loaded). Verified the VRAM
  is genuinely returned: 680 MiB baseline → 2253 MiB loaded → 674 MiB after the timeout.
  Cold reload 3.1 s, warm 0.6 s.

- **`large-v3`, not `medium`.** `medium int8` saves 736 MiB but mistranscribed German —
  "Bremdeneinstellungen" for "Blendeneinstellungen". Accuracy in German is a Section 1
  requirement, so the VRAM is worth it. Both were correct in English.

- **Accuracy check (espeak-ng synthesis, so a floor not a ceiling):** English and German
  both transcribed verbatim with correct auto language detection (de 0.979, en 0.926).
  Swiss German remains untested — Section 1 already calls it best-effort.

- **⚠ The microphone needs a secure context.** Browsers grant `getUserMedia` only on
  `localhost` or HTTPS. Over plain-HTTP Tailscale the mic will refuse, which affects
  Phase 5's phone app directly. Fix is `tailscale serve https`, at which point
  `IRIS_COOKIE_SECURE=1` should also be set. Not yet configured.

- **Still open in Phase 2:** TTS (blocked on the ASK USER voice-source decision),
  openWakeWord, and turn-taking/barge-in orchestration. Push-to-talk works today.

- **Verified 2026-08-02:** unauthenticated `/voice/transcribe` returns 401; authenticated
  German and English transcriptions are verbatim; empty audio returns 400; `/voice/status`
  reports the loaded model; the conversation list renders scrollable with the active item
  marked; the mic button renders in the composer and reports a clear message when the
  browser withholds microphone access. 20/20 tests pass.

---

## 16. Phase 2 (part 2) — Voice Output & Streaming

- **Voice source, resolved.** Santiago chose **option (b)**: a built-in XTTS speaker
  shaped through pacing and delivery, not a cloned recording. The Phase 2 ASK USER is
  closed and Section 5 is updated. XTTS v2 ships 58 built-in speakers; the voice and its
  pace are settings (`voice.tts_speaker`, `voice.tts_speed`), so the choice stays
  reversible without touching code.

- **Stated by Santiago, verbatim:**

  > For text i dont want to have to wait for the entire response to generate, it should
  > 'stream' the text, and the audio should also not generate the whole response and only
  > then say stuff, it should do it per sentence, so that while one sentence is being
  > outputted another is being generated stringing it together so there is no waiting for
  > the user, or at least a minimal amount.

  **Standing rule:** nothing user-facing waits for a whole response. Text streams token
  by token; speech is synthesised and played per sentence, with the next sentence
  synthesised while the current one plays. Applies to every later phase that produces
  long output, and to Phase 5's React port.

- **PyTorch must come from the cu124 index.** The default PyPI wheel targets a newer CUDA
  than driver 550 supports and dies at runtime with *"driver is too old (found version
  12040)"*. `tts/Dockerfile` installs `torch==2.6.0+cu124` **before** `coqui-tts` so the
  dependency resolver cannot pull the incompatible build. The STT service is unaffected
  because CTranslate2 bundles its own CUDA runtime — a difference worth remembering when
  adding any further GPU service.

- **Synthesis is faster than playback**, which is what makes the per-sentence pipeline
  work: 1.45 s to render a sentence that takes ~5 s to speak, so speech stays ahead of
  the reader after the first sentence. XTTS output is 24 kHz mono WAV.

- **One reasoning path, still.** `reasoning.stream()` is now the primitive and
  `reasoning.run()` consumes it, so streaming and non-streaming cannot drift. Tool calls
  work mid-stream: a round that ends in tool calls executes them, emits a tool event, and
  starts the next streamed round.

---

## 17. Persona, Voice Quality & Visual Corrections

Stated by Santiago, verbatim:

> I want the voice to have a brittish accent, if possible, even if just slightly.
> ALso as you can see in that image some icons are white and not really visible or in
> line with the rest of the design. I also want the conversations tab to be seperate
> from the chat. Also the voice is a bit too slow. [...] as you can see here i kind of
> want the system to 'pretend' to have feelings, also integrate more of that GlaDOS
> personality its barely shining through, i also want things like 'Hello, creator' and
> 'Certainly Creator' etc. like more sentences etc. that feel like an actual
> conversation instead of just a LLM answering, i want it to feel 'alive' yknow? Also
> sometimes it sais things like 'dot' for '.' It also sometimes sais things weird, like
> not quite right. at the end of sentences it kind of 'cuts off' for the last word i
> dont know. Instead of having 'iris' at the chat bubbles it should just be the logo.
> also redesign the logo its a bit too much like apertures, i need something original
> but fitting with my design philosophy. The voice also sometimes sais things like 'AG'
> as literally ag instead of A G or IT as 'it' instead of I T. [...] It also kind of
> behaves too much like a normal LLM, as said i want more of that GlaDOS personality
> integrated into it.

**Standing rule — IRiS is a character, not a chat completion.** It addresses Santiago as
Creator, speaks as though it has an interior life, and carries the Section 1 persona in
every reply. "I'm just a virtual assistant, so I don't have feelings" is a defect, not a
disclaimer. This is Phase 8 work pulled forward because the persona is what makes the
rest feel like IRiS rather than a model behind a form.

**Standing rule — spoken text is not written text.** Markdown, acronyms and bare
punctuation must be normalised before synthesis. Reading "**" aloud, saying "dot" for a
period, or pronouncing AG and IT as words are defects.

### 17.1 Resolutions

- **Persona (`api/persona.py`).** IRiS now carries a system prompt on every request that
  forbids the "just an AI" disclaimer, gives it an interior life, has it address Santiago
  as Creator, and prefers prose to bullet lists. It is a **setting**, so it is editable in
  the UI and gives Phase 8's self-modification something to diff against. The prompt is
  sent with each request but deliberately **never stored in the transcript**, so editing
  it takes effect on existing conversations instead of being frozen in at creation.

- **Spoken-text normalisation (`voice.speech_text`).** Strips code fences, inline code,
  links, emphasis, headings and list markers; renders "1." as "1," (the source of the
  spoken "dot"); spells initialisms but not pronounceable all-caps words; and appends a
  trailing pause because XTTS clips the tail of an utterance. Heuristic for spelling:
  ≤3 letters or no vowels → spell it (AG, IT, DHCP, API); otherwise leave it (SIDMAR,
  NASA), with a small exception set. Mixed case (IRiS) is never touched.

- **⚠ TTS engine changed to Piper — needs Santiago's confirmation.** Two problems forced
  this: XTTS cannot be given a British accent reliably (its 58 speakers are unlabelled
  and unauditionable), and it **cannot coexist with the language model**. Measured:

  | | XTTS (GPU) | XTTS (CPU) | **Piper (CPU)** |
  |---|---|---|---|
  | VRAM | 1.65 GB | 0 | **0** |
  | Speed | 3.4x realtime | 0.3x realtime | **25–33x realtime** |
  | Alongside qwen3:8b | **CUDA OOM** | works, too slow | **works** |
  | British voices | unlabelled | unlabelled | **11 explicit en_GB** |

  Quantising the LLM's KV cache (`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`)
  recovered ~700 MB and returned qwen3:8b to 100% GPU, but XTTS still OOMs beside it.
  Both engines ship and are selectable (`voice.engine`); Piper is the default. XTTS falls
  back to CPU on OOM rather than failing outright. **This deviates from Section 5, which
  names XTTS v2/F5-TTS — ASK USER to confirm.**

- **Asset versioning.** `app.css`/`app.js` are served with a content-hash query and the
  HTML shell with `no-store`. `no-cache` alone was insufficient: a copy cached *before*
  that header existed kept being served, which is what made the icons render as unstyled
  white boxes in Santiago's screenshot. This was a caching fault, not a styling one.

- **UI.** Conversations moved to their own tab; the assistant bubble carries the logo mark
  instead of the word "IRiS"; long-form settings (the persona prompt) render as a
  textarea; the voice setting has a preview button, since a voice can only be chosen by
  ear.

- **⚠ Logo redesigned — needs Santiago's pick.** The aperture mark was too close to its
  inspiration. Eight alternatives were drawn and three refined; the installed default is a
  **silicon die** — square outline, pin ticks, central node — which is original, reads at
  15 px, and finally uses the "in Silico" half of the name. Alternatives kept for
  comparison: a bracket monogram and a HUD reticle.

---

## 18. Web Search, Interior Life, UI Polish

Stated by Santiago, verbatim:

> en GB-cori-high should be a good default voice, but now its a bit too fast so 1.1
> should be good. For reasoning the default should be model-default. [...] i also want
> you to get rid of em-dashes, they ruin the flow of text. Also, the iris logo in the tab
> favicon is still the old one, make the logo on the webui and in conversations a bit
> bigger, like twice the current size, same goes for the speaker icon next to it in
> conversations. The top menu bar should be 'sticky' so that when a conversation
> continues beyond viewport length only the chat itself is scrollable, the menu stays
> where it is. I like the silicon die logo so keep that but yknow make the changes i
> mentioned for size etc. Refine the top bar, especially the user section with the
> username and sign out it seems a bit unpolished. [...] it sais something about its data
> being from 2023, which im assuming is when it was trained, but for information it does
> not have it should have that web search feature where it can get that information (i
> also want an indicator that its searching something on the web when it is etc.) just
> make the whole UI more polished and make the changes i said, the thing should be able
> to search the internet for information not guess based on the data it was trained on.
> [...] as you can see the inner life is also not very polished, still seems more like a
> chatbot rather than a living, breathing, thinking colleague. Also the conversation bar
> where i can enter text etc. should also be sticky to the botton of the viewport to make
> things easier.

**Standing rule — no em-dashes.** Not in IRiS's replies, not anywhere user-facing.

**Standing rule — search, do not guess.** IRiS must never answer a factual question from
stale training data or cite a "knowledge cutoff" as a dead end. If it does not know
something current, it searches. The user sees when it is searching and what for.

**Standing rule — the interior life is not a disclaimer with better manners.**
"I don't have personal curiosities, Creator, but..." is the same defect as "I'm just an
AI". Asked what it is curious about, IRiS names something specific and means it.

### 18.1 Resolutions

- **Web search (`searxng` service + `web_search` tool).** Self-hosted SearXNG per Section 5.
  The tool routes through the API like everything else. The persona now instructs IRiS to
  search rather than answer from memory, and forbids citing a "knowledge cutoff" at all.
  Verified on Santiago's own example: asked about SIDMAR AG in Mönchaltorf it previously
  invented a German battery company; it now searches and returns the real Swiss IT firm,
  address, phone and all.

- **Search is visible.** `reasoning.stream` emits a `tool_start` event before running a
  tool, so the transcript shows "searching the web: <query>" live, then collapses to a
  compact list of source links.

- **Em-dashes are removed at the source.** The persona forbids them and
  `reasoning.strip_dashes` rewrites any that slip through as ", " on the way out, before
  the delta reaches the client.

- **Persona, second pass.** Rules alone did not hold on an 8B model: it replaced the
  banned "What can I assist with?" with "Let's see what you need", and answered "do you
  get bored" with "Boredom is a state, not an emotion, I don't experience it as humans
  do", which is the same disclaimer in a new costume. What worked was **worked examples**:
  each common question paired with its BAD chatbot answers and *several* GOOD ones. Several
  matters, one GOOD line per question got copied verbatim. Known ceiling: on the exact
  example questions it still leans on the examples' phrasing, and the model is small enough
  that this needs revisiting if the persona drifts.

- **Defaults set as requested:** voice `en_GB-cori-high`, pace 1.1, reasoning
  `model-default`.

- **UI.** Chrome (top bar, HUD, tabs) and the composer are fixed; only the transcript
  scrolls. Bubble mark 15px to 30px, speaker icon 13px to 24px, header mark 34px to 46px.
  The user section is a proper chip with name over role and an icon sign-out. `logo.svg`
  is now stamped with the asset hash too, which is why the favicon was still showing the
  old mark: the tab icon is cached hardest of all.

---

## 19. Attachments, Sources, and Chrome

Stated by Santiago, verbatim:

> The search indicator and the scrolling on the website have the default browser / html
> scrollbar which is pretty ugly, make it inline with our design philosophy. I also dont
> need to see 'everything' it found in the search, like its just a little 'Performed a
> websearch' thing that i can click to see the full result instead of always seing the
> full result of the search even when i might now want it. I also want that detailed view
> of the search to have URLs in it from where it got the information so i can check the
> sources where needed.
>
> Also the logo is good but make the lines ticker on the logo everywhere so its easier to
> see and not so 'flimsy' [...] especially the favicon which has to be easily decipherable
> as the iris logo
>
> Also the conversations are kept but when i click on one i want that conversation to
> actually open up in the chat so i can continue where i left off. Also the web search
> summary thingie is good but its blue, make it orange to fit with the rest of the design.
> Also it sais 'creator' twice at the top where i see my user which i dont want. Is image
> and document analysis part of a later phase? if not i want you to integrate it now
>
> Also i cant scroll on the chat anymore and the top bar isnt sticky to the top

### 19.1 Resolutions

- **Scrollbars are app-drawn.** Thin, `--line-2` thumb turning orange on hover, inset to
  match whichever surface they sit on. Chrome ignores `::-webkit-scrollbar` once
  `scrollbar-width` is set, so the standard properties are scoped behind
  `@supports not selector(::-webkit-scrollbar)` for Firefox and the webkit pseudo-elements
  drive Chrome.

- **Search results are collapsed by default** to a single "Performed a web search for …,
  N sources" line, expandable to the title, full URL and snippet of every source. Native
  `<details>` carries the toggle and keyboard behaviour; only its marker is restyled. The
  same component renders stored conversations, not just live ones, which was a real gap:
  the collapse originally applied only while streaming, so reloading a conversation
  brought the raw dump back.

- **Chat scrolling regression.** `.chat-main` had `min-width: 0` but not `min-height: 0`.
  A column flex child defaults to `min-height: auto` and refuses to shrink below its
  content, so `.chat-log`'s overflow never engaged, the shell overflowed, and the chrome
  scrolled off the top. Both symptoms, one cause.

- **Logo.** Variant B all round, per Santiago: stroke 5.2 to 10, pins 4.2 to 9, centre dot
  6.5 to 9.5. A separate lighter favicon cut was tried and rejected; one asset serves both.

- **Inline markdown** is rendered as DOM nodes (bold, italic, code) rather than shown as
  literal asterisks. Never `innerHTML`.

- **The user chip** prints the role only when it differs from the username, so `creator`
  no longer appears twice.

- **Image and document analysis (`api/files.py`)** — Phase 6, pulled forward like web
  search. Images go to a local VLM (`qwen2.5vl:3b`, chosen over 7b so loading it disturbs
  the language model less on 8 GB); PDF via pypdf, DOCX via python-docx, and plain text
  read directly. An upload becomes **text**, which then travels with the message as an
  `attachments` field.

  **Attachments are stored beside the content, not inside it.** `reasoning.for_model`
  folds them into the prompt on every request and strips keys Ollama does not understand.
  The model therefore still has the file on later turns without it being resent, while the
  UI shows the message clean with a collapsible attachment beneath. Verified: a follow-up
  question answered from the document without the file being sent again.

- **Attachments render below the message**, stacked, in a `.msg-group` wrapper rather than
  inside the bubble. The "USER" label is gone; alignment already says who is speaking.

- **`current_time` returns a spelled-out timestamp**, not ISO. The model read
  `2026-08-03T01:44+02:00` and said "3 PM"; it now gets
  "01:45 on Monday, 03 August 2026 (Europe/Zurich, 24-hour clock)". The clock itself was
  always correct: host 01:44 CEST, container 23:44 UTC, same instant.

---

## 20. Search Aggressiveness, Speech Prosody, Rendering

- **Scrolling "sometimes stopped" because inner boxes stole the wheel.** The search-source
  list, tool detail and attachment body each had their own `max-height` + `overflow-y`, so
  hovering one latched scrolling to it. They now grow naturally; the transcript is the only
  scroller. Verified zero inner scroll containers in the log.

- **`51–200` became `51, 200`.** The em-dash filter treated a numeric range as a clause
  break, inventing a second number. A dash between digits is now a hyphen.

- **The model anchored on its training year.** Given no date it searched "Subnautica 2
  release status 2023" and answered "as of 2023". The system turn now carries today's date
  and forbids putting a year in a query or saying "as of <year>". It then searched
  ...2026 and reported the real Early Access date.

- **`llm.search_policy`**, default **aggressive**: search essentially any question of fact,
  including ones it believes it knows. Also balanced / sparing / off; `off` removes the
  tool entirely rather than just discouraging it.

- **Speech prosody.** A comma is inserted before an unpunctuated "(", every line ends with
  a stop so numbered steps do not run together, and the trailing ellipsis is now XTTS-only,
  which is what made Piper trail off into a mumble.

- **Markdown blocks render.** Bullets, numbered lists and headings were shown literally;
  now rendered as DOM nodes alongside the existing bold/italic/code.

- **Attachments sit inside the message container**, below the text, stacked, separated by a
  rule. Images in formats the vision model rejects (webp, bmp, tiff) are converted to PNG
  first, which is what caused "Failed to load image or audio file".

- **Not built, deliberately.** Reverse image search and identifying people from photographs
  are face-recognition capabilities and are not being added. What does work: the vision
  model reads logos, text and landmarks in a picture, and IRiS then searches for those, as
  it did with the SIDMAR logo. German words spoken by an English Piper voice will stay
  wrong; the phonemiser is English-only, so fixing it properly means routing German text to
  a German voice.

---

## 21. Playback Control, Brevity, Code Rendering

- **One audio player for the page.** Clicking a speaker while it plays stops it; clicking
  a different reply's speaker replaces the current one instead of layering a second voice.
  The button swaps to a stop icon while playing. A `playToken` guards the async gap, so a
  reply superseded during synthesis never starts.
- **`voice.speak_replies` now defaults on.**
- **`voice.expressiveness`** (default 0.25) scales Piper's `noise_scale` / `noise_w_scale`,
  whose defaults (0.667 / 0.8) read as excitable. Low is level and matter-of-fact.
- **Brevity.** The persona now bans "Key Details" headers, restating the question,
  summarising itself and listing next steps. The SIDMAR answer went from ~1600 to 607
  characters.
- **An explicit instruction to search is an order**, not a suggestion, and the persona says
  so. Verified: "Search the web for the current Debian stable version" searched.
- **Ordered lists carry their real number** via `li.value`; a paragraph between items used
  to restart the list at 1 while the spoken version said the right numbers.
- **Fenced code blocks render** as a bordered monospace panel with the language tag, rather
  than printing the backticks.
- **The logo viewBox is cropped to content** (`9 9 102 102`), so the mark fills the frame
  and the tab icon stops looking tiny.

## 22. Automatic Playback Owns the Button, Emoji Ban

Santiago, verbatim:

> Since the model automatically sais things by default once it starts playing the response
> automatically the icon should also change to a stop icon from the speaker, right now it
> only does so when i manually click the sound icon.

> Also NO emojis, AT ALL they are BANNED this is an assistant not some visual guide to
> making you feel better

Resolved:

- **Root cause of the icon bug:** there were two playback paths. `speakText()` owned the
  button state (`currentBtn`, `setPlayIcon`); the streaming `speechPipeline()` shared only
  `currentAudio` and never claimed a button. Worse, the reply's speaker button was not
  created until the `done` event, so during automatic playback there was no button to
  change. `speechPipeline(btn)` now claims and releases the button exactly as a manual play
  does, the button is created on the first delta, and `stopSpeech()` stops an active
  pipeline, so clicking the stop icon during automatic playback actually stops it. An empty
  queue mid-reply does not release the icon (that is just the generator outpacing the
  voice); `finish()` marks the end of the reply.
- **Emojis are banned in both places, same pattern as em-dashes:** the persona forbids them
  outright, and `reasoning.strip_emoji()` is the safety net applied to every streamed delta.
  The filter covers pictographs, dingbats and the variation-selector/ZWJ sequences, and
  deliberately does not touch bullets, arrows, accented letters or the copyright sign.
- The observed failure ("I'm operational and ready to assist! ... While I don't experience
  emotions like humans do ... How can I make your day better?") is now a verbatim BAD
  example in the persona, since worked examples move this model where rules do not.

## 23. Phase 2 Completion — Wake Word and Turn-Taking

Closes the two items §6 left open for Phase 2: "wake-word listener, turn-taking
orchestration (barge-in, silence detection)".

**Where it runs.** The microphone belongs to the browser, so the browser is the
microphone and nothing more: an `AudioWorklet` decimates to 16 kHz mono Int16 and
ships 80 ms frames up `ws://…/voice/listen`. openWakeWord runs in the `api` service
rather than `stt`, because the WebSocket has to terminate where the session cookie
is understood, and splitting them would mean proxying every frame through a second
hop for nothing. The socket is only an *ear*: it returns a transcript, which the
page sends through the ordinary chat path. A spoken turn and a typed one are the
same code, the same conversation and the same storage.

**Turn-taking state machine** (`api/wake.py`, `_Turn`):
`sleeping` → (wake word) → `waiting` → (speech starts) → `capturing` → (silence, or
the length cap) → transcribe → `sleeping`. A refractory period plus `Model.reset()`
stops the audio still sitting in openWakeWord's buffers from re-firing.

**Barge-in.** While the page reports that IRiS is speaking, the wake word is
deliberately *not* watched for, so IRiS saying its own name cannot wake it; only
barge-in is armed. Sustained speech (0.32 s, not one frame, or a cough interrupts)
stops playback and captures the interruption, including ~1 s of pre-roll so the
first words are not clipped. It relies on the browser's `echoCancellation`, which is
why it is a setting that can be turned off on loudspeakers.

**Verified end to end** against the running stack, using our own Piper voice as the
test speaker: unauthenticated socket refused (1008); two seconds of silence produce
no events; "Hey Jarvis." fires `wake`; the following sentence produces
`listening` → `thinking` → `transcript: "Stop talking for a moment, I have a
question."`; and speech during playback produces `barge_in`.

**Wake words that are not names.** Santiago: *"are there any other non-name specific
words we can use to wake it i dont want jarvis"*. All six words bundled with
openWakeWord are names, so `setup.sh` fetches four from the Home Assistant community
collection (~200 KB each) and the default is now **`computer`**. Measured against our
own Piper voice, peak score on its own phrase versus on unrelated speech:

| model | its phrase | other speech |
|---|---|---|
| `computer` | **0.990** | 0.001 |
| `ok_computer` | 0.881 | 0.001 |
| `ok_home` | 0.869 | 0.001 |
| `hey_house` | 0.854 | 0.001 |
| `glados` | 0.001 | 0.001 |

`glados` was tried first, being thematically ideal, and is a dud: it never fires at
any pronunciation, so it is deliberately not shipped. None of the 122 community
models is an "iris".

**Open, and Santiago's call: the wake word is not "IRiS".** §5 specifies
"openWakeWord, trained on 'IRiS'". No pre-trained model for that phrase exists
publicly — the official repo bundles six words and the 241-model Home Assistant
community collection has no match. The listener is model-agnostic and any `.onnx`
dropped into `./data/wakewords/` appears in the dropdown without a restart, so the
remaining work is one training run: openWakeWord's pipeline needs
piper-sample-generator plus several GB of negative-feature datasets and about an
hour on the 3060 Ti. Default is `hey_jarvis` until that is done. **ASK USER** before
spending the download and the GPU time.

### Decisions and traps recorded

- **openWakeWord 0.6.0 declares a hard `tflite-runtime` dependency with no Python
  3.12 wheel.** A plain `pip install openwakeword` therefore back-tracks silently to
  0.4.0, which has a different API (`wakeword_model_paths`, no `inference_framework`,
  no `download_models`). Installed with `--no-deps` plus explicit dependencies;
  `inference_framework="onnx"` must be passed because the default is `"tflite"`.
- **`download_models(target_directory=…)` is a trap.** The bundled Silero VAD path is
  computed at import time from site-packages, so redirecting the download breaks
  `vad_threshold` with a missing-file error. The 18 MB of weights are baked into the
  image instead, and the volume carries only custom models.
- **`openwakeword.MODELS[name]["model_path"]` points at the `.tflite` copy**, which is
  unusable here. Both formats are downloaded, so the ONNX sibling is resolved by
  extension swap.
- **The first five `predict()` calls always return 0.0** regardless of input
  (`model.py` buffers before scoring). Harmless here, but it means a wake word cannot
  fire in the first 400 ms of a connection.
- **Silero VAD only accepts whole 30 ms frames** and 80 ms is not a multiple of 30 ms,
  so the remainder is carried between calls rather than dropped; dropping it would
  quietly hide a third of the audio from the endpointer.
- **Auth on a WebSocket.** `auth._token_from` and `auth.current_user` are typed
  `HTTPConnection` rather than `Request`, which is the shared base of both and is
  what FastAPI fills on either scope, so the existing session gate works unchanged.
  The handler translates `HTTPException` into a 1008 close, because Starlette's HTTP
  exception handler cannot produce a valid response on a WebSocket scope. A browser
  cannot set an `Authorization` header on a WebSocket, so this authenticates by
  cookie; a session token is deliberately not accepted in the query string.
- **One switch, not two.** The composer button writes `voice.hands_free` and the live
  settings feed drives the microphone, so the button and the Settings toggle cannot
  disagree.
- **One `Model` per connection.** ponytail: ~50 MB and half a second each, which is
  right for a personal assistant; share and lock it if many clients ever listen at once.

### Pre-existing defects fixed in passing

- `setup.sh` pulled only the language model. `qwen2.5vl:3b`, which reads uploaded
  images, was never pulled, so image analysis stalled on a silent 3 GB download at
  first use. This was a live §3.4 violation; `pull_models` now pulls both, and
  `IRIS_VISION_MODEL` is in `.env`, `.env.example` and compose.
- The test suite was red. Two assertions described behaviour deliberately changed in
  earlier commits: `current_time` no longer returns ISO (§20 changed it because the
  model read an ISO timestamp back as the wrong time of day), and the trailing
  ellipsis became XTTS-only (§21, it made Piper mumble). Both now assert the real
  contract. 37 pass.
- `README.md` documented a test command that could not work: `test_api.py` is not in
  the image and the api service has no source mount. Corrected to mount the file.

## 24. Training a Custom Wake Word

Santiago, verbatim:

> Can we train a model to listen to Iris / hey iris etc. ? / are there any other
> non-name specific words we can use to wake it i dont want jarvis

Answered in two parts. The non-name words are handled in §23 and shipped
immediately (`computer` is now the default). This section is the training path, and
Santiago chose **"hey iris"** over bare "iris": upstream's guidance is that longer
phrases are markedly more reliable, and "iris" is both two syllables and an ordinary
English word, so it would collide with normal speech far more often.

**`./wakeword/train.sh "hey iris"`** does the whole job. It is not part of `setup.sh`
and `docker compose up` never builds it, because it pulls torch, speechbrain and a
TTS stack that the running assistant has no use for.

| stage | what it does |
|---|---|
| build | `wakeword/Dockerfile`, CUDA + torch on Ubuntu 22.04 for Python 3.10 |
| fetch | `fetch_data.py`, ~6 GB into `data/wakeword-training` |
| generate | 30,000 synthetic sayings of the phrase, many voices, via piper-sample-generator |
| augment | room impulse responses and background noise mixed in, features computed |
| train | DNN head over the frozen melspectrogram and embedding models |
| install | the `.onnx` into `data/wakewords/`, where the dropdown finds it |

**Python 3.10, not 3.12.** `piper-phonemize` has no 3.12 wheels, and the pipeline was
written against Colab's 3.10. Ubuntu 22.04's system Python is 3.10, which is why the
training image is built on the CUDA Ubuntu base rather than `python:3.12-slim`.
`webrtcvad` has no wheels at all and fails to compile without headers, so
`webrtcvad-wheels` is used instead.

**The tflite export at the end is expected to fail** and is ignored. It needs
`tensorflow-cpu==2.8.1` and `onnx_tf`, an ancient stack we deliberately do not
install; the ONNX file we actually use is written *before* that step. `train.sh`
tolerates the failure and then checks the `.onnx` exists.

**The three stages are separately flagged** (`--generate_clips`, `--augment_clips`,
`--train_model`) and run as three invocations, so an interrupted run resumes at the
stage it reached instead of regenerating 30,000 clips.

**`target_false_positives_per_hour: 0.2`.** A wake word that fires unbidden is worse
than one that occasionally needs saying twice, and this listens all day in a home.
The config also names confusable phrases explicitly (`iris`, `irish`, `hey chris`,
`the iris`, `my eyes`) on top of the phoneme-overlap negatives the pipeline mines
for itself.

**Validate before trusting.** `wakeword/evaluate.py` scores a model against three
Piper en_GB voices the training never saw, plus negatives that deliberately include
"The iris of the eye controls how much light gets in" and "Hey Chris, are you coming
to the thing on Saturday". The gap between the columns is the whole result; a model
is only usable if the weakest positive clears 0.5 while the worst negative stays
under 0.3.

## 25. Phase 3 Part One — Memory

Delivers the parts of Phase 3 that were never blocked: embedding, storage, recall
and the Memory tab §14 asked for. The **backup destination is still an open ASK
USER**, and raw ingestion depends on decisions that follow from it.

**Two paths, and the second is the one that matters.** A `remember` tool lets IRiS
deliberately store something, but automatic recall is what makes memory work: every
user turn is embedded, searched, and anything relevant is folded into the system turn
before the model sees the question. Leaving retrieval to a `recall` tool alone would
mean an 8B model mostly never calls it.

**No new dependency.** Qdrant speaks plain HTTP and httpx is already in the image, so
there is no client library. Embeddings come from Ollama, which is already running, so
bge-m3 is a model pull rather than a service.

**Thresholds are measured, not guessed.** The first draft used 0.55 and silently
dropped half the genuine recalls. Measured against a real store:

| query | score of the memory that should match |
|---|---|
| "should I use emojis" | 0.589 |
| "what hardware does he have" | 0.563 |
| "what GPU is in the machine" | 0.541 |
| "where does he work" | 0.534 |
| "who employs him" | 0.518 |
| "how should I write to him" | 0.434 |

against unrelated questions topping out at 0.368 ("explain TCP handshakes"). The
default is 0.42. The bands are narrow and this is a calibration knob, not a constant,
which is why it is a setting.

**Short turns skip recall entirely.** bge-m3 scores a two-word fragment at ~0.44
against almost anything, well inside the range a real match occupies, so "the
weather" pulled in every memory stored. Turns under three words do not reach the
store, and they are also the ones least likely to need it.

**Dedup on write.** A new memory scoring above 0.93 against an existing one replaces
it rather than stacking a near-identical copy. Verified: storing four facts where two
are paraphrases leaves three.

**Failure is silent by design.** Memory is an enhancement to a reply, so a cold
Qdrant or a missing embedding model returns nothing rather than breaking the turn.
The one loud failure is a dimension mismatch, which happens if the embedding model is
changed under an existing store; that says so explicitly rather than returning
nonsense.

**Tools are withheld when they cannot work.** With no user in context, or memory
switched off, `remember` and `recall` are not offered at all. An 8B model handed a
tool that cannot work will call it anyway and then explain the error to the user.

### Still open in Phase 3

- Raw conversation ingestion, diarization (pyannote), chunking.
- Nightly compaction enforcing the 30-day rolling raw retention.
- **ASK USER, unchanged:** where encrypted Postgres and Qdrant exports should go.
  Proxmox Backup Server, a NAS, an external drive, or cloud storage.

## 26. Backups

Santiago, verbatim:

> The backups should be in the repository in a 'backup' folder of some kind, you
> chose the most elegant solution.

Built as `./backup/`, beside `./data/`, and **gitignored**. §4 fixes this repo as
public with "secrets/credentials/personal data never committed", and an archive of
every conversation and memory is precisely that. "In the repository" is therefore
read as the repository *directory* on disk, never the git history.

**One flag raised, and it stands.** §6 Phase 3 asks for a target *outside*
stzrhws01, because the point is surviving that machine failing, and a folder on it
does not. The instruction was explicit so it is built as asked; because the target is
an ordinary directory, an rsync to a NAS, a Tailscale copy to another node or an
external drive covers the gap without changing anything here.

**What is in it.** All three stores, or the backup is worthless: Postgres via
`pg_dump` (accounts, settings, audit log), Qdrant via its own snapshot API
(memories), Redis as our own JSON (conversations). Sessions are deliberately excluded
— restoring a month-old login token is a liability, not a feature.

**Encryption with no new dependency.** AES-256-CBC, PBKDF2 at 200,000 iterations,
via the `openssl` binary that `setup.sh` already requires for generating the database
password. A restore therefore needs nothing that is not on every Linux box. The key
is `IRIS_BACKUP_KEY`, generated once into `.env` by `setup.sh`; `add_env` never
overwrites an existing value, because regenerating it would silently orphan every
archive already written.

**Stateless scheduling.** Rather than remembering when it last ran, the scheduler
asks the backup directory whether today already has an archive. A restart cannot
cause a double run, and a machine that was off overnight backs up as soon as it is
on rather than skipping the day. Checked once a minute so a change to the time in
the UI takes effect immediately.

**Pruning happens only after a successful archive**, so a failing backup can never
eat the history it was supposed to protect.

**`restore.sh` exists because a backup that has never been restored is theatre.**
Verified end to end: three memories stored, archive taken, the entire Qdrant
collection deleted, restored from inside the encrypted archive, three memories back.

**Path traversal.** The delete endpoint takes a filename from the client, so it is
reduced to its basename and must match the archive prefix and suffix; a test asserts
that `../../etc/passwd` and friends cannot escape.

## 27. Learning From Conversations

Memory that only fills when the model remembers to call `remember` mostly stays
empty, so every completed exchange now gets a second, tool-free and persona-free
extraction pass. It is fired detached from the reply: the user is already reading,
and a failure there must never surface as a broken conversation.

**The padding problem, and the actual fix.** The extractor pads its list regardless
of the prompt. Asked about a move from Zurich to Winterthur it returned three lines,
the third being "They are adjusting to a new daily routine" — which nobody said.
Tightening the prompt with "do not infer, generalise or embellish" did not remove it;
it reproduced verbatim on the next run. Two things were wrong at the root:

1. **A quota is an instruction to fill it.** "At most 3 lines" reads as "give me 3".
   The prompt now says the opposite and means it: *"Most exchanges contain nothing
   durable. An empty list is the normal, correct answer and is always better than a
   padded one."*
2. **Free-form prose left nothing to check.** Every fact must now arrive with a
   `quote`: a span copied word for word from the conversation. Ollama's structured
   output (`format` with a JSON Schema) makes the shape non-negotiable, and the quote
   is then verified as a substring of the source, compared on words alone so
   punctuation and case cannot break a real span.

That moves the question from *"did the model obey"* — undecidable, and it does not —
to *"is this string present"*, which is decided here. **A model can invent a fact; it
cannot invent a quote that is already in the text.** The earlier word-overlap check
survives as a second net, now applied against the fact's own quote rather than the
whole exchange, which is far tighter: quoting correctly and then asserting something
unrelated is the obvious way round the first check, and it is tested.

Measured after the change, one precise fact per real exchange and nothing else:

| exchange | stored |
|---|---|
| "moved from Zurich to Winterthur" | *The user has moved from Zurich to Winterthur.* |
| "always give me answers in metric" | *The user prefers metric units over Fahrenheit.* |
| "stzrhws01 has an RTX 3060 Ti with only 8GB" | *The user's server stzrhws01 has 8GB of VRAM.* |
| "What is the capital of Australia?" | nothing |
| "haha ok thanks that helps a lot" | nothing |

## 28. Retention

§6 asks for "nightly compaction enforcing the 30-day rolling raw retention". That
became buildable once conversations were distilled into memories (§27), because the
distinction it depends on now exists: **the transcript is raw, the memory is not.**
What expires nightly is the verbatim record; what IRiS learned from it survives.

Runs an hour after the backup, so a transcript is always archived before it is
expired. `memory.retention_days` defaults to 30 and 0 keeps everything forever.

**A conversation with no timestamp is treated as old, not immortal.** Entries predate
the `updated` field, and the alternative reading would make them permanently
un-compactable.

Verified on fixtures, since this deletes real user data: at 30 days a 29-day-old
conversation is kept and a 31-day-old one is not, `0` removes nothing at all, the
transcript body is deleted alongside its index entry rather than orphaned in Redis,
and other users are untouched. All of that is pinned by a test with a fake Redis.

## 29. Audio Ingestion

A recording goes in and two different things come out, which is the point: the
**verbatim record**, chunked and embedded so it can be searched, and any **durable
facts** in it, distilled through the same evidenced extractor as a chat turn (§27).
The chunks are episodic; the facts are what IRiS actually learned.

Chunking splits on sentence boundaries only. A chunk cut mid-sentence embeds badly,
because half a thought is close to nothing, and one sentence of overlap keeps a fact
that straddles a boundary retrievable from either side.

Verified end to end with our own Piper voice as the speaker: a synthesised meeting was
transcribed, chunked and stored, three correct facts were distilled from it, and
asking "how long are transcripts kept" returned the distilled fact at 0.799 above the
raw transcript chunk at 0.614 — which is the ranking that makes this worth doing.

### Still open in Phase 3: diarization

**Blocked on Santiago, not on work.** pyannote's speaker-diarization models are gated
on HuggingFace: using them means accepting the model licence with a HuggingFace
account and issuing a read token. Nothing in IRiS can do that on his behalf.

The unblocked alternative, if he would rather not: speechbrain's ECAPA-TDNN speaker
embeddings are ungated and already installed in the wake word training image.
Clustering those over Whisper's segments gives "speaker 1 / speaker 2" without names
or a token, which is most of the value. **ASK USER** before building either.


## 30. Phase 4 Part One — Looking at Cameras

Santiago: *"move on to phase 4"*. Built the half that needs no decisions from him.

**IRiS can look at a camera and say what it sees.** A frame is pulled from the stream
with ffmpeg and handed to the vision model that already reads uploaded images, so the
whole feature is a camera registry plus a tool. Cameras are added in the UI with their
stream URL; `look_at_camera` is registered in the tool loop, so "is anyone at the
front door?" resolves in chat.

**Credentials never leave this service intact.** Stream URLs carry a password that is
usually reused across a household's devices, so cameras are admin-only and everything
outbound is masked: the browser, the activity log, and ffmpeg's stderr, which echoes
the URL verbatim on failure. Two bugs were caught here by testing rather than reading:

- The first masking pattern was lazy and leaked the tail of any password containing
  a colon: `rtsps://user:p@ss:word@cam.local` became `rtsps://user:____@ss:word@...`.
  Now greedy to the last `@` in the authority.
- `-rtsp_transport tcp` was passed unconditionally, and ffmpeg refuses the whole
  command when the input is not RTSP. That would have broken every `http://` snapshot
  URL, which is how a good many cameras expose a still.

RTSP is forced over TCP because UDP loses packets on wifi cameras and produces smeared
frames the vision model then earnestly describes as fog.

Verified with a generated clip standing in for a camera: a frame is captured as real
JPEG, the cache returns the second request in 0.0 ms instead of waking the stream
again, the vision model correctly reported "a test pattern for television or video
equipment", and a dead host raises an error with no password in it.

### Still open in Phase 4

**Frigate**, §5's choice for the NVR half: continuous recording, motion and object
detection, event history and retention. Deliberately not built blind, because it needs
three things that cannot be guessed:

1. **The camera inventory** — makes, models, and whether they offer a substream. The
   original **ASK USER** in §8 stands.
2. **A hardware acceleration decision.** The 3060 Ti is already shared between the
   language model, Whisper and the vision model. Frigate doing continuous detection on
   it changes the VRAM budget that every earlier phase was tuned around.
3. **Retention and storage**, which interacts with §26's backups and the media volume.


## 31. Phase 6 — Transit and Places

The two integrations in §5 that need no credentials, so they work on a fresh install:
`transport.opendata.ch` and OpenStreetMap Nominatim. Three tools: `transit` (a
journey), `departures` (the board at the platform) and `find_place`.

**"Home" and "work" are settings, not an ASK USER.** §8 listed home and work
addresses as a question for Santiago. They are two text fields in the UI instead,
which is where §3.1 says configuration belongs, and `resolve()` maps the words a
person actually uses — "home", "work", "the office", and the German forms — onto
them. Unset, they fall through to the literal word rather than to an empty query,
which the transit API cheerfully answers with every station in the country.

**Durations are rewritten for the ear.** The API returns `00d00:19:00`. Since these
answers get read aloud (§17), that becomes "19 min" and "1h 05".

**Nominatim's terms are implemented, not hoped for**: an identifying User-Agent and
at most one request a second, serialised behind a lock so concurrent tool calls
cannot breach it.

Verified live against both services: a departure board for Winterthur, four
connections to Zurich HB with platforms and change counts, two pharmacies from the
map, and an unknown stop answered with "Check the stop name" rather than an empty
list.

### Remaining Phase 6 integrations

Email (Microsoft Graph, Gmail), calendar, and WhatsApp via Baileys all need
credentials and, for WhatsApp, a secondary number. Those stay blocked on Santiago.

## 32. Wake Word Training: What Upstream Got Stale

Four failures between a working image and a running trainer, all of them upstream
drift rather than anything specific to IRiS. Recorded because the next person to
touch this will hit exactly the same wall.

1. **`datasets==2.14.6` calls `pa.PyExtensionType`**, removed in pyarrow 15. Pinned
   pyarrow to 14.0.2 in its own layer so the large install above it stays cached.
2. **The AudioSet tar in the notebook is gone.** That repository is parquet now, and
   the pinned `datasets` cannot stream the new layout either. Replaced with ESC-50, a
   direct ungated zip of the same kind of audio, extracted with stdlib `zipfile`
   because adding `unzip` would invalidate the torch layer.
3. **`from generate_samples import generate_samples` fails against rhasspy's repo**,
   which has moved that module into a package. openWakeWord's own config comment
   points at dscripka's fork, which keeps the flat layout; the notebook's clone
   command no longer satisfies the notebook's own trainer.
4. **The fork wants `en-us-libritts-high.pt`** from rhasspy's v1.0.0 release, matching
   the `.json` config it ships. The v2.0.0 "medium" weights the newer notebook
   downloads describe a different architecture. And torch 2.6 flipped `torch.load` to
   `weights_only=True`, which cannot load a pickled model object, so the generator is
   patched at build time.

Every corpus fetch is now individually non-fatal: one unavailable dataset was
throwing away a 17.3 GB download that had already succeeded.


## 33. Devices, Integrations and Quick Commands

Santiago, verbatim:

> Cameras should be able to be dynamically added under a new tab called devices where
> you click add and select the type of device (to support microphones, cameras, later
> other devices etc.)

> there should also be a new tab integrations where you can similar to the devices tab
> add a integration of some type like whatsapp email etc. to add credentials and
> whatever is required for the type etc.

> for things like transit maybe add buttons to the UI like next to the attach thing a
> button for quick commands where you can select it, type a text and the model makes
> the best decisions it can based on the quick command selected etc. just basically
> make everything dynamic and more expandable for more featuers down the road

**One registry, two kinds.** `api/registry.py` stores typed instances in a single
`things` table keyed by `(kind, name)`. A type declares its fields; the API validates
against that declaration and the UI *renders the form from it*. `make_router(kind)`
produces the whole REST surface, so devices and integrations are the same code with a
different word. Adding a type is one `register(...)` call: no endpoint, no form, no
list rendering, no audit wiring. This is deliberately the same bargain
`settings.setting(...)` already makes for single values.

Types today: **camera** and **microphone** (devices), **mailbox** and **webhook**
(integrations). The camera moved off its own table; existing rows are migrated in
`init()` rather than orphaned.

**Secrets.** A field marked `secret` is returned as `••••••••` and never otherwise.
Sending the mask back means *unchanged*, so editing a mailbox's host does not require
retyping its password — taking the dots literally would silently replace every
credential with bullet characters, which is the bug this design exists to prevent, and
it has a test. ponytail: stored as given in Postgres, on a gitignored volume whose
backups are AES encrypted; a key-wrapped column is the upgrade if this ever holds more
than a home camera password.

**Mailbox is real, not a placeholder.** IMAP is in the standard library, so it needs
no dependency and works today with an app password. Headers only: the body of every
message is a lot of text to put in front of a model that was asked "any new mail".
Microsoft Graph and Gmail's own APIs (§5) buy push and richer search at the cost of an
OAuth app registration each, which is a decision, not work.

**Microphone reuses what already existed.** ffmpeg is here for cameras and pulls audio
identically; the transcript goes through the ingestion path recordings already take
(§29). A device type that did nothing would have been dead weight, so it does the
thing the pipeline already supports.

**Quick commands** are a directive prepended to the turn — enough to aim an 8B model
at the right tool without taking the decision away from it. Applied to the copy the
model sees, never to the stored transcript, so the conversation reads as what was
actually typed. Commands whose feature is off are not offered at all, since steering
at a disabled tool produces an apology rather than an answer.

Verified against the live server exactly as the browser drives it: both type lists,
creation with a secret that does not appear in the response, an edit that keeps the
password while changing the host, `400` for an unknown type, `400` for a missing
required field, `409` for a duplicate name, and the command list shrinking when a
feature is disabled.


## 34. Phase 7 — The Proactive Engine

The first shape of IRiS speaking first, and the one that pays for itself daily.

**Facts from tools, wording from the model.** `gather()` calls the transit and mail
tools directly and returns plain notes; the model is then asked only to phrase them,
under a prompt that forbids adding anything not in the notes. The tempting design is
to hand the model the tools and say "brief me", and it is the wrong one: an 8B model
sometimes decides it already knows, and a briefing that quietly invents your morning
is worse than none. If the wording step fails, the notes are delivered raw.

**Delivered where a person looks.** A new conversation in the chat, as if IRiS had
messaged first, plus every configured webhook (§33). It reuses `chat._save`, so a
briefing is an ordinary conversation: searchable, speakable, and subject to the same
retention as any other.

**Quiet hours wrap midnight**, because that is the normal case. A plain
`start <= now < end` is wrong for 22:00–07:00 and would have IRiS talking at three in
the morning; tested at both kinds of window, and at equal bounds meaning "no quiet
hours" rather than "permanently silent".

**Stateless scheduling** again: it tracks the date already briefed rather than a
timer, so a restart cannot double-brief, and a machine that was off at briefing time
skips the day instead of delivering breakfast news at lunchtime.

**Off by default.** Everything else in IRiS answers when spoken to. Something that
starts a conversation should be switched on deliberately.

Verified: quiet hours across midnight; the commute pulled live from the timetable;
and a briefing with nothing configured saying "Nothing else to report" rather than
padding.

### Still open in Phase 7

Event-driven triggers — a camera seeing someone, mail from a particular sender,
something in memory falling due — need a rules UI, which is the natural next use of
the registry in §33: a `rule` kind with a trigger type and an action type.


## 35. Tools Announce Themselves

Santiago, verbatim:

> If its doing something like generating the daily report, doing something like
> getting travel routes, checking google maps for things like cafes nearby etc.
> basically everything it does proactively and or by yknow just generally doing it
> there should be some indicator, like with the web search. all of these things are
> 'tools' and all of them should show some message of what its doing like the web
> search does now, they shouldnt be separate features but more like dynamic tools
> that can be added to easily yknow

He is right, and the old code proved it: `tool_start` was already emitted for every
tool, but the client special-cased `web_search` and rendered everything else as
"Running transit". A tool the UI has to be taught about separately is not a tool you
can just add.

**Presentation moved into the declaration.** `TOOLS` now holds a `Tool` dataclass
carrying `activity` and `display` alongside the schema and the function:

    @tool("transit", "...", {...},
          activity="Checking the timetable, {origin} to {destination}",
          display="lines")

`activity` is formatted with the call's own arguments and travels on both the
`tool_start` and `tool` events, so the chat says what IRiS is actually doing. A
missing argument leaves a gap rather than raising mid-reply, and an unregistered tool
still gets a sane line. `display` chooses the result rendering: `sources` (titles with
links, as the web search always had), `lines`, or `text`.

**The client knows no tool names.** One `toolActivity()` and one `toolResult()` render
whatever arrives. The label and display mode are stored on the tool message too, so
reopening an old conversation shows the same line instead of a bare name; messages
from before this change fall back gracefully.

`_MODEL_KEYS` already filtered unknown keys before messages reach Ollama, so the extra
fields never confuse the model.

**Proof that it worked:** adding the weather afterwards was one decorator and one
quick-command entry. No endpoint, no client change, and it appeared in the chat
announcing "Checking the weather", in the quick-command menu, and in the daily
briefing.


## 36. Calendar and Push

Santiago picked CalDAV and asked for suggestions that fit. Built: **Calendar
(CalDAV)** and **Push to phone (ntfy)**, both credential-light and both real.

**CalDAV without a library.** It is one `REPORT` request with a time-range filter and
a little XML. Every CalDAV client library pulls in a dependency tree larger than the
feature, and this is httpx plus a regex. Works against Outlook, Google and Nextcloud
with a username and an app password, which is the whole reason it beat Graph and the
Gmail API: no OAuth app registration, so nothing is blocked on Santiago.

Two iCalendar details that are easy to get wrong and are pinned by tests:

- **Lines are folded at 75 characters**, continued with a leading space. Parsing
  before unfolding truncates exactly the long summaries worth reading.
- **An all-day event carries a DATE, not a DATETIME.** Rendering it naively puts every
  birthday at midnight, so it reads "all day" instead.

**ntfy** puts IRiS on a phone with no account, no app store dependency and no push
certificate: install the app, subscribe to an unguessable topic, done. The topic is
stored as a secret field because on a public server it *is* the credential. It also
joins `notify()`, so the daily briefing now reaches the phone as well as the chat.

### Suggested next, still unbuilt

- **Home Assistant** — one long-lived token, and it makes "turn the heating down" real.
- **MQTT** — mosquitto has been running since Phase 0 and is still unused; this is the
  cheapest route to anything else in the house.
- **RSS** — a feed or two, folded into the briefing.


## 37. Frigate, on the CPU

Santiago chose CPU detection, so the GPU budget every earlier phase was tuned around
is untouched: the 3060 Ti still holds the language model, Whisper and the vision model
and nothing else.

**The config is generated, not written.** Frigate is config-file driven, which sits
badly with §3.4's "configure in the UI". `build_config()` renders it from the cameras
already in the device registry (§33), so a camera is added once, in one place, and
Frigate follows. The file carries a header saying edits are overwritten.

**It runs under a compose profile.** Frigate refuses to start with an empty camera
list, so a plain `docker compose up` must not try. `setup.sh` starts it only when a
generated config exists, and adding the first camera needs one restart — the single
part of this that cannot happen from the browser, because Frigate reads cameras at
startup.

**Resolution is probed, not guessed.** `ffprobe` asks the stream, because a wrong
`detect: width/height` either crops the frame or wastes CPU rescaling it. An offline
camera falls back to 1280x720 rather than refusing to write a config — and that
fallback path had a real bug found by testing it: `proc.kill()` raises
`ProcessLookupError` when ffprobe has already exited, which is the *common* case for
an unreachable camera, not the rare one.

**Two things a generated config must not get wrong**, both tested: camera names become
identifier-safe keys ("Garden (side)" is not a YAML key, and an empty key makes the
whole file unparseable), and stream URLs are always quoted with their quotes escaped,
since a camera password is full of punctuation and this is user input going into a
config file.

### Still open in Phase 4

Frigate publishes events to MQTT, and mosquitto has been running unused since Phase 0.
Subscribing to `frigate/events` would let a detection become a memory, a push, or a
proactive message — which is the natural meeting point of §33, §34 and this section.


## 38. Location, Greetings and News

Three things Santiago caught in one screenshot and two messages, all of them the same
underlying mistake: leaving to the model what could be decided in code.

**"Good morning" at 22:18.** The prompt said "greet them" and the correct time was
sitting in the notes directly above. The model said good morning anyway. `greeting()`
now works it out and the notes carry the exact words to use. Under five in the morning
it says "You're up late.", which is more useful than any of the three.

**"The weather in Switzerland".** With Home unset, the forecast fell back to
`location.region` and geocoded the country, putting the forecast in a field somewhere.
The browser knows where it is and the server does not, so *"use my location"* next to
Home posts a position, which is reverse-geocoded for a name and stored as ordinary
settings. Weather now prefers the fix over any name. `0, 0` is treated as unset,
because that is the Atlantic and it is also what an unset number setting defaults to.

**"When is the next bus to Uster."** That question *has* an origin, it just is not
spoken, and defaulting it to Home is wrong exactly when it matters: away from home.
`nearest_stop()` asks the timetable for the closest stop to the stored fix, and both
`transit` and `departures` take it when no origin is given. The tool schemas say so
explicitly, because a model handed an optional argument fills it in anyway. Naming a
place still wins over the fix, and Home still wins for the word "home".

**News, with its sources.** The briefing searches the news category and attaches the
results to the conversation as tool messages, so a briefing carries the same collapsed,
linked source list a web search in chat does. Two bugs found by looking at the output
rather than trusting it:

1. **A 2015 Charlie Hebdo story arrived as today's news.** SearXNG needs
   `time_range=day`, and results without a `publishedDate` are dropped outright: news
   that cannot be dated cannot be shown as today's.
2. **The headlines buried everything else.** Fed the full block, URLs and snippets and
   all, the model stopped mentioning the weather and the train entirely. The notes now
   carry titles only; the links stay in the attached sources where they belong. This is
   the same lesson as §34: give the model less to weigh, not firmer instructions.


## 41. Memory Was Learning the Wrong Things

Santiago's memory list, after a day of use, held: last night's departure times, three
facts about a company IRiS had looked up, and two preferences he never expressed
("the user prefers Cafe Oase as the only option near Gusch", from a single question
about coffee). His diagnosis was right and the cause was mine.

**The assistant was its own witness.** §27 requires every fact to carry a quote, and
verifies the quote against the exchange. The exchange *includes IRiS's own reply*, so
anything it had just said counted as evidence. The quote must come from the USER's
words alone, and the assistant's turn is now context rather than a source. That alone
removes the company facts and the timetables.

**Volatile facts are refused outright.** A store holding last night's timetable is
worse than an empty one, because it is recalled with confidence. Anything containing a
clock time, a departure, "the next available", "currently", a price or opening hours
is rejected by pattern, not by asking the model nicely.

**Asking about something is not preferring it.** A fact claiming a preference now
requires the user to have expressed one: "prefer", "always", "usually", "I like",
"can't stand". One question about a cafe became a standing preference that would have
resurfaced in another town.

Tested at all three, plus the case that must still get through: "I always take the
train rather than drive" is a stated preference and is kept.

## 42. Transit, Times and Places

Symptoms from real use, all of them the same root: the model filling gaps the tools
should have filled.

**"From Oetwil am See train stop, take the direct train to Gusch."** No such stop, no
such train. The persona now forbids inventing a stop, station or place name and
requires `where_am_i` before anything that depends on location, naming that exact
failure. A remembered location is a starting point for checking, never a substitute.

**Four departures when two were wanted.** "When is the next bus" wants the next bus.
Two by default, four when a time is named, because naming a time means planning.

**No arrival time.** The tool returned "20:09 to 20:28" and the model relayed only the
departure. It now says "departs 20:09, arrives 20:28".

**"Zero seven twenty three."** Times are rewritten for the ear before synthesis:
"seven twenty three AM", "two o'clock PM", "eleven oh five PM". Version numbers are
left alone.

**"How do I get there" now means public transport**, via a `route_to` tool that finds
the place, gives its address and straight-line distance, and plans the journey from
the nearest stop in one step. When the map does not know a small company, it plans to
the name anyway rather than refusing: the timetable knows towns the map does not.

Every route carries a Google Maps link so the answer can be checked.

## 43. Files Without an Arbitrary Limit

The cap is now the disk, not a number: an upload is refused only if it would come
within 5 GB of filling the volume the databases live on. Uploading shows the file and
its size while it transfers, then says it is being read, because a two-minute video
takes long enough that silence reads as failure.


## 44. The Regression: Too Many Tools

Santiago: *"it seems a lot worse in responses right now... it doesent use the right
tools doesent response right etc. what happened"*. Two symptoms, one cause.

- Asked "what is SIDMAR AG and how do I get there", it searched and then replied "It
  seems you've provided information about SIDMAR AG. Could you clarify what you'd like
  assistance with?" It had lost track of who said what and read its own tool result as
  the user's message.
- Asked "what's the weather like here" straight afterwards, it passed
  `place="Mönchaltorf"` from the previous exchange instead of omitting it.

**Measured before guessing: 18 tools, 7,492 characters of tool schema and an 8,335
character persona. Nearly 16,000 characters, about 4,000 tokens, before he said a
word.** Both symptoms are what an 8B model does under that load: it fills optional
arguments from whatever is nearby and stops tracking roles.

The cause was accretion, mine. Every request added a tool or a rule and nothing was
ever removed.

**Cut 41%, to 9,362 characters:**

- `analyze_image`, `analyze_document` and `analyze_video` called one identical
  function that dispatches on file extension. They are one `analyze_file`.
- `list_uploads` went: the not-found message already lists what is available.
- The persona lost its accreted duplication. The em-dash and emoji sections shrank to
  one line each, because both are enforced in code (§22) and the prompt was arguing a
  case already won.

**Precision was then put back.** Santiago: *"The tool schemas should not be less
precise, same functionality but maybe done more efficiently"*. Right, and the first
pass had cut the trigger phrases that actually route an 8B model, not just padding.
"who is at a door, whether a parcel arrived", "whether someone replied", "whether they
are free" all came back; what stayed cut was elaboration the persona already covered.
Terse is not the same as vague.

Verified afterwards, all four correct: the SIDMAR question now searches and calls
`route_to`; "the weather here" omits the place; "what are you doing" calls
`system_status` and reports its numbers; "next bus to uster" finds the nearest stop.

**One trap found while verifying.** The persona's worked example for `system_status`
contained plausible numbers, and the model reproduced them verbatim. An example that
can be parroted as fact is worse than none, so it now shows the shape with the figures
left as placeholders.


## 45. Links Belong Beside the Answer, Not Inside It

Every route and every place carries a link, and the model kept pasting 140-character
maps URLs into the middle of sentences. The persona was told not to, twice, and it
did anyway. So it is stripped in code, like the em-dashes (§18) and the emoji (§22):
a markdown link collapses to its text and a bare URL is removed.

**Stripping a stream is not stripping a string.** A URL arrives across several
tokens, so applying the filter to each delta alone emits half a link and strips the
other half. `split_for_links()` holds back a trailing fragment that could still become
one: a half-typed `[text`, a half-typed `[text](url`, or a bare `https://par`. It
releases the fragment once it resolves, and after 300 characters regardless, so an
ordinary bracket in prose never freezes the reply.

**The links did not disappear, they moved.** The tool banner beside the answer carries
them, and banners now render markdown and bare links as links rather than as text, so
a map or a website is one click away from the result that mentioned it. `find_place`
attaches a map link to each result for the same reason.

Also fixed: markdown links were never rendered anywhere in the chat, so every link
IRiS had ever produced was dead text. And `syncHandsFree()` was called by both the
click handler and the live settings feed, so the microphone was opened, and failed,
twice, which is why the no-microphone notice appeared twice.


## 46. Voice Broke Because Memory Took the VRAM

"transcription failed: CUDA failed with error out of memory", and with it the wake
word, which wakes and records and then cannot transcribe.

Measured: **7,303 MiB of 8,192 in use, 890 free.** Whisper large-v3 needs around 1.5
GB. The cause is partly my own: automatic recall (§25) embeds every turn, and bge-m3
then sat in VRAM indefinitely for the sake of a few milliseconds' work.

Two fixes:

- **Whisper falls back to the CPU on an out-of-memory error** rather than failing the
  recording, and stays there for ten minutes so every following request does not
  repeat the same failed attempt. Slower, and it works, which beats fast and broken.
  The device used is reported back.
- **The embedder is asked for a 60-second keep-alive.** It is needed for milliseconds
  a few times a turn; holding VRAM between turns only starved the thing that needed
  it.

Verified with the GPU full: a synthesised sentence transcribed verbatim on CPU, and
the whole hands-free loop still runs, "Hey iris." through to a transcript.

**A stale override was also found**: `voice.wake_sensitivity` was still 0.5 from
before the default moved to 0.85, and at 0.5 the trained model fires on "the iris"
(§32 measured it at 0.702). Corrected.

## 47. A Bus Is Not a Train

`transport.opendata.ch` returns a category code, "B" or "S" or "IC", which was
discarded. IRiS said "take the direct train" for bus 842. Each connection now names
what you actually board, leg by leg: "bus 842, then S-Bahn 5, then InterRegio 70".

It also shortened "Oetwil am See, Gusch" to "Oetwil am See", which is a different
place and not the one he is standing in. The persona now requires the stop name
exactly as the tool gave it.

**And links came back.** §45 stripped titled links along with bare URLs, which left
"Route: Google Maps" as dead text: worse than either. Only a bare URL is noise now; a
markdown link is short, useful, and renders as a link.


## 48. HTTPS, and a Backdrop

Santiago: *"Can we make it have a self signed certificate and run on https on port
8000 instead of http please?"*

`setup.sh` generates a self-signed certificate into `./data/tls/` and the API serves
HTTPS when one is mounted, plain HTTP when it is not, so an install without a
certificate still starts. The certificate names `localhost`, the hostname, every LAN
address and the Tailscale address: with the name wrong the browser complains about the
name instead of the signature, and that is the harder warning to get past. Ten years,
regenerated only when missing or nearly expired, so "trust this" is answered once.

`IRIS_COOKIE_SECURE` now defaults to 1, and the port binds to all interfaces rather
than loopback, because a phone cannot reach `127.0.0.1`.

This also settles the microphone caveat carried since §2: the microphone, hands-free
listening and geolocation all require a secure context, and now there is one.

**On exposing it: yes, and two warnings.** Forwarding 443 to 8000 works. But a
self-signed certificate warns in every browser and phones make that harder to click
past, so with a name he already owns a Let's Encrypt certificate is strictly better.
And once it faces the internet the login is the only thing between the world and
every conversation, memory and credential in the system, so the seeded password has
to go first. Tailscale avoids both.

**The backdrop.** A field of points drifting slowly, joined by lines when they pass
near each other, a few in the accent colour. Density follows the viewport area, so a
phone draws a handful and a monitor draws a field. It stops when the tab is hidden and
draws a single static frame when the machine asks for reduced motion. The engineering
grid stays underneath, fainter.

**Width.** The column was pinned at 880px, which left a wide monitor two thirds empty.
It now grows to 1080 and then 1280 as the screen does, and stops there because longer
lines are harder to read, not easier. Below 720px the tabs scroll instead of wrapping,
the composer puts the text box on its own row, and cards go single-column; below
420px the wordmark and the quieter HUD fields step aside for the chat.


## 49. Arriving, Today, and Times That Are Sometimes Read

Three from one session of real use.

**"What is my route to be there 08:30 tomorrow"** was answered with the next bus at
04:56 and a suggestion to adjust his schedule. `route_to` took no time at all, and
`transit`'s `when` meant *departure*. Both now take a time and an `arrive_by` flag,
and the timetable is asked with `isArrivalTime`, which is a different question and
has a different answer. `parse_when` takes it in one argument in his own words,
because a model given three optional arguments fills all three: "tomorrow 08:30",
"08:30", "2026-08-06 09:00", "next monday 07:00". Verified: four buses that all land
before half past eight.

**"What do I have in my calendar today"** listed tomorrow as well. `days=1` was a
rolling 24 hours, so asked late at night it covered most of the next day. It now means
whole calendar days from midnight, and each event is labelled "today", "tomorrow" or
its date, so a wider window can never read as one day.

**Times were read as times "sometimes".** Only with a colon. The model writes `08.30`
and `8h30` as well, and both fell straight through to the synthesiser as digits. Now
all three separators are read, and a dot counts only where it cannot be a decimal:
`08.30` and `16.00` are times, `8.50` is a price and `1.2` is a version. A written
meridiem is also consumed rather than repeated, so "8:30 AM" stopped becoming "eight
thirty A M A M", and "8:30 pm" is no longer half past eight in the morning.


## 50. The Last Few Hundred Metres

**"How to get there" went to the town, not the address.** SIDMAR AG is 61 m from
Mönchaltorf, Wihalde, and IRiS planned to Mönchaltorf, which is the village. `route_to`
now geocodes the destination, finds the stop nearest *those coordinates*, plans to that
stop, and says how far the walk is. The difference is arriving at the door rather than
in the village.

Two lookup failures were behind it. The map does not know "SIDMAR AG, Esslingerstrasse
32, Mönchaltorf" because it holds addresses, not company names; dropping the leading
component finds it immediately. And appending the user's home town to a full address,
which the region-narrowing did unconditionally, made it match nothing at all. Both are
now a cascade: as asked, then simplified, then narrowed.

**The voice read URLs aloud.** Markdown links were already reduced to their text, but a
bare address was read out character by character. Stripped before synthesis.

**A missed briefing is caught up.** The scheduler tracked the day in memory, which got
both cases wrong at once: a restart re-briefed a day already done, and a machine that
was off at seven had no way to tell a missed morning from a fresh one. Recorded in
Redis instead, a restart is silent and a machine coming back at nine briefs
immediately.

### Install and uninstall, audited

Every mounted volume is in `DATA_DIRS`, every model Ollama serves is pulled, the
certificate is generated on install, and Frigate starts under its profile when a
camera exists. Two gaps found and closed on the uninstall side: `down` was not passing
`--profile cameras`, so a running Frigate would have been left behind, and the wake
word training image is built outside compose so `--rmi all` never saw it.


## 51. Location Every Time, and Not on Another Continent

Santiago: *"i want it to take location EVERY TIME its relevant so its always up to
date and not a guess"*, after a route started from the wrong stop and the destination
link pointed at Maryland.

**"SIDMAR AG" resolved to Sidmar, Frederick County, Maryland.** The lookup cascade
ended with an unbounded global attempt, so a company name the map does not hold
matched the first thing anywhere on earth that looked like it. Searches are now
restricted to a box around the user's position and the global fallback is gone
entirely: nothing local means nothing local, and saying so beats routing to another
continent.

**The route was right and looked wrong.** The timetable had resolved "SIDMAR AG" to
"Mönchaltorf, Esslingerstr. 32" correctly, but the output was labelled with the input
string, so a correct answer read as a guess. Both ends now report the names the
timetable actually resolved, which also makes a genuinely wrong match visible instead
of silent.

**The fix is taken fresh.** The browser volunteers a position before every message
with no cached value accepted at all, and the wait is up to nine seconds rather than
two and a half, because a real GPS fix takes a moment and a cached one is exactly what
makes the nearest stop wrong. Where the position is old or coarse, the tools say so
rather than presenting it as certain, and where no preferred stop is set they name the
stop they chose and why.

The honest limit: a desktop without GPS positions itself by wifi and will drift by a
village. The preferred stop setting exists for that, and no amount of asking the
browser more often fixes it.


## 52. Uploads, and Stopping Things

**A progress bar, because there is progress to show.** `fetch` cannot report upload
progress at all, so a minute-long upload looked identical to a stalled one: three dots
either way. `upload()` uses XHR for that one reason. The bar covers the bytes going
up, which have a length; once they are sent it becomes dots again, because the model
reading them does not.

**A cross to cancel**, wired to an `AbortController`. A cancellation rejects with an
`AbortError` so callers can tell a decision from a failure and stay quiet about it.

**Send becomes Stop while a reply is being written**, aborting the stream and the
speech with it, and is blocked while a file is still uploading. One button, three
states, each saying which it is in.

**Leaving a conversation abandons what belonged to it.** `abandonTurn()` aborts the
reply being written, stops the audio reading it out, cancels uploads in flight and
clears the attachments. Called when switching conversations, starting a new one, and
deleting the one currently being written to, where previously the reply carried on
being generated and read aloud into a conversation that no longer existed.


## 53. Reading a File Is Part of the Answer

Santiago: *"instead of transcribing before sending it should transcribe after sending
... because for a video file that takes a while and i just want to upload, send and
have it transcribe itself"*.

Right, and it also fixes a smaller wrong: reading a file was the one thing IRiS did
without saying so, because it happened before the conversation started.

`POST /files/upload` now stores the file and reports only its name, kind and size.
The message carries that reference, and `for_model` tells the model in as many words
that the attachment **has not been read** and which tool reads it. The existing
`analyze_file` does the work, so the reading appears as "Reading briefing.mp4" beside
the reply, like every other action.

Verified end to end: a video uploaded in milliseconds, the banner appeared during the
reply, and the answer came from its transcribed audio.

Conversations from before this still carry their extracted text, and `for_model`
passes that through unchanged rather than asking the model to re-read something it
already has.

**Several at once, and from the clipboard.** The picker takes multiple files, and a
paste anywhere on the chat tab attaches whatever the clipboard holds, which is how a
screenshot actually arrives. The paste is only intercepted when the clipboard carries
files, so pasting text still pastes text.

## 54. A Briefing That Says What Happened, and a Video That Is Listened To

Santiago: *"the briefing is incredibly bland, has no news from switzerland and does
not explain what the headlines actually mean / doesent expand on it, i want a
briefing that yknow actually tells me whats happening not just headlines and not even
swiss ones in the mix, the briefing should also include the weather at my set home
location in the settings. video transcription isnt great, it just analyzes the
background etc. but not whats actualy being said in the video which is kind of the
point of 99% of video media id be uploading."*

Three separate faults, one symptom each.

**No Swiss news.** The regional search was built from `location.home`, so it asked
for "Oetwil am See news". A village of 4,000 has no news wire, the search returned
nothing, and the whole section was dropped silently. It now searches
`location.region`. Compounding it, a one-day `time_range` starved the engines before
the age filter ever ran: "Switzerland news" over a day returned four results, none of
them dated. The window is now a week and `max_age_days` is what actually decides
freshness, two phrasings per section are merged and deduped by URL, and results are
sorted newest first rather than by relevance, because in relevance order the freshest
story is the one the limit cuts off.

**Bland.** Only the *titles* went into the notes. The model could not expand on what
it was never given, so it read headlines back. Titles now carry the story's first
line, and the prompt asks for what happened and why it matters.

**Missing weather.** The weather was gathered correctly every time and the model
simply dropped it, along with anything else it felt like, on the way to the news.
The prompt now requires every section present in the notes. Written loosely
("cover every section: weather, appointments, mail, the journey") it went the other
way and invented an appointment with the local council and a visa interview, so the
rule is stated in both directions: a section in the notes must not be dropped, and a
subject absent from the notes must not be mentioned at all.

**The video.** `[files] video audio failed: 502: stt unreachable:` — with nothing
after the colon, because `str()` of an httpx timeout is the empty string. A 435
second video was extracted whole and sent as one request against a 300 second
timeout, and whisper had fallen back to the CPU because the language model held the
GPU. The transcription never returned, the exception was swallowed, and the answer
was written from six frames of scenery. Hence "it just analyzes the background".

Audio is now transcribed in five-minute chunks with a timeout scaled to each chunk,
so a chunk that fails costs its own minutes rather than the whole transcription, and
the STT service returns timestamped segments so the transcript reads as a timeline.
The transcript is printed **first** and labelled as the content of the video, with
the frame descriptions after it and labelled as background: six sentences of scene
description ahead of the speech were enough on their own to make the model answer
about the wallpaper. When there is no transcript the reason is stated in the text the
model reads, rather than logged where only I would see it.

Verified on the file that failed: 66 timestamped segments across the full seven
minutes, where before there were none.

## 55. Customize: Briefings Built From Widgets, and the Tool Library Made Visible

Santiago: *"7 with it being a new tab called customize where we can add more stuff
later, in this customize tab there should be a drag and drop modal where one can add
briefing 'widgets'. Like a widget called weather where in the widget i can set weather
where (like say give me the weather at my home adress, at work or some other custom
location), a widget for news like Global News Swiss news and a way to chose preferred
news provider for swiss news with some of the most well known sources (SRF, 20minuten,
blick etc.) ... theres a menu point called 'Briefings' that when clicked brings up a
list of Briefings with names, with a default briefing and a way to add and name more
briefings with different widgets and settings and i can set the standard briefing it
serves for the briefing task or when i ask just for a briefing or if i ask for a
specific briefing it gives that. also a way to tell it how many sentences to write
widget-specific with sensible default values."*

And later: *"add tools to customization so i can see the instructions ... with the user
being able to select what tools can be used and as we designed it before its a tool
library that the model knows about and pulls from where needed"*, then: *"the user can
select one and see what the tool actually does like what instructions are in it and if
wanted change them (though it should be noted when changes are made when trying to save
a modal pops up that this might break the tool and also a way to reset the individual
tools to their default values just to make sure users cant compeltely break stuff with
no way back"*.

Answers to the questions asked before starting: **SpeechBrain**, not pyannote.
**Each briefing has its own schedule**, with "Default Briefing" at 07:00 daily and
recurrence covering weekdays, certain days, once a month and never. **Warm chime.**
**Quick commands and the persona move into Customize.**

### A briefing is a list of widgets

`api/briefings.py`. A briefing is a name, a schedule, some options, and an ordered list
of widgets, each with its own settings and its own sentence budget. Ten widgets ship:
greeting, weather, news, calendar, mail, commute, this machine, timers and alarms, what
the cameras saw, and a standing note. A widget is one `register(Widget(...))` call
reusing `registry.Field`, so the client renders its form with no client work, the same
bargain devices and integrations already make.

The weather widget takes Home, Work, where you are standing, or a place you name. The
news widget takes World, your region, or a topic, and a list of preferred outlets from
SRF, 20 Minuten, Blick, NZZ, Tages-Anzeiger, Watson, SwissInfo, RTS, Le Temps, Luzerner
Zeitung, Reuters, BBC, AP, the Guardian, Al Jazeera, DW, France 24, NYT, the FT and
Bloomberg. Chosen outlets are searched with `site:` **as well as** an unrestricted
search, and the two are merged: restricting to an outlet that published nothing today
must not empty the section, which is exactly the failure that made the Swiss half
vanish in the first place.

### The prompt is built section by section

This is the load-bearing part. §54 fixed the flat wall of notes by asking for every
section; that made the model invent an appointment with the local council. Each section
now carries its own instruction and its own budget:

    SECTION 2 - Weather (write 2 sentences).
    Weather in Oetwil am See: ...

with the rule stated in both directions: a section that is here must not be dropped, a
subject that is not here must not be mentioned at all.

Widgets are gathered concurrently. Sequentially, a briefing was the sum of a weather
lookup, four searches, a CalDAV report and an IMAP login.

### Scheduling

One loop for every briefing, ticking each minute, firing on *past due* rather than *at*
so a machine asleep at 07:00 still briefs when it wakes. A period key
(`%Y%m%d`, ISO week, or `%Y%m`) held in Redis is what stops that becoming a briefing a
minute, and it is written **before** composing, because composing takes twenty seconds
and a crash halfway through must not deliver twice.

`proactive.py` keeps only delivery and quiet hours. The five `proactive.*` section
booleans are gone: widgets are the single source of truth, and two of them would drift.
An existing install's settings are read once when the Default Briefing is seeded, so
the briefing it had is the briefing it keeps.

### Automations

`api/rules.py`, the other half of Phase 7. Five triggers: mail matching a description
arrives, the weather turns, an appointment is close (optionally with the journey to
it), a camera saw something, this machine is unwell. Each check returns facts and a
**dedupe key**, because a trigger is a check rather than an event queue and without the
key it fires on every poll. Checks run concurrently with `return_exceptions=True`: a
mailbox that has stopped answering must not stop the weather rule. `POST /rules/{id}/test`
runs a check and reports what it would have said without delivering or consuming the key.

### The tool library

`api/toolkit.py`. Only overrides are stored, never a copy of the declaration, so a tool
whose wording improves in a later version is not frozen at whatever was stored the
first time the page was opened. The Tools section shows every tool grouped, with its
real instructions, its trigger words, and what each argument means, all editable.
Saving an edit opens a modal saying plainly that this can stop the tool being chosen or
make it be called with the wrong values; Reset is one click and needs no confirmation,
because making the undo hard would be backwards.

Argument *descriptions* are editable; types, names and required-ness are not. An
argument the model is told to fill but the function does not take is not a
customisation, it is a crash.

Tool selection now records **why** each tool was offered (`matched "rain"`, `closest
match for this message (0.71)`, `always available`), which travels to the banner and
into the stored transcript. When the routing misfires there was previously nothing to
look at.

### Everything else in this change

- **Attachments are read once.** A sidecar `<file>.iris.json` keyed on size and mtime
  holds the transcript, the scenery and the extracted text. A follow-up question about
  a seven minute video went from 13.8 seconds to 0.000.
- **Speaker labels.** SpeechBrain ECAPA embeddings clustered over Whisper's segments,
  on the CPU always. The threshold was **measured, not guessed**: on a seven minute
  recording of one person, 0.30 split him into fourteen speakers, 0.40 into five, 0.50
  into two and 0.60 into one. At 0.60 a genuine two-voice recording is still separated
  correctly and each speaker re-identified across turns. Three version pins were needed
  (torch, torchaudio, huggingface_hub) for breaks that only surface at the first
  transcription. Diarization failing never fails a transcription; the reason is
  reported. Speaker identity cannot survive chunking, so files under fifteen minutes go
  over in one request and longer ones say the numbering restarts per part.
- **Audio is a first-class upload.** `.mp3`, `.m4a`, `.wav`, `.ogg`, `.opus`, `.flac`
  and friends. `.webm`, `.ogg` and `.mkv` are genuinely ambiguous, so the stored file is
  probed with ffprobe rather than guessed from the extension.
- **A transcript beside the recording.** `GET /files/media/{name}` serves uploads with
  HTTP Range support, because without it a browser plays from the start and refuses to
  seek, which makes a clickable transcript useless.
- **Camera events over MQTT.** `api/events.py` subscribes to `frigate/events`, which
  had been idle since Phase 0. Built and verified with **no camera configured**: a
  synthetic event published to the broker flows through to "person at the front door at
  Wed 19:06, still going."
- **Timers and alarms.** In Postgres, so they survive a restart. The scheduler ticks
  every five seconds, because a twenty minute timer going off at twenty minutes fifty
  is a broken timer. `api/chime.py` synthesises the warm chime in pure Python: five
  inharmonic bell partials, higher ones decaying faster, three descending strikes. Its
  own self-check caught the first attempt stopping while the bell was still at a third
  of its amplitude, which is a sound cut off rather than one that decays.
- **Export, retry and edit.** `GET /chat/conversations/{id}/export` returns Markdown
  with the tool turns folded in, because half of what IRiS did in a conversation is
  what it looked up. Retry and edit are one server-side `rewind`, so the stored
  transcript and what the model sees cannot drift.
- **A stale position is said out loud**, with a refresh beside it. The threshold is a
  setting.

### What the browser check caught

The Customize tab rendered nothing. `showView` had the hook, but the tab strip has its
own click handler that duplicates `showView`'s body rather than calling it, so the hook
never ran. Worth recording because the duplication is still there and the next tab
added will hit it too.

## 56. Phase 8: Knowing What It Is, and Proposing What It Should Be

Two halves, and the second is deliberately smaller than it sounds.

### Self-inspection is generated, never written down

`api/introspect.py`. §17 records the failure this exists to prevent: asked what it was
doing, IRiS once said *"running at 72% GPU, parsing sensor data from the west wing"*.
There is no west wing. `system_status` fixed the measured half. This is the structural
half: what IRiS is made of.

Everything comes from the live registries — `reasoning.TOOLS`, `briefings.WIDGETS`,
`rules.TRIGGERS`, `registry.TYPES`, `settings.REGISTRY`. A hand-written architecture
summary is correct on the day it is written and quietly wrong a month later, which is
worse than having none because it still reads as authoritative. A tool registered
tomorrow appears in the answer tomorrow, with no edit here, and there is a test that
fails if that stops being true.

`describe(topic)` filters, because handing an 8B model everything it is made of and
asking about the memory system means it answers about the cameras.

The second tool is `what_did_i_do`, over the audit log. §3.2 always said the log was
how "why did you do that" would get a real answer in Phase 8; this is that. It reads a
record written when the action happened rather than reconstructing one afterwards, and
action names are turned into something a person would recognise, because `chat.message`
is a log line and not an answer.

### Self-modification is bounded to its own configuration, on purpose

`api/proposals.py`. IRiS can propose a change to itself. It cannot make one. A
proposal carries the current value, the suggested one and the reason, and sits in a
queue under Customize, Self until a person approves it. Anything approved can be undone
in one press, because the before-value is stored rather than reconstructed, and it is
**re-read at approval time**: something else may have changed it in between, and
reverting to a value that stopped being current an hour ago would be its own small
disaster.

What it may propose: a setting, its persona, a tool's description or trigger words, a
custom quick command. **Not code.**

That bound is a decision. The spec asks for human-approved diffs with git history and
rollback, and for configuration this delivers exactly that. Extending it to source
would mean an 8B model writing Python that runs as root on a box holding every
conversation, memory and credential, on a repo that is public and a port that may be
forwarded. "A human approves the diff" is a thin control when the diff is forty lines
of code at seven in the morning, and it is not a control this codebase has any way to
make thicker. If code-level self-modification is wanted it should be its own decision
with its own review, not a side effect of this one. **ASK USER if that is wanted.**

The queue is capped at 25 pending. IRiS refuses to add to a queue nobody has read, and
says so in those words.

### What testing caught

The first proposal failed on approval: `llm.tool_budget: '7' is not of type 'integer'`.
The tool takes `value` as a string on purpose, because an 8B model handles one argument
type far better than a union and will say `"7"` for a number every time; the settings
service validates against the registered JSON Schema and rightly refuses it. The
conversion is this side's job, and it happens at propose time so the queued diff shows
`7` rather than `"7"`. Worth recording that the failure mode was safe: validation
refused it rather than storing a string in an integer setting.

Also: a hash-only navigation does not reload the page, so a tab sitting on
`https://localhost:8000/` and sent to `#customize/self` keeps running the JavaScript it
already had. Not a bug in IRiS, but it wasted a debugging cycle and will waste another.

### Verification of the previous change

The three paths flagged as unverified in §55 were closed before this work started: an
automation created through the API fired through the scheduler, delivered a
conversation, and did **not** fire again on the next tick; a timer rang in the browser
with the modal and the chime; retry replaced the answer rather than appending, leaving
the stored conversation at one user turn and one assistant turn.

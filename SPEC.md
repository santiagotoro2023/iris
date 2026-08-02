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

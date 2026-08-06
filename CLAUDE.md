# IRiS

Full build spec, locked decisions, and phase breakdown: [SPEC.md](./SPEC.md). Read it before starting work each session.

Host: stzrhws01 (RTX 3060 Ti, 8GB VRAM). Data/media volumes: `./data/` (gitignored, bind-mounted).

**Standing rule:** any UX/design decision not explicitly resolved in SPEC.md — stop and ask, don't guess (Section 3.3). "ASK USER" callouts in SPEC.md are mandatory stops.

**Standing rule:** anything that adds a service, volume, model, system package or config file must update **both** paths of `setup.sh` (install and uninstall) plus README.md in the same change — stale install/uninstall components are a defect (Section 3.4). All user-facing configuration goes in the UI, not the CLI.

**Standing rule:** Santiago's requirements go into SPEC.md verbatim. My own notes stay in §9 (Decisions Log) — never edited into his sections.

## Phase status
- [x] Phase 0 — Foundation: docker-compose skeleton (Postgres/Qdrant/Redis/MQTT) up and verified, GPU passthrough verified, storage path decided (see SPEC.md §9)
- [x] Phase 1 — Core Reasoning Engine: Ollama + **`qwen3:8b`** (supersedes Qwen2.5-14B — no 14B fits 7.1 GiB VRAM; see SPEC.md §10), FastAPI `/infer` wrapper, tool-calling scaffold (`api/main.py`, `TOOLS` registry), thinking off by default with per-request `think` override
- [x] Phase 1B — Configuration System: `api/settings.py` registry + service (`/settings/schema|values|stream`), Postgres-backed, SSE live sync, schema-driven UI at `/` (`api/static/index.html`). Register new settings with `settings.setting(...)` — no client work needed
- [x] Phase 1C — Auth & User Management: `api/auth.py` — scrypt passwords, Redis sessions (cookie + bearer), seeded `creator`/`1234` with forced change, roles (creator/admin/user), API keys for Phase 6, login lockout. Login page at `/login.html`
- [x] Phase 2 — Voice I/O: **done**. `stt/` (faster-whisper large-v3) and `tts/` (**Piper** en_GB by default, XTTS selectable — see SPEC.md §17.1; XTTS OOMs beside the LLM). Streaming text (`/chat/stream`, NDJSON) and per-sentence speech pipelining; both models idle-unload to share the GPU (SPEC.md §15–16). Hands-free listening in `api/wake.py`: openWakeWord over a WebSocket, Silero endpointing, barge-in (SPEC.md §23). **Open: no public "IRiS" wake model exists — default is `hey_jarvis` until one is trained; ASK USER before spending the GPU time**
- [x] Phase 3 — Memory System: **embedding, storage, recall and encrypted backups done** (`api/memory.py` bge-m3 + Qdrant, automatic recall into the system turn, Memory tab; `api/backup.py` + `restore.sh`, AES-256 archives in `./backup/`, gitignored — SPEC.md §25–26). conversations are learned from automatically with a grounding filter (§27). nightly 30-day retention (§28), audio ingestion (§29). **Diarization done** — SpeechBrain ECAPA over Whisper's segments, CPU only, threshold measured not guessed (§55). Santiago chose SpeechBrain over the licence-gated pyannote
- [~] Phase 4 — Home Cameras: **on-demand looking done**, as one *device type* in the generic registry (`api/registry.py` + `cameras.py`/`devices.py` — camera and microphone; SPEC.md §30, §33). **Frigate on CPU done** (`api/frigate.py` — config generated from the registry, compose profile; §37). **MQTT events done** (`api/events.py` — subscribes to `frigate/events`, stores in Redis, feeds a tool, a briefing widget and a rule; built and verified with no camera configured, §55). Remaining: nothing until Santiago adds a camera
- [ ] Phase 5 — Shared Frontend & Client Apps
- [~] Phase 6 — Integrations & Tools: **web search, vision/document analysis, transit + places done** (`api/places.py` — transport.opendata.ch and OSM Nominatim, both credential-free; home/work are settings, closing that ASK USER — SPEC.md §31) (`searxng` + `web_search` tool; `api/files.py` with qwen2.5vl for images, pypdf/python-docx for documents). **Integrations are a registry too** (`api/integrations.py` — IMAP mailbox and webhook work today; §33). calendar (CalDAV) and push (ntfy) added, §36. WhatsApp/Graph still need credentials or an OAuth app
- [x] Phase 7 — Proactive Engine: **daily briefing done** (`api/proactive.py` — facts gathered from tools directly and only the wording left to the model, quiet hours that wrap midnight — SPEC.md §34). **Briefings are now named, scheduled and built from widgets** (`api/briefings.py`, ten widgets, per-widget sentence budgets, per-briefing recurrence; §55). **Event-driven rules done** (`api/rules.py` — five triggers, dedupe keys, `/rules/{id}/test`; §55)
- [~] Phase 8 — Personality & Self-Awareness: persona pulled forward (`api/persona.py`, editable under Customize > Persona). **The tool library is self-inspection of a kind** (`api/toolkit.py` — IRiS's own tool instructions visible and editable, §55). Self-modification not started
- [ ] Phase 9 — Branding

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
- [~] Phase 2 — Voice I/O: **STT done** (`stt/` service, faster-whisper large-v3, `/voice/transcribe`, push-to-talk in chat, idle VRAM release — see SPEC.md §15). **TTS blocked on the ASK USER voice-source decision**; wake word and turn-taking not started
- [ ] Phase 3 — Memory System
- [ ] Phase 4 — Home Cameras
- [ ] Phase 5 — Shared Frontend & Client Apps
- [ ] Phase 6 — Integrations & Tools
- [ ] Phase 7 — Proactive Engine
- [ ] Phase 8 — Personality & Self-Awareness
- [ ] Phase 9 — Branding

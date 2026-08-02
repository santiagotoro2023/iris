# IRiS

Full build spec, locked decisions, and phase breakdown: [SPEC.md](./SPEC.md). Read it before starting work each session.

Host: stzrhws01 (RTX 3060 Ti, 8GB VRAM). Data/media volumes: `./data/` (gitignored, bind-mounted).

**Standing rule:** any UX/design decision not explicitly resolved in SPEC.md — stop and ask, don't guess (Section 3.3). "ASK USER" callouts in SPEC.md are mandatory stops.

## Phase status
- [x] Phase 0 — Foundation: docker-compose skeleton (Postgres/Qdrant/Redis/MQTT), .gitignore, storage path decided (see SPEC.md §9)
- [ ] Phase 1 — Core Reasoning Engine
- [ ] Phase 1B — Configuration System
- [ ] Phase 1C — Auth & User Management
- [ ] Phase 2 — Voice I/O
- [ ] Phase 3 — Memory System
- [ ] Phase 4 — Home Cameras
- [ ] Phase 5 — Shared Frontend & Client Apps
- [ ] Phase 6 — Integrations & Tools
- [ ] Phase 7 — Proactive Engine
- [ ] Phase 8 — Personality & Self-Awareness
- [ ] Phase 9 — Branding

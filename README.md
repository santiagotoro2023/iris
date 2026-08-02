# IRiS — Integrated Reasoning, in Silico

Locally-run PDA-style assistant. Build spec and phase plan: [SPEC.md](./SPEC.md).

## Phase 0 — quick start

```
cp .env.example .env   # edit POSTGRES_PASSWORD
docker compose up -d
```

Brings up Postgres, Qdrant, Redis, MQTT. Data persists in `./data/` (gitignored).

GPU containers must request the CDI device explicitly — `--device nvidia.com/gpu=all`, not `--gpus all`. See [SPEC.md §9](./SPEC.md#9-phase-0-decisions-log).

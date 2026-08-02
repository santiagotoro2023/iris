# IRiS — Integrated Reasoning, in Silico

Locally-run PDA-style assistant. Build spec and phase plan: [SPEC.md](./SPEC.md).

## Install

```bash
git clone https://github.com/santiagotoro2023/iris
cd iris
./setup.sh
```

That is the whole CLI story. `setup.sh` installs the NVIDIA Container Toolkit if
missing, generates the CDI spec, adds you to the `docker` group, installs and
connects Tailscale, writes a `.env` with a random database password, starts every
service and pulls the model. It is idempotent — re-run it any time to update.

The only interactive prompt is Tailscale's one-time browser login, and it is
skipped entirely if the host is already on the tailnet.

Everything else is configured in the UI, never on the command line.

| | |
|---|---|
| Settings UI | http://localhost:8000/ |
| API health | http://localhost:8000/health |

## Uninstall

```bash
./setup.sh --uninstall           # stop and remove the stack, keep ./data
./setup.sh --uninstall --purge   # also delete ./data and .env (asks first)
```

`--uninstall` removes containers, images, networks and named volumes but leaves
`./data`, which holds all of IRiS's memory — Postgres, Qdrant, downloaded models
and media. `--purge` deletes that too, and requires typing `DELETE` to confirm.

Deliberately left alone by both: Docker itself, the NVIDIA Container Toolkit,
`/etc/cdi/nvidia.yaml` and Tailscale — other software on the host may depend on
them, and removing Tailscale could cut your remote access to the machine.

## Services

## Signing in

First run seeds a single account — **`creator` / `1234`** — which cannot do anything
except change its own password until it does. Everything else in the API returns
401 without a session and 403 while a password change is owed.

| Endpoint | |
|---|---|
| `POST /auth/login` | sets an httpOnly cookie and returns a bearer token |
| `POST /auth/logout` | ends the session |
| `GET /auth/me` | current user |
| `POST /auth/password` | change password; evicts this user's other sessions |
| `GET/POST/DELETE /auth/users` | user management (creator/admin) |
| `GET/POST/DELETE /auth/apikeys` | API keys for Phase 6 webhooks (creator/admin) |

The web UI uses the cookie; the Android app can use `Authorization: Bearer <token>`.
Both resolve to the same Redis session, which is what lets Phase 5 hand a conversation
between devices. Passwords are hashed with scrypt, API keys stored as SHA-256 digests
and shown exactly once. Ten failed logins lock an account for 15 minutes.

`GET /healthz` is public and deliberately says nothing but `{"ok": true}`.

## Web UI

`http://localhost:8000/` is the full client, not a settings page. Three tabs today:

- **Chat** — conversations with IRiS, including the tool calls it made. History is
  stored server-side per user, so the same conversation opens on any device.
- **Settings** — every registered setting, rendered from the schema.
- **Activity** — the audit log (SPEC.md 3.2): what IRiS did, when, and for whom.

Voice and memory tabs arrive with Phases 2 and 3.

Every control is drawn by the app — no native `<select>`, no number-spinner arrows,
no browser validation bubbles. Forms carry `novalidate` and report errors inline.
The pieces live in `api/static/app.js` (`Combo`, `Stepper`, `Toggle`) and are shared
by every page; the combobox filters, which is what makes a 486-entry timezone list
usable. Phase 5's React app should port these, not reintroduce native controls.

## Settings

Everything configurable lives at http://localhost:8000/ and is driven by a schema,
not hand-written forms. A module registers a setting in one line:

```python
settings.setting("voice.wake_sensitivity", type="number", minimum=0, maximum=1,
                 default=0.5, title="Wake word sensitivity")
```

…and it appears in the UI on every device automatically, with validation and
live sync. No client-side change is needed to expose a new setting.

| Endpoint | |
|---|---|
| `GET /settings/schema` | JSON Schema of every registered setting |
| `GET /settings/values` | current values (defaults merged with stored overrides) |
| `PUT /settings/values` | partial update; validates the merged result |
| `GET /settings/stream` | server-sent events, one per change |

Values persist in Postgres. Only overrides are stored, so changing a default in
code takes effect for anything the user has not explicitly set.

## Reasoning

`POST /infer` with `{"messages": [...]}`. Optional `model` and `think` override
the defaults per request.

```bash
curl -X POST localhost:8000/infer -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"what time is it?"}]}'
```

The model (`qwen3:8b`) reasons before answering only when asked: `"think": true`
buys accuracy on hard questions at roughly 40 s instead of 3 s. Default is off.
Tools are registered with the `@tool` decorator in `api/main.py`.

| Service | Port (localhost only) | Role |
|---|---|---|
| `api` | 8000 | FastAPI `/infer` — all reasoning routes through here |
| `ollama` | 11434 | LLM serving, GPU |
| `postgres` | 5432 | structured/event store |
| `qdrant` | 6333 | vector memory |
| `redis` | 6379 | session state |
| `mqtt` | 1883 | event bus |

Redis runs with AOF persistence into `./data/redis` — it holds conversations, not
just sessions, so its data has to survive a container recreate.

## Notes for contributors

- GPU containers must request the CDI device explicitly — `devices: ["nvidia.com/gpu=all"]`,
  not `--gpus all`. See [SPEC.md §9](./SPEC.md#9-phase-0-decisions-log).
- Per [SPEC.md §3.4](./SPEC.md#34-standing-rule--one-command-installuninstall-configure-in-ui),
  anything that adds a service, volume, model or system package must update
  **both** paths of `setup.sh` and this README in the same change.
- Config files belong in the repo (e.g. `mosquitto/`); `./data/` is runtime state
  only and is gitignored.
- API tests: `docker compose run --rm api sh -c "pip install -q pytest && python -m pytest test_api.py -q"`

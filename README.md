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
| Web UI | http://localhost:8000/ |
| API liveness | http://localhost:8000/healthz |

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
  stored server-side per user, so the same conversation opens on any device. The mic
  button records and transcribes into the composer.
- **Settings** — every registered setting, rendered from the schema.
- **Activity** — the audit log (SPEC.md 3.2): what IRiS did, when, and for whom.

A memory tab arrives with Phase 3.

### Voice

Speech-to-text runs in its own GPU service (`stt`, faster-whisper large-v3). It
transcribes English and German accurately and auto-detects which is being spoken.

Speech and language models share one 8 GB GPU and do not both fit. Holding Whisper
resident costs the LLM roughly **2.3×** its throughput (72.7 → 31.9 tok/s), so the
speech model is **released after 5 minutes idle** and reloaded on the next recording
(~3 s cold, 0.6 s warm). Tune that with *Release speech model after* in Settings; `0`
keeps it loaded permanently.

> **Microphone needs a secure context.** Browsers only grant `getUserMedia` on
> `localhost` or HTTPS. Over plain-HTTP Tailscale the mic button will refuse. Run
> `tailscale serve https / http://127.0.0.1:8000` to terminate TLS, and set
> `IRIS_COOKIE_SECURE=1` once you do.

Text-to-speech runs in its own service with two selectable engines. **Piper** is the
default: CPU-only, 25-33x realtime, and it ships 11 explicitly British (`en_GB`) voices.
**XTTS** is more expressive but needs 1.65 GB of VRAM it cannot have while the language
model is loaded, so it falls back to CPU and becomes too slow to keep up with playback.
Voice, engine and pace are all settings, with a preview button — a voice can only be
chosen by ear.

**Nothing waits for a whole response.** Text streams token by token, and speech is
synthesised and played one sentence at a time, with the next sentence rendering while
the current one plays. Measured on a four-sentence reply:

| | streamed | if it waited for the whole reply |
|---|---|---|
| first text visible | 0.43 s | 3.06 s |
| speech starts | 1.99 s | ~4.5 s |

Synthesis runs ~3x faster than playback (1.45 s for ~5 s of speech), so after the
first sentence the audio never stalls. Turn it on with *Speak replies aloud*, or play
any single reply with the speaker button on it.

**Hands-free.** Turn on *Hands-free listening* and the microphone stays open,
waiting for a wake word. The browser streams 16 kHz mono audio to the API, where
openWakeWord watches for the phrase and Silero VAD decides when your turn ended;
the transcript is then sent as an ordinary message, so a spoken turn and a typed
one land in the same conversation. Talking over IRiS stops it and captures what you
said instead (*Interrupt while speaking*).

> **The wake word is not "IRiS" yet.** No pre-trained openWakeWord model for that
> phrase exists publicly, so the six bundled words ship as stand-ins and the default
> is `hey_jarvis`. Training an "IRiS" model is a separate job; drop the resulting
> `.onnx` into `./data/wakewords/` and it appears in the *Wake word* dropdown on its
> own, no restart. Wake detection is deliberately suppressed while IRiS is speaking,
> so it cannot wake itself, and barge-in leans on the browser's echo cancellation.

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

Reasoning mode defaults to `model-default`, letting qwen3 decide; `never` and `always`
are also available. Tools are registered with the `@tool` decorator in `api/reasoning.py`.

**Attach an image or document** with the paperclip in the composer. Images are described
by a local vision model, PDFs and DOCX have their text extracted. The result travels with
the message, so IRiS still has the file on later turns without you resending it, and the
transcript shows the attachment collapsed rather than inline.

IRiS **searches rather than guesses**: a `web_search` tool backed by self-hosted SearXNG,
which it is instructed to use for any company, person, product, place or date it is not
certain of. The transcript shows what it is searching for while it happens, then lists the
sources it used. Its persona lives in `api/persona.py` and is editable in Settings.

## Services

| Service | Port (localhost only) | Role |
|---|---|---|
| `api` | 8000 | FastAPI `/infer` — all reasoning routes through here |
| `ollama` | 11434 | LLM serving, GPU |
| `postgres` | 5432 | structured/event store |
| `qdrant` | 6333 | vector memory |
| `redis` | 6379 | session state |
| `stt` | 8001 | speech-to-text, GPU |
| `tts` | 8002 | text-to-speech (Piper/XTTS) |
| `searxng` | 8080 | self-hosted web search |
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
- API tests (the file is not in the image, so it is mounted in):
  ```
  docker compose run --rm --no-deps -v "$PWD/api/test_api.py:/app/test_api.py:ro" \
    --entrypoint sh api -c "pip install -q pytest && python -m pytest test_api.py -q"
  ```
- openWakeWord 0.6.0 declares a hard `tflite-runtime` dependency with no Python 3.12
  wheel, so a plain `pip install openwakeword` silently resolves back to 0.4.0 and a
  different API. `api/Dockerfile` installs it with `--no-deps` and passes
  `inference_framework="onnx"`; do not "fix" that back.

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
> phrase exists publicly. The six words bundled with openWakeWord are all names, so
> `setup.sh` also fetches four that are not — `computer`, `ok_computer`, `ok_home`
> and `hey_house` — and the default is **`computer`**, measured at 0.99 on its own
> phrase against 0.001 on unrelated speech. Training an "IRiS" model is a separate
> job, and `./wakeword/train.sh "hey iris"` does it: it builds a throwaway training
> image, fetches ~6 GB of speech, reverb and noise corpora, synthesises 30,000 spoken
> variations of the phrase, trains, and installs the result. Budget an hour or two,
> mostly downloading. Any `.onnx` in `./data/wakewords/` appears in the *Wake word*
> dropdown on its own, no restart, and `wakeword/evaluate.py` scores one against
> voices it was never trained on before you trust it. Wake detection is deliberately suppressed while IRiS is speaking,
> so it cannot wake itself, and barge-in leans on the browser's echo cancellation.

Every control is drawn by the app — no native `<select>`, no number-spinner arrows,
no browser validation bubbles. Forms carry `novalidate` and report errors inline.
The pieces live in `api/static/app.js` (`Combo`, `Stepper`, `Toggle`) and are shared
by every page; the combobox filters, which is what makes a 486-entry timezone list
usable. Phase 5's React app should port these, not reintroduce native controls.

## Memory

IRiS remembers things between conversations. Facts are embedded with **bge-m3**
(multilingual, so it works across English and German) and stored in Qdrant. Two
paths use them:

- **Automatic recall.** Every turn is searched against the store and anything
  relevant is folded into the system prompt before the model sees the question.
  This is what makes memory actually work; leaving it to the model to *decide* to
  search means it mostly does not.
- **A `remember` tool**, so IRiS can deliberately store something it just learned,
  and a `recall` tool for digging out an older detail.
- **Learning from conversations.** After each exchange a second, tool-free pass picks
  out anything durable and stores it. Every fact must arrive with a **quote copied
  word for word** from the conversation, which is then verified as a real substring
  before the fact is kept. An 8B model pads its list no matter how the prompt is
  worded, and asked about a move to Winterthur it volunteered "they are adjusting to
  a new daily routine", which nobody said. Requiring evidence turns "did it obey" into
  "is this string present": a model can invent a fact, but not a quote that is
  already in the text.

A near-identical fact replaces the existing one rather than piling up copies. The
**Memory** tab lists everything remembered, searches it, and lets you add or forget
entries by hand.

Measured with bge-m3 on full-sentence questions: genuine matches score down to 0.43
and unrelated ones up to 0.37, so the recall threshold defaults to 0.42. The bands
are close, so it is worth tuning by eye once there are real memories in there. Turns
shorter than three words skip recall entirely, because a two-word fragment scores
~0.44 against almost anything and would drag in the whole store.

**Retention.** Transcripts older than *Keep transcripts for* (30 days by default) are
deleted nightly, an hour after the backup so nothing expires before it is archived.
Only the verbatim record expires: whatever IRiS learned from a conversation was
distilled into a memory when the exchange happened and is kept. Set it to 0 to keep
everything forever.

**Ingesting a recording.** The Memory tab takes an audio or video file, transcribes
it, chunks the transcript on sentence boundaries and stores it, then distils any
durable facts out of it through the same evidenced extractor. Searching afterwards
ranks the distilled fact above the raw chunk it came from.

> Not yet built: speaker diarization. pyannote's models are gated behind a
> HuggingFace licence that has to be accepted with an account, so it needs a decision
> rather than more work.

## Transit and places

Two integrations that need no account, so they work the moment IRiS is installed:
Swiss public transport via **transport.opendata.ch** and place lookup via
OpenStreetMap's **Nominatim**. IRiS gets three tools — a journey, a departure board,
and a place search — so "when's the next train to Zurich?" and "where's the nearest
pharmacy?" are answered from the live timetable and the map rather than from a web
search.

Set **Home** and **Work** in Settings and the words work as words: "the next train
home" resolves without naming the stop. Left empty, IRiS asks rather than guessing.

> Transit is Switzerland only, which is what [SPEC.md §5](./SPEC.md) specifies.
> Nominatim's usage policy caps requests at one a second and requires an identifying
> User-Agent; both are enforced here rather than hoped for.

## Speaking first

IRiS can start a conversation rather than only answering. **Speak first** is off by
default, because it should be your decision that it may interrupt you.

The daily briefing is built the sober way round: the facts are gathered by calling
the tools directly, and only the *wording* is left to the model. Asking an 8B model
to "go and check everything" means it sometimes decides it already knows, and a
briefing that quietly invents your morning is worse than no briefing. It arrives as a
new conversation in the chat — as if IRiS had messaged first — and goes out to every
configured webhook. *briefing* in the Conversations tab runs one on demand.

**Quiet hours** wrap midnight properly, so 22:00 to 07:00 means what it says. A
briefing due inside them waits. If the machine was off at briefing time, the day is
skipped rather than delivered at lunchtime.

## Backups

Everything IRiS knows lives in three places, so a backup is all three or it is
worthless: **Postgres** (accounts, settings, audit log), **Qdrant** (memories) and
**Redis** (conversations). One encrypted archive a day, at 03:00 by default, lands in
**`./backup/`**.

```
./restore.sh --list          what is available
./restore.sh                 restore the most recent
./restore.sh backup/iris-... restore a specific one
```

Archives are AES-256-CBC with PBKDF2, using `IRIS_BACKUP_KEY` from `.env`. `openssl`
was already required by `setup.sh` to generate the database password, so this adds no
dependency and a restore needs nothing that is not on any Linux box. Time, retention
count and whether to include conversations are all settings; the Memory tab has a
*back up now* button and lists what exists.

> **`./backup/` is gitignored, deliberately.** This repo is public
> ([SPEC.md §4](./SPEC.md)) and an archive of every conversation and memory is exactly
> what must never be committed. The folder lives beside `./data/`, not in git.

> **Two warnings worth taking seriously.**
> **`IRIS_BACKUP_KEY` is not in the backup.** It is in `.env`, which is also
> gitignored. Lose `.env` and every archive is permanently unreadable, so keep a copy
> of that key somewhere else.
> **This is one machine.** Phase 3 asks for an off-machine copy, and a folder on
> stzrhws01 does not survive stzrhws01 dying. Because it is a plain directory, any
> sync you like covers that: an rsync cron to a NAS, a Tailscale copy to another
> node, or an external drive.

## Devices and integrations

Two tabs, one mechanism. **Devices** are things in the house; **Integrations** are
accounts and services. Both work the same way: press *add*, pick a type, fill in the
form the type declares, and the instance appears with whatever actions that type
supports.

| | types today | actions |
|---|---|---|
| Devices | Camera, Microphone | *look*, *snapshot*, *listen* |
| Integrations | Mailbox (IMAP), Webhook | *check mail*, *send a test* |

A **camera** takes an `rtsp://…` URL or the `http://…/snapshot.jpg` endpoint many
cameras expose; *look* pulls a frame and describes it, and IRiS gets a
`look_at_camera` tool so "is anyone at the front door?" works in chat. A
**microphone** records a span of a network audio stream, transcribes it, and files it
in memory. A **mailbox** is any IMAP server — Outlook, Gmail with an app password, or
your own — and gives IRiS a `check_mail` tool.

Credentials never come back out. A secret field is stored on this machine and
returned to the browser as dots; sending the dots back means "unchanged", so editing a
mailbox's server does not require retyping its password. ffmpeg's error text is masked
too, since it echoes the whole URL on failure.

> **Adding a type costs one `register(...)` call and no client work.** The form, the
> validation, the list, the action buttons and the audit entries are all generic. That
> is the point of the split: the next device or integration is a declaration, not a
> feature.

> Frigate — continuous recording, motion and object detection, event history — is
> [SPEC.md §5](./SPEC.md)'s choice for the NVR half and is **not built yet**. It needs
> per-camera tuning, a hardware-acceleration decision and the actual camera inventory.

## Quick commands

The lightning button in the composer picks a command — *Transit*, *Look at a camera*,
*Check mail*, *Search the web*, *Remember this*, *Find a place* — and a chip appears
above the box. What you type is then aimed at the right tool without the decision
being taken away from the model. The command applies to one turn and clears itself,
and the transcript stores what you actually typed, not the directive.

Commands whose feature is switched off do not appear, because a command that steers at
a disabled tool produces an apology rather than an answer. Adding one is a dict entry
in `reasoning.QUICK_COMMANDS`; the menu is built from the server.

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

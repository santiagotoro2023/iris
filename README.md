# IRiS — Integrated Reasoning, in Silico

Locally-run PDA-style assistant. Build spec and phase plan: [SPEC.md](./SPEC.md).

## HTTPS

IRiS serves **https on port 8000**. `setup.sh` generates a self-signed certificate
into `./data/tls/`, naming `localhost`, this machine's hostname, every LAN address it
has and its Tailscale address, so the browser complains about the signature rather
than the name. Accept it once. The certificate lasts ten years and is only regenerated
when it is missing or within a month of expiry.

The microphone, hands-free listening and geolocation all need a secure context, so
this is what makes them work anywhere but `localhost`.

> **Reaching it from outside.** Forwarding your router's 443 to this host's 8000 does
> work, with two things worth knowing. A self-signed certificate makes every browser
> warn, and phones make that harder to click past than desktops; if the name is
> already yours, a real certificate from Let's Encrypt removes the warning entirely.
> And once it is on the internet, the login is the only thing between the world and
> everything IRiS knows, so change the password off `1234` first. Tailscale avoids
> both problems by not exposing anything.

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

## Starting with the machine

Docker is enabled at boot and every service is `restart: unless-stopped`, so IRiS
comes back on its own after a reboot or a power cut. Nothing else to set up.

**A missed briefing is caught up.** If the machine was off at briefing time, the
briefing is written as soon as it is on, and the fact that today has been briefed is
recorded outside the process, so a restart does not produce a second one.

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

> The microphone and geolocation both need a secure context, which HTTPS provides.
> `setup.sh` generates the certificate, so this is handled.

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

Three services that need no account, so they work the moment IRiS is installed: Swiss
public transport via **transport.opendata.ch**, place lookup via OpenStreetMap's
**Nominatim**, and forecasts from **Open-Meteo**. IRiS gets four tools — a journey, a
departure board, a place search and the weather — so "when's the next train to
Zurich?", "where's the nearest pharmacy?" and "do I need a coat?" are answered from
live data rather than from a web search.

**Follow my location** is on by default: the browser volunteers a position when the
page opens and before each message, so what IRiS knows is where you are now rather
than where you were when you last pressed a button. The fix is taken with GPS
accuracy requested, because a coarse network position put the nearest stop in the
wrong part of a village. After that:

- *"what's the weather"* uses where you are, not a town name and certainly not the
  middle of the country.
- *"when's the next bus to Uster"* starts from **the nearest stop to you**, because
  that question has an origin, it just is not spoken. Naming a starting point still
  wins.
- Set **Home** and **Work** too, and the words work as words: "the next train home"
  resolves without naming the stop.

Geolocation needs a secure context, the same caveat as the microphone: localhost or
HTTPS.

> Transit is Switzerland only, which is what [SPEC.md §5](./SPEC.md) specifies.
> Nominatim's usage policy caps requests at one a second and requires an identifying
> User-Agent; both are enforced here rather than hoped for.

## Speaking first

IRiS can start a conversation rather than only answering, which **Speak first**
controls.

The daily briefing is built the sober way round: the facts are gathered by calling
the tools directly, and only the *wording* is left to the model. Asking an 8B model
to "go and check everything" means it sometimes decides it already knows, and a
briefing that quietly invents your morning is worse than no briefing. It arrives as a
new conversation in the chat — as if IRiS had messaged first — and goes out to every
configured webhook. *briefing* in the Conversations tab runs one on demand.

It covers, in order: the weather where you are, the commute, your calendar, your mail,
and a few headlines from the world and your region. The headlines come from a live
news search and **the sources are attached to the briefing** as an ordinary collapsed
source list, with links, exactly as a web search in chat looks.

The briefing is written by the server, not the browser, so it does not matter whether
the page is open: get up at half seven and the seven o'clock briefing is already
waiting in Conversations.

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

**Devices** are things in the house; **Integrations** are accounts and services.
Press *add*, pick a type, fill in the form, and the instance appears with its actions.

| | types today | actions |
|---|---|---|
| Devices | Camera, Microphone | *look*, *snapshot*, *listen* |
| Integrations | Mailbox (IMAP), Calendar (CalDAV), Push to phone (ntfy), Webhook | *check mail*, *what's on*, *send a test* |

A **camera** takes an `rtsp://…` URL or the `http://…/snapshot.jpg` endpoint many
cameras expose; *look* pulls a frame and describes it, and IRiS gets a
`look_at_camera` tool so "is anyone at the front door?" works in chat. A
**microphone** records a span of a network audio stream, transcribes it, and files it
in memory. A **mailbox** is any IMAP server — Outlook, Gmail with an app password, or
your own — and gives IRiS a `check_mail` tool. A **calendar** is CalDAV, which
Outlook, Google and Nextcloud all speak with a username and app password, no OAuth
registration. **Push** is ntfy: notifications on your phone with no account and no
app store, and it carries the daily briefing too.

Credentials never come back out: a secret is shown as dots, and sending the dots back
means "unchanged", so changing a mailbox's server does not need the password retyped.

> Adding a type is one `register(...)` call. The form, validation, list, action buttons
> and audit entries are all generic.

**Frigate** handles the recording half: continuous recording, motion and CPU object
detection. Its config is *generated* from the cameras you added, not hand-written, so
adding a camera in the UI and pressing apply is the whole workflow:

```
curl -X POST localhost:8000/frigate/apply     # or the button in Devices
docker compose --profile cameras up -d frigate
```

It runs under a compose profile because Frigate refuses to start with an empty camera
list, so `docker compose up` alone never starts it. **Detection is on the CPU by
choice** — the 3060 Ti's 8 GB is already split between the language model, Whisper and
the vision model, and continuous detection on top would change the budget every
earlier phase was tuned around. *Detection frame rate* is the setting that decides how
much of the machine it takes.

## Tools announce themselves

Every tool says what it is doing while it does it: *"Checking the timetable,
Winterthur to Zurich HB"*, *"Looking at the front door camera"*. Open the banner for
the result and its links.

The line is part of the tool's declaration:

```python
@tool("weather", "...", {...},
      activity="Checking the weather {place}", display="lines")
```

`display` picks how the result renders: `sources`, `lines`, or `text`. Only relevant
tools are sent each turn, so a small model is not choosing between fifteen.

## Quick commands

The lightning button picks a command — *Transit*, *Schedule*, *Weather*, *Check
mail*, *Look at a camera*, *Find a place*, *Search the web*, *Remember this*. What you
type is then aimed at that tool. It applies to one message and clears itself, and the
message shows which command it carried.

Commands for switched-off features are hidden. Adding one is a dict entry in
`reasoning.QUICK_COMMANDS`.

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
- API tests. The bind mount matters: `COPY *.py` bakes the tests into the image, so
  without it you silently run the copy from the last build.
  ```
  docker compose run --rm --no-deps -v "$PWD/api:/app:ro" --entrypoint sh api \
    -c "pip install -q pytest && python -m pytest test_api.py -q"
  ```
- openWakeWord 0.6.0 declares a hard `tflite-runtime` dependency with no Python 3.12
  wheel, so a plain `pip install openwakeword` silently resolves back to 0.4.0 and a
  different API. `api/Dockerfile` installs it with `--no-deps` and passes
  `inference_framework="onnx"`; do not "fix" that back.

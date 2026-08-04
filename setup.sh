#!/usr/bin/env bash
# IRiS installer.
#
#   ./setup.sh                      install / update everything
#   ./setup.sh --uninstall          stop and remove the stack, KEEP ./data
#   ./setup.sh --uninstall --purge  also delete ./data and .env (irreversible)
#
# SPEC.md 3.4 — this script is a standing obligation: any phase that adds a
# service, volume, model, system package or config file updates BOTH paths here
# (install and uninstall) in the same change. Stale components are a defect.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

MODEL_DEFAULT="qwen3:8b"
VISION_DEFAULT="qwen2.5vl:3b"
EMBED_DEFAULT="bge-m3"
# Every runtime state dir. Uninstall --purge removes the ./data root wholesale.
DATA_DIRS=(data/postgres data/qdrant data/ollama data/redis data/whisper data/tts
           data/searxng data/media data/wakewords
           data/mosquitto/data data/mosquitto/log)

log()  { printf '\033[38;5;208m::\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
sudo_() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# Run compose even when this shell predates our docker-group membership.
dc() {
  if docker info >/dev/null 2>&1; then
    docker compose "$@"
  elif getent group docker | grep -qw "${USER:-$(id -un)}"; then
    sg docker -c "docker compose $(printf '%q ' "$@")"
  else
    die "Cannot reach the Docker daemon. Re-run after logging out and back in."
  fi
}

usage() { sed -n '2,6p' "$0" | sed 's/^# \?//'; }

# ---------------------------------------------------------------- install ----

check_prereqs() {
  have docker || die "Docker is not installed. See https://docs.docker.com/engine/install/"
  docker compose version >/dev/null 2>&1 || die "The 'docker compose' plugin is missing."
}

ensure_docker_group() {
  local u="${USER:-$(id -un)}"
  getent group docker | grep -qw "$u" && return 0
  log "Adding $u to the docker group..."
  sudo_ usermod -aG docker "$u"
  warn "Group membership applies at next login — log out and back in when convenient."
}

setup_gpu() {
  if ! have nvidia-smi; then
    warn "No NVIDIA GPU detected. IRiS will run on CPU, which is far slower."
    return 0
  fi

  if ! have nvidia-ctk; then
    log "Installing the NVIDIA Container Toolkit..."
    if ! { curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
             | sudo_ gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
           && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
             | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
             | sudo_ tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null \
           && sudo_ apt-get update -qq \
           && sudo_ apt-get install -y nvidia-container-toolkit; }; then
      warn "NVIDIA Container Toolkit install failed — IRiS will fall back to CPU (much slower)."
      return 0
    fi
  fi

  # The CDI spec pins driver-versioned library paths (libcuda.so.<driver>), so a
  # driver update silently breaks passthrough until it is regenerated. SPEC.md 9.
  # The spec is full of driver-versioned paths; if the running driver's version
  # string is absent, the spec predates the current driver and must be rebuilt.
  local driver
  driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  if ! grep -q "$driver" /etc/cdi/nvidia.yaml 2>/dev/null; then
    log "Generating CDI spec for driver $driver..."
    sudo_ nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null
  fi
}

# Remote access is a convenience, not a prerequisite — a failure here warns and
# continues rather than aborting an otherwise working install.
setup_tailscale() {
  if ! have tailscale; then
    log "Installing Tailscale..."
    if ! curl -fsSL https://tailscale.com/install.sh | sudo_ sh; then
      warn "Tailscale install failed. IRiS runs fine locally; re-run setup.sh to retry."
      return 0
    fi
  fi
  # Already on the tailnet? Then there is nothing interactive left to do.
  if tailscale status >/dev/null 2>&1; then
    log "Tailscale is connected: $(tailscale ip -4 2>/dev/null | head -1)"
    return 0
  fi
  warn "Tailscale needs a one-time browser login. This is the only interactive step."
  if sudo_ tailscale up; then
    log "Tailscale connected: $(tailscale ip -4 2>/dev/null | head -1)"
  else
    warn "Could not connect Tailscale. Run 'sudo tailscale up' later for remote access."
  fi
}

# Add a key only if absent, so an existing .env gains new settings on upgrade
# instead of silently missing them. Existing values are never overwritten.
add_env() {
  grep -qE "^$1=" .env 2>/dev/null && return 0
  printf '%s=%s\n' "$1" "$2" >> .env
  log "  .env: added $1"
}

# Wake words that are not somebody's name (Santiago's requirement). The six models
# bundled in the image are all names, so these come from the Home Assistant
# community collection, ~200 KB each. Measured against our own voice: "computer"
# scores 0.99 on its phrase and 0.001 on unrelated speech.
WAKE_WORDS="computer:en/computer/computer_v2.onnx
ok_computer:en/ok_computer/ok_computer.onnx
ok_home:en/ok_home/ok_home.onnx
hey_house:en/hey_house/hey_house.onnx"
WAKE_REPO="https://raw.githubusercontent.com/fwartner/home-assistant-wakewords-collection/main"

ensure_wakewords() {
  local name path
  while IFS=: read -r name path; do
    [ -n "$name" ] || continue
    [ -f "data/wakewords/$name.onnx" ] && continue
    log "  fetching wake word '$name'..."
    curl -sfL "$WAKE_REPO/$path" -o "data/wakewords/$name.onnx" \
      || warn "Could not fetch the '$name' wake word; the bundled ones still work."
  done <<EOF
$WAKE_WORDS
EOF
}

# SearXNG needs a secret in its config file. Generated here so it never enters the repo.
ensure_searxng() {
  local cfg=data/searxng/settings.yml
  [ -f "$cfg" ] && return 0
  log "Writing SearXNG config with a generated secret..."
  sed "s|__SECRET__|$(openssl rand -hex 32)|" searxng/settings.template.yml > "$cfg"
  chmod 600 "$cfg"
}

ensure_env() {
  mkdir -p "${DATA_DIRS[@]}"
  have openssl || die "openssl is required to generate the database password."
  if [ ! -f .env ]; then
    log "Creating .env with a random database password..."
    : > .env
  fi
  chmod 600 .env
  add_env POSTGRES_USER iris
  add_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  add_env POSTGRES_DB iris
  add_env IRIS_MODEL "$MODEL_DEFAULT"
  add_env IRIS_VISION_MODEL "$VISION_DEFAULT"
  add_env IRIS_EMBED_MODEL "$EMBED_DEFAULT"
  add_env IRIS_TZ "$(cat /etc/timezone 2>/dev/null || echo Europe/Zurich)"
  add_env IRIS_THINK ""
}

wait_for_ollama() {
  local i
  for i in $(seq 1 60); do
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && return 0
    sleep 2
  done
  die "Ollama did not come up within 120s. Check: docker compose logs ollama"
}

# Speech (~3 GB) and voice (~2 GB) weights download on first run. Wait so the install
# finishes ready to use rather than stalling the first recording. Non-fatal: text chat
# works without either, so a slow download must never fail the install.
wait_for_voice() {
  local i stt=0 tts=0
  log "Waiting for the speech and voice models (~5 GB on first run)..."
  for i in $(seq 1 180); do
    if [ "$stt" -eq 0 ] && curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
      stt=1
      log "  speech service ready"
    fi
    if [ "$tts" -eq 0 ] && curl -fsS http://127.0.0.1:8002/health >/dev/null 2>&1; then
      tts=1
      log "  voice service ready"
    fi
    if [ "$stt" -eq 1 ] && [ "$tts" -eq 1 ]; then
      return 0
    fi
    sleep 5
  done
  warn "Voice services still downloading (speech=$stt voice=$tts). Text chat works meanwhile."
}

# Everything Ollama serves: the language model, the vision model that reads uploaded
# images, and the embedding model memories are searched by. Pulled here so the first
# upload or the first remembered fact is not a silent multi-GB stall.
pull_models() {
  local model vision embed
  model="$(grep -E '^IRIS_MODEL=' .env | cut -d= -f2- || true)"
  vision="$(grep -E '^IRIS_VISION_MODEL=' .env | cut -d= -f2- || true)"
  embed="$(grep -E '^IRIS_EMBED_MODEL=' .env | cut -d= -f2- || true)"
  log "Pulling ${model:-$MODEL_DEFAULT} (~5 GB), ${vision:-$VISION_DEFAULT} (~3 GB)" \
      "and ${embed:-$EMBED_DEFAULT} (~1 GB)..."
  dc exec -T ollama ollama pull "${model:-$MODEL_DEFAULT}"
  dc exec -T ollama ollama pull "${vision:-$VISION_DEFAULT}" \
    || warn "Vision model pull failed; image analysis will be unavailable until it succeeds."
  dc exec -T ollama ollama pull "${embed:-$EMBED_DEFAULT}" \
    || warn "Embedding model pull failed; memory will be unavailable until it succeeds."
}

install() {
  check_prereqs
  ensure_docker_group
  setup_gpu
  setup_tailscale
  ensure_env
  ensure_searxng
  ensure_wakewords
  log "Building and starting services..."
  dc up -d --build
  wait_for_ollama
  pull_models
  wait_for_voice
  log "Done. IRiS is running."
  echo
  echo "  Settings   http://localhost:8000/"
  echo "  API        http://localhost:8000/healthz"
  echo
  echo "  First sign-in: creator / 1234 — you are asked to change it immediately."
  echo "  Everything else is configured in that UI, not here."
}

# -------------------------------------------------------------- uninstall ----

uninstall() {
  log "Stopping containers and removing images, networks and named volumes..."
  dc down --rmi all --volumes --remove-orphans || warn "Compose teardown reported errors; continuing."

  if [ "${PURGE:-0}" != 1 ]; then
    log "Kept ./data and .env."
    echo "  ./data holds all of IRiS's memory (Postgres, Qdrant, models, media)"
    echo "  and any wake word models you trained, in ./data/wakewords."
    [ -d data/wakeword-training ] && echo "  Wake word training data is still there too:  ./wakeword/train.sh --clean"
    echo "  To remove those too:  ./setup.sh --uninstall --purge"
    return 0
  fi

  [ -e data ] || [ -e .env ] || { log "Nothing left to purge."; return 0; }
  warn "--purge permanently deletes ./data and .env. This cannot be undone."
  [ -d data ] && du -sh data 2>/dev/null | sed 's/^/     /'
  read -r -p "Type DELETE to confirm: " reply
  [ "$reply" = "DELETE" ] || die "Aborted — nothing was deleted."
  rm -rf data .env
  log "Purged ./data and .env."
  echo "  Left alone deliberately: Docker itself, the NVIDIA Container Toolkit,"
  echo "  /etc/cdi/nvidia.yaml and Tailscale — other software on this host may"
  echo "  depend on them, and removing Tailscale could cut your remote access."
}

# ------------------------------------------------------------------- main ----

ACTION=install
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --uninstall) ACTION=uninstall ;;
    --purge)     PURGE=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "Unknown option: $arg (try --help)" ;;
  esac
done

[ "$PURGE" = 1 ] && [ "$ACTION" != uninstall ] && die "--purge only applies with --uninstall."

"$ACTION"

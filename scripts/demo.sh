#!/usr/bin/env bash
#
# One command, clean clone to working demo. Brings up MongoDB, seeds synthetic
# data, starts the API, runs scripts/demo.py against it, and tears the API back
# down. Mongo is left running — reruns are then a few seconds.
#
#   make demo          (or)   ./scripts/demo.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
API_PORT="${API_PORT:-8000}"
BASE_URL="http://localhost:${API_PORT}"
API_PID=""

say()  { printf '%s\n' "${BOLD}==> $*${OFF}"; }
dim()  { printf '%s\n' "${DIM}    $*${OFF}"; }
die()  { printf '%s\n' "${RED}==> $*${OFF}" >&2; exit 1; }

cleanup() {
  # Only the API is ours to stop. Leaving Mongo up is deliberate: the second
  # run of this script should not have to wait for a database to boot.
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    say "Stopping the API (pid $API_PID)"
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
if [[ ! -f .env ]]; then
  say "No .env — creating one from .env.example"
  cp .env.example .env
fi

if grep -qE '^MODEL_API_KEY=(changeme)?$' .env; then
  cat <<EOF

${YELLOW}${BOLD}MODEL_API_KEY is not set in .env.${OFF}

The demo drives a real agent loop, so it needs a model. Two options:

  ${BOLD}Hosted${OFF}   set MODEL_PROVIDER + MODEL_API_KEY in .env
           (google | openai | anthropic — see .env.example)

  ${BOLD}Local${OFF}    no key at all, via Ollama:
             ollama serve && ollama pull llama3.1
             then in .env:  MODEL_PROVIDER=ollama
                            MODEL_NAME=llama3.1

Everything except the demo works without a key — ${BOLD}make test${OFF} runs the full
suite (63 tests) against a scripted model and an in-memory Mongo.

EOF
  die "Set a model provider in .env, then rerun."
fi

# --------------------------------------------------------------------------- #
# 2. Dependencies
# --------------------------------------------------------------------------- #
python -c 'import fastapi, langgraph, mcp, httpx' 2>/dev/null || {
  say "Installing Python dependencies"
  pip install -q -e ".[dev]"
}

# --------------------------------------------------------------------------- #
# 3. MongoDB
# --------------------------------------------------------------------------- #
if ! docker info >/dev/null 2>&1; then
  die "Docker is not available. Start it (e.g. sudo systemctl start docker) and rerun."
fi

say "Starting MongoDB"
docker compose up -d mongo >/dev/null

printf '%s' "${DIM}    waiting for mongo${OFF}"
for _ in $(seq 1 30); do
  if docker compose exec -T mongo mongosh --quiet --eval 'db.adminCommand("ping")' >/dev/null 2>&1; then
    printf ' %s\n' "up"
    break
  fi
  printf '.'
  sleep 1
done
docker compose exec -T mongo mongosh --quiet --eval 'db.adminCommand("ping")' >/dev/null 2>&1 \
  || die "MongoDB did not come up. Check: docker compose logs mongo"

# --------------------------------------------------------------------------- #
# 4. Synthetic data
# --------------------------------------------------------------------------- #
say "Seeding synthetic data"
python -m scripts.seed

# --------------------------------------------------------------------------- #
# 5. API
# --------------------------------------------------------------------------- #
say "Starting the API on port ${API_PORT}"
uvicorn src.api.main:app --port "${API_PORT}" --log-level warning &
API_PID=$!

printf '%s' "${DIM}    waiting for /healthz${OFF}"
for _ in $(seq 1 30); do
  if curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1; then
    printf ' %s\n' "ok"
    break
  fi
  kill -0 "$API_PID" 2>/dev/null || die "The API exited during startup. Rerun without --log-level warning to see why."
  printf '.'
  sleep 1
done
curl -fsS "${BASE_URL}/healthz" >/dev/null 2>&1 || die "The API never became healthy."

# --------------------------------------------------------------------------- #
# 6. The demo itself
# --------------------------------------------------------------------------- #
dim "everything below is over HTTP, exactly as a real client would call it"
python -m scripts.demo --base-url "${BASE_URL}"

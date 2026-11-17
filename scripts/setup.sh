#!/usr/bin/env sh
# One-shot, idempotent setup for the coding harness (Linux / macOS). Same steps as setup.ps1.
#
#   sh scripts/setup.sh                         # everything, FH endpoint model (needs API key + FH network/VPN)
#   PROVIDER=local sh scripts/setup.sh          # everything, local Ollama model instead
#   PROVIDER=local MODEL=qwen2.5-coder:7b sh scripts/setup.sh
#   SKIP_DOCKER=1 sh scripts/setup.sh           # no sandbox image (local backend only)
#
# The provider choice is written to .env (git-ignored); an API key already in .env is kept.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
COMMIT=14c84797ffa77255d53cf1a02fe6aafda2b68aeb
PROVIDER="${PROVIDER:-fh}"
MODEL="${MODEL:-}"
PY="${PYTHON:-python3}"

step() { printf '\n==> %s\n' "$1"; }
warn() { printf 'WARNING: %s\n' "$1" >&2; }

# set_env KEY VALUE: replace or append KEY=VALUE in .env (values are never printed)
set_env() {
  touch .env
  grep -v "^[[:space:]]*$1[[:space:]]*=" .env > .env.tmp || true
  printf '%s=%s\n' "$1" "$2" >> .env.tmp
  mv .env.tmp .env
}
get_env() { [ -f .env ] && sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | head -n 1 | tr -d "\"'" || true; }

case "$PROVIDER" in fh|local) ;; *) echo "PROVIDER must be fh or local" >&2; exit 2 ;; esac

step "Checking Python (3.11+ required)"
"$PY" -c "import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version)"

step "Harness virtualenv (.venv)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q -e ".[dev]"

step "Target repository (git submodule targets/cosmicpython @ $(echo $COMMIT | cut -c1-7))"
git submodule update --init targets/cosmicpython
HEAD="$(git -C targets/cosmicpython rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "targets/cosmicpython is at $HEAD, expected $COMMIT" >&2; exit 1; }
echo "ok: $HEAD"

step "Target test environment (targets/.venv-cosmic, used by the local sandbox backend)"
[ -x targets/.venv-cosmic/bin/python ] || "$PY" -m venv targets/.venv-cosmic
targets/.venv-cosmic/bin/python -m pip install -q -r sandbox/requirements-cosmic.txt

step "Sandbox image (container engine: docker or podman)"
if [ -n "${SKIP_DOCKER:-}" ]; then
  echo "skipped (SKIP_DOCKER)"
elif ! .venv/bin/harness build-image --config examples/cosmic-feature/harness.yaml; then
  warn "no container engine / image build failed - only the local (non-isolated) backend is available. Use --sandbox local."
fi

MODEL_READY=1
if [ "$PROVIDER" = fh ]; then
  step "Model: FH endpoint (OpenAI-compatible)"
  MODEL="${MODEL:-Qwen/Qwen3.6-35B-A3B}"
  set_env HARNESS_PROVIDER openai
  set_env HARNESS_MODEL "$MODEL"
  set_env OPENAI_BASE_URL YOUR_ENDPOINT_URL
  KEY="$(get_env OPENAI_API_KEY)"
  if [ -z "$KEY" ] || [ "$KEY" = YOUR_API_KEY ]; then
    set_env OPENAI_API_KEY ""
    MODEL_READY=0
    warn "Put your FH API key into .env (OPENAI_API_KEY=...), then run: .venv/bin/harness model-check"
  else
    set_env OPENAI_API_KEY "$KEY"
  fi
  echo "provider openai, model $MODEL (.env). The endpoint is reachable only from the FH network / VPN."
else
  step "Model: local Ollama"
  MODEL="${MODEL:-qwen2.5-coder:14b}"
  set_env HARNESS_PROVIDER ollama
  set_env HARNESS_MODEL "$MODEL"
  set_env OLLAMA_HOST http://localhost:11434
  if command -v ollama >/dev/null 2>&1; then
    ollama pull "$MODEL"
  else
    MODEL_READY=0
    warn "ollama not found - install it from https://ollama.com, then run: ollama pull $MODEL"
  fi
fi

step "Self-test (no model needed)"
.venv/bin/python -m pytest -q

step "Model check (one tool call)"
if [ "$MODEL_READY" = 1 ]; then
  .venv/bin/harness model-check || warn "model check failed - see above (FH: VPN connected? key valid?  local: ollama running?)"
else
  echo "skipped (model not ready yet, see warning above)"
fi

printf '\nSetup complete. Try:\n'
echo "  .venv/bin/harness demo                      # no model needed: full run incl. one fix round"
echo "  .venv/bin/harness start --project cosmic-feature --task-file examples/cosmic-feature/task.md"

#!/usr/bin/env sh
# One-shot, idempotent setup for the coding harness (Linux / macOS).
#
#   sh scripts/setup.sh                    # everything
#   SKIP_DOCKER=1 sh scripts/setup.sh      # no sandbox image (local backend only)
#   MODEL=qwen2.5-coder:7b sh scripts/setup.sh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
COMMIT=14c84797ffa77255d53cf1a02fe6aafda2b68aeb
MODEL="${MODEL:-qwen2.5-coder:14b}"
PY="${PYTHON:-python3}"

step() { printf '\n==> %s\n' "$1"; }

step "Checking Python (3.11+ required)"
"$PY" -c "import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version)"

step "Harness virtualenv (.venv)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q -e ".[dev]"

step "Target repository (git submodule targets/cosmicpython @ $COMMIT)"
git submodule update --init targets/cosmicpython
HEAD="$(git -C targets/cosmicpython rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || { echo "targets/cosmicpython is at $HEAD, expected $COMMIT" >&2; exit 1; }

step "Target test environment (targets/.venv-cosmic, used by the local sandbox backend)"
[ -d targets/.venv-cosmic ] || "$PY" -m venv targets/.venv-cosmic
targets/.venv-cosmic/bin/python -m pip install -q -r sandbox/requirements-cosmic.txt

step "Sandbox image (container engine: docker or podman)"
if [ -n "${SKIP_DOCKER:-}" ]; then
  echo "skipped (SKIP_DOCKER)"
elif ! .venv/bin/harness build-image --config examples/cosmic/harness.yaml; then
  echo "WARNING: no container engine / image build failed - only the local (non-isolated) backend is available. Use --sandbox local." >&2
fi

step "Model (Ollama)"
if command -v ollama >/dev/null 2>&1; then
  ollama pull "$MODEL"
else
  echo "WARNING: ollama not found - install it from https://ollama.com, then run: ollama pull $MODEL" >&2
fi

step "Self-test (no model needed)"
.venv/bin/python -m pytest -q

printf '\nSetup complete. Try:\n'
echo "  .venv/bin/harness baseline --config examples/cosmic-demo/harness.yaml"
echo "  .venv/bin/harness run --config examples/cosmic-demo/harness.yaml --task-file examples/cosmic-demo/task.md"

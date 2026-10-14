# One-shot, idempotent setup for the coding harness (Windows / PowerShell).
#
#   scripts\setup.ps1                 # everything
#   scripts\setup.ps1 -SkipDocker     # no sandbox image (local backend only)
#   scripts\setup.ps1 -Model qwen2.5-coder:7b
param(
    [switch]$SkipDocker,
    [string]$Model = "qwen2.5-coder:14b"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Commit = "14c84797ffa77255d53cf1a02fe6aafda2b68aeb"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Run {
    & $args[0] $args[1..($args.Count - 1)]
    if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $args" }
}

Step "Checking Python (3.11+ required)"
Run python -c "import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version)"

Step "Harness virtualenv (.venv)"
if (-not (Test-Path .venv)) { Run python -m venv .venv }
Run .venv\Scripts\python.exe -m pip install -q -e ".[dev]"

Step "Target repository (git submodule targets/cosmicpython @ $Commit)"
Run git submodule update --init targets/cosmicpython
$head = (git -C targets/cosmicpython rev-parse HEAD).Trim()
if ($head -ne $Commit) { throw "targets/cosmicpython is at $head, expected $Commit" }

Step "Target test environment (targets\.venv-cosmic, used by the local sandbox backend)"
if (-not (Test-Path targets\.venv-cosmic)) { Run python -m venv targets\.venv-cosmic }
Run targets\.venv-cosmic\Scripts\python.exe -m pip install -q -r sandbox\requirements-cosmic.txt

Step "Sandbox image (container engine: docker, podman, or either one inside WSL)"
if ($SkipDocker) {
    Write-Host "skipped (-SkipDocker)"
} else {
    & .venv\Scripts\harness.exe build-image --config examples/cosmic/harness.yaml
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "no container engine / image build failed - only the local (non-isolated) backend is available. Use --sandbox local."
    }
}

Step "Model (Ollama)"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Run ollama pull $Model
} else {
    Write-Warning "ollama not found - install it from https://ollama.com, then run: ollama pull $Model"
}

Step "Self-test (no model needed)"
Run .venv\Scripts\python.exe -m pytest -q

Write-Host "`nSetup complete. Try:" -ForegroundColor Green
Write-Host "  .venv\Scripts\harness baseline --config examples/cosmic-demo/harness.yaml"
Write-Host "  .venv\Scripts\harness run --config examples/cosmic-demo/harness.yaml --task-file examples/cosmic-demo/task.md"

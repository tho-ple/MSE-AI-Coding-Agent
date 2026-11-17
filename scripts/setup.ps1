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

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Run {
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $($args -join ' ')" }
}

# Returns the full path of a working Python >= 3.11 (skips the Microsoft Store stub).
function Find-Python {
    $candidates = @(
        @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"),
        @("python")
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $extra = @($c | Select-Object -Skip 1)
        try {
            $out = & $c[0] @extra -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out)) { return $out.Trim() }
        } catch { }
    }
    throw "No working Python >= 3.11 found. Install it from python.org (not the Microsoft Store stub)."
}

# Creates the venv if missing, and rebuilds it if it is broken
# (e.g. copied from another machine/user, base interpreter gone).
function Ensure-Venv([string]$Path) {
    $py = Join-Path $Path "Scripts\python.exe"
    $ok = $false
    if (Test-Path $py) {
        try { & $py --version *> $null; $ok = ($LASTEXITCODE -eq 0) } catch { $ok = $false }
    }
    if (-not $ok) {
        if (Test-Path $Path) {
            Write-Host "$Path is broken (copied from another machine?), recreating"
            Remove-Item -Recurse -Force $Path
        }
        Run $script:BasePython -m venv $Path
    }
}

Step "Checking Python (3.11+ required)"
$BasePython = Find-Python
Run $BasePython -c "import sys; print(sys.version)"

Step "Harness virtualenv (.venv)"
Ensure-Venv ".venv"
Run .venv\Scripts\python.exe -m pip install -q -e ".[dev]"

# NOTE: the target repository (targets/cosmicpython) is NOT set up here.
# Clone it yourself when needed, e.g.:
#   git clone https://github.com/cosmicpython/code targets/cosmicpython

Step "Target test environment (targets\.venv-cosmic, used by the local sandbox backend)"
Ensure-Venv "targets\.venv-cosmic"
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
Write-Host "  .venv\Scripts\harness projects"
Write-Host "  .venv\Scripts\harness baseline --project cosmic-feature"
Write-Host "  .venv\Scripts\harness start --project cosmic-feature --task-file examples/cosmic-feature/task.md"
Write-Host "  .venv\Scripts\harness baseline --config examples/cosmic-demo/harness.yaml"
Write-Host "  .venv\Scripts\harness run --config examples/cosmic-demo/harness.yaml --task-file examples/cosmic-demo/task.md"
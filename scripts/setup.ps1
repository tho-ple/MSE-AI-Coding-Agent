# One-shot, idempotent setup for the coding harness (Windows / PowerShell).
#
#   scripts\setup.ps1                          # everything, FH model (needs endpoint URL, API key + FH network/VPN)
#   scripts\setup.ps1 -Provider local          # everything, local Ollama model instead
#   scripts\setup.ps1 -Provider local -Model qwen2.5-coder:7b
#   scripts\setup.ps1 -SkipDocker              # no sandbox image (local backend only)
#
# The provider choice is written to .env (git-ignored). Re-run with the other -Provider to switch;
# an API key already in .env is kept.
param(
    [ValidateSet("fh", "local")]
    [string]$Provider = "fh",
    [string]$Model = "",
    [switch]$SkipDocker
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
# Runs a native command and returns its exit code. Success is judged by the exit code only:
# Windows PowerShell 5.1 would otherwise turn harmless stderr output (e.g. pip notices) into a
# terminating error when the script's output is redirected.
function Invoke-Native {
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $exe @rest 2>&1 | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { "$($_.TargetObject)" } else { "$_" } } | Out-Host } finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}
function Run {
    $code = Invoke-Native @args
    if ($code -ne 0) { throw "command failed ($code): $($args -join ' ')" }
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

$Commit = "14c84797ffa77255d53cf1a02fe6aafda2b68aeb"
Step "Target repository (git submodule targets\cosmicpython @ $($Commit.Substring(0, 7)))"
Run git submodule update --init targets/cosmicpython
$head = "$(git -C targets/cosmicpython rev-parse HEAD)".Trim()
if ($head -ne $Commit) { throw "targets/cosmicpython is at $head, expected $Commit" }
Write-Host "ok: $head"

Step "Target test environment (targets\.venv-cosmic, used by the local sandbox backend)"
Ensure-Venv "targets\.venv-cosmic"
Run targets\.venv-cosmic\Scripts\python.exe -m pip install -q -r sandbox\requirements-cosmic.txt

Step "Sandbox image (container engine: docker, podman, or either one inside WSL)"
if ($SkipDocker) {
    Write-Host "skipped (-SkipDocker)"
} else {
    $code = Invoke-Native .venv\Scripts\harness.exe build-image --config examples/cosmic-feature/harness.yaml
    if ($code -ne 0) {
        Write-Warning "no container engine / image build failed - only the local (non-isolated) backend is available. Use --sandbox local."
    }
}

# Writes KEY=VALUE into .env (replacing an existing line, or appending). Never prints values.
function Set-EnvLine([string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path ".env") { $lines = @(Get-Content ".env" -Encoding utf8) }
    $found = $false
    $lines = @($lines | ForEach-Object {
        if ($_ -match "^\s*$([regex]::Escape($Key))\s*=") { $found = $true; "$Key=$Value" } else { $_ }
    })
    if (-not $found) { $lines += "$Key=$Value" }
    [IO.File]::WriteAllLines((Join-Path $Root ".env"), [string[]]$lines)
}
function Get-EnvLine([string]$Key) {
    if (-not (Test-Path ".env")) { return "" }
    $m = Select-String -Path ".env" -Pattern "^\s*$([regex]::Escape($Key))\s*=(.*)$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim().Trim('"', "'") } else { return "" }
}

$ModelReady = $true
if ($Provider -eq "fh") {
    Step "Model: FH endpoint (OpenAI-compatible)"
    if (-not $Model) { $Model = "Qwen/Qwen3.6-35B-A3B" }
    Set-EnvLine "HARNESS_PROVIDER" "openai"
    Set-EnvLine "HARNESS_MODEL" $Model
    $url = Get-EnvLine "OPENAI_BASE_URL"
    if (-not $url -or $url -eq "YOUR_ENDPOINT_URL") {
        Set-EnvLine "OPENAI_BASE_URL" ""
        $ModelReady = $false
        Write-Warning "Put the FH endpoint URL into .env (OPENAI_BASE_URL=https://.../v1)"
    }
    $key = Get-EnvLine "OPENAI_API_KEY"
    if (-not $key -or $key -eq "YOUR_API_KEY") {
        Set-EnvLine "OPENAI_API_KEY" ""
        $ModelReady = $false
        Write-Warning "Put your FH API key into .env (OPENAI_API_KEY=...), then run: .venv\Scripts\harness model-check"
    }
    Write-Host "provider openai, model $Model (.env). The endpoint is reachable only from the FH network / VPN."
} else {
    Step "Model: local Ollama"
    if (-not $Model) { $Model = "qwen2.5-coder:14b" }
    Set-EnvLine "HARNESS_PROVIDER" "ollama"
    Set-EnvLine "HARNESS_MODEL" $Model
    Set-EnvLine "OLLAMA_HOST" "http://localhost:11434"
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        Run ollama pull $Model
    } else {
        $ModelReady = $false
        Write-Warning "ollama not found - install it from https://ollama.com, then run: ollama pull $Model"
    }
}

Step "Self-test (no model needed)"
Run .venv\Scripts\python.exe -m pytest -q

Step "Model check (one tool call)"
if ($ModelReady) {
    $code = Invoke-Native .venv\Scripts\harness.exe model-check
    if ($code -ne 0) { Write-Warning "model check failed - see above (FH: VPN connected? key valid?  local: ollama running?)" }
} else {
    Write-Host "skipped (model not ready yet, see warning above)"
}

Write-Host "`nSetup complete. Try:" -ForegroundColor Green
Write-Host "  .venv\Scripts\harness demo                      # no model needed: full run incl. one fix round"
Write-Host "  .venv\Scripts\harness start --project cosmic-feature --task-file examples/cosmic-feature/task.md"

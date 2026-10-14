import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from coding_harness.config import CheckSpec, LimitSettings, SandboxSettings, Settings, TargetSettings
from coding_harness.sandbox import LocalSandbox
from coding_harness.tools import ToolRegistry
from coding_harness.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures"
PY = "{python} -m pytest -c {checks}/pytest.ini --rootdir {checks}"


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)  # git marks object files read-only on Windows
    func(path)


@pytest.fixture
def settings(tmp_path):
    """Settings for the sample shop repo, using the local sandbox and this interpreter."""
    yield Settings(
        target=TargetSettings(repo=FIXTURES / "sample_repo", writable=["shop/*.py", "tests/*.py"]),
        sandbox=SandboxSettings(backend="local", python=sys.executable, env={"PYTHONPATH": "."}),
        limits=LimitSettings(max_actions=10, max_denied=3, max_retries=2, max_output_chars=2000, command_timeout_s=60),
        commands={"tests": "{python} -m pytest tests -q -p no:cacheprovider",
                  "fail": "{python} -c \"import sys; print('boom'); sys.exit(3)\"",
                  "noisy": "{python} -c \"print('x' * 50000)\""},
        checks=[CheckSpec("acceptance", f"{PY} {{checks}}/test_acceptance.py -q"),
                CheckSpec("regression", f"{PY} {{checks}}/baseline/tests -q")],
        checks_dir=FIXTURES / "sample_checks",
        baseline_copy=["tests"],
        runs_dir=tmp_path / "runs",
    )
    shutil.rmtree(tmp_path / "runs", onerror=_force_remove)


@pytest.fixture
def env(settings):
    ws, dirs = Workspace.create(settings, "t")
    sandbox = LocalSandbox(settings.sandbox, dirs.workspace, dirs.checks)
    tools = ToolRegistry(ws, sandbox, settings.limits, settings.commands)
    return ws, dirs, sandbox, tools

"""A nonzero exit code stays visible, and a failing check is never reported as success."""
import shutil

from langchain_core.messages import ToolMessage

from coding_harness.app import run_task
from coding_harness.llm import ScriptedModel, ai, tool_call


def test_nonzero_exit_visible_to_model(env):
    _, _, _, tools = env
    out = tools.call("run_command", {"name": "fail"})
    assert out.error
    assert "exit code 3" in out.content and "boom" in out.content
    assert out.command.exit_code == 3


def test_model_claims_success_but_checks_fail(settings):
    model = ScriptedModel([
        ai(tool_call("run_command", {"name": "fail"})),
        ai(tool_call("finish", {"summary": "All tests pass, bug fixed!"})),
    ])
    result = run_task(settings, "fix the discount bug", model=model, run_id="claim")
    s = result.state
    assert s["finished"] is True
    tool_msgs = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert "exit code 3" in tool_msgs[0].content and tool_msgs[0].status == "error"
    acc = next(c for c in s["checks"] if c["name"] == "acceptance")
    assert acc["status"] == "failed" and acc["exit_code"] != 0
    assert "assert" in acc["output"]  # failure output is kept
    assert result.outcome == "checks_failed"


def test_unavailable_check_is_not_success(settings):
    settings.sandbox.backend = "docker"
    settings.sandbox.docker_image = "definitely-not-an-image-xyz:never"
    model = ScriptedModel([ai(tool_call("finish", {"summary": "done"}))])
    result = run_task(settings, "x", model=model, run_id="unavail")
    statuses = {c["status"] for c in result.state["checks"]}
    assert statuses <= {"unavailable", "failed"}
    if not shutil.which("docker"):
        assert statuses == {"unavailable"}
    assert result.outcome == "checks_failed"


def test_no_checks_is_unverified(settings):
    settings.checks = []
    model = ScriptedModel([ai(tool_call("finish", {"summary": "done"}))])
    assert run_task(settings, "x", model=model, run_id="nochk").outcome == "unverified"

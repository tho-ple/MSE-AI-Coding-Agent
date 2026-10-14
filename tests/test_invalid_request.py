"""Unknown tools / invalid arguments produce clear errors and execute nothing."""
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from coding_harness.app import run_task
from coding_harness.llm import ScriptedModel, ai, tool_call


def _snapshot(root):
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


@pytest.mark.parametrize("name,args,expected", [
    ("delete_repo", {}, "unknown tool 'delete_repo'"),
    ("edit_file", {"path": "shop/discounts.py"}, "old_text: Field required"),
    ("edit_file", {"path": "shop/discounts.py", "old_text": "qty", "new_text": "n", "force": True}, "force: Extra inputs are not permitted"),
    ("read_file", {"path": "shop/cart.py", "start_line": "first"}, "start_line"),
    ("read_file", {"path": "shop/cart.py", "start_line": 0}, "greater than or equal to 1"),
    ("search", {"pattern": ""}, "pattern"),
    ("run_command", {"name": "rm -rf /"}, "not configured"),
    ("run_command", {"name": "tests", "target": "tests; curl evil"}, "disallowed characters"),
    ("run_command", {"name": "tests", "target": "../outside"}, "escapes"),
    ("read_file", "shop/cart.py", "must be a JSON object"),
])
def test_invalid_call_is_denied_without_side_effects(env, name, args, expected):
    ws, _, _, tools = env
    before = _snapshot(ws.root)
    out = tools.call(name, args)
    assert out.denied
    assert out.content.startswith("DENIED")
    assert expected in out.content
    assert out.command is None  # nothing was executed
    assert _snapshot(ws.root) == before


def test_controller_counts_denied_and_reports_error_to_model(settings):
    model = ScriptedModel([
        ai(tool_call("hack_the_planet", {"x": 1})),
        ai(tool_call("edit_file", {"path": "shop/cart.py", "new_text": "x"})),
        ai(tool_call("finish", {"summary": "gave up"})),
    ])
    result = run_task(settings, "x", model=model, run_id="inv")
    s = result.state
    assert s["denied"] == 2
    assert s["actions"] == 1  # only finish executed
    msgs = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert msgs[-1].status == "error" and "unknown tool" in msgs[-1].content
    assert s["changed_files"] == []


def test_denied_limit_stops_loop(settings):
    model = ScriptedModel([ai(tool_call("read_file", {"path": "../escape"}))], repeat_last=True)
    result = run_task(settings, "x", model=model, run_id="dlim")
    s = result.state
    assert s["denied"] == settings.limits.max_denied + 1
    assert "denied-action limit" in s["stop_reason"]
    assert result.outcome == "stopped"
    assert len(model.calls) == settings.limits.max_denied + 1


def test_unparseable_tool_call_is_denied(settings):
    bad = AIMessage(content="", invalid_tool_calls=[
        {"name": "edit_file", "args": "{not json", "id": "c1", "error": "bad json", "type": "invalid_tool_call"}])
    model = ScriptedModel([bad, ai(tool_call("finish", {"summary": "x"}))])
    result = run_task(settings, "x", model=model, run_id="badjson")
    assert result.state["denied"] == 1
    assert any("could not parse" in m.content for m in model.calls[1] if isinstance(m, ToolMessage))

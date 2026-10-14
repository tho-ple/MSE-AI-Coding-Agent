"""Controller: scripted model replies drive the LangGraph loop; tool results go back to the model."""
from langchain_core.messages import ToolMessage

from coding_harness.app import run_task
from coding_harness.llm import ScriptedModel, ai, tool_call


def test_scripted_reply_calls_tool_and_receives_result(settings):
    model = ScriptedModel([
        ai(tool_call("read_file", {"path": "shop/discounts.py"})),
        ai(tool_call("finish", {"summary": "looked around"})),
    ])
    result = run_task(settings, "inspect discounts", model=model, run_id="ctl")

    # The second model call saw the tool result of the first call.
    second_context = model.calls[1]
    tool_msgs = [m for m in second_context if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "BULK_THRESHOLD = 10" in tool_msgs[0].content
    assert tool_msgs[0].tool_call_id == second_context[-2].tool_calls[0]["id"]

    s = result.state
    assert s["actions"] == 2 and s["denied"] == 0 and s["finished"]
    # Final checks always run; no change was made, so the acceptance check still fails.
    assert {c["name"]: c["status"] for c in s["checks"]} == {"acceptance": "failed", "regression": "passed"}
    assert result.outcome == "checks_failed"
    assert s["changed_files"] == [] and s["diff"] == ""


def test_multiple_tool_calls_in_one_reply(settings):
    model = ScriptedModel([
        ai(tool_call("search", {"pattern": "UnknownSku"}), tool_call("list_files", {"path": "shop"})),
        ai(tool_call("finish", {"summary": "done"})),
    ])
    result = run_task(settings, "explore", model=model, run_id="multi")
    tool_msgs = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 2
    assert result.state["actions"] == 3


def test_reply_without_tool_call_is_nudged_then_retried(settings):
    model = ScriptedModel([
        ai(content="I think I am done."),
        ai(tool_call("finish", {"summary": "ok"})),
    ])
    result = run_task(settings, "x", model=model, run_id="nudge")
    assert result.state["retries"] == 1
    assert "no tool call" in model.calls[1][-1].content
    assert result.state["finished"]


def test_artifacts_written(settings):
    model = ScriptedModel([ai(tool_call("finish", {"summary": "nothing"}))])
    result = run_task(settings, "x", model=model, run_id="art")
    for name in ("report.json", "diff.patch", "events.jsonl"):
        assert (result.dirs.root / name).exists()


def test_graph_has_expected_nodes():
    from coding_harness.graph import Harness
    g = Harness(settings=None, workspace=None, sandbox=None, model=None, tools=None).build().get_graph()
    assert {"agent", "act", "verify", "report"} <= set(g.nodes)

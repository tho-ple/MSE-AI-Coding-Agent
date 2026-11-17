"""DemoModel picks its reply from the conversation, so a resumed demo continues at the right turn."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coding_harness.demo import REFERENCE_REPLIES, DemoModel


def test_reply_follows_the_number_of_assistant_turns():
    m = DemoModel()
    first = m.invoke([HumanMessage("task")])
    assert [c["name"] for c in first.tool_calls] == [c["name"] for c in REFERENCE_REPLIES[0].tool_calls]
    history = [HumanMessage("task"), first, *[ToolMessage("ok", tool_call_id=c["id"]) for c in first.tool_calls]]
    assert m.invoke(history).tool_calls[0]["name"] == REFERENCE_REPLIES[1].tool_calls[0]["name"]
    # a fresh instance (e.g. after `harness resume`) gives the same answer for the same history
    assert DemoModel().invoke(history).tool_calls[0]["name"] == REFERENCE_REPLIES[1].tool_calls[0]["name"]


def test_script_end_is_an_error_not_a_silent_repeat():
    history = [HumanMessage("task")] + [AIMessage("x")] * len(REFERENCE_REPLIES)
    with pytest.raises(RuntimeError, match="only"):
        DemoModel().invoke(history)


def test_reference_has_a_deliberate_first_finish_and_a_fix():
    names = [c["name"] for r in REFERENCE_REPLIES for c in r.tool_calls]
    assert names.count("finish") == 2
    assert {"find_symbol", "find_references", "module_deps"} <= set(names)

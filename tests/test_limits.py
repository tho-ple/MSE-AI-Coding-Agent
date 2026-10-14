"""Action, output and retry limits."""
from coding_harness.app import run_task
from coding_harness.limits import truncate
from coding_harness.llm import ScriptedModel, ai, tool_call


def test_repeating_reply_hits_action_limit(settings):
    model = ScriptedModel([ai(tool_call("list_files", {"path": "."}))], repeat_last=True)
    events = []
    result = run_task(settings, "loop forever", model=model, run_id="alim", on_event=events.append)
    s = result.state
    executed = [e for e in events if e["kind"] == "action"]
    assert len(executed) == settings.limits.max_actions == s["actions"]
    assert "action limit reached" in s["stop_reason"]
    assert result.outcome == "stopped"
    # One extra model call produced the request that was refused; nothing after that.
    assert len(model.calls) == settings.limits.max_actions + 1
    # Checks still ran after the stop.
    assert len(s["checks"]) == 2


def test_action_limit_inside_single_reply(settings):
    settings.limits.max_actions = 2
    model = ScriptedModel([ai(*[tool_call("list_files", {"path": "."}) for _ in range(5)])])
    result = run_task(settings, "x", model=model, run_id="alim2")
    assert result.state["actions"] == 2
    assert result.outcome == "stopped"


def test_truncate_marks_shortened_output():
    text, cut = truncate("a" * 1000 + "END", 100)
    assert cut and "output truncated by harness" in text and text.endswith("END")
    assert len(text) < 200
    same, cut2 = truncate("short", 100)
    assert same == "short" and not cut2


def test_large_command_output_is_bounded(env, settings):
    _, _, _, tools = env
    out = tools.call("run_command", {"name": "noisy"})
    assert out.truncated
    assert "output truncated by harness" in out.content
    assert len(out.content) <= settings.limits.max_output_chars + 200


def test_large_file_read_is_bounded(env, settings):
    ws, _, _, tools = env
    (ws.root / "shop" / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(20000)))
    settings.limits.max_read_chars = 3000
    out = tools.call("read_file", {"path": "shop/big.py"})
    assert out.truncated and "output truncated by harness" in out.content
    assert len(out.content) <= 3300


def test_search_results_bounded(env, settings):
    ws, _, _, tools = env
    (ws.root / "shop" / "many.py").write_text("\n".join("hit = 1" for _ in range(500)))
    out = tools.call("search", {"pattern": "hit"})
    assert out.truncated and f"truncated by harness at {settings.limits.max_search_results}" in out.content


def test_check_output_is_bounded(settings):
    from coding_harness.config import CheckSpec
    settings.checks = [CheckSpec("noisy", "{python} -c \"print('y' * 50000)\"")]
    result = run_task(settings, "x", model=ScriptedModel([ai(tool_call("finish", {"summary": "s"}))]), run_id="chkout")
    [c] = result.state["checks"]
    assert c["truncated"] and len(c["output"]) <= settings.limits.max_output_chars + 200


def test_model_errors_hit_retry_limit(settings):
    model = ScriptedModel([ConnectionError("ollama down")], repeat_last=True)
    result = run_task(settings, "x", model=model, run_id="retry")
    s = result.state
    assert s["retries"] == settings.limits.max_retries + 1
    assert "retry limit" in s["stop_reason"]
    assert s["actions"] == 0
    assert result.outcome == "stopped"

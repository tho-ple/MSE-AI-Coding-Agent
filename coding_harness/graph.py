"""The controller, expressed as a LangGraph state machine.

    START -(resume router)-> agent -> act -> (agent | verify) ... verify -> (fix -> agent | report) -> END

* ``agent``  sends the bounded context view to the model and records its reply.
* ``act``    validates every requested tool call and executes the allowed ones; enforces the
             action / denied limits; journals each call before and after it runs.
* ``verify`` always runs: the configured checks are executed in the sandbox, whatever the model claims.
* ``fix``    edit-test-fix loop: feeds failed checks back to the model (bounded by ``max_fix_rounds``).
* ``report`` collects changed files and the diff, and decides the outcome.

The router at START continues a saved state at the right node: every node records ``last_node``,
and the same routing functions that connect the nodes decide where a resumed run continues.
After each node the full state is checkpointed (``Recorder.checkpoint``).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .config import Settings
from .context import ContextManager, wrap_untrusted
from .limits import truncate
from .llm import ChatModel
from .prompts import NO_TOOL_CALL_NUDGE, SYSTEM_PROMPT
from .sandbox import Sandbox
from .session import SIDE_EFFECT_TOOLS, CancelToken, Cancelled, Paused
from .tools import ToolOutcome, ToolRegistry
from .workspace import Workspace


class HarnessState(TypedDict, total=False):
    task: str
    messages: Annotated[list[AnyMessage], add_messages]
    actions: int  # executed tool calls
    denied: int  # rejected tool calls
    retries: int  # model errors + replies without tool calls
    model_calls: int
    finished: bool  # the model called finish (in the current round)
    summary: str
    stop_reason: str | None  # set when the harness stopped the loop
    checks: list[dict]  # latest final-check results
    check_history: list[dict]  # one entry per verification round
    fix_round: int
    failure_sig: str | None
    prev_failure_sig: str | None
    end_reason: str
    plan: list[dict]
    selected_files: list[dict]
    errors: list[str]
    last_node: str | None
    changed_files: list[str]
    diff: str
    outcome: str


Event = dict[str, Any]

# Tool results written by the harness itself; everything else is wrapped as untrusted data.
_TRUSTED_RESULTS = {"finish", "update_plan"}


class Recorder:
    """Where the controller records steps. The default keeps them in memory; the app persists them."""

    def __init__(self):
        self.done: dict[str, dict] = {}
        self.started: set[str] = set()
        self.last_state: HarnessState = {}

    def journal(self, type_: str, **data) -> None:
        pass

    def completed(self, call_id: str) -> dict | None:
        return self.done.get(call_id)

    def is_uncertain(self, call_id: str) -> bool:
        return call_id in self.started and call_id not in self.done

    def action_start(self, call: dict, n: int) -> None:
        self.started.add(call["id"])

    def action_done(self, call: dict, outcome: ToolOutcome, side_effect: bool) -> None:
        self.done[call["id"]] = outcome_record(call, outcome)

    def checkpoint(self, state: HarnessState) -> None:
        self.last_state = state


def outcome_record(call: dict, o: ToolOutcome) -> dict:
    return {"call_id": call["id"], "tool": call["name"], "content": o.content, "denied": o.denied, "error": o.error,
            "finished": o.finished, "truncated": o.truncated, "cancelled": o.cancelled, "plan": o.plan}


def outcome_from_record(rec: dict) -> ToolOutcome:
    return ToolOutcome(rec["tool"], rec["content"], denied=rec.get("denied", False), error=rec.get("error", False),
                       finished=rec.get("finished", False), truncated=rec.get("truncated", False),
                       cancelled=rec.get("cancelled", False), plan=rec.get("plan"))


@dataclass
class Harness:
    settings: Settings
    workspace: Workspace
    sandbox: Sandbox
    model: ChatModel
    tools: ToolRegistry
    on_event: Callable[[Event], None] = field(default=lambda e: None)
    recorder: Recorder | None = None
    cancel: CancelToken | None = None
    context: ContextManager | None = None

    def __post_init__(self):
        self.recorder = self.recorder or Recorder()

    def emit(self, kind: str, **data) -> None:
        self.on_event({"kind": kind, **data})

    def _check_cancel(self, where: str) -> None:
        if self.cancel is not None and self.cancel.is_set():
            self.emit("cancelled", during=where)
            raise Cancelled(f"cancelled by user during {where}")

    def _ctx(self) -> ContextManager:
        if self.context is None:
            self.context = ContextManager(self.settings)
        return self.context

    # ------------------------------------------------------------------ nodes
    def agent(self, state: HarnessState) -> dict:
        self._check_cancel("model request")
        lim = self.settings.limits
        view, stats = self._ctx().view(state["messages"])
        self.emit("context", **stats.as_dict())
        self.emit("thinking", step=state.get("actions", 0))
        calls = state.get("model_calls", 0) + 1
        try:
            reply = self._invoke(view)
        except Cancelled:
            raise
        except Exception as e:  # network error, malformed response, ...
            retries = state.get("retries", 0) + 1
            err = f"{type(e).__name__}: {e}"
            self.emit("model_error", error=err, retries=retries)
            self.recorder.journal("model_error", error=err, retries=retries)
            update: dict = {"retries": retries, "model_calls": calls, "last_node": "agent",
                            "errors": state.get("errors", []) + [f"model error: {err}"[:500]]}
            if retries > lim.max_retries:
                update["stop_reason"] = f"retry limit reached ({lim.max_retries}) after model error: {err}"
            return update

        tool_calls = [{"name": c["name"], "args": c["args"]} for c in reply.tool_calls]
        self.emit("model_reply", content=_text(reply.content), tool_calls=tool_calls)
        self.recorder.journal("model_reply", n=calls, content=_text(reply.content)[:500], tool_calls=_short_args(tool_calls))
        if not reply.tool_calls and not getattr(reply, "invalid_tool_calls", None):
            retries = state.get("retries", 0) + 1
            update = {"messages": [reply, HumanMessage(NO_TOOL_CALL_NUDGE, additional_kwargs={
                "harness_kind": "nudge", "summary": "reply without tool call (nudged)"})],
                "retries": retries, "model_calls": calls, "last_node": "agent"}
            if retries > lim.max_retries:
                update["stop_reason"] = f"retry limit reached ({lim.max_retries}): model kept replying without tool calls"
            return update
        return {"messages": [reply], "model_calls": calls, "last_node": "agent"}

    def _invoke(self, messages: list) -> AIMessage:
        """Model request that can be cancelled: it runs in a worker thread; on cancel its reply is discarded."""
        if self.cancel is None:
            return self.model.invoke(messages)
        box: dict = {}

        def work():
            try:
                box["reply"] = self.model.invoke(messages)
            except BaseException as e:  # noqa: BLE001 - re-raised in the controller thread
                box["error"] = e

        t = threading.Thread(target=work, name="model-request", daemon=True)
        t.start()
        while t.is_alive():
            t.join(0.1)
            if t.is_alive() and self.cancel.is_set():
                self.emit("cancelled", during="model request")
                raise Cancelled("cancelled by user during model request (reply will be discarded)")
        if "error" in box:
            raise box["error"]
        return box["reply"]

    def act(self, state: HarnessState) -> dict:
        lim = self.settings.limits
        reply: AIMessage = state["messages"][-1]
        actions, denied = state.get("actions", 0), state.get("denied", 0)
        out: list[ToolMessage] = []
        finished, summary, stop = False, state.get("summary", ""), None
        plan = state.get("plan", [])

        # Tool calls the provider could not even parse (bad JSON) are denied.
        for bad in getattr(reply, "invalid_tool_calls", None) or []:
            denied += 1
            msg = f"DENIED: could not parse tool call {bad.get('name')!r}: {bad.get('error') or 'invalid JSON arguments'}"
            self.emit("denied", tool=bad.get("name"), reason=msg, denied=denied)
            out.append(ToolMessage(msg, tool_call_id=bad.get("id") or "invalid", status="error"))

        for call in reply.tool_calls:
            if stop or finished:
                out.append(ToolMessage("SKIPPED: the harness stopped the loop before this call.", tool_call_id=call["id"], status="error"))
                continue
            if call["name"] != "finish" and actions >= lim.max_actions:
                stop = f"action limit reached ({lim.max_actions} actions)"
                self.emit("limit", reason=stop)
                out.append(ToolMessage(f"STOPPED: {stop}. No further actions are executed.", tool_call_id=call["id"], status="error"))
                continue

            fresh = False
            rec = self.recorder.completed(call["id"])
            if rec is not None:  # already executed before an interruption: reuse, never replay
                outcome = outcome_from_record(rec)
                self.emit("reused", tool=call["name"], call_id=call["id"])
            elif self.recorder.is_uncertain(call["id"]):
                raise Paused(f"uncertain interrupted action {call['name']} ({call['id']}) - not replayed")
            else:
                self._check_cancel(f"before {call['name']}")
                n = actions + 1
                self.recorder.action_start(call, n)
                outcome = self.tools.call(call["name"], call["args"], cancel=self.cancel)
                self.recorder.action_done(call, outcome, call["name"] in SIDE_EFFECT_TOOLS and not outcome.denied)
                fresh = True

            if outcome.denied:
                denied += 1
                self.emit("denied", tool=call["name"], args=call["args"], reason=outcome.content, denied=denied)
                if denied > lim.max_denied:
                    stop = f"denied-action limit reached ({lim.max_denied} denied requests)"
                    self.emit("limit", reason=stop)
            else:
                actions += 1
                self.emit("action", n=actions, tool=call["name"], args=_short_args(call["args"]),
                          error=outcome.error, truncated=outcome.truncated, result=outcome.content)
            if outcome.finished:
                finished, summary = True, call["args"].get("summary", "")
            if outcome.plan is not None:
                plan = outcome.plan
            trusted = outcome.denied or call["name"] in _TRUSTED_RESULTS
            content = outcome.content if trusted else wrap_untrusted(f"{call['name']} result", outcome.content)
            out.append(ToolMessage(content, tool_call_id=call["id"], status="error" if (outcome.denied or outcome.error) else "success"))
            if outcome.cancelled and fresh:
                self.emit("cancelled", during=f"{call['name']} (process tree killed)")
                raise Cancelled(f"cancelled by user during {call['name']}")

        return {"messages": out, "actions": actions, "denied": denied, "finished": finished,
                "summary": summary, "stop_reason": stop, "plan": plan, "last_node": "act"}

    def verify(self, state: HarnessState) -> dict:
        self._check_cancel("final checks")
        if state.get("stop_reason"):
            self.emit("stopped", reason=state["stop_reason"])
        lim = self.settings.limits
        changed_py = [line.split()[-1] for line in self.workspace.changed_files()
                      if not line.startswith("D") and line.endswith(".py")]
        results = []
        for check in self.settings.checks:
            self._check_cancel(f"check {check.name}")
            timeout = check.timeout_s or lim.command_timeout_s
            template = check.command
            if "{changed_py}" in template:
                if not changed_py:
                    self.emit("check_start", name=check.name, command=template)
                    r = {"name": check.name, "type": check.kind, "command": template, "status": "skipped",
                         "exit_code": None, "duration_s": 0.0, "output": "no changed Python files to check",
                         "truncated": False}
                    self.emit("check_done", **r)
                    results.append(r)
                    continue
                files = " ".join(self.sandbox.quote(p).replace("{", "{{").replace("}", "}}") for p in changed_py)
                template = template.replace("{changed_py}", files)
            cmd = self.sandbox.render(template)
            self.emit("check_start", name=check.name, command=cmd)
            if check.probe:
                pr = self.sandbox.run(self.sandbox.render(check.probe), timeout, cancel=self.cancel)
                if pr.cancelled:
                    self._check_cancel(f"check {check.name}")
                if not pr.ok:
                    out, cut = truncate(f"probe failed ({check.probe}):\n{pr.error or pr.output}", lim.max_output_chars)
                    r = {"name": check.name, "type": check.kind, "command": cmd, "status": "unavailable",
                         "exit_code": pr.exit_code, "duration_s": round(pr.duration_s, 2), "output": out, "truncated": cut}
                    self.emit("check_done", **r)
                    results.append(r)
                    continue
            res = self.sandbox.run(cmd, timeout, cancel=self.cancel)
            if res.cancelled:
                self.emit("cancelled", during=f"check {check.name}")
                raise Cancelled(f"cancelled by user during check {check.name}")
            if res.error:
                status = "unavailable"
            elif res.timed_out:
                status = "timeout"
            else:
                status = "passed" if res.exit_code == 0 else "failed"
            output, cut = truncate(res.output if not res.error else res.error, lim.max_output_chars)
            r = {"name": check.name, "type": check.kind, "command": cmd, "status": status, "exit_code": res.exit_code,
                 "duration_s": round(res.duration_s, 2), "output": output, "truncated": cut}
            self.emit("check_done", **r)
            results.append(r)
        rnd = state.get("fix_round", 0)
        brief = [{"name": r["name"], "status": r["status"], "exit_code": r["exit_code"]} for r in results]
        self.recorder.journal("checks", round=rnd, results=brief)
        failing = sorted(r["name"] for r in results if r["status"] in ("failed", "timeout"))
        sig = hashlib.sha256((json.dumps(failing) + self.workspace.diff()).encode()).hexdigest()[:16]
        return {"checks": results, "check_history": state.get("check_history", []) + [{"round": rnd, "checks": brief}],
                "failure_sig": sig, "last_node": "verify"}

    def fix(self, state: HarnessState) -> dict:
        lim = self.settings.limits
        rnd = state.get("fix_round", 0) + 1
        failing = [c for c in state["checks"] if c["status"] in ("failed", "timeout")]
        names = ", ".join(c["name"] for c in failing)
        parts = [f"HARNESS FEEDBACK (trusted): the final checks failed after you called finish - fix round {rnd} "
                 f"of {lim.max_fix_rounds}.",
                 "Failed checks: " + ", ".join(f"{c['name']} ({c['status']}, exit {c['exit_code']})" for c in failing),
                 "Find the cause in the check output below, fix the code (only in the allowed files), run the tests "
                 "with run_command, then call finish again. The TASK message still applies."]
        for c in failing:
            text, _ = truncate(c["output"] or "(no output)", lim.fix_feedback_chars)
            parts.append(wrap_untrusted(f"output of check {c['name']}", text))
        self.emit("fix_round", round=rnd, failed=[c["name"] for c in failing])
        self.recorder.journal("fix_round", round=rnd, failed=[c["name"] for c in failing])
        msg = HumanMessage("\n\n".join(parts), additional_kwargs={
            "harness_kind": "fix_feedback", "summary": f"fix round {rnd}: final checks failed ({names})"})
        return {"messages": [msg], "fix_round": rnd, "finished": False,
                "prev_failure_sig": state.get("failure_sig"), "last_node": "fix"}

    def report(self, state: HarnessState) -> dict:
        diff = self.workspace.diff()
        changed = self.workspace.changed_files()
        checks = state.get("checks", [])
        passed = [c for c in checks if c["status"] == "passed"]
        all_ok = bool(passed) and all(c["status"] in ("passed", "skipped") for c in checks)
        if state.get("stop_reason"):
            outcome = "stopped"
        elif not state.get("finished"):
            outcome = "incomplete"
        elif not passed and all(c["status"] == "skipped" for c in checks):
            outcome = "unverified"  # nothing was checked: never reported as success
        else:
            outcome = "success" if all_ok else "checks_failed"
        _, reason = self._loop_end(state)
        self.emit("report", outcome=outcome, changed_files=changed, end_reason=reason)
        return {"diff": diff, "changed_files": changed, "outcome": outcome, "end_reason": reason, "last_node": "report"}

    # ----------------------------------------------------------------- routing
    def _loop_end(self, state: HarnessState) -> tuple[str, str]:
        """The stopping rule of the edit-test-fix loop: ("fix" | "report", reason)."""
        if state.get("stop_reason"):
            return "report", f"stopped by harness: {state['stop_reason']}"
        if not state.get("finished"):
            return "report", "model did not call finish"
        checks = state.get("checks", [])
        failing = [c["name"] for c in checks if c["status"] in ("failed", "timeout")]
        unavailable = [c["name"] for c in checks if c["status"] == "unavailable"]
        if not failing and not unavailable:
            return "report", "all checks passed" if any(c["status"] == "passed" for c in checks) else "no checks ran"
        if unavailable:
            return "report", f"check unavailable ({', '.join(unavailable)}): cannot be fixed by editing code"
        rounds = self.settings.limits.max_fix_rounds
        if state.get("fix_round", 0) >= rounds:
            return "report", f"fix rounds exhausted ({rounds}); still failing: {', '.join(failing)}"
        if state.get("prev_failure_sig") and state.get("failure_sig") == state.get("prev_failure_sig"):
            return "report", "no progress: same failing checks and unchanged diff as the previous round"
        return "fix", f"checks failed: {', '.join(failing)}"

    def after_verify(self, state: HarnessState) -> str:
        return self._loop_end(state)[0]

    @staticmethod
    def after_agent(state: HarnessState) -> str:
        if state.get("stop_reason"):
            return "verify"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and (last.tool_calls or getattr(last, "invalid_tool_calls", None)):
            return "act"
        return "agent"  # model error or nudge: ask again

    @staticmethod
    def after_act(state: HarnessState) -> str:
        return "verify" if state.get("stop_reason") or state.get("finished") else "agent"

    def route_start(self, state: HarnessState) -> str:
        """Fresh run -> agent. Resumed run -> wherever the routing after the last finished node points."""
        last = state.get("last_node")
        routes = {None: lambda s: "agent", "agent": self.after_agent, "act": self.after_act,
                  "verify": self.after_verify, "fix": lambda s: "agent", "report": lambda s: END}
        return routes[last](state)

    def build(self):
        g = StateGraph(HarnessState)
        g.add_node("agent", self.agent)
        g.add_node("act", self.act)
        g.add_node("verify", self.verify)
        g.add_node("fix", self.fix)
        g.add_node("report", self.report)
        g.add_conditional_edges(START, self.route_start,
                                {"agent": "agent", "act": "act", "verify": "verify", "fix": "fix", END: END})
        g.add_conditional_edges("agent", self.after_agent, {"act": "act", "agent": "agent", "verify": "verify"})
        g.add_conditional_edges("act", self.after_act, {"agent": "agent", "verify": "verify"})
        g.add_conditional_edges("verify", self.after_verify, {"fix": "fix", "report": "report"})
        g.add_edge("fix", "agent")
        g.add_edge("report", END)
        return g.compile()

    def initial_state(self, task: str) -> HarnessState:
        system = SYSTEM_PROMPT.format(
            writable=", ".join(self.settings.target.writable),
            commands=", ".join(f"{k} ({v})" for k, v in self.settings.commands.items()) or "none",
        )
        messages, selected = self._ctx().initial_messages(system, task, self.tools.index)
        self.emit("context_selected", files=selected)
        return {"task": task, "messages": messages, "actions": 0, "denied": 0, "retries": 0, "model_calls": 0,
                "finished": False, "summary": "", "stop_reason": None, "checks": [], "check_history": [],
                "fix_round": 0, "failure_sig": None, "prev_failure_sig": None, "end_reason": "", "plan": [],
                "selected_files": selected, "errors": [], "last_node": None,
                "changed_files": [], "diff": "", "outcome": ""}

    def run(self, state: HarnessState) -> HarnessState:
        """Run (or continue) the graph, checkpointing after every node. Raises Cancelled / Paused."""
        lim = self.settings.limits
        # Upper bound on graph steps; the harness limits stop the loop long before this.
        recursion = 4 * (lim.max_actions + lim.max_denied + lim.max_retries) + 12 * (lim.max_fix_rounds + 1) + 20
        last = state
        for snapshot in self.build().stream(state, {"recursion_limit": recursion}, stream_mode="values"):
            last = snapshot
            self.recorder.checkpoint(last)
        return last


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content)[:2000]


def _short_args(args: Any) -> Any:
    if isinstance(args, list):
        return [_short_args(a) for a in args]
    if not isinstance(args, dict):
        return args
    return {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else _short_args(v) if isinstance(v, (dict, list)) else v)
            for k, v in args.items()}

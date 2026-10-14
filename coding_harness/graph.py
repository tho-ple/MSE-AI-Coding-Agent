"""The controller, expressed as a LangGraph state machine.

    START -> agent -> act -> (agent | verify) ... verify -> report -> END
                 \\-> verify (on model failure / retry limit)

* ``agent``  sends the context to the model and records its reply.
* ``act``    validates every requested tool call and executes the allowed ones.
             It enforces the action / denied / retry limits.
* ``verify`` always runs: the configured checks are executed in the sandbox,
             whatever the model claims.
* ``report`` collects changed files and the diff, and decides the outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .config import Settings
from .limits import truncate
from .llm import ChatModel
from .prompts import NO_TOOL_CALL_NUDGE, SYSTEM_PROMPT
from .sandbox import Sandbox
from .tools import ToolRegistry
from .workspace import Workspace


class HarnessState(TypedDict, total=False):
    task: str
    messages: Annotated[list[AnyMessage], add_messages]
    actions: int  # executed tool calls
    denied: int  # rejected tool calls
    retries: int  # model errors + replies without tool calls
    finished: bool  # the model called finish
    summary: str
    stop_reason: str | None  # set when the harness stopped the loop
    checks: list[dict]
    changed_files: list[str]
    diff: str
    outcome: str


Event = dict[str, Any]


@dataclass
class Harness:
    settings: Settings
    workspace: Workspace
    sandbox: Sandbox
    model: ChatModel
    tools: ToolRegistry
    on_event: Callable[[Event], None] = field(default=lambda e: None)

    def emit(self, kind: str, **data) -> None:
        self.on_event({"kind": kind, **data})

    # ------------------------------------------------------------------ nodes
    def agent(self, state: HarnessState) -> dict:
        lim = self.settings.limits
        self.emit("thinking", step=state.get("actions", 0))
        try:
            reply = self.model.invoke(state["messages"])
        except Exception as e:  # network error, malformed response, ...
            retries = state.get("retries", 0) + 1
            self.emit("model_error", error=f"{type(e).__name__}: {e}", retries=retries)
            update: dict = {"retries": retries}
            if retries > lim.max_retries:
                update["stop_reason"] = f"retry limit reached ({lim.max_retries}) after model error: {type(e).__name__}: {e}"
            return update

        self.emit("model_reply", content=_text(reply.content), tool_calls=[{"name": c["name"], "args": c["args"]} for c in reply.tool_calls])
        if not reply.tool_calls and not getattr(reply, "invalid_tool_calls", None):
            retries = state.get("retries", 0) + 1
            update = {"messages": [reply, HumanMessage(NO_TOOL_CALL_NUDGE)], "retries": retries}
            if retries > lim.max_retries:
                update["stop_reason"] = f"retry limit reached ({lim.max_retries}): model kept replying without tool calls"
            return update
        return {"messages": [reply]}

    def act(self, state: HarnessState) -> dict:
        lim = self.settings.limits
        reply: AIMessage = state["messages"][-1]
        actions, denied = state.get("actions", 0), state.get("denied", 0)
        out: list[ToolMessage] = []
        finished, summary, stop = False, state.get("summary", ""), None

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

            outcome = self.tools.call(call["name"], call["args"])
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
            out.append(ToolMessage(outcome.content, tool_call_id=call["id"], status="error" if (outcome.denied or outcome.error) else "success"))

        return {"messages": out, "actions": actions, "denied": denied, "finished": finished,
                "summary": summary, "stop_reason": stop}

    def verify(self, state: HarnessState) -> dict:
        if state.get("stop_reason"):
            self.emit("stopped", reason=state["stop_reason"])
        results = []
        for check in self.settings.checks:
            cmd = self.sandbox.render(check.command)
            self.emit("check_start", name=check.name, command=cmd)
            res = self.sandbox.run(cmd, check.timeout_s or self.settings.limits.command_timeout_s)
            if res.error:
                status = "unavailable"
            elif res.timed_out:
                status = "timeout"
            else:
                status = "passed" if res.exit_code == 0 else "failed"
            output, cut = truncate(res.output if not res.error else res.error, self.settings.limits.max_output_chars)
            r = {"name": check.name, "command": cmd, "status": status, "exit_code": res.exit_code,
                 "duration_s": round(res.duration_s, 2), "output": output, "truncated": cut}
            self.emit("check_done", **r)
            results.append(r)
        return {"checks": results}

    def report(self, state: HarnessState) -> dict:
        diff = self.workspace.diff()
        changed = self.workspace.changed_files()
        checks = state.get("checks", [])
        all_passed = bool(checks) and all(c["status"] == "passed" for c in checks)
        if state.get("stop_reason"):
            outcome = "stopped"
        elif not state.get("finished"):
            outcome = "incomplete"
        elif not checks:
            outcome = "unverified"  # nothing to check: never reported as success
        else:
            outcome = "success" if all_passed else "checks_failed"
        self.emit("report", outcome=outcome, changed_files=changed)
        return {"diff": diff, "changed_files": changed, "outcome": outcome}

    # ----------------------------------------------------------------- routing
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

    def build(self):
        g = StateGraph(HarnessState)
        g.add_node("agent", self.agent)
        g.add_node("act", self.act)
        g.add_node("verify", self.verify)
        g.add_node("report", self.report)
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", self.after_agent, {"act": "act", "agent": "agent", "verify": "verify"})
        g.add_conditional_edges("act", self.after_act, {"agent": "agent", "verify": "verify"})
        g.add_edge("verify", "report")
        g.add_edge("report", END)
        return g.compile()

    def initial_state(self, task: str) -> HarnessState:
        system = SYSTEM_PROMPT.format(
            writable=", ".join(self.settings.target.writable),
            commands=", ".join(f"{k} ({v})" for k, v in self.settings.commands.items()) or "none",
        )
        return {"task": task, "messages": [SystemMessage(system), HumanMessage(f"Task:\n{task}")],
                "actions": 0, "denied": 0, "retries": 0, "finished": False, "summary": "",
                "stop_reason": None, "checks": [], "changed_files": [], "diff": "", "outcome": ""}

    def run(self, task: str) -> HarnessState:
        lim = self.settings.limits
        # Upper bound on graph steps; the harness limits above stop the loop long before this.
        recursion = 4 * (lim.max_actions + lim.max_denied + lim.max_retries) + 20
        return self.build().invoke(self.initial_state(task), {"recursion_limit": recursion})


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content)[:2000]


def _short_args(args: Any) -> Any:
    if not isinstance(args, dict):
        return args
    return {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v) for k, v in args.items()}

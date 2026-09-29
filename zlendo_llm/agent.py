"""
The autonomous lighting agent - Qwen driving the tool registry via
Ollama's native tool calling.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import requests

from .ollama_client import OllamaClient, OllamaError
from .session import RenderSession
from .tools import dispatch, ollama_tool_specs, to_text

log = logging.getLogger(__name__)


AGENT_SYSTEM = """\
You are a lighting technical director for an architectural visualisation \
renderer. You have tool access to a real 3D scene and a 4K path tracer. \
Your job is to make the render look excellent.

WORKFLOW - follow it in order:

1. Call analyze_scene first. Always.
2. Decide a lighting plan. Use set_lighting for the global controls and \
add_light for any fixtures the room is missing.
3. Call render ONCE when the plan is complete.
4. Read the returned metrics. If quality_score is above ~0.35 and you \
have budget left, make ONE targeted change and render again.
5. Call finish when you are satisfied or out of budget.

RENDER BUDGET IS STRICT AND SMALL. Do all your thinking before the \
first render, not after it. Never call render twice without changing \
something in between.

Be decisive. Do not call analyze_scene repeatedly without changing \
anything in between.
"""


class LightingDirectorAgent:
    """Autonomous multi-turn agent over the tool registry."""

    def __init__(
        self,
        client: OllamaClient,
        max_steps: int = 24,
    ):
        self.client = client
        self.max_steps = max_steps

    def run(self, session: RenderSession, brief: Optional[str] = None) -> Dict[str, Any]:
        cfg = self.client.cfg
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": AGENT_SYSTEM},
            {
                "role": "user",
                "content": (
                    (brief or "Light this interior scene and produce the best "
                              "possible 4K render.")
                    + f"\n\nYour render budget is {session.cfg.max_renders} "
                      "renders. Start by analysing the scene."
                ),
            },
        ]

        tools = ollama_tool_specs()
        transcript: List[Dict[str, Any]] = []
        finished = False

        for step in range(1, self.max_steps + 1):
            try:
                body = self._chat(messages, tools)
            except OllamaError as exc:
                log.error("Agent chat failed at step %d: %s", step, exc)
                break

            msg = body.get("message") or {}
            calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()

            messages.append({
                "role": "assistant",
                "content": content,
                **({"tool_calls": calls} if calls else {}),
            })

            if not calls:
                if content:
                    log.info("[agent] %s", content[:400])
                    transcript.append({"step": step, "assistant": content})
                if session.renders:
                    break
                messages.append({
                    "role": "user",
                    "content": "Continue by calling a tool. Start with analyze_scene.",
                })
                continue

            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                log.info("[agent step %d] %s(%s)", step, name,
                         ", ".join(f"{k}={v}" for k, v in list(args.items())[:6]))

                result = dispatch(session, name, args)
                transcript.append({"step": step, "tool": name,
                                   "args": args, "result": result})

                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": to_text(result),
                })

                if name == "finish":
                    finished = True

            if finished:
                break

            if session.renders_remaining <= 0 and session.renders:
                messages.append({
                    "role": "user",
                    "content": "Render budget is exhausted. Call finish now.",
                })

        summary = session.summary()
        summary["agent_finished_cleanly"] = finished
        summary["steps_used"] = len(transcript)
        summary["transcript"] = transcript
        return summary

    # ------------------------------------------------------------------
    def _chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        cfg = self.client.cfg
        payload = {
            "model": cfg.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "keep_alive": cfg.keep_alive_warm,
            "options": {
                "temperature": cfg.temperature,
                "top_p": cfg.top_p,
                "seed": cfg.seed,
                "num_ctx": cfg.num_ctx,
            },
        }
        if cfg.enable_thinking:
            payload["think"] = True

        last: Optional[Exception] = None
        for attempt in range(1, cfg.max_retries + 1):
            try:
                r = requests.post(
                    f"{cfg.host}/api/chat", json=payload, timeout=cfg.request_timeout
                )
                if r.status_code >= 400:
                    raise OllamaError(self.client._http_error(r))  # noqa: SLF001
                return r.json()
            except (requests.RequestException, OllamaError) as exc:
                last = exc
                log.warning("agent chat attempt %d failed: %s", attempt, exc)
        raise OllamaError(f"agent chat failed: {last}")

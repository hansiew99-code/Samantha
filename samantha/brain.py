"""The agent brain: a manual Anthropic tool loop (BRIEF §4, §8).

Manual rather than the SDK's beta tool runner because we need three things per
turn that the runner doesn't expose cleanly: spend logging on every call,
mid-loop model escalation, and a governor cap on which tier may run.
"""

from __future__ import annotations

import logging

import anthropic

from .config import Settings
from .context import assemble
from .governor import DETERMINISTIC, Governor, Usage
from .memory import Memory
from .router import MODEL_PARAMS, OPUS, SONNET, TIER_ORDER, cap_tier, pick_model
from .tools.registry import ToolRegistry

log = logging.getLogger(__name__)

MAX_ITERATIONS = 8

ESCALATE_SPEC = {
    "name": "escalate",
    "description": (
        "Hand the current task to a more capable (more expensive) model. Call "
        "this instead of delivering a mediocre answer when the task needs "
        "real reasoning: multi-person scheduling with several constraints, a "
        "delicate or high-stakes outbound draft, weekly planning, or anything "
        "you already attempted once and got wrong. Do not call it for routine "
        "lookups, reminders, or simple replies."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "enum": ["sonnet", "opus"],
                "description": "sonnet for multi-step reasoning/drafting; opus only for genuinely hard planning.",
            },
            "reason": {"type": "string"},
        },
        "required": ["model", "reason"],
    },
}

_ESCALATE_TARGETS = {"sonnet": SONNET, "opus": OPUS}


class Brain:
    def __init__(
        self,
        settings: Settings,
        memory: Memory,
        registry: ToolRegistry,
        governor: Governor,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.registry = registry
        self.governor = governor
        self.client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    def max_tier(self) -> str:
        return self.governor.max_tier()

    async def handle_message(self, text: str) -> str:
        self.memory.log_message("user", text)
        if self.governor.mode() == DETERMINISTIC:
            reply = (
                "I've hit today's token budget, so I'm resting my brain until "
                "midnight — reminders still fire and I'm still collecting "
                "events for tomorrow's brief. (/spend for details.)"
            )
            self.memory.log_message("assistant", reply)
            return reply
        model = pick_model(text, max_tier=self.max_tier())
        system, messages = assemble(self.memory, text, self.settings.timezone)
        reply = await self.run_loop(model, system, messages, purpose="chat")
        self.memory.log_message("assistant", reply)
        return reply

    async def run_loop(
        self,
        model: str,
        system: list[dict],
        messages: list[dict],
        purpose: str,
        tools: list[dict] | None = None,
    ) -> str:
        """The agentic loop: call → execute tools → feed results → repeat."""
        if tools is None:
            tools = sorted(
                self.registry.specs() + [ESCALATE_SPEC], key=lambda t: t["name"]
            )
        messages = list(messages)

        for _ in range(MAX_ITERATIONS):
            params: dict = dict(MODEL_PARAMS[model])
            resp = await self.client.messages.create(
                model=model, system=system, messages=messages, tools=tools, **params
            )
            self.governor.record(model, purpose, Usage.from_api(resp.usage))

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason == "tool_use" and tool_uses:
                messages.append({"role": "assistant", "content": resp.content})
                results: list[dict] = []
                escalate_to: str | None = None
                for block in tool_uses:
                    if block.name == "escalate":
                        target = self._resolve_escalation(model, block.input)
                        if target:
                            escalate_to = target
                            content, is_error = f"Escalating to {target}.", False
                        else:
                            content, is_error = (
                                "Escalation declined (already at or above that tier, "
                                "or budget-capped). Answer with the current model.",
                                False,
                            )
                    else:
                        content, is_error = await self.registry.execute(
                            block.name, block.input or {}
                        )
                    result: dict = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    }
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
                messages.append({"role": "user", "content": results})
                if escalate_to:
                    log.info("escalating %s -> %s", model, escalate_to)
                    model = escalate_to
                continue

            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return text or "Hmm — I came back empty. Try me again?"

        return "I went around in circles on that one. Mind rephrasing?"

    def _resolve_escalation(self, current: str, tool_input: dict) -> str | None:
        target = _ESCALATE_TARGETS.get((tool_input or {}).get("model", ""))
        if target is None:
            return None
        target = cap_tier(target, self.max_tier())
        if TIER_ORDER.index(target) <= TIER_ORDER.index(current):
            return None
        return target

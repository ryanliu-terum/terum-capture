"""Echo-loop guard tests: upload._strip_delivery_injection + its _parse_turns wiring.

The delivery hook injects [Terum ...]-marked blocks into the session; if they land inside a
captured user prompt, uploading them would re-distill the TEAM's knowledge as if this user
decided it. The strip removes exactly our marker blocks and nothing else.
"""
import json

from terum_capture.delivery_hooks import (
    CONFLICT_PREAMBLE,
    CONTEXT_MARKER,
    DECISION_MARKER,
    REMINDER_MARKER,
    SELF_CHECK_INSTRUCTION,
)
from terum_capture.upload import _parse_turns, _strip_delivery_injection

INJECTED_BLOCK = (
    f"{CONTEXT_MARKER} Your team already discussed/decided these — use if helpful, ignore if not:\n"
    "- we use Upstash Redis for rate limits (Teddy)\n"
    "- never run supabase db push against prod (Ryan)"
)

DECISION_BLOCK = (
    f"{CONFLICT_PREAMBLE}\n"
    f"{DECISION_MARKER} Decision (Ryan Liu, 2026-07-28): Use Vercel Queue Functions, "
    "not a self-hosted worker"
)


class TestStrip:
    def test_no_markers_passes_through_untouched(self):
        text = "an ordinary prompt\n- with a bullet list\n- of its own"
        assert _strip_delivery_injection(text) is text  # fast path: same object, zero work

    def test_strips_context_block_keeps_user_text(self):
        text = f"real question before\n\n{INJECTED_BLOCK}\n\nreal text after"
        out = _strip_delivery_injection(text)
        assert "real question before" in out
        assert "real text after" in out
        assert CONTEXT_MARKER not in out
        assert "Upstash Redis" not in out and "db push" not in out

    def test_strips_reminder_line(self):
        text = f"do the thing\n\n{SELF_CHECK_INSTRUCTION}"
        out = _strip_delivery_injection(text)
        assert "do the thing" in out
        assert REMINDER_MARKER not in out

    def test_user_bullets_outside_block_survive(self):
        text = f"{INJECTED_BLOCK}\n\nmy own list:\n- keep this bullet\n- and this one"
        out = _strip_delivery_injection(text)
        assert "- keep this bullet" in out and "- and this one" in out
        assert "Upstash Redis" not in out

    def test_strips_decision_block_keeps_user_text(self):
        text = f"real question before\n\n{DECISION_BLOCK}\n\nreal text after"
        out = _strip_delivery_injection(text)
        assert "real question before" in out
        assert "real text after" in out
        assert DECISION_MARKER not in out
        assert "Vercel Queue Functions" not in out and "judge silently" not in out

    def test_strips_all_three_marker_kinds_together(self):
        text = f"do the thing\n\n{INJECTED_BLOCK}\n\n{DECISION_BLOCK}\n\n{SELF_CHECK_INSTRUCTION}"
        out = _strip_delivery_injection(text)
        assert "do the thing" in out
        for marker in (CONTEXT_MARKER, DECISION_MARKER, REMINDER_MARKER):
            assert marker not in out
        assert "Upstash Redis" not in out and "Vercel Queue Functions" not in out

    def test_indented_markers_stripped(self):
        text = f"prompt\n  {REMINDER_MARKER} indented reminder\n    {CONTEXT_MARKER} hdr:\n    - item"
        out = _strip_delivery_injection(text)
        assert out.splitlines() == ["prompt"]


class TestParseTurnsWiring:
    @staticmethod
    def _entries(user_content: str):
        return [
            {"type": "user", "message": {"content": user_content}, "timestamp": "2026-07-29T00:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a sufficiently long reply"}]},
             "timestamp": "2026-07-29T00:00:05Z"},
        ]

    def test_injected_block_removed_from_captured_prompt(self):
        _, turns = _parse_turns(self._entries(f"what should I use for rate limits?\n\n{INJECTED_BLOCK}"))
        assert len(turns) == 1
        prompt = turns[0][0]
        assert prompt == "what should I use for rate limits?"
        assert "Upstash" not in json.dumps(turns)

    def test_pure_injection_prompt_drops_to_assistant_only_turn(self):
        _, turns = _parse_turns(self._entries(INJECTED_BLOCK))
        assert len(turns) == 1
        assert turns[0][0] == ""  # prompt stripped to nothing -> response-only turn, nothing echoed

    def test_decision_block_removed_from_captured_prompt(self):
        _, turns = _parse_turns(self._entries(f"should we self-host the queue?\n\n{DECISION_BLOCK}"))
        assert len(turns) == 1
        prompt = turns[0][0]
        assert prompt == "should we self-host the queue?"
        assert "Vercel Queue Functions" not in json.dumps(turns)

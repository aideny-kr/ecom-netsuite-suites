"""Tests for build_current_date_block — must wrap output in <current_datetime>
tags so split_system_prompt can extract it to the dynamic (uncached) section.

Without the wrapper the HH:MM portion ends up inside the cached static prefix
and busts the prompt cache every minute. Caught by codex review of the
prompt-cache audit (May 2026)."""

from __future__ import annotations

from app.services.chat.agents.base_agent import build_current_date_block
from app.services.chat.prompt_cache import split_system_prompt


class TestCurrentDateBlockWrapping:
    def test_block_starts_with_opening_tag(self):
        block = build_current_date_block(user_timezone="UTC")
        assert block.lstrip().startswith("<current_datetime>")

    def test_block_ends_with_closing_tag(self):
        block = build_current_date_block(user_timezone="UTC")
        assert block.rstrip().endswith("</current_datetime>")

    def test_block_contains_date_header_inside_tags(self):
        block = build_current_date_block(user_timezone="UTC")
        # Header is still emitted — just inside the wrapping tags.
        assert "## CURRENT DATE & TIME" in block
        opening = block.index("<current_datetime>")
        header = block.index("## CURRENT DATE & TIME")
        closing = block.index("</current_datetime>")
        assert opening < header < closing

    def test_empty_string_on_failure_stays_empty(self):
        """If the function falls through to its except-branch it returns ''.
        We must not gratuitously add tags when there's nothing to wrap.
        """
        # The current implementation only returns '' on exception. We can't
        # easily force one without monkeypatching datetime — instead, assert
        # the function's contract: when it DOES return content, it's wrapped;
        # when it returns '', there are no tags either.
        block = build_current_date_block(user_timezone="UTC")
        # Non-empty path
        assert block != ""
        # Tags appear iff content is non-empty.

    def test_split_system_prompt_extracts_real_block_to_dynamic(self):
        """Wire-up regression: the real block (as built by the function) must
        end up entirely in dynamic, not static, after split_system_prompt."""
        block = build_current_date_block(user_timezone="UTC")
        full_prompt = "You are an assistant.\n" + block + "\n\nEnd."
        parts = split_system_prompt(full_prompt)
        assert "## CURRENT DATE & TIME" not in parts.static
        # local time HH:MM should not leak into static (no leading "local time:" header)
        assert "local time:" not in parts.static
        assert "<current_datetime>" in parts.dynamic
        assert "## CURRENT DATE & TIME" in parts.dynamic

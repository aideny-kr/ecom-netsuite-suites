"""Tests for chitchat detection — short-circuiting expensive context assembly."""

import pytest

from app.services.chat.orchestrator import _CHITCHAT_RE


def _is_chitchat(text: str) -> bool:
    return bool(_CHITCHAT_RE.match(text.strip()))


class TestChitchatPositiveMatches:
    """These messages SHOULD be classified as chitchat."""

    @pytest.mark.parametrize(
        "msg",
        [
            # Compliments
            "you are perfect bro",
            "you're the best",
            "you are awesome",
            "you're amazing!",
            "great!",
            "nice!",
            "awesome",
            "perfect",
            "brilliant",
            "cool",
            "excellent",
            "wonderful",
            "fantastic",
            # Thanks
            "thanks",
            "thank you",
            "thanks!",
            "thx",
            "ty",
            "cheers",
            "thanks!!",
            # Job compliments
            "good job",
            "well done",
            "nice work",
            "nailed it",
            # Love it
            "love it",
            "love this",
            "you rock",
            "bravo",
            # Affirmations
            "ok",
            "okay",
            "sure",
            "yep",
            "nope",
            "got it",
            "understood",
            "i see",
            "makes sense",
            # Reactions
            "wow",
            "lol",
            "haha",
            "hahaha",
            # Greetings
            "hi",
            "hello",
            "hey",
            "good morning",
            "good afternoon",
            "good evening",
            "good night",
            # Farewells
            "bye",
            "goodbye",
            "see ya",
            "later",
            "gn",
            # Combinations (up to 5 phrases)
            "thanks, good job!",
            "awesome, thank you!",
            "ok thanks",
            "hi, thanks!",
            "wow, amazing!",
            "ok cool",
            # Whitespace / punctuation
            "  thanks  ",
            "thanks!!!",
            "great...",
        ],
    )
    def test_chitchat_detected(self, msg):
        assert _is_chitchat(msg), f"Expected chitchat for: {msg!r}"


class TestChitchatNegativeMatches:
    """These messages should NOT be classified as chitchat."""

    @pytest.mark.parametrize(
        "msg",
        [
            # Actual queries
            "show me the income statement",
            "what's our P&L this month?",
            "how many orders today",
            "find invoice INV12345",
            "revenue by platform this month",
            # Looks like chitchat but has substance
            "thanks, now show me the balance sheet",
            "great, can you run the trend chart?",
            "ok now pull the expenses by GL category",
            "nice work, what about Q4?",
            "cool, compare that with last month",
            # Mixed — substantive content wins
            "hey can you show me today's sales",
            "hi, what's the net income for January?",
            "thanks for that. now show me COGS breakdown",
            # Long messages are not chitchat
            "you are perfect bro can you also show me the revenue by account",
            # Questions about the system
            "how does the chat work?",
            "what tables are available?",
        ],
    )
    def test_not_chitchat(self, msg):
        assert not _is_chitchat(msg), f"Expected NOT chitchat for: {msg!r}"

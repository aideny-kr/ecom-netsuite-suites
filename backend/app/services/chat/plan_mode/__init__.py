"""Plan Mode — HITL clarification for ambiguous financial queries.

Architectural twin of write-confirm (PR #39). Same persistence pattern
(ChatMessage.structured_output), same HMAC pattern (mutation_guard tokens),
same short-circuit point (chat.py POST + orchestrator.run_chat_turn).
"""

"""Shared fixtures for metric-service tests.

CI runs with no embedding provider configured, so the real
``embed_domain_texts`` returns ``None`` and the seeder (§12.2) correctly
refuses to seed rows without 1536-d vectors. That makes the seeder /
idempotency tests depend on a live OpenAI key — green locally, red in CI.

This autouse fixture replaces the seeder's bound ``embed_domain_texts`` name
(it imports the symbol with ``from ...domain_knowledge import embed_domain_texts``)
with a deterministic stand-in so the upsert / RLS-context / idempotency logic is
exercised without a network embedder. The blast radius is limited to code paths
that call the seeder's embedder — every other metric test inserts its
``intent_embedding`` directly or patches ``embed_domain_query`` itself, so this
is a no-op for them.
"""

import pytest

_DIM = 1536


async def _fake_embed_domain_texts(texts: list[str]) -> list[list[float]]:
    # Distinct, deterministic per-row vectors so seeded rows are never identical
    # (keeps any incidental vector ordering stable) and always exactly 1536-d.
    vectors: list[list[float]] = []
    for i, _ in enumerate(texts):
        vec = [0.1] * _DIM
        vec[i % _DIM] = 1.0
        vectors.append(vec)
    return vectors


@pytest.fixture(autouse=True)
def _stub_seeder_embedder(monkeypatch):
    monkeypatch.setattr(
        "app.services.metrics.metric_catalog_seeder.embed_domain_texts",
        _fake_embed_domain_texts,
    )

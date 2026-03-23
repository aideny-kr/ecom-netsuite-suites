"""Tests for RAG partition filtering on domain_knowledge_chunks."""

import pytest

from app.services.chat.agents.tool_filter import filter_knowledge_by_partition


class TestRagPartitionFilter:

    def test_partition_filter_single(self):
        chunks = [
            {"raw_text": "A", "partition_id": "pricing"},
            {"raw_text": "B", "partition_id": "inventory"},
            {"raw_text": "C", "partition_id": "pricing"},
        ]
        result = filter_knowledge_by_partition(chunks, partition_ids=["pricing"])
        assert len(result) == 2
        assert all(c["partition_id"] == "pricing" for c in result)

    def test_partition_filter_none_returns_all(self):
        chunks = [
            {"raw_text": "A", "partition_id": "pricing"},
            {"raw_text": "B", "partition_id": None},
        ]
        result = filter_knowledge_by_partition(chunks, partition_ids=None)
        assert len(result) == 2

    def test_partition_filter_multiple(self):
        chunks = [
            {"raw_text": "A", "partition_id": "pricing"},
            {"raw_text": "B", "partition_id": "inventory"},
            {"raw_text": "C", "partition_id": "general"},
        ]
        result = filter_knowledge_by_partition(chunks, partition_ids=["pricing", "inventory"])
        assert len(result) == 2
        assert {c["partition_id"] for c in result} == {"pricing", "inventory"}

    def test_partition_filter_no_match(self):
        chunks = [
            {"raw_text": "A", "partition_id": "pricing"},
        ]
        result = filter_knowledge_by_partition(chunks, partition_ids=["nonexistent"])
        assert result == []

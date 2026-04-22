"""Unit tests for partition_id propagation in ingest_domain_knowledge.

DomainKnowledgeChunk.partition_id has been on the model since the
knowledge-profile work but the ingest script never populated it.
netsuite.yaml's rag_partitions (netsuite/suiteql-rules, netsuite/joins,
etc.) only work if the matching chunks have partition_id set.

This test asserts that `partition_id` in frontmatter flows through to
the chunk record's partition_id field.
"""

from scripts.ingest_domain_knowledge import parse_frontmatter


class TestParseFrontmatterPartitionId:
    def test_partition_id_in_frontmatter(self):
        content = '---\npartition_id: netsuite/suiteql-rules\ntopic_tags: ["suiteql"]\n---\n\n# Body\n'
        fm, body = parse_frontmatter(content)
        assert fm.get("partition_id") == "netsuite/suiteql-rules"
        assert body.startswith("# Body")

    def test_partition_id_absent_is_none(self):
        content = '---\ntopic_tags: ["suiteql"]\n---\n\n# Body\n'
        fm, body = parse_frontmatter(content)
        assert fm.get("partition_id") is None

    def test_no_frontmatter_at_all(self):
        content = "# Just a heading\n\nsome body\n"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content


class TestChunkPartitionIdPropagation:
    """The chunk record inserted into domain_knowledge_chunks must have
    partition_id populated when the source markdown declares it in
    frontmatter.
    """

    def test_ingest_passes_partition_id_to_chunk_dict(self):
        """The dict representation of a chunk built from frontmatter with
        partition_id must carry that partition_id forward."""
        from scripts.ingest_domain_knowledge import build_chunk_dicts_for_file

        # A minimal markdown file string with frontmatter
        content = (
            "---\n"
            'topic_tags: ["suiteql", "joins"]\n'
            "source_type: expert_rules\n"
            "partition_id: netsuite/joins\n"
            "---\n\n"
            "# Join Patterns\n\n"
            "## Header vs Line\n\n"
            "Some content about joins.\n"
        )
        chunks = build_chunk_dicts_for_file(source_uri="test.md", content=content)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk["partition_id"] == "netsuite/joins"

    def test_ingest_handles_missing_partition_id(self):
        """Files without partition_id frontmatter produce chunks with
        partition_id=None (not a crash, not a default string)."""
        from scripts.ingest_domain_knowledge import build_chunk_dicts_for_file

        content = '---\ntopic_tags: ["suiteql"]\nsource_type: expert_rules\n---\n\n# Something\n\nbody\n'
        chunks = build_chunk_dicts_for_file(source_uri="test.md", content=content)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk["partition_id"] is None

from app.services.drive_rag.chunker import chunk_text, count_tokens


def test_chunk_empty_text_returns_empty_list():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunk_short_text_returns_single_chunk():
    text = "This is a short paragraph."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0]["content"] == text
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["token_count"] > 0


def test_chunk_respects_max_tokens():
    paragraph = "word " * 2000
    chunks = chunk_text(paragraph, max_tokens=800, overlap_tokens=100)
    assert len(chunks) >= 2
    for c in chunks:
        assert c["token_count"] <= 800 + 50  # small fuzz for overlap prepend


def test_chunks_are_sequentially_indexed():
    text = "\n\n".join([f"Paragraph {i}. " + ("word " * 400) for i in range(5)])
    chunks = chunk_text(text, max_tokens=800, overlap_tokens=100)
    for i, c in enumerate(chunks):
        assert c["chunk_index"] == i


def test_chunk_overlap_is_applied():
    text = " ".join([f"word{i}" for i in range(2000)])
    chunks = chunk_text(text, max_tokens=400, overlap_tokens=100)
    assert len(chunks) >= 2
    tail = chunks[0]["content"].split()[-80:]
    head = chunks[1]["content"].split()[:80]
    shared = set(tail) & set(head)
    assert len(shared) >= 40


def test_count_tokens_returns_positive_int():
    t = count_tokens("hello world " * 100)
    assert isinstance(t, int) and t > 0

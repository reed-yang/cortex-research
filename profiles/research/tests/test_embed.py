import os
import pytest

def test_embed_text_skip_returns_zero_vector(monkeypatch):
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research.embed import embed_text
    v = embed_text("anything")
    assert isinstance(v, list) and len(v) == 4096 and all(x == 0.0 for x in v)

def test_embed_texts_skip_shape(monkeypatch):
    monkeypatch.setenv("CORTEX_SKIP_EMBED", "1")
    from cortex_research.embed import embed_texts
    vs = embed_texts(["a", "b", "c"])
    assert len(vs) == 3 and all(len(v) == 4096 for v in vs)

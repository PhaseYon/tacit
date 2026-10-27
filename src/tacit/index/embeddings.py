"""One replaceable interface for embeddings. All backends return 384-dim vectors to
match the Neo4j vector index (tacit_chunk_embedding).

- fastembed (default): local ONNX model, no API key, no per-document cost, and the
  same results everywhere, so benchmarks are reproducible.
- openai: text-embedding-3-small, shortened to 384 dims via the `dimensions` param.
- fake: deterministic hashing embedder for tests (no model download).
"""

import hashlib
import math
from functools import lru_cache
from typing import Protocol

from .. import secrets
from ..config import get_config

DIM = 384


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        self.model = model
        self._m = TextEmbedding(model_name=model)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._m.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._m.query_embed(text))).tolist()


class OpenAIEmbedder:
    def __init__(self, model: str, api_key: str) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self.model, input=texts, dimensions=DIM)
        return [d.embedding for d in resp.data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class FakeEmbedder:
    """Bag-of-words hashing: texts that share words get similar vectors."""

    model = "fake-hash-384"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for word in text.lower().split():
            h = int.from_bytes(hashlib.sha1(word.encode()).digest()[:4], "big")
            v[h % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@lru_cache
def get_embedder() -> Embedder:
    c = get_config().embeddings
    if c.provider == "fake":
        return FakeEmbedder()
    if c.provider == "openai":
        model = c.model if "embedding" in c.model else "text-embedding-3-small"
        return OpenAIEmbedder(model, secrets.get("openai_api_key") or "")
    return FastEmbedEmbedder(c.model)

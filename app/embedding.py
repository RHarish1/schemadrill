from functools import lru_cache

from app.config import get_settings


@lru_cache
def get_embedding_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(get_settings().embedding_model)


def embed_passage(text: str) -> list[float]:
    return get_embedding_model().encode(text, normalize_embeddings=True).tolist()


def embed_query(text: str) -> list[float]:
    instruction = "Represent this question for searching relevant database schema: "
    return get_embedding_model().encode(instruction + text, normalize_embeddings=True).tolist()

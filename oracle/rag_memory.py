# oracle/rag_memory.py
# FAISS in-memory IndexFlatL2 + OpenAI embeddings
# Fallback на TF-IDF без OpenAI если ключа нет

import os
import json
import hashlib
import numpy as np
from pathlib import Path
from loguru import logger

try:
    import faiss
    FAISS_OK = True
except ImportError:
    FAISS_OK = False
    logger.warning("faiss-cpu не установлен, RAG отключён")

_index = None
_chunks: list[str] = []
_embeddings_cache: dict[str, list[float]] = {}

STRATEGIES_DIR = Path("data/strategies")
EMBED_DIM = 1536  # text-embedding-3-small


async def _get_embedding(text: str) -> list[float] | None:
    """OpenAI embeddings с кэшем по тексту."""
    key = hashlib.md5(text.encode()).hexdigest()
    if key in _embeddings_cache:
        return _embeddings_cache[key]

    from core.config import settings
    if not settings.openai_api_key:
        return None

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": "text-embedding-3-small", "input": text},
            )
            if r.status_code != 200:
                logger.error(f"OpenAI error {r.status_code}: {r.text}")
                return None
            
            data = r.json()
            if "data" not in data:
                logger.error(f"OpenAI unexpected JSON: {data}")
                return None
                
            vec = data["data"][0]["embedding"]
            _embeddings_cache[key] = vec
            return vec
    except Exception as e:
        logger.warning(f"OpenAI embedding failed: {e}")
        return None


def _load_documents() -> list[str]:
    """Загружает .txt файлы из data/strategies/, разбивает на чанки."""
    chunks = []
    if not STRATEGIES_DIR.exists():
        logger.warning(f"Директория {STRATEGIES_DIR} не найдена")
        return chunks

    for file in STRATEGIES_DIR.glob("*.txt"):
        text = file.read_text(encoding="utf-8")
        # Разбиваем по строкам, группируем по 3 строки = 1 чанк
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i in range(0, len(lines), 3):
            chunk = " | ".join(lines[i:i+3])
            if chunk:
                chunks.append(chunk)

    logger.info(f"FAISS: загружено {len(chunks)} чанков из {STRATEGIES_DIR}")
    return chunks


async def build_index():
    """Строит FAISS индекс при старте бота."""
    global _index, _chunks

    if not FAISS_OK:
        return

    _chunks = _load_documents()
    if not _chunks:
        return

    vecs = []
    for chunk in _chunks:
        vec = await _get_embedding(chunk)
        if vec is not None:
            vecs.append(vec)
        else:
            # Fallback: нулевой вектор (не идеально, но не ломает)
            vecs.append([0.0] * EMBED_DIM)

    arr = np.array(vecs, dtype="float32")
    _index = faiss.IndexFlatL2(EMBED_DIM)
    _index.add(arr)
    logger.info(f"FAISS index built: {_index.ntotal} векторов")


async def get_context(query: str, top_k: int = 3) -> str:
    """Поиск релевантного контекста по запросу."""
    if not FAISS_OK or _index is None or not _chunks:
        return ""

    vec = await _get_embedding(query)
    if vec is None:
        # Fallback: keyword search
        q_lower = query.lower()
        results = [c for c in _chunks if any(w in c.lower() for w in q_lower.split())]
        return "\n".join(results[:top_k])

    q_arr = np.array([vec], dtype="float32")
    distances, indices = _index.search(q_arr, top_k)

    results = []
    for idx in indices[0]:
        if 0 <= idx < len(_chunks):
            results.append(_chunks[idx])

    return "\n".join(results)


def get_chunk_count() -> int:
    return len(_chunks)

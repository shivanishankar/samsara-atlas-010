import hashlib
import logging
import os
import threading
from pathlib import Path

from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BM25_CACHE: dict[str, BM25Okapi] = {}
_cache_lock = threading.Lock()


def clear_retrieval_cache() -> None:
    with _cache_lock:
        _BM25_CACHE.clear()


def _load_docs(docs_dir: str) -> list[tuple[str, str]]:
    """Load markdown policy docs in filename order so retrieval is repeatable."""
    docs_path = Path(docs_dir)
    if not docs_path.exists():
        return []
    pairs = []
    for path in sorted(docs_path.glob("*.md"), key=lambda item: item.name):
        try:
            pairs.append((path.stem, path.read_text(encoding="utf-8")))
        except OSError as exc:
            logger.warning("policy_doc_unreadable file=%s error_type=%s", path.name, type(exc).__name__)
    return pairs


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


def _fingerprint(pairs: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for name, text in pairs:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def retrieve_policy_snippets(query: str, top_k: int = 5) -> list[str]:
    """Rank policy docs with BM25. The index is rebuilt when document bytes change."""
    docs_dir = os.getenv("DOCS_DIR", str(_PROJECT_ROOT / "data" / "docs"))
    pairs = _load_docs(docs_dir)
    if not pairs:
        return []

    fingerprint = _fingerprint(pairs)
    corpus = [_tokenize(text) for _, text in pairs]
    with _cache_lock:
        bm25 = _BM25_CACHE.get(fingerprint)
        if bm25 is None:
            bm25 = BM25Okapi(corpus)
            _BM25_CACHE.clear()
            _BM25_CACHE[fingerprint] = bm25

    scores = bm25.get_scores(_tokenize(query))
    # Filename is the tie-break. hash() is process-randomized and would make
    # two replays of the same case cite different policies.
    ranked = sorted(
        zip(scores, pairs),
        key=lambda item: (-item[0], item[1][0]),
    )
    best = ranked[0][0] if ranked else 0
    # A raw top-k keeps any doc that shares a common word. On a PTO question
    # that pulled in the harassment policy, and the model cited it. Keep a
    # hit only when it is in the same band as the best match.
    minimum = best * 0.30

    snippets = []
    seen = set()
    for score, (name, content) in ranked:
        if score <= 0 or score < minimum:
            continue
        labeled = f"{name}\n{content[:500]}"
        if labeled in seen:
            continue
        seen.add(labeled)
        snippets.append(labeled)
        if len(snippets) >= top_k:
            break
    return snippets

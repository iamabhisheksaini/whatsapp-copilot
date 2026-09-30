"""Vector-store and text tools for Agent A.

Kept separate from graph.py so the graph nodes stay readable and so tests can
swap the store for an in-memory fake.
"""

import hashlib
import os
from typing import Any, Protocol

from langchain_text_splitters import RecursiveCharacterTextSplitter

from shared.llm import get_embeddings, get_logger

log = get_logger("agentA.tools")

CHROMA_HOST = os.getenv("CHROMA_HOST", "chroma")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "knowledge")
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "150"))

splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
)


class VectorStore(Protocol):
    def upsert(self, **kwargs: Any) -> Any: ...
    def query(self, **kwargs: Any) -> Any: ...
    def delete(self, **kwargs: Any) -> Any: ...


_collection: VectorStore | None = None


def get_collection() -> VectorStore:
    """Lazily connect to Chroma.

    Lazy so importing this module (in tests, or at container start before Chroma
    is healthy) does not require a live server.
    """
    global _collection
    if _collection is None:
        import chromadb

        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        _collection = client.get_or_create_collection(name=COLLECTION_NAME)
        log.info(f"connected to chroma collection '{COLLECTION_NAME}'")
    return _collection


def set_collection(collection: VectorStore) -> None:
    """Test seam: inject a fake collection."""
    global _collection
    _collection = collection


def document_key(filename: str, drive_file_id: str | None = None) -> str:
    """Stable identity for a source document.

    Prefer the Drive file id — a file renamed in Drive is still the same
    document, and re-ingesting it should replace its chunks rather than add a
    second copy.
    """
    basis = drive_file_id or filename
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def split(text: str) -> list[str]:
    return [c for c in splitter.split_text(text) if c.strip()]


def embed_documents(chunks: list[str]) -> list[list[float]]:
    return get_embeddings().embed_documents(chunks)


def embed_query(text: str) -> list[float]:
    return get_embeddings().embed_query(text)


def purge_document(doc_key: str) -> None:
    """Drop existing chunks for a document so re-ingest is idempotent."""
    try:
        get_collection().delete(where={"docKey": doc_key})
    except Exception as exc:  # noqa: BLE001 - first ingest has nothing to delete
        log.info(f"purge skipped for {doc_key}: {exc}")


def persist_chunks(
    doc_key: str,
    filename: str,
    chunks: list[str],
    vectors: list[list[float]],
    metadata: dict[str, Any],
) -> None:
    ids = [f"{doc_key}-{i}" for i in range(len(chunks))]
    metas = [
        {
            "docKey": doc_key,
            "filename": filename,
            "chunkIndex": i,
            # Chroma rejects None values in metadata, so drop empty keys.
            **{k: v for k, v in metadata.items() if v is not None},
        }
        for i in range(len(chunks))
    ]
    get_collection().upsert(
        ids=ids, documents=chunks, metadatas=metas, embeddings=vectors
    )


def retrieve(query_vector: list[float], k: int = 4) -> list[dict[str, Any]]:
    results = get_collection().query(
        query_embeddings=[query_vector],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    hits = []
    for doc, meta, dist in zip(documents, metadatas, distances, strict=False):
        hits.append({"document": doc, "metadata": meta or {}, "distance": dist})
    return hits


def estimate_tokens(chunks: list[str]) -> int:
    """Rough token count (~4 chars/token). Good enough for reporting."""
    return sum(len(c) for c in chunks) // 4

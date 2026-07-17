from pathlib import Path
from typing import List

import chromadb
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

from config import DATA_DIR

_CHROMA_DIR = DATA_DIR / "chroma_db"
_CHROMA_DIR.mkdir(exist_ok=True)

_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        ef = ONNXMiniLM_L6_V2()
        client = chromadb.PersistentClient(path=str(_CHROMA_DIR))
        _collection = client.get_or_create_collection(
            name="garments",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"}
        )
    return _collection


def _garment_doc(garment: dict) -> str:
    parts = [
        garment.get('type', ''),
        garment.get('color', ''),
        garment.get('brand', ''),
        garment.get('name', ''),
        garment.get('fit', ''),
        garment.get('occasion', ''),
        garment.get('material', ''),
        garment.get('size', ''),
        garment.get('notes', ''),
    ]
    tags = garment.get('tags') or []
    if isinstance(tags, list):
        parts.extend(tags)
    return " ".join(p for p in parts if p)


def embed_garment(garment: dict):
    col = _get_collection()
    col.upsert(
        ids=[str(garment['id'])],
        documents=[_garment_doc(garment)],
        metadatas=[{
            "garment_id": garment['id'],
            "user_id": garment['user_id'],
            "type": garment.get('type', ''),
            "color": garment.get('color', ''),
            "occasion": garment.get('occasion', ''),
        }]
    )


def delete_garment(garment_id: int):
    col = _get_collection()
    try:
        col.delete(ids=[str(garment_id)])
    except Exception:
        pass


def search_garments(query: str, user_id: int, n_results: int = 10, types: list = None) -> List[int]:
    col = _get_collection()
    count = col.count()
    if count == 0:
        return []
    conditions = [{"user_id": user_id}]
    if types:
        conditions.append({"type": {"$in": types}})
    where = conditions[0] if len(conditions) == 1 else {"$and": conditions}
    results = col.query(query_texts=[query], n_results=min(n_results, count), where=where)
    return [int(id_) for id_ in results['ids'][0]]

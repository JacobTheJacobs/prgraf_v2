from loguru import logger

from . import logs as ls
from .config import settings
from .constants import PAYLOAD_NODE_ID, PAYLOAD_QUALIFIED_NAME, PAYLOAD_PROJECT_NAME
from .utils.dependencies import has_qdrant_client

_CLIENT = None
_QDRANT_AVAILABLE = None  # None = not checked, True/False = checked


def get_qdrant_client():
    """Lazy import to avoid grpc issues at module load time."""
    global _CLIENT, _QDRANT_AVAILABLE
    
    if _CLIENT is not None:
        return _CLIENT
    
    # Already tried and failed
    if _QDRANT_AVAILABLE is False:
        return None
        
    if not has_qdrant_client():
        _QDRANT_AVAILABLE = False
        return None
        
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        
        _CLIENT = QdrantClient(path=settings.QDRANT_DB_PATH, prefer_grpc=False)
        if not _CLIENT.collection_exists(settings.QDRANT_COLLECTION_NAME):
            _CLIENT.create_collection(
                collection_name=settings.QDRANT_COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=settings.QDRANT_VECTOR_DIM, distance=Distance.COSINE
                ),
            )
        _QDRANT_AVAILABLE = True
        return _CLIENT
    except Exception as e:
        logger.warning(f"Qdrant client unavailable: {e}")
        _QDRANT_AVAILABLE = False
        return None


def store_embedding(
    node_id: int, 
    embedding: list[float], 
    qualified_name: str,
    project_name: str = ""
) -> None:
    client = get_qdrant_client()
    if client is None:
        return
        
    try:
        from qdrant_client.models import PointStruct
        
        client.upsert(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points=[
                PointStruct(
                    id=node_id,
                    vector=embedding,
                    payload={
                        PAYLOAD_NODE_ID: node_id,
                        PAYLOAD_QUALIFIED_NAME: qualified_name,
                        PAYLOAD_PROJECT_NAME: project_name,
                    },
                )
            ],
        )
    except Exception as e:
        logger.warning(
            ls.EMBEDDING_STORE_FAILED.format(name=qualified_name, error=e)
        )


def embedding_exists(node_id: int) -> bool:
    """Check if an embedding already exists for this node_id."""
    client = get_qdrant_client()
    if client is None:
        return False
        
    try:
        # Try to retrieve the point by ID
        result = client.retrieve(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            ids=[node_id],
        )
        return len(result) > 0
    except Exception:
        return False


def search_embeddings(
    query_embedding: list[float], 
    top_k: int | None = None,
    project_name: str | None = None
) -> list[tuple[int, float]]:
    client = get_qdrant_client()
    if client is None:
        return []
        
    effective_top_k = top_k if top_k is not None else settings.QDRANT_TOP_K
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        
        # Apply Project Filter if provided
        query_filter = None
        if project_name:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key=PAYLOAD_PROJECT_NAME,
                        match=MatchValue(value=project_name)
                    )
                ]
            )

        result = client.query_points(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            query=query_embedding,
            query_filter=query_filter,
            limit=effective_top_k,
        )
        return [
            (hit.payload[PAYLOAD_NODE_ID], hit.score)
            for hit in result.points
            if hit.payload is not None
        ]
    except Exception as e:
        logger.warning(ls.EMBEDDING_SEARCH_FAILED.format(error=e))
        return []


def delete_project_embeddings(project_name: str) -> None:
    client = get_qdrant_client()
    if client is None:
        return
        
    logger.info(f"🗑️ Deleting embeddings for project '{project_name}' (Fresh Start)...")
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        
        client.delete(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key=PAYLOAD_PROJECT_NAME,
                        match=MatchValue(value=project_name)
                    )
                ]
            )
        )
    except Exception as e:
        logger.error(f"Failed to delete embeddings for project {project_name}: {e}")

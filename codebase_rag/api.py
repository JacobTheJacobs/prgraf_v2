from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from pathlib import Path
from loguru import logger
from codebase_rag.services.pr_service import PRService
import time
from collections import deque

# --- Log Store (captures all loguru messages) ---
class LogStore:
    def __init__(self, max_logs=200):
        self.logs = deque(maxlen=max_logs)
    
    def sink(self, message):
        """Loguru sink function - captures log messages."""
        record = message.record
        self.logs.append({
            "timestamp": record["time"].strftime("%H:%M:%S"),
            "level": record["level"].name,
            "message": record["message"],
            "module": record["name"]
        })
    
    def get_logs(self):
        return list(self.logs)
    
    def clear(self):
        self.logs.clear()

log_store = LogStore(max_logs=200)

# Add loguru sink to capture logs
logger.add(log_store.sink, format="{message}", level="DEBUG")

# --- Status Store ---
class ProcessingStatus:
    def __init__(self):
        self.progress = 0
        self.message = "Idle"
        self.state = "idle"  # idle, processing, complete, error

    def update(self, progress, message, state="processing"):
        self.progress = progress
        self.message = message
        self.state = state

    def reset(self):
        self.progress = 0
        self.message = "Idle"
        self.state = "idle"

# Job Management
import uuid
import asyncio
from datetime import datetime

class JobManager:
    def __init__(self):
        self.jobs = {}

    def create_job(self):
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            "id": job_id,
            "status": "pending",
            "progress": 0,
            "message": "Initializing...",
            "result": None,
            "created_at": datetime.now().isoformat()
        }
        return job_id

    def update_job(self, job_id, progress=None, message=None, status=None, result=None):
        if job_id in self.jobs:
            if progress is not None: self.jobs[job_id]["progress"] = progress
            if message is not None: self.jobs[job_id]["message"] = message
            if status is not None: self.jobs[job_id]["status"] = status
            if result is not None: self.jobs[job_id]["result"] = result

    def get_job(self, job_id):
        return self.jobs.get(job_id)

job_manager = JobManager()
status_store = ProcessingStatus() # Keep for legacy compatibility if needed

app = FastAPI(title="PocketFlow PR Reviewer")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Service Instance
# We assume a default project name for now or pass it via API
# ideally checking if we have a configured project
router_service = PRService(project_name="TUTORIAL_RAG")

from fastapi import BackgroundTasks

async def run_analysis_task(job_id: str, data: dict):
    try:
        logger.info(f"🚀 Starting Async Analysis Job {job_id}")
        job_manager.update_job(job_id, status="processing", progress=5, message="Starting Cognit Protocol...")
        
        async def progress_callback(pct, msg):
            job_manager.update_job(job_id, progress=pct, message=msg)
            # Also update legacy status_store for global visibility (optional)
            status_store.update(pct, msg)

        review_result = await router_service.route_and_review(data, on_progress=progress_callback)
        
        job_manager.update_job(job_id, status="completed", progress=100, message="Analysis Complete", result=review_result)
        logger.info(f"✅ Job {job_id} Complete")
    except Exception as e:
        logger.error(f"❌ Job {job_id} Failed: {e}")
        job_manager.update_job(job_id, status="failed", message=str(e))

@app.post("/api/analyze")
async def analyze_pr(request: Request, background_tasks: BackgroundTasks):
    """
    Async Analysis Endpoint. Returns job_id.
    """
    try:
        data = await request.json()
        logger.info(f"Received Analysis Request for PR #{data.get('pr_number')}")
        
        job_id = job_manager.create_job()
        
        # Start background task
        background_tasks.add_task(run_analysis_task, job_id, data)
        
        return JSONResponse(content={"status": "queued", "job_id": job_id})
    except Exception as e:
        logger.error(f"Analysis Queue Failed: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)
    return JSONResponse(content=job)

@app.post("/api/review")
async def review_pr(request: Request):
    """
    Explicit review trigger (if separated from analyze).
    """
    return await analyze_pr(request)

@app.post("/api/comment")
async def post_comment(request: Request):
    # TODO: Implement GitHub comment posting
    return JSONResponse(content={"status": "success", "message": "Comment posted (Mock)"})

@app.get("/api/logs")
async def get_logs():
    """Returns captured log messages for the System Logs tab."""
    return {"logs": log_store.get_logs()}

@app.get("/api/status")
async def get_status():
    return {
        "progress": status_store.progress,
        "message": status_store.message,
        "state": status_store.state
    }

@app.get("/api/graph")
async def get_graph(project_name: str | None = None):
    """
    Returns the full graph JSON for visualization.
    Optional: ?project_name=X to filter.
    """
    try:
        # Pass project_name to get_graph_data if supported
        # For now, export everything or we need to implement filter in PRService
        graph_data = router_service.get_graph_data(project_name=project_name)
        return JSONResponse(content=graph_data)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/projects")
async def list_projects():
    """Calculated list of available projects in Memgraph."""
    projects = router_service.list_projects()
    return {"projects": projects}

@app.get("/api/vectors")
async def get_vectors(project_name: str | None = None):
    """
    Returns 3D PCA-reduced vectors for visualization.
    """
    try:
        # Lazy imports to avoid startup errors if deps missing
        from codebase_rag.vector_store import get_qdrant_client
        from codebase_rag.config import settings
        from sklearn.decomposition import PCA
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        from codebase_rag.constants import PAYLOAD_PROJECT_NAME

        client = get_qdrant_client()
        
        scroll_filter = None
        if project_name:
            scroll_filter = Filter(
                must=[
                    FieldCondition(
                        key=PAYLOAD_PROJECT_NAME,
                        match=MatchValue(value=project_name)
                    )
                ]
            )

        # Fetch up to 5k points - filtered if project_name provided
        points, _ = client.scroll(
            collection_name=settings.QDRANT_COLLECTION_NAME,
            limit=5000,
            with_payload=True,
            with_vectors=True,
            scroll_filter=scroll_filter
        )

        if not points:
            return JSONResponse(content={"nodes": []})

        # Extract vectors
        vectors = [p.vector for p in points]
        
        # Reduce dimensions to 3D
        pca = PCA(n_components=3)
        reduced = pca.fit_transform(vectors)
        
        # Format for frontend
        data = []
        for i, point in enumerate(points):
            payload = point.payload or {}
            data.append({
                "id": point.id,
                "x": float(reduced[i][0]),
                "y": float(reduced[i][1]),
                "z": float(reduced[i][2]),
                "label": payload.get("qualified_name", str(point.id)),
                "group": "Function",
                "file": payload.get("file_path", "")
            })
            
        return JSONResponse(content={"nodes": data})
        
    except Exception as e:
        logger.error(f"Vector Visual Error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


# Serve Static Files (Frontend)
# Ensure the directory exists
web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("codebase_rag.api:app", host="0.0.0.0", port=8000, reload=True)

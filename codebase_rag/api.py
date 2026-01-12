from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from pathlib import Path
from loguru import logger
from codebase_rag.services.pr_service import PRService

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

@app.post("/api/analyze")
async def analyze_pr(request: Request):
    """
    Analyzes the PR changes. 
    Accepts: { "repo_url": str, "pr_number": int, "changes": list }
    """
    try:
        data = await request.json()
        logger.info(f"Received Analysis Request for PR #{data.get('pr_number')}")
        
        # Trigger the Router
        # The frontend expects a review text in the response? 
        # Or does it poll? The app.js seemed to wait for response.
        
        review_result = await router_service.route_and_review(data)
        logger.info(f"✅ Analysis Complete. Sending response (Length: {len(str(review_result))})")
        return JSONResponse(content={"status": "success", "review": review_result})
    except Exception as e:
        logger.error(f"Analysis Failed: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

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
    # TODO: Stream logs. For now return empty.
    return {"logs": []}


# Serve Static Files (Frontend)
# Ensure the directory exists
web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("codebase_rag.api:app", host="0.0.0.0", port=8000, reload=True)

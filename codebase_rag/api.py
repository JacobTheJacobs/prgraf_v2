from pathlib import Path
from collections import deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from codebase_rag.services.pr_service import PRService


class LogStore:
    def __init__(self, max_logs=200):
        self.logs = deque(maxlen=max_logs)

    def sink(self, message):
        record = message.record
        self.logs.append(
            {
                "timestamp": record["time"].strftime("%H:%M:%S"),
                "level": record["level"].name,
                "message": record["message"],
            }
        )

    def get_logs(self):
        return list(self.logs)


app = FastAPI(title="Blast-Radius PR Reviewer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

log_store = LogStore()
logger.add(log_store.sink, format="{message}", level="INFO")
review_service = PRService()


@app.post("/api/analyze")
async def analyze_pr(request: Request):
    try:
        data = await request.json()
        review = review_service.route_and_review(data)
        return {"status": "ok", "review": review}
    except Exception as exc:
        logger.error(f"Review failed: {exc}")
        return JSONResponse(
            content={"status": "error", "message": str(exc)},
            status_code=500,
        )


@app.get("/api/logs")
async def get_logs():
    return {"logs": log_store.get_logs()}


web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")


def main():
    uvicorn.run("codebase_rag.api:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()

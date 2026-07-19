"""prgraf — one app.

FastAPI backend that builds the graph, reviews a range, and serves the
blast-radius UI. No LLM, no external services; everything runs locally off
the SQLite graph.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from codebase_rag.graph.build import build_graph
from codebase_rag.graph.review import format_report, review_range
from codebase_rag.graph.store import GraphStore, default_db_path


class LogStore:
    def __init__(self, max_logs: int = 200) -> None:
        self.logs: deque = deque(maxlen=max_logs)

    def sink(self, message) -> None:
        record = message.record
        self.logs.append({
            "timestamp": record["time"].strftime("%H:%M:%S"),
            "level": record["level"].name,
            "message": record["message"],
        })

    def get_logs(self) -> list:
        return list(self.logs)


app = FastAPI(title="prgraf — blast-radius review")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

log_store = LogStore()
logger.add(log_store.sink, format="{message}", level="INFO")


def _render_graph(result, cap: int) -> dict:
    """Trim the subgraph to what reads well: all seeds + top-N by impact.

    The full radius can be many hundreds of nodes; showing every one turns
    the picture into a hairball and makes the layout O(n^2) expensive. Seeds
    are always kept (they are the change itself); the rest are the highest-
    impact nodes, which is exactly what a reviewer should look at first.
    """
    sub = result.subgraph
    seeds = set(sub.get("seeds", []))
    scores = sub.get("impact_scores", {})
    nodes = sub.get("nodes", [])

    ranked = sorted(
        (n for n in nodes if n["qualified_name"] not in seeds),
        key=lambda n: -scores.get(n["qualified_name"], 0.0),
    )
    keep = seeds | {n["qualified_name"] for n in ranked[: max(0, cap)]}

    kept_nodes = [n for n in nodes if n["qualified_name"] in keep]
    kept_edges = [
        e for e in sub.get("edges", [])
        if e["source"] in keep and e["target"] in keep
    ]
    return {
        "seeds": sorted(seeds),
        "nodes": kept_nodes,
        "edges": kept_edges,
        "impact_scores": {qn: s for qn, s in scores.items() if qn in keep},
        "shown": len(kept_nodes),
        "total": len(nodes),
    }


def _resolve_repo(raw: str | None) -> Path:
    repo = Path(raw or ".").expanduser().resolve()
    if not repo.exists():
        raise ValueError(f"repo path does not exist: {repo}")
    return repo


@app.post("/api/build")
async def build(request: Request):
    """Build or refresh the graph for a repo."""
    try:
        data = await request.json()
        repo = _resolve_repo(data.get("repo_path"))
        stats = build_graph(repo, db_path=default_db_path(repo), full=bool(data.get("full")))
        return {"status": "ok", "stats": stats}
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        logger.error(f"Build failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.post("/api/review")
async def review(request: Request):
    """Review a range. Returns the full blast-radius subgraph for rendering."""
    try:
        data = await request.json()
        repo = _resolve_repo(data.get("repo_path"))
        db = default_db_path(repo)
        if not db.exists():
            logger.info("No graph yet — building before review")
            build_graph(repo, db_path=db, full=True)

        base = data.get("base") or "HEAD~1"
        head = data.get("head") or "HEAD"
        render_cap = int(data.get("render_cap") or 160)
        result = review_range(repo, base=base, head=head, db_path=db)

        return {
            "status": "ok",
            "report": format_report(result),
            "overall_risk": result.overall,
            "findings": [
                {
                    "symbol": f.node.qualified_name,
                    "name": f.node.name,
                    "kind": f.node.kind,
                    "severity": f.severity,
                    "level": f.level,
                    "risk": f.score,
                    "location": f"{f.node.file_path}:{f.node.line_start}",
                    "reasons": f.factors.reasons(),
                    "impacted": f.impacted_count,
                    "impacted_files": f.impacted_files,
                    "tests": f.test_count,
                    "callers": f.caller_count,
                    "top_impacted": f.top_impacted,
                }
                for f in result.findings
            ],
            "changed_files": result.changed_files,
            "graph": _render_graph(result, render_cap),
            "truncated": result.truncated,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Review failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.post("/api/export")
async def export(request: Request):
    """Assemble a single self-contained HTML snapshot of a review.

    Inlines style.css, app.js, and the review payload into one file that
    renders offline with no server — the same app.js drives it in embedded
    mode, so there is no second renderer to keep in sync.
    """
    try:
        data = await request.json()
        payload = data.get("payload")
        if not payload:
            raise ValueError("export needs a review payload")

        web = Path(__file__).parent / "web"
        index_html = (web / "index.html").read_text(encoding="utf-8")
        css = (web / "style.css").read_text(encoding="utf-8")
        js = (web / "app.js").read_text(encoding="utf-8")

        payload = dict(payload)
        payload.setdefault("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M"))
        # </script> inside JSON would close the tag early; escape it.
        data_json = json.dumps(payload).replace("</", "<\\/")

        html = index_html.replace(
            '<link rel="stylesheet" href="style.css">',
            f"<style>\n{css}\n</style>",
        ).replace(
            '<script src="app.js"></script>',
            f"<script>window.__PRGRAF_DATA__ = {data_json};</script>\n"
            f"<script>\n{js}\n</script>",
        )
        return HTMLResponse(content=html)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Export failed: {exc}")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.post("/api/status")
async def status(request: Request):
    try:
        data = await request.json()
        repo = _resolve_repo(data.get("repo_path"))
        db = default_db_path(repo)
        if not db.exists():
            return {"status": "ok", "built": False}
        with GraphStore(db) as store:
            return {"status": "ok", "built": True, "stats": store.stats()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/logs")
async def get_logs():
    return {"logs": log_store.get_logs()}


web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")


def main() -> None:
    uvicorn.run("codebase_rag.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

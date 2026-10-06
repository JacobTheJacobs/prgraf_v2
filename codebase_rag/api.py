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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from codebase_rag.graph.build import build_graph
from codebase_rag.graph.pr import current_ref, parse_pr_url, prepare_pr, restore_checkout
from codebase_rag.graph.review import review_range, web_payload
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

# The UI is served from this same server, so no cross-origin access is needed.
# Wildcard CORS let any website a user visited drive this API: clone repos,
# check out their local clone, index arbitrary paths, read the logs.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _host_of(value: str) -> str:
    host = value.split("://", 1)[-1].split("/", 1)[0]
    return host if host.startswith("[") and host.endswith("]") else host.rsplit(":", 1)[0]


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Refuse requests from other sites, and DNS-rebinding hostnames.

    A missing Origin is allowed (curl, same-origin GET). `null` is not: a
    sandboxed iframe on any site sends it. The HTML export never calls the API.
    """
    host = _host_of(request.headers.get("host", ""))
    origin = request.headers.get("origin")
    if host not in _LOCAL_HOSTS or (origin is not None and _host_of(origin) not in _LOCAL_HOSTS):
        return JSONResponse({"status": "error", "message": "forbidden origin"}, status_code=403)
    return await call_next(request)


log_store = LogStore()
logger.add(log_store.sink, format="{message}", level="INFO")


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
        raw = (data.get("repo_path") or "").strip()
        base = data.get("base") or "HEAD~1"
        head = data.get("head") or "HEAD"
        pr_slug = None

        # One field accepts either a local path or a GitHub PR URL; a PR just
        # resolves to a range, so the rest of the pipeline is unchanged.
        pr = parse_pr_url(raw) or parse_pr_url(data.get("pr_url") or "")
        restore_to = None
        restore_repo = None
        if pr:
            # Reviewing a PR checks the tree out at the PR head. When that tree
            # is the user's own clone we must put it back, exactly as the CLI
            # does — leaving it detached silently orphans any commit they make
            # next. Capture the ref BEFORE anything moves.
            local_raw = data.get("local_repo")
            if local_raw:
                local = Path(local_raw).expanduser().resolve()
                if (local / ".git").exists():
                    restore_to = current_ref(local)
                    restore_repo = local
            repo, base, head = prepare_pr(pr, local_repo=local_raw)
            if restore_repo != repo:
                restore_to = None  # a cached clone, not the user's checkout
            pr_slug = pr.slug
        else:
            repo = _resolve_repo(raw)

        # Always refresh: the build is hash-incremental and cheap, and a graph
        # left from a different checkout has stale line numbers, which silently
        # maps the diff onto the wrong symbols.
        db = default_db_path(repo)
        render_cap = int(data.get("render_cap") or 160)
        try:
            # Inside the try: a failed build must still put the user's clone
            # back, or it is left detached on the PR head.
            build_graph(repo, db_path=db)
            result = review_range(repo, base=base, head=head, db_path=db)
        finally:
            if restore_to and restore_repo:
                restore_checkout(restore_repo, restore_to)
        payload = web_payload(result, cap=render_cap)
        payload["repo"] = str(repo)
        payload["base"] = base
        payload["head"] = head
        if pr_slug:
            payload["pr"] = pr_slug
        return payload
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

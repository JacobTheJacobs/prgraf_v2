"""Print a review payload as JSON on stdout.

The VSCode extension spawns this, injects the JSON into its webview, and the
same app.js renders it in embedded mode. No server, no second renderer.

    python -m codebase_rag.graph.export_cli --repo . --base HEAD~1 --head HEAD
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loguru import logger

from .build import build_graph
from .review import review_range, web_payload
from .store import default_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prgraf-review-json")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="HEAD~1")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--cap", type=int, default=160)
    args = parser.parse_args(argv)

    # Logs must not pollute stdout — the extension parses stdout as JSON.
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    repo = Path(args.repo).resolve()
    db = default_db_path(repo)
    try:
        build_graph(repo, db_path=db)  # incremental after first build
        result = review_range(repo, base=args.base, head=args.head, db_path=db)
        sys.stdout.write(json.dumps(web_payload(result, cap=args.cap)))
        return 0
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"status": "error", "message": str(exc)}))
        logger.error(f"review-json failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

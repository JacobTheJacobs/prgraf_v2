# Architecture

`prgraf` is intentionally small. Primary path is **CLI on every PR** via the template workflow.

```text
GitHub pull_request (opened / synchronize / …)
  -> template/.github/workflows/pr-blast-radius.yml
     -> prgraf --base origin/$BASE --head HEAD
        -> RepoFetcher.fetch_range_diff
        -> PocketStrategyRouter
           -> StructuralTriage
           -> deleted-file reference scan
           -> touched-symbol reference scan
        -> prgraf-report.md + PR comment

Optional web:
POST /api/analyze
  -> PRService
     -> RepoFetcher (range | remote PR | supplied changes)
     -> PocketStrategyRouter
  -> concise Pre-Landing Review
```

## Review Rules

- Deleted files: report surviving files that still reference deleted paths.
- Logic changes: report touched functions/classes that are referenced from other files.
- Critical paths: auth, billing, db, migration, payment, permission, security, session, token.
- Output: maximum 3 findings, each with severity, confidence, file:line, and one reason line.

## Graph engine (default)

`--engine graph` (the default) builds a real code graph and reviews against it:

```
prgraf --base origin/main --head HEAD
  -> build_graph            tree-sitter extract -> SQLite (nodes, edges)
     -> link_graph          resolve bare call/import targets to qualified names
  -> review_range
     -> diff -> SYMBOL-level seeds   (overlap changed lines with node ranges)
     -> get_impact_radius            bidirectional, score x weight x 0.6 decay
     -> risk score per seed          two axes: reach and risk
  -> findings -> report + PR comment
```

Same engine backs the web UI (`codebase_rag/api.py` + `web/`) and the MCP server
(`codebase_rag/graph/mcp_server.py`). The web UI renders impact score as radial
distance and risk as color; it can export a self-contained HTML snapshot.

`--engine heuristic` keeps the original regex reviewer (below) as a fallback.

## Non-Goals

- No external graph database (SQLite only).
- No vector database.
- No in-product LLM calls (the MCP host agent writes the review).
- No generated multi-tab dashboard.

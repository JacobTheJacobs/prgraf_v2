# Architecture

`prgraf` answers one question: **what might break when this change lands?**
It builds a code graph, seeds it with the symbols a diff actually touched, and
scores what those symbols reach.

```text
prgraf --base origin/main --head HEAD
  -> build_graph                tree-sitter extract -> SQLite (nodes, edges)
     -> link_graph              resolve bare call/import targets to qualified names
  -> review_range
     -> diff -> SYMBOL seeds    overlap changed lines with node line ranges
     -> get_impact_radius       bidirectional, score x edge weight x 0.6 decay
     -> risk score per seed     two axes: reach and risk
  -> findings -> report + PR comment
```

Seeding at symbol level rather than file level is the whole game: on a real
repo a file-level seed pulled 1393 impacted nodes (noise) where the symbol-level
seed for the same edit pulled 167 (signal).

## Surfaces

One engine, four hosts — none of them re-implement the graph or the renderer.

| Surface | Entry | Notes |
|---|---|---|
| CLI | `codebase_rag/cli.py` | what the PR template workflow runs |
| Web app | `codebase_rag/api.py` + `web/` | radial blast-radius UI, HTML export |
| MCP | `codebase_rag/graph/mcp_server.py` | 7 tools, `detail_level` tiering |
| VS Code | `vscode-extension/` | webview over the *same* `web/app.js` |

The UI renders impact score as radial distance (rings: direct, ~2 hops, …) and
risk as color.

## Review rules

- Changed symbols are found by overlapping diff hunks with node line ranges;
  edits outside any symbol fall back to the file node.
- Risk is additive and explainable: test coverage, security sensitivity,
  caller count, blast breadth, cross-file spread.
- File-node reach and test-file symbols are damped — structural reach is not
  the same as risk.
- Output: a few findings, each with severity, risk, file:line, and reasons.

## Non-goals

- No external graph database (SQLite only).
- No vector database.
- No in-product LLM calls. Narration is optional and additive: an MCP host
  agent, or one small editor-model call that sees only computed graph facts —
  never the diff.
- No second renderer. The web app, the HTML export, and the VS Code webview all
  load the same `web/app.js`.

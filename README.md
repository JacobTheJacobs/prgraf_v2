# prgraf

Low-noise **blast-radius** PR reviewer: what is most likely to break when a PR lands.

Built on a real code graph — tree-sitter symbol extraction into SQLite, with a
bounded bidirectional impact traversal. One self-contained app: a CLI (what CI
runs), a FastAPI web UI with an Obsidian-style blast-radius graph, and an MCP
server so an agent can review from a token-minimal context packet.

Surfaces:
- **PR template** — runs on every PR upload (GitHub Actions), posts a comment.
- **Web UI** — `prgraf-web`, paste a repo + range, see the blast radius; export a
  self-contained HTML snapshot.
- **MCP** — `prgraf-mcp`, graph tools with `detail_level` tiering.

No LLM calls in-product, no vector search, no external graph database. Narration
is optional and additive — an MCP host agent, or one small editor-model call
that sees only computed graph facts, never your diff.

## Use as a PR template (recommended)

Copy the workflow + this package into any repo once. After that, every PR open/update posts a short review comment.

See **[template/README.md](template/README.md)** for the full copy steps.

Quick shape:

```text
your-repo/
  .github/workflows/pr-blast-radius.yml
  tools/prgraf/          ← this package
```

Workflow triggers: `pull_request` → `opened` | `synchronize` | `reopened` | `ready_for_review`.

## CLI (local or CI)

```bash
pip install -e .
prgraf --base origin/main --head HEAD
```

Useful flags:

```bash
prgraf --base origin/main --head HEAD -o prgraf-report.md
prgraf --fail-on p1          # exit 1 on P0/P1
prgraf --quiet               # report only
```

In GitHub Actions, `GITHUB_BASE_REF` / `GITHUB_WORKSPACE` are picked up automatically when you pass `--base origin/$GITHUB_BASE_REF`.

## Optional web UI

```bash
pip install -e ".[web]"
prgraf-web
# or: uvicorn codebase_rag.api:app --reload
```

Open `http://localhost:8000` and paste a GitHub PR URL.

## API (web mode)

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "content-type: application/json" \
  -d '{"repo_url":"https://github.com/org/repo.git","pr_number":123}'
```

Or pass changes / a local range:

```json
{"repo_path": ".", "base": "origin/main", "head": "HEAD"}
```

## Output shape

```text
Pre-Landing Review: 2 blast-radius finding(s)
- [P2] (confidence: 8/10) `app/auth.py:1` - `login` changed and is referenced by 3 file(s): ...
  Review caller contracts first; this is the real runtime blast radius.
```

## Layout

| Path | Role |
|------|------|
| `codebase_rag/graph/` | **The engine** — extract, link, impact, risk, review |
| `codebase_rag/cli.py` | **CLI** — what CI runs |
| `template/.github/workflows/` | **PR template** workflow |
| `codebase_rag/api.py` | FastAPI server for the web app |
| `codebase_rag/web/` | The UI — one renderer, shared by every surface |
| `codebase_rag/graph/mcp_server.py` | MCP server (7 tools) |
| `vscode-extension/` | VS Code webview over the same `web/app.js` |
| `tests/test_graph_engine.py` | Engine tests |

## Verify

```bash
python -m py_compile codebase_rag/cli.py codebase_rag/api.py codebase_rag/graph/*.py
python tests/test_graph_engine.py               # 11 engine tests
prgraf --repo . --base HEAD~1 --quiet           # real review
prgraf-web                                      # UI on http://localhost:8000
```

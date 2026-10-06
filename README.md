# prgraf

PR reviewer that tells you what is most likely to break when a change lands.

## How it works

1. **Parse** — [tree-sitter](https://tree-sitter.github.io/) parses every source
   file into an AST (Python, JS/TS, Go).
2. **Graph** — functions, classes and files become nodes; calls, imports,
   inheritance and test coverage become edges. Stored in SQLite.
3. **Diff** — changed lines are mapped to the symbols that contain them, and
   removed symbols are found by re-parsing the old side.
4. **Blast radius** — walk the graph out from each changed symbol (2 hops,
   weighted by edge type) to find what it reaches.
5. **Risk** — score each change on test coverage, security-sensitive names,
   caller count and reach. Report the top 3.

No LLM, no vector DB, no graph database. Deterministic.

## Install

```bash
pip install -e .              # CLI
pip install -e ".[web,mcp]"   # + web UI and MCP server
```

Requires Python 3.12+.

## Use

```bash
prgraf --base origin/main                 # review this branch
prgraf --uncommitted --base HEAD          # review uncommitted work
prgraf --pr https://github.com/o/r/pull/1 # review a GitHub PR
prgraf --fail-on p1                       # exit 1 on P0/P1 (for CI)
```

Output:

```text
Pre-Landing Review: 2 finding(s) · overall risk 0.78 (high)
- [P1] (risk 0.78) `core.py:10` - `token` was removed but 1 caller(s) still reference it.
  removed but still called.
  Check first: route, api.py
- [P2] (risk 0.56) `core.py:1` - `login` impacts 4 symbol(s) across 3 file(s).
  no direct test coverage, security- or money-sensitive surface.
```

## Other surfaces

| Surface | Run | What |
|---|---|---|
| PR comment | copy [template/](template/README.md) into your repo | GitHub Action, comments on every PR |
| Web UI | `prgraf-web` → http://localhost:8000 | paste a repo path or PR URL, see the graph |
| MCP | `prgraf-mcp` | 7 graph tools for an agent |
| VS Code | [vscode-extension/](vscode-extension/README.md) | same graph inside the editor |

## Layout

| Path | Role |
|---|---|
| `codebase_rag/graph/` | engine: extract, link, diff, risk, review |
| `codebase_rag/cli.py` | CLI |
| `codebase_rag/api.py`, `web/` | web server and UI |
| `vscode-extension/` | VS Code extension |
| `template/` | GitHub Actions workflow |
| `tests/` | `python -m pytest tests` |

Design notes: [architecture.md](architecture.md).

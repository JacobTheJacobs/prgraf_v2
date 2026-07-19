# prgraf

Low-noise **blast-radius** PR reviewer: what is most likely to break when a PR lands.

Primary use: **template that runs on every PR upload** (GitHub Actions).  
Optional: local CLI, and a small web UI.

No LLMs, vector search, Memgraph, or Tree-sitter.

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
| `codebase_rag/cli.py` | **CLI** — what CI runs |
| `template/.github/workflows/` | **PR template** workflow |
| `codebase_rag/services/pocket_router.py` | Blast-radius rules |
| `codebase_rag/services/pr_review/` | Triage + git fetch/diff |
| `codebase_rag/api.py` | Optional FastAPI + static UI |
| `codebase_rag/web/` | Optional UI |

## Verify

```bash
python -m py_compile codebase_rag/cli.py codebase_rag/api.py codebase_rag/services/pr_service.py codebase_rag/services/pocket_router.py codebase_rag/services/pr_review/analyzer.py codebase_rag/services/pr_review/fetcher.py
python test_pr_review.py
python test_real_repo_pr.py
prgraf --base HEAD --head HEAD --quiet   # empty range smoke
```

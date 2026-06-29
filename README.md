# prgraf

Low-noise PR reviewer focused on blast radius: what is most likely to break when a PR lands.

It does not use LLMs, vector search, Memgraph, or Tree-sitter. It checks changed files, deleted-file references, and touched functions/classes that are still referenced elsewhere.

## Run

```bash
uvicorn codebase_rag.api:app --reload
```

Open `http://localhost:8000` and paste a GitHub PR URL.

## API

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "content-type: application/json" \
  -d '{"repo_url":"https://github.com/org/repo.git","pr_number":123}'
```

You can also pass already-fetched changes:

```json
{
  "changes": [
    {
      "file": "app/auth.py",
      "old_content": "def login(user):\n    return token(user)\n",
      "new_content": "def login(user):\n    if not user.active:\n        return None\n    return token(user)\n"
    }
  ]
}
```

## Output Shape

```text
Pre-Landing Review: 2 blast-radius finding(s)
- [P2] (confidence: 8/10) `app/auth.py:1` - `login` changed and is referenced by 3 file(s): app/main.py, app/routes.py, tests/test_auth.py.
  Review caller contracts first; this is the real runtime blast radius.
```

## Files

- `codebase_rag/api.py` - FastAPI endpoint and static UI mount.
- `codebase_rag/services/pr_service.py` - Fetches a PR or accepts supplied changes.
- `codebase_rag/services/pocket_router.py` - Blast-radius finding logic.
- `codebase_rag/services/pr_review/analyzer.py` - Cheap file triage.
- `codebase_rag/services/pr_review/fetcher.py` - Git fetch/diff helper.
- `codebase_rag/web/` - Tiny static UI.

## Verify

```bash
python -m py_compile codebase_rag/api.py codebase_rag/services/pr_service.py codebase_rag/services/pocket_router.py codebase_rag/services/pr_review/analyzer.py codebase_rag/services/pr_review/fetcher.py
python test_pr_review.py
python test_real_repo_pr.py
```
